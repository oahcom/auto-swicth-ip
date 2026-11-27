#!/usr/bin/env python3
"""测每个 h2 节点入口的 TCP 握手延迟，写 .warp/h2/node_delay.json。

只做一件事：给 `ASIP_H2_NODE_POLICY=best` 提供「同 colo 里哪个节点最快」的依据。
渲染器在 best 模式下按这份表排序该 colo 的节点池，队首即最低延迟。

用法：
    python3 deploy/probe_node_delay.py             # 全量（约 214 个节点，32 并发）
    python3 deploy/probe_node_delay.py --limit 20  # 只测前 20 个（调试用）
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import socket
import statistics
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
OUT = PROJECT / ".warp" / "h2" / "node_delay.json"
PAT = re.compile(r"^([^:]+):(\d+)")


def probe(tag: str, tries: int = 3, timeout: float = 3.0) -> float | None:
    m = PAT.match(tag)
    if not m:
        return None
    host, port = m.group(1), int(m.group(2))
    samples = []
    for _ in range(tries):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                samples.append((time.perf_counter() - t0) * 1000)
        except OSError:
            continue
        time.sleep(0.05)
    return round(statistics.median(samples), 1) if samples else None


def main() -> int:
    nodes = json.loads((PROJECT / ".warp" / "block-map.json").read_text()).get("nodes") or {}
    tags = [t for t, m in nodes.items()
            if isinstance(m, dict) and m.get("carrier") == "h2"]
    if "--limit" in sys.argv:
        tags = tags[: int(sys.argv[sys.argv.index("--limit") + 1])]
    out: dict[str, float] = {}
    with cf.ThreadPoolExecutor(max_workers=32) as ex:
        for tag, ms in zip(tags, ex.map(probe, tags)):
            if ms is not None:
                out[tag] = ms
    OUT.write_text(json.dumps({"updated": time.time(), "delays": out},
                              ensure_ascii=False, indent=1))
    print(f"✓ {len(out)}/{len(tags)} 个节点有 RTT，写入 {OUT}")
    for t, ms in sorted(out.items(), key=lambda kv: kv[1])[:8]:
        print(f"  {ms:7.1f}ms  {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
