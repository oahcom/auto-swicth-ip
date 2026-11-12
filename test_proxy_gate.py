#!/usr/bin/env python3
"""第七闸口（代理池兜底 slot-7）的确定性单元测试（无网络、<10s）。

验证用户指定的设计：
  - 常规闸口有得选时，第七闸口绝不参与（严格兜底）
  - 六个常规闸口全都选不出来时，才把第七闸口当重试目标
  - 第七闸口报 429 = 池内换节点，绝不换回 WARP 线（不进 _repair_slot）
  - 池内轮换作用在第七闸口自己的 pool selector 上（PUT 只打这台进程）
  - keeper 的健康结论/死节点换掉/落池开关关闭语义
  - 配置层：slot-7 的端口/路径/默认池出口
"""
import time
import types

import pytest

import daemon
import config
import parse_subscription


WARP_SLOTS = [{"name": "slot-%d" % i, "selector": "opencode-out-%d" % i,
               "port": 7890 if i == 1 else 7890 + i} for i in range(1, 7)]
GATE = {"name": "slot-7", "kind": "proxy", "selector": "opencode-out-7",
        "pool_selector": "proxy", "port": 7897, "clash_api_port": 9097,
        "config_file": "/tmp/asip-test-slot7.json"}
SLOTS = WARP_SLOTS + [GATE]


class _NoopThread:
    def __init__(self, target=None, args=(), kwargs=None, **kw):
        pass
    def start(self):
        pass
    def join(self, timeout=None):
        pass


