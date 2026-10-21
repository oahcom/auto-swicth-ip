#!/usr/bin/env python3
"""WARP 线刷新守卫回归测试（无网络、无副作用）。

覆盖 2026-10-19 19:08 事故中失效的判据：
  1. 用「字节是否还在增长」判线忙闲，而非「连接数是否为 0」
  2. 被换下的线等不到空闲时必须补刷，而非单次超时即放弃
  3. 出口 IP 复用窗口须长于 opencode 的记忆

运行: python3 -m pytest test_warp_refresh.py -v
"""
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
    # 探测台账是进程级全局：不隔离则前一个用例探过的 IP 在本用例被当成
    # 「本会话已探过」而跳过真实探测，台账相关断言全落空。
    monkeypatch.setattr(daemon, "_WARP_PROBE_EPOCH", {})
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    # 模型探测通过会记 last_ok_ts 并落盘，不隔离就写进生产 .warp_ip_used.json。
    monkeypatch.setenv("WARP_IP_USED_FILE_PATH", str(tmp_path / "warp_ip_used.json"))


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


class ScriptedBytes:
    """按脚本返回 _warp_line_bytes 的值；脚本耗尽后返回 None（模拟查询失败）。

    末值不重复投递 —— 重复会让「恒忙」被误读成「闲」。
    """

    def __init__(self, seq):
        self.seq = list(seq)

    def __call__(self, tag):
        return self.seq.pop(0) if self.seq else None


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
    monkeypatch.setattr(daemon, "_warp_line_bytes", ScriptedBytes([1000, 2000]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF") is True


def test_line_idle_when_bytes_unchanged(monkeypatch):
    monkeypatch.setattr(daemon, "_warp_line_bytes", ScriptedBytes([2000, 2000]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF") is False


def test_line_idle_when_bytes_drop(monkeypatch):
    """连接被关闭时总量回落，同样说明该线闲下来了。"""
    monkeypatch.setattr(daemon, "_warp_line_bytes", ScriptedBytes([2000, 1500]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF") is False


def test_line_busy_unknown_when_query_fails(monkeypatch):
    monkeypatch.setattr(daemon, "_warp_line_bytes", ScriptedBytes([None, None]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF") is None


def test_line_busy_unknown_on_partial_failure(monkeypatch):
    monkeypatch.setattr(daemon, "_warp_line_bytes", ScriptedBytes([1000, None]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon.warp_line_busy("WARP-CF") is None


def test_wait_idle_returns_true_when_line_goes_quiet(monkeypatch):
    monkeypatch.setattr(daemon, "_warp_line_bytes", ScriptedBytes([1, 2, 2, 2]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert daemon._wait_warp_idle({"tag": "WARP-CF"}) is True


def test_wait_idle_times_out_when_always_busy(monkeypatch):
    """恒忙（且脚本耗尽后查询失败）必须超时返回 False。"""
    monkeypatch.setattr(daemon, "_warp_line_bytes", ScriptedBytes([1, 2]))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 10.0
        return clock["t"]

    monkeypatch.setattr(time, "time", tick)
    assert daemon._wait_warp_idle({"tag": "WARP-CF"}) is False


def test_wait_idle_treats_query_failure_as_busy(monkeypatch):
    """查不到连接表时不得当作空闲，否则会强刷掐断在途连接。"""
    monkeypatch.setattr(daemon, "_warp_line_bytes", ScriptedBytes([None]))
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

    def fake_wait(member):
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
                        lambda m: (st.__setitem__("wait", st["wait"] + 1), False)[1])
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
    monkeypatch.setattr(daemon, "_wait_warp_idle", lambda m: True)
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
    monkeypatch.setattr(daemon, "_wait_warp_idle", lambda m: True)
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
                        lambda port, timeout: (called.append(port), ("OK", 0.1, ""))[1])
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", False)
    assert daemon._warp_model_usable({"tag": "W1", "port": 1080}, "ip-a") is True
    assert called == [], "原模式下不得发探测请求"


def test_model_probe_maps_quota_to_unusable(monkeypatch):
    """429/配额类判定不得算可用；OK 才算（换一条线，避开每会话每 IP 只探一次）。"""
    import probe_warp
    verdict = {"v": "quota"}
    monkeypatch.setattr(probe_warp, "probe",
                        lambda port, timeout: (verdict["v"], 0.1, ""))
    assert daemon._warp_model_usable({"tag": "W1", "port": 1080}, "ip") is False
    verdict["v"] = "OK"
    assert daemon._warp_model_usable({"tag": "W2", "port": 1081}, "ip2") is True


def test_probe_runs_at_most_once_per_ip_per_session(monkeypatch):
    """同一会话同一 IP 只探一次，避免重复烧配额；重启隧道后允许再探。"""
    import probe_warp
    calls = []
    monkeypatch.setattr(probe_warp, "probe",
                        lambda port, timeout: (calls.append(port), ("quota", 0.1, ""))[1])
    m = {"tag": "W1", "port": 1080}
    assert daemon._warp_model_usable(m, "ip") is False
    assert daemon._warp_model_usable(m, "ip") is False
    assert len(calls) == 1, f"同会话同 IP 只应探 1 次，实际 {len(calls)}"

    daemon._bump_probe_epoch("W1")
    assert daemon._warp_model_usable(m, "ip") is False
    assert len(calls) == 2, f"重启后同 IP 应允许再探，实际 {len(calls)}"

    assert daemon._warp_model_usable(m, "ip-other") is False
    assert len(calls) == 3, f"新 IP 应各探一次，实际 {len(calls)}"


def test_model_mode_ignores_ip_history(monkeypatch):
    """模型模式下 IP 历史不得否决候选：被标记、窗口内用过都放行，由实测决定。"""
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now: True)
    monkeypatch.setattr(daemon, "_warp_ip_recently_used", lambda ip, now, g=0: True)
    assert daemon._ip_disqualified("ip-a", time.time(), 86400) is False, \
        "模型模式下历史不得参与判断"


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
    monkeypatch.setattr(daemon, "_wait_warp_idle", lambda m: True)
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
    monkeypatch.setattr(daemon, "_wait_warp_idle", lambda m: True)
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


def test_model_probe_ok_marks_address_served(monkeypatch):
    """实测通过要把地址记进 last_ok_ts，否则同址停在档位 2 被反复重启。"""
    import probe_warp
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", True)
    monkeypatch.setattr(probe_warp, "probe", lambda port, timeout: ("OK", 0.1, ""))
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {"ip-a": {"ts": 1000.0}})
    monkeypatch.setattr(time, "time", lambda: 2000.0)

    assert daemon._warp_model_usable({"tag": "W1", "port": 1080}, "ip-a") is True
    assert daemon._WARP_IP_USED["ip-a"]["last_ok_ts"] == 2000.0
    assert daemon._warp_ip_needs_refresh("ip-a", 2000.0) is False, \
        "刚验过的地址不得再被判低质"


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
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
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
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
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
                        lambda ip, now, blocked=False, kind=None: marked.append((ip, blocked)))
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


