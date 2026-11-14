#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原生渠道延迟保底测量（asip 侧，不依赖 9router / 生产流量）。

背景：生产 TTFT 残差受上游排队/缓存/生成长度干扰，且 asip 未来可能脱离
9router 独立运行。本脚本在渠道的三个原生层级直接测延迟，随时可跑、独立成榜：

  warp-endpoint  本机→CF WARP 端点 162.159.198.3-9:443 TCP 握手 RTT
                 （所有 WARP 线共享的基底；MASQUE over h2 走 TCP 443）
  node           代理池节点 host:port 的 TCP connect + TLS 握手耗时
                 （渠道底层构件；清单来自 .warp/h2_probe_state.json）
  channel        经各渠道 socks_port 的端到端：TLS 建连 + TTFB 打到
                 1.1.1.1 IPv6（2606:4700:4700::1111），顺带取回出口
                 IPv6 与 colo。清单来自 .warp/h2/channels.json

输出：.warp/channel_latency.json（含 ts，可增量对比）+ 控制台排行。
用法：native_channel_probe.py [all|nodes|channels|warp-endpoint] [-n 样本数]
"""
import json
import re
import socket
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = "/home/administrator/dkk-projects/auto-switch-ip"
CHANNELS = f"{ROOT}/.warp/h2/channels.json"
PROBE_STATE = f"{ROOT}/.warp/h2_probe_state.json"
OUT = f"{ROOT}/.warp/channel_latency.json"
TRACE_URL = "https://[2606:4700:4700::1111]/cdn-cgi/trace"
WARP_ENDPOINTS = [f"162.159.198.{i}" for i in range(3, 10)]

NODE_RE = re.compile(r"^(?P<host>[^:]+):(?P<port>\d+)")


def tcp_connect(host, port, timeout=5.0):
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return (time.monotonic() - t0) * 1000
    except OSError:
        return None


def tls_handshake(host, port, timeout=6.0):
    t0 = time.monotonic()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, int(port)), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host):
                return (time.monotonic() - t0) * 1000
    except OSError:
        return None


def probe_warp_endpoints(samples=3):
    rows = []
    for ip in WARP_ENDPOINTS:
        vals = [v for v in (tcp_connect(ip, 443) for _ in range(samples)) if v is not None]
        rows.append({"endpoint": ip, "n": len(vals),
                     "tcp_ms": round(min(vals), 1) if vals else None})
    return rows


def probe_nodes(samples=2, workers=12):
    with open(PROBE_STATE) as f:
        nodes = json.load(f).get("nodes", {})
    hosts = {}
    for key, st in nodes.items():
        m = NODE_RE.match(key)
        if m:
            hosts[f"{m['host']}:{m['port']}"] = st

    def one(item):
        hp, st = item
        host, port = hp.rsplit(":", 1)
        tcp = [v for v in (tcp_connect(host, port) for _ in range(samples)) if v is not None]
        tls = tls_handshake(host, port) if tcp else None
        return {"node": hp, "colo": st.get("colo"), "probe_ok": st.get("ok"),
                "tcp_ms": round(min(tcp), 1) if tcp else None,
                "tls_ms": round(tls, 1) if tls is not None else None}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, hosts.items()))


def probe_channels(samples=1, workers=8):
    with open(CHANNELS) as f:
        chans = json.load(f)

    def one(ch):
        sp = ch.get("socks_port")
        if not sp:
            return None
        best = None
        for _ in range(samples):
            out = subprocess.run(
                ["curl", "-s", "--max-time", "12", "-x", f"socks5h://127.0.0.1:{sp}",
                 "-o", "-", "-w",
                 "\n__T__ %{time_appconnect} %{time_starttransfer} %{time_total} %{http_code}",
                 TRACE_URL],
                capture_output=True, text=True).stdout
            tm = re.search(r"__T__ ([\d.]+) ([\d.]+) ([\d.]+) (\d+)", out)
            if not tm or tm.group(4) != "200":
                continue
            rec = {
                "tls_ms": round(float(tm.group(1)) * 1000, 1),
                "ttfb_ms": round(float(tm.group(2)) * 1000, 1),
                "total_ms": round(float(tm.group(3)) * 1000, 1),
            }
            ip = re.search(r"^ip=(.+)$", out, re.M)
            colo = re.search(r"^colo=(.+)$", out, re.M)
            rec["egress_ip"] = ip.group(1) if ip else None
            rec["colo"] = colo.group(1) if colo else None
            if best is None or rec["tls_ms"] < best["tls_ms"]:
                best = rec
        return {"channel": ch.get("channel"), "index": ch.get("index"),
                "socks_port": sp, "unit": ch.get("unit"),
                "backing_node": ch.get("node"),
                "result": best}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return [r for r in ex.map(one, chans) if r]


def fmt_table(rows, cols, limit=None):
    if limit:
        rows = rows[:limit]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols} if rows else {}
    line = "  ".join(c.ljust(widths[c]) for c in cols)
    print(line)
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def main():
    args = sys.argv[1:]
    mode = "all"
    samples = None
    for i, a in enumerate(args):
        if a == "-n" and i + 1 < len(args):
            samples = int(args[i + 1])
        elif not a.startswith("-") and a in ("all", "nodes", "channels", "warp-endpoint"):
            mode = a

    result = {"ts": int(time.time())}
    if mode in ("all", "warp-endpoint"):
        rows = probe_warp_endpoints(samples or 3)
        result["warp_endpoints"] = rows
        print("== warp-endpoint TCP/443 (ms, min-of-samples) ==")
        fmt_table(sorted(rows, key=lambda r: (r["tcp_ms"] is None, r["tcp_ms"] or 0)),
                  ["endpoint", "n", "tcp_ms"])
        print()
    if mode in ("all", "nodes"):
        rows = probe_nodes(samples or 2)
        result["nodes"] = rows
        ok = sorted([r for r in rows if r["tcp_ms"] is not None], key=lambda r: r["tcp_ms"])
        bad = [r for r in rows if r["tcp_ms"] is None]
        print(f"== node TCP/TLS (ms) · 可达 {len(ok)}/{len(rows)} ==")
        fmt_table(ok[:15], ["node", "colo", "tcp_ms", "tls_ms", "probe_ok"])
        print(f"（最差 10：）")
        fmt_table(ok[-10:], ["node", "colo", "tcp_ms", "tls_ms", "probe_ok"])
        print(f"不可达 {len(bad)} 个（详见 JSON）\n")
    if mode in ("all", "channels"):
        rows = probe_channels(samples or 1)
        result["channels"] = rows
        ok = [r for r in rows if r["result"]]
        ok.sort(key=lambda r: r["result"]["tls_ms"])
        bad = [r for r in rows if not r["result"]]
        print(f"== channel 端到端（TLS 建连 ms）· 成功 {len(ok)}/{len(rows)} ==")
        flat = [{"channel": r["channel"], "colo": r["result"].get("colo"),
                 "tls_ms": r["result"]["tls_ms"], "ttfb_ms": r["result"]["ttfb_ms"],
                 "egress_ip": r["result"].get("egress_ip")} for r in ok]
        print("前 15 快：")
        fmt_table(flat[:15], ["channel", "colo", "tls_ms", "ttfb_ms", "egress_ip"])
        print("后 10 慢：")
        fmt_table(flat[-10:], ["channel", "colo", "tls_ms", "ttfb_ms", "egress_ip"])
        print(f"失败 {len(bad)} 条（详见 JSON）")

    with open(OUT, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"\n结果已写入 {OUT}")


if __name__ == "__main__":
    main()
