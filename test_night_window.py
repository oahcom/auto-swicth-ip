"""夜间刷新锁的回归用例（2026-11-01）。

背景：上游免费层配额按出口 IP 计，次日 08:00 重置。20:00→08:00 之间被烧的
地址，重启只会换到另一个同样被烧的新 /128 —— 10-31 夜间实测 17036 次重启、
0 次刷新成功。窗口内只该刷新隔夜存活下来的线。

本文件锁三条契约：
1. 窗口按**主机本地时区**算，且跨零点正确（20→8 是两段，不是一段反区间）。
2. 锁只作用于重启，**探测照常** —— 与 test_restart_cap_does_not_stop_probing 同源。
3. 当前出口不锁：`_restart_warp_inplace` 是「所有备线都不新鲜」时的最后兜底，
   锁住它等于夜里彻底断流。
"""
import time
import zoneinfo

import pytest

import daemon


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {})
    monkeypatch.setattr(daemon, "_WARP_PROBING", set())
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_PROBE_BLOCKED_AT", {})
    monkeypatch.setattr(daemon, "_save_warp_line_state", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_NIGHT_LOG_TS", {})
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _set_tz(name: str) -> None:
    import os
    os.environ["TZ"] = name
    time.tzset()


def _at(hour: int, tz: str = "Asia/Shanghai") -> float:
    """构造「该时区某天某点」的时间戳，用于把窗口判定钉死在墙上时间。"""
    base = time.mktime(time.struct_time((2026, 11, 1, 0, 0, 0, 0, 0, -1)))
    return base + hour * 3600


def _night(**kw) -> dict:
    nw = dict(daemon._warp()["night_window"])
    nw.update(kw)
    return nw


@pytest.fixture
def night(monkeypatch):
    """替换夜间锁配置，退出时由 monkeypatch 还原。"""
    def _apply(**kw):
        monkeypatch.setitem(daemon.CFG["warp"], "night_window", _night(**kw))
    return _apply


def _stub_restart(monkeypatch):
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WARP-CF")
    monkeypatch.setattr(daemon, "warp_line_busy", lambda tag, win: False)
    monkeypatch.setattr(daemon, "_acquire_refresh_slot", lambda tag: True)
    monkeypatch.setattr(daemon, "_release_refresh_slot", lambda tag: None)
    monkeypatch.setattr(daemon, "_RESTART_SEM", None)
    monkeypatch.setattr(daemon, "_restart_service", lambda svc, tag=None: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda port, t: True)
    monkeypatch.setattr(daemon, "_probe_refresh_egress", lambda port: "9.9.9.9")
    monkeypatch.setattr(daemon, "_invalidate_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(daemon, "_line_block_remaining", lambda ip, ts: 0.0)


def _member(tag: str) -> dict:
    return {"tag": tag, "port": 10002, "service": f"usque-warp-{tag.lower()}"}


# ─────────────────────────────────────────────────────────────
# 窗口判定
# ─────────────────────────────────────────────────────────────

def test_window_is_local_time_across_midnight(night):
    """20→8 必须覆盖 20/23/0/7 点，且排除 8/12/19 点。

    写成 `start <= h < end` 时，跨零点的窗口会被判成空集 —— 夜里一眼看不出，
    等到第二天看日志才发现一次都没锁上。
    """
    _set_tz("Asia/Shanghai")
    night(enabled=True, start_hour=20, end_hour=8)
    for h in (20, 23, 0, 3, 7):
        assert daemon._night_active(_at(h)), f"{h} 点不在夜间窗口内"
    for h in (8, 12, 19):
        assert not daemon._night_active(_at(h)), f"{h} 点被误判进夜间窗口"


def test_window_disabled_never_blocks(night):
    night(enabled=False)
    assert not daemon._night_active(_at(23)), "enabled=False 仍在拦 —— 开关失效"


def test_allowlist_intersects_live_members(monkeypatch, night):
    """白名单与 sing-box 实际成员取交集，避免把刷新花在切不走的线上。

    `.warp/warp-outbound.json` 自 10-28 未更新，CF-E/CF-F/WG-13 至今不在
    `opencode-out` 里 —— 夜里刷出地址也切不过去。
    """
    night(allow=["WARP-CF", "WARP-CF-E", "WARP-WG-5"])
    monkeypatch.setattr(daemon, "_clash_snapshot",
                        lambda sel: {"all": ["WARP-CF", "WARP-WG-5", "proxy"]})
    assert daemon._night_allow_tags() == {"WARP-CF", "WARP-WG-5"}

    # 快照未建立（启动早期）时不能把白名单整个清空，否则夜里一次都不放行。
    monkeypatch.setattr(daemon, "_clash_snapshot", lambda sel: None)
    assert daemon._night_allow_tags() == {"WARP-CF", "WARP-CF-E", "WARP-WG-5"}

    # 交集为空（warp-outbound 没重建）时退回不锁：失败要落在「无限制」一侧。
    monkeypatch.setattr(daemon, "_clash_snapshot",
                        lambda sel: {"all": ["WARP-WG-2", "proxy"]})
    assert daemon._night_allow_tags() == {"WARP-CF", "WARP-CF-E", "WARP-WG-5"}, (
        "交集为空时把白名单清空了 —— 夜里一条线都不许刷新"
    )


# ─────────────────────────────────────────────────────────────
# 锁的作用面：只拦重启
# ─────────────────────────────────────────────────────────────

def test_night_blocks_non_allowlisted_restart(monkeypatch, night):
    _set_tz("Asia/Shanghai")
    night(enabled=True, start_hour=20, end_hour=8, allow=["WARP-CF"])
    monkeypatch.setattr(daemon, "_clash_snapshot",
                        lambda sel: {"all": ["WARP-CF", "WARP-WG-2"]})
    _stub_restart(monkeypatch)

    ran = []
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda svc, tag=None: ran.append(tag) or True)
    now = _at(23)

    assert daemon._night_block_restart("WARP-WG-2", "模型层 quota", now) is True
    assert daemon._night_block_restart("WARP-CF", "模型层 quota", now) is False
    assert daemon._night_block_restart("WARP-WG-2", "模型层 quota", _at(12)) is False, (
        "窗口外仍被拦 —— 锁没按时段放开"
    )


def test_night_never_blocks_active_line(monkeypatch, night):
    """当前出口必须始终放行：原地重连是最后兜底，锁住它等于夜里断流。"""
    _set_tz("Asia/Shanghai")
    night(enabled=True, start_hour=20, end_hour=8, allow=["WARP-CF"])
    monkeypatch.setattr(daemon, "_clash_snapshot", lambda sel: {"all": ["WARP-WG-2"]})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WARP-WG-2")

    assert daemon._night_block_restart("WARP-WG-2", "落池兜底", _at(23)) is False, (
        "当前出口被夜间锁拦下 —— _restart_warp_inplace 会整夜失败"
    )


def test_night_blocks_the_choke_point(monkeypatch, night):
    """主闸必须装在 `_restart_warp_line` 上：它是四条刷新路径的唯一汇合点。"""
    _set_tz("Asia/Shanghai")
    night(enabled=True, start_hour=20, end_hour=8, allow=["WARP-CF"])
    monkeypatch.setattr(daemon, "_clash_snapshot",
                        lambda sel: {"all": ["WARP-CF", "WARP-WG-2"]})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WARP-CF")
    monkeypatch.setattr(daemon, "_restart_rate_allow", lambda now: True)
    calls = []
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda svc, tag=None: calls.append(tag) or True)
    monkeypatch.setattr(daemon.time, "time", lambda: _at(23))

    assert daemon._restart_warp_line(_member("WARP-WG-2")) is False
    assert calls == [], f"夜间锁被绕过，实际重启了 {calls}"
    assert daemon._restart_warp_line(_member("WARP-CF")) is True


