#!/usr/bin/env python3
"""渠道 ban 率锁自检（离线、无网络、无 pytest）。

规格（2026-11-02 主人定）：
  - 维度是**渠道**（线路 tag），不是 colo；
  - 判据：最近 `channel_ban_window_sec`(30min) 内模型层实测全部封禁且样本 ≥2；
  - 命中后该渠道锁到**次日** `channel_ban_reset_hour`(08:00)，期间不重启；
  - 样本/锁定跨 daemon 重启保留，窗口外样本在启动时裁掉。

跑法： python3 -I /tmp/opencode/channel_ban_selftest.py
"""
import os
import sys
import tempfile
import time

try:                      # 直跑（python3 -I）没有 pytest，本文件要能独立运行
    import pytest
except ImportError:
    pytest = None

_TMP = tempfile.mkdtemp(prefix="asip-banhold-test-")
os.environ["ASIP_NO_SIDE_EFFECTS"] = "1"
os.environ.setdefault("WARP_LINE_STATE_FILE_PATH", os.path.join(_TMP, "state.json"))
os.environ.setdefault("WARP_IP_USED_FILE_PATH", os.path.join(_TMP, "used.json"))
os.environ.setdefault("WARP_SEGMENT_FILE_PATH", os.path.join(_TMP, "seg.json"))

sys.path.insert(0, "/home/administrator/auto-swicth-ip")
import config
import daemon

W = config.CFG["warp"]

def _sync_submit_once(key, fn, *a):
    """同步执行：本文件测的是锁/还原逻辑，不是线程模型。"""
    fn(*a)
    return True

# 本文件会改的全局原值。pytest 和别的测试文件同进程跑，必须还原 ——
# 不还原的话 `test_daemon_unit.py::test_submit_once_skips_reentry_until_task_
# finishes` 会红，别的文件的限速断言会静默地跑在「无上限」上。
_ORIG = {"submit_once": daemon._submit_once,
         "restart_max_per_minute": W.get("restart_max_per_minute"),
         "restart_max_per_hour_line": W.get("restart_max_per_hour_per_line")}

def _cleanup_globals():
    daemon._submit_once = _ORIG["submit_once"]
    W["restart_max_per_minute"] = _ORIG["restart_max_per_minute"]
    W["restart_max_per_hour_per_line"] = _ORIG["restart_max_per_hour_line"]

if pytest is not None:
    @pytest.fixture(autouse=True)
    def _guard():
        yield
        _cleanup_globals()

def _reset():
    W["channel_ban_hold_enabled"] = True
    W["channel_ban_window_sec"] = 3600
    W["channel_ban_min_samples"] = 30      # 口径：严格大于 30 条
    W["channel_ban_reset_hour"] = 8
    daemon._WARP_LINE_STATE.clear()
    daemon._NIGHT_LOG_TS.clear()
    # `_channel_ban_note` 命中时会 `_submit_once("channel-rehome", ...)` 起一个
    # **后台线程**。测试里不拦它，它就会挑下一个测试跑在半路时才落地，用那个
    # 测试的 stub 写台账 —— 表现为「跑 5 次有 1 次红」。改成同步执行：本文件
    # 要测的是锁/还原逻辑本身，不是那个线程模型。
    daemon._submit_once = _sync_submit_once

# ---- 时间算术 ----
def test_hold_secs_until_next_8am():
    _reset()
    # 2026-11-02 16:30 本地 → 次日 08:00 = 15.5h
    t = time.mktime((2026, 11, 2, 16, 30, 0, 0, 0, -1))
    assert abs(daemon._channel_ban_hold_secs(t) - 15.5 * 3600) < 1, "应锁到次日 8 点"
    # 07:30 → 当天 08:00，只有 30min
    t = time.mktime((2026, 11, 3, 7, 30, 0, 0, 0, -1))
    assert abs(daemon._channel_ban_hold_secs(t) - 1800) < 1, "8 点前应只锁 30 分钟"

# ---- 窗口与命中 ----
def test_window_trims_and_counts():
    _reset()
    now = 1_000_000.0
    win = [(now - 4000, True), (now - 100, True), (now - 50, True)]
    n, b = daemon._channel_ban_level(win, now)
    assert (n, b) == (2, 2), f"1h 窗口外样本应被裁掉，实际 {n},{b}"
    assert len(win) == 2, "裁剪要就地生效"

