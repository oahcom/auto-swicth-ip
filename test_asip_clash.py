"""ClashClient 契约单测（无网络，monkeypatch requests.request）。"""

from urllib.parse import unquote

from asip import clash_client
from asip.clash_client import ClashClient, DEFAULT_TEST_URL


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _client():
    return ClashClient("http://x", lambda: "s")


def test_request_json_returns_status_and_payload(monkeypatch):
    monkeypatch.setattr(clash_client.requests, "request",
                        lambda *a, **k: _Resp(200, {"ok": True}))
    assert _client().request_json("GET", "proxies/sel") == (200, {"ok": True})


def test_request_json_transport_failure(monkeypatch):
    def boom(*a, **k):
        raise clash_client.requests.RequestException("down")

    monkeypatch.setattr(clash_client.requests, "request", boom)
    assert _client().request_json("GET", "proxies/sel") == (None, None)


def test_request_json_non_json_body(monkeypatch):
    monkeypatch.setattr(clash_client.requests, "request",
                        lambda *a, **k: _Resp(200, None))
    assert _client().request_json("GET", "proxies/sel") == (200, None)


def test_select_200_204_500_and_transport(monkeypatch):
    for status in (200, 204):
        monkeypatch.setattr(clash_client.requests, "request",
                            lambda *a, _s=status, **k: _Resp(_s))
        assert _client().select("sel", "n") is True

    monkeypatch.setattr(clash_client.requests, "request",
                        lambda *a, **k: _Resp(500))
    assert _client().select("sel", "n") is False

    def boom(*a, **k):
        raise clash_client.requests.RequestException("down")

    monkeypatch.setattr(clash_client.requests, "request", boom)
    assert _client().select("sel", "n") is False


def test_names_with_slash_and_space_are_quoted(monkeypatch):
    seen = []

    def fake(method, url, json=None, headers=None, timeout=None, **k):
        seen.append(url)
        return _Resp(200, {"now": "n", "all": ["n"]})

    monkeypatch.setattr(clash_client.requests, "request", fake)
    assert _client().current("a/b c") == "n"
    assert seen[-1] == "http://x/proxies/a%2Fb%20c"


def test_selector_state_single_request_returns_all_and_now(monkeypatch):
    calls = []

    def fake(*a, **k):
        calls.append(a)
        return _Resp(200, {"all": ["n1", "n2"], "now": "n2"})

    monkeypatch.setattr(clash_client.requests, "request", fake)
    names, now = _client().selector_state("sel")
    assert names == ["n1", "n2"]
    assert now == "n2"
    assert len(calls) == 1


def test_selector_state_non_200_returns_none_pair(monkeypatch):
    monkeypatch.setattr(clash_client.requests, "request",
                        lambda *a, **k: _Resp(500, {"all": [], "now": "n"}))
    assert _client().selector_state("sel") == (None, None)


def test_delay_returns_int_and_sends_params(monkeypatch):
    seen = {}

    def fake(method, url, json=None, headers=None, timeout=None, **k):
        seen.update(method=method, url=url, params=k.get("params"),
                    timeout=timeout)
        return _Resp(200, {"delay": 123})

    monkeypatch.setattr(clash_client.requests, "request", fake)
    assert _client().delay("node/one", 5000) == 123
    assert seen["method"] == "GET"
    assert seen["url"] == "http://x/proxies/node%2Fone/delay"
    assert seen["params"] == {"timeout": 5000, "url": DEFAULT_TEST_URL}
    assert seen["timeout"] == 7.0


def test_delay_without_value_returns_none(monkeypatch):
    monkeypatch.setattr(clash_client.requests, "request",
                        lambda *a, **k: _Resp(200, {"message": "ok"}))
    assert _client().delay("n", 5000) is None


def test_delay_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(clash_client.requests, "request",
                        lambda *a, **k: _Resp(500, {"delay": 12}))
    assert _client().delay("n", 5000) is None


def test_delays_preserves_order_and_dedupes(monkeypatch):
    calls = []

    def fake(method, url, json=None, headers=None, timeout=None, **k):
        node = unquote(url.split("/proxies/", 1)[1].rsplit("/delay", 1)[0])
        calls.append(node)
        return _Resp(200, {"delay": 10})

    monkeypatch.setattr(clash_client.requests, "request", fake)
    result = _client().delays(["b", "a", "b", "c"])
    assert list(result) == ["b", "a", "c"]
    assert result == {"b": 10, "a": 10, "c": 10}
    assert sorted(calls) == ["a", "b", "c"]


def test_delays_empty_returns_empty_without_pool(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("thread pool must not be created")

    monkeypatch.setattr(clash_client, "ThreadPoolExecutor", forbidden)
    assert _client().delays([]) == {}


def test_connections_returns_dict_only_on_200(monkeypatch):
    monkeypatch.setattr(clash_client.requests, "request",
                        lambda *a, **k: _Resp(200, {"connections": []}))
    assert _client().connections() == {"connections": []}

    monkeypatch.setattr(clash_client.requests, "request",
                        lambda *a, **k: _Resp(500, {"connections": []}))
    assert _client().connections() is None
