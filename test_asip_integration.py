"""daemon ↔ asip 边界的回归测试。

这些测试只碰共享层的调用形状，不启动 daemon 主循环，也不访问真实网络：
它们保护的是重构后最容易悄悄改掉的三件事 —— Clash 不可达仍返回 `(0, None)`、
出口探测仍要求 `warp=on`、line_worker 默认不接管旧守护。
"""
import sys
import time

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
    ("\x1b[31mred\x1b[0m", "red"),
    ("\x1b]0;title\x1b\\after", "after"),
    ("a\x00b", "a b"),
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

    started = []

    class NoopThread:
        def __init__(self, target=None, *a, **k):
            self.target = target

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(daemon.threading, "Thread", NoopThread)
    daemon._WARP_REFRESH_PENDING.discard("W1")
    daemon._schedule_idle_refresh({"tag": "W1", "port": 1080})
    assert "W1" in daemon._WARP_REFRESH_PENDING
    assert started
    daemon._WARP_REFRESH_PENDING.discard("W1")


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


def test_line_worker_dry_run_keeps_legacy_scheduling(monkeypatch):
    """灰度 dry_run 只观测：旧守护必须继续预热/补刷，否则窗口内无人修复。"""
    monkeypatch.setitem(daemon.CFG["warp"], "line_worker",
                        {"enabled": True, "dry_run": True, "interval_sec": 30})
    assert daemon._line_worker_owns_scheduling() is False
    monkeypatch.setitem(daemon.CFG["warp"], "line_worker",
                        {"enabled": True, "dry_run": False, "interval_sec": 30})
    assert daemon._line_worker_owns_scheduling() is True
    monkeypatch.setitem(daemon.CFG["warp"], "line_worker",
                        {"enabled": False, "dry_run": False, "interval_sec": 30})
    assert daemon._line_worker_owns_scheduling() is False


def test_dead_nodes_export_shares_lock_and_keeps_foreign_entries(monkeypatch, tmp_path):
    """daemon 与 cron 都读改写 .bad_nodes.json，必须共用同一把锁且不覆盖。"""
    import json as _json

    target = tmp_path / ".bad_nodes.json"
    target.write_text(_json.dumps({"from-cron": {"expire": 999, "reason": "x"}}))
    monkeypatch.setattr(daemon, "_BAD_NODES_FILE", str(target))
    monkeypatch.setattr(daemon, "_DEAD_NODES", {"from-daemon"})
    monkeypatch.setattr(daemon, "_DEAD_NODES_RELEASE_TS", {})
    monkeypatch.setattr(daemon, "_now_ts", lambda: 1000)

    seen = {}

    def fake_update(path, merge, **kwargs):
        seen["path"] = path
        seen["lock"] = kwargs.get("lock_path")
        seen["data"] = merge(_json.loads(target.read_text()))

    monkeypatch.setattr(daemon, "_atomic_update_json", fake_update)
    daemon._export_dead_nodes_locked(10)
    assert seen["path"] == str(target)
    assert seen["lock"] == str(target) + ".lock"
    assert set(seen["data"]) == {"from-cron", "from-daemon"}


# ─────────────────────────────────────────────────────────────
# /rotate 的回复必须带上「此刻哪条线是出口」
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def _rotate_isolated(monkeypatch):
    """让 _rotate_once 只做自己的事：不换线、不落盘、不碰真实台账。"""
    monkeypatch.setattr(daemon, "_last_rotate_ts", 0.0)
    monkeypatch.setattr(daemon, "_api_stats",
                        {"calls": 0, "switched": 0, "coalesced": 0, "retired": 0,
                         "too_frequent": 0, "no_standby": 0})
    monkeypatch.setattr(daemon, "_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", None)
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now: False)