def test_needs_strictly_more_than_30_samples():
    _reset()
    now = 1_000_000.0
    # 跨度必须 ≥ 1800s 才过得了「铺满窗口」那条判据，否则测的永远是 cover，
    # 条数判据根本没被执行到 —— 用间隔 63s 让 30 条铺满 1827s。
    full = lambda k, step=63: [(now - i * step, True) for i in range(k)]
    assert daemon._channel_ban_hold_hit(full(30), now) is False, "恰好 30 条不锁（严格大于）"
    assert daemon._channel_ban_hold_hit(full(31), now) is True, "31 条全封且铺满应锁"
    # 同一批样本把阈值降到 29 必须变 True：证明上面那个 False 来自条数判据，
    # 而不是被 cover 判据顺手挡掉的。
    W["channel_ban_min_samples"] = 29
    assert daemon._channel_ban_hold_hit(full(30), now) is True
    W["channel_ban_min_samples"] = 30

def test_burst_does_not_lock():
    """31 条挤在 4.7 分钟里不算「持续一小时」——实测 19:43 那批 17 条线就是这样锁的。"""
    _reset()
    now = 1_000_000.0
    burst = [(now - i * 9, True) for i in range(31)]   # 跨度 270s < 1800s
    assert daemon._channel_ban_hold_hit(burst, now) is False, "连撞 31 次不该锁"

def test_one_ok_breaks_100_percent():
    _reset()
    now = 1_000_000.0
    # 必须铺满 1800s，否则 cover 判据先返回 False —— 那样删掉 `b != n` 这条
    # 判据测试也照样绿，等于没测。40 条每 50s 一条，跨 1950s。
    win = [(now - i * 50, True) for i in range(40)] + [(now - 60, False)]
    # 先确认这批样本**本来会锁**（全 True 时命中），再看加一条 OK 就不锁。
    assert daemon._channel_ban_hold_hit([(t, True) for t, _ in win], now) is True
    assert daemon._channel_ban_hold_hit(win, now) is False, "有一次 OK 就不算 100%"

# ---- 记账 → 锁定 ----
def test_note_locks_channel():
    _reset()
    now = 1_000_000.0
    for i in range(31):
        daemon._channel_ban_note("WARP-WG-3", True, now - 1800 + i * 60)
    st = daemon._WARP_LINE_STATE["WARP-WG-3"]
    assert daemon._channel_ban_held("WARP-WG-3", now) is True, "31 条 100% 应锁"
    assert abs(st["ban_hold_until"] - (now + daemon._channel_ban_hold_secs(now) - 1)) < 2

def test_low_sample_channel_not_locked():
    """用户口径：WG-4/11/12 那种只有几条实测的 100% 不该锁。"""
    _reset()
    now = 1_000_000.0
    for i in range(6):
        daemon._channel_ban_note("WARP-WG-12", True, now - 300 + i)
    assert daemon._channel_ban_held("WARP-WG-12", now) is False, "6 条样本不足以判烧穿"

def test_note_ok_prevents_lock():
    """窗口内出现过一次 OK 就不是 100%，哪怕封禁条数已经过 30。"""
    _reset()
    now = 1_000_000.0
    for i in range(15):
        daemon._channel_ban_note("WARP-WG-9", True, now - 600 + i)
    daemon._channel_ban_note("WARP-WG-9", False, now - 500)
    for i in range(16):
        daemon._channel_ban_note("WARP-WG-9", True, now - 400 + i)
    assert daemon._channel_ban_held("WARP-WG-9", now) is False, "夹一条 OK 不该锁"

def test_held_channel_skips_restart():
    _reset()
    W["restart_max_per_minute"] = 0
    W["restart_max_per_hour_per_line"] = 0
    tag = "WARP-WG-4"
    member = {"tag": tag, "port": 11997, "service": "warp-wg-4.service"}
    now = time.time()
    daemon._warp_line_update(tag, ban_hold_until=now + 3600,
                             next_restart_ts=0.0, warming=False)
    orig_active, orig_restart = daemon.active_warp_tag, daemon._restart_warp_line
    called = []
    daemon.active_warp_tag = lambda: "WARP-CF"
    daemon._restart_warp_line = lambda *a, **k: called.append(a) or True
    try:
        assert daemon._warp_line_restart(member, "模型层 quota") is False
        assert called == [], "锁定渠道不该进重启路径"
    finally:
        daemon.active_warp_tag, daemon._restart_warp_line = orig_active, orig_restart

def test_hold_expires_after_reset():
    _reset()
    tag = "WARP-WG-1"
    now = 1_000_000.0
    daemon._warp_line_update(tag, ban_hold_until=now - 1)
    assert daemon._channel_ban_held(tag, now) is False, "过了解封时刻就该放行"

