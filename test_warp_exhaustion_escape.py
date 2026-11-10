#!/usr/bin/env python3
"""全池换不出配额时的逃生路径（落池到机场池）。

背景（2026-11-09 21:00–23:00 实测）：备线的判据是「120s 内出现过 OK 的样本」，
而整个 WARP 地址空间被判 quota 时这个条件**永远无法满足** —— 没线 OK 就没有
备线，没备线就不换线，换了也不会有 OK。那两小时因此空转：

    本次不切 937 次 / 换线成功 4 次 / 惩罚期熔断 347 次
    同期 9router 报 432 条 503（当晚 598 条里的 72%）

改造后的规则：连续多次走到死锁分支后，允许把流量甩到机场池（另一个 IP 空间），
但必须同时满足「开关打开 + 池健康实探通过」。
"""
import time

import pytest

import daemon


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(daemon, "_WARP_EXHAUST_STREAK", 0)
    monkeypatch.setattr(daemon, "_PROXY_HEALTH_CACHE", None)
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "old-ip")
    monkeypatch.setattr(daemon, "_proxy_dwell_since", 0)
    monkeypatch.setattr(daemon, "_last_proxy_fallback_ts", 0.0)
    monkeypatch.setattr(daemon, "_proxy_fallback_ts_loaded", True)
    monkeypatch.setattr(daemon, "_write_json_atomic", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_begin_segment", lambda ts: None)
    monkeypatch.setattr(daemon, "_exit_ledger_note", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_after_warp_switch", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_port_open", lambda p: True)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setitem(daemon.CFG["warp"], "members",
                        [{"tag": "W1", "port": 10001, "service": "warp-W1"}])
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", True)
    monkeypatch.setitem(daemon.CFG["warp"], "warp_exhaust_streak_min", 3)
    # 死锁的前提：没有备线、等也等不到
    monkeypatch.setattr(daemon, "_warp_line_fresh_candidates",
                        lambda exclude, now=None: [])
    monkeypatch.setattr(daemon, "_warp_line_wait_fresh",
                        lambda exclude, deadline: [])
    yield


def _capture_switch(monkeypatch, restarted=None):
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp",
                        lambda tag, **kw: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_restart_warp_inplace",
                        lambda m, ip: (restarted.append(m["tag"]) if restarted
                                       is not None else None) or True)
    return switched


def test_deadlock_lands_on_pool_only_after_three_attempts(monkeypatch):
    """前两次按「单线抖动」留观，第三次才换地址空间。"""
    switched = _capture_switch(monkeypatch)
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda *a, **k: True)

    assert daemon._reconnect_warp_by_table("transport") is False
    assert daemon._reconnect_warp_by_table("transport") is False
    assert switched == [], "留观期内不得落池"
    assert daemon._WARP_EXHAUST_STREAK == 2

    assert daemon._reconnect_warp_by_table("transport") is True
    assert switched == ["proxy"], f"第三次应落池，实际 {switched}"
    assert daemon._WARP_EXHAUST_STREAK == 0, "落池后计数必须清零，否则下轮立刻再落"


def test_deadlock_does_not_land_when_pool_is_unhealthy(monkeypatch):
    """池健康实探不通过时绝不落池 —— 这就是 2026-10-26「落池即断流」的重演条件。"""
    switched = _capture_switch(monkeypatch)
    restarted = []
    monkeypatch.setattr(daemon, "_restart_warp_inplace",
                        lambda m, ip: restarted.append(m["tag"]) or True)
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda *a, **k: False)

    for _ in range(5):
        daemon._reconnect_warp_by_table("transport")

    assert switched == [], f"池不可用时不得切 proxy，实际 {switched}"
    assert restarted == ["W1"], "应退回原地重连当前线"


def test_pool_health_none_is_treated_as_unavailable(monkeypatch):
    """Clash API 抖动（None）不能当「可用」——未知不是健康。"""
    switched = _capture_switch(monkeypatch)
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda *a, **k: None)

    for _ in range(4):
        daemon._reconnect_warp_by_table("transport")

    assert switched == [], f"探不到池状态时不得落池，实际 {switched}"


def test_escape_disabled_keeps_old_behaviour(monkeypatch):
    """开关关着时行为必须与改造前一致：只记日志、不切任何出口。"""
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", False)
    switched = _capture_switch(monkeypatch)
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda *a, **k: True)

    for _ in range(6):
        assert daemon._reconnect_warp_by_table("transport") is False

    assert switched == [], f"开关关闭时不得切 proxy，实际 {switched}"


def test_streak_resets_once_a_candidate_appears(monkeypatch):
    """又出现备线了 → 死锁证据作废，计数清零，正常换线。"""
    switched = _capture_switch(monkeypatch)
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda *a, **k: True)
    daemon._reconnect_warp_by_table("transport")
    daemon._reconnect_warp_by_table("transport")
    assert daemon._WARP_EXHAUST_STREAK == 2

    m = {"tag": "W2", "port": 10002, "service": "warp-W2"}
    monkeypatch.setattr(daemon, "_warp_line_fresh_candidates",
                        lambda exclude, now=None: [(m, "2.2.2.2")])
    monkeypatch.setattr(daemon, "_warp_line_ok_fresh", lambda tag, now=None: True)
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: False)
    monkeypatch.setattr(daemon, "_acquire_refresh_slot", lambda tag: True)
    monkeypatch.setattr(daemon, "_release_refresh_slot", lambda tag: None)
    monkeypatch.setattr(daemon, "_clash_snapshot",
                        lambda tag: {"all": [m["tag"]], "now": m["tag"]})

    assert daemon._reconnect_warp_by_table("transport") is True
    assert switched == ["W2"]
    assert daemon._WARP_EXHAUST_STREAK == 0


def test_fallback_gate_blocks_landing_even_with_cooldown_clear(monkeypatch):
    """直接调 `_fallback_to_proxy` 也必须过池健康闸（四道调用点共用）。"""
    switched = _capture_switch(monkeypatch)
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda *a, **k: False)

    ok = daemon._fallback_to_proxy({"tag": "W1", "port": 10001}, "old-ip",
                                   time.time())

    assert ok is True, "仍应原地重连成功，只是不落池"
    assert switched == [], f"池不健康时不得落池，实际 {switched}"


def test_already_on_pool_never_blind_switches_back(monkeypatch):
    """已经在池上时不得"盲切回 WARP"。

    `_fallback_to_proxy` 的冷却分支会为了"别停在坏出口上"盲切其它 WARP 成员，
    但落池的前提恰恰是「WARP 换不出配额」。让它在这条路上盲切，等于把正在
    服务的流量从池上拽回一条被 quota 的线。离开池只走 `_maybe_failback_to_warp`。
    """
    switched = _capture_switch(monkeypatch)
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "_proxy_dwell_since", time.time())

    for _ in range(6):
        assert daemon._reconnect_warp_by_table("transport") is False

    assert switched == [], f"在池上时不得切任何出口，实际 {switched}"
