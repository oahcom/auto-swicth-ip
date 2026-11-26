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
