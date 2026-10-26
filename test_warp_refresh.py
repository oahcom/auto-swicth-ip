#!/usr/bin/env python3
"""WARP 线刷新守卫回归测试（无网络、无副作用）。

覆盖 2026-10-19 19:08 事故中失效的判据：
  1. 用「字节是否还在增长」判线忙闲，而非「连接数是否为 0」
  2. 被换下的线等不到空闲时必须补刷，而非单次超时即放弃
  3. 出口 IP 复用窗口须长于 opencode 的记忆

运行: python3 -m pytest test_warp_refresh.py -v
"""
import os
import sys
import time

import pytest

sys.path.insert(0, '.')

# 必须早于 import daemon —— 否则模块级已冻结点错文件
import config as C

C.CFG["log_file"] = "/dev/null"

import daemon


@pytest.fixture(autouse=True)
def _isolate_warp_state(monkeypatch, tmp_path):
    """_probe_warp_egress 会把探测到的 tag→IP 写进 _WARP_LINE_STATE。

    不隔离时别的用例留下的 IP 会让 _retry_refresh_idle_warp 误判「已知新鲜」
    而提前返回，四个补刷用例全挂（实测单独跑 22 passed、与 unit 合跑 4 个挂）。
    """
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    # 离渠台账是进程级全局：残留条目会让冷却闸门挡住后续用例的重启。
    monkeypatch.setattr(daemon, "_WARP_OFF_AGE", {})
    monkeypatch.setattr(daemon, "_WARP_REFRESH_COUNT", {})
    # 验收时刻同样要隔离：`_reclaim_dirty_pass` 在它之后 12s 内跳过该线，
    # 残留的真 time.time() 会让后续用例的脏线巡检静默少派一条。
    monkeypatch.setattr(daemon, "_WARP_LAST_ACCEPT", {})
    # 探测台账是进程级全局：不隔离则前一个用例探过的 IP 在本用例被当成
    # 「TTL 内已探过」而跳过真实探测，台账相关断言全落空。
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    # 整批探测结论也是进程级全局：不隔离会让上一个用例的 tag→verdict 参与
    # 本用例的失明判定（尤其「缓存跳过也要记结论」分支）。
    monkeypatch.setattr(daemon, "_PROBE_LAST_VERDICT", {})
    # 标脏墓碑同样是进程级全局：残留条目会拒收后续用例带 started_at 的写入。
    monkeypatch.setattr(daemon, "_PROBE_BLOCKED_AT", {})
    # 模型探测通过会记 last_ok_ts 并落盘，不隔离就写进生产 .warp_ip_used.json。
    monkeypatch.setenv("WARP_IP_USED_FILE_PATH", str(tmp_path / "warp_ip_used.json"))
    # 历史是只追加文件且永不清理：`_mark_warp_ip_used` 顺手写它，不隔离则桩地址
    # 永久留在生产 .warp_ip_history.jsonl（已实测写进 10 条假记录）。
    monkeypatch.setenv("WARP_IP_HISTORY_FILE_PATH", str(tmp_path / "warp_ip_history.jsonl"))
    # 落池时刻同样落盘：不隔离时任何走到 _fallback_to_proxy 的用例都会把「刚落池」
    # 写进生产目录，下一次跑就命中 3h 冷却、改走盲切分支 —— 断言看似通过，其实是
    # 上一次运行留下的状态在替它作答（实测同一目录连跑两次结论相反）。
    monkeypatch.setenv("WARP_PROXY_FALLBACK_FILE_PATH", str(tmp_path / "proxy_fallback.json"))
    monkeypatch.setattr(daemon, "_last_proxy_fallback_ts", 0.0)
    monkeypatch.setattr(daemon, "_proxy_fallback_ts_loaded", False)
    # 换线三件套是裸全局，`reconnect_warp` 直接赋值、不经 monkeypatch：
    # 不在这里兜住，本文件跑完后它们停在 time.time() 量级，随后导入
    # test_daemon_unit 的换线用例用假时钟 now=1000 就把「生效间隔硬闸」
    # (now - switch_ok < min_switch_gap_sec) 恒判成立，5 个用例静默全挂。
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 0.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0.0)
    # 换线成功会起一个 20s 后真调 systemctl 的补刷线程，monkeypatch 撤了它照跑。
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)


def test_offline_refresh_count_starts_when_leaving_and_resets_on_return(monkeypatch):
    """离渠计数语义：被换下才起算，切回（成为出口）即清零。"""
    monkeypatch.setattr(daemon, "_WARP_OFF_AGE", {})
    monkeypatch.setattr(daemon, "_WARP_REFRESH_COUNT", {})
    daemon._track_offline_refresh("W1", "W2")          # W1 离渠
    assert "W1" in daemon._WARP_OFF_AGE
    daemon._WARP_REFRESH_COUNT["W1"] = 3               # 期间重启 3 次
    assert "第 3 次刷" in daemon._warp_refresh_progress("W1")
    daemon._track_offline_refresh("W2", "W1")          # W1 切回
    assert "W1" not in daemon._WARP_OFF_AGE
    assert daemon._warp_refresh_progress("W1") == ""
    # 切到轮换池（new_tag=None）同样让被换下的线进入离渠态
    daemon._track_offline_refresh("W1", None)
    assert "W1" in daemon._WARP_OFF_AGE
    assert daemon._WARP_REFRESH_COUNT["W1"] == 0


def _ok_refresh_stubs(monkeypatch):
    """让 `_refresh_idle_warp` 走到「重启成功」的最小桩。"""
    monkeypatch.setattr(daemon, "_restart_service", lambda s, tag=None: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: True)
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: "ip-new")
    monkeypatch.setattr(daemon, "_ip_disqualified", lambda ip, now, guard: False)
    monkeypatch.setattr(daemon, "_warp_model_usable", lambda m, ip: True)


def test_standby_cooldown_blocks_restart_while_connections_alive(monkeypatch):
    """刚被换下且仍有连接 → 不得重启：重启即掐断在途 SSE。

    实测 646 次换线里 63.3% 的被切下线在 120s 内被重启（中位 73s，最快 0s）。
    """
    assert daemon.CFG["warp"]["standby_cooldown_sec"] == 120, \
        "冷却窗口是用户指定的 120s，改动需先有实测依据"
    monkeypatch.setattr(daemon, "_WARP_OFF_AGE", {"W1": time.time() - 30})
    monkeypatch.setattr(daemon, "_warp_line_stats", lambda tag: (1024, 1, 3))
    restarts = []
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda s, tag=None: (restarts.append(s), True)[1])

    assert daemon._refresh_idle_warp({"tag": "W1", "port": 1080,
                                      "service": "s"}) is None
    assert restarts == [], "线上还有连接时不得重启"


def test_standby_cooldown_skipped_when_line_has_no_connections(monkeypatch):
    """冷却期内但零连接 → 直接放行。

    一条连接都没有的线不存在会被掐断的请求，等满 120s 纯属空转。实测 17 条
    备线常驻仅 2 条挂连接，其余全是空壳。
    """
    monkeypatch.setattr(daemon, "_WARP_OFF_AGE", {"W1": time.time() - 10})
    monkeypatch.setattr(daemon, "_warp_line_stats", lambda tag: (0, 0, 0))
    _ok_refresh_stubs(monkeypatch)

    assert daemon._refresh_idle_warp({"tag": "W1", "port": 1080,
                                      "service": "s"}) == "ip-new"


def test_standby_cooldown_expiry_lifts_time_gate_only(monkeypatch):
    """过了 120s 时间闸门即放行，连接是否还活着由调用方的判闲决定。

    `_refresh_idle_warp` 自己不做空闲判定（它没有观察窗语义，只负责重启）：
    补刷路径先 `_wait_warp_idle`、脏线守护先 `warp_line_busy`，两处都会在
    连接还活着时挡下 —— 实测 WARP-CF-J 有 378s 的连接仍在传数据。
    """
    monkeypatch.setattr(daemon, "_WARP_OFF_AGE", {"W1": time.time() - 121})
    monkeypatch.setattr(daemon, "_warp_line_stats", lambda tag: (5_000_000, 2, 4))
    restarts = []
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda s, tag=None: (restarts.append(s), True)[1])
    _ok_refresh_stubs(monkeypatch)
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda s, tag=None: (restarts.append(s), True)[1])

    assert daemon._refresh_idle_warp({"tag": "W1", "port": 1080,
                                      "service": "s"}) == "ip-new"
    assert restarts == ["s"], "冷却已过，时间闸门不得再挡重启"


