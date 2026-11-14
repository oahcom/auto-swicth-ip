import sys
from collections import defaultdict

sys.path.insert(0, "/home/administrator/dkk-projects/auto-switch-ip")

import daemon as d

# already_retired veto（2026-11-14 wfgkef17a 三连撞同死 IP 实测）：
# 报障地址 == 当前出口时「已换走」不成立 —— 两条 retired 判据都会说谎：
#   1. exit ledger 只看「这个地址曾经被换走」（IP 回收后同一 /128 又回来，旧记录还在）；
#   2. switched_at > request_started_at 是时刻推断，重试沿用原请求的开始时间恒真。
# 此时原地重连必然还是同一个死地址，必须真换线。veto 只用于否定「已换走」，
# 不用于肯定（判不出时保持原行为）。

SLOT = {"name": "slot-1", "port": 7890}
CUR_IP = "2a09:bac5:2a94:2482::3a3:55"
OTHER_IP = "2a09:bac5:495b:16c8::245:98"


def _patch(monkeypatch, cur_ip=CUR_IP, ip_retired=True, switched_at=0.0):
    reasons = []
    marked = []
    monkeypatch.setattr(d, "_mark_egress_cooling", lambda *a, **k: None)
    monkeypatch.setattr(d, "_mark_warp_ip_used",
                        lambda *a, **k: marked.append(a[0] if a else None))
    monkeypatch.setattr(d, "_api_cfg",
                        lambda: {"coalesce_ms": 800, "slot_cooldown_sec": 15})
    monkeypatch.setattr(d, "_slot_current",
                        lambda slot: ("WARP-H2-FRA-77", cur_ip))
    monkeypatch.setattr(d, "_probe_verdict_for_ip", lambda *a, **k: None)
    monkeypatch.setattr(d, "_slot_health", lambda name: {
        "status": "ok", "switched_at": switched_at, "last_rotate_ts": 0.0})
    monkeypatch.setattr(d, "_slot_ip_is_retired", lambda *a, **k: ip_retired)
    monkeypatch.setattr(d, "_warp_ip_is_blocked", lambda ip, t0: False)
    monkeypatch.setattr(d, "_slot_begin_repair", lambda name, reason: False)
    monkeypatch.setattr(d, "_slot_redirect_payload",
                        lambda slot, cur_tag, ip_, reason, *a, **k:
                        reasons.append(reason) or {"ok": True, "reason": reason})
    monkeypatch.setattr(d, "_rotate_gap", lambda: 0)
    monkeypatch.setattr(d, "_api_stats", defaultdict(int))
    monkeypatch.setattr(d, "_slot_mark", lambda *a, **k: None)
    monkeypatch.setattr(d, "log", lambda *a, **k: None)
    return reasons, marked


def test_veto_when_reported_ip_is_still_current(monkeypatch):
    # 报障地址 == 当前出口：台账/时刻推断说「换走过」也必须真换线
    reasons, marked = _patch(monkeypatch, cur_ip=CUR_IP, ip_retired=True)
    out = d._rotate_slot_once(dict(SLOT), "rate429", request_started_at=100.0,
                              egress_ip=CUR_IP, req_id="req-veto")
    assert out["reason"] != "already_retired"
    assert reasons and reasons[-1] != "already_retired"
    # 真换线路径必须把当前出口标脏（不能白跳）
    assert CUR_IP in marked


def test_already_retired_stands_when_ip_moved_away(monkeypatch):
    # 迟到报障：地址 != 当前出口（真换走了）→ 重连即可，不烧新线
    reasons, _ = _patch(monkeypatch, cur_ip=CUR_IP, ip_retired=True)
    out = d._rotate_slot_once(dict(SLOT), "rate429", request_started_at=100.0,
                              egress_ip=OTHER_IP, req_id="req-late")
    assert out["reason"] == "already_retired"


def test_no_egress_ip_keeps_timestamp_inference(monkeypatch):
    # 调用方没给地址：switched_at > request_started_at 的时刻推断照旧生效
    reasons, _ = _patch(monkeypatch, cur_ip=CUR_IP, ip_retired=False,
                        switched_at=200.0)
    out = d._rotate_slot_once(dict(SLOT), "rate429", request_started_at=100.0,
                              egress_ip=None, req_id="req-noip")
    assert out["reason"] == "already_retired"


def test_veto_requires_ip_evidence(monkeypatch):
    # 只有时刻推断（无台账记录）且地址 == 当前出口：veto 同样否决
    reasons, marked = _patch(monkeypatch, cur_ip=CUR_IP, ip_retired=False,
                             switched_at=200.0)
    out = d._rotate_slot_once(dict(SLOT), "rate429", request_started_at=100.0,
                              egress_ip=CUR_IP, req_id="req-veto-ts")
    assert out["reason"] != "already_retired"
    assert CUR_IP in marked
