"""asip/ 复用工具的确定性单测（无网络）。"""
import os

import pytest

from asip import clash_client
from asip.atomic_io import read_json, write_json
from asip.clash_client import ClashClient
from asip.redact import redact_secrets, redact_url


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_clash_select_treats_200_and_204_as_success(monkeypatch):
    for status in (200, 204):
        monkeypatch.setattr(clash_client.requests, "request",
                            lambda *a, _s=status, **k: _Resp(_s))
        assert ClashClient("http://x", lambda: "s").select("sel", "n") is True


def test_clash_select_rejects_http_error_and_transport_error(monkeypatch):
    monkeypatch.setattr(clash_client.requests, "request",
                        lambda *a, **k: _Resp(500))
    assert ClashClient("http://x", lambda: "s").select("sel", "n") is False

    def boom(*a, **k):
        raise clash_client.requests.RequestException("down")

    monkeypatch.setattr(clash_client.requests, "request", boom)
    assert ClashClient("http://x", lambda: "s").select("sel", "n") is False


def test_clash_client_sends_rotated_secret(monkeypatch):
    seen = []

    def fake(method, url, json=None, headers=None, timeout=None):
        seen.append(headers)
        return _Resp(200, {"now": "n", "all": ["n"]})

    monkeypatch.setattr(clash_client.requests, "request", fake)
    secret = "old"
    c = ClashClient("http://x", lambda: secret)
    assert c.current("sel") == "n"
    secret = "new"
    assert c.current("sel") == "n"
    assert seen[0]["Authorization"] == "Bearer old"
    assert seen[1]["Authorization"] == "Bearer new"


def test_redact_hides_query_path_bearer_and_userinfo():
    text = ("GET https://h/pq/0123456789abcdef?token=deadbeef failed; "
            "Authorization: Bearer abc.def; https://u:p@host/x")
    out = redact_secrets(text)
    assert "deadbeef" not in out and "0123456789abcdef" not in out
    assert "abc.def" not in out and "u:p@" not in out
    assert redact_url("https://h/pq/0123456789abcdef?token=x") == "https://h/***"


def test_write_json_is_private_and_readable(tmp_path):
    path = tmp_path / "state.json"
    write_json(str(path), {"a": 1})
    assert read_json(str(path)) == {"a": 1}
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("state.json.*.tmp"))
