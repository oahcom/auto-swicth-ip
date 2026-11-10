#!/usr/bin/env python3
"""换节点的「占用」判据：同一条线不能被换到兄弟线正在用的节点上。

2026-11-10 事故：一轮同块轮回把 15 条 WG 线全换到
`bilibili-image-cdn.juhazf.cn:50011-any`（出口清一色 ORD）。
两个缺陷：
1. `_same_block_target` 的 `taken` 用的是调用方传入的循环快照 —— 前面几条线
   刚换过节点它看不到，同一个空闲节点被反复选中；
2. `warp_wg.rebind` 沿 rotation 走，rotation 是每条线一份**同一份全序**，
   批量换节点时所有线都先撞排序第一的那个。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="asip-rebind-test-")
os.environ["ASIP_NO_SIDE_EFFECTS"] = "1"
os.environ.setdefault("WARP_LINE_STATE_FILE_PATH", os.path.join(_TMP, "state.json"))
os.environ.setdefault("WARP_IP_USED_FILE_PATH", os.path.join(_TMP, "used.json"))
os.environ.setdefault("WARP_SEGMENT_FILE_PATH", os.path.join(_TMP, "seg.json"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import warp_wg  # noqa: E402


def _line(node, rotation=None):
    return {"node": node, "rotation": rotation or [node],
            "unit": "u", "port": 1, "family": "v6", "rebind_ts": 0}


def test_rebind_skips_node_bound_by_sibling(monkeypatch):
    """rebind 必须把兄弟线占着的节点排到最后。"""
    state = {"lines": {
        "WARP-WG-1": _line("n1", ["n1", "shared", "free"]),
        "WARP-WG-2": _line("shared", ["shared", "free"]),
    }}
    written = []
    monkeypatch.setattr(warp_wg, "_read_state", lambda: state)
    monkeypatch.setattr(warp_wg, "line_nodes",
                        lambda: {"WARP-WG-1": "n1", "WARP-WG-2": "shared"})
    monkeypatch.setattr(warp_wg, "udp_capable",
                        lambda nodes: [{"tag": t} for t in ("n1", "shared", "free")])
    monkeypatch.setattr(warp_wg, "write_line",
                        lambda i, node, family=None: written.append(node["tag"]))
    monkeypatch.setattr(warp_wg, "write_json", lambda *a, **k: None)
    got = warp_wg.rebind(1)
    assert got == "free", f"应跳过共享节点，实际 {got}"
    assert state["lines"]["WARP-WG-1"]["node"] == "free"


def test_same_block_standby_avoids_bound_nodes(monkeypatch):
    """同块候选里，兄弟线正绑着的节点不能被再选一次。"""
    import daemon

    board = {"lines": {
        "WARP-WG-1": _line("n1", ["n1", "n2", "n3"]),
        "WARP-WG-2": _line("n2", ["n2", "n3"]),
    }}
    monkeypatch.setattr(warp_wg, "_read_state", lambda: board)
    monkeypatch.setattr(daemon.warp_wg, "_read_state", lambda: board)
    monkeypatch.setattr(warp_wg, "load_nodes",
                        lambda: [{"tag": t} for t in ("n1", "n2", "n3")])
    monkeypatch.setattr(warp_wg, "udp_capable",
                        lambda nodes: [{"tag": t} for t in ("n1", "n2", "n3")])
    monkeypatch.setattr(daemon.warp_blocks, "load_map",
                        lambda: {"n1": {"colo": "NRT"}, "n2": {"colo": "NRT"},
                                 "n3": {"colo": "NRT"}})
    monkeypatch.setattr(daemon, "_node_tcp_ok", lambda t: True)
    monkeypatch.setattr(daemon, "_warp_alive_rank", lambda *a, **k: 0)

    got = daemon._same_block_standby("WARP-WG-1", time.time())
    assert got == "n3", f"n2 已被 WG-2 占用，应选 n3，实际 {got}"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {name}: {e}")
    print("all passed" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
