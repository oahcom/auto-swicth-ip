#!/usr/bin/env python3
"""状态表驱动的 WARP 探测器/换线单元测试（无网络、无 systemctl）。"""
import time

import pytest

import daemon


@pytest.fixture(autouse=True)
def _isolate_line_state(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {})
    monkeypatch.setattr(daemon, "_PROBER_STARTED", False)
    monkeypatch.setattr(daemon, "_MODEL_PROBE_SEM", None)
    monkeypatch.setattr(daemon, "_WARP_PROBING", set())
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_PROBE_LAST_VERDICT", {})
    monkeypatch.setattr(daemon, "_PROBE_BLOCKED_AT", {})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setenv("WARP_LINE_STATE_FILE_PATH", str(tmp_path / "line_state.json"))
    monkeypatch.setattr(daemon, "_save_warp_line_state", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 0.0)
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", None)
    yield


def _member(tag="W2", port=10002):
    return {"tag": tag, "port": port, "service": f"warp-{tag}"}


def test_ok_fresh_requires_same_ip_and_120s(monkeypatch):
    now = 1_700_000_000.0
    monkeypatch.setitem(daemon.CFG["warp"], "line_fresh_sec", 120)
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", last_ok_ip="1.1.1.1", last_ok_ts=now - 10)
    assert daemon._warp_line_ok_fresh("W2", now) is True
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2")["last_ok_ts"] = now - 121
    assert daemon._warp_line_ok_fresh("W2", now) is False
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="2.2.2.2", last_ok_ip="1.1.1.1", last_ok_ts=now)
    assert daemon._warp_line_ok_fresh("W2", now) is False


def test_reconnect_waits_and_does_not_switch_without_fresh(monkeypatch):
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)
    monkeypatch.setattr(daemon, "_warp_line_fresh_candidates",
                        lambda exclude, now=None: [])
    monkeypatch.setattr(daemon, "_warp_line_wait_fresh",
                        lambda exclude, deadline: [])
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp",
                        lambda tag, interrupt=False: switched.append(tag) or True)
    assert daemon._reconnect_warp_by_table("test") is False
    assert switched == []


def test_reconnect_switches_to_fresh_candidate(monkeypatch):
    m = _member()
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)
    monkeypatch.setattr(daemon, "_warp_line_fresh_candidates",
                        lambda exclude, now=None: [(m, "1.1.1.1")])
    monkeypatch.setattr(daemon, "_warp_line_ok_fresh", lambda tag, now=None: True)
    monkeypatch.setattr(daemon, "_acquire_refresh_slot", lambda tag: True)
    monkeypatch.setattr(daemon, "_release_refresh_slot", lambda tag: None)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp",
                        lambda tag, interrupt=False: switched.append(tag) or True)
    marked = []
    monkeypatch.setattr(daemon, "_mark_warp_ip_used",
                        lambda ip, now, **kw: marked.append(ip))
    assert daemon._reconnect_warp_by_table("test") is True
    assert switched == ["W2"]
    assert marked == ["1.1.1.1"]
    assert daemon._ACTIVE_WARP_IP == "1.1.1.1"
    assert daemon._warp_line_state("W2")["ip"] == "1.1.1.1"


def test_probe_unknown_ip_does_not_restart(monkeypatch):
    m = _member()
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: None)
    restarted = []
    monkeypatch.setattr(daemon, "_warp_line_restart",
                        lambda member, reason: restarted.append(member["tag"]) or True)
    daemon._probe_one_line(m)
    st = daemon._warp_line_state("W2")
    assert st["last_verdict"] == "unknown"
    assert st["next_probe_ts"] > time.time()
    assert restarted == []


def test_probe_quota_restarts_non_active_line(monkeypatch):
    m = _member()
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: "1.1.1.1")
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: False)
    monkeypatch.setattr(daemon, "_warp_probe_blind", lambda now=None: False)
    monkeypatch.setattr(daemon, "_probe_rate_allow", lambda: True)
    monkeypatch.setattr(daemon, "_warp_model_usable", lambda member, ip: False)
    monkeypatch.setitem(daemon._PROBE_LAST_VERDICT, "W2", "quota")
    restarted = []
    monkeypatch.setattr(daemon, "_warp_line_restart",
                        lambda member, reason: restarted.append(member["tag"]) or True)
    daemon._probe_one_line(m)
    assert restarted == ["W2"]
    assert daemon._warp_line_state("W2")["last_verdict"] == "quota"
    assert daemon._warp_line_state("W2")["last_ok_ts"] == 0.0


def _stub_restart(monkeypatch, new_ip):
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_line_busy", lambda tag, win: False)
    monkeypatch.setattr(daemon, "_acquire_refresh_slot", lambda tag: True)
    monkeypatch.setattr(daemon, "_release_refresh_slot", lambda tag: None)
    monkeypatch.setattr(daemon, "_RESTART_SEM", None)
    monkeypatch.setattr(daemon, "_restart_service", lambda svc, tag=None: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda port, t: True)
    monkeypatch.setattr(daemon, "_probe_refresh_egress", lambda port: new_ip)


