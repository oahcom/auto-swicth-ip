#!/usr/bin/env python3
"""IP 轮换健康验证 — 7890 端口 / IP 切换 / 切换合理性。

用法:
    python3 verify_rotation.py              # 全量（约 6-8 分钟）
    python3 verify_rotation.py --quick      # 快速（约 30s，跳过全池扫描）
    python3 verify_rotation.py --samples 20 # 指定逐节点采样数

只读验证，除「切换测试」外不改变 sing-box 状态；退出码非 0 表示有检查项失败。

背景：2026-10-16 换订阅 token 时，以下 5 个 bug 曾使订阅刷新静默失效。
本脚本是端点侧的验证；解析侧回归见 test_subscription_parse.py。
"""
import argparse
import collections
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time

import requests

from config import get_singbox_secret

CLASH_API = "http://127.0.0.1:9090"
PROXY = "http://127.0.0.1:7890"
IP_URL = "https://api.ipify.org"
GEO_URL = "https://ipinfo.io/json"
PROBE_URL = "https://www.gstatic.com/generate_204"

PASS, FAIL = 0, 0
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  ❌ {name}" + (f": {detail}" if detail else ""))
    return ok


def hdr():
    return {"Authorization": f"Bearer {get_singbox_secret()}"}


def nodes() -> list[str]:
    r = requests.get(f"{CLASH_API}/proxies/proxy", headers=hdr(), timeout=10)
    return [n for n in r.json()["all"] if n != "direct"]


def current() -> str:
    r = requests.get(f"{CLASH_API}/proxies/proxy", headers=hdr(), timeout=10)
    return r.json().get("now", "")


def switch(node: str) -> bool:
    r = requests.put(f"{CLASH_API}/proxies/proxy", json={"name": node},
                     headers=hdr(), timeout=10)
    return r.status_code == 204


def curl(args: list[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(["curl", "-s", "-m", str(timeout)] + args,
                           capture_output=True, text=True, timeout=timeout + 5)
        return r.stdout.strip()
    except Exception:
        return ""


def egress_ip() -> str:
    out = curl(["-x", PROXY, IP_URL], timeout=12)
    return out if out and "." in out and "<" not in out else "FAIL"


def journal(since: str, pattern: str) -> list[str]:
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", "auto-switch-ip.service",
             "--since", since, "--no-pager"],
            capture_output=True, text=True, timeout=30)
        return [l for l in r.stdout.splitlines() if re.search(pattern, l)]
    except Exception:
        return []


# ============= 1. 7890 端口正常性 =============
def verify_port():
    print("\n[1] 7890 端口正常性")
    r = subprocess.run(["ss", "-ltn"], capture_output=True, text=True)
    check("7890 监听中", ":7890" in r.stdout)
    check("9090 Clash API 监听中", ":9090" in r.stdout)

    proc = subprocess.run(["pgrep", "-x", "sing-box"], capture_output=True, text=True)
    check("sing-box 进程存活", bool(proc.stdout.strip()))

    for label, args in [
        ("HTTP", ["-x", PROXY, "-o", "/dev/null", "-w", "%{http_code}",
                  "http://www.gstatic.com/generate_204"]),
        ("HTTPS", ["-x", PROXY, "-o", "/dev/null", "-w", "%{http_code}", PROBE_URL]),
        ("CONNECT 隧道", ["-x", PROXY, "-o", "/dev/null", "-w", "%{http_code}",
                          "https://api.github.com"]),
    ]:
        code = curl(args, timeout=15)
        check(f"{label} 经 7890 可用", code in ("200", "204"), f"HTTP {code}")

    dn = curl(["-x", PROXY, "https://1.1.1.1/dns-query?name=example.com&type=A",
               "-H", "accept: application/dns-json"], timeout=12)
    check("DNS over 7890 解析正常", dn.startswith("{") and '"Status":0' in dn, dn[:60])

    size = curl(["-x", PROXY, "-o", "/dev/null", "-w", "%{http_code} %{size_download}",
                 "https://speed.cloudflare.com/__down?bytes=100000"], timeout=25)
    check("大文件传输经 7890 正常", size.startswith("200"), size)

    code = curl(["-o", "/dev/null", "-w", "%{http_code}",
                 "http://127.0.0.1:20128/v1/models"], timeout=8)
    check("9router 网关可达", code == "200", f"HTTP {code}")


