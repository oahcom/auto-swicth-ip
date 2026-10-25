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
import json
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

from asip.clash_client import ClashClient
from asip.egress import probe_egress
from asip.proc import run as proc_run, run_curl
from config import CFG, get_singbox_secret

# 端点全部取自 config：改端口/selector 时不必再改本脚本。
CLASH_API = CFG["singbox"]["clash_api"].rstrip("/")
SELECTOR = CFG["singbox"]["selector"]
MIXED_PORT = CFG["singbox"]["mixed_port"]
CLASH_PORT = urlparse(CLASH_API).port or 9090
NINE_ROUTER_BASE = CFG["9router"]["base"].rstrip("/")
PROXY = f"http://127.0.0.1:{MIXED_PORT}"
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


class ClashUnreachable(RuntimeError):
    """Clash API 不可达 —— 比裸 traceback 更能说明问题。"""


_clash = ClashClient(CLASH_API, get_singbox_secret, timeout=10)


def _selector() -> dict:
    status, data = _clash.get_json(f"proxies/{SELECTOR}", timeout=10)
    if status is None:
        raise ClashUnreachable(f"Clash API {CLASH_API} 不可达")
    if status != 200 or not isinstance(data, dict):
        raise ClashUnreachable(f"Clash API {CLASH_API} 返回 HTTP {status}")
    return data


def nodes() -> list[str]:
    return [n for n in _selector()["all"] if n != "direct"]


def current() -> str:
    return _selector().get("now", "")


def switch(node: str) -> bool:
    return _clash.select(SELECTOR, node, timeout=10)


def egress_ip() -> str:
    result = probe_egress(http_proxy=PROXY, urls=(IP_URL,), timeout=12)
    return result.ip if result.ip else "FAIL"


def _healthy_nodes(candidates: list[str]) -> list[str]:
    """用 Clash API 的延迟测试筛出当前可用的节点（并发，最多 15s）。"""
    delays = _clash.delays(candidates, workers=16, timeout_ms=5000, hard_timeout=8)
    return [n for n in candidates if delays.get(n) is not None]


def journal(since: str, pattern: str) -> list[str]:
    r = proc_run(
        ["journalctl", "--user", "-u", "auto-switch-ip.service",
         "--since", since, "--no-pager"],
        timeout=10)
    if not r.ok:
        return []
    return [l for l in r.stdout.splitlines() if re.search(pattern, l)]


# ============= 1. 混合端口正常性 =============
def verify_port():
    print(f"\n[1] {MIXED_PORT} 端口正常性")
    r = proc_run(["ss", "-ltn"], timeout=5)
    check(f"{MIXED_PORT} 监听中", f":{MIXED_PORT}" in r.stdout)
    check(f"{CLASH_PORT} Clash API 监听中", f":{CLASH_PORT}" in r.stdout)

    proc = proc_run(["pgrep", "-x", "sing-box"], timeout=5)
    check("sing-box 进程存活", bool(proc.stdout.strip()))

    for label, args in [
        ("HTTP", ["-x", PROXY, "-o", "/dev/null", "-w", "%{http_code}",
                  "http://www.gstatic.com/generate_204"]),
        ("HTTPS", ["-x", PROXY, "-o", "/dev/null", "-w", "%{http_code}", PROBE_URL]),
        ("CONNECT 隧道", ["-x", PROXY, "-o", "/dev/null", "-w", "%{http_code}",
                          "https://api.github.com"]),
    ]:
        code = run_curl(args, timeout=15).stdout.strip()
        check(f"{label} 经 {MIXED_PORT} 可用", code in ("200", "204"), f"HTTP {code}")

    dn = run_curl(["-x", PROXY, "https://1.1.1.1/dns-query?name=example.com&type=A",
                   "-H", "accept: application/dns-json"], timeout=12).stdout.strip()
    check(f"DNS over {MIXED_PORT} 解析正常", dn.startswith("{") and '"Status":0' in dn, dn[:60])

    size = run_curl(["-x", PROXY, "-o", "/dev/null", "-w", "%{http_code} %{size_download}",
                     "https://speed.cloudflare.com/__down?bytes=100000"], timeout=25).stdout.strip()
    check(f"大文件传输经 {MIXED_PORT} 正常", size.startswith("200"), size)

    code = run_curl(["-o", "/dev/null", "-w", "%{http_code}",
                     f"{NINE_ROUTER_BASE}/v1/models"], timeout=8).stdout.strip()
    check("9router 网关可达", code == "200", f"HTTP {code}")


# ============= 2. IP 切换正常性 =============
def verify_switch(samples: int, quick: bool) -> list[str]:
    """切到若干节点验证出口，返回节点池；结束时恢复原选择。

    WHY 必须 finally：本脚本是运维手动跑的诊断工具，中途异常/超时会把线上
    opencode 留在被测试的节点上，后续请求继续打到那个出口 —— 测完不还原
    等于让诊断改变被测系统。
    """
    try:
        original = current()
    except ClashUnreachable:
        original = ""
    try:
        return _verify_switch_body(samples, quick)
    finally:
        if original:
            try:
                if current() != original:
                    print(f"  恢复原选择: {original}")
                    switch(original)
            except ClashUnreachable:
                print("  ⚠ Clash API 不可达，未能恢复原选择")


