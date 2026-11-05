"""asip.egress 的确定性单测（不联网，monkeypatch egress.proc_run）。"""

from asip.proc import ProcResult

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
        return ProcResult(code=returncode, stdout=stdout)

    monkeypatch.setattr(egress, "proc_run", fake)
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
    monkeypatch.setattr(
        egress, "proc_run",
        lambda argv, **kw: ProcResult(code=None, error="binary not found: curl"))
    result = probe_egress()
    assert result.ip is None
    assert result.error == "curl_missing"


def test_timeout(monkeypatch):
    monkeypatch.setattr(egress, "proc_run",
                        lambda argv, **kw: ProcResult(code=None, timed_out=True))
    result = probe_egress()
    assert result.ip is None
    assert result.error == "timeout"


def test_nonzero_returncode(monkeypatch):
    _install(monkeypatch, "", returncode=22)
    result = probe_egress()
    assert result.ip is None
    assert result.error == "rc"


def test_curl_max_time_exit_is_reported_as_timeout(monkeypatch):
    """curl 自己撞上 `--max-time` 时退出码是 28 —— 那是超时，不是 `rc`。

    不分开的话，「端口没监听」（7，6ms 返回）和「端到端挂起」（28，整段
    预算）在台账里是同一个词，调用方没法据此判断该不该重启隧道。
    """
    _install(monkeypatch, "", returncode=28)
    result = probe_egress()
    assert result.ip is None
    assert result.error == "timeout"


def test_multiple_urls_skip_warp_off_then_succeed(monkeypatch):
    bodies = iter([TRACE_OFF, TRACE_ON])
    seen = []

    def fake(argv, **kwargs):
        seen.append(argv[-1])
        return ProcResult(code=0, stdout=next(bodies))

    monkeypatch.setattr(egress, "proc_run", fake)
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
        ProcResult(code=None, timed_out=True),
        ProcResult(code=7, stdout=""),
    ])
    monkeypatch.setattr(egress, "proc_run", lambda *a, **k: next(outcomes))
    result = probe_egress(urls=(TRACE_URL, IPIFY_URL))
    assert result.ip is None
    assert result.error == "timeout"


def test_proc_run_is_the_single_execution_seam(monkeypatch):
    seen = []

    def fake(argv, **kwargs):
        seen.append(argv)
        return ProcResult(code=0, stdout=TRACE_ON)

    monkeypatch.setattr(egress, "proc_run", fake)
    assert probe_egress().ip == "203.0.113.7"
    assert len(seen) == 1


def test_trace_carries_colo_and_loc(monkeypatch):
    """trace 里的 `colo=` / `loc=` 必须带出来。

    WHY：节点→colo 台账（`.warp/wg_colo_map.json`）此前只能靠
    `warp_wg.survey()` 逐节点起 sing-box 测出来。而生产探测**本来每次都
    拿到了完整 trace**，只是 `EgressResult` 把它丢了。实测 2026-11-05：
    WG-3/WG-4 的落地节点从没跑过 survey，块表里没有它们 ⇒ 同块轮回判不出
    「同块」直接放弃，两条线在死节点上无限重启（fail_streak 197 / 88）。
    """
    _install(monkeypatch,
             stdout="ip=203.0.113.7\nwarp=on\ncolo=NRT\nloc=JP\n")
    r = probe_egress()
    assert r.ip == "203.0.113.7"
    assert r.colo == "NRT", f"colo 被丢掉了: {r}"
    assert r.loc == "JP", f"loc 被丢掉了: {r}"


def test_missing_colo_stays_none(monkeypatch):
    """trace 里没有 colo 时给 None，不能编一个空串 —— 调用方用 `if colo` 判。"""
    _install(monkeypatch, stdout="ip=203.0.113.7\nwarp=on\n")
    r = probe_egress()
    assert r.colo is None and r.loc is None
