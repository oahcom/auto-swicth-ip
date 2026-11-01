#!/usr/bin/env python3
"""全量扫描：哪些「不能跑 WireGuard」的机场节点能承载 MASQUE 出口。

对每个节点串行执行一条完整链路并判定 WARP 出口：

    节点 sing-box(socks:20xxx) → masque_bridge(127.0.0.2:444xx)
        → usque socks(QUIC, endpoint=127.0.0.2, 出 114xx) → trace 判定

判定标准（踩过的坑都记在这里）
------------------------------
* 只有 `warp=on` 才算成功。usque 的 SOCKS 在隧道断开时**回落直连**，
  trace 会显示 warp=off + 本机 IP —— 那是假成功，必须排除。
* curl 必须用 `--noproxy ""`（空字符串）。`--noproxy "*"` 会把
  `--proxy` 一起禁掉，测出来的是本机直连。
* usque 的 `-P` 是**远端端点端口**（= 桥端口），不是本地监听端口。
* QUIC 模式下端点读 `endpoint_v4`，不是 `endpoint_h2_v4`。
* 设备闲置久了 token 过期，CF 静默丢包 —— 用前先 `enroll`。

用法
----
    # 扫那 44 个节点（串行，约 20 分钟）
    python3 tools/scan_masque_nodes.py --inputs /tmp/opencode/dead44_inputs.json

    # 只扫前 6 个，看工具是否正常
    python3 tools/scan_masque_nodes.py --limit 6

    # 跳过 TCP 已死的节点（默认开启，省时间）
    python3 tools/scan_masque_nodes.py --no-tcp-check
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DIR))

import warp_wg  # noqa: E402

USQUE_BIN = str(DIR / ".warp/usque3/usque")
BRIDGE = str(DIR / "tools/masque_bridge.py")
SINGBOX = str(DIR / "sing-box")
MASQUE_DST = "162.159.198.2:443"

# 闲置设备池（避开生产线的 a-i）。串行复用，同一时刻只有一台在跑。
DEVICES = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]

BASE_NODE_PORT = 20000
BASE_BRIDGE_PORT = 44400
BASE_OUT_PORT = 11400

DOH_SERVER = "custom.juhazf.cn"
DOH_PATH = "/inst"


def build_node_cfg(node: dict, port: int) -> dict:
    """单节点 socks 出站配置。

    WHY 必须挂机场 DoH：机场拿 `cn.bing.com` / `hk-aws-*.acczzz.com`
    这些别人的域名当跳板，公网解析要么 NXDOMAIN 要么到真 Bing，
    证书对不上 → 握手失败。DoH 走机场自己的解析器才拿得到真实地址。
    """
    out = warp_wg.prepare_node(node)
    return {
        "log": {"level": "error"},
        "inbounds": [{"type": "socks", "tag": "in",
                      "listen": "127.0.0.1", "listen_port": port}],
        "outbounds": [out],
        "dns": {
            "servers": [
                {"type": "https", "tag": "dns-airport", "server": DOH_SERVER,
                 "path": DOH_PATH, "domain_resolver": "dns-default"},
                {"type": "local", "tag": "dns-default"},
            ],
            "rules": [{"domain_suffix": ["okzdns.com", "juhazf.cn", "hi2026.net",
                                         "acczzz.com", "bing.com"],
                       "server": "dns-airport", "strategy": "ipv4_only"}],
            "final": "dns-default",
        },
        "route": {"final": out["tag"],
                  "default_domain_resolver": {"server": "dns-default"}},
    }


def trace(port: int, timeout: int = 20) -> dict:
    """经 socks 端口取 CF trace。注意 --noproxy 必须是空字符串。"""
    r = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout), "--noproxy", "",
         "--proxy", f"socks5h://127.0.0.1:{port}",
         "https://www.cloudflare.com/cdn-cgi/trace"],
        capture_output=True, text=True, timeout=timeout + 8)
    out = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def tcp_alive(port: int) -> bool:
    """节点本身 TCP 通不通（不通就别浪费 20 秒 MASQUE 探测）。"""
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "8", "--noproxy", "",
             "--proxy", f"socks5h://127.0.0.1:{port}",
             "-o", "/dev/null", "-w", "%{http_code}",
             "https://www.gstatic.com/generate_204"],
            capture_output=True, text=True, timeout=14)
        return r.stdout.strip() in ("204", "200")
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default="/tmp/opencode/dead44_inputs.json",
                    help="节点清单 JSON（含 tag / country / node 字段）")
    ap.add_argument("--limit", type=int, default=0, help="只扫前 N 个")
    ap.add_argument("--out", default="/tmp/opencode/masque_scan.json")
    ap.add_argument("--no-tcp-check", action="store_true",
                    help="不做 TCP 存活预检（默认做）")
    ap.add_argument("--enroll", action="store_true",
                    help="每个设备用前重新 enroll（token 过期时开）")
    args = ap.parse_args()

    nodes = json.load(open(args.inputs))
    if args.limit:
        nodes = nodes[:args.limit]
    print(f"待扫节点: {len(nodes)}  设备池: {DEVICES[:4]}…(串行复用)", flush=True)

    results = []
    for i, item in enumerate(nodes):
        tag = item["tag"]
        country = item.get("country", "?")
        node_port = BASE_NODE_PORT + 100 + i
        bridge_port = BASE_BRIDGE_PORT + i
        out_port = BASE_OUT_PORT + i
        dev = DEVICES[i % len(DEVICES)]
        dev_cfg = DIR / f".warp/usque{dev}" / "config.json"

        # 1) 节点 socks
        cfg_path = f"/tmp/mq_node_{i}.json"
        json.dump(build_node_cfg(item["node"], node_port), open(cfg_path, "w"))
        sb = subprocess.Popen([SINGBOX, "run", "-c", cfg_path],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
        time.sleep(3)

        rec = {"tag": tag, "country": country, "device": f"usque{dev}"}
        if not args.no_tcp_check and not tcp_alive(node_port):
            rec.update(ok=False, reason="节点 TCP 不通（节点本身已死）")
            results.append(rec)
            print(f"[{i+1}/{len(nodes)}] {tag[:40]:40s} ✗ 节点TCP死", flush=True)
            sb.kill()
            continue

        # 2) UDP 桥
        bridge = subprocess.Popen(
            [sys.executable, BRIDGE, "--node-socks", str(node_port),
             "--listen", str(bridge_port), "--dst", MASQUE_DST],
            stdout=open(f"/tmp/mq_bridge_{i}.log", "w"),
            stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(2)

        # 3) usque（QUIC 模式，endpoint 指向桥）
        cfg = json.load(open(dev_cfg))
        cfg["endpoint_v4"] = "127.0.0.2"
        json.dump(cfg, open(dev_cfg, "w"))
        if args.enroll:
            subprocess.run(f"echo y | {USQUE_BIN} enroll -c {dev_cfg}",
                           shell=True, capture_output=True, text=True, timeout=90)
        usque = subprocess.Popen(
            [USQUE_BIN, "socks", "-c", str(dev_cfg), "-P", str(bridge_port),
             "-b", "127.0.0.1", "-p", str(out_port)],
            stdout=open(f"/tmp/mq_usque_{i}.log", "w"),
            stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(15)

        t = trace(out_port)
        if t.get("warp") == "on":
            rec.update(ok=True, colo=t.get("colo"), loc=t.get("loc"),
                       ip=t.get("ip"), http=t.get("http"))
            print(f"[{i+1}/{len(nodes)}] {tag[:40]:40s} ✓ warp=on "
                  f"colo={t.get('colo')} loc={t.get('loc')}", flush=True)
        else:
            rec.update(ok=False, reason=f"MASQUE 未建立 (warp={t.get('warp','?')})",
                       colo=t.get("colo"))
            print(f"[{i+1}/{len(nodes)}] {tag[:40]:40s} ✗ {rec['reason']}", flush=True)
        results.append(rec)

        usque.kill()
        bridge.kill()
        sb.kill()
        time.sleep(1)

    ok = [r for r in results if r.get("ok")]
    print(f"\n=== 扫描完成：{len(ok)}/{len(results)} 节点可承载 MASQUE ===")
    for r in ok:
        print(f"  ✓ {r['tag'][:44]:44s} {r['country']:>4s} colo={r['colo']} loc={r['loc']}")
    json.dump(results, open(args.out, "w"), indent=2, ensure_ascii=False)
    print(f"明细: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
