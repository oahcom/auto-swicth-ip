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

sys.path.insert(0, '.')

# 必须早于 import daemon —— 否则模块级已冻结点错文件
import config as C

C.CFG["log_file"] = "/dev/null"

import daemon


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

    def fake_refresh(member):
        st["refresh"] += 1
        return "2a09:bac5:NEW::1"

    monkeypatch.setattr(daemon, "_wait_warp_idle", fake_wait)
    monkeypatch.setattr(daemon, "_refresh_idle_warp", fake_refresh)
    monkeypatch.setattr(time, "time", lambda: 0.0)

    daemon._retry_refresh_idle_warp({"tag": "WARP-CF-C"})
    assert st["refresh"] == 1, "首轮忙之后必须重试并刷新成功"


def test_retry_loop_keeps_trying_while_busy(monkeypatch):
    """恒忙时必须重试多次，而非一次即弃。"""
    st = {"wait": 0}
    monkeypatch.setattr(daemon, "_wait_warp_idle",
                        lambda m: (st.__setitem__("wait", st["wait"] + 1), False)[1])
    monkeypatch.setattr(daemon, "_refresh_idle_warp", lambda m: None)

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

    clock = {"t": 0.0}

    def tick():
        clock["t"] += 100.0
        return clock["t"]

    monkeypatch.setattr(time, "time", tick)
    daemon._retry_refresh_idle_warp({"tag": "WARP-CF-C"})
    assert st["refresh"] > 1, f"应重试多次，实际 {st['refresh']}"


def test_failback_is_wired_into_main_loop():
    """反向兜底（池挂 → 切回 WARP）必须真的挂在主循环上。

    主循环是单体 main()，无法单测；这里退而检查调用点存在，
    防止重构时把它删掉而所有函数级测试依然全绿。
    """
    src = open('daemon.py', encoding='utf-8').read()
    assert "_maybe_failback_to_warp(now)" in src, "主循环未调用反向兜底"


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