def test_expired_hold_can_lock_again():
    """过期的 ban_hold_until 不能永久挡住重新锁定。

    原实现用 `if st.get("ban_hold_until"):` 判「已经锁过」—— 字段是数值，
    过期后仍非零，于是渠道只要在前一天被锁过一次，此后永远不会再被锁。
    远端连续跑几天必然踩到，本地只看单日日志看不出来。
    """
    _reset()
    # 判据是「严格大于 min_samples」，设 2 就要 3 条样本。
    W["channel_ban_min_samples"] = 2
    now = 1_000_000.0
    tag = "WARP-WG-8"
    daemon._warp_line_update(tag, ban_hold_until=now - 86400)  # 昨天的锁
    for i in range(3):
        # 样本要铺满窗口（1h）的一半，否则会被「铺满窗口」判据挡掉。
        # 跨度取满 1h：第 3 条入账时 now-earliest = 3600 ≥ 1800。
        daemon._channel_ban_note(tag, True, now - 3600 + i * 1800)
    assert daemon._channel_ban_held(tag, now) is True, "过期锁之后应能重新锁上"

def test_save_and_reload_keeps_hold_and_trims_window():
    _reset()
    now = time.time()
    tag = "WARP-WG-2"
    daemon._warp_line_update(
        tag, ban_hold_until=now + 7200,
        ban_window=[(now - 60, True), (now - 4000, True)],
        ip="2a09:bac1:3b00:10::16:181", last_ok_ip="2a09:bac1:3b00:10::16:181")
    daemon._save_warp_line_state(force=True)
    daemon._WARP_LINE_STATE.clear()
    daemon._load_warp_line_state()
    st = daemon._WARP_LINE_STATE[tag]
    assert st["ban_hold_until"] > now, "渠道锁必须跨重启保留"
    assert len(st["ban_window"]) == 1, "窗口外样本应在启动时裁掉"

def test_disabled_switch_is_inert():
    _reset()
    W["channel_ban_hold_enabled"] = False
    now = 1_000_000.0
    daemon._channel_ban_note("WARP-WG-7", True, now - 10)
    daemon._channel_ban_note("WARP-WG-7", True, now)
    assert daemon._channel_ban_held("WARP-WG-7", now) is False
    assert daemon._channel_ban_label(now) == "关"

def test_label_counts_held_channels():
    _reset()
    now = time.time()
    daemon._warp_line_update("WARP-WG-1", ban_hold_until=now + 600,
                             ban_hold_ready=now)
    daemon._warp_line_update("WARP-WG-3", ban_hold_until=now + 600,
                             ban_hold_ready=now)
    assert daemon._channel_ban_label(now) == "2线→08:00"
    # 有一条配置写了但重启没成功 → 必须能看出来，否则「换了」只是台账上的说法
    daemon._warp_line_update("WARP-WG-3", ban_hold_ready=0.0)
    assert daemon._channel_ban_label(now) == "2线→08:00·1待重启"

# ---- 重定位 / 次日还原 ----
def _rehome_env(monkeypatch=None):
    """把节点/节点池读写全部换成内存替身。

    台账里的 node 用 `_ledger` 模拟：`write_line` 改它、`_line_node` 读它，
    这样「写完配置后 `_line_node` 应该等于新节点」和「apply 抹回 home 后要
    补写」都能被测到。`_restarts` 记重启调用，`restart_ok` 控制成功与否。
    """
    daemon._rehome_calls = []
    daemon._ledger = {"WARP-WG-4": "n-old"}
    daemon._restarts = []
    daemon.restart_ok = True
    orig = (daemon.warp_wg.udp_capable, daemon.warp_wg.load_nodes,
            daemon.warp_wg.write_line, daemon.warp_wg._read_state,
            daemon.warp_wg.write_json, daemon.warp_wg.state_path,
            daemon.warp_wg.load_colo_map, daemon._line_node,
            daemon._warp_line_restart, daemon._warp_member)
    daemon.warp_wg.load_nodes = lambda: [{"tag": "n-la"}, {"tag": "n-sin"}, {"tag": "n-old"}]
    daemon.warp_wg.udp_capable = lambda nodes, probe=False: list(nodes)
    daemon.warp_wg.write_line = lambda i, n, family=None: (
        daemon._rehome_calls.append((i, n["tag"])),
        daemon._ledger.__setitem__("WARP-WG-4", n["tag"]))[0]
    daemon.warp_wg._read_state = lambda: {"lines": {}}
    daemon.warp_wg.write_json = lambda *a, **k: None
    daemon.warp_wg.state_path = lambda: "/tmp/unused-state.json"
    daemon.warp_wg.load_colo_map = lambda: {
        "n-la": {"colo": "LAX", "ok": True}, "n-sin": {"colo": "SIN", "ok": True},
        "n-old": {"colo": "SIN", "ok": True}}
    daemon._line_node = lambda tag: daemon._ledger.get(tag)
    daemon._warp_member = lambda t: {"tag": t, "kind": "wg", "index": 4,
                                     "family": "v6", "service": "warp-wg-4.service"}
    daemon._warp_line_restart = lambda m, r, **k: (
        daemon._restarts.append((m["tag"], r, k)), daemon.restart_ok)[1]
    return orig

