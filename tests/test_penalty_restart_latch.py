import threading
import time

import pytest

import daemon as d


TAG = "WARP-H2-LATCH-TEST"
IP = "2001:db8::101"
NEW_IP = "2001:db8::202"


@pytest.fixture
def harness(monkeypatch):
    """隔离 daemon 状态和所有重启副作用。"""
    old_state = d._WARP_LINE_STATE
    d._WARP_LINE_STATE = {}

    member = {
        "tag": TAG,
        "port": 12345,
        "kind": "h2",
        "entry": None,
    }

    cfg = {
        "restart_cooldown_sec": 0,
        "restart_max_per_hour_per_line": 0,
        "restart_futile_limit": 999999,
        "restart_ip_stuck_fuse": 999999,
        "probe_unknown_retry_sec": 0,
        "ready_timeout_sec": 1,
        "channel_ban_hold_enabled": False,
        "drain_poll_sec": 1,
    }

    calls = {
        "restart": 0,
        "probe": 0,
        "release": 0,
        "save": 0,
        "blocked": [],
    }

    def blocked(ip, now=None):
        calls["blocked"].append(ip)
        return True

    monkeypatch.setattr(d, "_warp", lambda: cfg)
    monkeypatch.setattr(d, "active_warp_tag", lambda: "ACTIVE-OTHER")
    monkeypatch.setattr(d, "_night_block_restart", lambda *a: False)
    monkeypatch.setattr(d, "_line_enabled", lambda *a: True)
    monkeypatch.setattr(d, "_warp_line_restart_allowed", lambda *a: True)
    monkeypatch.setattr(d, "_drain_gate", lambda *a: (True, "clear"))
    monkeypatch.setattr(d, "warp_line_busy", lambda *a: False)
    monkeypatch.setattr(d, "_acquire_refresh_slot", lambda *a: True)

    def release(*args):
        calls["release"] += 1

    monkeypatch.setattr(d, "_release_refresh_slot", release)
    monkeypatch.setattr(d, "_restart_warp_line",
                        lambda *a: calls.__setitem__(
                            "restart", calls["restart"] + 1) or True)
    monkeypatch.setattr(d, "_wait_port_ready", lambda *a: True)

    def probe(*args):
        calls["probe"] += 1
        return NEW_IP

    monkeypatch.setattr(d, "_probe_refresh_egress", probe)
    monkeypatch.setattr(d, "_invalidate_warp_line_ip", lambda *a: None)
    monkeypatch.setattr(d, "_record_line_ip_drift", lambda *a: None)
    monkeypatch.setattr(d, "_restart_outcome_note", lambda *a: None)
    monkeypatch.setattr(d, "_save_warp_line_state",
                        lambda *a: calls.__setitem__(
                            "save", calls["save"] + 1))
    monkeypatch.setattr(d, "_egress_error_hint", lambda *a: "unknown")
    monkeypatch.setattr(d, "_warp_ip_is_blocked", blocked)
    monkeypatch.setattr(d, "_RESTART_SEM", None)

    with d._WARP_LINE_CV:
        st = d._warp_line_state_locked(TAG)
        st["ip"] = IP
        st["ip_ts"] = time.time()

    yield member, calls

    d._WARP_LINE_STATE = old_state


def test_first_blocked_restart_sets_latch(harness):
    member, calls = harness

    assert d._warp_line_restart(member, "penalty") is True

    st = d._warp_line_state(TAG)
    assert st["penalty_restart_latched"] is True
    assert calls["restart"] == 1


def test_same_blocked_ip_is_not_restarted_twice(harness, monkeypatch):
    member, calls = harness

    with d._WARP_LINE_CV:
        st = d._warp_line_state_locked(TAG)
        st["penalty_restart_latched"] = True

    monkeypatch.setattr(d, "_warp_ip_is_blocked", lambda *a: True)

    assert d._warp_line_restart(member, "penalty-again") is False
    assert calls["restart"] == 0


def test_unblocked_ip_clears_latch(harness, monkeypatch):
    _, calls = harness

    with d._WARP_LINE_CV:
        st = d._warp_line_state_locked(TAG)
        st["ip"] = IP
        st["penalty_restart_latched"] = True

    monkeypatch.setattr(d, "_warp_ip_is_blocked", lambda *a: False)

    assert d._clear_penalty_restart_latch_if_unblocked(TAG, IP) is True
    assert d._warp_line_state(TAG)["penalty_restart_latched"] is False


def test_failed_restart_keeps_latch(harness, monkeypatch):
    member, calls = harness

    monkeypatch.setattr(d, "_restart_warp_line", lambda *a: False)

    assert d._warp_line_restart(member, "penalty") is False
    assert d._warp_line_state(TAG)["penalty_restart_latched"] is True
    assert calls["restart"] == 0


def test_missing_ip_does_not_clear_existing_latch(harness, monkeypatch):
    member, calls = harness

    with d._WARP_LINE_CV:
        st = d._warp_line_state_locked(TAG)
        st["ip"] = None
        st["penalty_restart_latched"] = True

    assert d._warp_line_restart(member, "penalty") is False
    assert d._warp_line_state(TAG)["penalty_restart_latched"] is True
    assert calls["restart"] == 0