def test_standby_cooldown_ignores_never_active_line(monkeypatch):
    """从未当过出口的备线不受冷却约束 —— 它没有「离渠」时刻。

    备线预热本就该立刻生效，把冷却套到它头上等于池子停转 2 分钟。
    """
    monkeypatch.setattr(daemon, "_WARP_OFF_AGE", {})
    _ok_refresh_stubs(monkeypatch)

    assert daemon._refresh_idle_warp({"tag": "W1", "port": 1080,
                                      "service": "s"}) == "ip-new"


def test_zero_connection_line_is_idle_without_waiting(monkeypatch):
    """零连接的线立即判闲，不付观察窗。

    实测 17 条备线常驻只有 2 条挂连接；每条都等 12s 会把 5s 一轮的守护拖成
    60s+ 一轮，而出口 IP 存活中位只有 12.5s。
    """
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(daemon, "_warp_line_stats", lambda tag: (0, 0, 0))
    assert daemon.warp_line_busy("WARP-CF") is False
    assert slept == [], "零连接不该睡眠采样"


def test_quota_verdict_turns_line_into_refresh_candidate(monkeypatch):
    """探到 quota 的线必须被判为低质并进入预热候选，否则永不重启。

    实测故障：CF-J 缓存恒报「已探过: quota（跳过）」，但 75 分钟无一次重启 ——
    quota 结论只进台账不出台账，`_warp_ip_needs_refresh` 在模型模式下不认它。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    ip = "2a09:bac5:1ef9:281e::1"
    assert daemon._warp_ip_needs_refresh(ip, 1000.0) is False  # 无结论：全新地址不算低质
    daemon._mark_warp_ip_used(ip, 1000.0, blocked=True, kind="rate429",
                              ban_after=3600)
    daemon._probe_cache_put("W1", ip, "quota", now=1000.0)
    assert daemon._probe_cache_verdict("W1", ip, 1001.0) == "quota"
    assert daemon._warp_ip_needs_refresh(ip, 1001.0, "W1") is True, \
        "本线实测 quota 必须判为低质"
    assert "quota" in daemon._warp_ip_refresh_reason(ip, 1001.0, "W1")

    # 精确解禁（Retry-After 1h）到期、缓存 TTL（6h）未到：旧 quota 不得再
    # 驱动重启，否则「精确解禁」只在探测路径生效、预热路径仍白烧隧道。
    assert daemon._probe_cache_verdict("W1", ip, 4602.0) == "quota"
    assert daemon._warp_ip_needs_refresh(ip, 4602.0, "W1") is False
    assert "quota" not in daemon._warp_ip_refresh_reason(ip, 4602.0, "W1")

    # 长 TTL 过后结论作废：同一地址重新可探，不再凭缓存作答
    assert daemon._probe_cache_verdict("W1", ip, 1000.0 + 21601) is None


def test_probe_cache_never_bleeds_across_ips_in_same_segment(monkeypatch):
    """台账按 /128 键控：同段别的 IP 不得沾光，无论正向还是负向。

    依据控制变量后的实测：已烧段里的**全新** /128 只有 0.0% OK（n=2469），
    但未烧段里的全新 /128 也只有 2.0% OK（n=152）—— 段不是充分条件，
    判死的是那个地址自己，所以段级聚合既会误杀也会误放。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "probe_transient_ttl_sec", 120)
    monkeypatch.setitem(daemon.CFG["warp"], "probe_persistent_ttl_sec", 21600)
    ip1 = "2a09:bac5:1ef9:281e::1"
    ip2 = "2a09:bac5:1ef9:281e::ffff"   # 同段别的 IP
    ip3 = "2a09:bac5:aaaa:bbbb::1"      # 异段

    daemon._probe_cache_put("W1", ip1, "quota", now=1000.0)
    assert daemon._probe_cache_verdict("W1", ip1, 1001.0) == "quota"
    assert daemon._probe_cache_verdict("W1", ip2, 1001.0) is None, \
        "同段别的地址没被实测过，不能判死"
    assert daemon._probe_cache_verdict("W1", ip3, 1001.0) is None
    assert daemon._probe_cache_verdict("W2", ip1, 1001.0) is None, "按线隔离"

    daemon._probe_cache_put("W1", ip2, "OK", now=2000.0)
    assert daemon._probe_cache_verdict("W1", ip2, 2001.0) == "OK"
    assert daemon._probe_cache_verdict("W1", ip1, 2001.0) == "quota", \
        "别的 IP 的 OK 不能给已判死的地址平反"


def test_probe_cache_ttl_by_persistence_not_by_polarity(monkeypatch):
    """TTL 按**结论持久性**分三档：quota/flagged 6h > OK 10min > err/unreachable 2min。

    OK 不能和 err/unreachable 同档：它说的也是地址状态，而重探间隔中位 212s，
    2min 档位命中不到自己 —— 实测 1980 次「前一次 OK」里 1831 次（92.5%）结论
    不变，那 38% 的探测量纯属白烧配额。也不能直接套 6h：47 例 OK→quota 的间隔
    中位 1267s，长 TTL 只是把「凭旧 OK 选线」的窗口拉宽。
    探测侧故障（err/unreachable）不是地址的长期状态：日志实测同一地址 err 后
    66s 就 OK。给它们长 TTL 等于一次探测抖动锁死健康线 6h（重连已不作废台账）。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "probe_transient_ttl_sec", 120)
    monkeypatch.setitem(daemon.CFG["warp"], "probe_ok_ttl_sec", 600)
    monkeypatch.setitem(daemon.CFG["warp"], "probe_persistent_ttl_sec", 21600)
    ok_ip = "2a09:bac5:1ef9:281e::1"
    quota_ip = "2a09:bac5:1ef9:281e::2"
    err_ip = "2a09:bac5:1ef9:281e::3"
    daemon._probe_cache_put("W1", ok_ip, "OK", now=1000.0)
    daemon._probe_cache_put("W1", quota_ip, "quota", now=1000.0)
    daemon._probe_cache_put("W1", err_ip, "err", now=1000.0)
    # OK 档：比瞬时档长，覆盖 212s 的重探间隔中位
    assert daemon._probe_cache_verdict("W1", ok_ip, 1600.0) == "OK"      # 边界内
    assert daemon._probe_cache_verdict("W1", ok_ip, 1601.0) is None      # 过期
    # 瞬时档
    assert daemon._probe_cache_verdict("W1", err_ip, 1120.0) == "err"
    assert daemon._probe_cache_verdict("W1", err_ip, 1121.0) is None, \
        "探测侧故障不得按地址持久结论缓存 6h"
    # 持久档
    assert daemon._probe_cache_verdict("W1", quota_ip, 22599.0) == "quota"
    assert daemon._probe_cache_verdict("W1", quota_ip, 22601.0) is None


def test_probe_cache_reprobe_overwrites_verdict(monkeypatch):
    """同一地址重新实测覆盖旧结论（OK 复现率仅 43~95%，不可长期采信）。"""
    monkeypatch.setitem(daemon.CFG["warp"], "probe_transient_ttl_sec", 120)
    monkeypatch.setitem(daemon.CFG["warp"], "probe_persistent_ttl_sec", 21600)
    ip = "2a09:bac5:1ef9:281e::1"
    daemon._probe_cache_put("W1", ip, "OK", now=1000.0)
    daemon._probe_cache_put("W1", ip, "quota", now=1100.0)
    assert daemon._probe_cache_verdict("W1", ip, 1101.0) == "quota"


def test_marking_ip_blocked_invalidates_its_probe_verdicts(monkeypatch):
    """地址被 403/429 标记后，它此前探出的 OK 必须立刻作废。

    实测 829 次换线里 170 次（20.5%）是同一 IP 在 120s 内被重新选中，其中
    158 次依据的 OK 早于该 IP 的标脏时刻 —— 证据比事实旧，于是「探过 OK →
    被 429 烧 → 9 秒后又拿旧 OK 切回去」闭环。作废后必须重新实测。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    ip = "2a09:bac5:1ef9:281e::1"
    daemon._probe_cache_put("W1", ip, "OK", now=1000.0)
    assert daemon._probe_cache_verdict("W1", ip, 1001.0) == "OK"
    daemon._mark_warp_ip_used(ip, 1002.0, blocked=True, kind="rate429")
    assert daemon._probe_cache_verdict("W1", ip, 1003.0) is None, \
        "标脏后旧 OK 不能继续放行"
    assert daemon._warp_ip_needs_refresh(ip, 1003.0, "W1") is True