def _rehome_restore(orig):
    (daemon.warp_wg.udp_capable, daemon.warp_wg.load_nodes,
     daemon.warp_wg.write_line, daemon.warp_wg._read_state,
     daemon.warp_wg.write_json, daemon.warp_wg.state_path,
     daemon.warp_wg.load_colo_map, daemon._line_node,
     daemon._warp_line_restart, daemon._warp_member) = orig

def test_rehome_target_picks_lowest_ban_colo():
    _reset()
    W["channel_rehome_min_samples"] = 10
    orig = _rehome_env()
    cake = lambda win, now: {"SIN": (500, 400), "LAX": (50, 5), "CFB": (3, 0)}
    import ipv6_segments
    orig_rates = ipv6_segments.colo_ban_rates
    ipv6_segments.colo_ban_rates = cake
    try:
        got = daemon._channel_rehome_target(1_000_000.0)
        assert got == ("LAX", "n-la"), f"应选 ban 率最低且有样本的 LAX，实际 {got}"
        W["channel_rehome_min_samples"] = 100      # CFB 样本不足；LAX 50 也不足
        got = daemon._channel_rehome_target(1_000_000.0)
        assert got == ("SIN", "n-sin"), f"样本闸提高后应只剩 SIN，实际 {got}"
        # 本轮已用掉的节点必须排除，否则一批线会挤到同一个节点
        got = daemon._channel_rehome_target(1_000_000.0, {"n-sin", "n-old"})
        assert got is None, f"候选都被排除时应返回 None，实际 {got}"
        # 跨调用：别的线已经绑着的节点也不能选（既定事实第 20 条）。
        # 阈值必须先调回 10 —— 上面提到 100 之后 LAX 本来就会被样本闸挡掉，
        # 那种情况下即使不排除占用也照样返回 SIN，测不出任何东西。
        W["channel_rehome_min_samples"] = 10
        orig_ln = daemon.warp_wg.line_nodes
        daemon.warp_wg.line_nodes = lambda: {"WARP-WG-9": "n-la"}
        try:
            got = daemon._channel_rehome_target(1_000_000.0)
            assert got == ("SIN", "n-sin"), f"LAX 已被占用，应退到 SIN，实际 {got}"
        finally:
            daemon.warp_wg.line_nodes = orig_ln
    finally:
        ipv6_segments.colo_ban_rates = orig_rates
        _rehome_restore(orig)

def test_rehome_restarts_line_after_write():
    """写完配置必须立刻重启 —— 否则进程仍跑旧节点，锁又挡着重启路径。"""
    _reset()
    W["channel_rehome_min_samples"] = 10
    orig = _rehome_env()
    import ipv6_segments
    orig_rates = ipv6_segments.colo_ban_rates
    ipv6_segments.colo_ban_rates = lambda win, now: {"LAX": (50, 5), "SIN": (500, 400)}
    now = time.time()
    try:
        tag = "WARP-WG-4"
        daemon._warp_line_update(tag, ban_hold_until=now + 3600)
        daemon._rehome_held_lines(now)
        assert daemon._restarts, "重定位后必须重启这条线"
        assert daemon._restarts[-1][2].get("rebound") is True, "必须走 rebound 分支"
        assert daemon._WARP_LINE_STATE[tag]["ban_hold_ready"] > 0, "重启成功后要记 ready"
        # 重启失败 → 不记 ready，下一轮重试
        daemon.restart_ok = False
        daemon._WARP_LINE_STATE[tag]["ban_hold_ready"] = 0.0
        daemon._rehome_held_lines(now)
        assert daemon._WARP_LINE_STATE[tag]["ban_hold_ready"] == 0.0, "失败不该记 ready"
    finally:
        ipv6_segments.colo_ban_rates = orig_rates
        _rehome_restore(orig)

def test_rehome_rewrites_after_apply_clobber():
    """apply/订阅换绑定把 node 抹回 home 后，下一轮对账要补写并重启。"""
    _reset()
    W["channel_rehome_min_samples"] = 10
    orig = _rehome_env()
    import ipv6_segments
    orig_rates = ipv6_segments.colo_ban_rates
    ipv6_segments.colo_ban_rates = lambda win, now: {"LAX": (50, 5), "SIN": (500, 400)}
    now = time.time()
    try:
        tag = "WARP-WG-4"
        daemon._warp_line_update(tag, ban_hold_until=now + 3600)
        daemon._rehome_held_lines(now)
        n = len(daemon._rehome_calls)
        daemon._ledger[tag] = "n-old"          # 模拟 apply 整表重写
        daemon._WARP_LINE_STATE[tag]["ban_hold_ready"] = 0.0
        daemon._rehome_held_lines(now)
        assert len(daemon._rehome_calls) == n + 1, "被抹回 home 后应补写"
        assert daemon._ledger[tag] == "n-la", "补写后应回到目标节点"
        assert daemon._WARP_LINE_STATE[tag]["ban_hold_ready"] > 0
    finally:
        ipv6_segments.colo_ban_rates = orig_rates
        _rehome_restore(orig)

