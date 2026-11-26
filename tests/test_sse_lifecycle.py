"""Batch B §4.4 SSE 生命周期 + SIGTERM 受控排空回归。

inflight 计数靠 try/finally 释放；SSE 已启动后禁止重放；SIGTERM 排空默认关闭。
"""
import time

import pytest

import daemon
from asip.failure_domain import FailureDomain, ProbeClass


# ---------------------------------------------------------------- §4.4 ---
def test_sse_started_disables_request_replay():
    ctx = daemon.SSEStreamContext(req_id="r1")
    assert daemon.should_replay_request(ctx, ProbeClass.RATE_LIMITED) is True
    ctx.mark_first_event()
    assert ctx.sse_started is True
    assert daemon.should_replay_request(ctx, ProbeClass.RATE_LIMITED) is False


def test_sse_first_event_timestamp_recorded():
    t0 = time.monotonic()
    ctx = daemon.SSEStreamContext(req_id="r2", connection_id="conn-2")
    ts = ctx.mark_first_event()
    assert ts is not None and ts >= t0
    assert ctx.first_event_at == ts
    assert daemon.should_replay_request(ctx, ProbeClass.RATE_LIMITED) is False


def test_sse_disconnect_not_equivalent_to_ip_failure():
    ctx = daemon.SSEStreamContext(req_id="r3", connection_id="conn-3")
    ctx.mark_first_event()
    domain = daemon.sse_disconnect_failure_domain(ctx)
    assert domain == FailureDomain.REQUEST
    assert domain != FailureDomain.EXIT_IP
    assert daemon.should_mark_exit_ip_on_sse_disconnect(ctx) is False


def test_sse_active_connection_excluded_from_cleanup():
    ctx = daemon.SSEStreamContext(req_id="r4", connection_id="conn-4")
    ctx.mark_first_event()
    view = daemon.connection_from_sse_view(ctx)
    assert (daemon.classify_connection_lifecycle(view)
            is daemon.ConnectionLifecycle.SSE_ACTIVE)
    assert daemon.is_connection_safe_to_cleanup(view) is False


def test_stream_completion_releases_inflight_count():
    tracker = daemon.SSEInflightTracker()
    ctx = daemon.SSEStreamContext(req_id="r5", connection_id="conn-5")
    with tracker.track(ctx):
        assert tracker.inflight("conn-5") == 1
    assert tracker.inflight("conn-5") == 0
    assert ctx.ended_at is not None
    assert ctx.end_reason != "exception"


def test_client_cancel_releases_inflight_count():
    tracker = daemon.SSEInflightTracker()
    ctx = daemon.SSEStreamContext(req_id="r6", connection_id="conn-6")
    with tracker.track(ctx):
        assert tracker.inflight("conn-6") == 1
        ctx.end_reason = "cancelled"
    assert tracker.inflight("conn-6") == 0
    assert ctx.end_reason == "cancelled"


def test_exception_releases_inflight_count():
    tracker = daemon.SSEInflightTracker()
    ctx = daemon.SSEStreamContext(req_id="r7", connection_id="conn-7")
    with pytest.raises(ValueError):
        with tracker.track(ctx):
            assert tracker.inflight("conn-7") == 1
            raise ValueError("stream boom")
    assert tracker.inflight("conn-7") == 0
    assert ctx.end_reason == "exception"


def test_replay_policy_excludes_quota_and_context():
    ctx = daemon.SSEStreamContext(req_id="r8")
    for classification in (ProbeClass.ACCOUNT_QUOTA, ProbeClass.MODEL_QUOTA,
                           ProbeClass.CONTEXT_REJECTED):
        assert daemon.should_replay_request(ctx, classification) is False
    ctx.retry_count = 1
    assert daemon.should_replay_request(ctx, ProbeClass.RATE_LIMITED) is False


# ------------------------------------- SIGTERM 受控排空（Owner 追加） ---
def test_drain_stops_accepting_new_requests():
    ctrl = daemon.GracefulDrainController()
    assert ctrl.enter_request() is True
    assert ctrl.accepting_new() is True
    ctrl.begin()
    assert ctrl.draining is True
    assert ctrl.accepting_new() is False
    assert ctrl.enter_request() is False
    assert ctrl.inflight() == 1
    with pytest.raises(RuntimeError):
        with ctrl.request():
            pass
    ctrl.leave_request()
    assert ctrl.inflight() == 0
    ctrl.reset()  # 排空结束、服务恢复后才允许新请求
    assert ctrl.accepting_new() is True
    with ctrl.request():
        assert ctrl.inflight() == 1
    assert ctrl.inflight() == 0


def test_sigterm_drain_disabled_by_default(monkeypatch):
    monkeypatch.setitem(daemon.CFG["warp"], "graceful_drain_enabled", False)
    assert daemon.batchb_sigterm_handler() is False


def test_sigterm_waits_for_inflight(monkeypatch):
    ctrl = daemon.GracefulDrainController()
    ctrl.enter_request()
    monkeypatch.setattr(daemon, "_DRAIN", ctrl)
    monkeypatch.setitem(daemon.CFG["warp"], "graceful_drain_enabled", True)
    monkeypatch.setitem(daemon.CFG["warp"], "graceful_drain_timeout_sec", 30.0)
    # 时钟恒定 0 → 永远不到 deadline；有 inflight 时必须继续等待，不退出。
    result = daemon.batchb_sigterm_handler(
        clock=lambda: 0.0, sleep_func=lambda _s: None, max_polls=3)
    assert result is False
    assert ctrl.draining is True
    assert ctrl.inflight() == 1


def test_sigterm_exits_after_timeout(monkeypatch):
    ctrl = daemon.GracefulDrainController()
    ctrl.enter_request()
    monkeypatch.setattr(daemon, "_DRAIN", ctrl)
    monkeypatch.setitem(daemon.CFG["warp"], "graceful_drain_enabled", True)
    monkeypatch.setitem(daemon.CFG["warp"], "graceful_drain_timeout_sec", 30.0)
    ticks = iter([0.0, 31.0])

    def _clock():
        return next(ticks, 31.0)

    with pytest.raises(SystemExit):
        daemon.batchb_sigterm_handler(clock=_clock,
                                      sleep_func=lambda _s: None)


def test_sigterm_wait_releases_when_inflight_drains():
    ctrl = daemon.GracefulDrainController()
    ctrl.enter_request()
    ticks = iter([0.0, 0.1, 0.2, 0.3])

    def _clock():
        return next(ticks, 0.3)

    def _sleep(_s):
        ctrl.leave_request()  # 模拟在途 SSE 自然结束

    assert ctrl.wait(30.0, clock=_clock, sleep_func=_sleep) is True
    assert ctrl.inflight() == 0
