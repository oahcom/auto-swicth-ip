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
    """daemon 与 cron 都读改写 .bad_nodes.json，必须共用同一把锁且不覆盖。

    写口已收编到 `asip.state_store.writers.update_bad_nodes`：锁路径由该模块
    唯一计算（不再由 daemon 自己拼），语义差异（append-only vs overwrite）是
    参数。这里钉两件事：daemon 走的是 append-only 语义（不覆盖他方条目），
    并且锁路径与 cron 侧一致。
    """
    import json as _json

    from asip.state_store import writers

    target = tmp_path / ".bad_nodes.json"
    target.write_text(_json.dumps({"from-cron": {"expire": 999, "reason": "x"}}))
    monkeypatch.setattr(daemon, "_BAD_NODES_FILE", str(target))
    monkeypatch.setattr(daemon, "_DEAD_NODES", {"from-daemon"})
    monkeypatch.setattr(daemon, "_DEAD_NODES_RELEASE_TS", {})
    monkeypatch.setattr(daemon, "_now_ts", lambda: 1000)
    monkeypatch.setattr(daemon, "BAD_NODES_TTL", 300)

    seen = {}
    real_update = writers.update_bad_nodes

    def spy(path, entries, **kwargs):
        seen["path"] = path
        seen["entries"] = set(entries)
        seen["overwrite"] = kwargs.get("overwrite")
        return real_update(path, entries, **kwargs)

    monkeypatch.setattr(writers, "update_bad_nodes", spy)
    daemon._export_dead_nodes_locked(10)

    assert seen["path"] == str(target)
    assert seen["entries"] == {"from-daemon"}
    assert seen["overwrite"] is False            # append-only：不抹他方条目
    assert writers.lock_path_for(str(target)) == str(target) + ".lock"
    # 真的落了盘，且他方条目保留
    assert set(_json.loads(target.read_text())) == {"from-cron", "from-daemon"}


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
    # 错误切换主链路只做秒切：用秒切成功替身代替原来的同步换线替身。
    monkeypatch.setattr(daemon, "_fast_switch",
                        lambda reason: {"tag": "WG-NEW", "egress": "1.2.3.4"})

    out = daemon._rotate_once("rate429")

    assert out["switched"] is True
    assert out["active_tag"] == "WG-NEW", (
        "换线成功后 active_tag 不是刚切上去那条 —— 调用方会以为没换、不重置连接"
    )
    assert out["active_egress"] == "1.2.3.4", (
        "换线成功后 active_egress 不是新地址 —— 调用方读到的身份可能是旧的"
    )


def test_quick_switch_failure_reports_no_healthy_slot(monkeypatch, _rotate_isolated):
    """秒切失败：回包说清「无可用闸口，直接重启」，不切线，旧闸仍后台修。"""
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 0.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-OLD")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "old-egress")
    calls = []
    monkeypatch.setattr(daemon, "_fast_switch",
                        lambda reason: calls.append("quick") or None)
    monkeypatch.setattr(daemon, "_reconnect_warp_by_table",
                        lambda *a, **k: calls.append("sync") or False)
    monkeypatch.setattr(daemon, "_kick_background_repair",
                        lambda tag, ip, reason: calls.append("repair"))

    out = daemon._rotate_once("rate429")

    assert out["switched"] is False and out["ok"] is True, out
    assert out["reason"] == "no_healthy_slot", out
    assert out["restart_required"] is True and out["line_unknown"] is True, out
    assert out["switch_to_url"] is None, out
    assert calls == ["quick", "repair"], calls

def test_transport_rotation_does_not_blacklist_the_ip(monkeypatch, _rotate_isolated):
    """无首字节超时只换线，不把可能是健康 IP 的出口按 429/403 记脏。"""
    marked = []
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 0.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-OLD")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "old-egress")
    monkeypatch.setattr(daemon, "_mark_warp_ip_used",
                        lambda *a, **kw: marked.append((a, kw)))
    monkeypatch.setattr(daemon, "_reconnect_warp_by_table",
                        lambda reason, interrupt=False, out=None: (
                            out.update({"tag": "WG-NEW", "ip": "1.2.3.4",
                                        "switched_at": 1234.0}) or True))

    out = daemon._rotate_once("transport")

    assert out["switched"] is True
    assert marked == [], "transport 悬挂不是 IP 级封禁证据，不能写 blocked 台账"
    assert daemon._normalize_rotate_reason("header-timeout") == "transport"
    assert daemon._normalize_rotate_reason("upstream-stall") == "transport"


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
    # 落池前的池健康闸（2026-11-09 加）：真探会连外网，这里按「池可用」打桩。
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "_PROXY_HEALTH_CACHE", None)

    ok = daemon._fallback_to_proxy({"tag": "WG-7", "port": 1087}, "stale-egress",
                                   time.time())

    assert ok is True, "用例前提：落池应当成功"
    assert daemon._ACTIVE_WARP_IP is None, (
        "落池后进程级出口地址仍是旧 WARP 地址 —— /rotate 会把它当成当前出口"
    )