def test_rotate_reports_active_line_when_it_declines(monkeypatch, _rotate_isolated):
    """没换线也必须回答「现在哪条线、哪个地址是出口」。

    调用方只有这个字段能看见「asip 自己换过线」（主循环、探测判死、定时轮换都
    不经过 9router）。被限速的那次回复若什么都不说，调用方就以为没变、继续骑着
    旧隧道 —— 那批连接会一直吐旧出口的 429。
    """
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-7")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "2a09:bac5:6215:28::4:347")
    # 刚换过、但没到合并窗口：走 too_frequent 而不是 coalesced。
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 3600.0)
    monkeypatch.setattr(daemon, "_last_rotate_ts", time.time() - 2)

    out = daemon._rotate_once("rate429")

    assert out["reason"] == "too_frequent", "用例前提：这一轮应当被限速"
    assert out["active_tag"] == "WG-7", (
        "被限速的回复没带 active_tag —— 调用方看不见 asip 自己换过的那次线"
    )
    assert out["active_egress"] == "2a09:bac5:6215:28::4:347", (
        "被限速的回复没带 active_egress —— 原地重连换了地址但线名不变，"
        "调用方据此会以为没换、继续骑着断掉的连接"
    )


def test_rotate_reports_the_new_line_after_switching(monkeypatch, _rotate_isolated):
    """换线成功时要报刚切上去那条，不能去读 selector 快照。

    快照轮询 0.5s 才刷新一次，刚 PUT 完读回来可能还是被换下的旧线；调用方据此
    判断「没变」就不重置连接，这批连接会一直吐旧出口的 429。
    """
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 0.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-OLD")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "old-egress")
    monkeypatch.setattr(daemon, "_reconnect_warp_by_table",
                        lambda reason, interrupt=False, out=None: (
                            out.update({"tag": "WG-NEW", "ip": "1.2.3.4",
                                        "switched_at": 1234.0}) or True))

    out = daemon._rotate_once("rate429")

    assert out["switched"] is True
    assert out["active_tag"] == "WG-NEW", (
        "换线成功后 active_tag 不是刚切上去那条 —— 调用方会以为没换、不重置连接"
    )
    assert out["active_egress"] == "1.2.3.4", (
        "换线成功后 active_egress 不是新地址 —— 调用方读到的身份可能是旧的"
    )


def test_rotate_sees_in_place_reconnect_that_keeps_the_same_line(monkeypatch, _rotate_isolated):
    """原地重连：线名不变、只有地址变，也必须报出来。

    这条路径重启的是当前隧道，在途连接是被杀掉的。只比线名的话调用方会以为
    什么都没发生 —— 它手上那批连接已经死了，还继续往里发请求。
    """
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-7")
    # 原地重连成功时线表里的地址已被 invalidate 成 None，进程级出口是新地址。
    monkeypatch.setattr(daemon, "_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "2a09:bac5:6215:28::4:347")
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 3600.0)
    monkeypatch.setattr(daemon, "_last_rotate_ts", time.time() - 2)

    out = daemon._rotate_once("rate429")

    assert out["active_tag"] == "WG-7"
    assert out["active_egress"] == "2a09:bac5:6215:28::4:347", (
        "线表里的地址已被清成 None 时没有退回进程级出口 —— 原地重连看不见"
    )


def test_rotate_reports_no_warp_line_after_falling_back_to_proxy(monkeypatch, _rotate_isolated):
    """落池后不能还报着旧 WARP 出口。

    此时连接仍挂在被换下那条隧道上，而「当前出口」已经不是任何 WARP 线。
    继续报旧线名/旧地址，调用方会以为身份没变、什么都不做。
    """
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)
    monkeypatch.setattr(daemon, "_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", None)

    out = daemon._with_active_line({}, time.time())

    assert out["active_tag"] is None
    assert out["active_egress"] is None, (
        "落池后仍报着 WARP 出口地址 —— 调用方看不出出口已经换到轮换池"
    )


