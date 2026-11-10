import sys
from collections import defaultdict

sys.path.insert(0, "/home/administrator/dkk-projects/auto-switch-ip")

import daemon as d

# /rotate 报障时，短冷却必须落在「asip 自己从这台闸读到的出口」上，而不是
# 调用方报来的 egress_ip。后者在并发下可能是别的请求残留的地址（9router 侧
# currentAsipEgress 是进程级全局，实测 52% 的报障 IP 连 /64 都对不上）——
# 拿它冷却会罚一条无辜的线，真正该躲的那条继续放行。

SLOT = {"name": "slot-6", "port": 7896}
CUR_IP = "2a09:bac5:495e:263c::3cf:6c"
WRONG_IP = "2a09:bac5:a5ff:25a5::3c0:27"  # 别的请求残留


def _patch(monkeypatch, cur_ip=CUR_IP):
    cooled = []
    marked = []
    monkeypatch.setattr(d, "_mark_egress_cooling",
                        lambda ip, now=None: cooled.append(ip))
    monkeypatch.setattr(d, "_mark_warp_ip_used",
                        lambda *a, **k: marked.append((a, k)))
    monkeypatch.setattr(d, "_api_cfg",
                        lambda: {"coalesce_ms": 800, "slot_cooldown_sec": 15})
    monkeypatch.setattr(d, "_slot_current", lambda slot: ("WARP-H2-HKG-10", cur_ip))
    monkeypatch.setattr(d, "_probe_verdict_for_ip", lambda *a, **k: None)
    monkeypatch.setattr(d, "_slot_health", lambda name: {
        "status": "ok", "switched_at": 0.0, "last_rotate_ts": 0.0})
    monkeypatch.setattr(d, "_slot_ip_is_retired", lambda *a, **k: False)
    monkeypatch.setattr(d, "_warp_ip_is_blocked", lambda ip, t0: False)
    monkeypatch.setattr(d, "_slot_begin_repair", lambda name, reason: False)
    monkeypatch.setattr(d, "_slot_redirect_payload", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(d, "_rotate_gap", lambda: 0)
    monkeypatch.setattr(d, "_api_stats", defaultdict(int))
    monkeypatch.setattr(d, "log", lambda *a, **k: None)
    return cooled, marked


def _report(monkeypatch, reason, cur_ip=CUR_IP):
    cooled, marked = _patch(monkeypatch, cur_ip)
    out = d._rotate_slot_once(
        dict(SLOT), reason, request_started_at=None,
        egress_ip=WRONG_IP, req_id="req-test")
    assert out["ok"] is True
    return cooled, marked


def test_cooling_uses_the_slots_own_address_not_the_reported_one(monkeypatch):
    cooled, _ = _report(monkeypatch, "rate429")
    assert cooled == [CUR_IP]
    assert WRONG_IP not in cooled


def test_transport_also_cools_even_without_an_upstream_status(monkeypatch):
    cooled, marked = _report(monkeypatch, "transport")
    assert cooled == [CUR_IP]
    # transport 只换不标脏：上游根本没回话，不该把长期配额账算到这个地址头上。
    assert marked == []


def test_proactive_rotation_does_not_cool(monkeypatch):
    cooled, _ = _report(monkeypatch, "proactive")
    assert cooled == []


def test_abuse403_also_cools(monkeypatch):
    cooled, _ = _report(monkeypatch, "abuse403")
    assert cooled == [CUR_IP]


def test_no_address_means_no_cooling_and_no_crash(monkeypatch):
    cooled, _ = _report(monkeypatch, "rate429", cur_ip=None)
    assert cooled == []
