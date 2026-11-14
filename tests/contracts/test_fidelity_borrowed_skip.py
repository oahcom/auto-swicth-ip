"""①组保真契约 · borrowed_skip 处理语义（附件 A6 / 附件 D「borrowed 借道」右栏）。

【防什么事故】把 borrowed 三态折叠成普通 reason → 借道 429 冤杀好线/坏账：
被借线没坏，判死它=白烧一条线；本闸的 429 若被当借道放过 → 坏 IP 不标脏
反复回池。两头都是事故，语义边界必须钉住：
  - 置位后处理（本组，①）：只冷不判死、不标脏、不动被借线、回健康备槽；
  - 何时置位（③组，修前红修后绿）：9router 三发循环跨端口误标，另见
    test_acceptance_borrowed_mislabel.py。
【事故出处】附件 A6；3,313 次借道误记账（D14 已修闸口透传）；WG-21/误杀池 50/56。
【三分法】A 保真契约（语义层）——绝不可当坏行为修掉。
【取值】rate429_recheck_mode 取 config 实配。
"""
from collections import defaultdict

import pytest

import daemon as d


SLOT = {"name": "slot-1", "port": 7890}
CUR_IP = "2001:db8::bo1"


@pytest.fixture
def slot_env(monkeypatch):
    """_rotate_slot_once 离线跑桩（对齐 tests/test_already_retired_veto.py 口径）。"""
    rec = {"mark_dirty": [], "cool": [], "recheck": [], "repair": 0,
           "pick": 0, "reasons": []}
    monkeypatch.setattr(d, "_is_proxy_gate", lambda slot: False)
    monkeypatch.setattr(d, "_mark_warp_ip_used",
                        lambda *a, **k: rec["mark_dirty"].append(
                            (a[0], k.get("blocked"))))
    monkeypatch.setattr(d, "_repair_slot", lambda *a, **k: None)  # 修整线程不真跑
    monkeypatch.setattr(d, "_mark_egress_cooling",
                        lambda *a, **k: rec["cool"].append(a))
    monkeypatch.setattr(d, "_cool_egress_after_recheck",
                        lambda *a, **k: rec["recheck"].append(a))
    monkeypatch.setattr(d, "_api_cfg",
                        lambda: {"coalesce_ms": 800, "slot_cooldown_sec": 15,
                                 "rate429_recheck_mode":
                                     d._warp().get("rate429_recheck_mode")})
    monkeypatch.setattr(d, "_slot_current", lambda slot: ("WARP-BORROW", CUR_IP))
    monkeypatch.setattr(d, "_probe_verdict_for_ip", lambda *a, **k: None)
    monkeypatch.setattr(d, "_slot_health", lambda name: {
        "status": "ok", "switched_at": 0.0, "last_rotate_ts": 0.0})
    monkeypatch.setattr(d, "_slot_ip_is_retired", lambda *a, **k: False)
    monkeypatch.setattr(d, "_warp_ip_is_blocked", lambda ip, t0: False)
    monkeypatch.setattr(d, "_line_ok_strike_add", lambda *a, **k: 0)
    monkeypatch.setattr(d, "_probe_ok_recent", lambda *a, **k: False)

    def _begin(name, reason):
        rec["repair"] += 1
        return True

    monkeypatch.setattr(d, "_slot_begin_repair", _begin)

    def _pick(name, avoid_tags=None, avoid_ips=None):
        rec["pick"] += 1
        return ({"name": "slot-2", "port": 7891}, "WARP-HEALTHY", "2001:db8::h1")

    monkeypatch.setattr(d, "_pick_healthy_slot", _pick)
    monkeypatch.setattr(d, "_slot_no_usable_gate", lambda: False)

    orig_redirect = d._slot_redirect_payload

    def _redirect(slot, cur_tag, cur_ip, reason, *a, **k):
        rec["reasons"].append(reason)
        return orig_redirect(slot, cur_tag, cur_ip, reason, *a, **k)

    monkeypatch.setattr(d, "_slot_redirect_payload", _redirect)
    monkeypatch.setattr(d, "_rotate_gap", lambda: 0.0)
    monkeypatch.setattr(d, "_api_stats", defaultdict(int))
    return rec


def test_borrowed_skip_only_cools_not_kills(slot_env):
    """借道 429：只冷不判死、不标脏、不动被借线（附件 A6 语义层）。

    防什么事故：借道那一发不是本槽发起的（body/session 是原线的），单发
    429 不足以证明本槽额度耗尽 —— 判死=冤杀好线；标脏=坏账。
    """
    d._rotate_slot_once(SLOT, "rate429", request_started_at=1.0,
                        egress_ip="2001:db8::other", ban_after=60.0,
                        borrowed=True)
    assert slot_env["reasons"] == ["borrowed_skip"]
    assert slot_env["mark_dirty"] == []  # 不标脏
    assert slot_env["repair"] == 0  # 不认领修整 = 不动被借线
    # 先复测再定性（defer_verdict）：只打短冷，不进 24h 台账
    assert slot_env["recheck"] == [(CUR_IP, "WARP-BORROW", "rate429")]


def test_borrowed_skip_returns_healthy_backup_slot(slot_env):
    """借道失败仍回健康备槽让 9router 递下去（附件 D 右栏「回健康备槽」）。"""
    out = d._rotate_slot_once(SLOT, "rate429", request_started_at=1.0,
                              egress_ip="2001:db8::other", borrowed=True)
    assert slot_env["pick"] == 1
    assert out.get("switch_to") == "slot-2"


def test_own_gate_429_marks_dirty_contrast(slot_env):
    """对照：非借道的本闸 429 必须标脏（借道与否是语义边界）。"""
    d._rotate_slot_once(SLOT, "rate429", request_started_at=1.0,
                        egress_ip="2001:db8::other", ban_after=60.0,
                        borrowed=False)
    assert slot_env["mark_dirty"] == [(CUR_IP, True)]
    assert slot_env["recheck"] == []
    assert slot_env["reasons"] == ["ok"]


def test_borrowed_mode_from_config():
    """借道复测定性模式取 config 实配（默认 borrowed，非法值退回 borrowed）。
    """
    assert d._rate429_recheck_mode() == d._warp().get("rate429_recheck_mode")
