#!/usr/bin/env python3
"""D1/D3/D6/D14 行为修复的回归测试（无网络、<10s）。

出处：2026-11-13 四线交叉取证（trackA–D）与主控对码：
  - D3 already_retired 不再下发 switch_to_url（实测 357 次白跳 74.5% 落地仍 429）
  - D6 too_frequent 与主路径对齐（ok=False）且原地重连，不白跳第三条线
  - D1 transport 报障但地址最新探针判 OK（TTL 窗口，默认 1800s）→ 回 line_ok
    不换线；二击出局：同址 10min 内第 2 次报障照常换线
  - D14 闸口分支透传 borrowed：借道 429 不冷节点、不换节点（borrowed_skip）
"""
import time

import pytest

import daemon


SLOT = {"name": "slot-2", "selector": "opencode-out-2", "port": 7892}
GATE = {"name": "slot-7", "kind": "proxy", "selector": "opencode-out-7",
        "pool_selector": "proxy", "port": 7897, "clash_api_port": 9097,
        "config_file": "/tmp/asip-test-slot7-rotate-fix.json"}
STANDBY = ({"name": "slot-3", "selector": "opencode-out-3", "port": 7893},
           "WARP-B", "2606:b::2")


class _NoopThread:
    def __init__(self, target=None, args=(), kwargs=None, **kw):
        pass

    def start(self):
        pass


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(daemon.threading, "Thread", _NoopThread)
    monkeypatch.setattr(daemon, "_slot_current",
                        lambda slot: ("WARP-A", "2606:a::1"))
    monkeypatch.setattr(daemon, "_probe_verdict_for_ip",
                        lambda ip, now=None: None)
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked",
                        lambda ip, now=None: False)
    monkeypatch.setattr(daemon, "_slot_ip_is_retired",
                        lambda name, ip, since: False)
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_mark_egress_cooling", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cool_egress_after_recheck",
                        lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_slot_begin_repair",
                        lambda name, reason: True)
    monkeypatch.setattr(daemon, "_warp_guard_for_reason",
                        lambda reason: 300.0)
    daemon._LINE_OK_STRIKES.clear()
    daemon._SLOT_HEALTH.clear()  # last_rotate_ts 跨测试残留会把第 2 次调用挡进 too_frequent
    return monkeypatch


# ---------- D3 / D6：回包形态 ----------

def test_already_retired_with_standby_still_returns_in_place(isolated, monkeypatch):
    """D3：即使有健康备槽，already_retired 也不下发 switch_to_url。

    白跳会把请求推到第三条线：实测 357 次白跳 74.5% 落地仍 429（trackc5）。
    """
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: STANDBY)
    monkeypatch.setattr(daemon, "_is_proxy_gate", lambda slot: False)
    out = daemon._slot_redirect_payload(SLOT, "WARP-A", "2606:new::9",
                                        "already_retired", 123.0)
    assert out["switch_to_url"] is None, "already_retired 必须原地重连"
    assert out["switched"] is False
    assert out["reason"] == "already_retired"
    assert out["active_egress"] == "2606:new::9"


def test_too_frequent_returns_in_place_and_not_ok(isolated, monkeypatch):
    """D6：too_frequent 与主路径对齐（ok=False）且不再白跳备槽。"""
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: STANDBY)
    monkeypatch.setattr(daemon, "_is_proxy_gate", lambda slot: False)
    out = daemon._slot_redirect_payload(SLOT, "WARP-A", "2606:a::1",
                                        "too_frequent", 123.0)
    assert out["switch_to_url"] is None, "too_frequent 原地重连（线刚换过）"
    assert out["ok"] is False, "与主路径回包对齐"
    assert out["reason"] == "too_frequent"
    assert not out.get("line_unknown")
    assert not out.get("restart_required")


def test_rate429_still_redirects_to_standby(isolated, monkeypatch):
    """对照：普通 rate429 仍然给备槽跳板（只收 already_retired/too_frequent）。"""
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: STANDBY)
    monkeypatch.setattr(daemon, "_is_proxy_gate", lambda slot: False)
    out = daemon._slot_redirect_payload(SLOT, "WARP-A", "2606:a::1",
                                        "rate429", 0.0)
    assert out["switch_to_url"] == "http://127.0.0.1:7893"
    assert out["switched"] is True


# ---------- D1：transport line_ok 早退 ----------

def test_transport_with_fresh_probe_ok_returns_line_ok(isolated, monkeypatch):
    """D1：transport 报障但 30s 内探针判过 OK → line_ok，不换线不认领修整。"""
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: STANDBY)
    monkeypatch.setattr(daemon, "_is_proxy_gate", lambda slot: False)
    daemon._WARP_PROBE_SEEN[("WARP-A", "2606:a::1")] = ("OK", time.time() - 5)
    rep = []
    monkeypatch.setattr(daemon, "_slot_begin_repair",
                        lambda name, reason: rep.append(reason) or True)
    r = daemon._rotate_slot_once(SLOT, "transport", req_id="T1")
    assert r["reason"] == "line_ok", r
    assert r["switch_to_url"] is None
    assert not rep, "line_ok 不得认领修整/换线"


def test_transport_stale_ok_outside_window_repairs(isolated, monkeypatch):
    """对照：OK 结论超出窗口（默认 1800s）不拦换线。"""
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: STANDBY)
    monkeypatch.setattr(daemon, "_is_proxy_gate", lambda slot: False)
    daemon._WARP_PROBE_SEEN[("WARP-A", "2606:a::1")] = ("OK", time.time() - 2000)
    rep = []
    monkeypatch.setattr(daemon, "_slot_begin_repair",
                        lambda name, reason: rep.append(reason) or True)
    r = daemon._rotate_slot_once(SLOT, "transport", req_id="T2")
    assert r["reason"] != "line_ok", r
    assert rep == ["transport"], "仍应换线"