def _verify_switch_body(samples: int, quick: bool) -> list[str]:
    print("\n[2] IP 切换正常性")
    all_nodes = nodes()
    check("节点池非空", len(all_nodes) > 10, f"实际={len(all_nodes)}")

    # 只挑 Clash API 已确认健康的节点：切到死节点会得到 FAIL，
    # 那是节点问题而非切换功能问题，会污染本项判定
    pool = [n for n in all_nodes if not n.endswith("-h2")]  # h2 在 WSL2 被过滤
    healthy = _healthy_nodes(pool)
    if not healthy:
        print("  ⚠ Clash API 延迟测试无健康节点，回退到未验证节点池")
        healthy = pool
    step = max(1, len(healthy) // 5)
    picks = healthy[::step][:5]

    seen: list[tuple[str, str]] = []
    for n in picks:
        if not switch(n):
            continue
        ip = "FAIL"
        for _ in range(4):                 # 切换后代理就绪需要时间，重试而非一次判定
            time.sleep(1.5)
            ip = egress_ip()
            if ip != "FAIL":
                break
        seen.append((n, ip))
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
    want = CFG["intervals"]["proactive_rotate_sec"]
    lines = journal("30 minutes ago", "主动轮换")
    ts = [re.search(r"\[([\d-]+ [\d:]+)\]", l) for l in lines]
    stamps = []
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
        # 轮换完全停滞时不能静默 exit 0 —— 这正是本项要发现的情况
        check(f"轮换运行中（近 30 分钟 ≥4 次，实际 {len(stamps)} 次）", False,
              "无轮换事件：daemon 可能未运行或轮换停了")

    # 3.2 切换避让死节点
    # 必须按事件时序判定：节点被判死后由 TTL 机制释放、随后被选中是**正常流程**，
    # 单纯对集合求交会把这种正常回收误报成违规。
    ts_re = re.compile(r"\[([\d-]+ [\d:]+)\]")

    def _events(lines, pat):
        out = []
        for l in lines:
            mt, mp = ts_re.search(l), re.search(pat, l)
            if mt and mp:
                out.append((mt.group(1), mp.group(1)))
        return out

    picks = _events(journal("10 minutes ago", "切代理"), r"-> (\S+)")
    deaths = _events(journal("10 minutes ago", r"死 \(连续失败"), r"(\S+?) 死")
    # 节点列表形如 `: ['a', 'b']`，必须锚定在 ": [" 之后；
    # 用 r"\[(.+?)\]" 会先命中行首的 [时间戳]（实测到的是 PID [1]）
    releases = []
    for line in journal("10 minutes ago", r"释放 \d+ 个黑名单节点"):
        mt = ts_re.search(line)
        ml = re.search(r": \[([^\]]*)\]", line)
        if not (mt and ml):
            continue
        for node in re.findall(r"'([^']+)'", ml.group(1)):
            releases.append((mt.group(1), node))

    # 同一节点在窗口内可能判死/释放多次，必须取"选中时刻之前最近一次状态"：
    # 该次事件是 death → 违规；是 release → 正常（TTL 回收后重选是设计行为）。
    events = [(ts, n, "death") for ts, n in deaths] + \
             [(ts, n, "release") for ts, n in releases]

    violations = []
    for pick_ts, node in picks:
        before = [(ts, kind) for ts, n, kind in events if n == node and ts <= pick_ts]
        if not before:
            continue                       # 选中前无死亡记录 → 正常
        if max(before)[1] == "death":      # 最近的先导事件是"判死" → 违规
            violations.append(f"{node}(选中 {pick_ts} 时仍处于死亡状态)")

    check("切换目标不含未释放的死节点", not violations, f"违规={violations}")
    print(f"  近期切换目标 {len(picks)} 个, 判死 {len(deaths)} 次, 释放 {len(releases)} 次")

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
        check("daemon 服务 active", proc_run(
            ["systemctl", "--user", "is-active", "auto-switch-ip.service"],
            timeout=20).stdout.strip() == "active")

    # 3.6 地理多样性（订阅绕限速的核心价值）
    if not quick:
        geo = collections.Counter()
        sample = [n for n in all_nodes if not n.endswith("-h2")][:30]
        for n in sample:
            if switch(n):
                time.sleep(0.5)
                out = run_curl(["-x", PROXY, GEO_URL], timeout=12).stdout.strip()
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
    try:
        verify_port()
        all_nodes = verify_switch(args.samples, args.quick)
        verify_reasonableness(all_nodes, args.quick)
    except ClashUnreachable as e:
        print(f"\n✗ {e}")
        print("  （先把 sing-box/daemon 拉起来再跑本脚本）")
        return 1

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