def test_rotate_skips_switching_when_the_exit_ledger_shows_the_ip_was_replaced(
        monkeypatch, _rotate_isolated):
    """换线表显示这个地址在这条请求发出**之后**被换掉 → 重连即可。

    这是唯一不靠推断的判据：表里那条「旧地址 → 新地址 @ 时刻」是切换真的发生后
    写下的，请求时刻早于它意味着这条请求打通的隧道已经下线。
    """
    monkeypatch.setattr(daemon, "_EXIT_LEDGER", [])
    now = time.time()
    daemon._exit_ledger_note("2a09:bac5:DEAD::1", "2a09:bac5:NEW::9", now)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-NEW")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "2a09:bac5:NEW::9")
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 0.0)
    switched = []
    monkeypatch.setattr(daemon, "_reconnect_warp_by_table",
                        lambda *a, **k: switched.append(1) or True)

    # 请求发出于切换之前 2 秒。
    out = daemon._rotate_once("rate429", now - 2, "2a09:bac5:DEAD::1")

    assert out["reason"] == "already_retired", (
        "换线表显示地址在请求后被换走，却仍去换线 —— 用旧证据烧掉了一条好线"
    )
    assert out["switched"] is False
    assert not switched, "already_retired 分支不该调用换线"
    assert out["active_egress"] == "2a09:bac5:NEW::9", (
        "already_retired 的回复没带当前出口 —— 调用方缓存的还是那个作废地址"
    )


def test_rotate_switches_when_the_switch_happened_before_the_request(
        monkeypatch, _rotate_isolated):
    """主动换线发生在请求**之前** → 必须真换线。

    这是「只比 IP 是否等于当前出口」会踩的坑：asip 自己主动换线（主循环、探测
    判死、定时轮换，都不经过 9router），客户端缓存必然与当前出口不符。若据此
    判 already_retired，就从不换线，服务彻底卡死。请求打的是新出口，429 真实，
    必须换。
    """
    monkeypatch.setattr(daemon, "_EXIT_LEDGER", [])
    now = time.time()
    daemon._exit_ledger_note("2a09:bac5:OLD::1", "2a09:bac5:NEW::9", now - 60)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-NEW")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "2a09:bac5:NEW::9")
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 0.0)
    # 错误切换主链路只做秒切：用秒切成功替身代替原来的同步换线替身。
    monkeypatch.setattr(daemon, "_fast_switch",
                        lambda reason: {"tag": "WG-X", "egress": "1.2.3.4"})

    # 缓存里还是换线前的旧地址，但这次请求是在切换之后发出的。
    out = daemon._rotate_once("rate429", now, "2a09:bac5:OLD::1")

    assert out["switched"] is True, (
        "主动换线早于请求发出，却判成 already_retired —— 服务会从此永不换线"
    )
    assert out["active_egress"] == "1.2.3.4"


def test_rotate_switches_when_the_429_egress_is_still_current(
        monkeypatch, _rotate_isolated):
    """429 追的地址仍是当前出口 → 必须真换线，重连没有意义。"""
    monkeypatch.setattr(daemon, "_EXIT_LEDGER", [])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-OLD")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "2a09:bac5:AAAA::1")
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 0.0)
    # 错误切换主链路只做秒切：用秒切成功替身代替原来的同步换线替身。
    monkeypatch.setattr(daemon, "_fast_switch",
                        lambda reason: {"tag": "WG-NEW", "egress": "1.2.3.4"})

    out = daemon._rotate_once("rate429", time.time(), "2a09:bac5:AAAA::1")

    assert out["switched"] is True, "地址仍是出口却没换线 —— 调用方重试必然再 429"
    assert out["active_egress"] == "1.2.3.4"


