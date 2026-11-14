"""②组待改策略 · 记录现状（P4/阶段 3a 改后更新预期）。

【对象】处女 IP 偏好（_warp_ip_rank 四档）、24h 复用守卫（ip_reuse_guard_sec）、
预热换址（_prewarm_standby_warp）——「不攒量」三机制合谋（D3/D4 收敛）。
【三分法】B 待改策略：先懂原作用（防什么事故）再动；本组**只记录现状**，
策略翻转（均匀消耗/守卫重审/预热纳入）落地后由 P4 更新预期，不作回归基线。
【原作用】
  - 四档排序：0 未出现（必然没被 opencode 见过）> 1 探针刚 OK（此刻能服务）
    > 2 撑过生产且过惩罚期（可复用≠废址）> 3 出现过无成功（兜底）——
    把「处女 IP」优先喂给流量，防烧过的地址反复回池撞 429。
  - 24h 复用守卫：24h 内用过的地址必须先换会话 IP 才能再入——opencode
    对同出口 IP 的记忆很长（实测 10.6 分钟复用即爆发）。
  - 预热换址：闲置备线定期换新 IP——不预热则换线时只有一条新鲜线，
    流量在两条线间往返（config.py prewarm 注释）。
【注意】右栏保真语义（惩罚期先筛、满期降档、/64 热段同档压后、档 1 压档 2）
见 test_fidelity_* 相关断言与本组边界注释；权重本身（0 档最优）是待改对象。
【取值】guard/窗口取 config 实配（ip_reuse_guard_sec=86400 等）。
"""
import time

import pytest

import daemon as d


def test_rank_four_tiers_current_behavior():
    """记录现状：四档排序与「越新越优先」（P4 若改权重/均匀消耗，更新本预期）。"""
    now = time.time()
    w = d._warp()
    window = float(w["ip_reuse_guard_sec"])
    # 档 0：从未出现
    assert d._warp_ip_rank("2001:db8::v0", now, window)[0] == 0
    # 档 1：探针刚 OK，越新越优先（排序值=距上次 OK 时长）
    d._mark_warp_ip_used("2001:db8::v1a", now - 5.0)
    d._ledger_mark_probe_ok("2001:db8::v1a", now - 5.0)
    d._mark_warp_ip_used("2001:db8::v1b", now - 50.0)
    d._ledger_mark_probe_ok("2001:db8::v1b", now - 50.0)
    ra = d._warp_ip_rank("2001:db8::v1a", now, window)
    rb = d._warp_ip_rank("2001:db8::v1b", now, window)
    assert ra[0] == rb[0] == 1
    assert ra[2] < rb[2]  # 更新的探针 OK 排前
    # 档 2：撑过生产（last_ok_ts）
    d._mark_warp_ip_used("2001:db8::v2", now - 30.0)
    d._WARP_IP_USED["2001:db8::v2"]["last_ok_ts"] = now - 30.0
    assert d._warp_ip_rank("2001:db8::v2", now, window)[0] == 2
    # 档 3：出现过但无成功记录
    d._mark_warp_ip_used("2001:db8::v3", now - 30.0)
    assert d._warp_ip_rank("2001:db8::v3", now, window)[0] == 3
    # 档 1 压档 2（不得随权重调整丢弃的边界，此处先记录现状次序）
    assert d._warp_ip_rank("2001:db8::v1a", now, window)[0] < \
        d._warp_ip_rank("2001:db8::v2", now, window)[0]


def test_rank_penalty_addresses_pre_filtered_by_callers():
    """记录现状：惩罚期地址由调用方先筛（_ip_disqualified），不进 rank 比较。"""
    now = time.time()
    d._mark_warp_ip_used("2001:db8::p1", now - 5.0, blocked=True,
                         kind="rate429", ban_after=3600.0)
    assert d._ip_disqualified("2001:db8::p1", now,
                              float(d._warp()["ip_reuse_guard_sec"])) is True
    # 满期惩罚 ≠ 废址：满期后可复用（降档路径），disqualified 只在惩罚期内为真
    d._mark_warp_ip_used("2001:db8::p2", now - 7200.0, blocked=True,
                         kind="rate429")
    assert d._warp_ip_is_blocked("2001:db8::p2", now) is False  # 60s 窗早已过
    assert d._warp_ip_rank("2001:db8::p2", now,
                           float(d._warp()["ip_reuse_guard_sec"]))[0] == 3


def test_reuse_guard_24h_current_behavior():
    """记录现状：24h 复用守卫（ip_reuse_guard_sec）语义（P4 重审后更新）。"""
    now = time.time()
    guard = float(d._warp()["ip_reuse_guard_sec"])
    assert guard == 86400.0  # 现状实配
    d._mark_warp_ip_used("2001:db8::u1", now - 3600.0)
    d._WARP_IP_USED["2001:db8::u1"]["last_ok_ts"] = now - 3600.0
    d._mark_warp_ip_used("2001:db8::u2", now - 90000.0)
    d._WARP_IP_USED["2001:db8::u2"]["last_ok_ts"] = now - 90000.0
    # 原模式（IP 历史=真相）：窗口内成功过 → 不合格（必须先换会话 IP）
    orig = d._ip_history_is_truth
    try:
        d._ip_history_is_truth = lambda: True
        assert d._ip_disqualified("2001:db8::u1", now, guard) is True
        assert d._ip_disqualified("2001:db8::u2", now, guard) is False
        # 模型模式旁路复用窗，但惩罚期两模式都生效
        d._ip_history_is_truth = lambda: False
        assert d._ip_disqualified("2001:db8::u1", now, guard) is False
        d._mark_warp_ip_used("2001:db8::u3", now - 5.0, blocked=True,
                             kind="rate429", ban_after=3600.0)
        assert d._ip_disqualified("2001:db8::u3", now, guard) is True
    finally:
        d._ip_history_is_truth = orig


def test_prewarm_schedule_current_behavior():
    """记录现状：预热换址节流与批量（prewarm_sec / prewarm_max_per_round）。"""
    now = time.time()
    w = d._warp()
    assert float(w["prewarm_sec"]) == 120.0
    assert int(w["prewarm_max_per_round"]) == 4
    # 节流：窗口内重复调用直接返回
    d._last_warp_prewarm_ts = now - 1.0
    scheduled = []
    orig = d._schedule_idle_refresh
    d._schedule_idle_refresh = lambda m: scheduled.append(m["tag"])
    try:
        d._prewarm_standby_warp(now)
        assert scheduled == []
    finally:
        d._schedule_idle_refresh = orig
        d._last_warp_prewarm_ts = 0.0


def test_prewarm_traffic_window_matches_reuse_guard():
    """记录现状：预热的流量判据窗口（代码 86400 字面）与 ip_reuse_guard_sec 同值。

    P4 守卫重审若改 ip_reuse_guard_sec，此处字面量需同步（现状二者一致）。
    """
    import inspect
    src = inspect.getsource(d._prewarm_standby_warp)
    assert "now - 86400" in src
    assert float(d._warp()["ip_reuse_guard_sec"]) == 86400.0
