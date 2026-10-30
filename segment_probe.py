#!/usr/bin/env python3
"""按节点抽样 WARP 出口 IPv6 /64，验证「抽到的段是否可用」

复用 warp_wg.py 的临时实例机制：每个节点起一个独立的 sing-box（临时端口、
临时配置目录），跑一次 trace 取出口 IP，然后立即杀掉。不改线路配置、不动
daemon、不写 .warp_ip_used.json。

用法（在项目根目录、WSL 内执行）：
    python3 segment_probe.py --nodes pq.us02.yydjc.top:443-vl --rounds 10
    python3 segment_probe.py --rounds 5 --family v6 --out .warp/segment_probe.json

判据：--rounds 建议 ≥10。每轮都是一次新的 WARP 会话，/64 会重新抽取。
输出里 hit_rate = 抽到的 /64 落在 .warp/ipv6_segments.json 中 usable=true 的比例。
先跑 1 个节点 1 轮做冒烟，确认能起隧道再放大。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import warp_wg as W
except Exception as e:  # noqa: BLE001
    sys.exit(f"无法导入 warp_wg.py（需在项目根目录执行）：{e}")

BASE = HERE


def load_baseline() -> dict[str, bool]:
    p = BASE / ".warp" / "ipv6_segments.json"
    if not p.exists():
        print("⚠ 缺少 .warp/ipv6_segments.json，先跑 python3 ipv6_segments.py；本次仍会采样但不判可用", file=sys.stderr)
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    return {s["segment"]: s["usable"] for s in d.get("ipv6_segments", [])}


def sample_once(profile: dict, node: dict, port: int, family: str, timeout: float) -> dict:
    cfg = W.build_line_config(profile, port, node, W._wg()["peer_v4"], family=family)
    cfg["log"]["level"] = "error"
    fields: dict[str, str] = {}
    sb = W.CFG["singbox"]["binary"]
    with tempfile.TemporaryDirectory(prefix="segprobe-") as td:
        path = os.path.join(td, "c.json")
        Path(path).write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        proc = subprocess.Popen([sb, "run", "-c", path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                fields = W._trace_fields(port, 5.0)
                if fields.get("warp") == "on":
                    break
                time.sleep(1.0)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    ip = fields.get("ip") or ""
    seg = str(ipaddress.ip_network(ip + "/64", strict=False)) if ":" in ip else None
    return {"ip": ip, "segment": seg, "colo": fields.get("colo"),
            "loc": fields.get("loc"), "ok": fields.get("warp") == "on"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", nargs="*", default=[], help="节点 tag，留空=全部 udp 可用节点")
    ap.add_argument("--rounds", type=int, default=10, help="每个节点采样轮数（默认 10）")
    ap.add_argument("--family", default="v6", choices=["v4", "v6"])
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--port", type=int, default=None, help="临时 socks 端口（默认 base_port+950）")
    ap.add_argument("--out", default=str(BASE / ".warp" / "segment_probe.json"))
    args = ap.parse_args()

    baseline = load_baseline()
    nodes = W.load_nodes()
    if args.nodes:
        want = set(args.nodes)
        nodes = [n for n in nodes if n["tag"] in want]
        missing = want - {n["tag"] for n in nodes}
        if missing:
            sys.exit(f"这些 tag 不在当前 singbox-config.json 里：{sorted(missing)}")
    if not nodes:
        sys.exit("没有可测节点")

    profile = W.read_profile(W.line_dir(1) / "profile.conf")
    port = args.port or W._wg()["base_port"] + 950
    print(f"基线段表 {len(baseline)} 段；端口 {port}；family={args.family}；轮数 {args.rounds}", file=sys.stderr)

    results = []
    for n in nodes:
        tag = n["tag"]
        rows = []
        for i in range(args.rounds):
            r = sample_once(profile, n, port, args.family, args.timeout)
            r["node"] = tag
            r["round"] = i + 1
            r["ts"] = int(time.time())
            rows.append(r)
            results.append(r)
            mark = "?" if not baseline else ("✓" if baseline.get(r["segment"]) else "×")
            print(f"{tag[:38]:38s} #{i+1:<2} {r['colo'] or '-':4s} {r['segment'] or '-':26s} {mark}",
                  file=sys.stderr, flush=True)
            time.sleep(1.0)
        segs = [r["segment"] for r in rows if r["segment"]]
        hits = sum(1 for s in segs if baseline.get(s))
        print(f"  -> {tag}：{len(segs)}/{len(rows)} 成功，"
              f"{len(set(segs))} 个不同段，可用 {hits} 次"
              f"（命中率 {hits/len(segs)*100:.0f}%）" if segs else f"  -> {tag}：全部失败",
              file=sys.stderr)

    out = {
        "generated_at": int(time.time()),
        "generated_at_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "family": args.family,
        "rounds": args.rounds,
        "baseline_segments": len(baseline),
        "summary": {},
        "samples": results,
    }
    by_node: dict[str, list[dict]] = {}
    for r in results:
        by_node.setdefault(r["node"], []).append(r)
    for tag, rows in by_node.items():
        segs = [r["segment"] for r in rows if r["segment"]]
        hits = sum(1 for s in segs if baseline.get(s))
        out["summary"][tag] = {
            "rounds": len(rows),
            "ok": len(segs),
            "distinct_segments": len(set(segs)),
            "usable_hits": hits,
            "hit_rate": round(hits / len(segs) * 100, 1) if segs else 0.0,
            "segments": sorted(set(segs)),
        }

    outp = Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n写入 {outp}", file=sys.stderr)
    for tag, s in out["summary"].items():
        print(f"  {tag:<42} 成功 {s['ok']}/{s['rounds']}  段 {s['distinct_segments']}  命中率 {s['hit_rate']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
