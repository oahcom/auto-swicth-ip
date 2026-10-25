"""daemon ↔ asip 边界的回归测试。

这些测试只碰共享层的调用形状，不启动 daemon 主循环，也不访问真实网络：
它们保护的是重构后最容易悄悄改掉的三件事 —— Clash 不可达仍返回 `(0, None)`、
出口探测仍要求 `warp=on`、line_worker 默认不接管旧守护。
"""
import sys

import pytest

import daemon


def test_singbox_api_maps_transport_failure_to_zero(monkeypatch):
    monkeypatch.setattr(daemon._CLASH, "request_json",
                        lambda *a, **k: (None, None))
    assert daemon.singbox_api("/proxies/x", silent=True) == (0, None)


def test_singbox_api_passes_through_http_status(monkeypatch):
    monkeypatch.setattr(daemon._CLASH, "request_json",
                        lambda *a, **k: (401, None))
    assert daemon.singbox_api("/proxies/x", silent=True) == (401, None)


def test_singbox_api_returns_parsed_body(monkeypatch):
    monkeypatch.setattr(daemon._CLASH, "request_json",
                        lambda *a, **k: (200, {"now": "W1"}))
    assert daemon.singbox_api("/p") == (200, {"now": "W1"})


def test_warp_egress_ip_requires_warp_on(monkeypatch):
    calls = {}

    class Result:
        ip = "1.2.3.4"

    def fake_probe(**kwargs):
        calls.update(kwargs)
        return Result()

    monkeypatch.setattr(daemon, "probe_egress", fake_probe)
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "socks_host": "10.0.0.1"})

    assert daemon.warp_egress_ip(1080) == "1.2.3.4"
    assert calls["socks_port"] == 1080
    assert calls["socks_host"] == "10.0.0.1"
    assert calls["require_warp"] is True
    assert calls["urls"] == (daemon.TRACE_URL,)


def test_warp_egress_ip_returns_none_when_probe_fails(monkeypatch):
    monkeypatch.setattr(daemon, "probe_egress",
                        lambda **k: type("R", (), {"ip": None})())
    assert daemon.warp_egress_ip(1080) is None


@pytest.mark.parametrize("raw,expected", [
    ("line1\nline2", "line1\\nline2"),
    ("line1\r\nline2", "line1\\nline2"),
    ("plain", "plain"),
])
def test_log_message_is_single_line(raw, expected):
    assert daemon._sanitize_log_message(raw) == expected


def test_log_message_truncates_pathological_input():
    text = daemon._sanitize_log_message("x" * 5000)
    assert len(text) < 5000
    assert text.endswith("…(truncated)")


def test_line_worker_disabled_by_default():
    assert daemon._line_worker_enabled() is False


def test_start_line_workers_forwards_config(monkeypatch):
    members = [{"tag": "W1", "port": 1080}]
    seen = {}

    def fake_start(host, members_arg=None, *, dry_run, interval_sec):
        seen.update(host=host, members=members_arg, dry_run=dry_run,
                    interval=interval_sec)

    monkeypatch.setitem(sys.modules, "warp_line_worker",
                        type(sys)("warp_line_worker"))
    sys.modules["warp_line_worker"].start = fake_start
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"],
        "members": members,
        "line_worker": {"enabled": True, "dry_run": True, "interval_sec": 7},
    })

    assert daemon._line_worker_enabled() is True
    daemon._start_line_workers()
    assert seen["host"] is daemon
    assert seen["members"] == members
    assert seen["dry_run"] is True
    assert seen["interval"] == 7


def test_schedule_idle_refresh_is_noop_when_worker_enabled(monkeypatch):
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"],
        "line_worker": {"enabled": True},
    })
    before = set(daemon._WARP_REFRESH_PENDING)
    daemon._schedule_idle_refresh({"tag": "W1", "port": 1080})
    assert daemon._WARP_REFRESH_PENDING == before
