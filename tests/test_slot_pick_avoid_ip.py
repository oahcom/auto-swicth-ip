import sys

sys.path.insert(0, "/home/administrator/dkk-projects/auto-switch-ip")

import daemon as d

# 秒切选槽的两条硬约束：
#  1. 不能把刚在这条请求上吃了 429 的出口地址再选回来 —— 六条线共用一个 /64 池，
#     换一台闸完全可能挂着同一个 /128（实测 waxmn5h09：slot-6 报障的
#     2a09:bac5:495b:e6::17:368 正是 slot-3 的当前出口）。
#  2. 全都撞过就返回「没有备槽」，让调用方停手，而不是挑一个必然再 429 的地址。

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


def _patch(monkeypatch, ips):
    monkeypatch.setattr(d, "consumer_slots", lambda: list(SLOTS))
    monkeypatch.setattr(d, "_slot_health", lambda name: {"status": "ok"})
    monkeypatch.setattr(d, "_slot_current", lambda slot: ips[slot["name"]])
    monkeypatch.setattr(d, "_slot_ip_is_quarantined", lambda ip, t0: False)
    monkeypatch.setattr(d, "_slot_serving_name", lambda: None)
    monkeypatch.setattr(d, "_slot_pick_key", lambda s, tag, ip, t0, serving: (0, 0))


def test_skips_address_that_just_failed(monkeypatch):
    _patch(monkeypatch, IPS)
    slot, tag, ip = d._pick_healthy_slot("slot-1", None, {"2a09::b"})
    assert slot is not None
    assert ip != "2a09::b"
    assert slot["name"] == "slot-3"


def test_returns_no_standby_when_every_candidate_shares_the_failed_address(monkeypatch):
    _patch(monkeypatch, {
        "slot-1": ("WARP-A", "2a09::a"),
        "slot-2": ("WARP-B", "2a09::bad"),
        "slot-3": ("WARP-C", "2a09::bad"),
    })
    slot, tag, ip = d._pick_healthy_slot("slot-1", None, {"2a09::bad"})
    assert slot is None and tag is None and ip is None


def test_without_avoid_ips_behaviour_is_unchanged(monkeypatch):
    _patch(monkeypatch, IPS)
    slot, tag, ip = d._pick_healthy_slot("slot-1", None)
    assert slot is not None
    assert slot["name"] in ("slot-2", "slot-3")
