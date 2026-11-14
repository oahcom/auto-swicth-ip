"""①组保真契约 · warming 置位时机与排空闸顺序（附件 A9、附件 B3/B4）。

【防什么事故】warming 在闸门拒绝时被置位 → 这条线被所有探测路径**永久跳过**
（僵尸线）；排空闸/策略闸重排顺序 → 拒绝路径互相遮蔽（夜间锁下白跑排空、
排空状态被清后策略拒绝遗留「已排空」假象）。
【事故出处】附件 A9「warming 只在全部闸门放行后置位；拒绝时置→线被永久
跳过」；附件 B3/B4 顺序敏感。
【三分法】A 保真契约（warming 时机）；门序契约见 test_fidelity_gate_order_b4.py。
【现状记录·B4 修复后重验】live 闸门次序 = 熔断 → 夜间 → 策略闸（渠道锁 →
重启额度 → 档位）→ 排空闸（CV 锁外）→ warming=True。变更理由：旧次序把
排空闸排在渠道锁/额度/档位之前，被策略拒绝的线先经排空闸放行清掉
`_RETIRED_AT`/`_DRAIN_PENDING` 却不重启，下一轮落「台账洞」分支刷假告警
（2026-11-14 18:03 WARP-H2-LAX-175 实证）。故本组只钉 warming 时机
（S1~S4）与「任何拒绝不置 warming」；「策略拒绝优先于排空」「排空闸在 CV
锁外」已由 gate_order_b4 组钉住，勿在本文件重复。
"""
import time

import pytest

import daemon as d


MEMBER = {"tag": "WARP-WG-TEST", "port": 17999, "kind": "wg"}


@pytest.fixture
def restart_env(monkeypatch):
    """_warp_line_restart 离线跑桩：可控闸门 + warming 时机观测。"""
    obs = {"drain_calls": 0, "restart_calls": 0, "warming_at_restart": None}
    monkeypatch.setattr(d, "active_warp_tag", lambda: "OTHER-TAG")
    monkeypatch.setattr(d, "_night_block_restart", lambda *a, **k: False)

    def _drain(member, now, reason):
        obs["drain_calls"] += 1
        return True, "ok"

    monkeypatch.setattr(d, "_drain_gate", _drain)
    monkeypatch.setattr(d, "warp_line_busy", lambda *a, **k: False)
    monkeypatch.setattr(d, "_acquire_refresh_slot", lambda tag: True)
    monkeypatch.setattr(d, "_release_refresh_slot", lambda tag: None)
    monkeypatch.setattr(d, "_line_enabled", lambda m: True)
    monkeypatch.setattr(d, "_warp_line_restart_allowed", lambda st, now: True)
    monkeypatch.setattr(d, "_invalidate_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(d, "_RESTART_SEM", None)

    def _restart(member):
        obs["restart_calls"] += 1
        obs["warming_at_restart"] = d._warp_line_state(
            member["tag"]).get("warming")
        return False  # 失败路径即可，不必走完就绪检查

    monkeypatch.setattr(d, "_restart_warp_line", _restart)
    monkeypatch.setattr(d, "_restart_outcome_note", lambda *a, **k: None)
    monkeypatch.setattr(d, "_wait_port_ready", lambda *a, **k: False)
    monkeypatch.setattr(d, "_probe_refresh_egress", lambda *a, **k: None)
    d._WARP_LINE_STATE.clear()
    return obs


def test_warming_set_only_after_all_gates_pass(restart_env):
    """全部闸门放行后才置 warming=True（附件 A9 核心）。"""
    assert d._warp_line_restart(MEMBER, "probe") is False  # restart 失败返回
    assert restart_env["restart_calls"] == 1
    assert restart_env["warming_at_restart"] is True  # 置位先于动作
    # 失败路径会清 warming，不留僵尸
    assert d._warp_line_state(MEMBER["tag"]).get("warming") is False


def test_rejection_paths_never_leave_warming(restart_env, monkeypatch):
    """任何闸门拒绝都不置 warming（拒绝时置→永久跳过，附件 A9）。"""
    # 排空闸拒绝
    monkeypatch.setattr(d, "_drain_gate",
                        lambda *a, **k: (False, "busy draining"))
    assert d._warp_line_restart(MEMBER, "probe") is False
    assert restart_env["restart_calls"] == 0
    assert d._warp_line_state(MEMBER["tag"]).get("warming") is False
    # 渠道锁拒绝（排在排空闸之前，附件 B4；排空不被触发）
    monkeypatch.setattr(d, "_drain_gate", lambda *a, **k: (True, "ok"))
    d._warp_line_update(MEMBER["tag"], ban_hold_until=time.time() + 600)
    assert d._warp_line_restart(MEMBER, "probe") is False
    assert restart_env["restart_calls"] == 0
    assert d._warp_line_state(MEMBER["tag"]).get("warming") is False
    # 档位停用拒绝
    d._warp_line_update(MEMBER["tag"], ban_hold_until=0.0)
    monkeypatch.setattr(d, "_line_enabled", lambda m: False)
    assert d._warp_line_restart(MEMBER, "probe") is False
    assert d._warp_line_state(MEMBER["tag"]).get("warming") is False
    # 重启额度拒绝
    monkeypatch.setattr(d, "_line_enabled", lambda m: True)
    monkeypatch.setattr(d, "_warp_line_restart_allowed", lambda st, now: False)
    assert d._warp_line_restart(MEMBER, "probe") is False
    assert d._warp_line_state(MEMBER["tag"]).get("warming") is False
    assert restart_env["restart_calls"] == 0


def test_night_reject_short_circuits_before_drain(restart_env, monkeypatch):
    """夜间锁拒绝优先于排空闸：策略拒绝不与排空纠缠（附件 B4）。"""
    monkeypatch.setattr(d, "_night_block_restart", lambda *a, **k: True)
    assert d._warp_line_restart(MEMBER, "probe") is False
    assert restart_env["drain_calls"] == 0  # 排空闸未被触发
    assert restart_env["restart_calls"] == 0
    assert d._warp_line_state(MEMBER["tag"]).get("warming") is False


def test_warming_self_guard_prevents_double_restart(restart_env):
    """warming 置位期间重复调用直接拒绝（防重复重启同一线）。"""
    d._warp_line_update(MEMBER["tag"], warming=True)
    assert d._warp_line_restart(MEMBER, "probe") is False
    assert restart_env["restart_calls"] == 0