# ============= 2. IP 切换正常性 =============
def verify_switch(samples: int, quick: bool) -> list[str]:
    print("\n[2] IP 切换正常性")
    all_nodes = nodes()
    check("节点池非空", len(all_nodes) > 10, f"实际={len(all_nodes)}")

    # 连续强制切换，观察出口 IP 是否真的改变
    pool = [n for n in all_nodes if not n.endswith("-h2")]  # h2 在 WSL2 被过滤
    step = max(1, len(pool) // 5)
    picks = pool[::step][:5]
    seen: list[tuple[str, str]] = []
    for n in picks:
        if not switch(n):
            continue
        time.sleep(2)
        seen.append((n, egress_ip()))
    ips = [ip for _, ip in seen if ip != "FAIL"]
    print(f"  切换采样: {[(n.split(':')[1], ip) for n, ip in seen]}")
    check("切换后出口 IP 可获取", len(ips) >= 3, f"有效={len(ips)}/{len(seen)}")
    check("连续切换出口 IP 互不相同", len(set(ips)) == len(ips),
          f"唯一={len(set(ips))}/{len(ips)}")

    if quick:
        print("  (--quick: 跳过全池扫描)")
        return all_nodes

    # 逐节点采样：唯一出口 IP 比例
    n_sample = min(samples, len(pool))
    uniq = set()
    got = 0
    for n in pool[:n_sample]:
        if switch(n):
            time.sleep(0.8)
            ip = egress_ip()
            if ip != "FAIL":
                got += 1
                uniq.add(ip)
    print(f"  逐节点采样 {n_sample} 个: 有效 {got}, 唯一 IP {len(uniq)}")
    check("逐节点出口 IP 唯一率 >80%", got > 0 and len(uniq) / got > 0.8,
          f"{len(uniq)}/{got}")
    return all_nodes


# ============= 3. 切换合理性 =============
def verify_reasonableness(all_nodes: list[str], quick: bool):
    print("\n[3] 切换合理性")

    # 3.1 轮换节律符合配置
    import config
    want = config.CFG["intervals"]["proactive_rotate_sec"]
    lines = journal("30 minutes ago", "主动轮换")
    ts = [re.search(r"\[([\d-]+ [\d:]+)\]", l) for l in lines]
    stamps = []
    from datetime import datetime
    for m in ts:
        if m:
            try:
                stamps.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                pass
    if len(stamps) > 3:
        gaps = [(stamps[i + 1] - stamps[i]).total_seconds() for i in range(len(stamps) - 1)]
        # 取中位数：重启/人为测试会引入离群值
        med = sorted(gaps)[len(gaps) // 2]
        check(f"轮换间隔中位数≈{want}s (配置)", abs(med - want) <= want * 0.5,
              f"中位数={med:.0f}s, 样本={len(gaps)}")
        print(f"  全部间隔: {[int(g) for g in gaps]}")
    else:
        print(f"  (轮换样本不足: {len(stamps)} 次，跳过节律检查)")

    # 3.2 切换避让死节点
    log = journal("10 minutes ago", "切代理")
    picked = set()
    for l in log:
        m = re.search(r"-> (\S+)", l)
        if m:
            picked.add(m.group(1))
    dead = journal("10 minutes ago", r"死 \(连续失败|无可用节点")
    dead_nodes = set()
    for l in dead:
        m = re.search(r"(\S+?) 死", l)
        if m:
            dead_nodes.add(m.group(1))
    check("切换目标不含死节点", not (picked & dead_nodes),
          f"交集={picked & dead_nodes}")
    print(f"  近期切换目标 {len(picked)} 个, 死节点 {len(dead_nodes)} 个")

    # 3.3 策略模式判定正确
    strat = set(re.findall(r"策略=(\w+)", "\n".join(
        journal("5 minutes ago", "策略="))))
    check("策略模式已判定", bool(strat), f"实际={strat or '无日志'}")

    # 3.4 错误检测范围（只查 opencode）
    errs = journal("10 minutes ago", "OpenCode Free")
    print(f"  opencode 错误检测日志: {len(errs)} 条")

    # 3.5 daemon 无异常
    if not quick:
        bad = journal("5 minutes ago", "Traceback|UnboundLocalError|主循环异常")
        check("daemon 主循环无异常", not bad, f"{len(bad)} 条")
        check("daemon 服务 active", subprocess.run(
            ["systemctl", "--user", "is-active", "auto-switch-ip.service"],
            capture_output=True, text=True).stdout.strip() == "active")

    # 3.6 地理多样性（订阅绕限速的核心价值）
    if not quick:
        geo = collections.Counter()
        sample = [n for n in all_nodes if not n.endswith("-h2")][:30]
        for n in sample:
            if switch(n):
                time.sleep(0.5)
                out = curl(["-x", PROXY, GEO_URL], timeout=12)
                try:
                    geo[json.loads(out).get("country", "?")] += 1
                except Exception:
                    geo["FAIL"] += 1
        countries = [c for c in geo if c not in ("FAIL", "?")]
        print(f"  采样 {len(sample)} 节点 → {len(countries)} 国: {dict(geo.most_common(8))}")
        check("出口国家数 ≥ 3", len(countries) >= 3, f"实际={len(countries)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过全池扫描（约 30s）")
    ap.add_argument("--samples", type=int, default=20, help="逐节点采样数")
    args = ap.parse_args()

    print("=" * 60)
    print("auto-switch-ip 轮换健康验证")
    print(f"代理: {PROXY}  Clash API: {CLASH_API}  模式: {'quick' if args.quick else 'full'}")
    print("=" * 60)

    t0 = time.time()
    verify_port()
    all_nodes = verify_switch(args.samples, args.quick)
    verify_reasonableness(all_nodes, args.quick)

    print("\n" + "=" * 60)
    print(f"结果: {PASS} 通过 / {FAIL} 失败  ({time.time() - t0:.0f}s)")
    if FAILURES:
        print("失败项:")
        for f in FAILURES:
            print(f"  - {f}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
