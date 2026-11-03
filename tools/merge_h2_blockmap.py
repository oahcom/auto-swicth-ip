#!/usr/bin/env python3
"""把 H2 实测（scan_h2.json）并入 `.warp/block-map.json`。

WHY 单独一个脚本而不改 build_map：`build_map` 的输入是「一轮探测结果」，
键是 `egress_ip`，还要按 IP 聚合多轮、取 colo 众数。H2 扫描的结果已经是
「一个节点 → 一个确定的 colo」，没有多轮也没有共用 IP 的歧义 —— 硬塞进
build_map 的输入格式只会让两边都变形。

**只增不删**：已有节点的条目（来自 WG 探测）一律保留。H2 与 WG 是两条独立的
承载路径，同一节点在两条路上的稳定性互不代表；覆盖等于拿一个观测推翻另一个。
节点名重叠时保留原有条目，仅在 `h2` 字段里记录 H2 的结论。

用法：
    python3 tools/merge_h2_blockmap.py /tmp/opencode/h2test/scan_h2.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    scan = json.loads(Path(sys.argv[1]).read_text())
    path = PROJECT / ".warp" / "block-map.json"
    data = json.loads(path.read_text())
    nodes = data.setdefault("nodes", {})

    added = merged = skipped = 0
    for row in scan:
        if not row.get("ok") or not row.get("colo"):
            skipped += 1
            continue
        tag, colo = row["tag"], row["colo"]
        if tag in nodes:
            nodes[tag].setdefault("h2", {})["colo"] = colo
            nodes[tag]["h2"]["ip"] = row.get("ip")
            merged += 1
            continue
        # H2 单轮成功：success/attempts == 1，按 category_for 的判据就是
        # regular。样本量只有 1，所以 ok/n 如实记 1/1，不美化。
        nodes[tag] = {
            "colo": colo, "category": "regular", "ok": 1, "n": 1,
            "egress_ip": row.get("ip"),
            "colo_mix": {colo: 1}, "drift": [],
            "carrier": "h2",
        }
        added += 1

    data["updated"] = int(time.time())
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.chmod(0o600)
    tmp.replace(path)

    from collections import Counter
    print(f"H2 新增 {added} 个节点，已存在合并 {merged}，跳过失败 {skipped}",
          file=sys.stderr)
    print(f"块分布 {dict(Counter(v['colo'] for v in nodes.values()))}",
          file=sys.stderr)
    print(f"→ {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