@pytest.fixture
def isolated(monkeypatch):
    """7 闸沙箱：6 条 WARP 线 + 1 个池闸。"""
    monkeypatch.setattr(daemon.threading, "Thread", _NoopThread)
    monkeypatch.setattr(daemon, "consumer_slots", lambda: list(SLOTS))

    current = {"slot-%d" % i: ("WARP-%s" % chr(64 + i), "2606:%s::%d" % (chr(97 + i), i))
               for i in range(1, 7)}
    gate_state = {"now": "n1", "all": ["n1", "n2", "n3", "direct"], "puts": []}

    monkeypatch.setattr(daemon, "_slot_current",
                        lambda slot: current.get(slot["name"], (None, None)))

    def fake_slot_api(slot, path, method="GET", body=None, timeout=3,
                      silent=False):
        if slot["name"] == "slot-7" and path == "/proxies/proxy":
            if method == "PUT":
                gate_state["puts"].append(body.get("name"))
                gate_state["now"] = body.get("name")
                return 204, {}
            return 200, {"now": gate_state["now"], "all": gate_state["all"]}
        # 其余（含 /slots 之类）一律给个无害应答
        return 200, {"now": current.get(slot["name"], (None, None))[0],
                     "all": []}

    monkeypatch.setattr(daemon, "slot_singbox_api", fake_slot_api)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WARP-A")
    # _warp_member 的桩必须「非 WARP 一律 None」：_is_pool_node 靠它区分
    # 池节点与 WARP 线，返回 False 会让池节点被当成线过滤光。
    monkeypatch.setattr(daemon, "_warp_member",
                        lambda t: ({"tag": t} if (t and t.startswith("WARP-"))
                                   else None))
    monkeypatch.setattr(daemon, "_switch_warp",
                        lambda tag, ip=None: True)
    monkeypatch.setattr(daemon, "_probe_verdict_for_ip", lambda ip, now=None: None)
    monkeypatch.setattr(daemon, "_slot_ip_is_quarantined",
                        lambda ip, now=None: False)
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: False)
    monkeypatch.setattr(daemon, "_proxy_pool_healthy_cached",
                        lambda now=None: True)
    monkeypatch.setattr(daemon, "update_node_info_async", lambda n: None)
    monkeypatch.setattr(daemon, "_cool_down_node",
                        lambda n, sec=None: None)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    for n in ("_mark_warp_ip_used", "_warp_line_selected", "_set_warp_line_ip",
              "_begin_segment", "_publish_clash_snapshot", "_exit_ledger_note",
              "_write_json_atomic"):
        monkeypatch.setattr(daemon, n, lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_warp_line_ip", lambda tag: None)

    daemon._SLOT_HEALTH.clear()
    daemon._SLOT_EXIT_LEDGER.clear()
    daemon._EGRESS_COOLING.clear()
    daemon._SLOT_REDIRECT_RR["index"] = 0
    daemon._PROXY_GATE_LAST_USED["ts"] = 0.0
    daemon._PROXY_GATE_LAST_ROTATE["ts"] = 0.0
    daemon._DEAD_NODES.clear()
    daemon._SLOT_BOOT_TS = time.time() - 3600   # 超出启动宽限期，判据走稳态
    daemon._MAIN_SLOT_NAME["name"] = "slot-1"
    daemon._ACTIVE_WARP_IP = None
    daemon._proxy_dwell_since = 0.0
    daemon._api_stats.update({"calls": 0, "switched": 0, "coalesced": 0,
                              "retired": 0, "too_frequent": 0, "failed": 0})
    for s in WARP_SLOTS:
        daemon._slot_mark(s["name"], "ok")
    return types.SimpleNamespace(current=current, gate=gate_state)


# ── 1. 配置层 ──────────────────────────────────────────────────────────

def test_consumer_slots_appends_proxy_gate():
    slots = config.consumer_slots()
    assert len(slots) == 7
    gate = slots[-1]
    assert gate["name"] == "slot-7"
    assert gate["kind"] == "proxy"
    assert gate["port"] == 7897          # 避开 7891(LAN) 的顺延编号
    assert gate["clash_api_port"] == 9097  # 避开 9091(h2) 的顺延编号
    assert gate["config_file"].endswith("singbox-config.json-slot7.json")
    assert gate["pool_selector"] == "proxy"
    # 常规闸口不带 kind，池闸必须带 —— 这是所有分支判据的分界
    assert all("kind" not in s for s in slots[:-1])


def test_consumer_slots_proxy_gate_disabled(monkeypatch):
    monkeypatch.setitem(config.CFG["singbox"], "proxy_gate", False)
    slots = config.consumer_slots()
    assert len(slots) == 6
    assert all(s.get("kind") != "proxy" for s in slots)


def test_make_config_default_to_pool(monkeypatch):
    """第七闸口的配置：warp selector 的 default 指池 selector。"""
    warps = [{"tag": "WARP-A", "server_port": 1, "type": "socks"},
             {"tag": "WARP-B", "server_port": 2, "type": "socks"}]
    monkeypatch.setattr(parse_subscription, "_load_warp_outbounds",
                        lambda: list(warps))
    monkeypatch.setattr(parse_subscription, "_load_dormant_outbounds",
                        lambda: [])
    monkeypatch.setattr(parse_subscription, "_load_dormant_state",
                        lambda cur=None: set())
    monkeypatch.setattr(parse_subscription, "write_sub_stats",
                        lambda *a, **k: None)
    nodes = [{"tag": "n1", "type": "socks", "server": "1.2.3.4",
              "server_port": 443}]
    pool_sel = config.CFG["singbox"]["selector"]
    warp_sel = config.CFG["warp"]["selector"]

    cfg_gate = parse_subscription.make_config(nodes, 7897, 9097, "s",
                                               slot_index=6,
                                               default_to_pool=True)
    sel = [o for o in cfg_gate["outbounds"] if o.get("tag") == warp_sel][0]
    assert sel["default"] == pool_sel
    assert pool_sel in sel["outbounds"]   # 池 selector 必须是它的成员

    cfg_normal = parse_subscription.make_config(nodes, 7896, 9096, "s",
                                                 slot_index=1)
    sel = [o for o in cfg_normal["outbounds"] if o.get("tag") == warp_sel][0]
    assert sel["default"] == "WARP-B"     # slot-2 轮转到的 WARP 线


# ── 2. 严格兜底：常规闸口有得选时绝不选池闸 ────────────────────────────

def test_pick_never_returns_gate_when_warp_candidate_exists(isolated):
    daemon._slot_mark("slot-7", "ok")
    s, tag, ip = daemon._pick_healthy_slot(None)
    assert s["name"] != "slot-7"
    assert tag == "WARP-B"


def test_pick_returns_gate_only_when_no_warp_candidates(isolated):
    for s in WARP_SLOTS:
        daemon._slot_mark(s["name"], "bad")
    daemon._slot_mark("slot-7", "ok")
    s, tag, ip = daemon._pick_healthy_slot(None)
    assert s["name"] == "slot-7"
    assert tag == "n1"                    # 它的「线」= 当前池节点
    assert ip is None
    assert daemon._PROXY_GATE_LAST_USED["ts"] > 0


def test_pick_returns_none_when_gate_also_unavailable(isolated):
    for s in WARP_SLOTS:
        daemon._slot_mark(s["name"], "bad")
    daemon._slot_mark("slot-7", "bad")   # 池闸也坏了：回到旧语义
    s, tag, ip = daemon._pick_healthy_slot(None)
    assert s is None and tag is None and ip is None


def test_fast_switch_falls_to_pool_on_main_path(isolated):
    """主路径秒切兜底：六个常规闸口全坏 → 主出口翻到轮换池 proxy。"""
    for s in WARP_SLOTS:
        daemon._slot_mark(s["name"], "bad")
    daemon._slot_mark("slot-7", "ok")
    calls = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(daemon, "_switch_warp",
                   lambda tag, ip=None: calls.append(tag) or True)
    try:
        r = daemon._fast_switch("rate429")
    finally:
        monkey.undo()
    assert r is not None
    assert r["tag"] == "proxy"
    assert calls == ["proxy"]             # 主进程 selector 翻到池
    assert daemon._proxy_dwell_since > 0  # 落池计时已启动（回切判据要用）
    assert daemon._MAIN_SLOT_NAME["name"] is None


def test_fast_switch_prefers_warp_slot_over_gate(isolated):
    daemon._slot_mark("slot-7", "ok")
    calls = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(daemon, "_switch_warp",
                   lambda tag, ip=None: calls.append(tag) or True)
    try:
        r = daemon._fast_switch("rate429")
    finally:
        monkey.undo()
    assert r is not None and r["tag"] != "proxy"
    assert calls and calls[0] != "proxy"


# ── 3. 池闸报障 = 池内换节点，绝不换 WARP 线 ────────────────────────────

def test_rotate_on_gate_rotates_pool_node(isolated, monkeypatch):
    """池闸收到 429：常规闸口全坏 → 池内换节点，重试目标还是它自己。"""
    for s in WARP_SLOTS:
        daemon._slot_mark(s["name"], "bad")
    daemon._slot_mark("slot-7", "ok")
    monkeypatch.setattr(daemon, "pick_next_node",
                        lambda cur, nodes, fail, count_call=True: "n2")

    out = daemon._rotate_slot_once(GATE, "rate429")
    assert out["switched"] is True
    assert out["switch_to"] == "slot-7"
    assert out["switch_to_url"] == "http://127.0.0.1:7897"
    assert out["switch_to_tag"] == "n2"
    # 给 9router 的「出口已变」证据：池节点没有 /128 地址，靠这两项
    assert out["switched_at"] > 0
    assert out["switch_to_node"] == "n2"
    assert out["switch_to_egress"] is None
    assert isolated.gate["puts"] == ["n2"]       # PUT 打在池闸自己的 selector
    assert isolated.gate["now"] == "n2"
    # 绝不进 WARP 修整链路（那里会 claim WARP 线）
    assert daemon._slot_health("slot-7")["status"] == "ok"


def test_rotate_on_gate_returns_to_warp_slot_when_one_recovers(isolated,
                                                               monkeypatch):
    """池闸报 429 时若已有常规闸口恢复，重试目标立刻回常规闸口。"""
    daemon._slot_mark("slot-7", "ok")
    # 只有 slot-3 恢复健康，其余常规闸口仍坏
    for s in WARP_SLOTS:
        daemon._slot_mark(s["name"], "bad")
    daemon._slot_mark("slot-3", "ok")
    monkeypatch.setattr(daemon, "pick_next_node",
                        lambda cur, nodes, fail, count_call=True: "n2")

    out = daemon._rotate_slot_once(GATE, "rate429")
    assert out["switched"] is True
    assert out["switch_to"] == "slot-3"
    assert out["switch_to_url"] == "http://127.0.0.1:7893"
    assert out["switch_to_tag"] == "WARP-C"
    # 常规闸口能接：池内不用换节点
    assert isolated.gate["puts"] == []


def test_rotate_on_gate_coalesces_burst(isolated, monkeypatch):
    for s in WARP_SLOTS:
        daemon._slot_mark(s["name"], "bad")
    daemon._slot_mark("slot-7", "ok")
    monkeypatch.setattr(daemon, "pick_next_node",
                        lambda cur, nodes, fail, count_call=True: "n2")
    first = daemon._rotate_slot_once(GATE, "rate429")
    assert first["reason"] == "ok"
    second = daemon._rotate_slot_once(GATE, "rate429")
    assert second["reason"] == "coalesced"   # 合并窗内只换一次
    # 没真换节点就不能给「出口已变」的证据，否则 9router 会白打一发
    assert not second.get("switch_to_node")
    assert second.get("switch_to_url") is None
    assert isolated.gate["puts"] == ["n2"]


def test_rotate_on_gate_all_exhausted_signals_restart(isolated, monkeypatch):
    """池也换不出节点（pick_next_node=None）：收口语义与常规闸口一致。"""
    for s in WARP_SLOTS:
        daemon._slot_mark(s["name"], "bad")
    daemon._slot_mark("slot-7", "ok")
    monkeypatch.setattr(daemon, "pick_next_node",
                        lambda cur, nodes, fail, count_call=True: None)
    out = daemon._rotate_slot_once(GATE, "rate429")
    assert out["reason"] == "no_healthy_slot"
    assert out["restart_required"] is True
    assert daemon._slot_health("slot-7")["status"] == "bad"


def test_redirect_payload_exposes_pool_node_for_gate(isolated):
    """常规闸口全坏时切到第七闸口：回包带池节点身份，9router 才认得切线。"""
    for s in WARP_SLOTS:
        daemon._slot_mark(s["name"], "bad")
    daemon._slot_mark("slot-7", "ok")
    out = daemon._slot_redirect_payload(
        WARP_SLOTS[0], "WARP-A", "2606:a::1", "rate429", time.time() - 5)
    assert out["switch_to"] == "slot-7"
    assert out["switch_to_url"] == "http://127.0.0.1:7897"
    assert out["switch_to_node"] == "n1"
    assert out["switch_to_egress"] is None


def test_redirect_payload_warp_slot_has_no_pool_node(isolated):
    """常规闸口能接时，回包不得冒充池闸切线。"""
    out = daemon._slot_redirect_payload(
        WARP_SLOTS[0], "WARP-A", "2606:a::1", "rate429", time.time() - 5)
    assert out["switch_to"] != "slot-7"
    assert out["switch_to_node"] is None


# ── 4. 池闸不进任何 WARP 线机制 ─────────────────────────────────────────

def test_assign_consumer_slots_never_claims_for_gate(isolated, monkeypatch):
    claimed = []
    monkeypatch.setattr(daemon, "_slot_claim",
                        lambda slot, extra_avoid=None:
                        (claimed.append(slot["name"]) or ("WARP-NEW", None)))
    monkeypatch.setattr(daemon, "_probe_line_for_slots",
                        lambda tag, ip, fresh=False: {"verdict": "OK",
                                                      "sec": 1.0})
    # 坏闸才走 claim：slot-1 在服务被跳过，slot-2..6 正常认领，池闸绝不能进
    for s in WARP_SLOTS[1:]:
        daemon._slot_mark(s["name"], "bad")
    daemon.assign_consumer_slots()
    assert "slot-7" not in claimed
    assert sorted(claimed) == ["slot-%d" % i for i in range(2, 7)]


def test_probe_fill_bad_gate_skips_proxy_gate(isolated, monkeypatch):
    """探测补闸不会把 WARP 线塞进池闸 —— 塞了它就不再是纯池闸。"""
    monkeypatch.setattr(daemon, "_channel_verdict_snapshot",
                        lambda tag, ip: {"verdict": "OK", "sec": 1.0,
                                         "cached": True, "source": "channel"})
    puts = []
    monkeypatch.setattr(daemon, "_slot_put",
                        lambda slot, tag: puts.append((slot["name"], tag))
                        or True)
    daemon._slot_mark("slot-4", "bad")
    daemon._slot_mark("slot-7", "bad")
    daemon._probe_line_for_slots("WARP-NEW", "2606:n::9")
    assert ("slot-4", "WARP-NEW") in puts
    assert all(name != "slot-7" for name, _ in puts)


# ── 5. keeper：健康结论 / 死节点 / 落池开关 ─────────────────────────────

def test_maintain_marks_gate_ok_when_pool_healthy(isolated):
    daemon._proxy_gate_maintain(GATE)
    assert daemon._slot_health("slot-7")["status"] == "ok"
    assert isolated.gate["puts"] == []      # 不在役、节点没死：不轮换


def test_maintain_rotates_dead_node(isolated, monkeypatch):
    daemon._DEAD_NODES.add("n1")
    monkeypatch.setattr(daemon, "pick_next_node",
                        lambda cur, nodes, fail, count_call=True: "n2")
    daemon._proxy_gate_maintain(GATE)
    assert isolated.gate["now"] == "n2"
    assert daemon._slot_health("slot-7")["status"] == "ok"


def test_maintain_marks_bad_when_unreachable(isolated, monkeypatch):
    monkeypatch.setattr(daemon, "slot_singbox_api",
                        lambda slot, path, method="GET", body=None,
                        timeout=3, silent=False: (0, None))
    daemon._proxy_gate_maintain(GATE)
    assert daemon._slot_health("slot-7")["status"] == "bad"


def test_maintain_respects_proxy_fallback_disabled(isolated, monkeypatch):
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", False)
    daemon._proxy_gate_maintain(GATE)
    assert daemon._slot_health("slot-7")["status"] == "bad"
    assert daemon._slot_health("slot-7")["verdict"] == "disabled"


def test_maintain_proactive_rotates_only_when_serving(isolated, monkeypatch):
    """待命时不轮换（省配额）；在役且到节奏才换。"""
    monkeypatch.setattr(daemon, "pick_next_node",
                        lambda cur, nodes, fail, count_call=True: "n2")
    # 待命：不换
    daemon._PROXY_GATE_LAST_USED["ts"] = time.time() - 3600
    daemon._proxy_gate_maintain(GATE)
    assert isolated.gate["puts"] == []
    # 在役且轮换间隔已过：换
    daemon._PROXY_GATE_LAST_USED["ts"] = time.time()
    daemon._PROXY_GATE_LAST_ROTATE["ts"] = time.time() - 3600
    daemon._proxy_gate_maintain(GATE)
    assert isolated.gate["puts"] == ["n2"]
