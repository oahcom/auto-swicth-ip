"""Batch B §4.3 连接清理安全 + TransportEvidence 分层观测回归。

所有断言都针对影子/观测代码；不触碰真实切换、不发真实 DELETE。
"""
import json

import pytest

import daemon
from asip.failure_domain import FailureDomain


class _FakeHTTP:
    """只记录 DELETE 调用的假 HTTP 端；不产生任何网络流量。"""

    def __init__(self):
        self.requests = []

    def delete(self, path):
        self.requests.append(("DELETE", path))
        return 200


def _conn(**kw):
    base = {
        "id": "conn-1",
        "sse_active": False,
        "inflight_requests": 0,
        "state": "stale",
        "last_activity": 0.0,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- §4.3 ---
def test_cleanup_disabled_never_calls_delete(monkeypatch):
    http = _FakeHTTP()
    monkeypatch.setitem(daemon.CFG["warp"], "cleanup_enabled", False)
    assert daemon.cleanup_one_connection("conn-123", http=http) is False
    assert daemon._delete_single_connection("conn-123", http=http) is False
    assert http.requests == []


def test_active_sse_connection_never_cleaned():
    conn = _conn(sse_active=True)
    assert (daemon.classify_connection_lifecycle(conn, 10_000)
            is daemon.ConnectionLifecycle.SSE_ACTIVE)
    assert daemon.is_connection_safe_to_cleanup(conn, 10_000) is False


def test_inflight_request_connection_never_cleaned():
    conn = _conn(inflight_requests=1)
    assert (daemon.classify_connection_lifecycle(conn, 10_000)
            is daemon.ConnectionLifecycle.INFLIGHT)
    assert daemon.is_connection_safe_to_cleanup(conn, 10_000) is False


def test_recently_active_connection_never_cleaned():
    conn = _conn(last_activity=9_970)  # age=30s，落在 60s 近期窗口内
    assert (daemon.classify_connection_lifecycle(conn, 10_000)
            is daemon.ConnectionLifecycle.RECENTLY_ACTIVE)
    assert daemon.is_connection_safe_to_cleanup(conn, 10_000) is False


def test_unknown_connection_state_never_cleaned():
    wrong_state = _conn(state="established", last_activity=9_000)
    assert (daemon.classify_connection_lifecycle(wrong_state, 10_000)
            is daemon.ConnectionLifecycle.UNKNOWN)
    assert daemon.is_connection_safe_to_cleanup(wrong_state, 10_000) is False
    assert daemon.is_connection_safe_to_cleanup({"id": "x"}, 10_000) is False
    assert daemon.is_connection_safe_to_cleanup(None, 10_000) is False


def test_stale_idle_connection_requires_minimum_age():
    fresh = _conn(last_activity=9_900)  # age=100s < 300s
    old = _conn(last_activity=9_600)    # age=400s >= 300s
    assert (daemon.classify_connection_lifecycle(fresh, 10_000)
            is daemon.ConnectionLifecycle.RECENTLY_ACTIVE)
    assert daemon.is_connection_safe_to_cleanup(fresh, 10_000) is False
    assert (daemon.classify_connection_lifecycle(old, 10_000)
            is daemon.ConnectionLifecycle.STALE_IDLE)
    assert daemon.is_connection_safe_to_cleanup(old, 10_000) is True


def test_connection_cleanup_uses_single_connection_id(monkeypatch):
    http = _FakeHTTP()
    monkeypatch.setitem(daemon.CFG["warp"], "cleanup_enabled", True)
    monkeypatch.setattr(daemon, "_SINGLE_CONNECTION_DELETE_VERIFIED", True)
    assert daemon._delete_single_connection("conn-123", http=http) is True
    assert http.requests == [("DELETE", "/connections/conn-123")]


def test_no_fallback_to_delete_all(monkeypatch):
    http = _FakeHTTP()
    monkeypatch.setitem(daemon.CFG["warp"], "cleanup_enabled", True)
    # 单连接删除 API 未验证（OUT-OF-SCOPE）→ 即便开关打开也零 DELETE。
    assert daemon.cleanup_one_connection("conn-123", http=http) is False
    assert daemon._delete_single_connection("conn-123", http=http) is False
    assert not any(method == "DELETE" and path.rstrip("/").endswith("/connections")
                   for method, path in http.requests)
    assert http.requests == []


# ------------------------------------------- TransportEvidence / 子因 ---
def test_transport_evidence_classifies_each_stage():
    assert (daemon.classify_transport_failure(egress_ok=False)
            == "EGRESS_PROBE_FAILED")
    assert (daemon.classify_transport_failure(egress_ok=True, proxy_tcp_ok=False)
            == "CONNECT_FAILED")
    assert (daemon.classify_transport_failure(egress_ok=True, tls_ok=False)
            == "TLS_FAILED")
    assert (daemon.classify_transport_failure(egress_ok=True, http_status=429)
            == "HTTP_429")
    assert (daemon.classify_transport_failure(egress_ok=True, http_status=503)
            == "HTTP_503")
    assert (daemon.classify_transport_failure(egress_ok=True, first_byte_ms=None)
            == "FIRST_BYTE_TIMEOUT")
    assert (daemon.classify_transport_failure(
        egress_ok=True, first_byte_ms=10, first_sse_event_ms=None)
        == "FIRST_SSE_EVENT_TIMEOUT")
    assert (daemon.classify_transport_failure(
        egress_ok=True, first_byte_ms=10, first_sse_event_ms=5,
        sse_interrupted=True) == "SSE_INTERRUPTED")
    assert (daemon.classify_transport_failure(
        egress_ok=True, http_status=200, first_byte_ms=10,
        first_sse_event_ms=5) == "UNKNOWN")


def test_http_response_is_not_model_service_available():
    bad = daemon.TransportEvidence(egress_ok=True, http_status=429,
                                   error_class="HTTP_429")
    assert daemon.is_model_service_available(bad) is False
    ok = daemon.TransportEvidence(egress_ok=True, http_status=200,
                                  first_sse_event_ms=120, error_class=None)
    assert daemon.is_model_service_available(ok) is True


def test_header_timeout_classified_as_transport_not_exit_ip():
    ec = daemon.classify_transport_failure(egress_ok=True,
                                           sidecar_reason="header-timeout")
    assert ec == "HEADER_TIMEOUT"
    domain = daemon.transport_error_domain(ec)
    assert domain == FailureDomain.TRANSPORT
    assert domain != FailureDomain.EXIT_IP
    assert daemon.should_dirty_exit_on_transport_error(ec) is False


def test_sidecar_subreasons_are_distinguished():
    got = {
        reason: daemon.classify_transport_failure(egress_ok=True,
                                                  sidecar_reason=reason)
        for reason in ("header-timeout", "caller-aborted", "upstream-timeout")
    }
    assert got["header-timeout"] == "HEADER_TIMEOUT"
    assert got["caller-aborted"] == "CALLER_ABORTED"
    assert got["upstream-timeout"] == "UPSTREAM_TIMEOUT"
    assert len(set(got.values())) == 3
    for ec in got.values():
        assert daemon.transport_error_domain(ec) == FailureDomain.TRANSPORT
        assert daemon.should_dirty_exit_on_transport_error(ec) is False


def test_observe_transport_failure_writes_jsonl(monkeypatch, tmp_path):
    path = tmp_path / "transport_evidence.jsonl"
    monkeypatch.setenv("ASIP_TRANSPORT_EVIDENCE_FILE_PATH", str(path))
    ev = daemon.observe_transport_failure(
        "WARP-X", 7890, 2,
        egress_probe=lambda port, timeout_sec: "2a09::1",
        lane_probe=lambda port: {
            "http_status": 503,
            "first_byte_ms": 12,
            "first_sse_event_ms": None,
            "sidecar_reason": "header-timeout",
        },
        req_id="req-1", slot="slot-6",
    )
    assert ev.error_class == "HEADER_TIMEOUT"
    assert path.exists()
    rec = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["error_class"] == "HEADER_TIMEOUT"
    assert rec["sidecar_reason"] == "header-timeout"
    assert rec["http_status"] == 503
    assert rec["req_id"] == "req-1"