def test_daytime_pays_no_selector_lookup(monkeypatch, night):
    """白天不查 selector：夜里才生效的闸不该让全天每次重启多一发 HTTP。"""
    _set_tz("Asia/Shanghai")
    night(enabled=True, start_hour=20, end_hour=8, allow=["WARP-CF"])
    queried = []
    monkeypatch.setattr(daemon, "_clash_snapshot",
                        lambda sel: queried.append(sel) or {"all": ["WARP-CF"]})
    monkeypatch.setattr(daemon, "active_warp_tag",
                        lambda: queried.append("active") or "WARP-WG-2")

    assert daemon._night_block_restart("WARP-WG-2", "测试", _at(12)) is False
    assert queried == [], f"白天仍然查了 selector: {queried}"


def test_night_gate_is_first_in_line_restart(monkeypatch, night):
    """`_warp_line_restart` 的闸必须在 warming/退避记账之前。

    删掉它流程仍会在更下层 `_restart_warp_line` 被拦，但那时已经
    `warming=True`、`restart_hist` 追加了一条、`fail_streak` +1 ——
    一次都没重启，却记了一次重启和一次失败，把每小时的额度和退避白烧掉。
    """
    _set_tz("Asia/Shanghai")
    night(enabled=True, start_hour=20, end_hour=8, allow=["WARP-CF"])
    monkeypatch.setattr(daemon, "_clash_snapshot",
                        lambda sel: {"all": ["WARP-CF", "WARP-WG-2"]})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WARP-CF")
    monkeypatch.setattr(daemon.time, "time", lambda: _at(23))
    calls = []
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda svc, tag=None: calls.append(tag) or True)
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("WARP-WG-2").update(
            ip="1.1.1.1", restart_hist=[], fail_streak=3, warming=False)

    assert daemon._warp_line_restart(_member("WARP-WG-2"), "模型层 quota") is False
    assert calls == [], "夜间锁未拦住 _warp_line_restart"
    st = daemon._warp_line_state("WARP-WG-2")
    assert st["restart_hist"] == [], f"被拦的重启仍记进了额度: {st['restart_hist']}"
    assert st["fail_streak"] == 3, f"被拦的重启污染了 fail_streak: {st['fail_streak']}"
    assert not st.get("warming"), "被拦的重启把线标成了 warming"