def test_restore_clears_home_for_non_wg():
    """非 WG 线（usque/H2）还原时 _rehome_one 返回 skip，必须当结束处理。

    否则 ban_hold_home 永远留着，主循环每分钟白跑一次还原。
    """
    _reset()
    now = time.time()
    daemon._WARP_LINE_STATE.clear()
    daemon._warp_line_update("WARP-CF-D", ban_hold_until=now - 1,
                             ban_hold_home="n-x", ban_hold_node="n-y")
    orig = (daemon._rehome_one, daemon._warp_member)
    daemon._rehome_one = lambda t, n: "skip"
    daemon._warp_member = lambda t: None
    try:
        daemon._rehome_held_lines(now)
        st = daemon._WARP_LINE_STATE["WARP-CF-D"]
        assert st["ban_hold_home"] is None, "skip 也要清台账，否则每分钟重试一次"
        assert st["ban_hold_node"] is None
    finally:
        daemon._rehome_one, daemon._warp_member = orig


def test_rehome_records_home_then_restores():
    _reset()
    W["channel_rehome_min_samples"] = 10
    orig = _rehome_env()
    import ipv6_segments
    orig_rates = ipv6_segments.colo_ban_rates
    ipv6_segments.colo_ban_rates = lambda win, now: {"LAX": (50, 5), "SIN": (500, 400)}
    now = time.time()
    try:
        tag = "WARP-WG-4"
        daemon._warp_line_update(tag, ban_hold_until=now + 3600)
        daemon._rehome_held_lines(now)
        st = daemon._WARP_LINE_STATE[tag]
        assert st["ban_hold_home"] == "n-old", "重定位前必须记下原节点"
        assert st["ban_hold_node"] == "n-la", "目标节点要记账，供对账用"
        assert daemon._rehome_calls[-1] == (4, "n-la"), "应写到最低 ban 率 colo 的节点"
        # 配置与台账一致 → 不该反复写
        n_before = len(daemon._rehome_calls)
        daemon._rehome_held_lines(now)
        assert len(daemon._rehome_calls) == n_before, "已重定位的线不该反复改配置"
        # 锁到期 → 还原
        daemon._WARP_LINE_STATE[tag]["ban_hold_until"] = now - 1
        daemon._rehome_held_lines(now)
        assert daemon._rehome_calls[-1] == (4, "n-old"), "到期应还原到原节点"
        assert daemon._WARP_LINE_STATE[tag]["ban_hold_home"] is None, "还原后清掉台账"
    finally:
        ipv6_segments.colo_ban_rates = orig_rates
        _rehome_restore(orig)

def test_switch_off_releases_existing_holds():
    """总闸关掉后，锁要解除，并且停在临时 colo 上的线要还原回原节点。"""
    _reset()
    orig = _rehome_env()
    now = time.time()
    try:
        daemon._warp_line_update("WARP-WG-4", ban_hold_until=now + 7200,
                                 ban_window=[(now - 60, True)],
                                 ban_hold_home="n-old", ban_hold_node="n-la")
        daemon._ledger["WARP-WG-4"] = "n-la"
        W["channel_ban_hold_enabled"] = False
        daemon._channel_ban_note("WARP-WG-4", True, now)
        st = daemon._WARP_LINE_STATE["WARP-WG-4"]
        assert daemon._channel_ban_held("WARP-WG-4", now) is False, "开关关了不该还锁着"
        assert not st["ban_window"]
        assert st["ban_hold_home"] == "n-old", "home 不能被清掉，否则永远回不去"
        daemon._rehome_held_lines(now)
        assert daemon._ledger["WARP-WG-4"] == "n-old", "关闸后应还原回原节点"
        assert daemon._WARP_LINE_STATE["WARP-WG-4"]["ban_hold_home"] is None
        # 关闸后不应再往临时节点搬
        n = len(daemon._rehome_calls)
        daemon._warp_line_update("WARP-WG-4", ban_hold_until=now + 7200)
        daemon._rehome_held_lines(now)
        assert len(daemon._rehome_calls) == n, "关掉开关后不该再重定位"
    finally:
        W["channel_ban_hold_enabled"] = True
        _rehome_restore(orig)

