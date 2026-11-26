import pytest

from asip.failure_domain import (
    FailureDomain,
    ProbeClass,
    classify_response,
    classify_rotate_request,
    should_retire_exit,
    FailureEvent,
)


def test_http_200_classified_ok():
    assert classify_response(200, "data: {...}") == ProbeClass.OK


def test_free_usage_limit_not_ip_blocked():
    got = classify_response(429, '{"error":{"type":"FreeUsageLimitError"}}')
    assert got == ProbeClass.ACCOUNT_QUOTA
    assert got != ProbeClass.IP_BLOCKED


def test_http_429_without_body_is_unknown():
    assert classify_response(429, "") == ProbeClass.UNKNOWN


def test_http_429_rate_limit_is_request_scope():
    got = classify_response(429, '{"error":"rate limit exceeded"}')
    assert got == ProbeClass.RATE_LIMITED


def test_model_quota_not_ip_blocked():
    got = classify_response(429, '{"error":"model quota exhausted"}')
    assert got == ProbeClass.MODEL_QUOTA


def test_http_403_not_automatically_ip_blocked():
    got = classify_response(403, "abusive traffic")
    assert got == ProbeClass.CONTEXT_REJECTED
    assert got != ProbeClass.IP_BLOCKED


def test_http_503_not_ip_blocked():
    got = classify_response(503, "Service Unavailable")
    assert got == ProbeClass.TRANSPORT_ERROR
    assert got != ProbeClass.IP_BLOCKED


def test_incomplete_response_is_unknown():
    assert classify_response("bad", None) == ProbeClass.UNKNOWN


def test_ip_blocked_requires_independent_evidence():
    e = FailureEvent("r1", "slot-1", "::1", FailureDomain.EXIT_IP,
                     "quota", 0.0, None)
    assert not should_retire_exit(e)
    e2 = FailureEvent("r2", "slot-1", "::1", FailureDomain.EXIT_IP,
                      "quota", 0.0, "ev-1")
    assert should_retire_exit(e2)


def test_account_domain_never_retires():
    e = FailureEvent("r3", "slot-1", "::1", FailureDomain.ACCOUNT,
                     "quota", 0.0, "ev-1")
    assert not should_retire_exit(e)


def test_rotate_request_long_retry_is_account():
    req = {"reason": "rate429", "retry_after_ms": 3600 * 1000}
    assert classify_rotate_request(req) == FailureDomain.ACCOUNT


def test_rotate_request_short_retry_is_request():
    req = {"reason": "rate429", "retry_after_ms": 1500}
    assert classify_rotate_request(req) == FailureDomain.REQUEST


def test_rotate_request_transport():
    assert classify_rotate_request({"reason": "transport"}) == FailureDomain.TRANSPORT


def test_rotate_request_unknown():
    assert classify_rotate_request({}) == FailureDomain.UNKNOWN
