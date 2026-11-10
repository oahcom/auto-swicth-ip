import sys

sys.path.insert(0, "/home/administrator/dkk-projects/auto-switch-ip")

import daemon as d

# 真实流量撞墙（429，或 20s 连响应头都不回的 transport）时，3KB 小探针往往在
# 几秒前刚判过 OK。等探针自己改口太慢，所以按 asip 自己看到的出口地址打一段
# 90 秒短冷却：这段时间不再把这个 /128 选给任何请求。
#
# 与 ban_until 的分工：ban_until 按上游 Retry-After 走（生产 429 兜底 12 小时），
# 是长期账；短冷却只求躲开当前这一波，不写台账、不参与隔离判定。

SLOTS = [
    {"name": "slot-1", "port": 7890},
    {"name": "slot-2", "port": 7892},
    {"name": "slot-3", "port": 7893},
]

IPS = {
    "slot-1": ("WARP-A", "2a09::a"),
    "slot-2": ("WARP-B", "2a09::b"),
    "slot-3": ("WARP-C", "2a09::c"),
}


def _patch(monkeypatch):
    monkeypatch.setattr(d, "consumer_slots", lambda: list(SLOTS))
    monkeypatch.setattr(d, "_slot_health", lambda name: {"status": "ok"})
    monkeypatch.setattr(d, "_slot_current", lambda slot: IPS[slot["name"]])
    monkeypatch.setattr(d, "_slot_ip_is_quarantined", lambda ip, t0: False)
    monkeypatch.setattr(d, "_slot_serving_name", lambda: None)
    monkeypatch.setattr(d, "_slot_pick_key", lambda s, tag, ip, t0, serving: (0, 0))
    d._EGRESS_COOLING.clear()


def test_cooling_takes_an_address_out_of_the_pool(monkeypatch):
    _patch(monkeypatch)
    now = d.time.time()
    d._mark_egress_cooling("2a09::b", now)
    assert d._egress_cooling_left("2a09::b", now) > 80

    slot, tag, ip = d._pick_healthy_slot("slot-1")
    assert slot is not None
    assert ip == "2a09::c", "冷却中的地址不能被选回来"


def test_cooling_expires_after_the_configured_window(monkeypatch):
    _patch(monkeypatch)
    now = d.time.time()
    d._mark_egress_cooling("2a09::b", now)
    assert d._egress_cooling_left("2a09::b", now + 91) == 0.0
    assert d._egress_cooling_left("2a09::c", now) == 0.0


def test_all_standbys_cooling_returns_no_standby(monkeypatch):
    _patch(monkeypatch)
    now = d.time.time()
    d._mark_egress_cooling("2a09::b", now)
    d._mark_egress_cooling("2a09::c", now)
    assert d._pick_healthy_slot("slot-1") == (None, None, None)


def test_cooling_can_be_disabled_by_config(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(d, "_egress_cooling_sec", lambda: 0.0)
    d._mark_egress_cooling("2a09::b", d.time.time())
    assert d._egress_cooling_left("2a09::b") == 0.0
    slot, tag, ip = d._pick_healthy_slot("slot-1")
    assert ip == "2a09::b"


def test_expired_entries_are_pruned_on_next_mark(monkeypatch):
    _patch(monkeypatch)
    now = d.time.time()
    d._mark_egress_cooling("2a09::b", now - 500)
    d._mark_egress_cooling("2a09::c", now)
    assert "2a09::b" not in d._EGRESS_COOLING
    assert "2a09::c" in d._EGRESS_COOLING


def test_empty_address_is_ignored(monkeypatch):
    _patch(monkeypatch)
    d._mark_egress_cooling("", d.time.time())
    d._mark_egress_cooling(None, d.time.time())
    assert d._EGRESS_COOLING == {}
