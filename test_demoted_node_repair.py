#!/usr/bin/env python3
"""换线后修复被切线的离线自检（不联网、不碰 systemd）。

覆盖 `_demoted_repair_applies` 的原因分类与 `_demoted_node_repair` 的
选点/节流/重启语义。跑法：python3 -m pytest -q test_demoted_node_repair.py
"""
import sys
import time
from unittest.mock import patch

import pytest

import daemon  # noqa: E402


# ---------------------------------------------------------------- 原因分类
def test_applies_transport_reasons():
    assert daemon._demoted_repair_applies("transport ×29（出口探测失败）")
    assert daemon._demoted_repair_applies("transport ×8（opencode 链路连续悬挂）")


def test_rejects_ip_level_reasons():
    # IP/配额级：节点本身是好的，修节点等于白丢链路
    assert not daemon._demoted_repair_applies("rate429 ×5")
    assert not daemon._demoted_repair_applies("abuse403 ×3")
    assert not daemon._demoted_repair_applies("主动轮换 已用配额 157 格 dsv4 ≥ 150")
    assert not daemon._demoted_repair_applies("探测发现当前出口处于惩罚期")
    assert not daemon._demoted_repair_applies("启动校验 当前出口低质")
    assert not daemon._demoted_repair_applies("")


# ---------------------------------------------------------------- 测试夹具
MEMBER_WG = {"tag": "WARP-WG-7", "kind": "wg", "index": 7}
MEMBER_H2 = {"tag": "WARP-H2-HKG-71", "kind": "h2", "index": 71}

NODES = [{"tag": "a.nrt:443-any"}, {"tag": "b.nrt:443-any"},
         {"tag": "c.nrt:443-any"}, {"tag": "d.hkg:443-any"}]
COLO_MAP = {
    "a.nrt:443-any": {"colo": "NRT", "ok": True},
    "b.nrt:443-any": {"colo": "NRT", "ok": True},
    "c.nrt:443-any": {"colo": "NRT", "ok": True},
    "d.hkg:443-any": {"colo": "HKG", "ok": True},
}
WG_STATE = {"lines": {"WARP-WG-7": {"node": "a.nrt:443-any"}}}
BLOCKS_MAP = {"a.nrt:443-any": {"wg_ok": 1, "wg_n": 2},
             "b.nrt:443-any": {"wg_ok": 0, "wg_n": 2},   # 实测不能承载
             "c.nrt:443-any": {},                        # 没测过
             "d.hkg:443-any": {"wg_ok": 1, "wg_n": 2}}


def _base_patches(rehome="ok", tcp=True, h2=True):
    """通用 mock：默认所有依赖可用。"""
    return {
        "_warp": lambda *a, **k: {
            "demoted_node_repair_enabled": True,
            "demoted_node_repair_cooldown_sec": 600,
        },
        "_warp_member": lambda tag: {"WARP-WG-7": MEMBER_WG,
                                     "WARP-H2-HKG-71": MEMBER_H2}.get(tag),
        "active_warp_tag": lambda: "WARP-WG-8",
        "_warp_line_state": lambda tag: {},
        "_rehome_one": lambda tag, node: rehome,
        "_node_tcp_ok": lambda node, timeout=6.0: tcp,
        "_h2_repair": lambda member: h2,
        "_warp_line_restart": lambda member, reason, rebound=False: True,
    }


def _patch_wg_tables(monkeypatch, *, exclude_nodes=None, cur_node="a.nrt:443-any"):
    monkeypatch.setattr(daemon.warp_wg, "load_nodes", lambda: NODES, raising=False)
    monkeypatch.setattr(daemon.warp_wg, "udp_capable", lambda ns: ns, raising=False)
    monkeypatch.setattr(daemon.warp_wg, "load_colo_map", lambda: COLO_MAP, raising=False)
    monkeypatch.setattr(daemon.warp_wg, "line_nodes",
                        lambda: {"WARP-WG-8": "z.tpe:443-any",
                                 **(exclude_nodes or {})}, raising=False)
    monkeypatch.setattr(daemon.warp_wg, "_read_state",
                        lambda: {"lines": {"WARP-WG-7": {"node": cur_node}}},
                        raising=False)
    monkeypatch.setattr(daemon.warp_blocks, "load_map", lambda: BLOCKS_MAP, raising=False)


@pytest.fixture(autouse=True)
def _clean():
    daemon._DEMOTED_REPAIR_COOLDOWN.clear()
    daemon._DEMOTED_REPAIR_RUNNING.clear()
    daemon._WARP_REFRESHING.clear()
    daemon._WARP_REFRESH_PENDING.clear()
    daemon._NODE_DELAYS.clear()
    yield


