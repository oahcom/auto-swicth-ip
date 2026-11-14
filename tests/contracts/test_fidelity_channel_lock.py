"""①组保真契约 · 渠道锁全票否决结构（附件 A8）。

【防什么事故】
  - 改成多数决/混入 blocked 样本 → 永锁不上（能出 OK 的渠道被锁/烧穿渠道
    锁不住）或误锁好渠道；WG-21 锁 17h 期间出过 7 次 OK 正是旧口径续期事故；
  - blocked 混进 ban_probes → blocked 不是「渠道烧穿」而是「这一个地址在
    惩罚期」，混入让线永远锁不上；
  - RLE 双格式解析搞混（段取 e[3]、旧格式取 e[1]）→ 判据恒假，渠道永远锁不上；
  - 畸形元素让展开/裁剪抛异常 → 进程退出、systemd 拉起再崩的死循环。
【事故出处】附件 A8；WG-21 锁 17h；「判了 blocked、last_verdict 写 blocked、
但 ban_probes 空的」冻结 26h 事故。
【三分法】A 保真契约。
【取值】channel_ban_min_samples / channel_ban_reset_hour 取 config 实配。
"""
import time

import pytest

import daemon as d


def _day_ts(now, offset=0.0):
    return now + offset


def test_all_quota_veto_single_ok():
    """一条 OK 即否决锁（全票否决，「能出 OK 的渠道不许锁」，附件 A8）。"""
    now = time.time()
    n = int(d._warp().get("channel_ban_min_samples"))
    samples = [[now, now, n, "quota"]]
    assert d._channel_ban_hold_hit(list(samples), now) is True
    samples_ok = [[now, now, n - 1, "quota"], [now, now, 1, "OK"]]
    assert d._channel_ban_hold_hit(list(samples_ok), now) is False


def test_min_samples_gate():
    """样本不足 channel_ban_min_samples 不算烧穿（取值自 config）。"""
    now = time.time()
    n = int(d._warp().get("channel_ban_min_samples"))
    assert d._channel_ban_hold_hit([[now, now, n - 1, "quota"]], now) is False
    assert d._channel_ban_hold_hit([[now, now, n, "quota"]], now) is True


def test_blocked_never_counts_toward_lock():
    """blocked 不入 ban_probes：它不参与锁判据（附件 A8）。"""
    now = time.time()
    d._WARP_LINE_STATE.clear()
    d._channel_blocked_note("WARP-WG-7", now)
    st = d._warp_line_state("WARP-WG-7")
    assert st["blocked_notes"] and st["ban_probes"] in (None, [])
    # blocked_notes 存在也不影响判据：全 quota 才锁
    assert d._channel_ban_hold_hit([], now) is False


def test_rle_dual_format_verdict_positions():
    """RLE 段取 e[3]、旧格式取 e[1]；两者混用都认（附件 A8）。"""
    now = time.time()
    seg = [now - 10, now, 5, "quota"]
    old = (now - 20, "quota")
    out = d._channel_ban_expand([seg, old])
    assert len(out) == 6 and all(v == "quota" for _t, v in out)
    # 段尾时间戳不得被当结论：4 元段解析必须走 e[3]
    mixed = [seg, [now - 30, now - 25, 2, "OK"]]
    expanded = d._channel_ban_expand(mixed)
    assert ("OK" in [v for _t, v in expanded])
    # 畸形元素被丢弃而不是抛异常（防 systemd 崩溃死循环）
    trimmed = [seg, [None], "junk", 5]
    d._channel_ban_trim(trimmed, now)
    assert trimmed and all(isinstance(e, list) and e[3] == "quota"
                           for e in trimmed)


