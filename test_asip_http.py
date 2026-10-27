"""asip/http.py 的确定性单测（无网络，monkeypatch requests.request）。"""

import json

import pytest

from asip import http
from asip.http import HttpResult, request_json

_MISSING = object()


class _Resp:
    def __init__(self, status, payload=_MISSING, text=""):
        self.status_code = status
        self._payload = payload
        if payload is not _MISSING and not text:
            text = json.dumps(payload)
        self.text = text

    def json(self):
        if self._payload is _MISSING:
            raise ValueError("no json body")
        return self._payload


def _transport_error(message):
    raise http.requests.RequestException(message)


def test_200_json_returns_ok_and_data(monkeypatch):
    monkeypatch.setattr(
        http.requests, "request",
        lambda *a, **k: _Resp(200, {"ip": "203.0.113.5"}),
    )
    result = request_json("GET", "http://x", timeout=1.0)
    assert result == HttpResult(status=200, data={"ip": "203.0.113.5"})
    assert result.ok is True
    assert result.text is None


def test_non_json_body_is_kept_as_text(monkeypatch):
    monkeypatch.setattr(
        http.requests, "request",
        lambda *a, **k: _Resp(200, text="203.0.113.5\n"),
    )
    result = request_json("GET", "http://x", timeout=1.0)
    assert result.status == 200
    assert result.ok is True
    assert result.data is None
    assert result.text == "203.0.113.5\n"
    assert result.error is None


def test_500_is_not_ok_but_body_is_preserved(monkeypatch):
    monkeypatch.setattr(
        http.requests, "request",
        lambda *a, **k: _Resp(500, {"detail": "boom"}),
    )
    result = request_json("GET", "http://x", timeout=1.0)
    assert result.ok is False
    assert result.status == 500
    assert result.data == {"detail": "boom"}
    assert result.error is None


def test_500_non_json_body_kept_in_text(monkeypatch):
    monkeypatch.setattr(
        http.requests, "request",
        lambda *a, **k: _Resp(502, text="bad gateway"),
    )
    result = request_json("GET", "http://x", timeout=1.0)
    assert result.ok is False
    assert result.data is None
    assert result.text == "bad gateway"


def test_transport_error_has_status_none_and_redacted_error(monkeypatch):
    def boom(*a, **k):
        _transport_error(
            "GET https://h/pq/0123456789abcdef?token=deadbeef failed"
        )

    monkeypatch.setattr(http.requests, "request", boom)
    result = request_json("GET", "http://x", timeout=1.0)

    assert result.status is None
    assert result.ok is False
    assert result.data is None and result.text is None
    assert "deadbeef" not in result.error
    assert "0123456789abcdef" not in result.error
    assert "***" in result.error


def test_retries_one_retries_once_then_succeeds(monkeypatch):
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            _transport_error("connection reset")
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(http.requests, "request", flaky)
    result = request_json("GET", "http://x", timeout=1.0, retries=1, backoff=0)

    assert len(calls) == 2
    assert result.ok is True
    assert result.data == {"ok": True}


def test_retries_zero_makes_a_single_attempt(monkeypatch):
    calls = []

    def down(*a, **k):
        calls.append(1)
        _transport_error("connection refused")

    monkeypatch.setattr(http.requests, "request", down)
    result = request_json("GET", "http://x", timeout=1.0)

    assert len(calls) == 1
    assert result.status is None
    assert "connection refused" in result.error


@pytest.mark.parametrize("timeout", [0, -1, 0.0, None])
def test_timeout_must_be_positive(monkeypatch, timeout):
    def unexpected(*a, **k):
        pytest.fail("request must not run for an invalid timeout")

    monkeypatch.setattr(http.requests, "request", unexpected)
    with pytest.raises(ValueError):
        request_json("GET", "http://x", timeout=timeout)


def test_session_is_used_and_kwargs_are_forwarded(monkeypatch):
    calls = []

    class _FakeSession:
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return _Resp(200, {"via": "session"})

    def unexpected(*a, **k):
        pytest.fail("module-level requests.request must not be used")

    monkeypatch.setattr(http.requests, "request", unexpected)
    result = request_json(
        "GET", "http://x", timeout=2.0, params={"a": 1},
        headers={"X-Test": "1"}, session=_FakeSession(),
    )

    assert result.data == {"via": "session"}
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert (method, url) == ("GET", "http://x")
    assert kwargs["timeout"] == 2.0
    assert kwargs["params"] == {"a": 1}
    assert kwargs["headers"] == {"X-Test": "1"}


def test_redact_false_keeps_token_verbatim(monkeypatch):
    def boom(*a, **k):
        _transport_error("GET https://h/x?token=deadbeef failed")

    monkeypatch.setattr(http.requests, "request", boom)
    result = request_json("GET", "http://x", timeout=1.0, redact=False)

    assert result.status is None
    assert "token=deadbeef" in result.error


def test_long_non_json_body_is_truncated(monkeypatch):
    monkeypatch.setattr(
        http.requests, "request",
        lambda *a, **k: _Resp(200, text="x" * 5000),
    )
    result = request_json("GET", "http://x", timeout=1.0)

    assert result.text == "x" * 2000


def test_204_and_empty_body_are_not_parsed(monkeypatch):
    for response in (_Resp(204, text=""), _Resp(200, text="  \n")):
        monkeypatch.setattr(http.requests, "request", lambda *a, _r=response, **k: _r)
        result = request_json("GET", "http://x", timeout=1.0)
        assert result.data is None
        assert result.text is None


def test_text_limit_can_be_disabled_for_large_bodies(monkeypatch):
    """订阅是几十 KB 的 base64：截断会静默丢节点，必须能取全文。"""
    body = "x" * 9000
    monkeypatch.setattr(http.requests, "request",
                        lambda *a, **k: _Resp(200, text=body))

    default = request_json("GET", "http://x", timeout=1.0)
    full = request_json("GET", "http://x", timeout=1.0, text_limit=None)

    assert len(default.text) == 2000
    assert full.text == body


def test_any_2xx_is_ok_like_raise_for_status():
    """旧白名单只认 200/204；201/206 会被误判为失败（订阅源分片返回 206）。"""
    assert HttpResult(status=206).ok is True
    assert HttpResult(status=299).ok is True
    assert HttpResult(status=300).ok is False
    assert HttpResult(status=None).ok is False


def test_negative_text_limit_is_rejected():
    """负数切片会静默截错，不能当合法 limit。"""
    with pytest.raises(ValueError, match="text_limit"):
        request_json("GET", "http://x", timeout=1.0, text_limit=-1)