# ---------------------------------------------------------------- WG 路径
def test_wg_picks_same_block_low_latency_capable(monkeypatch):
    """同块、排除已绑定与不能承载的，按延迟升序选点；成功后 rebound 重启。"""
    _patch_wg_tables(monkeypatch)
    daemon._NODE_DELAYS.update({"a.nrt:443-any": 100, "b.nrt:443-any": 50,
                                "c.nrt:443-any": 200, "d.hkg:443-any": 30})
    calls = {}
    with patch.multiple(daemon, **_base_patches()), \
         patch.object(daemon, "log"):
        daemon._rehome_one = (
            lambda tag, node: calls.__setitem__("rehome", node) or "ok")
        daemon._warp_line_restart = (
            lambda m, r, rebound=False: calls.__setitem__("restart", rebound))
        daemon._demoted_node_repair("WARP-WG-7", "transport ×29（出口探测失败）")
    # b.nrt 延迟最低但实测不能承载（rank 2）→ 选 c.nrt（rank 1 里的最低延迟）
    assert calls["rehome"] == "c.nrt:443-any"
    assert calls["restart"] is True


def test_wg_skips_when_no_same_block_candidate(monkeypatch):
    _patch_wg_tables(monkeypatch, exclude_nodes={
        "WARP-WG-1": "b.nrt:443-any", "WARP-WG-2": "c.nrt:443-any"})
    seen = []
    with patch.multiple(daemon, **_base_patches()), \
         patch.object(daemon, "log"):
        daemon._rehome_one = lambda tag, node: seen.append(node) or "ok"
        daemon._demoted_node_repair("WARP-WG-7", "transport ×1")
    assert not seen          # 同块候选全被其他线占用 → 不动


def test_wg_rehome_gone_means_no_restart(monkeypatch):
    _patch_wg_tables(monkeypatch)
    restarted = []
    with patch.multiple(daemon, **_base_patches(rehome="gone")), \
         patch.object(daemon, "log"):
        daemon._warp_line_restart = (
            lambda m, r, rebound=False: restarted.append(m["tag"]))
        daemon._demoted_node_repair("WARP-WG-7", "transport ×1")
    assert not restarted


def test_cooldown_blocks_second_repair(monkeypatch):
    _patch_wg_tables(monkeypatch)
    n = []
    with patch.multiple(daemon, **_base_patches()), \
         patch.object(daemon, "log"):
        daemon._rehome_one = lambda tag, node: n.append(node) or "ok"
        daemon._demoted_node_repair("WARP-WG-7", "transport ×1")
        daemon._demoted_node_repair("WARP-WG-7", "transport ×2")
    assert len(n) == 1        # 600s 冷却内第二次直接跳过


def test_busy_line_not_repaired(monkeypatch):
    _patch_wg_tables(monkeypatch)
    daemon._WARP_REFRESHING.add("WARP-WG-7")
    n = []
    with patch.multiple(daemon, **_base_patches()), \
         patch.object(daemon, "log"):
        daemon._rehome_one = lambda tag, node: n.append(node) or "ok"
        daemon._demoted_node_repair("WARP-WG-7", "transport ×1")
    assert not n


def test_stop_probe_line_not_repaired(monkeypatch):
    _patch_wg_tables(monkeypatch)
    overrides = _base_patches()
    overrides["_warp_line_state"] = lambda tag: {"stop_probe": True}
    n = []
    with patch.multiple(daemon, **overrides), \
         patch.object(daemon, "log"):
        daemon._rehome_one = lambda tag, node: n.append(node) or "ok"
        daemon._demoted_node_repair("WARP-WG-7", "transport ×1")
    assert not n


def test_switched_back_in_line_not_repaired(monkeypatch):
    _patch_wg_tables(monkeypatch)
    overrides = _base_patches()
    overrides["active_warp_tag"] = lambda: "WARP-WG-7"   # 又切回来了
    n = []
    with patch.multiple(daemon, **overrides), \
         patch.object(daemon, "log"):
        daemon._rehome_one = lambda tag, node: n.append(node) or "ok"
        daemon._demoted_node_repair("WARP-WG-7", "transport ×1")
    assert not n


# ---------------------------------------------------------------- H2 路径
def test_h2_uses_h2_repair(monkeypatch):
    called = {}
    overrides = _base_patches()
    overrides["_h2_repair"] = lambda m: called.setdefault("tag", m["tag"]) or True
    with patch.multiple(daemon, **overrides), \
         patch.object(daemon, "log"):
        daemon._demoted_node_repair("WARP-H2-HKG-71", "transport ×4（出口探测失败）")
    assert called["tag"] == "WARP-H2-HKG-71"


# ---------------------------------------------------------------- 挂载点
def test_after_warp_switch_triggers_on_transport(monkeypatch):
    _patch_wg_tables(monkeypatch)
    started = []
    with patch.multiple(daemon, **_base_patches()), \
         patch.object(daemon, "log"), \
         patch.object(daemon, "_schedule_demoted_node_repair",
                      lambda tag, reason: started.append((tag, reason))):
        # interrupt=False（主动轮换语义）也不拦修复 —— 触发与否只看 reason
        daemon._after_warp_switch("WARP-WG-7", interrupt=False,
                                  reason="transport ×1（出口探测失败）")
        daemon._after_warp_switch("WARP-WG-7", interrupt=True, reason="rate429 ×5")
    assert started == [("WARP-WG-7", "transport ×1（出口探测失败）")]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
