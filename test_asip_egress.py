"""asip.egress 的确定性单测（不联网，monkeypatch subprocess.run）。"""

import subprocess
from types import SimpleNamespace

import pytest

from asip import egress
from asip.egress import (
    IPIFY_URL,
    TRACE_URL,
    EgressResult,
    fetch_egress_ip,
    probe_egress,
)

TRACE_ON = "fl=abc\nh=www.cloudflare.com\nip=203.0.113.7\nts=1\nwarp=on\n"
TRACE_OFF = "fl=abc\nip=203.0.113.7\nwarp=off\n"


def _install(monkeypatch, stdout="", returncode=0):
    calls = []

    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(egress.subprocess, "run", fake)
    return calls


def test_trace_returns_ip_and_warp_on(monkeypatch):
    calls = _install(monkeypatch, TRACE_ON)
    result = probe_egress()
    assert isinstance(result, EgressResult)
    assert result.ip == "203.0.113.7"
    assert result.warp is True
    assert result.url == TRACE_URL
    assert result.error is None
    assert len(calls) == 1


def test_require_warp_off_returns_none_and_warp_off(monkeypatch):
    _install(monkeypatch, TRACE_OFF)
    result = probe_egress(require_warp=True)
    assert result.ip is None
    assert result.error == "warp_off"


def test_warp_off_is_kept_when_not_required(monkeypatch):
    _install(monkeypatch, TRACE_OFF)
    result = probe_egress()
    assert result.ip == "203.0.113.7"
    assert result.warp is False


def test_plain_text_ip(monkeypatch):
    _install(monkeypatch, "198.51.100.9\n")
    result = probe_egress(urls=(IPIFY_URL,))
    assert result.ip == "198.51.100.9"
    assert result.warp is None
    assert result.url == IPIFY_URL


def test_invalid_ip_string(monkeypatch):
    _install(monkeypatch, "999.999.999.999\n")
    result = probe_egress()
    assert result.ip is None
    assert result.error == "invalid_ip"


def test_empty_body_is_bad_body(monkeypatch):
    _install(monkeypatch, "  \n")
    result = probe_egress()
    assert result.ip is None
    assert result.error == "bad_body"


def test_curl_missing(monkeypatch):
    def boom(argv, **kwargs):
        raise FileNotFoundError("curl")

    monkeypatch.setattr(egress.subprocess, "run", boom)
    result = probe_egress()
    assert result.ip is None
    assert result.error == "curl_missing"


def test_timeout(monkeypatch):
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(egress.subprocess, "run", slow)
    result = probe_egress()
    assert result.ip is None
    assert result.error == "timeout"


def test_nonzero_returncode(monkeypatch):
    _install(monkeypatch, "", returncode=22)
    result = probe_egress()
    assert result.ip is None
    assert result.error == "rc"


def test_multiple_urls_skip_warp_off_then_succeed(monkeypatch):
    bodies = iter([TRACE_OFF, TRACE_ON])
    seen = []

    def fake(argv, **kwargs):
        seen.append(argv[-1])
        return SimpleNamespace(returncode=0, stdout=next(bodies), stderr="")

    monkeypatch.setattr(egress.subprocess, "run", fake)
    urls = (TRACE_URL, IPIFY_URL)
    result = probe_egress(urls=urls, require_warp=True)
    assert result.ip == "203.0.113.7"
    assert result.url == IPIFY_URL
    assert seen == list(urls)


def test_socks_and_http_proxy_are_mutually_exclusive():
    with pytest.raises(ValueError):
        probe_egress(socks_port=1080, http_proxy="http://127.0.0.1:8080")


def test_fetch_egress_ip_returns_str(monkeypatch):
    _install(monkeypatch, TRACE_ON)
    assert fetch_egress_ip() == "203.0.113.7"


def test_fetch_egress_ip_returns_none_on_failure(monkeypatch):
    _install(monkeypatch, "", returncode=7)
    assert fetch_egress_ip() is None


def test_curl_argv_uses_max_time_and_socks5h(monkeypatch):
    calls = _install(monkeypatch, TRACE_ON)
    probe_egress(socks_port=1080, timeout=7.5)
    argv, kwargs = calls[0]
    assert argv[0] == "curl"
    assert argv[argv.index("--max-time") + 1] == "7.5"
    assert argv[argv.index("--proxy") + 1] == "socks5h://127.0.0.1:1080"
    assert kwargs["timeout"] == 12.5


def test_http_proxy_is_passed_through(monkeypatch):
    calls = _install(monkeypatch, TRACE_ON)
    probe_egress(http_proxy="http://10.0.0.1:3128")
    argv, _ = calls[0]
    assert argv[argv.index("--proxy") + 1] == "http://10.0.0.1:3128"


def test_most_informative_error_wins(monkeypatch):
    outcomes = iter([
        subprocess.TimeoutExpired(cmd="curl", timeout=1),
        SimpleNamespace(returncode=7, stdout="", stderr=""),
    ])

    def fake(argv, **kwargs):
        item = next(outcomes)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(egress.subprocess, "run", fake)
    result = probe_egress(urls=(TRACE_URL, IPIFY_URL))
    assert result.ip is None
    assert result.error == "timeout"


def test_module_level_run_alias_is_patchable(monkeypatch):
    seen = []

    def fake(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=TRACE_ON, stderr="")

    monkeypatch.setattr(egress, "run", fake)
    assert probe_egress().ip == "203.0.113.7"
    assert len(seen) == 1