def test_restore_failure_keeps_home_and_retries():
    """还原时重启失败不能清 home —— 那是这条线唯一的回程记录。

    清了之后进程仍跑在临时 colo 上，台账却以为已经回家，永远不会再试。
    """
    _reset()
    orig = _rehome_env()
    now = time.time()
    calls = []
    orig_one = daemon._rehome_one
    daemon._rehome_one = lambda t, n: (calls.append(n), "ok")[1]
    daemon.restart_ok = False
    try:
        daemon._warp_line_update("WARP-WG-4", ban_hold_until=now - 1,
                                 ban_hold_home="n-old", ban_hold_node="n-la")
        daemon._rehome_held_lines(now)
        st = daemon._WARP_LINE_STATE["WARP-WG-4"]
        assert st["ban_hold_home"] == "n-old", "重启失败时必须保留 home"
        daemon._rehome_held_lines(now + 1)
        assert calls == ["n-old", "n-old"], f"下一轮必须重试，实际 {calls}"
    finally:
        daemon._rehome_one = orig_one
        _rehome_restore(orig)

def test_restore_converges_when_already_home_and_restarted():
    """已停在原节点、且写回后重启过 → 直接收尾，别再写配置再重启一次。

    实测 08:00~11:50：978 次「还原重启未成功」只换来 4 次成功，其中 14 条线
    当前节点本来就等于 home；每分钟一次的重写还把 `rebind_ts` 顶住，同块轮回
    对这批线永远不跑。
    """
    _reset()
    orig = _rehome_env()
    now = time.time()
    try:
        daemon._ledger["WARP-WG-4"] = "n-old"        # 已经在 home 上
        daemon.warp_wg._read_state = lambda: {
            "lines": {"WARP-WG-4": {"node": "n-old", "rebind_ts": int(now) - 60}}}
        daemon._warp_line_update("WARP-WG-4", ban_hold_until=now - 1,
                                 ban_hold_home="n-old", ban_hold_node="n-la",
                                 ban_window=[(now - 60, True)], restart_ts=now - 30)
        daemon._rehome_held_lines(now)
        st = daemon._WARP_LINE_STATE["WARP-WG-4"]
        assert st["ban_hold_home"] is None, "已还原的线必须收尾，否则每分钟重来"
        assert not st["ban_window"], "收尾同样要清窗口，不然会重新锁一天"
        assert daemon._rehome_calls == [], "不该再写一次配置"
        assert daemon._restarts == [], "不该再重启一次"
    finally:
        _rehome_restore(orig)

def test_restore_rewrites_when_home_but_never_restarted():
    """配置写回了、进程却没重启过 → 仍要写并重启（不重启等于没生效）。"""
    _reset()
    orig = _rehome_env()
    now = time.time()
    try:
        daemon._ledger["WARP-WG-4"] = "n-old"
        daemon.warp_wg._read_state = lambda: {
            "lines": {"WARP-WG-4": {"node": "n-old", "rebind_ts": int(now) - 10}}}
        daemon._warp_line_update("WARP-WG-4", ban_hold_until=now - 1,
                                 ban_hold_home="n-old", ban_hold_node="n-la",
                                 restart_ts=now - 300)
        daemon._rehome_held_lines(now)
        assert daemon._rehome_calls == [(4, "n-old")], "必须补写配置"
        assert daemon._restarts, "写回后必须重启，否则进程还在旧节点"
        assert daemon._WARP_LINE_STATE["WARP-WG-4"]["ban_hold_home"] is None
    finally:
        _rehome_restore(orig)

def test_restore_clears_window_so_it_does_not_relock():
    """还原后必须清 ban_window。

    不清的话，解封前那小时攒的样本还在，08:00 之后再记一条就直接锁到下一个
    08:00 —— 「锁一天」退化成「永久锁定」（已复现：31 条 → 1 条 → 重锁）。
    """
    _reset()
    orig = _rehome_env()
    now = time.time()
    try:
        for i in range(31):
            daemon._channel_ban_note("WARP-WG-4", True, now - 1800 + i * 60)
        st = daemon._WARP_LINE_STATE["WARP-WG-4"]
        assert st["ban_hold_until"] > now, "31 条应锁上"
        st["ban_hold_home"] = "n-old"
        st["ban_hold_node"] = "n-la"
        st["ban_hold_until"] = now - 1
        daemon._rehome_held_lines(now)
        assert not daemon._WARP_LINE_STATE["WARP-WG-4"]["ban_window"], "还原后窗口必须清空"
        daemon._channel_ban_note("WARP-WG-4", True, now + 60)
        assert daemon._WARP_LINE_STATE["WARP-WG-4"]["ban_hold_until"] <= now + 60, \
            "一条新样本不该把整条线重新锁一天"
    finally:
        _rehome_restore(orig)