def test_forget_by_ip_covers_every_line_holding_it():
    """作废按 IP 全清，不只清当前线：被烧是地址属性，不是某条隧道的。

    同一地址可能被两条线先后持有（重连竞态），只清当前 tag 会留下另一条的
    OK —— 下次换线正好从那条线上把它当新鲜地址切回来。
    """
    ip = "2a09:bac5:1ef9:281e::1"
    daemon._probe_cache_put("W1", ip, "OK", now=1000.0)
    daemon._probe_cache_put("W2", ip, "OK", now=1000.0)
    daemon._probe_cache_put("W2", "2a09:bac5:1ef9:281e::2", "OK", now=1000.0)
    daemon._probe_cache_forget(ip)
    assert daemon._probe_cache_verdict("W1", ip, 1001.0) is None
    assert daemon._probe_cache_verdict("W2", ip, 1001.0) is None
    assert daemon._probe_cache_verdict("W2", "2a09:bac5:1ef9:281e::2", 1001.0) == "OK", \
        "别的地址不受牵连"
    daemon._probe_cache_forget(None)   # 出口未知时不该炸


def test_inflight_probe_started_before_block_is_rejected():
    """在途探测晚回来时不得把已作废的结论重新写进台账。

    探测 1.4~8.7s，期间地址完全可能被 403/429 烧掉。只删已有条目挡不住紧接着
    返回的那次 `_probe_cache_put` —— 它拿的是标脏前起手的 OK。codex P2。
    """
    ip = "2a09:bac5:1ef9:281e::1"
    daemon._probe_cache_forget(ip)                      # 标脏时刻 = now
    assert daemon._probe_cache_put("W1", ip, "OK", started_at=time.time() - 5) is False
    assert daemon._probe_cache_verdict("W1", ip) is None, "起手早于标脏的 OK 不得落地"
    # 标脏之后才起手的探测照常收下：那是「烧了之后又实测好」的合法新证据
    assert daemon._probe_cache_put("W1", ip, "OK", started_at=time.time() + 1) is True
    assert daemon._probe_cache_verdict("W1", ip) == "OK"


class ScriptedBytes:
    """按脚本返回 _warp_line_stats 的 (字节, 等首字节数, 总连接数)；耗尽后返回 None。

    只写数字时按「一条连接在传字节」处理：零连接不可能有字节增长，写 0
    会让 `warp_line_busy` 走零连接快速判闲，用例的原义（线在动）就丢了。
    元组可写 2 或 3 个元素；2 元组的总连接数取 max(等首字节数, 1)。
    末值不重复投递 —— 重复会让「恒忙」被误读成「闲」。
    """

    def __init__(self, seq):
        self.seq = list(seq)

    def __call__(self, tag):
        if not self.seq:
            return None
        v = self.seq.pop(0)
        if v is None:
            return None
        if isinstance(v, int):
            return (v, 0, 1)
        if len(v) == 3:
            return tuple(v)
        return (v[0], v[1], max(v[1], 1))


def test_ip_reuse_window_covers_measured_interval():
    """复用窗口须长于实测的复用间隔。

    实测同一 IP 被重新切回的间隔：25.2 / 28.3 / 49.1 分钟都有出现，
    而 8~11 分钟就切回的那些几乎立刻再次 403。窗口取 1h 覆盖这些观测。
    """
    assert C.CFG["warp"]["ip_reuse_guard_sec"] >= 1800, "窗口须覆盖 25 分钟以上的复用间隔"
    assert C.CFG["warp"]["blocked_ttl_sec"] >= 1800, "被标记的 IP 保留时长须同量级"


def test_retry_budget_exceeds_single_wait():
    """补刷总时长必须大于单次等待上限，否则重试无意义。"""
    w = C.CFG["warp"]
    assert w["ip_refresh_retry_sec"] > w["idle_wait_sec"]


def test_connection_count_criterion_removed():
    """按连接数判闲的旧判据必须已从源码移除。"""
    src = open('daemon.py', encoding='utf-8').read()
    assert "warp_active_conns" not in src


def test_retry_refresh_is_wired_in():
    """补刷函数必须真的被调度处调用。"""
    src = open('daemon.py', encoding='utf-8').read()
    assert "_retry_refresh_idle_warp(member)" in src