def test_restart_same_ip_keeps_quota_park(monkeypatch):
    _stub_restart(monkeypatch, "1.1.1.1")
    park = time.time() + 16 * 3600
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", last_verdict="quota", next_probe_ts=park)
    assert daemon._warp_line_restart(_member(), "模型层 quota") is True
    st = daemon._warp_line_state("W2")
    assert st["next_probe_ts"] == pytest.approx(park)
    assert st["last_verdict"] == "quota"


def test_restart_new_ip_clears_park_for_untested_ip(monkeypatch):
    _stub_restart(monkeypatch, "9.9.9.9")
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", last_verdict="quota",
            next_probe_ts=time.time() + 16 * 3600)
    assert daemon._warp_line_restart(_member(), "模型层 quota") is True
    st = daemon._warp_line_state("W2")
    assert st["ip"] == "9.9.9.9"
    assert st["last_verdict"] == "unknown"
    assert st["next_probe_ts"] - time.time() < 10


def test_restart_clears_warming_when_busy_check_raises(monkeypatch):
    """判忙异常不得把 warming 留在 True：那会让这条线被永久跳过。"""
    m = _member()
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_line_busy",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("clash down")))
def _stub_probe(monkeypatch, verdict="quota"):
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: "1.1.1.1")
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: False)
    monkeypatch.setattr(daemon, "_warp_probe_blind", lambda now=None: False)
    monkeypatch.setattr(daemon, "_probe_rate_allow", lambda: True)
    monkeypatch.setattr(daemon, "_warp_model_usable", lambda member, ip: False)
    monkeypatch.setitem(daemon._PROBE_LAST_VERDICT, "W2", verdict)


def test_long_quota_hold_blocks_quota_restart(monkeypatch):
    """长 Retry-After 的 quota 是账号级时间窗，换 IP 改不了结论。

    实测（2026-10-28）：1711 条 quota 结论反推的解禁时刻全部落在 08:00:03
    ±3 分钟内；换址后首探 1495/1519（98.4%）仍判 quota。此时重启隧道只是
    换一个同样耗尽的地址，换来 190 次/分的重启风暴。
    """
    _stub_probe(monkeypatch)
    monkeypatch.setattr(daemon, "_QUOTA_HOLD_UNTIL", time.time() + 3600)
    restarted = []
    monkeypatch.setattr(daemon, "_warp_line_restart",
                        lambda m, r: restarted.append(m["tag"]) or True)
    daemon._probe_one_line(_member())
    assert restarted == [], \
        "额度闸开着仍重启 quota 线 —— 换 IP 不能让账号级配额提前恢复"


def test_quota_hold_also_blocks_penalty_restart(monkeypatch):
    """quota 把新地址写进台账后，别的线会走「惩罚期」路径继续重启同一轮风暴。

    实测两条路径在 190 次/分里各占一半（模型层 quota 2383 / 出口处于惩罚期
    1316 次重启后首探仍 quota），故闸必须同时覆盖两者。
    """
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: "1.1.1.1")
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: True)
    monkeypatch.setattr(daemon, "_QUOTA_HOLD_UNTIL", time.time() + 3600)
    restarted = []
    monkeypatch.setattr(daemon, "_warp_line_restart",
                        lambda m, r: restarted.append(m["tag"]) or True)
    daemon._probe_one_line(_member())
    assert restarted == [], \
        "额度闸只拦了 quota 路径，惩罚期路径仍在重启 —— 同一轮风暴从另一半漏过去"


def test_model_probe_opens_hold_only_for_long_quota(monkeypatch):
    """只有长 Retry-After 的 quota 开闸；短冷却与 403 不开。"""
    monkeypatch.setattr(daemon, "_probe_cache_get", lambda tag, ip: "")
    monkeypatch.setattr(daemon, "_probe_cache_put", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "_warp_probe_blind", lambda now=None: False)
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: False)
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", lambda *a, **k: None)
    monkeypatch.setitem(daemon.CFG["warp"], "quota_hold_min_sec", 1800)
    import probe_warp

    for verdict, retry, expect in [("quota", 7200, True), ("quota", 20, False),
                                   ("flagged", 7200, False)]:
        monkeypatch.setattr(daemon, "_QUOTA_HOLD_UNTIL", 0.0)
        monkeypatch.setattr(probe_warp, "probe",
                            lambda *a, _v=verdict, _r=retry, **k: (_v, 0, "", _r))
        daemon._warp_model_usable(_member(), "1.1.1.1")
        held = daemon._QUOTA_HOLD_UNTIL > time.time()
        assert held is expect, f"{verdict}/{retry}s 开闸结果 {held}，应为 {expect}"