def test_non_wg_line_does_not_consume_target():
    """非 WG 线不参与重定位，也不该白占候选节点。"""
    _reset()
    orig = _rehome_env()
    try:
        member_orig = daemon._warp_member
        daemon._warp_member = lambda t: (
            {"tag": t, "kind": "h2", "index": 1, "service": "x.service"}
            if t == "WARP-H2-ORD-9" else
            {"tag": t, "kind": "wg", "index": 4, "family": "v6",
             "service": "warp-wg-4.service"})
        now = time.time()
        daemon._warp_line_update("WARP-H2-ORD-9", ban_hold_until=now + 3600)
        n = len(daemon._rehome_calls)
        daemon._rehome_held_lines(now)
        assert len(daemon._rehome_calls) == n, "H2 线不该写配置"
        assert "n-la" not in daemon._ledger.values()
        daemon._warp_member = member_orig
    finally:
        _rehome_restore(orig)

def test_tunnel_dead_restart_bypasses_hold():
    """拿不到出口地址是隧道故障，渠道锁不该拦。"""
    _reset()
    W["restart_max_per_minute"] = 0
    W["restart_max_per_hour_per_line"] = 0
    tag = "WARP-WG-5"
    now = time.time()
    daemon._warp_line_update(tag, ban_hold_until=now + 3600, next_restart_ts=0.0)
    member = {"tag": tag, "port": 11998, "service": "warp-wg-5.service"}
    calls = []
    orig = (daemon.active_warp_tag, daemon._restart_warp_line)
    daemon.active_warp_tag = lambda: "WARP-CF"
    daemon._restart_warp_line = lambda *a, **k: calls.append(a) or True
    try:
        assert daemon._warp_line_restart(member, "模型层 quota") is False
        assert calls == [], "普通 quota 重启仍应被渠道锁拦下"
        daemon._warp_line_restart(member, "连续 N 次取不到出口地址",
                                  tunnel_dead=True)
        assert calls, "隧道故障的重启不该被渠道锁拦"
    finally:
        daemon.active_warp_tag, daemon._restart_warp_line = orig


def test_malformed_state_does_not_crash_loader():
    """盘上写坏一条 ban_window / 时间戳，不能在启动时把进程打崩。

    这两处都在 `_load_warp_line_state` 里，而那里的 try 只包住 open/json.load。
    崩了 → systemd 10s 后拉起 → 同一个文件再崩，死循环。
    """
    _reset()
    now = time.time()
    import json
    path = os.environ["WARP_LINE_STATE_FILE_PATH"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "lines": {"WARP-WG-4": {
            "ban_window": [None, [1, 2, 3], 1720000000, [now - 10, True]],
            "ban_hold_until": "2026-11-03T08:00:00",
            "ban_hold_ready": "-",
            "ban_hold_home": {"x": 1},
            "restart_ts": None}}}, f)
    daemon._WARP_LINE_STATE.clear()
    daemon._load_warp_line_state()
    st = daemon._WARP_LINE_STATE["WARP-WG-4"]
    assert st["ban_hold_until"] == 0.0 and st["ban_hold_home"] is None
    assert len(st["ban_window"]) == 1, f"畸形样本应被丢弃，实际 {st['ban_window']}"
    assert daemon._channel_ban_label(now) == "无", "健康行也不能因此抛异常"


def test_health_pool_accepts_a_full_subscription():
    """首轮健康检查一次性提交全部节点，不能被队列深度截断。

    节点数随订阅增长（2026-11-03 实测 122 个），而队列原先按 worker 数推导
    （60×2=120）—— 启动那一秒固定丢 2 个，节点延迟表就缺两条。
    """
    n = 160
    assert daemon._health_pool._sem._value >= n, (
        f"健康池队列深度 {daemon._health_pool._sem._value} < 节点数 {n}，"
        "首轮会丢任务")
    got = []
    lock = __import__("threading").Lock()

    def fake(node):
        time.sleep(0.001)
        with lock:
            got.append(node)

    futs = [daemon._health_pool.submit(fake, f"n{i}") for i in range(n)]
    assert all(f is not None for f in futs), "有任务被丢弃"
    for f in futs:
        f.result(timeout=30)
    assert len(got) == n, f"只跑了 {len(got)}/{n} 个"