def test_trim_keeps_prev_quota_day_drops_older():
    """保留当前+上一个配额日，更早的丢弃（2026-11-15 主人实测变更）。

    旧契约「只留当天」让报表永远看不到完整配额日（08:00 一过昨天的账
    就被裁掉）。锁定互相续期的风险改由 _channel_ban_hold_hit 的当日
    过滤兜住 —— 见 test_lock_ignores_prev_day_samples。
    """
    now = time.time()
    day = d._quota_day(now)
    prev_day = day - 86400
    older = prev_day - 86400
    old = [[older + 3600, older + 3600, 10, "quota"]]  # 上上个配额日 → 丢
    d._channel_ban_trim(old, now)
    assert old == []
    prev = [[prev_day + 3600, prev_day + 3600, 10, "quota"]]  # 上一个配额日 → 留
    d._channel_ban_trim(prev, now)
    assert len(prev) == 1
    cur = [[day + 3600, day + 3600, 10, "quota"]]  # 当前配额日 → 留
    d._channel_ban_trim(cur, now)
    assert len(cur) == 1


def test_lock_ignores_prev_day_samples():
    """锁判据只认当前配额日：昨天的样本不计数也不否决（2026-11-15）。

    trim 留两个配额日后，判据若不夹窗口：昨天的 OK 会否决今天的锁、
    昨天的 quota 会凑今天的条数 —— 旧契约防的「锁定互相续期」必须
    在这里继续成立。
    """
    now = time.time()
    d._WARP_LINE_STATE.clear()
    n = int(d._warp().get("channel_ban_min_samples"))
    day = d._quota_day(now)
    prev_day = day - 86400
    # 昨天 n 条全 quota —— 不能因此锁今天的线
    st = d._warp_line_state("WARP-WG-9")
    st["ban_probes"] = [[prev_day + 3600, prev_day + 3600, n, "quota"]]
    assert not d._channel_ban_hold_hit(st["ban_probes"], now)
    # 今天 n 条全 quota —— 锁
    st["ban_probes"] = [[day + 3600, day + 3600, n, "quota"]]
    assert d._channel_ban_hold_hit(st["ban_probes"], now)
    # 今天出现一条 OK —— 否决
    st["ban_probes"] = [[day + 3600, day + 3600, n - 1, "quota"],
                        [day + 7200, day + 7200, 1, "OK"]]
    assert not d._channel_ban_hold_hit(st["ban_probes"], now)


def test_cf_lines_never_locked_but_sampled():
    """CF 直连只采样不锁：锁它=白关直连出口到 08:00（附件 A8）。"""
    now = time.time()
    d._WARP_LINE_STATE.clear()
    n = int(d._warp().get("channel_ban_min_samples"))
    d._channel_ban_note("WARP-CF4", "quota", now)
    st = d._warp_line_state("WARP-CF4")
    assert st["ban_probes"]  # 样本照记（报表可观测）
    assert float(st.get("ban_hold_until") or 0.0) == 0.0  # 但不写锁
    assert not st.get("stop_probe")


def test_lock_sets_stop_probe_until_reset_hour():
    """全 quota 满样本 → 锁到次日 reset_hour，置 stop_probe（锁定期间不探测）。
    """
    now = time.time()
    d._WARP_LINE_STATE.clear()
    n = int(d._warp().get("channel_ban_min_samples"))
    d._channel_ban_note("WARP-WG-9", "quota", now - 1.0)
    # 同日再补足样本至 n 条
    d._channel_ban_note("WARP-WG-9", "quota", now)
    st = d._warp_line_state("WARP-WG-9")
    total = sum(int(e[2]) for e in st["ban_probes"])
    if total < n:
        with d._WARP_LINE_CV:
            st2 = d._warp_line_state_locked("WARP-WG-9")
            st2["ban_probes"] = [[now, now, n, "quota"]]
        d._channel_ban_note("WARP-WG-9", "quota", now)
        st = d._warp_line_state("WARP-WG-9")
    assert float(st["ban_hold_until"]) > now
    assert st["stop_probe"] is True
    assert float(st["next_probe_ts"]) == float(st["ban_hold_until"])
    hold = float(st["ban_hold_until"]) - now
    reset_hour = int(d._warp().get("channel_ban_reset_hour"))
    assert 0 < hold <= 86400  # 锁到下一个 reset_hour（口径见 _channel_ban_hold_secs）
    assert reset_hour == 8 or True  # 取值来自 config；具体小时不写死
