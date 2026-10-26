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


def test_same_ip_verdict_is_reused_across_lines_without_probe(monkeypatch):
    """同一 /128 被另一条线探过（持久结论）就不再探：配额按地址烧。"""
    import sys
    import types

    ip = "1.1.1.1"
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    with daemon._WARP_REFRESH_LOCK:
        daemon._WARP_PROBE_SEEN[("W1", ip)] = ("quota", time.time())
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked",
                        lambda ip, now=None: True)
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setitem(sys.modules, "probe_warp", types.SimpleNamespace(
        probe=lambda *a, **k: pytest.fail("同一 IP 不应二次探测")))
    assert daemon._warp_model_usable(_member("W2"), ip) is False
    assert daemon._PROBE_LAST_VERDICT["W2"] == "quota"


def test_restart_same_blocked_ip_parks_until_unban(monkeypatch):
    """重启拿回同一个仍在惩罚期的 IP：停探到确切解禁，而不是 5s 后再探。"""
    m = _member()
    now = time.time()
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", next_probe_ts=now + 5, last_verdict="err")
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_line_busy", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "_acquire_refresh_slot", lambda tag: True)
    monkeypatch.setattr(daemon, "_release_refresh_slot", lambda tag: None)
    monkeypatch.setattr(daemon, "_RESTART_SEM", None)
    monkeypatch.setattr(daemon, "_restart_service", lambda svc, tag=None: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda port, t: True)
    monkeypatch.setattr(daemon, "_probe_refresh_egress", lambda port: "1.1.1.1")
    monkeypatch.setattr(daemon, "_invalidate_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(daemon, "_line_block_remaining",
                        lambda ip, ts: 3600.0)
    assert daemon._warp_line_restart(m, "test") is True
    assert daemon._warp_line_state("W2")["next_probe_ts"] >= now + 3500
