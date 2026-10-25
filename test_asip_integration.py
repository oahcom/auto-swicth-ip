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


def test_singbox_api_drops_error_body_on_non_2xx(monkeypatch):
    """4xx/5xx 的 JSON 错误页不能当业务数据返回（历史契约）。"""
    monkeypatch.setattr(daemon._CLASH, "request_json",
                        lambda *a, **k: (500, {"message": "boom"}))
    assert daemon.singbox_api("/p", silent=True) == (500, None)


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
    ("line1\u2028line2", "line1\\nline2"),
    ("line1\u2029line2", "line1\\nline2"),
    ("line1\x85line2", "line1\\nline2"),
    ("line1\vline2", "line1\\nline2"),
    ("line1\fline2", "line1\\nline2"),
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


def test_schedule_idle_refresh_is_noop_when_worker_owns_scheduling(monkeypatch):
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"],
        "line_worker": {"enabled": True, "dry_run": False},
    })
    before = set(daemon._WARP_REFRESH_PENDING)
    daemon._schedule_idle_refresh({"tag": "W1", "port": 1080})
    assert daemon._WARP_REFRESH_PENDING == before


def test_line_worker_dry_run_keeps_legacy_schedulers(monkeypatch):
    """dry_run 只观测；若让它接管，灰度窗口内没有任何预热/补刷/脏线重连。"""
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"],
        "line_worker": {"enabled": True, "dry_run": True},
    })
    assert daemon._line_worker_enabled() is True
    assert daemon._line_worker_owns_scheduling() is False


def test_connections_json_swallows_client_exceptions(monkeypatch):
    """读取失败必须缓存 None；旧实现吞掉所有异常，新通道不能把异常抛给调用方。"""
    def boom(*a, **k):
        raise RuntimeError("clash exploded")

    monkeypatch.setattr(daemon._CLASH, "connections", boom)
    monkeypatch.setattr(daemon, "_conn_snapshot", (0.0, None))
    monkeypatch.setattr(daemon, "_LAST_HEALTH_LOG", 0.0)
    assert daemon._connections_json() is None


def test_refresh_line_serialized_respects_background_slot(monkeypatch):
    """后台补刷/按线 worker 持锁时，同步路径不得再重启同一条线。"""
    called = []
    monkeypatch.setattr(daemon, "_acquire_refresh_slot", lambda tag: False)
    monkeypatch.setattr(daemon, "_refresh_idle_warp",
                        lambda m: called.append(m["tag"]) or "1.2.3.4")
    assert daemon._refresh_line_serialized({"tag": "W1"}) == (None, False)
    assert called == []


def test_refresh_line_serialized_releases_slot_on_failure(monkeypatch):
    tag = "W9"
    daemon._WARP_REFRESHING.discard(tag)

    def boom(m):
        raise RuntimeError("restart blew up")

    monkeypatch.setattr(daemon, "_refresh_idle_warp", boom)
    with pytest.raises(RuntimeError):
        daemon._refresh_line_serialized({"tag": tag})
    assert tag not in daemon._WARP_REFRESHING


def test_connections_json_caches_failures_by_completion_time(monkeypatch):
    """请求自身耗时超过 TTL 时，失败也必须缓存，否则等待者会串行重发。"""
    import time as _time

    calls = []

    def slow_boom(*a, **k):
        calls.append(1)
        _time.sleep(1.1)
        raise RuntimeError("clash slow failure")

    monkeypatch.setattr(daemon._CLASH, "connections", slow_boom)
    monkeypatch.setattr(daemon, "_conn_snapshot", (0.0, None))
    monkeypatch.setattr(daemon, "_LAST_HEALTH_LOG", 0.0)
    assert daemon._connections_json() is None
    assert daemon._connections_json() is None
    assert len(calls) == 1