def test_quota_verdict_does_not_accumulate_fail_streak():
    """`fail_streak` 只统计「拿不到出口地址」，不是「地址被封」。

    它唯一的消费点是 `_probe_warp_line` 里「连续 N 次取不到出口地址 → 隧道
    半死 → 重启」的判据。quota/blocked 说明隧道完全正常、只是这个地址被限流，
    累加进去会让健康线路被反复重启，日志还写「连续 2457 次取不到出口地址」
    —— 实测 WARP-WG-17 就是这样，与事实相反。
    """
    _reset()
    tag = "WARP-WG-17"
    now = time.time()

    # 连续 30 次 quota：隧道每次都拿到了出口地址，不该被算成「取不到地址」
    for i in range(30):
        daemon._warp_line_record_observation(
            tag, f"2a09:bac1:76c0:3d0::{i:x}", "quota", now + i, now + i)
    st = daemon._WARP_LINE_STATE[tag]
    assert st["fail_streak"] == 0, (
        f"quota 不该累加 fail_streak，实际 {st['fail_streak']}")
    assert st["ip"], "quota 结论下出口地址仍要记下来（隧道是通的）"

    # unknown 才累加：这才是「拿不到出口地址」
    for i in range(3):
        daemon._warp_line_record_observation(tag, None, "unknown", now + 100 + i, now + 100 + i)
    assert daemon._WARP_LINE_STATE[tag]["fail_streak"] == 3, "unknown 必须累加"

    # 任何一次 OK 都要清零
    daemon._warp_line_record_observation(
        tag, "2a09:bac1:76c0:3d0::ff", "OK", now + 200, now + 200)
    assert daemon._WARP_LINE_STATE[tag]["fail_streak"] == 0, "OK 必须清零"


def test_prober_skips_members_whose_unit_is_not_running():
    """members 是声明，不等于在跑。

    实测 usque-warp-c/g/h/i 是 disabled（设备让给了 H2），但 members 里仍留着。
    prober 照样每秒探，每次探不到出口，`fail_streak` 累积到 22 万，日志刷
    「连续 223495 次取不到出口地址」，把真故障埋掉。
    """
    _reset()
    daemon._UNIT_RUNNING_CACHE.clear()
    orig = daemon.subprocess.run

    class _R:
        def __init__(self, out): self.stdout = out

    def fake_run(cmd, **kw):
        svc = cmd[-1]
        return _R("inactive\n" if svc in ("usque-warp-c.service",
                                          "usque-warp-g.service") else "active\n")

    daemon.subprocess.run = fake_run
    try:
        now = time.time()
        assert not daemon._warp_member_unit_running(
            {"tag": "WARP-CF-C", "service": "usque-warp-c.service"}, now)
        assert daemon._warp_member_unit_running(
            {"tag": "WARP-CF", "service": "usque-warp.service"}, now)
        # 缓存生效：第二次不再调 systemctl
        calls = []
        daemon.subprocess.run = lambda cmd, **kw: calls.append(cmd) or _R("active\n")
        daemon._warp_member_unit_running(
            {"tag": "WARP-CF-C", "service": "usque-warp-c.service"}, now + 1)
        assert not calls, "60s 内应走缓存，不重复调 systemctl"
    finally:
        daemon.subprocess.run = orig
        daemon._UNIT_RUNNING_CACHE.clear()


# ---- 自愈：主配置丢了 WARP 出口要能被发现 ----
def test_warp_selector_missing_detects_lost_outbounds():
    """2026-11-03 远端事故：`warp-outbound.json` 属主被 root 抢走后，
    `parse_subscription.py` 生成了一份**没有 WARP 出口**的合法配置并上线。
    探活（Clash API 通 + 7890 能连）全绿，`singbox=🟢`，而 opencode 流量
    已经静默落到机场池 —— 3 小时 24 分、30362 次 `status=404`。

    这条判据必须同时满足：selector 整个消失要算缺失（404），成员骤降也算，
    但 API 自己不可达/5xx 时**不算**（那是另一条判据，不能在这里重复报）。
    """
    orig = daemon.singbox_api
    try:
        cases = [
            ("selector 不存在", (404, None), True),
            ("正常", (200, {"all": ["WARP-CF"] * 50}), False),
            ("成员骤降", (200, {"all": ["WARP-CF"] * 3}), True),
            ("selector 空", (200, {"all": []}), True),
            ("API 不可达", (0, None), False),
            ("API 5xx", (500, None), False),
        ]
        for name, resp, want in cases:
            daemon.singbox_api = lambda p, **kw: resp
            got = daemon._warp_selector_missing()
            assert got == want, f"{name}: got={got} want={want}"
    finally:
        daemon.singbox_api = orig


def test_parse_refuses_to_drop_warp_on_read_error():
    """warp-outbound.json 存在但读不了时必须让解析失败，不能静默产出
    「没有 WARP 出口」的配置。这是上面那场事故的第一道闸。"""
    import parse_subscription
    src = open("parse_subscription.py", encoding="utf-8").read()
    assert "拒绝生成不含 WARP 的配置" in src, "读取失败必须 raise，不能只 print"
    # 缺文件仍要放行：全新部署还没生成过，是合法形态
    assert "except FileNotFoundError:" in src, "缺文件必须仍返回 []"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {name}: {e}")
        finally:
            _cleanup_globals()
    print(f"\n{'all passed' if not fails else str(fails) + ' failed'}")
    sys.exit(1 if fails else 0)