def test_night_log_is_per_tag(monkeypatch, night):
    """节流按 tag：整池被拦时每条线都该留下痕迹。"""
    _set_tz("Asia/Shanghai")
    night(enabled=True, start_hour=20, end_hour=8, allow=["WARP-CF"])
    monkeypatch.setattr(daemon, "_clash_snapshot",
                        lambda sel: {"all": ["WARP-CF", "WARP-WG-2", "WARP-WG-3"]})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WARP-CF")
    logged = []
    monkeypatch.setattr(daemon, "log", lambda m: logged.append(m))
    now = _at(23)

    for tag in ("WARP-WG-2", "WARP-WG-3"):
        daemon._night_block_restart(tag, "模型层 quota", now)
    assert any("WARP-WG-2" in m for m in logged) and any("WARP-WG-3" in m for m in logged), (
        f"全局单时间戳把第二条线吞了，日志只有: {logged}"
    )


def test_night_covers_subscription_rebind(monkeypatch, night):
    """订阅刷新触发的换绑定也是一次重启，必须同源受锁。"""
    _set_tz("Asia/Shanghai")
    night(enabled=True, start_hour=20, end_hour=8, allow=["WARP-CF"])
    monkeypatch.setattr(daemon, "_clash_snapshot",
                        lambda sel: {"all": ["WARP-CF", "WARP-WG-2"]})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WARP-CF")
    monkeypatch.setattr(daemon.time, "time", lambda: _at(23))
    monkeypatch.setitem(daemon.CFG["warp"], "wg", {"enabled": True})

    import types
    fake = types.SimpleNamespace(validate_bindings=lambda: ["WARP-WG-2", "WARP-CF"])
    monkeypatch.setitem(__import__("sys").modules, "warp_wg", fake)
    monkeypatch.setattr(daemon, "_warp",
                        lambda: {"members": [_member("WARP-WG-2"), _member("WARP-CF")],
                                 "night_window": daemon.CFG["warp"]["night_window"],
                                 "selector": "opencode-out", "wg": {"enabled": True}})
    calls = []
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda svc, tag=None: calls.append(tag) or True)

    daemon._rebind_wg_lines_after_refresh()
    assert calls == ["WARP-CF"], f"夜间锁未覆盖订阅刷新换绑定，实际重启 {calls}"


def test_night_does_not_stop_probing(night):
    """夜里非白名单线照样探测 —— 压的是重启，不是观测。

    夜里探到 quota 要照常记账并计入 fail_streak，08:00 一开闸立刻知道
    哪条线该刷；停探会让闸门打开时整个池子都是未知状态。
    """
    _set_tz("Asia/Shanghai")
    night(enabled=True, start_hour=20, end_hour=8, allow=["WARP-CF"])
    now = _at(23)
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("WARP-WG-2").update(
            ip="1.1.1.1", last_ok_ts=0.0, last_ok_ip=None,
            last_probe_ts=now - 3600, next_probe_ts=0.0)
    assert daemon._warp_line_needs_probe("WARP-WG-2", now)[0], (
        "夜间锁把探测也停了 —— 闸门只作用于重启"
    )


# ─────────────────────────────────────────────────────────────
# 配置契约
# ─────────────────────────────────────────────────────────────

def test_production_config_is_sane():
    """线上配置本身必须是有效形态（起止不同、白名单非空、元素均为字符串）。

    读的是 config.py 的**原始**默认值，不是 CFG 现值 —— conftest 的
    `_disable_night_window` 会把 enabled 关掉以免干扰其它套件。

    成员是否存在于 CFG 依赖 `WARP_WG_*` 环境变量，没 source `.env` 时 WG 线
    根本不在里面 —— 所以「tag 是否存在」不在这里断言，由运行时交集兜住。
    """
    import config
    nw = config.CFG["warp"]["night_window"]
    assert isinstance(nw.get("enabled"), bool), "enabled 必须是布尔"
    assert nw["start_hour"] != nw["end_hour"], "起止小时相同＝全天锁死"
    assert nw["allow"], "allow 为空＝夜里没有任何线可刷新"
    assert all(isinstance(t, str) and t.strip() for t in nw["allow"]), "allow 里有非字符串"
