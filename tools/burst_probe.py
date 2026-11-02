#!/usr/bin/env python3
"""突发探测：单包能通不代表能撑隧道。

筛选器只发一次 1200B 就能拿到回包，但 MASQUE 需要在同一路径上持续
双向传 1200B+ 的包。中继只转发「偶发小包」时会通过筛选却在建隧道时失败
（实测 hk-aws-122/123/54、jp-aws-110/117 全部如此）。

本脚本对目标连发 N 个 1200B 探测，统计回包率，作为「能否承载隧道」的
更强判据。回包率接近 100% 才有戏。

用法：
    sudo -u administrator python3 tools/burst_probe.py \
        --tags hk-aws-122.acczzz.com:443-any hk-aws-53.acczzz.com:443-any --n 20
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DIR))

import warp_wg  # noqa: E402
from tools.screen_udp_matrix import node_cfg, quic_vn, wait_port  # noqa: E402


def burst(port: int, dst: tuple[str, int], n: int, size: int,
          timeout: float = 3.0) -> tuple[int, int]:
    """连发 n 个 size 字节的 QUIC 探测，返回 (回包数, 总发送数)。"""
    hits = 0
    for _ in range(n):
        pkt = quic_vn()
        if size > len(pkt):
            pkt = pkt + b"\x00" * (size - len(pkt))
        if warp_wg._socks_udp(port, dst, pkt[:size], timeout=timeout):
            hits += 1
        time.sleep(0.08)
    return hits, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default=str(DIR / "dormant-outbounds.json"))
    ap.add_argument("--tags", nargs="+", required=True)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--sizes", default="1200,1350")
    ap.add_argument("--base-port", type=int, default=32000)
    a = ap.parse_args()

    nodes = {x["tag"]: x for x in json.loads(Path(a.inputs).read_text())}
    sizes = [int(s) for s in a.sizes.split(",")]
    print(f"每目标 {a.n} 包 × 尺寸 {sizes}\n")
    print(f"{'tag':44s} " + " ".join(f"{s:>12d}B" for s in sizes))
    for i, tag in enumerate(a.tags):
        node = nodes.get(tag)
        if not node:
            print(f"{tag:44s} 未找到")
            continue
        port = a.base_port + i
        tf = Path(tempfile.mkdtemp(prefix=f"bst{i}_")) / "cfg.json"
        tf.write_text(json.dumps(node_cfg(node, port)))
        proc = subprocess.Popen([str(DIR / "sing-box"), "run", "-c", str(tf)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            if not wait_port(port):
                print(f"{tag:44s} sing-box 未起来")
                continue
            cells = []
            for s in sizes:
                h, t = burst(port, ("162.159.198.2", 443), a.n, s)
                cells.append(f"{h:>3d}/{t:<3d} {100*h//t:>3d}%")
            print(f"{tag:44s} " + " ".join(cells), flush=True)
        finally:
            try:
                os.killpg(os.getpgid(proc.pid), 15)
            except OSError:
                proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