def test_block_check_exception_does_not_clear_existing_latch(
        harness, monkeypatch):
    member, calls = harness

    with d._WARP_LINE_CV:
        st = d._warp_line_state_locked(TAG)
        st["penalty_restart_latched"] = True

    def explode(*args):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(d, "_warp_ip_is_blocked", explode)

    assert d._warp_line_restart(member, "penalty") is False
    assert d._warp_line_state(TAG)["penalty_restart_latched"] is True
    assert calls["restart"] == 0


def test_concurrent_calls_only_claim_one_restart(harness, monkeypatch):
    member, calls = harness

    entered_restart = threading.Event()
    release_restart = threading.Event()
    calls_lock = threading.Lock()
    results = []

    def restart(*args):
        with calls_lock:
            calls["restart"] += 1
        entered_restart.set()
        assert release_restart.wait(timeout=5)
        return True

    monkeypatch.setattr(d, "_restart_warp_line", restart)

    def invoke():
        results.append(d._warp_line_restart(member, "parallel"))

    t1 = threading.Thread(target=invoke)
    t2 = threading.Thread(target=invoke)

    t1.start()
    assert entered_restart.wait(timeout=5)

    # 第一个调用已经进入真实重启阶段并持有 warming。
    # 第二个调用应在 warming 复核处返回，不要求它到达 drain gate。
    t2.start()
    t2.join(timeout=5)

    assert not t2.is_alive()

    release_restart.set()
    t1.join(timeout=5)

    assert not t1.is_alive()
    assert calls["restart"] == 1
    assert results.count(True) == 1
    assert results.count(False) == 1


@pytest.mark.parametrize("flag", ["rebound", "tunnel_dead"])
def test_rebound_and_tunnel_dead_are_not_blocked_by_latch(
        harness, monkeypatch, flag):
    member, calls = harness

    with d._WARP_LINE_CV:
        st = d._warp_line_state_locked(TAG)
        st["penalty_restart_latched"] = True

    monkeypatch.setattr(d, "_warp_ip_is_blocked", lambda *a: True)

    kwargs = {flag: True}
    assert d._warp_line_restart(member, "recovery", **kwargs) is True
    assert calls["restart"] == 1

    # 这两种恢复路径不负责清除已有惩罚闩锁。
    assert d._warp_line_state(TAG)["penalty_restart_latched"] is True


def test_probe_freshness_respects_penalty_latch(monkeypatch):
    ip = "2001:db8::301"
    member = {
        "tag": "WARP-H2-PENALTY-PROBE",
        "port": 12346,
        "kind": "h2",
        "entry": None,
    }
    now = time.time()

    old_state = d._WARP_LINE_STATE
    d._WARP_LINE_STATE = {}

    try:
        def init_state():
            with d._WARP_LINE_CV:
                st = d._warp_line_state_locked(member["tag"])
                st.update({
                    "ip": ip,
                    "ip_ts": now,
                    "last_ok_ts": now,
                    "last_ok_ip": ip,
                    "penalty_restart_latched": True,
                    "warming": False,
                    "stop_probe": False,
                })

        def run_case(blocked):
            init_state()
            calls = {
                "restart": 0,
                "model": 0,
                "observation": [],
                "blocked_note": 0,
            }

            monkeypatch.setattr(d, "active_warp_tag",
                                lambda: "ACTIVE-OTHER")
            monkeypatch.setattr(d, "_warp", lambda: {"ip_observe_sec": 120})
            monkeypatch.setattr(d, "_port_open", lambda port: True)
            monkeypatch.setattr(d, "warp_egress_ip", lambda port: ip)
            monkeypatch.setattr(d, "_warp_ip_is_blocked",
                                lambda candidate, ts=None: blocked)
            monkeypatch.setattr(d, "_warp_line_ok_fresh", lambda tag, ts: True)
            monkeypatch.setattr(
                d, "_warp_line_record_observation",
                lambda tag, candidate, verdict, ts, started:
                    calls["observation"].append(verdict) or True)
            monkeypatch.setattr(
                d, "_channel_blocked_note",
                lambda *args: calls.__setitem__(
                    "blocked_note", calls["blocked_note"] + 1))
            monkeypatch.setattr(d, "_channel_noaddr_note", lambda *a: None)
            monkeypatch.setattr(d, "_line_node_cannot_carry", lambda tag: False)
            monkeypatch.setattr(d, "_penalty_restart_fused", lambda *a: False)
            monkeypatch.setattr(
                d, "_warp_line_restart",
                lambda *a, **kw: calls.__setitem__(
                    "restart", calls["restart"] + 1) or True)
            monkeypatch.setattr(d, "_penalty_restart_fuse", lambda *a: None)
            monkeypatch.setattr(d, "_reconnect_warp_now", lambda *a: None)

            monkeypatch.setattr(d, "_H2_TRIED", {})
            monkeypatch.setattr(d, "_H2_POOL_DEAD", {})
            monkeypatch.setattr(d, "_h2_pool_key", lambda m: "test-pool")
            monkeypatch.setattr(d, "_LAST_EGRESS_TRACE_LOCK",
                                threading.Lock())
            monkeypatch.setattr(d, "_LAST_EGRESS_TRACE_BY_PORT", {})
            monkeypatch.setattr(d, "_LAST_EGRESS_TRACE", {})
            monkeypatch.setattr(d, "_warp_line_update", lambda *a, **kw: None)
            monkeypatch.setattr(d, "_save_warp_line_state", lambda: None)
            monkeypatch.setattr(d, "_record_line_colo", lambda *a: None)
            monkeypatch.setattr(d, "_line_node", lambda tag: None)
            monkeypatch.setattr(d, "_warp_probe_blind", lambda ts: False)
            monkeypatch.setattr(d, "_probe_rate_allow", lambda: True)
            monkeypatch.setattr(d, "_MODEL_PROBE_SEM", None)
            monkeypatch.setattr(
                d, "_warp_model_usable",
                lambda *a: calls.__setitem__(
                    "model", calls["model"] + 1) or True)
            monkeypatch.setattr(d, "_WARP_PROBE_PERSISTENT", set())
            monkeypatch.setattr(d, "_channel_ban_note", lambda *a: None)
            monkeypatch.setattr(d, "_DRAIN_LOCK", threading.Lock())
            monkeypatch.setattr(d, "_BAD_ON_SLOT", {})

            d._probe_one_line(member)
            return calls

        blocked_calls = run_case(True)
        assert blocked_calls["restart"] == 1
        assert blocked_calls["blocked_note"] == 1
        assert blocked_calls["model"] == 0

        unblocked_calls = run_case(False)
        assert d._warp_line_state(member["tag"])[
            "penalty_restart_latched"] is False
        assert unblocked_calls["restart"] == 0
        assert unblocked_calls["model"] == 0

    finally:
        d._WARP_LINE_STATE = old_state


