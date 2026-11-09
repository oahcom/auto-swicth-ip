#!/usr/bin/env python3
"""渠道 ban 率锁自检（离线、无网络、无 pytest）。

规格（2026-11-04 主人定，替换 11-02 的旧口径）：
  - 维度是**渠道**（线路 tag），不是 colo；
  - 判据 A：当天最近 `channel_ban_min_samples`(1000) 条模型层实测**全部** quota；
    出现任何一条非 quota（OK/flagged/err）就否决 —— 能出 OK 的渠道不许锁；
  - 判据 B：连续 `channel_noaddr_hold_sec`(5h) 探测不到出口地址；
  - 命中后锁到**次日** `channel_ban_reset_hour`(08:00)，期间不重启、**不探测**；
  - 锁到期清样本；样本/锁定跨 daemon 重启保留，非当天样本在启动时裁掉。

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
         "restart_max_per_hour_line": W.get("restart_max_per_hour_per_line"),
         "backfill_open": daemon._backfill_open,
         "backfill_close": daemon._backfill_close,
         "backfill_rows": daemon._backfill_rows,
         "backfill_reconcile": daemon._backfill_reconcile,
         "channel_rehome_target": daemon._channel_rehome_target,
         "blocks_load_map": daemon.warp_blocks.load_map}

def _cleanup_globals():
    daemon._submit_once = _ORIG["submit_once"]
    W["restart_max_per_minute"] = _ORIG["restart_max_per_minute"]
    W["restart_max_per_hour_line"] = _ORIG["restart_max_per_hour_line"]
    daemon._backfill_open = _ORIG["backfill_open"]
    daemon._backfill_close = _ORIG["backfill_close"]
    daemon._backfill_rows = _ORIG["backfill_rows"]
    daemon._backfill_reconcile = _ORIG["backfill_reconcile"]
    daemon._channel_rehome_target = _ORIG["channel_rehome_target"]

if pytest is not None:
    @pytest.fixture(autouse=True)
    def _guard():
        yield
        _cleanup_globals()

def _reset():
    W["channel_ban_hold_enabled"] = True
    W["channel_ban_min_samples"] = 1000    # 当天最近 1000 条实测
    W["channel_noaddr_hold_sec"] = 5 * 3600
    W["channel_ban_reset_hour"] = 8
    daemon._WARP_LINE_STATE.clear()
    daemon._NIGHT_LOG_TS.clear()
    # `_channel_ban_note` 命中时会 `_submit_once("channel-rehome", ...)` 起一个
    # **后台线程**。测试里不拦它，它就会挑下一个测试跑在半路时才落地，用那个
    # 测试的 stub 写台账 —— 表现为「跑 5 次有 1 次红」。改成同步执行：本文件
    # 要测的是锁/还原逻辑本身，不是那个线程模型。
    daemon._submit_once = _sync_submit_once
    # 补位渠道会起 systemd 单元、写 `.warp/backfill.json`、重跑 members.py
    # 刷 `.warp/warp-outbound.json` —— 全是**真实副作用**（实测：漏拦一次就
    # 把线上 50 条 WARP 出口写成 29 条）。默认全部拦掉；补位自己的用例在
    # `_backfill_env()` 里换成内存替身。
    daemon._backfill_open = lambda tag, now, used: None
    daemon._backfill_close = lambda tag: False
    daemon._backfill_rows = lambda: []
    # 对账会 `_unit_ctl("stop", ...)` 真停线上单元、重发配置 —— 必须拦掉。
    daemon._backfill_reconcile = lambda now: None

# ---- 时间算术 ----
def _noon_today() -> float:
    """当天正午的本地时刻。

    样本判据按 `time.localtime(ts)[:3] == 当天` 裁，用例却要回溯半小时来构造
    「当天样本」—— 直接用 `time.time()` 时，**00:00~00:30 跑测试**回溯的一半
    样本落在昨天，被 `_channel_ban_trim` 裁掉，用例随机变红（实测 11-05 00:02
    两个用例同时红）。取正午：回溯小时级仍在当天内，且远离 08:00 解封边界。
    """
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))

def test_hold_secs_until_next_8am():
    _reset()
    # 2026-11-02 16:30 本地 → 次日 08:00 = 15.5h
    t = time.mktime((2026, 11, 2, 16, 30, 0, 0, 0, -1))
    assert abs(daemon._channel_ban_hold_secs(t) - 15.5 * 3600) < 1, "应锁到次日 8 点"
    # 07:30 → 当天 08:00，只有 30min
    t = time.mktime((2026, 11, 3, 7, 30, 0, 0, 0, -1))
    assert abs(daemon._channel_ban_hold_secs(t) - 1800) < 1, "8 点前应只锁 30 分钟"

# ---- 样本与命中 ----
def test_trim_keeps_today_only():
    _reset()
    now = time.mktime((2026, 11, 4, 12, 0, 0, 0, 0, -1))
    samples = [(now - 60, "quota"), (now - 86400, "quota"), ("bad", "quota")]
    kept = daemon._channel_ban_trim(samples, now)
    assert len(kept) == 1, f"隔天样本与畸形元素都要丢，实际 {kept}"
    assert kept[0][:3] == [now - 60, now - 60, 1], "裁剪要就地生效且压成一段"
    assert kept[0][3] == "quota"

def test_trim_merges_consecutive_same_verdict():
    """同结论相邻样本压成一段 —— 这是状态文件不爆的唯一原因。

    实测 2026-11-04：19k 条样本 526 KB，压缩后 484 段 14 KB（39x）。
    判据只看「是否全部 quota」，条数照留；丢的是重复的时间戳。
    """
    _reset()
    now = time.mktime((2026, 11, 4, 12, 0, 0, 0, 0, -1))
    samples = [(now - 10 + i, "quota") for i in range(10)]
    kept = daemon._channel_ban_trim(samples, now)
    assert len(kept) == 1, f"10 条同结论应压成 1 段，实际 {kept}"
    assert kept[0] == [now - 10, now - 1, 10, "quota"]
    # 结论一变就开新段，不能跨结论合并
    samples.append((now, "OK"))
    daemon._channel_ban_trim(samples, now)
    assert len(samples) == 2 and samples[1][3] == "OK"

def test_trim_keeps_all_samples_no_cap():
    """**不再按条数截断** —— 主人 2026-11-04 要求「ban_probes 得留多点」。

    旧实现拿 `channel_ban_min_samples`(1000) 当上限，超过就丢最旧的；那是把
    「最小样本数」误当容量常量。条数全留，靠 RLE 压体积。
    """
    _reset()
    W["channel_ban_min_samples"] = 5
    now = time.mktime((2026, 11, 4, 12, 0, 0, 0, 0, -1))
    samples = [(now - 9 + i, "quota") for i in range(10)]
    daemon._channel_ban_trim(samples, now)
    total = sum(int(e[2]) for e in samples if len(e) >= 4)
    assert total == 10, f"10 条样本必须全留，实际 {total}: {samples}"

def test_expand_round_trips_runs():
    """展开/压缩互为逆运算 —— 报表按点读，daemon 按段存。"""
    _reset()
    now = time.mktime((2026, 11, 4, 12, 0, 0, 0, 0, -1))
    samples = [(now - 3, "quota"), (now - 2, "quota"), (now - 1, "OK")]
    daemon._channel_ban_trim(samples, now)
    expanded = daemon._channel_ban_expand(samples)
    assert len(expanded) == 3
    assert [v for _t, v in expanded] == ["quota", "quota", "OK"]

def test_need_full_1000_quota_to_lock():
    """不足 1000 条不锁 —— 当天还没被打够，不是烧穿。"""
    _reset()
    W["channel_ban_min_samples"] = 5
    now = 1_000_000.0
    full = [(now - i, "quota") for i in range(5)]
    assert daemon._channel_ban_hold_hit(full, now) is True, "5 条 quota 应命中"
    # 注意 `full[:4]` 不行：`_channel_ban_hold_hit` 会**就地**把 full 压成一段，
    # 再切片拿到的是同一段（条数仍是 5）。要独立构造才能测「少一条」。
    fewer = [(now - i, "quota") for i in range(4)]
    assert daemon._channel_ban_hold_hit(fewer, now) is False, "少一条不该锁"

def test_any_ok_breaks_quota_run():
    """一条 OK 就否决 —— 能出 OK 的渠道不许锁（旧口径在这里锁了 WG-21 一整天）。"""
    _reset()
    W["channel_ban_min_samples"] = 5
    now = 1_000_000.0
    samples = [(now - i, "quota") for i in range(5)]
    assert daemon._channel_ban_hold_hit(samples, now) is True
    samples.append((now - 60, "OK"))
    assert daemon._channel_ban_hold_hit(samples, now) is False, "有一次 OK 就不算烧穿"

def test_flagged_also_breaks_quota_run():
    """判据是「全部 quota」，flagged/err 都不算 quota。"""
    _reset()
    W["channel_ban_min_samples"] = 3
    now = 1_000_000.0
    samples = [(now - 3, "quota"), (now - 2, "flagged"), (now - 1, "quota")]
    assert daemon._channel_ban_hold_hit(samples, now) is False

# ---- 记账 → 锁定 ----
def test_note_locks_channel():
    _reset()
    W["channel_ban_min_samples"] = 30
    now = _noon_today()
    for i in range(31):
        daemon._channel_ban_note("WARP-WG-3", "quota", now - 1800 + i * 60)
    st = daemon._WARP_LINE_STATE["WARP-WG-3"]
    assert daemon._channel_ban_held("WARP-WG-3", now) is True, "31 条全 quota 应锁"
    assert st["stop_probe"] is True, "锁定后不再探测"
    assert abs(st["ban_hold_until"] - (now + daemon._channel_ban_hold_secs(now) - 1)) < 2

def test_low_sample_channel_not_locked():
    """当天只有几条实测的 100% 不该锁 —— 那是「还没被打够」。"""
    _reset()
    W["channel_ban_min_samples"] = 30
    now = _noon_today()
    for i in range(6):
        daemon._channel_ban_note("WARP-WG-12", "quota", now - 300 + i)
    st = daemon._WARP_LINE_STATE["WARP-WG-12"]
    assert daemon._channel_ban_held("WARP-WG-12", now) is False, "6 条样本不足以判烧穿"
    assert not st["stop_probe"], "没锁就不该停探"

def test_note_ok_prevents_lock():
    """当天出现过一次 OK 就永不满足「全部 quota」，哪怕 quota 条数已经过 30。

    旧口径在窗口期外还有「锁定期内继续探、OK 不解锁」的漏洞：WG-21 锁在
    11-03 08:00 后出过 7 次 OK，锁一直留到次日。新口径把 OK 留在当天样本里，
    永久否决当天再锁。
    """
    _reset()
    W["channel_ban_min_samples"] = 30
    now = _noon_today()
    for i in range(15):
        daemon._channel_ban_note("WARP-WG-9", "quota", now - 600 + i)
    daemon._channel_ban_note("WARP-WG-9", "OK", now - 500)
    for i in range(16):
        daemon._channel_ban_note("WARP-WG-9", "quota", now - 400 + i)
    assert daemon._channel_ban_held("WARP-WG-9", now) is False, "夹一条 OK 不该锁"

# ---- 连续拿不到地址 ----
def test_noaddr_locks_after_5h():
    _reset()
    now = time.time()
    t0 = now - 5 * 3600 - 1
    daemon._channel_noaddr_note("WARP-H2-NRT-0", False, t0)
    daemon._channel_noaddr_note("WARP-H2-NRT-0", False, now)
    assert daemon._channel_ban_held("WARP-H2-NRT-0", now) is True, "连续 5h 无地址应锁"
    assert daemon._WARP_LINE_STATE["WARP-H2-NRT-0"]["stop_probe"] is True, \
        "无地址锁也要停探，否则这条线每轮都白探一次"


def test_expiry_sweep_does_not_release_active_holds():
    """还在锁定期内的线（stop_probe=True 且 until 在未来）不能被当成过期放出来。

    释放条件写成「stop_probe 或 已过期」就会把整池正在锁的线立刻放出来 ——
    而那正是锁在干活的时候。
    """
    _reset()
    orig = _rehome_env()
    now = time.time()
    try:
        daemon._warp_line_update("WARP-WG-7", ban_hold_until=now + 7200,
                                 stop_probe=True,
                                 ban_probes=[(now - 60, "quota")])
        daemon._rehome_held_lines(now)
        st = daemon._WARP_LINE_STATE["WARP-WG-7"]
        assert st["stop_probe"] is True, "锁定期内必须保持停探"
        assert st["ban_hold_until"] > now, "锁不能被提前释放"
        assert len(st["ban_probes"]) == 1, "锁定期内样本保留（供排查用）"
    finally:
        _rehome_restore(orig)


def test_expiry_sweep_releases_legacy_lock_without_stop_probe():
    """旧版本留下的锁没有 stop_probe 字段，到期也必须释放。

    只认 `stop_probe and until<=now` 的话，那批存量锁到期后样本不清，
    下一条实测进来就立刻重锁 —— 正是 WG-21 那个死循环。
    """
    _reset()
    orig = _rehome_env()
    now = time.time()
    try:
        daemon._warp_line_update("WARP-CF-B", ban_hold_until=now - 1,
                                 ban_probes=[(now - 60, "quota")])
        daemon._WARP_LINE_STATE["WARP-CF-B"]["stop_probe"] = False
        daemon._rehome_held_lines(now)
        st = daemon._WARP_LINE_STATE["WARP-CF-B"]
        assert st["ban_probes"] == [], "旧锁到期也要清样本"
        assert st["ban_hold_until"] == 0.0
    finally:
        _rehome_restore(orig)


def test_expiry_sweep_keeps_samples_of_unlocked_lines():
    """从没锁过的线（ban_hold_until=0）不能被到期清理每分钟抹一次样本。

    清理判据少写 `ban_hold_until > 0` 时，所有线每分钟都被清空 —— 1000 条
    样本永远攒不满，锁形同废除。
    """
    _reset()
    orig = _rehome_env()
    now = time.time()
    try:
        daemon._warp_line_update("WARP-WG-9", ban_hold_until=0.0,
                                 ban_probes=[(now - 60, "quota")])
        daemon._rehome_held_lines(now)
        st = daemon._WARP_LINE_STATE["WARP-WG-9"]
        assert len(st["ban_probes"]) == 1, "没锁过的线样本不能被清"
        assert st["stop_probe"] is False
    finally:
        _rehome_restore(orig)


def test_switch_off_releases_stop_probe_without_home():
    """关总闸必须立刻释放所有线，包括没有 home 的 CF/H2。

    只在 `_channel_ban_note` 里释放是死锁：那条分支要探测才执行，而
    `stop_probe` 正挡着探测；CF/H2 又走不到还原分支 → 永久不探测。
    """
    _reset()
    orig = _rehome_env()
    now = time.time()
    try:
        daemon._warp_line_update("WARP-CF-E", ban_hold_until=now + 7200,
                                 stop_probe=True,
                                 ban_probes=[(now - 60, "quota")],
                                 ban_noaddr_since=now - 6 * 3600)
        W["channel_ban_hold_enabled"] = False
        daemon._rehome_held_lines(now)
        st = daemon._WARP_LINE_STATE["WARP-CF-E"]
        assert st["stop_probe"] is False, "关闸后必须恢复探测"
        assert st["ban_hold_until"] == 0.0
        assert st["ban_probes"] == []
        assert st["ban_noaddr_since"] == 0.0
    finally:
        W["channel_ban_hold_enabled"] = True
        _rehome_restore(orig)


def test_backfill_line_is_never_channel_locked():
    """补位渠道不能被「连续无出口地址」锁掉。

    它的存在意义就是顶替一条被封的线；锁住它等于让被顶的那条线整夜没人补位。
    实测 2026-11-04：补位线 WG-23/24/25 被锁到 `ban_hold_until` 约 57 年后，
    当天三条 H2 线（SLC-11/12、NRT-13）整夜没有补位。`_channel_ban_note` 的
    quota 分支早有这道跳过，`_channel_noaddr_note` 漏了。
    """
    _reset()
    W["channel_noaddr_hold_sec"] = 60
    orig = daemon._warp_member
    now = time.time()
    daemon._warp_member = lambda t: (
        {"tag": t, "kind": "wg", "index": 23, "backfill_for": "WARP-H2-SLC-11"}
        if t == "WARP-WG-23" else {"tag": t, "kind": "wg", "index": 4})
    try:
        daemon._warp_line_update("WARP-WG-23", ban_noaddr_since=now - 3600)
        daemon._channel_noaddr_note("WARP-WG-23", False, now)
        st = daemon._WARP_LINE_STATE["WARP-WG-23"]
        assert st["ban_hold_until"] == 0.0, \
            f"补位线不该被锁，实际 ban_hold_until={st['ban_hold_until']}"
        assert not st["stop_probe"], "补位线要一直探测"
        # 普通线照旧：超过阈值就该锁
        daemon._warp_line_update("WARP-WG-4", ban_noaddr_since=now - 3600)
        daemon._channel_noaddr_note("WARP-WG-4", False, now)
        assert daemon._WARP_LINE_STATE["WARP-WG-4"]["ban_hold_until"] > now, \
            "普通线仍要按无地址计时锁定"
    finally:
        daemon._warp_member = orig


def test_expiry_sweep_releases_hold_without_home():
    """CF/H2 线没有 home，到期也必须解锁（旧实现 `continue` 掉，锁永久续期）。"""
    _reset()
    orig = _rehome_env()
    now = time.time()
    try:
        daemon._warp_line_update("WARP-CF-D", ban_hold_until=now - 1,
                                 stop_probe=True, ban_noaddr_since=now - 9 * 3600,
                                 ban_probes=[(now - 60, "quota")])
        daemon._rehome_held_lines(now)
        st = daemon._WARP_LINE_STATE["WARP-CF-D"]
        assert st["stop_probe"] is False, "到期必须恢复探测"
        assert st["ban_probes"] == [], "到期必须清样本，否则立刻重锁"
        assert st["ban_noaddr_since"] == 0.0, "无地址计时也要重置"
        assert st["ban_hold_until"] == 0.0, "时间戳归零，避免下一分钟再清一次"
    finally:
        _rehome_restore(orig)

def test_noaddr_resets_on_any_ip():
    _reset()
    now = time.time()
    daemon._channel_noaddr_note("WARP-H2-NRT-1", False, now - 6 * 3600)
    daemon._channel_noaddr_note("WARP-H2-NRT-1", True, now - 3600)   # 拿到过地址
    daemon._channel_noaddr_note("WARP-H2-NRT-1", False, now)
    assert daemon._channel_ban_held("WARP-H2-NRT-1", now) is False, "中途拿到过地址要重新计时"

# ---- 锁定期间不探测 ----
def test_locked_line_not_due_for_probe():
    _reset()
    now = time.time()
    daemon._warp_line_update("WARP-WG-5", ban_hold_until=now + 3600, stop_probe=True)
    due, _ = daemon._warp_line_needs_probe("WARP-WG-5", now)
    assert due is False, "锁定期间不参与探测排期"

def test_unlocked_line_due_again_after_expiry():
    _reset()
    now = time.time()
    daemon._warp_line_update("WARP-WG-6", ban_hold_until=now - 1, stop_probe=True)
    daemon._rehome_held_lines(now)
    st = daemon._WARP_LINE_STATE["WARP-WG-6"]
    assert st["stop_probe"] is False, "到期必须重新参与探测"
    assert st["ban_hold_until"] == 0.0 or st["ban_hold_until"] < now

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
    W["channel_ban_min_samples"] = 2
    now = _noon_today()
    tag = "WARP-WG-8"
    daemon._warp_line_update(tag, ban_hold_until=now - 86400)  # 昨天的锁
    for i in range(3):
        daemon._channel_ban_note(tag, "quota", now - 300 + i)
    assert daemon._channel_ban_held(tag, now) is True, "过期锁之后应能重新锁上"

def test_save_and_reload_keeps_hold_and_trims_window():
    _reset()
    now = time.time()
    tag = "WARP-WG-2"
    daemon._warp_line_update(
        tag, ban_hold_until=now + 7200, stop_probe=True,
        ban_probes=[(now - 60, "quota"), (now - 86400, "quota")],
        ip="2a09:bac1:3b00:10::16:181", last_ok_ip="2a09:bac1:3b00:10::16:181")
    daemon._save_warp_line_state(force=True)
    daemon._WARP_LINE_STATE.clear()
    daemon._load_warp_line_state()
    st = daemon._WARP_LINE_STATE[tag]
    assert st["ban_hold_until"] > now, "渠道锁必须跨重启保留"
    assert st["stop_probe"] is True, "锁定期间不探测也要跨重启保留"
    assert len(st["ban_probes"]) == 1, "隔天样本应在启动时裁掉"

def test_disabled_switch_is_inert():
    _reset()
    W["channel_ban_hold_enabled"] = False
    now = 1_000_000.0
    daemon._channel_ban_note("WARP-WG-7", "quota", now - 10)
    daemon._channel_ban_note("WARP-WG-7", "quota", now)
    assert daemon._channel_ban_held("WARP-WG-7", now) is False
    assert daemon._channel_ban_label(now) == "关"

def test_label_counts_held_channels():
    _reset()
    now = time.time()
    # 补位台账是本机真实文件（`.warp/backfill.json`）：不隔离的话，跑测试的
    # 机器上真有一条补位渠道时这条断言会因为多出「·N补位」而假失败。
    orig = daemon._backfill_rows
    daemon._backfill_rows = lambda: []
    try:
        daemon._warp_line_update("WARP-WG-1", ban_hold_until=now + 600,
                                 ban_hold_ready=now)
        daemon._warp_line_update("WARP-WG-3", ban_hold_until=now + 600,
                                 ban_hold_ready=now)
        assert daemon._channel_ban_label(now) == "2线→08:00"
        # 有一条配置写了但重启没成功 → 必须能看出来，否则「换了」只是台账上的说法
        daemon._warp_line_update("WARP-WG-3", ban_hold_ready=0.0)
        assert daemon._channel_ban_label(now) == "2线→08:00·1待重启"
        # 有补位渠道在顶班时也要能看出来（这是主人最关心的那条状态）
        daemon._backfill_rows = lambda: [{"tag": "WARP-WG-7"}]
        assert daemon._channel_ban_label(now) == "2线→08:00·1待重启·1补位"
    finally:
        daemon._backfill_rows = orig

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
            daemon._warp_line_restart, daemon._warp_member,
            daemon._backfill_open, daemon._backfill_close, daemon._backfill_rows)
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
    # 承载能力排序读的是块表。指成空表：本文件测的是 ban 率选点，不该由
    # 生产此刻的 `.warp/block-map.json` 决定结论（全 rank 1 = 稳定排序）。
    daemon.warp_blocks.load_map = lambda: {}
    daemon._line_node = lambda tag: daemon._ledger.get(tag)
    daemon._warp_member = lambda t: {"tag": t, "kind": "wg", "index": 4,
                                     "family": "v6", "service": "warp-wg-4.service"}
    daemon._warp_line_restart = lambda m, r, **k: (
        daemon._restarts.append((m["tag"], r, k)), daemon.restart_ok)[1]
    # 重定位测试不关心补位：补位要起 systemd 单元、刷订阅（秒级真副作用），
    # 在这里换成空实现；补位本身另有专门用例。
    daemon._backfill_open = lambda tag, now, used: None
    daemon._backfill_close = lambda tag: False
    daemon._backfill_rows = lambda: []
    return orig

def _rehome_restore(orig):
    (daemon.warp_wg.udp_capable, daemon.warp_wg.load_nodes,
     daemon.warp_wg.write_line, daemon.warp_wg._read_state,
     daemon.warp_wg.write_json, daemon.warp_wg.state_path,
     daemon.warp_wg.load_colo_map, daemon._line_node,
     daemon._warp_line_restart, daemon._warp_member,
     daemon._backfill_open, daemon._backfill_close, daemon._backfill_rows) = orig
    daemon.warp_blocks.load_map = _ORIG["blocks_load_map"]

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

def test_rehome_target_skips_nodes_that_cannot_carry_warp():
    """ban 率最低的 colo 里也要先挑实测能承载 WARP 的节点。

    实测 2026-11-04：WG-1 被锁到 `cn.bing.com:443~13-any`（wg=0/4）后连接
    拿不到出口，`ban_hold_ready` 永远写不进去 —— 每分钟重定位一次，近 30 分钟
    119 次重定位重启里它占 71 次。选点必须先过承载能力再谈 ban 率。
    """
    _reset()
    W["channel_rehome_min_samples"] = 10
    orig = _rehome_env()
    orig_map = daemon.warp_blocks.load_map
    import ipv6_segments
    orig_rates = ipv6_segments.colo_ban_rates
    # 同一 colo 两个候选：dead 块序在前、实测不通；live 在后、实测可用。
    # 两个都得在 UDP 池里 —— `_channel_rehome_target` 按池过滤候选。
    daemon.warp_wg.load_nodes = lambda: [{"tag": "n-dead"}, {"tag": "n-live"}]
    daemon.warp_wg.load_colo_map = lambda: {
        "n-dead": {"colo": "LAX", "ok": True}, "n-live": {"colo": "LAX", "ok": True}}
    daemon.warp_blocks.load_map = lambda: {
        "n-dead": {"colo": "LAX", "wg_ok": 0, "wg_n": 4},
        "n-live": {"colo": "LAX", "wg_ok": 2, "wg_n": 2}}
    ipv6_segments.colo_ban_rates = lambda win, now: {"LAX": (50, 5)}
    try:
        got = daemon._channel_rehome_target(1_000_000.0)
        assert got == ("LAX", "n-live"), \
            f"应跳过实测不通的 n-dead 选 n-live，实际 {got}"
        # 全块都没测过（rank 1）时不能因此返回 None：新节点还没跑过 diag，
        # 过滤掉等于人工刚加的节点永远上不了线。
        daemon.warp_blocks.load_map = lambda: {}
        got = daemon._channel_rehome_target(1_000_000.0)
        assert got is not None, "未知承载能力的节点必须仍可被选中"
    finally:
        daemon.warp_blocks.load_map = orig_map
        ipv6_segments.colo_ban_rates = orig_rates
        _rehome_restore(orig)

def test_held_line_on_dead_node_switches_instead_of_restarting_forever():
    """锁定节点实测承载不了 WARP 时，改投别的节点而不是每分钟重启它。"""
    _reset()
    W["channel_rehome_min_samples"] = 10
    orig = _rehome_env()
    orig_map = daemon.warp_blocks.load_map
    import ipv6_segments
    orig_rates = ipv6_segments.colo_ban_rates
    ipv6_segments.colo_ban_rates = lambda win, now: {"SIN": (50, 5)}
    daemon.warp_blocks.load_map = lambda: {
        "cn.bing.com:443~13-any": {"colo": "HKG", "wg_ok": 0, "wg_n": 4}}
    now = time.time()
    tag = "WARP-WG-4"
    try:
        daemon._warp_line_update(tag, ban_hold_until=now + 3600,
                                 ban_hold_node="cn.bing.com:443~13-any",
                                 ban_hold_home="n-origin", ban_hold_ready=0.0)
        daemon._line_node = lambda t: "cn.bing.com:443~13-any"
        daemon._rehome_held_lines(now)
        # 改投后**允许且必须**重启一次：`_rehome_one` 只写配置文件，不重启
        # 进程就还在旧节点上跑。要挡的是「每分钟重启一次、永不收敛」。
        assert len(daemon._restarts) == 1, \
            f"改投后应只重启一次，实际 {daemon._restarts}"
        assert daemon._WARP_LINE_STATE[tag]["ban_hold_node"] == "n-sin", \
            f"应改投到实测可用的节点，实际 {daemon._WARP_LINE_STATE[tag]}"
        # 改投并重启成功后 `ban_hold_ready` 置位，下一分钟不该再重启。
        assert daemon._WARP_LINE_STATE[tag]["ban_hold_ready"] > 0, "改投重启后要记 ready"
        daemon._line_node = lambda t: "n-sin"
        daemon._rehome_held_lines(now + 60)
        assert len(daemon._restarts) == 1, \
            f"ready 之后不该再重启，实际 {daemon._restarts}"
    finally:
        daemon.warp_blocks.load_map = orig_map
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

def test_rehome_uses_declared_family_not_stale_state():
    """写配置的族取成员声明值（`WARP_WG_FAMILIES`），不取状态表。

    状态表里的 `family` 只在 `warp_wg.apply()` 时写一次，之后既不重建也不随配置
    变 —— 改过拓扑的机器上它是**旧世代残留**。实测本机 `.env` 是
    `WARP_WG_FAMILIES=v6`（22 节点 × 1 渠道，全 v6），状态表里 WG-1/2/3 却还记着
    上一代 `v4,v6,v6,v6` 的 v4；照状态表重写会把三条线全写成 `prefer_ipv4`，
    而 v4 出口只由 colo 决定 ⇒ 实测三条线挤成同一个 `104.28.205.117`
    （既定事实第 27 条：v4 渠道数不应超过 colo 数）。
    """
    _reset()
    orig = _rehome_env()
    fams = []
    daemon.warp_wg._read_state = lambda: {"lines": {"WARP-WG-4": {"family": "v4"}}}
    daemon.warp_wg.write_line = lambda i, n, family=None: fams.append(family)
    try:
        assert daemon._rehome_one("WARP-WG-4", "n-sin") == "ok"
        assert fams == ["v6"], f"应取声明值 v6，不取状态表的 v4，实际 {fams}"
    finally:
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
                                 ban_probes=[(now - 60, "quota")],
                                 ban_hold_home="n-old", ban_hold_node="n-la")
        daemon._ledger["WARP-WG-4"] = "n-la"
        W["channel_ban_hold_enabled"] = False
        daemon._channel_ban_note("WARP-WG-4", "quota", now)
        st = daemon._WARP_LINE_STATE["WARP-WG-4"]
        assert daemon._channel_ban_held("WARP-WG-4", now) is False, "开关关了不该还锁着"
        assert not st["ban_probes"]
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
                                 ban_probes=[(now - 60, "quota")], restart_ts=now - 30)
        daemon._rehome_held_lines(now)
        st = daemon._WARP_LINE_STATE["WARP-WG-4"]
        assert st["ban_hold_home"] is None, "已还原的线必须收尾，否则每分钟重来"
        assert not st["ban_probes"], "收尾同样要清窗口，不然会重新锁一天"
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

def test_restore_clears_samples_so_it_does_not_relock():
    """到期还原必须清当天样本并恢复探测。

    不清的话，解封前的 quota 样本还在，再记一条就直接锁到下一个 08:00 ——
    「锁一天」退化成「永久锁定」（实测 WG-21 08:00:19 就是这么重锁的）。
    """
    _reset()
    W["channel_ban_min_samples"] = 30
    orig = _rehome_env()
    now = _noon_today()
    try:
        for i in range(31):
            daemon._channel_ban_note("WARP-WG-4", "quota", now - 1800 + i * 60)
        st = daemon._WARP_LINE_STATE["WARP-WG-4"]
        assert st["ban_hold_until"] > now, "31 条应锁上"
        st["ban_hold_home"] = "n-old"
        st["ban_hold_node"] = "n-la"
        st["ban_hold_until"] = now - 1
        daemon._rehome_held_lines(now)
        assert not daemon._WARP_LINE_STATE["WARP-WG-4"]["ban_probes"], "还原后样本必须清空"
        assert daemon._WARP_LINE_STATE["WARP-WG-4"]["stop_probe"] is False, "还原后必须恢复探测"
        daemon._channel_ban_note("WARP-WG-4", "quota", now + 60)
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
    """盘上写坏一条 ban_probes / 时间戳，不能在启动时把进程打崩。

    这两处都在 `_load_warp_line_state` 里，而那里的 try 只包住 open/json.load。
    崩了 → systemd 10s 后拉起 → 同一个文件再崩，死循环。
    """
    _reset()
    now = time.time()
    import json
    path = os.environ["WARP_LINE_STATE_FILE_PATH"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "lines": {"WARP-WG-4": {
            "ban_probes": [None, [1, 2, 3], 1720000000, [now - 10, "quota"], [now - 5, None]],
            "ban_hold_until": "2026-11-03T08:00:00",
            "ban_hold_ready": "-",
            "ban_hold_home": {"x": 1},
            "restart_ts": None}}}, f)
    daemon._WARP_LINE_STATE.clear()
    daemon._load_warp_line_state()
    st = daemon._WARP_LINE_STATE["WARP-WG-4"]
    assert st["ban_hold_until"] == 0.0 and st["ban_hold_home"] is None
    assert len(st["ban_probes"]) == 2, f"畸形样本应被丢弃，实际 {st['ban_probes']}"
    # 落盘形状是段 `[起, 止, 条数, 结论]`，结论在 e[3]。
    assert [e[3] for e in st["ban_probes"]] == ["quota", "None"], \
        "结论统一转成字符串（盘上写 null 也不能让 all() 抛异常）"
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
def test_rebuild_counts_as_failure_when_selector_still_missing():
    """重建「成功」但 selector 仍缺失 → 必须按失败计，否则退避永远停在 60s。

    重建可能全程返回 0、产出也带 WARP，但 sing-box 起不来（端口被占、
    二进制坏了）。若按命令返回码判成功，`_WARP_REBUILD_FAILS` 每轮清零，
    退避恒为 60s —— 每轮「成功」、每轮修不好。
    """
    orig_rebuild, orig_missing, orig_fails = (
        daemon._rebuild_warp_outbounds, daemon._warp_selector_missing,
        daemon._WARP_REBUILD_FAILS)
    try:
        daemon._WARP_REBUILD_FAILS = 0
        daemon._rebuild_warp_outbounds = lambda: True
        # 命令成功、但运行中的配置仍缺 WARP
        daemon._warp_selector_missing = lambda: True
        assert daemon._rebuild_warp_outbounds_watched() is False
        assert daemon._WARP_REBUILD_FAILS == 1, "仍缺失却记成成功"
        # 连续三次失败 → 退避必须已离开 60s 档（索引 3 → 480s）。
        # 若判据写错成「命令成功即成功」，这里会一直停在 60s。
        for _ in range(2):
            daemon._rebuild_warp_outbounds_watched()
        assert daemon._WARP_REBUILD_FAILS == 3
        wait = daemon._WARP_REBUILD_BACKOFF[
            min(daemon._WARP_REBUILD_FAILS, len(daemon._WARP_REBUILD_BACKOFF) - 1)]
        assert wait == 480, f"退避没有增长: fails={daemon._WARP_REBUILD_FAILS} wait={wait}"
        # 真的修好了就清零
        daemon._warp_selector_missing = lambda: False
        assert daemon._rebuild_warp_outbounds_watched() is True
        assert daemon._WARP_REBUILD_FAILS == 0
    finally:
        daemon._rebuild_warp_outbounds = orig_rebuild
        daemon._warp_selector_missing = orig_missing
        daemon._WARP_REBUILD_FAILS = orig_fails


def test_watchdog_does_not_burn_backoff_when_submit_skipped():
    """同名任务在跑时 `_submit_once` 返回 False，此时不得推进重建时间戳。

    先记时间戳再提交，会让这次尝试白记 —— 重建要几分钟，而 watchdog
    每 30s 一轮，那段空白里日志只会刷「仍缺失」，下一次还得等满退避周期。
    """
    orig_submit, orig_ts = daemon._submit_once, daemon._LAST_WARP_REBUILD_TS
    orig_probe = daemon._probe_singbox_with_retry
    orig_missing = daemon._warp_selector_missing
    try:
        daemon._probe_singbox_with_retry = lambda: True
        daemon._warp_selector_missing = lambda: True
        daemon._LAST_WARP_REBUILD_TS = 0          # 退避已过，该提交了
        daemon._submit_once = lambda *a, **k: False   # 同名任务在跑
        daemon._singbox_watchdog()
        assert daemon._LAST_WARP_REBUILD_TS == 0, "没提交却推进了时间戳"
        daemon._submit_once = lambda *a, **k: True
        daemon._singbox_watchdog()
        assert daemon._LAST_WARP_REBUILD_TS > 0, "提交成功却没记时间戳"
    finally:
        daemon._submit_once = orig_submit
        daemon._LAST_WARP_REBUILD_TS = orig_ts
        daemon._probe_singbox_with_retry = orig_probe
        daemon._warp_selector_missing = orig_missing


def test_warp_selector_missing_detects_lost_outbounds():
    """2026-11-03 远端事故：`warp-outbound.json` 属主被 root 抢走后，
    `parse_subscription.py` 生成了一份**没有 WARP 出口**的合法配置并上线。
    探活（Clash API 通 + 7890 能连）全绿，`singbox=🟢`，而 opencode 流量
    已经静默落到机场池 —— 3 小时 24 分、30362 次 `status=404`。

    这条判据必须同时满足：selector 整个消失要算缺失（404），成员骤降也算，
    但 API 自己不可达/5xx 时**不算**（那是另一条判据，不能在这里重复报）。
    """
    orig = daemon.singbox_api
    orig_warp = daemon._warp
    # 阈值是「selector 成员数 < 全部成员数 // 4」，所以要显式给一份像生产那样
    # 几十条的成员表。不钉死的话这个用例会随生产此刻有几条线变红变绿
    # （实测：隔离台账后只剩配置声明的 9 条，`3 < 9//4` 不成立，用例转红）。
    daemon._warp = lambda: {"selector": "opencode-out",
                            "members": [{"tag": f"WARP-CF-{i}"} for i in range(50)]}
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
        daemon._warp = orig_warp


# ---- 补位渠道 ----
def _backfill_env():
    """补位路径的内存替身：起单元/发布/刷订阅都不许真的发生。"""
    daemon._bf_log = []
    daemon._bf_units = []
    daemon._bf_published = []
    daemon._bf_switch = []
    daemon._bf_active = ["WARP-WG-4"]
    daemon._bf_spare = [7]
    daemon._bf_file = "/tmp/asip-test-backfill-%d.json" % os.getpid()
    daemon._bf_rows = []
    orig = (daemon._BACKFILL_FILE, daemon._unit_ctl, daemon._wait_port_ready,
            daemon.warp_wg.refresh_main_outbounds, daemon.refresh_subscription,
            daemon._switch_warp, daemon.active_warp_tag,
            daemon._backfill_spare_indices, daemon._rehome_one,
            daemon._channel_rehome_target, daemon._atomic_write_json,
            daemon._warp_member, daemon._warp, daemon.log, daemon._backfill_rows,
            daemon._backfill_reconcile)
    daemon._BACKFILL_FILE = daemon._bf_file
    daemon._unit_ctl = lambda action, unit: (
        daemon._bf_units.append((action, unit)),
        type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())[1]
    daemon._wait_port_ready = lambda port, t: True
    daemon.warp_wg.refresh_main_outbounds = lambda: daemon._bf_published.append("outbound")
    daemon.refresh_subscription = lambda reason="": (
        daemon._bf_published.append(reason), True)[1]
    daemon._switch_warp = lambda tag: (daemon._bf_switch.append(tag), True)[1]
    daemon.active_warp_tag = lambda: daemon._bf_active[0]
    daemon._backfill_spare_indices = lambda: list(daemon._bf_spare)
    daemon._rehome_one = lambda tag, node: "ok"
    daemon._channel_rehome_target = lambda now, exclude=None, ignore_enabled=False: (
        "LAX", "n-la")
    daemon._atomic_write_json = lambda path, payload: (
        daemon._bf_rows.clear(), daemon._bf_rows.extend(payload))
    daemon._warp_member = lambda t: next(
        (m for m in daemon._bf_members if m["tag"] == t), None)
    daemon._warp = lambda: {"members": daemon._bf_members,
                            "wg": {"base_port": 1110},
                            "ready_timeout_sec": 1,
                            "channel_ban_reset_hour": 8}
    daemon.log = lambda msg: daemon._bf_log.append(msg)
    daemon._bf_members = [{"tag": "WARP-WG-4", "kind": "wg", "index": 4,
                           "family": "v6", "service": "warp-wg-4.service"}]
    daemon._BACKFILL_TRY_TS.clear()
    # `_reset()` 已经把 `_backfill_*` 换成空实现，`orig` 里记的是那几个空壳。
    # 本文件要测的就是真实现，从 import 时的快照取回来。
    daemon._backfill_open = _ORIG["backfill_open"]
    daemon._backfill_close = _ORIG["backfill_close"]
    daemon._backfill_reconcile = _ORIG["backfill_reconcile"]
    daemon._backfill_rows = lambda: list(daemon._bf_rows)
    return orig

def _backfill_restore(orig):
    (daemon._BACKFILL_FILE, daemon._unit_ctl, daemon._wait_port_ready,
     daemon.warp_wg.refresh_main_outbounds, daemon.refresh_subscription,
     daemon._switch_warp, daemon.active_warp_tag,
     daemon._backfill_spare_indices, daemon._rehome_one,
     daemon._channel_rehome_target, daemon._atomic_write_json,
     daemon._warp_member, daemon._warp, daemon.log, daemon._backfill_rows,
     daemon._backfill_reconcile) = orig
    daemon._channel_rehome_target = _ORIG["channel_rehome_target"]
    daemon._BACKFILL_TRY_TS.clear()

def test_backfill_open_brings_up_replica_in_lowest_ban_colo():
    """渠道被封 → 必须另起一条渠道补位，落在 ban 率最低的 colo 上。"""
    _reset()
    orig = _backfill_env()
    try:
        btag = daemon._backfill_open("WARP-WG-4", 1_000_000.0, set())
        assert btag == "WARP-WG-7", f"应用空闲设备线 7 补位，实际 {btag}"
        assert ("start", "warp-wg-7.service") in daemon._bf_units, \
            f"补位单元必须被拉起，实际 {daemon._bf_units}"
        assert any("WARP-WG-7" in str(r) for r in daemon._bf_published), \
            f"补位渠道必须发布出去，实际 {daemon._bf_published}"
        assert daemon._bf_rows and daemon._bf_rows[0]["tag"] == "WARP-WG-7", \
            f"补位台账必须落盘，实际 {daemon._bf_rows}"
        st = daemon._warp_line_state("WARP-WG-4")
        assert st["ban_backfill_tag"] == "WARP-WG-7", st
        assert st["ban_backfill_node"] == "n-la", st
        # 被封的线正在服务流量 → 必须切到补位渠道
        assert daemon._bf_switch == ["WARP-WG-7"], daemon._bf_switch
    finally:
        _backfill_restore(orig)

def test_backfill_skipped_when_unit_will_not_start():
    """第一条空闲线起不来就换下一条；一条都起不来就不发布死端口。"""
    _reset()
    orig = _backfill_env()
    daemon._bf_spare = [7, 8]
    def ctl(action, unit):
        if unit == "warp-wg-7.service":
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "masked"})()
        daemon._bf_units.append((action, unit))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    daemon._unit_ctl = ctl
    try:
        btag = daemon._backfill_open("WARP-WG-4", 1_000_000.0, set())
        assert btag == "WARP-WG-8", f"7 起不来应退到 8，实际 {btag}"
        assert daemon._bf_rows[0]["tag"] == "WARP-WG-8", daemon._bf_rows
    finally:
        _backfill_restore(orig)

def test_backfill_never_nests():
    """补位渠道自己也被封时不再开新线 —— 否则一条封禁能无限拉线。"""
    _reset()
    orig = _backfill_env()
    try:
        daemon._bf_members.append({"tag": "WARP-WG-7", "kind": "wg", "index": 7,
                                   "family": "v6", "service": "warp-wg-7.service",
                                   "backfill_for": "WARP-WG-4"})
        assert daemon._backfill_open("WARP-WG-7", 1_000_000.0, set()) is None
        assert not daemon._bf_units, f"不该起任何单元，实际 {daemon._bf_units}"
    finally:
        _backfill_restore(orig)

def test_backfill_closed_at_8am_restores_original_arrangement():
    """08:00 解封：撤补位成员、停单元、selector 切回原线。"""
    _reset()
    orig = _backfill_env()
    try:
        daemon._bf_members.append({"tag": "WARP-WG-7", "kind": "wg", "index": 7,
                                   "family": "v6", "service": "warp-wg-7.service",
                                   "backfill_for": "WARP-WG-4"})
        daemon._bf_rows.append({"tag": "WARP-WG-7", "port": 1132,
                                "service": "warp-wg-7.service", "kind": "wg",
                                "index": 7, "backfill_for": "WARP-WG-4"})
        daemon._bf_active = ["WARP-WG-7"]      # 补位渠道正在服务流量
        daemon._warp_line_update("WARP-WG-4", ban_backfill_tag="WARP-WG-7",
                                 ban_backfill_node="n-la",
                                 ban_hold_until=1_000_000.0 - 10)
        # 锁已到期（until <= now）
        daemon._rehome_held_lines(1_000_000.0)
        assert daemon._bf_switch == ["WARP-WG-4"], \
            f"selector 必须先切回原线，实际 {daemon._bf_switch}"
        assert ("stop", "warp-wg-7.service") in daemon._bf_units, daemon._bf_units
        assert not [m for m in daemon._bf_members if m["tag"] == "WARP-WG-7"], \
            f"补位成员必须撤掉，实际 {daemon._bf_members}"
        assert not daemon._bf_rows, f"补位台账必须清空，实际 {daemon._bf_rows}"
        st = daemon._warp_line_state("WARP-WG-4")
        assert st["ban_backfill_tag"] is None, st
        assert st["ban_hold_until"] == 0.0, st
    finally:
        _backfill_restore(orig)

def test_backfill_open_runs_from_rehome_pass_when_held():
    """锁定中的渠道每轮由 `_rehome_held_lines` 补位，不依赖探测线程。"""
    _reset()
    orig = _backfill_env()
    try:
        daemon._warp_line_update("WARP-WG-4", ban_hold_until=2_000_000.0)
        daemon._rehome_held_lines(1_000_000.0)
        assert daemon._warp_line_state("WARP-WG-4")["ban_backfill_tag"] == "WARP-WG-7"
    finally:
        _backfill_restore(orig)

def test_backfill_node_excluded_from_next_rehome_target():
    """补位占着的节点不能再分给别的线：补位线不在台账里，只能靠锁线字段。"""
    _reset()
    orig = _backfill_env()
    w = (daemon.warp_wg.line_nodes, daemon.warp_wg.udp_capable,
         daemon.warp_wg.load_nodes, daemon.warp_wg.load_colo_map,
         daemon._lowest_ban_colos, daemon._backfill_rows)
    try:
        daemon.warp_wg.line_nodes = lambda: {}
        daemon.warp_wg.udp_capable = lambda nodes: [{"tag": "n-la"}, {"tag": "n-nrt"}]
        daemon.warp_wg.load_nodes = lambda: []
        daemon.warp_wg.load_colo_map = lambda: {
            "n-la": {"colo": "LAX"}, "n-nrt": {"colo": "NRT"}}
        daemon._lowest_ban_colos = lambda now: ["LAX", "NRT"]
        daemon._backfill_rows = lambda: []      # 台账行里没有 node 字段
        daemon._channel_rehome_target = _ORIG["channel_rehome_target"]
        daemon._warp_line_update("WARP-WG-4", ban_backfill_node="n-la")
        target = daemon._channel_rehome_target(1_000_000.0, set())
        assert target == ("NRT", "n-nrt"), f"应跳过补位占用的 n-la，实际 {target}"
    finally:
        (daemon.warp_wg.line_nodes, daemon.warp_wg.udp_capable,
         daemon.warp_wg.load_nodes, daemon.warp_wg.load_colo_map,
         daemon._lowest_ban_colos, daemon._backfill_rows) = w
        _backfill_restore(orig)

def test_backfill_target_ignores_rehome_switch():
    """补位是硬要求：`channel_rehome_enabled=False` 时仍要选出目标。"""
    _reset()
    orig = _backfill_env()
    w = (daemon.warp_wg.line_nodes, daemon.warp_wg.udp_capable,
         daemon.warp_wg.load_nodes, daemon.warp_wg.load_colo_map,
         daemon._lowest_ban_colos)
    try:
        daemon.warp_wg.line_nodes = lambda: {}
        daemon.warp_wg.udp_capable = lambda nodes: [{"tag": "n-la"}]
        daemon.warp_wg.load_nodes = lambda: []
        daemon.warp_wg.load_colo_map = lambda: {"n-la": {"colo": "LAX"}}
        daemon._lowest_ban_colos = lambda now: ["LAX"]
        daemon._channel_rehome_target = _ORIG["channel_rehome_target"]
        wf = daemon._warp
        daemon._warp = lambda: {**wf(), "channel_rehome_enabled": False}
        assert daemon._channel_rehome_target(1_000_000.0, set()) is None, \
            "重定位总闸关闭时不该选目标"
        assert daemon._channel_rehome_target(
            1_000_000.0, set(), ignore_enabled=True) == ("LAX", "n-la"), \
            "补位必须无视重定位总闸"
    finally:
        (daemon.warp_wg.line_nodes, daemon.warp_wg.udp_capable,
         daemon.warp_wg.load_nodes, daemon.warp_wg.load_colo_map,
         daemon._lowest_ban_colos) = w
        _backfill_restore(orig)

def test_backfill_tag_recorded_even_if_publish_raises():
    """发布半途抛异常也必须记 `ban_backfill_tag`，否则下轮再开一条、旧的撤不掉。"""
    _reset()
    orig = _backfill_env()
    try:
        def boom(path, payload):
            raise OSError("disk full")
        daemon._atomic_write_json = boom
        daemon._warp_line_update("WARP-WG-4", ban_hold_until=2_000_000.0)
        daemon._rehome_held_lines(1_000_000.0)
        assert daemon._warp_line_state("WARP-WG-4")["ban_backfill_tag"] == "WARP-WG-7"
        starts = [u for u in daemon._bf_units if u[0] == "start"]
        assert starts == [("start", "warp-wg-7.service")], \
            f"不能开出第二条补位，实际 {starts}"
    finally:
        _backfill_restore(orig)

def test_backfill_reconcile_removes_unowned_row():
    """台账行没有认领的锁线 → 撤单元、删行（解封半途异常留下的孤儿）。"""
    _reset()
    orig = _backfill_env()
    try:
        daemon._bf_members.append({"tag": "WARP-WG-7", "kind": "wg", "index": 7,
                                   "family": "v6", "service": "warp-wg-7.service",
                                   "backfill_for": "WARP-WG-4"})
        daemon._bf_rows.append({"tag": "WARP-WG-7", "port": 1132,
                                "service": "warp-wg-7.service", "kind": "wg",
                                "index": 7, "backfill_for": "WARP-WG-4"})
        daemon._backfill_reconcile(1_000_000.0)
        assert ("stop", "warp-wg-7.service") in daemon._bf_units, daemon._bf_units
        assert not daemon._bf_rows, f"孤儿行必须删掉，实际 {daemon._bf_rows}"
        assert not [m for m in daemon._bf_members if m["tag"] == "WARP-WG-7"]
    finally:
        _backfill_restore(orig)

def test_backfill_reconcile_reopens_when_row_lost():
    """锁线有 `ban_backfill_tag` 但台账行丢了 → 停残留单元、清字段、当轮重开。"""
    _reset()
    orig = _backfill_env()
    try:
        daemon._bf_members.append({"tag": "WARP-WG-7", "kind": "wg", "index": 7,
                                   "family": "v6", "service": "warp-wg-7.service",
                                   "backfill_for": "WARP-WG-4"})
        daemon._warp_line_update("WARP-WG-4", ban_hold_until=2_000_000.0,
                                 ban_backfill_tag="WARP-WG-7",
                                 ban_backfill_node="n-la")
        daemon._rehome_held_lines(1_000_000.0)
        st = daemon._warp_line_state("WARP-WG-4")
        assert st["ban_backfill_tag"] == "WARP-WG-7", \
            f"必须当轮重开补位，实际 {st}"
        assert len(daemon._bf_rows) == 1, f"台账应只有一行，实际 {daemon._bf_rows}"
        assert ("stop", "warp-wg-7.service") in daemon._bf_units, daemon._bf_units
    finally:
        _backfill_restore(orig)

def test_backfill_survives_restart_via_config():
    """daemon 重启后补位成员由 config.py 读台账恢复 —— 只在内存里的话，
    `deploy/members.py` 与 parse 都是独立进程，看不见它。"""
    src = open("config.py", encoding="utf-8").read()
    assert "_append_backfill_members" in src and "backfill.json" in src

def test_parse_refuses_to_drop_warp_on_read_error():
    """warp-outbound.json 存在但读不了时必须让解析失败，不能静默产出
    「没有 WARP 出口」的配置。这是上面那场事故的第一道闸。"""
    import parse_subscription
    src = open("parse_subscription.py", encoding="utf-8").read()
    assert "拒绝生成不含 WARP 的配置" in src, "读取失败必须 raise，不能只 print"
    # 缺文件仍要放行：全新部署还没生成过，是合法形态
    assert "except FileNotFoundError:" in src, "缺文件必须仍返回 []"


def test_scan_tag_orphan_is_dropped_once_unit_is_declared():
    """扫描兜底 tag 在单元被声明后就是孤儿，必须丢掉。

    2026-11-08 实测：usque-h2-31/33/34 三台单元先以 `WARP-H2-SCAN-*` 被探测
    （当时还没渲染声明），记了 OK；随后 channels/members 重建把它们收编为
    WARP-H2-SIN-31 / HKG-33 / HKG-34。旧 tag 的 OK 冻结在台账里 → `asip chan`
    一直显示幽灵 OK，而换线只认声明 tag（真 tag 是冷启动、无地址），
    全程没有任何日志解释「OK 却不切」。孤儿不是数据，是名字错了。
    """
    _reset()
    import json
    path = os.environ["WARP_LINE_STATE_FILE_PATH"]
    now = time.time()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "lines": {
            "WARP-H2-SCAN-31": {"ip": "2a09:bac5::1", "last_ok_ip": "2a09:bac5::1",
                                "last_ok_ts": now, "last_verdict": "OK"},
            "WARP-H2-SCAN-99": {"ip": "2a09:bac5::2", "last_ok_ip": "2a09:bac5::2",
                                "last_ok_ts": now, "last_verdict": "OK"}}}, f)
    orig = daemon._warp
    daemon._warp = lambda: {"members": [
        {"tag": "WARP-H2-SIN-31", "kind": "h2", "service": "usque-h2-31.service"}]}
    try:
        daemon._WARP_LINE_STATE.clear()
        daemon._load_warp_line_state()
    finally:
        daemon._warp = orig
    assert "WARP-H2-SCAN-31" not in daemon._WARP_LINE_STATE, \
        "已被声明收编的单元，其扫描兜底 tag 的结论必须丢弃"
    assert "WARP-H2-SCAN-99" in daemon._WARP_LINE_STATE, \
        "仍无声明归属的扫描单元照旧保留（它只有这一个名字）"


def test_cf_direct_records_samples_but_never_locks():
    """CF 直连只豁免「写锁」，不豁免采样。

    2026-11-09 的回归：早先 CF 分支在 _channel_ban_note 开头直接 return，
    ban_probes 从此不再增长，报表显示「无样本」，而 last_verdict 仍停在锁之前
    那次的 quota —— 两个字段互相矛盾，读起来就是「原因标错了」。
    """
    _reset()
    W["channel_ban_min_samples"] = 30
    now = _noon_today()
    for i in range(40):
        daemon._channel_ban_note("WARP-CF-B", "quota", now - 1800 + i)
    st = daemon._WARP_LINE_STATE["WARP-CF-B"]
    assert daemon._channel_ban_held("WARP-CF-B", now) is False, "CF 直连永不写锁"
    assert not st.get("stop_probe"), "CF 不该停探"
    total = sum(int(e[2]) for e in st.get("ban_probes") or [] if len(e) >= 4)
    assert total == 40, f"样本必须照记，实际 {total}"


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