def test_line_busy_when_bytes_grow(monkeypatch):
    monkeypatch.setattr(daemon, "_warp_line_stats", ScriptedBytes([1000, 2000]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF") is True


def test_line_idle_when_bytes_unchanged(monkeypatch):
    monkeypatch.setattr(daemon, "_warp_line_stats", ScriptedBytes([2000, 2000]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF") is False


def test_line_idle_when_bytes_drop(monkeypatch):
    """连接被关闭时总量回落，同样说明该线闲下来了。"""
    monkeypatch.setattr(daemon, "_warp_line_stats", ScriptedBytes([2000, 1500]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF") is False


def test_line_busy_when_request_awaits_first_byte(monkeypatch):
    """字节零增长但仍有请求在等首字节 → 必须判忙，否则重启掐断它。

    实测：重启前该线上仍有此类连接的有 13%，cut 掉后客户端收到的是
    upstream connection lost（请求已发出，一个 token 都没回来）。
    """
    monkeypatch.setattr(daemon, "_warp_line_stats", ScriptedBytes([(2000, 0), (2000, 2)]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF") is True


def test_line_busy_when_young_conn_has_bytes_but_stalled(monkeypatch):
    """连接已收过数据（download>0）但正处静默期 → 仍须判忙。

    12:22 实测：WARP-CF-H 被重启时线上 4 条连接各收 ~1MB、年龄 94~116s，
    12s 窗口内零增长。旧判据要求 download==0 才计入等待连接，这 4 条全部
    漏检，重启把它们掐断 —— 客户端收到的正是 upstream connection lost。
    """
    monkeypatch.setattr(daemon, "_warp_line_stats", ScriptedBytes([(5_000_000, 4), (5_000_000, 4)]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF-H") is True


def test_line_idle_despite_long_lived_idle_connections(monkeypatch):
    """连接数不是判据：长寿空闲连接不得把线钉死在「忙」上。

    活跃线上恒有 7~10 条连接且从不归零（单条实测存活 936s）；按连接数判忙会
    让这些线永远刷不到新鲜 IP。这里模拟「有连接但都在等首字节之外」的情形：
    第二次采样仍无等待连接的计数，故判闲。
    """
    monkeypatch.setattr(daemon, "_warp_line_stats", ScriptedBytes([(2000, 0), (2000, 0)]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF") is False


def test_line_busy_unknown_when_query_fails(monkeypatch):
    monkeypatch.setattr(daemon, "_warp_line_stats", ScriptedBytes([None, None]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF") is None


def test_line_busy_unknown_on_partial_failure(monkeypatch):
    monkeypatch.setattr(daemon, "_warp_line_stats", ScriptedBytes([1000, None]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF") is None


def test_wait_idle_returns_true_when_line_goes_quiet(monkeypatch):
    monkeypatch.setattr(daemon, "_warp_line_stats", ScriptedBytes([1, 2, 2, 2]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon._wait_warp_idle({"tag": "WARP-CF"}) is True


def test_wait_idle_times_out_when_always_busy(monkeypatch):
    """恒忙（且脚本耗尽后查询失败）必须超时返回 False。"""
    monkeypatch.setattr(daemon, "_warp_line_stats", ScriptedBytes([1, 2]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 10.0
        return clock["t"]

    monkeypatch.setattr(time, "time", tick)
    assert daemon._wait_warp_idle({"tag": "WARP-CF"}) is False


def test_wait_idle_treats_query_failure_as_busy(monkeypatch):
    """查不到连接表时不得当作空闲，否则会强刷掐断在途连接。"""
    monkeypatch.setattr(daemon, "_warp_line_stats", ScriptedBytes([None]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 10.0
        return clock["t"]

    monkeypatch.setattr(time, "time", tick)
    assert daemon._wait_warp_idle({"tag": "WARP-CF"}) is False


def test_retry_loop_refreshes_after_first_busy_round(monkeypatch):
    """首轮忙不该终止补刷 —— 这正是 19:00:04 丢失刷新的根因。"""
    st = {"wait": 0, "refresh": 0}

    def fake_wait(member, **kw):
        st["wait"] += 1
        return st["wait"] > 1

    def fake_refresh(member, **kw):
        st["refresh"] += 1
        return "2a09:bac5:NEW::1"

    monkeypatch.setattr(daemon, "_wait_warp_idle", fake_wait)
    monkeypatch.setattr(daemon, "_refresh_idle_warp", fake_refresh)
    # 必须打桩：真实 active_warp_tag() 会发 Clash API 请求（读 4 次时钟、耗时不定），
    # 既把假时钟的 tick 预算吃光，也让用例结果取决于线上此刻哪条线在服务。
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WARP-CF-OTHER")
    monkeypatch.setattr(time, "time", lambda: 0.0)

    daemon._retry_refresh_idle_warp({"tag": "WARP-CF-C"})
    assert st["refresh"] == 1, "首轮忙之后必须重试并刷新成功"


def test_retry_loop_keeps_trying_while_busy(monkeypatch):
    """恒忙时必须重试多次，而非一次即弃。"""
    st = {"wait": 0}
    monkeypatch.setattr(daemon, "_wait_warp_idle",
                        lambda m, **kw: (st.__setitem__("wait", st["wait"] + 1), False)[1])
    monkeypatch.setattr(daemon, "_refresh_idle_warp", lambda m, **kw: None)

    clock = {"t": 0.0}

    def tick():
        clock["t"] += 100.0
        return clock["t"]

    monkeypatch.setattr(time, "time", tick)
    daemon._retry_refresh_idle_warp({"tag": "WARP-CF-C"})
    assert st["wait"] > 1, f"应重试多次，实际 {st['wait']}"


def test_retry_loop_keeps_trying_when_refresh_yields_no_fresh_ip(monkeypatch):
    """空闲但刷不出新鲜地址时也要继续重试。"""
    st = {"refresh": 0}
    monkeypatch.setattr(daemon, "_wait_warp_idle", lambda m, **kw: True)
    monkeypatch.setattr(daemon, "_refresh_idle_warp",
                        lambda m: (st.__setitem__("refresh", st["refresh"] + 1), None)[1])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WARP-CF-OTHER")

    clock = {"t": 0.0}

    def tick():
        clock["t"] += 100.0
        return clock["t"]

    monkeypatch.setattr(time, "time", tick)
    daemon._retry_refresh_idle_warp({"tag": "WARP-CF-C"})
    assert st["refresh"] > 1, f"应重试多次，实际 {st['refresh']}"


def test_retry_loop_aborts_when_member_becomes_active(monkeypatch):
    """空闲后这条线已成当前出口时必须放弃刷新，不得重启正在服务的隧道。

    场景：换线线程的快照早于本次调度，等待期间目标线被切成出口；此时重启它
    会掐断在途连接（upstream connection lost）。判据必须落在重启之前 ——
    放在循环开头挡不住「等空闲的 40s 内才变成出口」那一段。
    """
    st = {"refresh": 0}
    monkeypatch.setattr(daemon, "_wait_warp_idle", lambda m, **kw: True)
    monkeypatch.setattr(daemon, "_refresh_idle_warp",
                        lambda m: (st.__setitem__("refresh", st["refresh"] + 1), True)[1])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WARP-CF-C")

    clock = {"t": 0.0}

    def tick():
        clock["t"] += 100.0
        return clock["t"]

    monkeypatch.setattr(time, "time", tick)
    assert daemon._retry_refresh_idle_warp({"tag": "WARP-CF-C"}) is False
    assert st["refresh"] == 0, "已成当前出口的线不得被重启"


def test_failback_is_wired_into_main_loop():
    """反向兜底（池挂 → 切回 WARP）必须真的挂在主循环上。

    主循环是单体 main()，无法单测；这里退而检查调用点存在，
    防止重构时把它删掉而所有函数级测试依然全绿。
    """
    src = open('daemon.py', encoding='utf-8').read()
    # 主循环只许调 _bg 版：`_maybe_failback_to_warp` 要探整池健康（最慢 15s），
    # 在循环里同步调等于把错误检测钉死 15s。
    assert "_maybe_failback_to_warp_bg(now)" in src, "主循环未调用反向兜底"


def test_proxy_fallback_is_wired_into_reconnect():
    """正向兜底（WARP 全挂 → 切 proxy）必须挂在换线路径上。"""
    src = open('daemon.py', encoding='utf-8').read()
    assert src.count("_fallback_to_proxy(") >= 4, "换线路径的正向兜底调用点缺失"
    assert "^def _fallback_to_proxy" not in src
    assert "def _fallback_to_proxy" in src


def test_proactive_rotation_gated_when_on_warp():
    """opencode 走 WARP 时不得轮换轮换池节点。

    出口由 usque 决定，切池内节点对 opencode 毫无作用，只消耗节点调用计数、
    制造日志噪音。主循环是单体 main()，这里断言门控仍在。
    """
    src = open('daemon.py', encoding='utf-8').read()
    assert ("if (not opencode_out_on_warp()\n"
            "                    and now - last_proactive_rotate") in src, \
        "步骤 3 主动轮换缺少 opencode_out_on_warp 门控"


def test_failback_is_not_triggered_by_warp_revival_alone():
    """回切必须依据代理池不堪用的证据，而不是「WARP 探测又通了」。

    实测 00:02:45 落到 proxy、00:02:52 就因一次探测通过切回（7s），
    83 节点池一条请求都没服务过 —— 代理池资源完全浪费。
    旧实现 _maybe_recheck_warp 只看 _warp_any_reachable()，不得再出现在主循环里。
    """
    src = open('daemon.py', encoding='utf-8').read()
    assert "_maybe_recheck_warp" not in src, "仅凭 WARP 可达回切的旧路径必须已移除"


def test_failback_requires_three_evidence_criteria():
    """回切判据必须齐全：连续探测失败 / 落池后仍被拦截 / 黑名单占比。"""
    w = C.CFG["intervals"]
    for key in ("proxy_fail_streak", "proxy_blocked_threshold", "proxy_dead_ratio"):
        assert key in w, f"缺少回切判据 {key}"
    src = open('daemon.py', encoding='utf-8').read()
    for marker in ("_proxy_fail_streak", "still_limited", "pool_saturated"):
        assert marker in src, f"回切判据 {marker} 未接入"


def test_reconnect_now_does_not_read_error_counts():
    """绕阈值入口不得依赖 count_warp_blocked_since。

    用 AST 查真实调用而非文本匹配 —— docstring 里也提到了该函数名，
    文本匹配会把说明文字误判成调用。
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(daemon._reconnect_warp_now))
    names = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    names |= {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "count_warp_blocked_since" not in names, \
        f"绕阈值入口不应查错误计数，实际调用: {sorted(names)}"


def test_refresh_rejects_line_when_model_probe_fails(monkeypatch):
    """IP 新鲜但模型层不通（如 429）不得算刷新成功，必须继续重启换地址。

    原模式只验 IP 新鲜度，新拿到的地址必然通过，于是切上去头几条就 429。
    """
    monkeypatch.setattr(daemon, "_restart_service", lambda s, tag=None: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: True)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    monkeypatch.setitem(daemon.CFG["warp"], "max_attempts", 2)
    ips = iter(["ip-a", "ip-b"])
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: next(ips, "ip-b"))
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})

    def probe(member, ip):
        return ip == "ip-b"

    monkeypatch.setattr(daemon, "_warp_model_usable", probe)
    assert daemon._refresh_idle_warp({"tag": "W1", "port": 1080,
                                      "service": "s"}) == "ip-b"


def test_refresh_model_check_disabled_keeps_old_mode(monkeypatch):
    """refresh_model_check=False 恢复原模式：只看 IP 新鲜度，不调探测。"""
    import probe_warp
    called = []
    monkeypatch.setattr(probe_warp, "probe",
                        lambda port, timeout: (called.append(port), ("OK", 0.1, "", None))[1])
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", False)
    assert daemon._warp_model_usable({"tag": "W1", "port": 1080}, "ip-a") is True
    assert called == [], "原模式下不得发探测请求"


def test_model_probe_maps_quota_to_unusable(monkeypatch):
    """429/配额类判定不得算可用；OK 才算（换一条线，避开每会话每 IP 只探一次）。"""
    import probe_warp
    # 本用例测的是模型模式语义；生产默认可能切回 False，由 config 提供，
    # 用例必须显式声明自己测哪个模式，不被默认值漂移绑架。
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    verdict = {"v": "quota"}
    monkeypatch.setattr(probe_warp, "probe",
                        lambda port, timeout: (verdict["v"], 0.1, "", None))
    assert daemon._warp_model_usable({"tag": "W1", "port": 1080}, "ip") is False
    verdict["v"] = "OK"
    assert daemon._warp_model_usable({"tag": "W2", "port": 1081}, "ip2") is True


def test_probe_runs_at_most_once_per_address_within_ttl(monkeypatch):
    """同一 /128 在 TTL 内只探一次（避免重复烧配额）；换地址必须重探。"""
    import probe_warp
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setitem(daemon.CFG["warp"], "probe_persistent_ttl_sec", 21600)
    calls = []
    monkeypatch.setattr(probe_warp, "probe",
                        lambda port, timeout: (calls.append(port), ("quota", 0.1, "", None))[1])
    m = {"tag": "W1", "port": 1080}
    assert daemon._warp_model_usable(m, "2a09:bac5:1ef9:281e::1") is False
    assert daemon._warp_model_usable(m, "2a09:bac5:1ef9:281e::1") is False
    assert len(calls) == 1, f"同一地址同 TTL 只应探 1 次，实际 {len(calls)}"

    assert daemon._warp_model_usable(m, "2a09:bac5:1ef9:281e::2") is False
    assert len(calls) == 2, f"同段别的地址没实测过，必须自己探，实际 {len(calls)}"

    assert daemon._warp_model_usable(m, "2a09:bac5:aaaa:bbbb::1") is False
    assert len(calls) == 3, f"异段地址同样各探一次，实际 {len(calls)}"


def test_model_mode_ignores_reuse_window_but_keeps_penalty(monkeypatch):
    """模型模式只旁路「复用窗口」，惩罚期照旧排除。

    惩罚期是「这个地址确实被拦过」的确定事实；复用窗口是启发式，按段计、几小时
    恢复，经常与事实不一致 —— 旁路的只能后者。实测 7 个烧毁段里 648 个 /128 被
    逐个探测、608 个（94%）判 quota，全是本该在探测前就挡住的。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(daemon, "_warp_ip_recently_used", lambda ip, now, g=0: True)
    # 只在复用窗口内、未处于惩罚期 → 模型模式放行
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now: False)
    assert daemon._ip_disqualified("ip-a", time.time(), 86400) is False, \
        "复用窗口不得否决候选"
    # 处于惩罚期 → 两种模式都必须排除
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now: True)
    assert daemon._ip_disqualified("ip-a", time.time(), 86400) is True, \
        "惩罚期内不得放回候选"


def test_model_quota_verdict_marks_ip_blocked(monkeypatch):
    """模型层判 quota 必须落进 blocked 台账，否则脏 IP 在探测前无法排除。"""
    import probe_warp
    monkeypatch.setattr(daemon, "_probe_blind_until", 0.0)
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(daemon, "_append_warp_ip_history",
                        lambda *a, **k: None)
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(probe_warp, "probe",
                        lambda port, timeout: ("quota", 0.1, "", None))
    assert daemon._warp_model_usable({"tag": "W1", "port": 1080}, "ip-q") is False
    entry = daemon._WARP_IP_USED.get("ip-q") or {}
    assert entry.get("blocked") is True, "quota 必须写进 blocked 台账"
    assert "rate429" in daemon._warp_block_stamps(entry), \
        "quota 对应生产侧 rate429（同一个 FreeUsageLimitError）"
    # 结论必须在缓存里活着：清掉它下次调用会重探一遍、白烧一格配额。
    assert daemon._probe_cache_get("W1", "ip-q") == "quota"
    assert daemon._ip_disqualified("ip-q", time.time(), 86400) is True


def test_model_flagged_verdict_maps_to_abuse403(monkeypatch):
    """flagged（abusive traffic）对应 abuse403，惩罚期 6h 而非 24h。"""
    import probe_warp
    monkeypatch.setattr(daemon, "_probe_blind_until", 0.0)
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(daemon, "_append_warp_ip_history", lambda *a, **k: None)
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(probe_warp, "probe",
                        lambda port, timeout: ("flagged", 0.1, "", None))
    assert daemon._warp_model_usable({"tag": "W2", "port": 1081}, "ip-f") is False
    entry = daemon._WARP_IP_USED["ip-f"]
    assert "abuse403" in daemon._warp_block_stamps(entry)


def test_model_ok_verdict_does_not_mark_blocked(monkeypatch):
    """OK 不得标脏：否则每次测得可用就把地址推进惩罚期，候选池瞬间清空。"""
    import probe_warp
    monkeypatch.setattr(daemon, "_probe_blind_until", 0.0)
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(probe_warp, "probe",
                        lambda port, timeout: ("OK", 0.1, "", None))
    assert daemon._warp_model_usable({"tag": "W3", "port": 1082}, "ip-ok") is True
    entry = daemon._WARP_IP_USED["ip-ok"]
    assert entry.get("blocked") is False
    assert daemon._ip_disqualified("ip-ok", time.time(), 86400) is False


def test_all_probe_exceptions_mark_channel_blind(monkeypatch):
    """整批探测抛异常与整批 unreachable 同义：都是探测器自己没跑通。

    旧实现异常分支不写 `_PROBE_LAST_VERDICT`，批判据看到的是空串/旧 OK，
    失明机制不触发 —— 每条线都被判「地址坏了」白重启、白烧配额。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(daemon, "_probe_blind_until", 0.0)
    monkeypatch.setattr(daemon, "_PROBE_LAST_VERDICT", {})
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    import probe_warp

    def _boom(port, timeout):
        raise RuntimeError("bun gone")

    monkeypatch.setattr(probe_warp, "probe", _boom)
    pairs = [({"tag": f"W{i}", "port": 1080 + i}, f"ip-{i}") for i in range(3)]
    out = daemon._warp_model_test_many(pairs)
    assert out == {"W0": False, "W1": False, "W2": False}
    assert daemon._warp_probe_blind() is True, "整批异常须判定通道失明"


def test_blind_channel_falls_back_to_ip_history(monkeypatch):
    """整批 unreachable ⇒ 探测通道失明，退回历史判据。

    实测 16:50:18 一批 17 条同一秒全 unreachable，重启换 IP 后 16:50:50~52 全部
    OK —— 那批结论说的是探测器自己没打通，拿它当真会白烧 17 条隧道的重启与配额。
    """
    monkeypatch.setattr(daemon, "_probe_blind_until", 0.0)
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    # 标脏墓碑同样是进程级全局：残留条目会拒收后续用例带 started_at 的写入。
    monkeypatch.setattr(daemon, "_PROBE_BLOCKED_AT", {})
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    daemon._note_probe_batch(["unreachable"] * 17)
    assert daemon._warp_probe_blind() is True
    assert daemon._ip_history_is_truth() is True, "失明期间必须退回历史判据"
    # 失明时不得再发探测请求：探出来只会是通道的故障，还白烧一格配额。
    import probe_warp
    called = []
    monkeypatch.setattr(probe_warp, "probe",
                        lambda port, timeout: (called.append(port), ("OK", 0.1, "", None))[1])
    assert daemon._warp_model_usable({"tag": "W1", "port": 1080}, "ip-a") is True
    assert called == [], "通道失明期间不该再探测"


def test_partial_unreachable_is_not_blindness(monkeypatch):
    """单条 unreachable 是出口的真实瞬态（TTL 2min），不得升级成通道失明。"""
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(daemon, "_probe_blind_until", 0.0)
    daemon._note_probe_batch(["unreachable", "OK", "quota"])
    assert daemon._warp_probe_blind() is False
    assert daemon._ip_history_is_truth() is False, "未失明时仍以实测为准"


def test_single_unreachable_batch_is_not_blindness(monkeypatch):
    """只测 1 条且 unreachable 不得判失明 —— `all()` 对单元素恒真，会误锁 120s。

    回归 2026-10-26：`probe_candidates_per_switch`=2 时错误换线每轮只测 1~2 条，
    日志实测出现「本批 1 条模型层探测全 unreachable → 判定通道失明」，随后 47 次
    候选跳过实测被放行。放行的正是 quota 地址：切上去首条请求就 429。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(daemon, "_probe_blind_until", 0.0)
    daemon._note_probe_batch(["unreachable"])
    assert daemon._warp_probe_blind() is False, "单条 unreachable 不足以判通道失明"
    daemon._note_probe_batch(["unreachable", "unreachable"])
    assert daemon._warp_probe_blind() is False, "2 条样本同样不足以判定"
    daemon._note_probe_batch(["unreachable"] * 3)
    assert daemon._warp_probe_blind() is True, "达到最小样本数才判定失明"


def test_blind_channel_does_not_restart_line_on_stale_unreachable(monkeypatch):
    """失明期间缓存的 unreachable 不得驱动重启：它不是「地址被拒」。"""
    monkeypatch.setattr(daemon, "_probe_blind_until", time.time() + 60)
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN",
                        {("W1", "ip-a"): ("unreachable", time.time())})
    assert daemon._warp_ip_needs_refresh("ip-a", time.time(), "W1") is False
    assert daemon._warp_ip_refresh_reason("ip-a", time.time(), "W1") == ""


def test_stale_unreachable_still_restarts_when_channel_healthy(monkeypatch):
    """通道健康时缓存的 unreachable 照旧驱动重启，语义不变。"""
    monkeypatch.setattr(daemon, "_probe_blind_until", 0.0)
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN",
                        {("W1", "ip-a"): ("unreachable", time.time())})
    assert daemon._warp_ip_needs_refresh("ip-a", time.time(), "W1") is True
    assert daemon._warp_ip_refresh_reason("ip-a", time.time(), "W1") == "模型层 unreachable"


def test_old_mode_still_rejects_by_ip_history(monkeypatch):
    """原模式保留：被标记或窗口内用过即排除，不依赖任何探测。"""
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", False)
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now: True)
    monkeypatch.setattr(daemon, "_warp_ip_recently_used", lambda ip, now, g=0: False)
    assert daemon._ip_disqualified("ip-a", time.time(), 86400) is True



def test_model_mode_needs_refresh_only_for_known_bad_addresses(monkeypatch):
    """低质判据：被标记过、或用过却没成功过才算脏；全新地址不算。

    「见过 IP 就当新鲜」会让脏线永远不刷；反过来「有 IP 就要刷」会把刚
    刷出来的干净地址反复重启，白烧配额。两条边界都要钉住。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "flagged": {"ts": 1000.0, "blocked": True,
                    "blocked_ts_by_kind": {"abuse403": 1000.0}},
        "never-served": {"ts": 1000.0},
        "served": {"ts": 1000.0, "last_ok_ts": 1000.0},
    })
    now = 1100.0
    assert daemon._warp_ip_needs_refresh("flagged", now) is True
    assert daemon._warp_ip_needs_refresh("never-served", now) is True
    assert daemon._warp_ip_needs_refresh("served", now) is False
    assert daemon._warp_ip_needs_refresh("brand-new", now) is False, \
        "从没出现过的地址不是低质，否则刚刷出的线会被反复重启"
    assert daemon._warp_ip_needs_refresh(None, now) is True, "IP 未知按待刷处理"


def test_retry_loop_restarts_line_whose_ip_never_served(monkeypatch):
    """模型模式下「有地址但从没成功过」必须真刷，不得空转记 ok。

    旧判据只要 known 存在就 return True，实测 239 次 ok 里绝大多数一次
    重启都没做 —— 批量排队的低质线全部被这个早退吞掉。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {"WARP-CF-C": {"ip": "dead-ip"}})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {"dead-ip": {"ts": 1000.0}})
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_wait_warp_idle", lambda m, **kw: True)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WARP-CF-OTHER")
    refreshed = []
    monkeypatch.setattr(daemon, "_refresh_idle_warp",
                        lambda m, **kw: (refreshed.append(m["tag"]), "new-ip")[1])
    assert daemon._retry_refresh_idle_warp({"tag": "WARP-CF-C"}) is True
    assert refreshed == ["WARP-CF-C"], f"从没成功过的地址必须被换掉，实际 {refreshed}"


def test_retry_loop_never_restarts_brand_new_ip(monkeypatch):
    """刚刷出的全新地址不得再重启一次 —— 那是白烧配额。"""
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {"WARP-CF-C": {"ip": "new-ip"}})
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_wait_warp_idle", lambda m, **kw: True)
    refreshed = []
    monkeypatch.setattr(daemon, "_refresh_idle_warp",
                        lambda m, **kw: (refreshed.append(m["tag"]), "x")[1])
    assert daemon._retry_refresh_idle_warp({"tag": "WARP-CF-C"}) is True
    assert not refreshed, f"全新地址不应再重启，实际 {refreshed}"


def test_dirty_worker_restarts_flagged_line_in_model_mode(monkeypatch):
    """模型模式下脏线守护探到被标记的地址仍要重连。

    `_ip_disqualified` 在模型模式恒 False，原先的早退让脏线守护从不重启，
    与预热的低质判据分叉。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: "burned-ip")
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "burned-ip": {"ts": 1000.0, "blocked": True,
                      "blocked_ts_by_kind": {"rate429": 1000.0}}})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "WARP-OTHER")
    monkeypatch.setattr(daemon, "warp_line_busy", lambda tag, window=2.0: False)
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_WARP_PROBING", set())
    refreshed = []
    monkeypatch.setattr(daemon, "_refresh_idle_warp",
                        lambda m, **kw: (refreshed.append(m["tag"]), "clean-ip")[1])

    daemon._reclaim_dirty_warp_worker({"tag": "W1", "port": 1080,
                                       "service": "usque-warp.service"})

    assert refreshed == ["W1"], f"被标记的出口必须重连，实际 {refreshed}"


def test_model_probe_ok_records_probe_ok_not_served(monkeypatch):
    """实测通过记 `probe_ok_ts`，不得冒充 `last_ok_ts`。

    `last_ok_ts` 的语义是「该地址真的撑过生产流量」，由 `_record_active_ip_success`
    独占；探针 OK 只说明此刻配额还没耗尽。混写会让选线把「只在空跑里验过」的地址
    排进次级优质档，与真的撑过流量的地址同权 —— 实测台账 2945 条里 738 条属于这种
    「验过没用过」。
    """
    import probe_warp
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(probe_warp, "probe", lambda port, timeout: ("OK", 0.1, "", None))
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {"ip-a": {"ts": 1000.0}})
    monkeypatch.setattr(time, "time", lambda: 2000.0)

    assert daemon._warp_model_usable({"tag": "W1", "port": 1080}, "ip-a") is True
    assert daemon._WARP_IP_USED["ip-a"]["probe_ok_ts"] == 2000.0
    assert daemon._WARP_IP_USED["ip-a"].get("last_ok_ts") is None, \
        "探针 OK 不是生产成功，不得写 last_ok_ts"
    assert daemon._warp_ip_needs_refresh("ip-a", 2000.0) is False, \
        "刚验过的地址不得再被判低质"
    assert daemon._warp_ip_rank("ip-a", 2000.0, 86400)[0] == 2, \
        "验过但没服务过的地址应落第 2 档"


def test_model_mode_selection_trusts_probe_not_history(monkeypatch):
    """选线也以实测为准：历史标为「烧过」的地址，此刻探测 OK 就该选它。

    若刷新按模型判、选线却仍按历史剔，会形成「刷新通过 → 选线丢弃 → 再刷新」
    的空转，这正是本条要盯的回归点。
    """
    two = [{"tag": "W1", "port": 1080, "service": "s1"},
           {"tag": "W2", "port": 1081, "service": "s2"}]
    monkeypatch.setitem(daemon.CFG["warp"], "members", two)
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "ip-w1")
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cached_warp_standby",
                        lambda standby, now, guard: [(two[1], "burned-ip")])
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: True)
    monkeypatch.setattr(daemon, "_warp_ip_recently_used", lambda ip, now, g=0: True)
    tested = []

    def fake_test_many(pairs):
        tested.extend(m["tag"] for m, _ in pairs)
        return {m["tag"]: True for m, _ in pairs}

    monkeypatch.setattr(daemon, "_warp_model_test_many", fake_test_many)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag, **kw: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)
    monkeypatch.setattr(daemon, "_warp_line_selected", lambda *a: None)
    monkeypatch.setattr(daemon, "_set_warp_line_ip", lambda *a: None)
    monkeypatch.setattr(daemon, "_warp_prefix_rest", lambda ip, now: None)

    assert daemon.reconnect_warp("test") is True
    assert tested == ["W2"], f"选线必须先实测候选，实际测了 {tested}"
    assert switched == ["W2"], f"历史被标记但实测通过应选它，实际 {switched}"


def _reconnect_fixture(monkeypatch, test_many, refresh_result=None):
    """搭好 reconnect_warp 的最小环境，返回 (two members, 已切换 tag 列表)。

    test_many: tag 列表 -> {tag: bool}；未列出的 tag 视为通过。
    refresh_result: _refresh_idle_warp 的返回值（None = 刷不出）。
    """
    two = [{"tag": "W1", "port": 1080, "service": "s1"},
           {"tag": "W2", "port": 1081, "service": "s2"}]
    monkeypatch.setitem(daemon.CFG["warp"], "members", two)
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "ip-w1")
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cached_warp_standby",
                        lambda standby, now, guard: [(two[1], "ip-w2")])
    monkeypatch.setattr(daemon, "_warp_model_test_many",
                        lambda pairs: {m["tag"]: test_many([m["tag"]])[m["tag"]]
                                       for m, _ in pairs})
    refreshed, scheduled = [], []
    monkeypatch.setattr(daemon, "_refresh_idle_warp",
                        lambda m, **kw: (refreshed.append(m["tag"]), refresh_result)[1])
    monkeypatch.setattr(daemon, "_schedule_idle_refresh",
                        lambda m: scheduled.append(m["tag"]))
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag, **kw: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_warp_line_selected", lambda *a: None)
    monkeypatch.setattr(daemon, "_set_warp_line_ip", lambda *a: None)
    monkeypatch.setattr(daemon, "_warp_prefix_rest", lambda ip, now: None)
    monkeypatch.setattr(daemon, "_fallback_to_proxy",
                        lambda *a: switched.append("proxy") or True)
    return two, switched, refreshed, scheduled


def test_model_probe_failure_reconnects_instead_of_falling_to_proxy(monkeypatch):
    """模型层测不过的备线必须重连换会话 IP，而不是直接落 proxy 池。

    回归点（codex P1）：重连阻塞块原本在实测之前，实测把 probes 清空后直接
    走到「无可用备线，切轮换池兜底」—— 一条备线都没重启就落池，T-175 退化。
    """
    two, switched, refreshed, scheduled = _reconnect_fixture(
        monkeypatch, lambda tags: {t: False for t in tags}, refresh_result="ip-new")
    assert daemon.reconnect_warp("test") is True
    assert refreshed == ["W2"], f"测不过的线必须重连，实际重连 {refreshed}"
    assert switched == ["W2"], f"重连后应切到新 IP，实际 {switched}"


def test_model_probe_failure_after_reconnect_still_falls_to_proxy(monkeypatch):
    """重连也刷不出可用出口时才落池 —— 兜底顺序不能丢。"""
    two, switched, refreshed, _ = _reconnect_fixture(
        monkeypatch, lambda tags: {t: False for t in tags}, refresh_result=None)
    assert daemon.reconnect_warp("test") is True
    assert refreshed == ["W2"], f"刷不出也要先试着重连，实际 {refreshed}"
    assert switched == ["proxy"], f"重连失败才落池，实际 {switched}"


def test_failed_lines_are_scheduled_for_background_refresh(monkeypatch):
    """其余测不过的线要进后台并发补刷 —— 不通过的隧道留着没有意义。"""
    three = [{"tag": "W1", "port": 1080, "service": "s1"},
             {"tag": "W2", "port": 1081, "service": "s2"},
             {"tag": "W3", "port": 1082, "service": "s3"}]
    two, switched, refreshed, scheduled = _reconnect_fixture(
        monkeypatch, lambda tags: {t: t != "W2" for t in tags}, refresh_result="ip-new")
    monkeypatch.setitem(daemon.CFG["warp"], "members", three)
    monkeypatch.setattr(daemon, "_cached_warp_standby",
                        lambda standby, now, guard: [(three[1], "ip-b"), (three[2], "ip-c")])
    assert daemon.reconnect_warp("test") is True
    assert switched == ["W3"], f"有一条可用就切它，实际 {switched}"
    # W3 实测通过 → 不必同步重连，直接切它；但测不过的 W2 不能就这么留着，
    # 必须进后台补刷队列重连换会话 IP。
    # 换下的 W1 本来就要补刷；关键是测不过的 W2 也必须进队列重连。
    assert "W2" in scheduled, f"测不过的 W2 必须排队后台重连，实际 {scheduled}"


def test_proxy_fallback_state_is_isolated_from_production():
    """落池时刻必须写到临时目录，不得落在生产仓库。

    这条是上面 `_isolate_warp_state` 的自检：隔离漏了的时候，任何走到
    `_fallback_to_proxy` 的用例都会把「刚落池」写进生产目录，届时**下一次**
    运行的结论由上一次运行的残留决定（实测同目录连跑两次结论相反，17s vs 0.06s）。
    """
    prod = os.path.join(os.path.dirname(os.path.abspath(daemon.__file__)),
                        ".warp_proxy_fallback.json")
    assert os.path.abspath(daemon._proxy_fallback_path()) != prod, \
        "落池时刻路径未隔离，测试会写进生产仓库"


def test_model_mode_excludes_blocked_ip_before_probing(monkeypatch):
    """惩罚期内的出口必须在模型探测**之前**排除，不得先探一格配额再否决。

    脏 IP 设计的意义是省配额：探过再丢等于每一格都付了学费。所以被标记的备线
    直接进刷新队列换新会话 IP，模型探测只打在刷新出来的新地址上。实测 7 个烧毁
    段里 648 个 /128 被逐个探测、608 个（94%）判 quota，全是本该提前挡住的。
    """
    two, switched, refreshed, _ = _reconnect_fixture(
        monkeypatch, lambda tags: {t: True for t in tags}, refresh_result="ip-new")
    probed = []

    def fake_test_many(pairs):
        probed.extend(ip for _, ip in pairs)
        return {m["tag"]: True for m, _ in pairs}

    monkeypatch.setattr(daemon, "_cached_warp_standby", lambda standby, now, guard: [])
    monkeypatch.setattr(daemon, "_probe_warp_egress",
                        lambda standby: {m["tag"]: "burned-ip" for m in standby})
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: ip == "burned-ip")
    monkeypatch.setattr(daemon, "_warp_model_test_many", fake_test_many)
    assert daemon.reconnect_warp("test") is True
    assert "burned-ip" not in probed, f"惩罚期地址不得进入模型探测，实际探了 {probed}"
    assert refreshed == ["W2"], f"被标记的线应直接重连换会话 IP，实际 {refreshed}"
    assert switched == ["W2"], f"重连后应切过去，实际 {switched}"


def test_min_gap_does_not_mark_new_exit_blocked(monkeypatch):
    """生效窗口内的陈旧错误不得把新出口标脏（codex P2）。

    新出口刚上线，上一段的在途请求才报错；若照旧标脏，这条刚验证过的好线
    会被排除 6~24h，且本段永远采不到真正的结束点。新出口首错延迟 p50=1.6s，
    所以「生效后还没有一条错误」是区分二者的判据。
    """
    marked = []
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "ip-new")
    monkeypatch.setattr(daemon, "_warp_member", lambda tag: {"tag": tag})
    monkeypatch.setattr(daemon, "_mark_warp_ip_used",
                        lambda ip, now, blocked=False, kind=None: marked.append(ip))
    monkeypatch.setattr(daemon, "_record_warp_cycle", lambda *a, **k: None)
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda **k: pytest.fail("陈旧错误不得触发换线"))
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 998.0)   # 2s 前刚生效
    monkeypatch.setattr(daemon, "_WARP_IP_CYCLES", [])
    blocked = {"abuse403": 0, "rate429": 3, "ok": 0, "consec_err": 3, "full_err": False}
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: blocked if ts == 0 else {**blocked, "abuse403": 0, "rate429": 0})
    daemon.maybe_reconnect_warp(1000.0)
    assert marked == [], f"新出口不得被陈旧错误标脏，实际标了 {marked}"


def test_real_error_after_new_exit_is_still_marked(monkeypatch):
    """新出口自己出错时必须照旧标脏 —— 判据是「生效后有无错误」，不是时间。"""
    marked = []
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "ip-new")
    monkeypatch.setattr(daemon, "_warp_member", lambda tag: {"tag": tag})
    monkeypatch.setattr(daemon, "_mark_warp_ip_used",
                        lambda ip, now, blocked=False, kind=None, **k:
                        marked.append((ip, blocked)))
    monkeypatch.setattr(daemon, "_record_warp_cycle", lambda *a, **k: None)
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda **k: pytest.fail("生效 2s 内硬闸必须拦住换线"))
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 998.0)
    monkeypatch.setattr(daemon, "_WARP_IP_CYCLES", [])
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 0, "rate429": 2, "ok": 0,
                                    "consec_err": 2, "full_err": False}
                        if ts == 0 else
                        {"abuse403": 0, "rate429": 2, "ok": 0,
                         "consec_err": 2, "full_err": False})
    daemon.maybe_reconnect_warp(1000.0)
    assert marked == [("ip-new", True)], f"新出口自己的错误必须标脏，实际 {marked}"




def test_dirty_worker_skips_reprobe_when_address_unchanged(monkeypatch):
    """地址与验收记录一致就免测：usque 自重连是换址的唯一来源。

    缓存 `_warp_line_ip` 被 `_invalidate_warp_line_ip` 在每次重启时清空，拿它
    当第二个源会把同一个已验证地址当成「没验过」反复重探（每探一次烧一格配额）。
    比对源必须是验收记录自己带的地址。
    """
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda *a, **k: "ip-same")
    monkeypatch.setattr(daemon, "_warp_ip_needs_refresh", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "_WARP_LAST_ACCEPT", {})          # 缓存已清空
    daemon._WARP_LAST_ACCEPT["W1"] = (999.0, "ip-same")
    probed = []
    monkeypatch.setattr(daemon, "_warp_model_usable",
                        lambda m, ip: (probed.append(ip), True)[1])

    daemon._reclaim_dirty_warp_worker({"tag": "W1", "port": 1080,
                                       "service": "usque-warp.service"})

    assert probed == [], f"同址不得重探，实际探了 {probed}"
    assert daemon._WARP_LAST_ACCEPT["W1"][1] == "ip-same", "同址应续期验收记录"


def test_dirty_worker_reprobes_after_usque_reconnect(monkeypatch):
    """usque 自己换址后，验收记录必须作废重判 —— 否则好 IP 的结论被新地址冒用。"""
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda *a, **k: "ip-new")
    monkeypatch.setattr(daemon, "_warp_ip_needs_refresh", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "_WARP_LAST_ACCEPT", {"W1": (999.0, "ip-old")})
    probed = []

    def fake_usable(member, ip):
        probed.append(ip)
        return True

    monkeypatch.setattr(daemon, "_warp_model_usable", fake_usable)

    daemon._reclaim_dirty_warp_worker({"tag": "W1", "port": 1080,
                                       "service": "usque-warp.service"})

    assert probed == ["ip-new"], f"换址后必须重探新地址，实际 {probed}"
    assert daemon._WARP_LAST_ACCEPT["W1"][1] == "ip-new", "通过验收后记录新地址"


def test_reclaim_pass_rechecks_line_whose_address_changed(monkeypatch):
    """静置窗口只在地址未变时生效：换了址的线必须立刻回到巡检范围。

    usque 自重连换址是常态，换掉的地址即便还在静置窗口内，池子也已经指向一个
    不存在的出口 —— 继续静置等于整段窗口放着脏线不管。
    """
    member = {"tag": "W1", "port": 1080, "service": "s1"}
    monkeypatch.setitem(daemon.CFG["warp"], "members", [member])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W2")
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_WARP_REFRESH_PENDING", set())
    monkeypatch.setattr(daemon, "_WARP_PROBING", set())
    monkeypatch.setattr(daemon, "_WARP_BUSY_UNTIL", {})
    monkeypatch.setattr(daemon, "_standby_defer_reason", lambda tag: None)
    monkeypatch.setattr(daemon, "_WARP_LAST_ACCEPT", {"W1": (time.time(), "ip-old")})
    monkeypatch.setattr(daemon, "_warp_line_ip", lambda tag: "ip-new")
    launched = []
    monkeypatch.setattr(daemon, "_reclaim_dirty_warp_worker",
                        lambda m: launched.append(m["tag"]))

    daemon._reclaim_dirty_pass()

    assert launched == ["W1"], f"换址后必须重新巡检，实际 {launched}"


def test_busy_window_covers_measured_sse_silence():
    """判闲窗口必须长于实物流式响应的 token 间隔。

    实测（N=30 连接，100s 采样，2026-10-22）两次字节增长之间的静默：
    p50=7s / p90=9.1s / max=10.1s。窗口 2s 时一半以上的采样落在静默期，
    把仍在传流的线判成空闲并立即重启 —— 9router 报 upstream connection lost。
    """
    assert C.CFG["warp"]["busy_window_sec"] >= 10.1, "窗口须覆盖实测最长 token 静默"


def test_reclaim_worker_leaves_busy_line_alone(monkeypatch):
    """线还在传数据时，脏线守护必须让出而不是重启它。

    旧实现 while 循环 600s 内反复重启（实测 26min 内 661 次 systemd Started），
    每次都掐断在途 SSE。让出后应登记冷却，避免守护每 5s 重派一次探测。
    """
    monkeypatch.setattr(daemon, "_WARP_BUSY_UNTIL", {})
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda *a, **k: "2a09:bac5::1")
    monkeypatch.setattr(daemon, "_warp_ip_needs_refresh", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)
    monkeypatch.setattr(daemon, "warp_line_busy", lambda tag, window=None: True)
    restarted = []
    monkeypatch.setattr(daemon, "_refresh_idle_warp",
                        lambda m, reason_hint=None: restarted.append(m) or "x")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    member = {"tag": "WARP-CF-B", "port": 1081, "service": "usque-warp-b"}
    daemon._reclaim_dirty_warp_worker(member)
    assert restarted == [], "在途线上不允许重启"
    assert daemon._WARP_BUSY_UNTIL.get("WARP-CF-B", 0) > time.time() - 1
