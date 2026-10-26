"""ClashClient 契约单测（无网络，monkeypatch requests.request）。"""

from urllib.parse import unquote

from asip import clash_client
from asip.clash_client import ClashClient, DEFAULT_TEST_URL

TEST_URL = DEFAULT_TEST_URL


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


def _patch(monkeypatch, resp):
    monkeypatch.setattr(clash_client.requests, "request",
                        lambda *a, **k: resp)


# ---------------------------------------------------------------- request_json

def test_request_json_200_json(monkeypatch):
    _patch(monkeypatch, _Resp(200, {"ok": True}))
    assert _client().request_json("GET", "proxies/sel") == (200, {"ok": True})


def test_request_json_204_empty_body(monkeypatch):
    _patch(monkeypatch, _Resp(204))
    assert _client().request_json("PUT", "proxies/sel") == (204, None)


def test_request_json_500_text_body(monkeypatch):
    _patch(monkeypatch, _Resp(500))
    assert _client().request_json("GET", "proxies/sel") == (500, None)


def test_request_json_transport_failure(monkeypatch):
    def boom(*a, **k):
        raise clash_client.requests.RequestException("down")

    monkeypatch.setattr(clash_client.requests, "request", boom)
    assert _client().request_json("GET", "proxies/sel") == (None, None)


# --------------------------------------------------------------------- select

def test_select_200_and_204_succeed(monkeypatch):
    for status in (200, 204):
        _patch(monkeypatch, _Resp(status))
        assert _client().select("sel", "n") is True


def test_select_500_fails(monkeypatch):
    _patch(monkeypatch, _Resp(500))
    assert _client().select("sel", "n") is False


def test_select_transport_failure_fails(monkeypatch):
    def boom(*a, **k):
        raise clash_client.requests.RequestException("down")

    monkeypatch.setattr(clash_client.requests, "request", boom)
    assert _client().select("sel", "n") is False


# -------------------------------------------------------------------- quoting

def _recording_fake(seen, payload):
    def fake(method, url, json=None, headers=None, timeout=None, **kwargs):
        seen.append((method, url, kwargs.get("params")))
        return _Resp(200, payload)

    return fake


def test_names_with_slash_and_space_are_quoted(monkeypatch):
    seen = []
    monkeypatch.setattr(clash_client.requests, "request",
                        _recording_fake(seen, {"all": ["n"], "now": "n"}))
    client = _client()

    assert client.current("a/b c") == "n"
    assert client.proxy_names("a/b c") == ["n"]
    assert client.select("a/b c", "n") is True

    assert [url for _, url, _ in seen] == ["http://x/proxies/a%2Fb%20c"] * 3


def test_delay_node_name_is_quoted_in_url(monkeypatch):
    seen = []
    monkeypatch.setattr(clash_client.requests, "request",
                        _recording_fake(seen, {"delay": 42}))
    assert _client().delay("node/one two", 5000) == 42
    method, url, params = seen[-1]
    assert method == "GET"
    assert url == "http://x/proxies/node%2Fone%20two/delay"
    assert params == {"timeout": 5000, "url": TEST_URL}


# ---------------------------------------------------------------------- delay

def test_delay_returns_int_and_sends_params(monkeypatch):
    seen = {}

    def fake(method, url, json=None, headers=None, timeout=None, **kwargs):
        seen.update(method=method, url=url, params=kwargs.get("params"),
                    timeout=timeout)
        return _Resp(200, {"delay": 123})

    monkeypatch.setattr(clash_client.requests, "request", fake)
    assert _client().delay("node/one", 5000) == 123
    assert seen["method"] == "GET"
    assert seen["url"] == "http://x/proxies/node%2Fone/delay"
    assert seen["params"] == {"timeout": 5000, "url": TEST_URL}
    assert seen["timeout"] == 7.0


def test_delay_accepts_float_and_normalises_to_int(monkeypatch):
    _patch(monkeypatch, _Resp(200, {"delay": 12.9}))
    assert _client().delay("n", 5000) == 12


def test_delay_rejects_bool_and_non_finite(monkeypatch):
    for value in (True, False, float("nan"), float("inf")):
        _patch(monkeypatch, _Resp(200, {"delay": value}))
        assert _client().delay("n", 5000) is None


def test_delay_without_value_returns_none(monkeypatch):
    _patch(monkeypatch, _Resp(200, {"message": "ok"}))
    assert _client().delay("n", 5000) is None


def test_delay_http_error_returns_none(monkeypatch):
    _patch(monkeypatch, _Resp(500, {"delay": 12}))
    assert _client().delay("n", 5000) is None


def test_delay_transport_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise clash_client.requests.RequestException("down")

    monkeypatch.setattr(clash_client.requests, "request", boom)
    assert _client().delay("n", 5000) is None


# --------------------------------------------------------------------- delays

def test_delays_returns_complete_key_set(monkeypatch):
    def fake(method, url, json=None, headers=None, timeout=None, **kwargs):
        encoded = url.split("/proxies/", 1)[1].rsplit("/delay", 1)[0]
        node = unquote(encoded)
        return _Resp(200, {"delay": len(node)})

    monkeypatch.setattr(clash_client.requests, "request", fake)
    nodes = ["b", "a/b c", "c"]
    result = _client().delays(nodes)

    assert set(result) == set(nodes)
    assert result == {"b": 1, "a/b c": 5, "c": 1}


def test_delays_keeps_none_for_unreachable_nodes(monkeypatch):
    def fake(method, url, json=None, headers=None, timeout=None, **kwargs):
        node = unquote(url.split("/proxies/", 1)[1].rsplit("/delay", 1)[0])
        return _Resp(200, {"delay": 10} if node == "ok" else {})

    monkeypatch.setattr(clash_client.requests, "request", fake)
    result = _client().delays(["ok", "bad"])
    assert result == {"ok": 10, "bad": None}


def test_delays_empty_returns_empty_without_pool(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("empty input must not touch the network")

    monkeypatch.setattr(clash_client.requests, "request", boom)
    assert _client().delays([]) == {}


# ------------------------------------------------------------- selector_state

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


def test_selector_state_missing_all_keeps_now(monkeypatch):
    _patch(monkeypatch, _Resp(200, {"now": "n2"}))
    assert _client().selector_state("sel") == (None, "n2")


def test_selector_state_missing_now_keeps_all(monkeypatch):
    _patch(monkeypatch, _Resp(200, {"all": ["n1", "n2"]}))
    assert _client().selector_state("sel") == (["n1", "n2"], None)


def test_selector_state_wrong_types_drop_only_bad_field(monkeypatch):
    _patch(monkeypatch, _Resp(200, {"all": "not-a-list", "now": "n2"}))
    assert _client().selector_state("sel") == (None, "n2")

    _patch(monkeypatch, _Resp(200, {"all": ["n1"], "now": 42}))
    assert _client().selector_state("sel") == (["n1"], None)


def test_selector_state_non_200_returns_none_pair(monkeypatch):
    _patch(monkeypatch, _Resp(500, {"all": [], "now": "n"}))
    assert _client().selector_state("sel") == (None, None)


# ---------------------------------------------------------------- connections

def test_connections_returns_dict_only_on_200(monkeypatch):
    _patch(monkeypatch, _Resp(200, {"connections": []}))
    assert _client().connections() == {"connections": []}

    _patch(monkeypatch, _Resp(500, {"connections": []}))
    assert _client().connections() is None


def test_connections_transport_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise clash_client.requests.RequestException("down")

    monkeypatch.setattr(clash_client.requests, "request", boom)
    assert _client().connections() is None