def test_fallback_to_proxy_clears_the_process_wide_egress(monkeypatch):
    """落池必须清掉进程级出口地址。

    留着它，`_with_active_line` 在 selector 指向 proxy 时会继续报旧地址，
    而那批连接恰恰还挂在被换下那条隧道上。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", True)
    monkeypatch.setattr(daemon, "_load_proxy_fallback_ts", lambda: 0.0)
    monkeypatch.setattr(daemon, "_write_json_atomic", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: True)
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)
    monkeypatch.setattr(daemon, "_begin_segment", lambda ts: None)
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "stale-egress")

    ok = daemon._fallback_to_proxy({"tag": "WG-7", "port": 1087}, "stale-egress",
                                   time.time())

    assert ok is True, "用例前提：落池应当成功"
    assert daemon._ACTIVE_WARP_IP is None, (
        "落池后进程级出口地址仍是旧 WARP 地址 —— /rotate 会把它当成当前出口"
    )


def test_rotate_skips_switching_when_the_429_egress_is_already_retired(
        monkeypatch, _rotate_isolated):
    """429 追的地址已经不是当前出口 → 重连即可，不该再烧一条新线。

    这是唯一不靠推断的判据：调用方手里的地址就是它刚打通的隧道，只要当前出口
    不是它，那条隧道就已经换掉了。拿时刻回溯去猜「当时哪条线」会隔三层推断，
    而这里是一次字符串比较。
    """
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-NEW")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "2a09:bac5:6215:28::4:347")
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 0.0)
    switched = []
    monkeypatch.setattr(daemon, "_reconnect_warp_by_table",
                        lambda *a, **k: switched.append(1) or True)

    out = daemon._rotate_once("rate429", None, "2a09:bac5:DEAD::1")

    assert out["reason"] == "already_retired", (
        "调用方报的地址已不是当前出口，却仍去换线 —— 用旧证据烧掉了一条好线"
    )
    assert out["switched"] is False
    assert not switched, "already_retired 分支不该调用换线"
    assert out["active_egress"] == "2a09:bac5:6215:28::4:347", (
        "already_retired 的回复没带当前出口 —— 调用方缓存的还是那个作废地址，"
        "下一条 429 会被反复判成 already_retired"
    )


def test_rotate_switches_when_the_429_egress_is_still_current(
        monkeypatch, _rotate_isolated):
    """429 追的地址仍是当前出口 → 必须真换线，重连没有意义。"""
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-OLD")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "2a09:bac5:AAAA::1")
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 0.0)
    monkeypatch.setattr(daemon, "_reconnect_warp_by_table",
                        lambda reason, interrupt=False, out=None: (
                            out.update({"tag": "WG-NEW", "ip": "1.2.3.4",
                                        "switched_at": 1234.0}) or True))

    out = daemon._rotate_once("rate429", None, "2a09:bac5:AAAA::1")

    assert out["switched"] is True, "地址仍是出口却没换线 —— 调用方重试必然再 429"
    assert out["active_egress"] == "1.2.3.4"


def test_rotate_switches_when_the_egress_is_unknown(monkeypatch, _rotate_isolated):
    """判不出来时按正常换线走：漏判比多换一次的代价大得多。

    调用方首轮没有缓存地址（还没见过任何 /rotate 回复），或停在轮换池时当前
    出口为 None。两种都判不出「已换走」，这时宁可多烧一格配额，也不能让调用方
    拿着一条可能已经死掉的隧道反复重试。
    """
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", None)
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 0.0)
    monkeypatch.setattr(daemon, "_reconnect_warp_by_table",
                        lambda reason, interrupt=False, out=None: (
                            out.update({"tag": "WG-NEW", "ip": "1.2.3.4",
                                        "switched_at": 1234.0}) or True))

    out = daemon._rotate_once("rate429", None, "2a09:bac5:DEAD::1")

    assert out["switched"] is True, "当前出口未知时不该判 already_retired"