def test_transport_mid_age_ok_within_ttl_hits_line_ok(isolated, monkeypatch):
    """窗口对齐探针 TTL：探针每线每 30min 一发，120s 前的 OK 仍是当前结论。"""
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: STANDBY)
    monkeypatch.setattr(daemon, "_is_proxy_gate", lambda slot: False)
    daemon._WARP_PROBE_SEEN[("WARP-A", "2606:a::1")] = ("OK", time.time() - 120)
    rep = []
    monkeypatch.setattr(daemon, "_slot_begin_repair",
                        lambda name, reason: rep.append(reason) or True)
    r = daemon._rotate_slot_once(SLOT, "transport", req_id="T2b")
    assert r["reason"] == "line_ok", r
    assert not rep, "TTL 内的 OK 应拦下换线"


def test_transport_second_strike_repairs(isolated, monkeypatch):
    """二击出局：同地址 10min 内第 2 次 transport 报障即使探针 OK 也换线。"""
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: STANDBY)
    monkeypatch.setattr(daemon, "_is_proxy_gate", lambda slot: False)
    daemon._WARP_PROBE_SEEN[("WARP-A", "2606:a::1")] = ("OK", time.time() - 5)
    rep = []
    monkeypatch.setattr(daemon, "_slot_begin_repair",
                        lambda name, reason: rep.append(reason) or True)
    r1 = daemon._rotate_slot_once(SLOT, "transport", req_id="T3a")
    assert r1["reason"] == "line_ok", r1
    # 跨过 3s 突发合并窗，模拟下一次独立报障
    daemon._LINE_OK_STRIKES["2606:a::1"] = [time.time() - 10]
    r2 = daemon._rotate_slot_once(SLOT, "transport", req_id="T3b")
    assert r2["reason"] != "line_ok", r2
    assert rep == ["transport"], "第 2 击必须换线"


def test_transport_burst_counts_one_strike(isolated, monkeypatch):
    """突发合并：3s 内连发（并发骑同一条慢线）只算一击，都回 line_ok。"""
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: STANDBY)
    monkeypatch.setattr(daemon, "_is_proxy_gate", lambda slot: False)
    daemon._WARP_PROBE_SEEN[("WARP-A", "2606:a::1")] = ("OK", time.time() - 5)
    rep = []
    monkeypatch.setattr(daemon, "_slot_begin_repair",
                        lambda name, reason: rep.append(reason) or True)
    r1 = daemon._rotate_slot_once(SLOT, "transport", req_id="T4a")
    r2 = daemon._rotate_slot_once(SLOT, "transport", req_id="T4b")
    assert r1["reason"] == "line_ok", r1
    assert r2["reason"] == "line_ok", r2
    assert not rep, "同一次突发不得触发换线"


def test_transport_probe_ok_recent_helper_window(isolated):
    """_probe_ok_recent 的窗口边界（含换线前实测台账回退）。"""
    now = time.time()
    daemon._WARP_PROBE_SEEN[("WARP-A", "2606:x::1")] = ("OK", now - 10)
    assert daemon._probe_ok_recent("2606:x::1", now, 30) is True
    assert daemon._probe_ok_recent("2606:x::1", now, 5) is False
    assert daemon._probe_ok_recent("2606:zz::1", now, 30) is False
    daemon._PROBE_BEFORE_ROTATE["WARP-Q|2606:y::1"] = {
        "verdict": "OK", "ts": now - 3}
    assert daemon._probe_ok_recent("2606:y::1", now, 30) is True
    assert daemon._probe_ok_recent(None, now, 30) is False


# ---------- D14：闸口 borrowed 透传 ----------

def test_gate_borrowed_skips_node_rotation(monkeypatch):
    """D14：借道 429 不冷节点、不换节点，reason_out=borrowed_skip。"""
    monkeypatch.setattr(daemon, "consumer_slots", lambda: [GATE])
    monkeypatch.setattr(daemon, "_proxy_gate_current",
                        lambda slot: ("n1", None))
    rotated = []
    cooled = []
    monkeypatch.setattr(daemon, "_proxy_gate_rotate_node",
                        lambda slot, node, reason: rotated.append(reason) or "n2")
    monkeypatch.setattr(daemon, "_cool_down_node",
                        lambda *a, **k: cooled.append(a))
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: STANDBY)
    out = daemon._rotate_proxy_gate_once(GATE, "rate429", borrowed=True)
    assert out["reason"] == "borrowed_skip", out
    assert not rotated, "借道不得换池节点"
    assert not cooled, "借道不得冷节点"
    assert out["switch_to_url"] == "http://127.0.0.1:7893"


def test_gate_own_429_still_rotates_node(monkeypatch):
    """对照：闸口自有 429 仍然池内换节点。"""
    monkeypatch.setattr(daemon, "consumer_slots", lambda: [GATE])
    monkeypatch.setattr(daemon, "_proxy_gate_current",
                        lambda slot: ("n1", None))
    rotated = []
    monkeypatch.setattr(daemon, "_proxy_gate_rotate_node",
                        lambda slot, node, reason: rotated.append(reason) or "n2")
    monkeypatch.setattr(daemon, "_cool_down_node", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None:
                            (None, None, None))
    out = daemon._rotate_proxy_gate_once(GATE, "rate429", borrowed=False)
    assert rotated == ["rate429"]
    assert out["switch_to_node"] == "n2", out
    assert out["switched"] is True