def test_api_state_unban_clears_all_matching_line_latches(monkeypatch):
    import copy

    ip = "2001:db8::401"
    tags = ("WARP-H2-UNBAN-A", "WARP-H2-UNBAN-B")

    old_state = d._WARP_LINE_STATE
    old_ip_used = d._WARP_IP_USED
    d._WARP_LINE_STATE = {}
    d._WARP_IP_USED = {
        ip: {
            "blocked": True,
            "blocked_kind": "abuse403",
            "ban_until": time.time() + 3600,
            "expires_at": time.time() + 7200,
        }
    }

    class FakeStateMgmt:
        @staticmethod
        def unban_transform(entry, *, now, ttl, retention_sec):
            before = copy.deepcopy(entry)
            after = copy.deepcopy(entry)
            after.update({"blocked": False, "blocked_kind": "", "ban_until": 0})
            return {
                "was_blocked": True,
                "cleared_fields": ["blocked", "blocked_kind", "ban_until"],
                "before": before,
                "after": after,
            }

    class FakeAlerting:
        CATEGORY_LEDGER_ANOMALY = "ledger-anomaly"
        SEVERITY_WARNING = "warning"

        @staticmethod
        def emit(*args, **kwargs):
            return None

    monkeypatch.setattr(d, "_state_mgmt", FakeStateMgmt)
    monkeypatch.setattr(d, "_alerting", FakeAlerting)
    monkeypatch.setattr(d, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(d, "_save_warp_line_state", lambda: None)
    monkeypatch.setattr(d, "_warp", lambda: {
        "blocked_guard_sec": 21600,
        "rate429_guard_sec": 60,
        "blocked_ttl_sec": 86400,
        "ledger_retention_sec": 86400,
    })
    monkeypatch.setattr(d, "_warp_ip_is_blocked", lambda *a: False)

    try:
        with d._WARP_LINE_CV:
            for tag in tags:
                st = d._warp_line_state_locked(tag)
                st["ip"] = ip
                st["penalty_restart_latched"] = True

        result = d._api_state_unban({"ip": ip})

        assert result["ok"] is True
        for tag in tags:
            assert d._warp_line_state(tag)["penalty_restart_latched"] is False

        # 重置 ledger 与两条线的闩锁，验证 dry-run 不修改任何闩锁。
        d._WARP_IP_USED[ip] = {
            "blocked": True,
            "blocked_kind": "abuse403",
            "ban_until": time.time() + 3600,
            "expires_at": time.time() + 7200,
        }
        with d._WARP_LINE_CV:
            for tag in tags:
                st = d._warp_line_state_locked(tag)
                st["ip"] = ip
                st["penalty_restart_latched"] = True

        dry_result = d._api_state_unban({"ip": ip, "dry_run": True})

        assert dry_result["ok"] is True
        assert dry_result["dry_run"] is True
        for tag in tags:
            assert d._warp_line_state(tag)["penalty_restart_latched"] is True

    finally:
        d._WARP_LINE_STATE = old_state
        d._WARP_IP_USED = old_ip_used