def test_rotate_switches_when_the_egress_is_unknown(monkeypatch, _rotate_isolated):
    """判不出来时按正常换线走：漏判比多换一次的代价大得多。"""
    monkeypatch.setattr(daemon, "_EXIT_LEDGER", [])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", None)
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 0.0)
    # 错误切换主链路只做秒切：用秒切成功替身代替原来的同步换线替身。
    monkeypatch.setattr(daemon, "_fast_switch",
                        lambda reason: {"tag": "WG-NEW", "egress": "1.2.3.4"})

    out = daemon._rotate_once("rate429", None, "2a09:bac5:DEAD::1")

    assert out["switched"] is True, "当前出口未知时不该判 already_retired"


def test_exit_ledger_drops_the_oldest_entries():
    """表只保留最近 N 条：它是旁路证据，不是审计日志。"""
    original = list(daemon._EXIT_LEDGER)
    try:
        daemon._EXIT_LEDGER.clear()
        for i in range(daemon._EXIT_LEDGER_MAX + 10):
            daemon._exit_ledger_note(f"old{i}", f"new{i}", float(i))
        assert len(daemon._EXIT_LEDGER) == daemon._EXIT_LEDGER_MAX
        assert daemon._EXIT_LEDGER[-1]["old"] == f"old{daemon._EXIT_LEDGER_MAX + 9}"
    finally:
        daemon._EXIT_LEDGER[:] = original


def test_rotate_ignores_a_cache_that_postdates_the_request(monkeypatch, _rotate_isolated):
    """调用方缓存的接入时刻晚于请求发出 → 这个地址不可能承载过这条请求。

    9router 的缓存只在 /rotate 的回复里更新，而回复说「此刻出口是 X」不代表
    这条请求走的是 X。若缓存的接入时刻比请求还新，它就是一条错配的证据，
    据此判 already_retired 会让一真实的 429 无人处理。判不出就正常换线。
    """
    monkeypatch.setattr(daemon, "_EXIT_LEDGER", [])
    now = time.time()
    daemon._exit_ledger_note("2a09:bac5:DEAD::1", "2a09:bac5:NEW::9", now)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-NEW")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "2a09:bac5:NEW::9")
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 0.0)
    # 错误切换主链路只做秒切：用秒切成功替身代替原来的同步换线替身。
    monkeypatch.setattr(daemon, "_fast_switch",
                        lambda reason: {"tag": "WG-X", "egress": "1.2.3.4"})

    # 请求发出于 now-10，但客户端缓存说这个地址是 now-2 才接上的 —— 晚于请求。
    out = daemon._rotate_once("rate429", now - 10, "2a09:bac5:DEAD::1", now - 2)

    assert out.get("reason") != "already_retired", (
        "缓存的接入时刻晚于请求发出，却据此判已换走 —— 这条 429 会被漏处理"
    )
    assert out["switched"] is True


def test_rotate_trusts_a_cache_that_predates_the_request(monkeypatch, _rotate_isolated):
    """缓存接入时刻早于请求 → 缓存可信，正常查换线表。"""
    monkeypatch.setattr(daemon, "_EXIT_LEDGER", [])
    now = time.time()
    daemon._exit_ledger_note("2a09:bac5:OLD::1", "2a09:bac5:NEW::9", now - 5)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-NEW")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "2a09:bac5:NEW::9")
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 0.0)

    out = daemon._rotate_once("rate429", now - 10, "2a09:bac5:OLD::1", now - 60)

    assert out["reason"] == "already_retired"


def test_rotate_reports_when_the_active_egress_was_installed(monkeypatch, _rotate_isolated):
    """回复要带 active_egress_since：调用方靠它判断自己的缓存可不可信。"""
    monkeypatch.setattr(daemon, "_EXIT_LEDGER", [])
    monkeypatch.setattr(daemon, "_WARP_SWITCH_HISTORY", [])
    now = time.time()
    daemon._exit_ledger_note("prev-ip", "2a09:bac5:CUR::1", now - 30)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WG-1")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "2a09:bac5:CUR::1")
    monkeypatch.setattr(daemon, "_rotate_gap", lambda: 3600.0)
    monkeypatch.setattr(daemon, "_last_rotate_ts", time.time() - 2)

    out = daemon._rotate_once("rate429")

    assert out["active_egress"] == "2a09:bac5:CUR::1"
    assert out["active_egress_since"] == pytest.approx(now - 30, abs=2), (
        "回复没带这个地址的接入时刻 —— 调用方无从分辨新地址和躺了很久的旧值"
    )