def test_restart_clears_warming_when_busy_check_raises(monkeypatch):
    """判忙异常不得把 warming 留在 True：那会让这条线被永久跳过。"""
    m = _member()
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_line_busy",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("clash down")))
    assert daemon._warp_line_restart(m, "test") is False
    assert daemon._warp_line_state("W2")["warming"] is False


def test_probe_ignores_stale_ok_verdict_when_usable_reports_failure(monkeypatch):
    """失败结论不得复用上一次残留的 OK：那会把坏线记成假成功。"""
    now = time.time()
    m = _member()
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", ip_ts=now, port_ok=True)
    daemon._PROBE_LAST_VERDICT["W2"] = "OK"
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: "1.1.1.1")
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: False)
    monkeypatch.setattr(daemon, "_warp_probe_blind", lambda now=None: False)
    monkeypatch.setattr(daemon, "_probe_rate_allow", lambda: True)
    monkeypatch.setattr(daemon, "_warp_model_usable", lambda member, ip: False)
    monkeypatch.setattr(daemon, "_warp_line_restart", lambda mm, r: False)
    daemon._probe_one_line(m)
    st = daemon._warp_line_state("W2")
    assert st["last_verdict"] != "OK", st
    assert not st["last_ok_ts"], "失败后不得保留 OK 时间戳"


def test_late_unknown_does_not_clear_newer_ok():
    """早启动、晚返回的 unknown 不得清掉期间刚记下的 OK。"""
    daemon._warp_line_note_ok("W2", "1.1.1.1", 200.0)
    kept = daemon._warp_line_record_observation(
        "W2", None, "unknown", ts=210.0, started_at=100.0)
    assert kept is False
    st = daemon._warp_line_state("W2")
    assert st["last_ok_ts"] == 200.0
    assert st["last_ok_ip"] == "1.1.1.1"


def test_blocked_ip_probe_restarts_without_burning_model_quota(monkeypatch):
    """已判 blocked 的出口只重启换地址，不再打模型接口（不烧配额）。"""
    m = _member()
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", next_probe_ts=time.time() + 5)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: "1.1.1.1")
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: True)
    calls = []
    monkeypatch.setattr(daemon, "_warp_model_usable",
                        lambda *a: calls.append(a) or True)
    monkeypatch.setattr(daemon, "_warp_line_restart", lambda mm, r: True)
    daemon._probe_one_line(m)
    assert calls == [], "blocked 判定后不得再烧模型配额"


def test_dirty_ip_shared_by_two_lines_is_not_probed(monkeypatch):
    """同一脏地址被两条线先后拿到，第二条也不再探：判据是台账，不是线。"""
    import sys
    import types

    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked",
                        lambda ip, now=None: True)
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setitem(sys.modules, "probe_warp", types.SimpleNamespace(
        probe=lambda *a, **k: pytest.fail("同一脏 IP 不应二次探测")))
    for tag in ("W2", "W3"):
        assert daemon._warp_model_usable(_member(tag), "1.1.1.1") is False
        assert daemon._PROBE_LAST_VERDICT[tag] == "blocked"


def test_dirty_ip_is_never_probed(monkeypatch):
    """台账已判脏的地址不再探：脏 IP 不耗额度，与它落在哪条线上无关。"""
    import sys
    import types

    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked",
                        lambda ip, now=None: True)
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setitem(sys.modules, "probe_warp", types.SimpleNamespace(
        probe=lambda *a, **k: pytest.fail("脏 IP 不应再探测")))
    assert daemon._warp_model_usable(_member("W2"), "1.1.1.1") is False
    assert daemon._PROBE_LAST_VERDICT["W2"] == "blocked"


def test_restart_to_another_banned_ip_parks_until_unban(monkeypatch):
    """重启换到的地址若也在惩罚期，照样停探到解禁——判据是台账，不是「同址」。"""
    _stub_restart(monkeypatch, "2.2.2.2")
    now = time.time()
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", next_probe_ts=now + 5, last_verdict="err")
    monkeypatch.setattr(daemon, "_invalidate_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(daemon, "_line_block_remaining",
                        lambda ip, ts: 3600.0)
    assert daemon._warp_line_restart(_member(), "test") is True
    st = daemon._warp_line_state("W2")
    assert st["ip"] == "2.2.2.2"
    assert st["next_probe_ts"] >= now + 3500


def test_stale_verdict_after_line_restart_is_dropped():
    """探测起手后该线已重启换址：旧地址结论不得写回。

    WHY：写回会把 st["ip"] 退回旧地址，并按旧地址的 quota/flagged 惩罚期
    把整条线停探（最长 16h），新地址一个请求都没被评估过。
    """
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="2.2.2.2", ip_ts=200.0, last_ok_ts=0.0, last_verdict="unknown")
    kept = daemon._warp_line_record_observation(
        "W2", "1.1.1.1", "quota", ts=210.0, started_at=100.0)
    assert kept is False
    st = daemon._warp_line_state("W2")
    assert st["ip"] == "2.2.2.2", st
    assert st["last_verdict"] == "unknown", st
