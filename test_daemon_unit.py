#!/usr/bin/env python3
"""auto-switch-ip 确定性单元测试（无网络、<30s）。

覆盖核心决策逻辑，替代 test_auto_switch.py 的慢速 live 集成测试：
- get_9router_error_rate：两层错误检测
- _update_strategy_mode：自适应回滞切换
- _delay_score / _p2c_select：节点选择评分
- _apply_test_results：坏节点标记 + TTL 升级 + 安全阀
- is_singbox_alive / restart_singbox：看门狗 + 重启互斥
"""
import json
import os
import sqlite3
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import pytest

import config
import daemon

# 夹具 `_isolate_production_files` 会把 `_warp_model_usable` 打桩成恒真；
# 验它本身的用例要拿回未打桩的原函数（模块加载期的引用）。
_REAL_WARP_MODEL_USABLE = daemon._warp_model_usable


@pytest.fixture(autouse=True)
def _isolate_production_files(tmp_path, monkeypatch):
    """把测试会写到的生产文件全部重定向到 tmp。

    1) egress_log：switch_proxy() 成功后会调 _record_egress() 写生产
       .egress_log.jsonl（实测泄漏过 15 条 "new-node-x"）。
    2) daemon.log：log() 会写到生产日志，注入伪造的 "✗ nN 死" 行，而
       self_evolve_checkpoint 的 repeat_failure_nodes 检查正是 grep 该文件 ——
       实测污染出 7 个不存在的节点（n0~n6）被判为重复失败。
    """
    import logging
    monkeypatch.setenv("EGRESS_LOG_FILE_PATH", str(tmp_path / "egress.jsonl"))
    # warp_ip_used：_mark_warp_ip_used 会落盘，不隔离则测试桩地址
    # （ip-w1 / burned-ip 之类）会写进生产 .warp_ip_used.json（实测已发生）。
    monkeypatch.setenv("WARP_IP_USED_FILE_PATH", str(tmp_path / "warp_ip_used.json"))
    # warp_ip_cycles：_record_warp_cycle 会落盘，同样必须隔离。
    monkeypatch.setenv("WARP_IP_CYCLES_FILE_PATH", str(tmp_path / "warp_ip_cycles.json"))
    # warp_ip_history：_mark_warp_ip_used 会追加历史，不隔离则桩地址写进生产
    # .warp_ip_history.jsonl —— 该文件永不清理，污染是永久的。
    monkeypatch.setenv("WARP_IP_HISTORY_FILE_PATH", str(tmp_path / "warp_ip_history.jsonl"))
    # node_ip_used：_record_node_ip_used 会落盘，不隔离则桩地址写进生产
    # .node_ip_used.json，下次真实 restart 时把生产节点误判为「6h 内用过」。
    monkeypatch.setenv("NODE_IP_USED_FILE_PATH", str(tmp_path / "node_ip_used.json"))
    monkeypatch.setattr(daemon, "_NODE_IP_USED", {})
    # proxy_fallback：落池成功会落盘，不隔离则测试会写生产文件、且下一次
    # 测试会读到真实落池时刻而误入冷却。
    monkeypatch.setenv("WARP_PROXY_FALLBACK_FILE_PATH", str(tmp_path / "proxy_fallback.json"))
    monkeypatch.setattr(daemon, "_last_proxy_fallback_ts", 0.0)
    monkeypatch.setattr(daemon, "_proxy_fallback_ts_loaded", False)
    monkeypatch.setattr(daemon, "_WARP_IP_CYCLES", [])
    # Clash selector 快照是进程级缓存：前一个用例留下的 "now" 会让 active_warp_tag()
    # 跳过 sing-box 直接命中，而单跑时快照为空、走真实查询 —— 同一用例两种结果。
    monkeypatch.setattr(daemon, "_CLASH_SNAP", {})
    # 探测台账与离渠计数是进程级全局：不隔离则前一个用例的探测结论会
    # 挡住下一个用例的真实探测（被当成已探过而跳过），离渠计数也会跨用例累积。
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    monkeypatch.setattr(daemon, "_WARP_OFF_AGE", {})
    monkeypatch.setattr(daemon, "_WARP_REFRESH_COUNT", {})
    # 验收时刻是进程级全局，`_reclaim_dirty_pass` 在它之后 12s 内跳过该线。
    # 不隔离时前一个用例留下的真实 time.time() 会让后续用例的脏线巡检静默
    # 少派一条（实测 test_dirty_janitor_reclaims_all_non_active_lines 按文档
    # 顺序与 test_warp_refresh.py 合跑时 launched 只剩 ['W3']）。
    monkeypatch.setattr(daemon, "_WARP_LAST_ACCEPT", {})
    # 模型层探测会真实发请求（每次烧一格出口配额），默认放行；需要验它的
    # 用例自行重新打桩。
    monkeypatch.setattr(daemon, "_warp_model_usable", lambda m, ip: True)
    # 本文件测的是原模式（IP 新鲜度即判据）的换线语义；模型模式在
    # test_warp_refresh.py 单独覆盖。不关掉的话历史用例会被「历史不再否决」
    # 放行，测的就不是它们声称的东西了。
    monkeypatch.setitem(daemon.CFG["warp"], "refresh_model_check", False)
    lg = logging.getLogger("auto-switch-ip")
    saved = lg.handlers[:]
    lg.handlers = []
    monkeypatch.setattr(daemon, "_logger", None)      # 强制重新 _init_logger
    monkeypatch.setitem(daemon.CFG, "log_file", str(tmp_path / "daemon.log"))

    # 拦截 selector 写入：漏 stub _switch_warp 的测试会真的翻转生产
    # opencode-out（实测发生过），必须 fail 而非静默改线上流量走向。
    # 只拦写操作 —— 读操作（active_warp_tag 等）无副作用，放行。
    _real_api = daemon.singbox_api

    def _guarded_api(path, method="GET", body=None, **kw):
        if method != "GET":
            raise AssertionError(
                f"测试试图写真实 sing-box: {method} {path} {body}。"
                "请 monkeypatch _switch_warp / switch_proxy 等。"
            )
        return _real_api(path, method, body, **kw)

    monkeypatch.setattr(daemon, "singbox_api", _guarded_api)

    yield
    for h in lg.handlers:
        h.close()
    lg.handlers = saved


# ============= 1. get_9router_error_rate（mock _get_db） =============
def test_错误率两层检测_窗口与连续(monkeypatch):
    # 窗口是配置项（recent_window），测试必须钉死自己构造的数据条数，
    # 否则 config 一改就变成「测的是窗口大小」而不是「测的是检测逻辑」。
    monkeypatch.setattr(daemon, "RECENT_WINDOW", 10)
    schema = "CREATE TABLE requestDetails (provider TEXT, model TEXT, status TEXT, timestamp REAL, data TEXT)"
    mem = sqlite3.connect(":memory:")
    mem.execute(schema)
    mem.row_factory = sqlite3.Row
    now = 1_700_000_000
    # 错误行必须带出口级关键词：402/超长按设计不计入出口错误（见 _classify_egress_error）
    e429 = json.dumps({"response": {"status": 429, "error": json.dumps(
        {"error": {"type": "FreeUsageLimitError",
                   "message": "Rate limit exceeded. Please try again later."}})}})
    rows = [
        # 最近 10 条：3 errors + 连续 2 失败结尾
        ("opencode", "mimo", "error", now, e429),          # 连续 1
        ("opencode", "mimo", "error", now - 1, e429),       # 连续 2
        ("opencode", "mimo", "success", now - 2, None),
        ("opencode", "mimo", "success", now - 3, None),
        ("opencode", "mimo", "error", now - 4, e429),       # 第 3 个 error
        ("opencode", "mimo", "success", now - 5, None),
        ("opencode", "mimo", "success", now - 6, None),
        ("opencode", "mimo", "success", now - 7, None),
        ("opencode", "mimo", "success", now - 8, None),
        ("opencode", "mimo", "success", now - 9, None),
        ("opencode", "mimo", "success", now - 10, None),
        # 非 opencode provider：必须被排除
        ("kiro", "x", "error", now - 0.5, e429),
        ("nvidia", "x", "error", now - 0.6, e429),
    ]
    mem.executemany("INSERT INTO requestDetails (provider,model,status,timestamp,data) VALUES (?,?,?,?,?)", rows)

    orig = daemon._get_db
    daemon._get_db = lambda: mem
    try:
        total, errors, _, consecutive, _ = daemon.get_9router_error_rate()
    finally:
        daemon._get_db = orig

    assert total == 10, f"total 应为 10（仅 opencode），实际 {total}"
    assert errors == 3, f"errors 应为 3，实际 {errors}"
    assert consecutive == 2, f"consecutive 应为 2，实际 {consecutive}"


def test_错误率_全成功返回零(monkeypatch):
    monkeypatch.setattr(daemon, "RECENT_WINDOW", 10)
    schema = "CREATE TABLE requestDetails (provider TEXT, model TEXT, status TEXT, timestamp REAL, data TEXT)"
    mem = sqlite3.connect(":memory:")
    mem.execute(schema)
    mem.row_factory = sqlite3.Row
    mem.executemany(
        "INSERT INTO requestDetails (provider,model,status,timestamp) VALUES (?,?,?,?)",
        [("opencode", "m", "success", 1_700_000_000 - i) for i in range(10)],
    )
    orig = daemon._get_db
    daemon._get_db = lambda: mem
    try:
        total, errors, _, consecutive, _ = daemon.get_9router_error_rate()
    finally:
        daemon._get_db = orig
    assert total == 10 and errors == 0 and consecutive == 0, (total, errors, consecutive)


def test_get_db_重开被替换的数据库文件(monkeypatch, tmp_path):
    """9router 重启/迁移替换 data.sqlite 后，旧连接会读已删除的 inode。

    现象：错误率与配额计数永久冻结（2026-10-26 实测 total=10/errors=2、
    轮换W=0/150 连续 20 分钟不变）。按文件身份检测并重开。
    """
    a = tmp_path / "a.sqlite"
    b = tmp_path / "b.sqlite"
    ca = sqlite3.connect(a)
    ca.execute("CREATE TABLE old_marker(x)")
    ca.commit()
    ca.close()
    cb = sqlite3.connect(b)
    cb.execute("CREATE TABLE new_marker(x)")
    cb.commit()
    cb.close()

    monkeypatch.setattr(daemon, "NODE_INFO_DB", str(a))
    monkeypatch.setattr(daemon, "_db_conn", None)
    monkeypatch.setattr(daemon, "_db_file_id", None)

    old_conn = daemon._get_db()
    assert old_conn.execute("SELECT name FROM sqlite_master WHERE name='old_marker'").fetchone()

    os.replace(b, a)  # 新文件换入同一路径，旧连接仍持有被删 inode

    new_conn = daemon._get_db()
    assert new_conn is not old_conn, "文件被替换后必须重开连接"
    assert new_conn.execute("SELECT name FROM sqlite_master WHERE name='new_marker'").fetchone()


def test_错误率_切换点过滤_旧节点错误不背锅(monkeypatch):
    """新节点上线后，切换点之前的旧节点错误不应触发拉黑"""
    monkeypatch.setattr(daemon, "RECENT_WINDOW", 10)
    schema = "CREATE TABLE requestDetails (provider TEXT, model TEXT, status TEXT, timestamp REAL, data TEXT)"
    mem = sqlite3.connect(":memory:")
    mem.execute(schema)
    mem.row_factory = sqlite3.Row
    # 生产表 timestamp 为 ISO TEXT（'2026-10-16T17:33:48.877Z'），fixture 须对齐
    iso = lambda epoch: datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    e429 = json.dumps({"response": {"status": 429, "error": json.dumps(
        {"error": {"type": "FreeUsageLimitError",
                   "message": "Rate limit exceeded. Please try again later."}})}})
    switch_ts = 1_700_000_000 - 5  # 5 秒前切换
    rows = [
        # 切换点之后：2 success + 1 error
        ("opencode", "m", "success", iso(switch_ts + 1), None),
        ("opencode", "m", "success", iso(switch_ts + 2), None),
        ("opencode", "m", "error",   iso(switch_ts + 3), e429),
        # 切换点之前：全 error（旧节点的锅）
        ("opencode", "m", "error", iso(switch_ts - 1), e429),
        ("opencode", "m", "error", iso(switch_ts - 2), e429),
    ]
    mem.executemany("INSERT INTO requestDetails (provider,model,status,timestamp,data) VALUES (?,?,?,?,?)", rows)
    orig = daemon._get_db
    daemon._get_db = lambda: mem
    try:
        # 不过滤：5 条中 3 error
        total_all, errors_all, _, _, _ = daemon.get_9router_error_rate()
        assert errors_all == 3, f"无过滤应 3 errors, got {errors_all}"
        # 过滤切换点后：只剩切换后的 3 条，其中 1 error
        total_f, errors_f, _, _, _ = daemon.get_9router_error_rate(max_backoff_ts=switch_ts)
        assert total_f == 3, f"过滤后应 3 条, got {total_f}"
        assert errors_f == 1, f"过滤后应 1 error, got {errors_f}"
    finally:
        daemon._get_db = orig

def test_error_rate_counts_403_client_rejections(monkeypatch):
    monkeypatch.setattr(daemon, "RECENT_WINDOW", 10)
    schema = "CREATE TABLE requestDetails (provider TEXT, model TEXT, status TEXT, timestamp REAL, data TEXT)"
    mem = sqlite3.connect(":memory:")
    mem.execute(schema)
    mem.row_factory = sqlite3.Row
    import json as _json
    # error 文本必须带出口级关键词：空 "{}" 会被判成 other（402/超长同类），
    # 按设计不计入出口错误，403 占比统计仍走 response.status，不受影响。
    e403 = _json.dumps({"status": "error", "response": {
        "status": 403, "error": _json.dumps(
            {"error": {"message": "detected abusive traffic from this IP address"}})}})
    e429 = _json.dumps({"status": "error", "response": {
        "status": 429, "error": _json.dumps(
            {"error": {"type": "FreeUsageLimitError",
                       "message": "Rate limit exceeded. Please try again later."}})}})
    rows = [
        ("opencode", "m", "error", 5, e403),
        ("opencode", "m", "error", 4, e429),
        ("opencode", "m", "error", 3, e403),
        ("opencode", "m", "success", 2, None),
    ]
    mem.executemany(
        "INSERT INTO requestDetails (provider,model,status,timestamp,data) VALUES (?,?,?,?,?)", rows
    )
    orig = daemon._get_db
    daemon._get_db = lambda: mem
    try:
        total, errors, _, _, rejected = daemon.get_9router_error_rate()
    finally:
        daemon._get_db = orig
    assert total == 4 and errors == 3, (total, errors)
    assert abs(rejected - 2 / 3) < 1e-9, f"403 占比应为 2/3，实际 {rejected}"


def test_error_rate_upstream_402_does_not_count_as_error(monkeypatch):
    """上游 402 与请求超长不算出口错误，也不打断连续失败的计数。

    旧口径 status != "success" 全算错误：recent_window=2 下一条 402 + 一条
    超长即凑满整窗全错 → full_err → 立刻换线。换出口解不了这两类错误。
    """
    monkeypatch.setattr(daemon, "RECENT_WINDOW", 10)
    schema = "CREATE TABLE requestDetails (provider TEXT, model TEXT, status TEXT, timestamp REAL, data TEXT)"
    mem = sqlite3.connect(":memory:")
    mem.execute(schema)
    mem.row_factory = sqlite3.Row
    now = 1_700_000_000
    e402 = json.dumps({"response": {"status": 402,
                                    "error": "Endpoint is unavailable"}})
    toolong = json.dumps({"response": {"error": "maximum context length is 128000 tokens"}})
    mem.executemany(
        "INSERT INTO requestDetails (provider,model,status,timestamp,data) VALUES (?,?,?,?,?)",
        [("opencode", "longcat-2.5-preview-free", "error", now - i, body)
         for i, body in enumerate((e402, toolong, e402, toolong, e402))],
    )
    orig = daemon._get_db
    daemon._get_db = lambda: mem
    try:
        total, errors, _, consecutive, _ = daemon.get_9router_error_rate()
    finally:
        daemon._get_db = orig
    assert total == 5, f"total 应为 5，实际 {total}"
    assert errors == 0, f"非出口错误不计 errors，实际 {errors}"
    assert consecutive == 0, f"非出口错误不断连，实际 {consecutive}"

# ============= 2. _update_strategy_mode（纯函数） =============
def test_策略_normal_to_pressure():
    assert daemon._update_strategy_mode("normal", 0.4, 5, 0.3, 0.1) == "pressure"
    # 样本不足（total<3）不切
    assert daemon._update_strategy_mode("normal", 0.9, 2, 0.3, 0.1) == "normal"
    # 未达压力阈值不切
    assert daemon._update_strategy_mode("normal", 0.2, 10, 0.3, 0.1) == "normal"


def test_策略_pressure_to_normal():
    # 降到回滞阈值以下
    assert daemon._update_strategy_mode("pressure", 0.1, 10, 0.3, 0.1) == "normal"
    # 样本不足也回 normal
    assert daemon._update_strategy_mode("pressure", 0.5, 2, 0.3, 0.1) == "normal"
    # 仍高于阈值保持 pressure
    assert daemon._update_strategy_mode("pressure", 0.5, 10, 0.3, 0.1) == "pressure"


# ============= 3. _delay_score / _p2c_select =============
def test_delay_score_分水岭形状():
    delays = {"a": 100, "b": 300, "c": 1000}
    median, dmin, dmax = daemon._precompute_delay_stats(list(delays.values()))
    # d <= median：平缓，fast 接近 1.0
    assert daemon._delay_score("a", delays, median, dmin, dmax) > daemon._delay_score("b", delays, median, dmin, dmax)
    # d > median：陡峭，慢节点接近 0
    slow = daemon._delay_score("c", delays, median, dmin, dmax)
    assert slow < 0.5, f"慢节点应被陡降，实际 {slow}"
    # median=0 → 全部 1.0（防除零）
    assert daemon._delay_score("a", delays, 0, 0, 0) == 1.0
    # 缺失延迟 → 0.1（保守惩罚）
    assert daemon._delay_score("missing", delays, median, dmin, dmax) == 0.1


def test_p2c_select_normal_picks_lowest_delay():
    daemon._NODE_DELAYS = {"fast": 50, "slow": 500, "mid": 200}
    daemon._NODE_CALL_COUNTS = {}
    daemon._strategy_mode = "normal"
    candidates = ["fast", "slow", "mid"]
    # P2C 随机抽 2 取最优，统计偏好（fast 延迟最低应占压倒多数）
    picks = [daemon._p2c_select(candidates, set(), {}) for _ in range(500)]
    fast_ratio = picks.count("fast") / len(picks)
    # P2C 抽 2 取最优：fast 延迟最低，在 fast 入对时必胜，占 2/3（仅 slow-mid 对落败）。
    assert fast_ratio >= 0.6, f"normal 模式 fast 应≥60%（P2C 理论上限 2/3），实际 {fast_ratio:.0%}"


def test_p2c_select_pressure_picks_least_called():
    daemon._NODE_DELAYS = {"a": 100, "b": 100, "c": 100}
    daemon._NODE_CALL_COUNTS = {"a": 50, "b": 10, "c": 0}
    daemon._strategy_mode = "pressure"
    candidates = ["a", "b", "c"]
    picks = [daemon._p2c_select(candidates, set(), {}) for _ in range(500)]
    c_ratio = picks.count("c") / len(picks)
    # pressure 模式 c 调用最少，应占压倒多数（仅在 c 未入抽样对时才可能落选）
    assert c_ratio >= 0.6, f"pressure 模式 c 应≥60%，实际 {c_ratio:.0%}"


# ============= 4. _apply_test_results（坏节点 + TTL + 安全阀） =============
def test_apply_test_results_坏节点标记与TTL升级():
    daemon._NODE_DELAYS = {}
    daemon._DEAD_NODES = set()
    daemon._DEAD_NODES_RELEASE_TS = {}
    daemon._dead_fail_count = {}
    nodes = ["n1", "n2", "n3", "n4", "n5"]
    now = 1_700_000_000
    # 两次失败同一节点 → TTL 升级
    daemon._apply_test_results(nodes, {"n1": None}, now)
    assert "n1" in daemon._DEAD_NODES, "首次失败应入黑名单"
    ttl1 = daemon._DEAD_NODES_RELEASE_TS["n1"] - now
    assert ttl1 == daemon._DEAD_TTL_STAGES[0], f"首次 TTL 应为 stage0={daemon._DEAD_TTL_STAGES[0]}s，实际 {ttl1}"
    daemon._apply_test_results(nodes, {"n1": None}, now)
    ttl2 = daemon._DEAD_NODES_RELEASE_TS["n1"] - now
    assert ttl2 == daemon._DEAD_TTL_STAGES[1], f"二次 TTL 应为 stage1={daemon._DEAD_TTL_STAGES[1]}s，实际 {ttl2}"
    assert ttl2 > ttl1, f"二次失败 TTL 应更长: {ttl1} -> {ttl2}"
    # 成功节点复活
    daemon._apply_test_results(nodes, {"n2": 200}, now)
    assert "n2" not in daemon._DEAD_NODES


def test_apply_test_results_安全阀防全拉黑():
    daemon._NODE_DELAYS = {}
    daemon._DEAD_NODES = set()
    daemon._DEAD_NODES_RELEASE_TS = {}
    daemon._dead_fail_count = {}
    # 10 节点：保底 30%，拉黑上限 = 10*(1-0.3) = int(7.0) = 7 个。
    # alive_count <= len(nodes)*ratio 时跳过拉黑：alive=3 → 3<=3.0 触发阀门保护，第 8 个不进。
    nodes = [f"n{i}" for i in range(10)]
    now = 1_700_000_000
    expected_max_dead = int(10 * (1 - daemon._POOL_MIN_RATIO))  # 7
    for i in range(expected_max_dead):
        daemon._apply_test_results(nodes, {f"n{i}": None}, now)
    assert len(daemon._DEAD_NODES) == expected_max_dead, f"应入黑名单 {expected_max_dead} 个，实际 {len(daemon._DEAD_NODES)}"
    # 第 (expected_max_dead+1) 个：alive=3 <= 3.0 → 阀门保护，不拉黑
    daemon._apply_test_results(nodes, {f"n{expected_max_dead}": None}, now)
    assert f"n{expected_max_dead}" not in daemon._DEAD_NODES, f"安全阀应阻止超上限拉黑 (dead={len(daemon._DEAD_NODES)})"


# ============= 5. 看门狗 is_singbox_alive / restart_singbox =============
def test_is_singbox_alive_api_down_pid_present_triggers_restart():
    orig_api = daemon.singbox_api
    orig_pids = daemon._singbox_pids
    daemon.singbox_api = lambda *a, **k: (0, None)   # API 不通
    daemon._singbox_pids = lambda: [12345]            # 进程还在
    try:
        assert daemon.is_singbox_alive() is False
    finally:
        daemon.singbox_api = orig_api
        daemon._singbox_pids = orig_pids


def test_is_singbox_alive_api_ok_port_refused_returns_false():
    import socket as _sock
    orig_api = daemon.singbox_api
    orig_pids = daemon._singbox_pids
    orig_conn = _sock.create_connection
    daemon.singbox_api = lambda *a, **k: (200, {"proxies": {}})
    daemon._singbox_pids = lambda: [12345]
    _sock.create_connection = lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError())
    try:
        assert daemon.is_singbox_alive() is False
    finally:
        daemon.singbox_api = orig_api
        daemon._singbox_pids = orig_pids
        _sock.create_connection = orig_conn


def test_is_singbox_alive_api_ok_port_ok_returns_true():
    import socket as _sock
    orig_api = daemon.singbox_api
    orig_pids = daemon._singbox_pids
    orig_conn = _sock.create_connection
    daemon.singbox_api = lambda *a, **k: (200, {"proxies": {}})

    class _FakeSock:
        def close(self): pass
    daemon._singbox_pids = lambda: [12345]
    _sock.create_connection = lambda *a, **k: _FakeSock()
    try:
        assert daemon.is_singbox_alive() is True
    finally:
        daemon.singbox_api = orig_api
        daemon._singbox_pids = orig_pids
        _sock.create_connection = orig_conn


# ---- 连续失败门控：单次抖动不得重启 ----
class _FakeTime:
    """可推进的假时钟，同时接管 `time.sleep`。

    `_probe_refresh_egress` 现在是「探测→睡 1s→再探测」的轮询，用真时钟要么
    真睡几十秒、要么让截止时间永不前进。睡眠同步推进 `current`，于是轮询次数
    也成了确定性的事实。
    """

    def __init__(self, start: float = 0.0, step_on_sleep: float = 8.0):
        self.current = start
        self.step_on_sleep = step_on_sleep

    def time(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += max(seconds, self.step_on_sleep)

def _stub_probe(monkeypatch, results):
    """把 is_singbox_alive 换成按脚本返回，并抹掉重试等待。"""
    seq = list(results)
    monkeypatch.setattr(daemon, "is_singbox_alive", lambda: seq.pop(0))
    monkeypatch.setattr(daemon, "_PROBE_RETRY_GAP_SEC", 0)
    return seq


def test_probe_survives_single_negative(monkeypatch):
    """单次探测失败不算死：第二次就通了必须存活，不许重启。"""
    _stub_probe(monkeypatch, [False, True])
    assert daemon._probe_singbox_with_retry() is True


def test_probe_dies_only_after_three_consecutive_failures(monkeypatch):
    _stub_probe(monkeypatch, [False, False, False, True])
    assert daemon._probe_singbox_with_retry() is False


def test_watchdog_does_not_restart_on_one_blip(monkeypatch):
    """红线：服务探测必须 3 次连续失败才标 down（单次超时不算）。"""
    _stub_probe(monkeypatch, [False, True])
    restarts = []
    monkeypatch.setattr(daemon, "restart_singbox", lambda: restarts.append(1) or True)
    daemon._RESTART_COUNT = 0
    daemon._singbox_watchdog()
    assert restarts == [], "单次探测抖动触发了 sing-box 重启"


def test_watchdog_restarts_after_three_consecutive_failures(monkeypatch):
    _stub_probe(monkeypatch, [False, False, False])
    restarts = []
    monkeypatch.setattr(daemon, "restart_singbox", lambda: restarts.append(1) or True)
    daemon._RESTART_COUNT = 0
    daemon._singbox_watchdog()
    assert restarts == [1]


def test_restart_singbox_refuses_concurrent_reentry():
    daemon._restart_lock = threading.Lock()
    daemon._RESTART_COUNT = 0
    # 持锁：第二次 acquire(blocking=False) 应失败
    daemon._restart_lock.acquire()
    try:
        assert daemon.restart_singbox() is False, "持锁时应拒绝并发重启"
    finally:
        daemon._restart_lock.release()


# ============= 6. switch_proxy PUT 验证（双端 PUT 契约） =============
def test_switch_proxy_put_ok_then_verify():
    """PUT 返回 204 且 GET 验证 now==new_node → 成功"""
    calls = {"put": 0}

    def fake_api(path, method="GET", body=None, **kw):
        if method == "PUT":
            calls["put"] += 1
            return 204, None
        # GET /proxies/proxy → 验证 now
        return 200, {"now": "new-node-x"}

    orig = daemon.singbox_api
    daemon.singbox_api = fake_api
    daemon._RESTART_COUNT = 0
    try:
        assert daemon.switch_proxy("new-node-x") is True
        assert calls["put"] == 1
    finally:
        daemon.singbox_api = orig


def test_switch_proxy_put_failed_status_returns_false():
    """PUT 返回非 2xx → 立即失败，不进入验证"""
    def fake_api(path, method="GET", body=None, **kw):
        if method == "PUT":
            return 502, None
        return 200, {"now": "other"}
    orig = daemon.singbox_api
    daemon.singbox_api = fake_api
    try:
        assert daemon.switch_proxy("n") is False
    finally:
        daemon.singbox_api = orig


def test_switch_proxy_verify_mismatch_returns_false():
    """PUT 2xx 但 GET 验证 now != new_node → 失败（双端一致性）"""
    def fake_api(path, method="GET", body=None, **kw):
        if method == "PUT":
            return 200, None
        return 200, {"now": "stale-node"}  # now 未更新
    orig = daemon.singbox_api
    daemon.singbox_api = fake_api
    try:
        assert daemon.switch_proxy("new-node") is False
    finally:
        daemon.singbox_api = orig


# ============= 7. _RESTART_COUNT 计数逻辑（防重启风暴安全阀） =============
def test_restart_singbox_increments_count_on_failure():
    daemon._restart_lock = threading.Lock()
    daemon._RESTART_COUNT = 0
    orig_sub = daemon.subprocess.run
    orig_popen = daemon.subprocess.Popen
    orig_set = daemon._set_outbound_proxy
    orig_alive = daemon.is_singbox_alive
    orig_sleep = daemon.time.sleep
    daemon.subprocess.run = lambda *a, **k: None
    daemon.subprocess.Popen = lambda *a, **k: None
    daemon._set_outbound_proxy = lambda *a, **k: None
    daemon.is_singbox_alive = lambda: False  # 重启后探测失败
    daemon.time.sleep = lambda *a, **k: None
    try:
        ok = daemon.restart_singbox()
        assert ok is False, "重启后探测失败应返回 False"
        assert daemon._RESTART_COUNT == 1, f"失败应计数 +1，实际 {daemon._RESTART_COUNT}"
    finally:
        daemon.subprocess.run = orig_sub
        daemon.subprocess.Popen = orig_popen
        daemon._set_outbound_proxy = orig_set
        daemon.is_singbox_alive = orig_alive
        daemon.time.sleep = orig_sleep


def test_restart_singbox_resets_count_on_success():
    daemon._restart_lock = threading.Lock()
    daemon._RESTART_COUNT = 2
    orig_sub = daemon.subprocess.run
    orig_popen = daemon.subprocess.Popen
    orig_set = daemon._set_outbound_proxy
    orig_alive = daemon.is_singbox_alive
    orig_sleep = daemon.time.sleep
    orig_switch = daemon._switch_warp
    orig_mode = daemon._current_mode
    daemon.subprocess.run = lambda *a, **k: None
    daemon.subprocess.Popen = lambda *a, **k: None
    daemon._set_outbound_proxy = lambda *a, **k: None
    daemon.is_singbox_alive = lambda: True  # 重启后探测成功
    daemon.time.sleep = lambda *a, **k: None
    # 重启成功后 daemon 会恢复重启前的出口（_switch_warp → PUT selector）。
    # 测试不得写到真实 sing-box，此处打桩。
    calls = []
    daemon._switch_warp = lambda tag: calls.append(tag) or True
    # 重启前停在轮换池：selector 会回到 default(WARP-CF)，必须切回 proxy，
    # 否则一次重启就把出口静默从池换成 WARP。本用例显式打开落池开关。
    orig_fallback_enabled = daemon.CFG["warp"]["proxy_fallback_enabled"]
    daemon.CFG["warp"]["proxy_fallback_enabled"] = True
    daemon._current_mode = lambda: ("proxy", None)
    try:
        ok = daemon.restart_singbox()
        assert ok is True, "重启后探测成功应返回 True"
        assert daemon._RESTART_COUNT == 0, f"成功应清零计数，实际 {daemon._RESTART_COUNT}"
        assert calls == ["proxy"], f"重启前在池应切回 proxy，实际 {calls}"
    finally:
        daemon.CFG["warp"]["proxy_fallback_enabled"] = orig_fallback_enabled
        daemon.subprocess.run = orig_sub
        daemon.subprocess.Popen = orig_popen
        daemon._set_outbound_proxy = orig_set
        daemon.is_singbox_alive = orig_alive
        daemon.time.sleep = orig_sleep
        daemon._switch_warp = orig_switch
        daemon._current_mode = orig_mode


def test_probe_active_ip_discards_ip_when_selector_moved_during_probe():
    """探测期间换线时，旧线的 IP 不能记到新线上。"""
    orig_egress, orig_tag, orig_member = (
        daemon.warp_egress_ip, daemon.active_warp_tag, daemon._warp_member)
    daemon._warp_member = lambda tag: {"tag": tag, "port": 1080}
    daemon.warp_egress_ip = lambda port, timeout_sec=20.0: "2a09::old"
    daemon.active_warp_tag = lambda: "WARP-CF-B"   # 探测期间已切走
    try:
        assert daemon._probe_active_ip("WARP-CF") is None, "换线后应丢弃旧线的探测结果"
        daemon.active_warp_tag = lambda: "WARP-CF"  # 未变
        assert daemon._probe_active_ip("WARP-CF") == "2a09::old", "未换线应采信探测结果"
    finally:
        daemon.warp_egress_ip = orig_egress
        daemon.active_warp_tag = orig_tag
        daemon._warp_member = orig_member


def test_restart_singbox_keeps_mode_when_previous_unknown():
    """查不到重启前模式时不得擅自动 selector —— 看门狗重启前的 sing-box 本来就是死的。"""
    daemon._restart_lock = threading.Lock()
    orig = (daemon.subprocess.run, daemon.subprocess.Popen, daemon._set_outbound_proxy,
            daemon.is_singbox_alive, daemon.time.sleep, daemon._switch_warp, daemon._current_mode)
    calls = []
    daemon.subprocess.run = lambda *a, **k: None
    daemon.subprocess.Popen = lambda *a, **k: None
    daemon._set_outbound_proxy = lambda *a, **k: None
    daemon.is_singbox_alive = lambda: True
    daemon.time.sleep = lambda *a, **k: None
    daemon._switch_warp = lambda tag: calls.append(tag) or True
    daemon._current_mode = lambda: ("unknown", None)
    try:
        assert daemon.restart_singbox() is True, "重启后探测成功应返回 True"
        assert calls == [], f"模式未知时不应切 selector，实际切到 {calls}"
    finally:
        (daemon.subprocess.run, daemon.subprocess.Popen, daemon._set_outbound_proxy,
         daemon.is_singbox_alive, daemon.time.sleep, daemon._switch_warp,
         daemon._current_mode) = orig


# ============= 8. get_per_egress_error_rate（出口归属） =============
def test_per_egress_error_rate_按节点分桶():
    """请求按 .egress_log.jsonl 切换点归属到当时活跃节点，各节点独立统计。"""
    base = 1_787_443_300  # 锚点 epoch
    # 切换序列：base 切到 A，base+200 切到 B
    switches = [
        {"ts": base, "node": "A"},
        {"ts": base + 200, "node": "B"},
    ]
    # 请求：A 期间(base+50,100,150 → 2 成功 1 失败)；B 期间(base+250,300 → 1 成功 1 失败)
    from datetime import datetime, timezone, timedelta
    def iso(delta):
        return (datetime.fromtimestamp(base + delta, tz=timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    reqs = [
        (iso(50), "success"),
        (iso(100), "error"),
        (iso(150), "success"),
        (iso(250), "error"),
        (iso(300), "success"),
    ]
    import types
    # mock conn: c.execute(...).fetchall() -> reqs
    class _Cursor:
        def fetchall(self_inner):
            return [(ts, st) for ts, st in reqs]
    class _Conn:
        def execute(self_inner, sql, params=None):
            return _Cursor()

    def fake_db():
        return _Conn()

    orig_db, orig_log = daemon._get_db, daemon._load_egress_log
    daemon._get_db = fake_db
    daemon._load_egress_log = lambda: switches
    try:
        rates = daemon.get_per_egress_error_rate(window_min=60)
    finally:
        daemon._get_db = orig_db
        daemon._load_egress_log = orig_log
    assert "A" in rates and "B" in rates, f"应含 A/B，实际 {list(rates)}"
    a_total, a_err, a_rate = rates["A"]
    b_total, b_err, b_rate = rates["B"]
    assert a_total == 3 and a_err == 1, f"A 应为 3/1，实际 {a_total}/{a_err}"
    assert b_total == 2 and b_err == 1, f"B 应为 2/1，实际 {b_total}/{b_err}"
    assert abs(a_rate - 1/3) < 1e-9, f"A rate 应为 1/3，实际 {a_rate}"
    assert len(rates) == 2, f"仅 2 个有归属节点，实际 {len(rates)}"


def test_per_egress_error_rate_无切换日志返回空():
    orig_db, orig_log = daemon._get_db, daemon._load_egress_log

    class _Cur:
        def fetchall(self):
            return [("2026-08-23T00:00:00+00:00", "success")]

    class _Conn:
        def execute(self, sql, params=None):
            return _Cur()

    def fake_db():
        return _Conn()

    daemon._get_db = fake_db
    daemon._load_egress_log = lambda: []
    try:
        rates = daemon.get_per_egress_error_rate()
    finally:
        daemon._get_db = orig_db
        daemon._load_egress_log = orig_log
    assert rates == {}, f"无切换日志应返回空，实际 {rates}"


# ============= 9. egress_log 读写隔离与裁剪 =============
def test_egress_log_read_does_not_write(tmp_path, monkeypatch):
    """读取路径不得写文件 —— 原实现的 read-modify-write 会丢掉期间的 append，
    导致 _active_node_at 把请求归因给错误的节点。"""
    import json as _json
    f = tmp_path / "egress.jsonl"
    old = int(time.time()) - 8 * 86400
    f.write_text(_json.dumps({"ts": old, "node": "ancient"}) + "\n"
                 + _json.dumps({"ts": int(time.time()), "node": "fresh"}) + "\n")
    monkeypatch.setenv("EGRESS_LOG_FILE_PATH", str(f))

    mtime_before = f.stat().st_mtime
    got = daemon._load_egress_log()
    assert [d["node"] for d in got] == ["fresh"], "只应返回窗口内记录"
    assert f.stat().st_mtime == mtime_before, "读取不应修改文件"
    assert "ancient" in f.read_text(), "读取不应删除记录"


def test_prune_egress_log_preserves_appended(tmp_path, monkeypatch):
    """prune 必须保留 rename 窗口内新追加的记录。"""
    import json as _json
    f = tmp_path / "egress.jsonl"
    old = int(time.time()) - 8 * 86400
    f.write_text(_json.dumps({"ts": old, "node": "ancient"}) + "\n"
                 + _json.dumps({"ts": int(time.time()), "node": "fresh"}) + "\n")
    monkeypatch.setenv("EGRESS_LOG_FILE_PATH", str(f))

    removed = daemon.prune_egress_log()
    assert removed == 1, f"应移除 1 条过期记录, 实际 {removed}"
    nodes = [_json.loads(l)["node"] for l in f.read_text().splitlines() if l.strip()]
    assert nodes == ["fresh"], f"窗口内记录必须保留: {nodes}"


def test_egress_log_path_env_override(tmp_path, monkeypatch):
    """读写都必须走 _egress_log_path()，否则测试会写进生产文件。"""
    f = tmp_path / "iso.jsonl"
    monkeypatch.setenv("EGRESS_LOG_FILE_PATH", str(f))
    assert daemon._egress_log_path() == str(f)
    daemon._record_egress("isolation-check-node")
    assert f.exists() and "isolation-check-node" in f.read_text()


# ============= 429 冷却池 =============
def test_cooldown_node_excluded_from_candidates(monkeypatch):
    """刚吃 429 的节点必须从候选中剔除，否则立刻切回只会继续 429。"""
    monkeypatch.setattr(daemon, "_DEAD_NODES", set())
    monkeypatch.setattr(daemon, "_COOLDOWN_NODES", {})
    monkeypatch.setattr(daemon, "_recent_nodes", __import__("collections").deque(maxlen=5))
    monkeypatch.setattr(daemon, "cleanup_dead_ttl", lambda: None)

    nodes = ["n1", "n2", "n3", "n4", "n5", "n6"]
    daemon._cool_down_node("n2", seconds=60)
    out = daemon._filter_candidates("n1", nodes, set())
    assert "n2" not in out, f"冷却中的节点不应出现在候选中: {out}"


def test_cooldown_expires_and_node_returns(monkeypatch):
    """冷却到期后节点必须回到候选池，不能被永久排除。"""
    monkeypatch.setattr(daemon, "_DEAD_NODES", set())
    monkeypatch.setattr(daemon, "_recent_nodes", __import__("collections").deque(maxlen=5))
    monkeypatch.setattr(daemon, "cleanup_dead_ttl", lambda: None)
    monkeypatch.setattr(daemon, "_COOLDOWN_NODES", {"n2": time.time() - 1})  # 已过期

    nodes = ["n1", "n2", "n3", "n4", "n5", "n6"]
    out = daemon._filter_candidates("n1", nodes, set())
    assert "n2" in out, f"冷却已到期的节点应回到候选池: {out}"


def test_cooldown_does_not_mark_node_dead(monkeypatch):
    """429 冷却不得污染死节点集合——两者语义必须隔离。"""
    dead = set()
    monkeypatch.setattr(daemon, "_DEAD_NODES", dead)
    monkeypatch.setattr(daemon, "_COOLDOWN_NODES", {})
    daemon._cool_down_node("n9", seconds=10)
    assert "n9" not in dead, "429 冷却不应把节点标记为死节点"
    assert "n9" in daemon._COOLDOWN_NODES

# ============= 8. WARP 主备隧道换线 =============
WARP_TWO = [
    {"tag": "W1", "port": 1080, "service": "usque-warp.service"},
    {"tag": "W2", "port": 1081, "service": "usque-warp-b.service"},
]


def _warp_env(monkeypatch, members, max_attempts=3):
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"], "members": members, "max_attempts": max_attempts,
        "retry_backoff_sec": [0]})
    # IP 使用记录是进程级全局，测试间必须清空，否则上一个用例的地址会把
    # 本用例的出口误判成"窗口内用过"。
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {})
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_WARP_PROBING", set())
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 0.0)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    # 换线成功会起一个延迟补刷线程（20s 后真调 systemctl 重启生产隧道）。
    # 测试里的 monkeypatch 早就撤了，只有这个桩能挡住它。实测不加时套件
    # 2/8 次里把 usque-warp.service 真的重启了一次。
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)


def test_warp_failover_not_gated_by_node_switch(monkeypatch):
    """WARP 换线不得受 _last_successful_switch_ts 门控。

    实测事故：主循环里主动轮换（每 60s）排在错误触发之前，它刷新该时间戳，
    使 errors_after_switch 恒为 0，15:47~15:52 的 252 条 abuse 403 一次换线都没触发。
    这里断言判定不读那个时间戳：命中一次 abuse 403 就必须调用换线。
    """
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "block_threshold": 1})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_warp_member", lambda tag: {"tag": tag, "port": 1080, "service": "s"} if tag == "W1" else None)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 0.0)  # Must be 0 to bypass stale_only check
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "some-ip")
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 3, "rate429": 0, "ok": 0, "consec_err": 3})
    called = []

    class T:
        def __init__(self, target=None, args=(), **k): self._t, self._a = target, args
        def start(self): called.append(1) or self._t(*self._a)

    monkeypatch.setattr(daemon.threading, "Thread", T)
    monkeypatch.setattr(daemon, "reconnect_warp", lambda r: True)
    daemon.maybe_reconnect_warp(1000.0)
    assert called, "abuse 403 出现即须触发换线，不受节点切换时间戳影响"


def test_warp_failover_on_429_respects_higher_threshold(monkeypatch):
    """429 同样按出口 IP 判定，须触发换线；但阈值高于 403，避免抖动式重启隧道。

    实测 16:24 切 WARP 成员后成功率 3.5% → 9.3%，429 换线确有效；
    而 429 密度达 2~5 req/s，阈值 1 会让两条隧道反复重启。
    """
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "block_threshold": 1,
                         "rate429_threshold": 5, "cooldown_ok_sec": 20})
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "reconnect_warp", lambda r: True)

    launched = []

    class T:
        def __init__(self, target=None, args=(), **k): self._t, self._a = target, args
        def start(self):        # 不跑 target：worker 会用 wall-clock 改写冷却值
            launched.append(self._a[0])
            daemon._WARP_LOCK.release()

    monkeypatch.setattr(daemon.threading, "Thread", T)

    # 4 条 429 未达阈值 5，不得换线
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 0, "rate429": 4, "ok": 0, "consec_err": 4})
    daemon.maybe_reconnect_warp(1000.0)
    assert not launched, "429 未达阈值不应换线"

    # 5 条 429 达阈值，必须换线，且冷却按 2^n 从 20s 起算
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 0, "rate429": 5, "ok": 0, "consec_err": 5})
    daemon.maybe_reconnect_warp(1000.0)
    assert launched and "rate429" in launched[0], f"429 达阈值须换线, 实际 {launched}"
    assert round(daemon._warp_cooldown_until - 1000.0) == 20


def test_warp_403_and_429_share_one_failover(monkeypatch):
    """两类同时达阈值只换一次线，理由里两项都出现（不得各换一次）。"""
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "block_threshold": 1,
                         "rate429_threshold": 5, "cooldown_ok_sec": 20})
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "reconnect_warp", lambda r: True)
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 1, "rate429": 9, "ok": 0, "consec_err": 10})
    launched = []

    class T:
        def __init__(self, target=None, args=(), **k): self._t, self._a = target, args
        def start(self):
            launched.append(self._a[0])
            daemon._WARP_LOCK.release()

    monkeypatch.setattr(daemon.threading, "Thread", T)
    daemon.maybe_reconnect_warp(1000.0)
    assert len(launched) == 1, f"两类同时达阈值只应换一次线, 实际 {len(launched)}"
    assert "abuse403" in launched[0] and "rate429" in launched[0], launched[0]


def test_maybe_reconnect_warp_suppressed_when_newest_row_succeeded(monkeypatch):
    """最新一条仍成功（连错 0 条）→ 不得换线。

    用户规则：最近两条连错都允许不切，第三条才换；最新一条成功时连错为 0。
    """
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "block_threshold": 1,
                         "rate429_threshold": 1, "cooldown_ok_sec": 5})
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 0, "rate429": 1, "ok": 9,
                                    "consec_err": 0, "full_err": False})
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda **k: pytest.fail("最新一条是成功，不应换线"))
    daemon.maybe_reconnect_warp(1000.0)
    assert daemon._WARP_LOCK.locked() is False, "被门控拦下时不得占用换线锁"


def test_maybe_reconnect_warp_allows_two_consecutive_errors(monkeypatch):
    """连错 2 条仍不切线 —— 用户要求「刚切线的 10 秒内允许出现 2 次报错而不切线」。

    WHY 不看窗口成功数：429 风暴里成功与错误交替，窗口里 9 条成功也可有连错
    2 条；用户要的是「多给一次机会」而不是「半数以上成功才不切」。
    """
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "block_threshold": 1,
                         "rate429_threshold": 1, "cooldown_ok_sec": 5})
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 0, "rate429": 2, "ok": 8,
                                    "consec_err": 2, "full_err": False})
    launched = []
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda **k: pytest.fail("连错 2 条时不得换线"))
    daemon.maybe_reconnect_warp(1000.0)
    assert daemon._WARP_LOCK.locked() is False, "被门控拦下时不得占用换线锁"


def test_maybe_reconnect_warp_switches_on_third_consecutive_error(monkeypatch):
    """连错 3 条 → 换线（用户的允许额只有 2 条）。"""
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "block_threshold": 1,
                         "rate429_threshold": 1, "cooldown_ok_sec": 5})
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 0, "rate429": 3, "ok": 7,
                                    "consec_err": 3, "full_err": False})
    launched = []

    class T:
        def __init__(self, target=None, args=(), **k): self._a = args
        def start(self):
            launched.append(self._a[0])
            daemon._WARP_LOCK.release()

    monkeypatch.setattr(daemon.threading, "Thread", T)
    daemon.maybe_reconnect_warp(1000.0)
    assert launched, "连错 3 条必须换线"


def test_maybe_reconnect_warp_full_error_window_bypasses_cooldown(monkeypatch):
    """最近 10 条 100% 打满 → 无视冷却立刻换线。"""
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "block_threshold": 1,
                         "rate429_threshold": 1, "cooldown_ok_sec": 5})
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 0, "rate429": 10, "ok": 0,
                                    "consec_err": 10, "full_err": True})
    launched = []

    class T:
        def __init__(self, target=None, args=(), **k): self._a = args
        def start(self):
            launched.append(self._a[0])
            daemon._WARP_LOCK.release()

    monkeypatch.setattr(daemon.threading, "Thread", T)
    # 冷却还剩 4s：普通触发会被挡住，满窗风暴必须照换
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 1004.0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 0)
    daemon.maybe_reconnect_warp(1000.0)
    assert launched, "满窗 10 条全错时冷却不得阻止换线"


def test_maybe_reconnect_warp_full_error_window_still_respects_min_gap(monkeypatch):
    """满窗风暴能绕过冷却，但不能绕过「两次生效间隔 ≥5s」这道硬闸。

    WHY：触发→生效 p50 4s，按触发起算的冷却在新出口刚上线时就已过期。
    实测 46 次错误触发里 39 次的错误全部来自上一段出口，新出口一条请求都
    没发就被切走。这里用生效时刻起算，杜绝「换完立刻再换」。
    """
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "block_threshold": 1,
                         "rate429_threshold": 1, "cooldown_ok_sec": 5,
                         "min_switch_gap_sec": 5})
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 0, "rate429": 10, "ok": 0,
                                    "consec_err": 10, "full_err": True})
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda **k: pytest.fail("生效后 4s 内不得再换线"))
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 996.0)   # 4s 前刚生效
    daemon.maybe_reconnect_warp(1000.0)
    assert daemon._WARP_LOCK.locked() is False, "被硬闸拦下时不得占用换线锁"


def test_maybe_reconnect_warp_min_gap_expires(monkeypatch):
    """生效满 5s 后硬闸放行 —— 闸门是延迟，不是封禁。"""
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "block_threshold": 1,
                         "rate429_threshold": 1, "cooldown_ok_sec": 5,
                         "min_switch_gap_sec": 5})
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 0, "rate429": 10, "ok": 0,
                                    "consec_err": 10, "full_err": True})
    launched = []

    class T:
        def __init__(self, target=None, args=(), **k): self._a = args
        def start(self):
            launched.append(self._a[0])
            daemon._WARP_LOCK.release()

    monkeypatch.setattr(daemon.threading, "Thread", T)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 995.0)   # 正好 5s 前
    daemon.maybe_reconnect_warp(1000.0)
    assert launched, "满 5s 必须放行"


def test_maybe_reconnect_warp_respects_cooldown_when_window_not_full(monkeypatch):
    """冷却期内、窗口未打满 → 不换线（用户要求：切完要有短时间限制）。"""
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "block_threshold": 1,
                         "rate429_threshold": 1, "cooldown_ok_sec": 5})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 0, "rate429": 2, "ok": 8,
                                    "consec_err": 3, "full_err": False})
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda **k: pytest.fail("冷却期内不得换线"))
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 1004.0)
    daemon.maybe_reconnect_warp(1000.0)


def test_active_warp_tag_maps_selector_now(monkeypatch):
    """selector 缺失（未启用 WARP）或指向轮换池时必须返回 None，旧轮换路径保持可用。"""
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "members": WARP_TWO})
    monkeypatch.setattr(daemon, "singbox_api", lambda *a, **k: (200, {"now": "W1"}))
    assert daemon.active_warp_tag() == "W1"
    assert daemon.opencode_out_on_warp() is True
    monkeypatch.setattr(daemon, "singbox_api", lambda *a, **k: (200, {"now": "some-node"}))
    assert daemon.opencode_out_on_warp() is False
    monkeypatch.setattr(daemon, "singbox_api", lambda *a, **k: (404, None))
    assert daemon.opencode_out_on_warp() is False


def test_count_warp_blocked_since_filters_abuse(monkeypatch):
    """两类出口级拦截都要分开放回：abuse 403 与限流 429。

    数据格式取自生产库真实行：429 的文案是 FreeUsageLimitError
    "Rate limit exceeded"，与 abuse 403 完全是两条不同的错误。
    """
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE requestDetails (provider TEXT, status TEXT, timestamp TEXT, data TEXT)")
    abuse = '{"response":{"error":"{\\"error\\":{\\"message\\":\\"detected abusive traffic from this IP address\\"}}","status":403}}'
    rate = '{"response":{"error":"{\\"error\\":{\\"type\\":\\"FreeUsageLimitError\\",\\"message\\":\\"Rate limit exceeded. Please try again later.\\"}}","status":429}}'
    mem.executemany("INSERT INTO requestDetails VALUES (?,?,?,?)", [
        ("opencode", "error", "2026-10-19T07:00:01.000Z", abuse),
        ("opencode", "error", "2026-10-19T07:00:02.000Z", rate),
        ("opencode", "success", "2026-10-19T07:00:03.000Z", '{"response":{"status":"success"}}'),
        ("opencode", "error", "2026-10-19T06:59:00.000Z", abuse),
    ])
    monkeypatch.setattr(daemon, "_get_db", lambda: mem)
    cutoff = datetime(2026, 10, 19, 7, 0, 0, tzinfo=timezone.utc).timestamp()
    got = daemon.count_warp_blocked_since(cutoff)
    assert {k: got[k] for k in ("abuse403", "rate429", "ok")} == \
        {"abuse403": 1, "rate429": 1, "ok": 1}, \
        "两类拦截分开计数；success 不计入拦截但计入 ok（判断出口是否在服务）"
    assert got["consec_err"] == 0, "窗口内最新一条（07:00:03）是成功，连错为 0"
    assert got["full_err"] is False, "窗口未满且成功数 > 0"
    # 显式给窗口：默认窗口现在至少 3 条（连错门控要数到 3），06:59 那条会落在窗外
    assert daemon.count_warp_blocked_since(0, limit=10)["abuse403"] == 2


def test_count_warp_blocked_since_counts_consecutive_tail(monkeypatch):
    """连错数从最新一条往前数，停在第一条成功处；成功夹在中间也挡不住尾部计数。

    显式 limit=10：默认窗口已抬到至少 3 条（连错门控要数到 3），本例的
    「更早的成功仍计入窗内 ok」需要包含那第 4 条成功。
    """
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE requestDetails (provider TEXT, status TEXT, timestamp TEXT, data TEXT)")
    rate = json.dumps({"response": {
        "error": json.dumps({"error": {"type": "FreeUsageLimitError",
                                       "message": "Rate limit exceeded. Please try again later."}}),
        "status": 429}})
    mem.executemany("INSERT INTO requestDetails VALUES (?,?,?,?)", [
        ("opencode", "success", "2026-10-19T07:00:01.000Z", '{"response":{"status":"success"}}'),
        ("opencode", "error", "2026-10-19T07:00:02.000Z", rate),
        ("opencode", "error", "2026-10-19T07:00:03.000Z", rate),
        ("opencode", "error", "2026-10-19T07:00:04.000Z", rate),
    ])
    monkeypatch.setattr(daemon, "_get_db", lambda: mem)
    assert daemon.count_warp_blocked_since(0, limit=10)["consec_err"] == 3, \
        "最新三条连错，中间无成功"
    assert daemon.count_warp_blocked_since(0, limit=10)["ok"] == 1, \
        "更早的成功仍计入窗内 ok"


def test_count_warp_blocked_since_production_nested_response(monkeypatch):
    """生产行形态：response.error 是**字符串化**的内层 JSON。

    实测格式（生产库原始行）：
      {"status":"error","response":{"error":"{\\"error\\":{...}}","status":429}}
    关键词就藏在那层转义引号之间，但**在原始文本里逐字可见** —— 子串匹配
    直接命中，不需要任何解析。本用例把生产形态钉住，防止将来改回解析式、
    或误以为关键词被转义遮蔽。
    """
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE requestDetails (provider TEXT, status TEXT, timestamp TEXT, data TEXT)")
    real_429 = json.dumps({
        "status": "error",
        "response": {
            "error": json.dumps({"error": {
                "type": "FreeUsageLimitError",
                "message": "Rate limit exceeded. Please try again later."}}),
            "status": 429}})
    real_403 = json.dumps({
        "status": "error",
        "response": {
            "error": json.dumps({"error": {
                "type": "FreeTierError",
                "message": "detected abusive traffic from this IP address"}}),
            "status": 403}})
    mem.executemany("INSERT INTO requestDetails VALUES (?,?,?,?)", [
        ("opencode", "error", "2026-10-19T19:46:28.822Z", real_429),
        ("opencode", "error", "2026-10-19T19:46:25.299Z", real_403),
        ("opencode", "success", "2026-10-19T19:46:20.000Z", '{"response":{"status":"success"}}'),
    ])
    monkeypatch.setattr(daemon, "_get_db", lambda: mem)
    got = daemon.count_warp_blocked_since(0)
    assert {k: got[k] for k in ("abuse403", "rate429", "ok")} == \
        {"abuse403": 1, "rate429": 1, "ok": 1}, \
        "生产嵌套 JSON 形态必须同样计入两类拦截"


def test_count_warp_blocked_since_attributes_by_request_start(monkeypatch):
    """按请求**开始**时刻归属，不按完成时刻。

    9router 在请求结束时才写行，换线又不掐断在途请求（实测 38.4% 的请求生命
    周期跨越一次换线）。若按完成时刻切窗口，上一出口的 429 会在换线后落进新
    窗口，再触发一次换线 —— 实测 45% 的触发计数属于这一类。
    """
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE requestDetails (provider TEXT, status TEXT, timestamp TEXT, data TEXT)")
    def row(status, ts, latency_ms, body=None):
        data = json.dumps({"status": status, "latency": {"total": latency_ms},
                           "response": body or {}})
        return ("opencode", status, ts, data)
    stale_429 = json.dumps({"error": {"type": "FreeUsageLimitError",
                                      "message": "Rate limit exceeded. Please try again later."}})
    mem.executemany("INSERT INTO requestDetails VALUES (?,?,?,?)", [
        # 完成于窗口内、但开始于窗口之前：属于上一出口，不能计入
        row("error", "2026-10-19T07:00:06.000Z", 8000, {"error": stale_429, "status": 429}),
        # 完成于窗口内、开始于窗口之后：属于本出口
        row("error", "2026-10-19T07:00:07.000Z", 1000, {"error": stale_429, "status": 429}),
        # 成功同理：开始于窗口之前的成功不算本出口的 ok
        row("success", "2026-10-19T07:00:05.000Z", 10000),
    ])
    monkeypatch.setattr(daemon, "_get_db", lambda: mem)
    cutoff = datetime(2026, 10, 19, 7, 0, 0, tzinfo=timezone.utc).timestamp()
    assert daemon.count_warp_blocked_since(cutoff)["rate429"] == 1, \
        "只统计开始时刻落在窗口内的行，完成时刻在窗口内不算"


def test_count_warp_blocked_since_other_error_never_triggers_switch(monkeypatch):
    """上游 402 / 请求超长是 other_err：不进 full_err，也不截断两侧的连错。

    复刻生产事故形态：recent_window=2 时一次 Endpoint is unavailable + 一次
    maximum context length 就凑出整窗全错，立刻换线 —— 而换出口既不解 402
    也不解超长。
    """
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE requestDetails (provider TEXT, status TEXT, timestamp TEXT, data TEXT)")
    e402 = '{"response":{"status":402,"error":"Endpoint is unavailable"}}'
    toolong = '{"response":{"error":"maximum context length is 128000 tokens"}}'
    rate = json.dumps({"response": {
        "error": json.dumps({"error": {"type": "FreeUsageLimitError",
                                       "message": "Rate limit exceeded. Please try again later."}}),
        "status": 429}})
    # 最新两条：只有 other 错误 —— 旧逻辑下 full_err=True 直接换线
    mem.executemany("INSERT INTO requestDetails VALUES (?,?,?,?)", [
        ("opencode", "error", "2026-10-19T07:00:03.000Z", toolong),
        ("opencode", "error", "2026-10-19T07:00:02.000Z", e402),
    ])
    monkeypatch.setattr(daemon, "_get_db", lambda: mem)
    got = daemon.count_warp_blocked_since(0, limit=3)
    assert got["full_err"] is False, "整窗只有非出口错误，不构成风暴"
    assert got["consec_err"] == 0, "other 不计入连错"
    assert got["other_err"] == 2, "两条 other 都要留在诊断计数里"

    # 402 夹在两条 429 中间：不能把两侧的连错割断
    mem.execute("DELETE FROM requestDetails")
    mem.executemany("INSERT INTO requestDetails VALUES (?,?,?,?)", [
        ("opencode", "error", "2026-10-19T07:00:03.000Z", rate),
        ("opencode", "error", "2026-10-19T07:00:02.000Z", e402),
        ("opencode", "error", "2026-10-19T07:00:01.000Z", rate),
    ])
    got = daemon.count_warp_blocked_since(0, limit=3)
    assert got["consec_err"] == 2, "中间的 402 不截断，两条 429 连续计数"
    assert got["full_err"] is False, "窗内有 other_err，不算纯出口风暴"


def test_count_segment_requests_attributes_by_request_start(monkeypatch):
    """段内计数按请求开始时刻归属，与 count_warp_blocked_since 同口径。

    9router 在请求结束时才写行，换线又不掐断在途请求（实测 38.4% 的请求
    生命周期跨越一次换线）。按完成时刻算，上一段的请求会被记进新段，
    「这条出口服务了多少条」被系统性污染。
    """
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE requestDetails (provider TEXT, status TEXT, timestamp TEXT, data TEXT)")
    rate = json.dumps({"response": {
        "error": json.dumps({"error": {"type": "FreeUsageLimitError",
                                       "message": "Rate limit exceeded. Please try again later."}}),
        "status": 429}})
    def row(status, ts, latency_ms, body=None):
        return ("opencode", status, ts,
                json.dumps({"status": status, "latency": {"total": latency_ms},
                            "response": body or {}}))
    mem.executemany("INSERT INTO requestDetails VALUES (?,?,?,?)", [
        # 开始于段起点之前、完成于之后：属于上一段，不计
        row("error", "2026-10-19T07:00:05.000Z", 8000, {"error": rate, "status": 429}),
        # 开始于段起点之后：属于本段
        row("success", "2026-10-19T07:00:40.000Z", 1000),
        row("success", "2026-10-19T07:01:00.000Z", 1000),
        row("error", "2026-10-19T07:01:10.000Z", 1000, {"error": rate, "status": 429}),
    ])
    monkeypatch.setattr(daemon, "_get_db", lambda: mem)
    cutoff = datetime(2026, 10, 19, 7, 0, 30, tzinfo=timezone.utc).timestamp()
    ok, blocked = daemon.count_segment_requests(cutoff)
    assert (ok, blocked) == (2, 1), \
        f"跨段请求不计入本段，应为 (2, 1)，实际 {(ok, blocked)}"


def test_failover_switches_to_standby_and_refreshes_idle(monkeypatch):
    """备线可用时必须换线（不原地重启），并异步刷新被换下的主线上会话。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: {1080: "1.1.1.1", 1081: "2.2.2.2"}[port])
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_restart_service", lambda s, tag=None: pytest.fail("换线成功不应重启隧道"))
    monkeypatch.setattr(daemon, "_refresh_idle_warp", lambda m, **kw: None)
    assert daemon.reconnect_warp("test") is True
    assert switched == ["W2"], f"应切到备线 W2, 实际 {switched}"


def test_failover_skips_dead_standby(monkeypatch):
    """备线探测不通且拉不起来时跳过它，改切轮换池 proxy。

    不得切到不通的备线；也不再原地重连 —— 原地重连要 systemctl restart，
    D-Bus 不可用时必败，流量会停在坏出口上（实测 2026-10-19 19:09）。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", True)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: "1.1.1.1" if port == 1080 else None)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_restart_service", lambda s, tag=None: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: False)
    assert daemon.reconnect_warp("test") is True
    assert switched == ["proxy"], f"应切轮换池 proxy，实际 {switched}"


def test_failover_without_standby_falls_back_to_proxy(monkeypatch):
    """单隧道部署（无备线）切到轮换池 proxy，而非原地重连当前隧道。"""
    _warp_env(monkeypatch, WARP_TWO[:1])
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", True)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: "1.1.1.1")
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_restart_service", lambda s, tag=None: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: True)
    assert daemon.reconnect_warp("test") is True
    assert switched == ["proxy"], f"应切 proxy，实际 {switched}"


def test_proxy_fallback_respects_cooldown(monkeypatch):
    """冷却期内不得再落池：其它线都切不过去时退回原地重连。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "_proxy_fallback_ts_loaded", True)
    monkeypatch.setattr(daemon, "_last_proxy_fallback_ts", 1000.0)
    monkeypatch.setattr(daemon, "_port_open", lambda p: False)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    restarted = []
    monkeypatch.setattr(daemon, "_restart_warp_inplace",
                        lambda m, ip: restarted.append(m["tag"]) or True)
    assert daemon._fallback_to_proxy(WARP_TWO[0], "1.1.1.1", 2000.0) is True
    assert switched == [], f"冷却期内不得切 proxy, 实际 {switched}"
    assert restarted == ["W1"], f"应原地重连当前线, 实际 {restarted}"


def test_proxy_fallback_cooldown_switches_other_warp_line(monkeypatch):
    """冷却期内改切其它 WARP 成员，且不更新落池计时。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", True)
    monkeypatch.setattr(daemon, "_proxy_fallback_ts_loaded", True)
    monkeypatch.setattr(daemon, "_last_proxy_fallback_ts", 1000.0)
    monkeypatch.setattr(daemon, "_port_open", lambda p: True)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_restart_warp_inplace",
                        lambda m, ip: pytest.fail("有可用备线时不应原地重连"))
    assert daemon._fallback_to_proxy(WARP_TWO[0], "1.1.1.1", 2000.0) is True
    assert switched == ["W2"], f"冷却期内应切另一条 WARP 线, 实际 {switched}"
    assert daemon._last_proxy_fallback_ts == 1000.0, "冷却期内的替代切换不得更新落池计时"


def test_proxy_fallback_updates_cooldown_after_success(monkeypatch):
    """冷却通过时切 proxy，落池时刻取切换生效时刻并落盘。"""
    _warp_env(monkeypatch, WARP_TWO[:1])
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", True)
    monkeypatch.setattr(daemon, "_proxy_fallback_ts_loaded", True)
    monkeypatch.setattr(daemon, "_last_proxy_fallback_ts", 0.0)
    monkeypatch.setattr(daemon.time, "time", lambda: 5000.0)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    assert daemon._fallback_to_proxy(None, None, 20000.0) is True
    assert switched == ["proxy"], f"冷却通过应落池, 实际 {switched}"
    assert daemon._last_proxy_fallback_ts == 5000.0


def test_proxy_fallback_disabled_restarts_inplace(monkeypatch):
    """落池禁用时不得切 proxy，只原地重连当前 WARP 线。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", False)
    monkeypatch.setattr(daemon, "_proxy_fallback_ts_loaded", True)
    monkeypatch.setattr(daemon, "_last_proxy_fallback_ts", 0.0)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    restarted = []
    monkeypatch.setattr(daemon, "_restart_warp_inplace",
                        lambda m, ip: restarted.append(m["tag"]) or True)
    assert daemon._fallback_to_proxy(WARP_TWO[0], "1.1.1.1", 20000.0) is True
    assert switched == [], f"落池禁用时不得切 proxy, 实际 {switched}"
    assert restarted == ["W1"], f"应原地重连当前线, 实际 {restarted}"


def test_proxy_fallback_disabled_without_active_gives_up(monkeypatch):
    """落池禁用且当前没有 WARP 线时直接放弃，不切任何出口。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", False)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    assert daemon._fallback_to_proxy(None, None, 20000.0) is False
    assert switched == [], f"落池禁用时不得切任何出口, 实际 {switched}"


def test_inplace_restart_gives_up_after_max_attempts(monkeypatch):
    """出口一直不变时必须放弃，不能无限重启隧道。

    直接测 _restart_warp_inplace —— reconnect_warp 现已优先切 proxy，
    只有 proxy 切换也失败时才退回原地重连。
    """
    calls = []
    _warp_env(monkeypatch, WARP_TWO[:1], max_attempts=3)
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: "1.1.1.1")
    monkeypatch.setattr(daemon, "_restart_service", lambda s, tag=None: calls.append(1) or True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: True)
    assert daemon._restart_warp_inplace(WARP_TWO[0], "1.1.1.1") is False
    assert len(calls) == 3, f"重启次数应受 max_attempts 限制, 实际 {len(calls)}"


def test_maybe_reconnect_warp_respects_threshold_and_cooldown(monkeypatch):
    """低于阈值不触发；窗口内重复触发只放行一次（防重连风暴）。"""
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "members": WARP_TWO, "block_threshold": 3})
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0)
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 2, "rate429": 0, "ok": 0, "consec_err": 2})
    monkeypatch.setattr(daemon.threading, "Thread", lambda **k: pytest.fail("未达阈值不应换线"))
    daemon.maybe_reconnect_warp(1000.0)

    launched = []

    class T:                       # 同步跑 target：等价于 worker 立刻结束
        def __init__(self, target=None, args=(), **k): self._t, self._a = target, args
        def start(self): launched.append(1) or self._t(*self._a)

    monkeypatch.setattr(daemon.threading, "Thread", T)
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 3, "rate429": 0, "ok": 0, "consec_err": 3})
    monkeypatch.setattr(daemon, "reconnect_warp", lambda r: True)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    daemon.maybe_reconnect_warp(1000.0)
    assert len(launched) == 1, "达到阈值应触发一次换线"
    daemon.maybe_reconnect_warp(1100.0)   # 1000+20 冷却窗口内
    assert len(launched) == 1, "冷却窗口内不得重复换线"
    assert daemon._WARP_LOCK.acquire(blocking=False), "worker 结束后必须释放锁，否则永不换线"
    daemon._WARP_LOCK.release()


# ============= 8b. 换线后必须用窗口内未使用过的出口 IP =============
def _reuse_env(monkeypatch, egress: dict, guard: int = 300, now: float = 1000.0):
    """搭好 reconnect_warp 的可控环境：IP 表、时间、重启/切换桩。"""
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"], "members": WARP_TWO, "ip_reuse_guard_sec": guard,
        "ready_timeout_sec": 1, "retry_backoff_sec": [0], "max_attempts": 3,
    })
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    # 脏线守护的真实线程会往 _WARP_REFRESHING / _WARP_PROBING 写标记，且不一定
    # 在测试结束前清理干净；不重置会污染后续用例（正在重启的线被剔出候选池）。
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_WARP_PROBING", set())
    monkeypatch.setattr(daemon, "_WARP_REFRESH_PENDING", set())
    # 快速选线读 `_WARP_LINE_STATE`（守护探到的 tag→IP+时刻）与 `_ACTIVE_WARP_IP`，
    # 都是进程级全局：不隔离时上一用例的缓存会被本用例当成「刚验证过」，
    # 直接跳过探测/刷新，断言全歪。
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {})
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", None)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon.time, "time", lambda: now)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: egress[str(port)])
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda port, t: True)
    # 同 _warp_env：延迟补刷线程会在 monkeypatch 撤销后醒来，真调 systemctl。
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)
    return egress

def test_warp_ip_reuse_window_and_cleanup(monkeypatch):
    """窗口内的地址算"用过"，出窗口的条目在写入时被清理。"""
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "ip_reuse_guard_sec": 300})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    daemon._mark_warp_ip_used("a", 1000.0)
    daemon._mark_warp_ip_used("b", 600.0)   # 已出窗口
    daemon._mark_warp_ip_used("c", 1000.0)
    # 「最近用过」按最近一次**成功流量**判定，所以显式写入成功时刻
    daemon._WARP_IP_USED["a"]["last_ok_ts"] = 1000.0
    # b 已在写入 a/c 时被清理，不再存在
    assert daemon._warp_ip_recently_used("a", 1100.0), "100s 前有成功流量，仍在窗口内"
    assert not daemon._warp_ip_recently_used("b", 1100.0), "500s 前有成功流量，已出窗口"
    assert "b" not in daemon._WARP_IP_USED, "出窗口的条目应被清理"
    assert not daemon._warp_ip_recently_used("never", 1100.0)
    assert not daemon._warp_ip_recently_used(None, 1100.0)

def test_record_active_ip_success_creates_missing_entry(monkeypatch):
    """当前出口没有记录时（重启后换出的新地址）也要能写 last_ok_ts。

    只更新已有条目会让成功流量永远打不上点 —— 实测选线日志恒为
    「有成功记录 0 条」。
    """
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "fresh-ip")
    monkeypatch.setattr(daemon, "_last_success_probe", ("fresh-ip", 3))
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)
    monkeypatch.setattr(daemon, "recent_success_ts", lambda n: [990.0])

    daemon._record_active_ip_success(total=5, errors=1)   # 成功数 3 → 4

    assert daemon._WARP_IP_USED["fresh-ip"]["last_ok_ts"] == 990.0
    assert daemon._WARP_IP_USED["fresh-ip"]["first_ok_ts"] == 990.0
    assert daemon._WARP_IP_USED["fresh-ip"]["blocked"] is False

def test_maybe_init_active_ip_fills_missing_ip(monkeypatch):
    """启动探测失败后必须能补探，否则 _ACTIVE_WARP_IP 恒 None，成功流量全丢。"""

    class _SyncThread:
        def __init__(self, target=None, args=(), **kw): self._t, self._a = target, args
        def start(self): self._t(*self._a)

    monkeypatch.setattr(daemon.threading, "Thread", _SyncThread)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_warp_member",
                        lambda tag: {"tag": tag, "port": 1080, "service": "s"})
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: "probed-ip")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", None)
    monkeypatch.setattr(daemon, "_ACTIVE_IP_INIT_TS", 0.0)

    daemon._maybe_init_active_ip(1000.0)

    assert daemon._ACTIVE_WARP_IP == "probed-ip"

def test_warp_any_reachable_ignores_blocked_ip(monkeypatch):
    """连通但已被 403/429 标记的 IP 不算可达。

    实测 22:36:28 切到 2a09:bac1:19c0:18::1d2:c4 后 403 仍 10/10，
    再换两个同段 IP 才恢复 —— 只看连通会让 daemon 反复切回已烧掉的地址。
    """
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "members": WARP_TWO})
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: {"1080": "ip-a", "1081": "ip-b"}[str(port)])
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-a": {"ts": 1000.0, "blocked": True},
        "ip-b": {"ts": 1000.0, "blocked": False},
    })
    assert daemon._warp_any_reachable() is True, "有一条未被标记就仍可达"

    daemon._WARP_IP_USED["ip-b"] = {"ts": 1000.0, "blocked": True}
    assert daemon._warp_any_reachable() is False, "三条全被标记 → 不可达"

def test_maybe_reconnect_marks_active_ip_as_blocked(monkeypatch):
    """触发换线时把当前出口 IP 记为 blocked，换线时不再把它当候选。"""
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "block_threshold": 1, "rate429_threshold": 5,
                         "members": WARP_TWO})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    # 出口 IP 取自换线时记下的缓存，不在错误路径上现探（warp_egress_ip 走 SOCKS，
    # 实测 1.2~7s，正好卡在「发现 403 → 起手换线」的最短链路上）。
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "burned-ip")
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: pytest.fail("错误路径不得同步探测出口 IP"))
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 3, "rate429": 0, "ok": 0, "consec_err": 3})

    class T:
        def __init__(self, target=None, args=(), **k): pass
        def start(self): pass

    monkeypatch.setattr(daemon.threading, "Thread", T)
    daemon.maybe_reconnect_warp(1000.0)
    entry = daemon._WARP_IP_USED.get("burned-ip", {})
    assert entry.get("blocked") is True, "被 403 拦下的出口 IP 必须记为 blocked"
    assert entry.get("blocked_kind") == "abuse403", \
        f"须记下是 403 烧的（6h 恢复期），实际 {entry.get('blocked_kind')}"

def test_warp_ip_used_survives_restart(monkeypatch, tmp_path):
    """被标记的 IP 必须跨 daemon 重启保留 —— 22:22 重启后旧 IP 全变"干净"，
    22:27 立即切回已被烧的地址，就是这条记录只存在进程内造成的。"""
    store = tmp_path / "warp_ip_used.json"
    monkeypatch.setenv("WARP_IP_USED_FILE_PATH", str(store))
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "ip_reuse_guard_sec": 900})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})

    daemon._mark_warp_ip_used("burned-ip", 1000.0, blocked=True)
    assert store.exists(), "写入后必须落盘"

    # 模拟 daemon 重启：清空内存，从磁盘恢复
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    daemon._load_warp_ip_used()
    assert daemon._WARP_IP_USED["burned-ip"]["blocked"] is True, "污染标记须跨重启存活"
    # 使用时刻本身也须恢复（选线按成功流量，污染标记按 blocked）
    assert daemon._WARP_IP_USED["burned-ip"]["ts"] == 1000.0, "使用时间戳须恢复"

def test_warp_failover_refreshes_target_ip_before_switch(monkeypatch):
    """目标线当前出口在窗口内用过 → 必须先重启那条隧道换会话 IP 再切过去。

    否则等于从一个被限流的出口切到另一个同样被限流的出口，白换一次。
    """
    egress = _reuse_env(monkeypatch, {"1080": "ip-w1", "1081": "stale-ip"})
    # 100s 前**有成功流量**，仍在窗口内 → 必须先刷新会话再切
    daemon._WARP_IP_USED["stale-ip"] = {"ts": 900.0, "blocked": False, "last_ok_ts": 900.0}
    refreshed = []

    def fake_restart(service, tag=None):
        if service == "usque-warp-b.service":
            egress["1081"] = "fresh-ip"        # 重启换会话 IP
            refreshed.append(service)
        else:
            egress["1080"] = "ip-w1-new"       # 被换下的线也要换到新鲜地址
        return True

    monkeypatch.setattr(daemon, "_restart_service", fake_restart)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    assert daemon.reconnect_warp("test") is True
    assert refreshed == ["usque-warp-b.service"], "目标 IP 用过就必须先刷新该隧道"
    assert switched == ["W2"], f"只应切到 W2，实际 {switched}"
    assert daemon._WARP_IP_USED["fresh-ip"]["ts"] == 1000.0, "切过去的 IP 应记为已使用"
    assert daemon._WARP_IP_USED["ip-w1"]["ts"] == 1000.0, "被换下的旧地址应记为已使用"
    assert daemon._WARP_IP_USED["ip-w1"]["ts"] > daemon._WARP_IP_USED["stale-ip"]["ts"], \
        "被换下时刷新使用时间，窗口从头算"

def test_warp_failover_skips_refresh_for_fresh_ip(monkeypatch):
    """目标线出口不在窗口内 → 直接切，不重启备线（省掉几秒断流）。"""
    egress = _reuse_env(monkeypatch, {"1080": "ip-w1", "1081": "clean-ip"})
    restarted = []
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda service, tag=None: restarted.append(service) or True)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    assert daemon.reconnect_warp("test") is True
    assert "usque-warp-b.service" not in restarted, "新鲜 IP 不该触发备线重启"
    assert switched == ["W2"]

def test_sync_path_refreshes_at_most_one_blocked_line(monkeypatch):
    """同步换线路径只串行刷新一条脏备线，其余交给后台补刷。

    `_refresh_idle_warp` 单条最坏 3×(12s 等端口 + 探测 + 退避 10s)。串行刷全部
    脏线时，这段时间流量一直打在已被拦的出口上 —— 实测 02:40:43→02:41:52
    在三条线上串行刷新失败，造成 69 秒换线空窗，同期错误率 100%。
    """
    members = [
        {"tag": "W0", "port": 1080, "service": "usque-warp.service"},
        {"tag": "W1", "port": 1081, "service": "usque-warp-b.service"},
        {"tag": "W2", "port": 1082, "service": "usque-warp-c.service"},
        {"tag": "W3", "port": 1083, "service": "usque-warp-d.service"},
    ]
    # 三条备线的出口全被 429 标记 → blocked_lines 有 3 条
    egress = _reuse_env(monkeypatch, {"1080": "burn-0", "1081": "burn-1",
                                      "1082": "burn-2", "1083": "burn-3"}, guard=3600)
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "members": members})
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", True)
    for ip in ("burn-0", "burn-1", "burn-2", "burn-3"):
        daemon._WARP_IP_USED[ip] = {"ts": 990.0, "blocked": True,
                                    "blocked_kind": "rate429", "blocked_ts": 990.0,
                                    "last_ok_ts": 990.0}
    refreshed = []
    monkeypatch.setattr(daemon, "_refresh_idle_warp",
                        lambda m: refreshed.append(m["tag"]) or None)
    scheduled = []
    monkeypatch.setattr(daemon, "_schedule_idle_refresh",
                        lambda m: scheduled.append(m["tag"]))
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: True)

    daemon.reconnect_warp("rate429 ×1")
    assert len(refreshed) == 1, f"同步路径只该刷一条，实际刷了 {refreshed}"
    assert sorted(scheduled) == sorted(set(
        ["W1", "W2", "W3"]) - set(refreshed)), \
        f"其余脏线应交给后台补刷，实际 {scheduled}"


def test_warp_failover_skips_target_when_refresh_keeps_stale_ip(monkeypatch):
    """刷新后仍是窗口内用过的地址 → 跳过该备线，改切轮换池 proxy。"""
    egress = _reuse_env(monkeypatch, {"1080": "ip-w1", "1081": "stale-ip"})
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", True)
    daemon._WARP_IP_USED["stale-ip"] = {"ts": 900.0, "blocked": False, "last_ok_ts": 900.0}
    monkeypatch.setattr(daemon, "_restart_service", lambda service, tag=None: True)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    assert daemon.reconnect_warp("test") is True
    assert switched == ["proxy"], f"不得切到仍是旧 IP 的备线，应切 proxy，实际 {switched}"


def test_failover_excludes_line_under_refresh(monkeypatch):
    """正在补刷的线不得被选为换线目标。

    补刷线程等在它闲下来后就会重启它换 IP；此刻切过去等于把在途请求
    放在一台即将被重启的隧道上（掐断 → upstream connection lost）。
    补刷窗口从 60s 拉长到 600s 后撞上的概率显著上升。
    """
    _warp_env(monkeypatch, WARP_TWO + [{"tag": "W3", "port": 1082,
                                        "service": "usque-warp-c.service"}])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: {
        1080: "ip-w1", 1081: "ip-w2-stale", 1082: "ip-w3-fresh"}[port])
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {"ip-w1": {"ts": 1000.0, "blocked": False},
                         "ip-w2-stale": {"ts": 900.0, "blocked": False}})
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", {"W2"})   # W2 正在补刷
    monkeypatch.setattr(daemon, "_restart_service", lambda s, tag=None: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: True)
    monkeypatch.setattr(daemon, "_wait_warp_idle", lambda m: False)
    monkeypatch.setattr(daemon, "_refresh_idle_warp", lambda m, **kw: None)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    daemon.reconnect_warp("test")
    assert "W2" not in switched, f"补刷中的线不得被切过去，实际 {switched}"
    assert switched == ["W3"], f"应切到唯一的空闲新鲜线 W3，实际 {switched}"


def test_reconnect_warp_refreshes_blocked_standby_before_fallback(monkeypatch):
    """备线出口全被标记 → 刷新一条换会话 IP，而不是直接落 proxy 池。

    IP 只有重启隧道才会变。全跳过会让候选池恒为 0（403 风暴几秒就能烧完全部
    备线），流量被甩到同样被限流的 proxy 池 —— 19:16 的兜底死循环即由此而来。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: True)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)
    probes = []
    restarted = []

    def _probe(port, timeout_sec=20.0):
        probes.append(port)
        if port == 1081 and restarted:
            return "ip-w2-new"              # 重启后换出全新会话 IP
        return {1080: "ip-w1", 1081: "ip-w2"}[port]

    monkeypatch.setattr(daemon, "warp_egress_ip", _probe)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-w1": {"ts": 999.0, "blocked": True},
        "ip-w2": {"ts": 999.0, "blocked": True},
    })
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda s, tag=None: restarted.append(s) or True)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    assert daemon.reconnect_warp("abuse403") is True
    assert restarted == ["usque-warp-b.service"], f"应刷新被标记的备线，实际 {restarted}"
    assert switched == ["W2"], f"应切到刷新后的备线，实际 {switched}"
    assert probes.count(1081) >= 1, f"必须探测备线才有判据，实际 {probes}"


def test_reconnect_warp_prefers_longest_unselected_fresh_line(monkeypatch):
    """多条备线都新鲜时选**最久没被选为出口**的那条，而非 members 顺序第一条。

    固定取第一条时前几条互相刷新得足够快，排在后面的线永远轮不到 ——
    实测整份 daemon.log 里 WARP-CF-D/E 被选中 0 次。
    WHY 按线而不是按 IP 排序：IP 使用表里出窗口的条目会被清理，清理后所有
    备线的 ts 并列 0，排序退化成 members 顺序，等于没修。
    """
    _warp_env(monkeypatch, WARP_TWO + [
        {"tag": "W3", "port": 1082, "service": "usque-warp-c.service"}])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: {
        1080: "ip-w1", 1081: "ip-w2", 1082: "ip-w3"}[port])
    monkeypatch.setattr(daemon.time, "time", lambda: 100_000.0)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)
    # 两个出口的 ts 都在复用窗口外 → 都算新鲜；轮转只看上次被选的时刻。
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-w2": {"ts": 50_000.0, "blocked": False},
        "ip-w3": {"ts": 10_000.0, "blocked": False},
    })
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {
        "W2": {"sel": 50_000.0, "ip": "ip-w2"},
        "W3": {"sel": 10_000.0, "ip": "ip-w3"},
    })
    monkeypatch.setattr(daemon, "_restart_service", lambda s, tag=None: True)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    assert daemon.reconnect_warp("test") is True
    assert switched == ["W3"], f"应选最久没被用过的 W3，实际 {switched}"
    assert daemon._WARP_LINE_STATE["W3"]["sel"] == 100_000.0, \
        "选中的线要记下选择时刻，否则下次仍会被重复选中"


def test_proactive_rotate_switches_after_n_dsv4_ok_without_errors(monkeypatch):
    """出口还在成功服务时，已用配额到阈值就主动换线，不等 429 冲断对话。

    配额按**成功的 dsv4 调用**扣，所以判据也数这个：被 429 拒掉的请求不占配额，
    按它计数会在风暴期越切越勤。这里断言没有任何错误信号，仅凭配额计数就换线。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"], "proactive_mode": "requests",
        "proactive_after_dsv4_ok": 20, "cooldown_ok_sec": 20})
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 1000.0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 1000.0)
    monkeypatch.setattr(daemon, "count_dsv4_ok_since", lambda ts: 20)
    fired = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now, ignore_cooldown=False:
                        fired.append(reason) or True)

    daemon._maybe_proactively_rotate_warp(2000.0)
    assert len(fired) == 1 and fired[0].startswith("主动轮换 已用配额 20 格 dsv4"), \
        f"应主动换线，实际 {fired}"
    # 换线路径本来就会打 `↻ WARP 换线 (主动轮换 已服务 20 条)`，这里不得重复
    assert daemon._last_warp_reconnect_ts == 1000.0, "本函数不该自己改换线窗口起点"


def test_proactive_rotate_counts_from_switch_effective_time(monkeypatch):
    """配额窗口从「换线生效」算起，不从「换线发起」算起。

    发起早于生效数秒，期间旧出口仍在服务流量；用发起时刻会把旧线的消耗
    算进新线配额，导致新线刚切上去就被主动换掉。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "proactive_mode": "requests",
                         "proactive_after_dsv4_ok": 1})
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 1000.0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 1010.0)
    seen = []
    monkeypatch.setattr(daemon, "count_dsv4_ok_since",
                        lambda ts: seen.append(ts) or 0)
    fired = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now, ignore_cooldown=False:
                        fired.append(reason) or True)

    daemon._maybe_proactively_rotate_warp(1100.0)
    assert seen == [1010.0], f"须以生效时刻为窗口起点，实际 {seen}"
    assert not fired, "计数为 0 不得换线"


def test_reconnect_warp_records_switch_effective_time(monkeypatch):
    """换线成功后记下生效时刻，主动轮换据此切窗口。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: {1080: "ip-w1", 1081: "ip-w2"}[port])
    monkeypatch.setattr(daemon.time, "time", lambda: 7_000_000.0)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: True)

    assert daemon.reconnect_warp("test") is True
    assert daemon._last_warp_switch_ok_ts == 7_000_000.0


def test_proactive_rotate_respects_cooldown(monkeypatch):
    """换线冷却期内即使满足阈值也不得再换，否则隧道变永动机。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"], "proactive_mode": "requests",
        "proactive_after_dsv4_ok": 1})
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 1030.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 1000.0)
    monkeypatch.setattr(daemon, "count_dsv4_ok_since", lambda ts: 999)
    fired = []

    def fake_reconnect(reason, now, ignore_cooldown=False):
        # 冷却由 _reconnect_warp_now 自己判（proactive 只负责给出忽略标志），
        # 桩必须保留这条语义，否则测的是桩而不是被测行为。
        if not ignore_cooldown and now < daemon._warp_cooldown_until:
            return False
        fired.append(reason)
        return True

    monkeypatch.setattr(daemon, "_reconnect_warp_now", fake_reconnect)

    daemon._maybe_proactively_rotate_warp(1010.0)   # 冷却中
    assert not fired, "冷却期内不得主动换线"
    daemon._maybe_proactively_rotate_warp(1031.0)
    assert fired, "冷却结束后就该换"


def test_prewarm_batches_all_stale_standby_lines(monkeypatch):
    """一轮把**所有**低质备线排进补刷，跳过当前线和新鲜线。

    旧实现每轮只取 min(cands, key=sel) 一条，实测全库只预热过 11 次且集中
    在两三条线上；脏线轮不完，换线时可选干净线随之变少。
    """
    _warp_env(monkeypatch, WARP_TWO + [
        {"tag": "W3", "port": 1082, "service": "usque-warp-c.service"}])
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "prewarm_sec": 120})
    monkeypatch.setattr(daemon, "_last_warp_prewarm_ts", 0.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {
        "W1": {"sel": 900.0, "ip": "ip-w1"},
        "W2": {"sel": 500.0, "ip": "ip-w2"},     # 旧 IP，需预热
        "W3": {"sel": 300.0, "ip": "ip-w3"},     # 旧 IP，需预热
    })
    monkeypatch.setattr(daemon, "_warp_ip_recently_used",
                        lambda ip, now: ip != "fresh-ip")
    monkeypatch.setattr(daemon, "count_opencode_since", lambda ts: 100)
    scheduled = []
    monkeypatch.setattr(daemon, "_schedule_idle_refresh",
                        lambda m: scheduled.append(m["tag"]))

    daemon._prewarm_standby_warp(1000.0)
    assert sorted(scheduled) == ["W2", "W3"], \
        f"应批量排入两条脏线，实际 {scheduled}"

    # 已知 IP 仍新鲜 → 不重启它（白换掉一个好会话）
    scheduled.clear()
    daemon._WARP_LINE_STATE["W3"]["ip"] = "fresh-ip"
    monkeypatch.setattr(daemon, "_last_warp_prewarm_ts", 0.0)
    daemon._prewarm_standby_warp(1000.0)
    assert scheduled == ["W2"], f"新鲜线应跳过，实际 {scheduled}"


def test_prewarm_skips_only_the_line_already_being_refreshed(monkeypatch):
    """正在刷的那条线不重复排队，其余照排 —— 并发不设全池上限。

    全池上限 `refresh_concurrency` 已删除（互斥粒度改为单条线），所以「名额满」
    不再是放弃整轮的理由。仍要守的是：`_WARP_REFRESHING` 里的线不得再排一次，
    否则同一条隧道会被两条线程同时重启。
    """
    _warp_env(monkeypatch, WARP_TWO + [
        {"tag": "W3", "port": 1082, "service": "usque-warp-c.service"}])
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"],
                                              "prewarm_sec": 120})
    monkeypatch.setattr(daemon, "_last_warp_prewarm_ts", 0.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", {"W2"})   # 这条正在刷
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {
        "W2": {"sel": 500.0, "ip": "ip-w2"},
        "W3": {"sel": 300.0, "ip": "ip-w3"},
    })
    monkeypatch.setattr(daemon, "_warp_ip_recently_used", lambda ip, now: True)
    monkeypatch.setattr(daemon, "count_opencode_since", lambda ts: 100)
    scheduled = []
    monkeypatch.setattr(daemon, "_schedule_idle_refresh",
                        lambda m: scheduled.append(m["tag"]))

    daemon._prewarm_standby_warp(1000.0)
    assert scheduled == ["W3"], \
        f"名额满仍应排队（W2 在重启中不重复排），实际 {scheduled}"


def test_prewarm_batches_lines_without_success_record_in_model_mode(monkeypatch):
    """模型模式下「有 IP 但从没成功服务过」的线也要批量刷。

    模型模式原先只看 bool(ip) 就算新鲜，一条能出地址但跑不出流量的线永远
    不会被预热 —— 它只会在换线时被当兜底选中。
    """
    _warp_env(monkeypatch, WARP_TWO + [
        {"tag": "W3", "port": 1082, "service": "usque-warp-c.service"}])
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"],
                                              "prewarm_sec": 120,
                                              "refresh_model_check": True})
    monkeypatch.setattr(daemon, "_last_warp_prewarm_ts", 0.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {
        "W2": {"sel": 500.0, "ip": "ip-w2"},
        "W3": {"sel": 300.0, "ip": "ip-w3"},
    })
    # W2 有成功记录（次级优质），W3 有记录但从没成功过（档位 2 = 低质）。
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-w2": {"ts": 100.0, "last_ok_ts": 100.0},
        "ip-w3": {"ts": 100.0},
    })
    monkeypatch.setattr(daemon, "count_opencode_since", lambda ts: 100)
    scheduled = []
    monkeypatch.setattr(daemon, "_schedule_idle_refresh",
                        lambda m: scheduled.append(m["tag"]))

    daemon._prewarm_standby_warp(1000.0)
    assert scheduled == ["W3"], \
        f"模型模式下从没成功过的线必须刷，实际 {scheduled}"


def test_prewarm_skips_when_no_opencode_traffic(monkeypatch):
    """没有流量时不预热：重启隧道换 IP 只是白换掉一个会话。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "prewarm_sec": 120})
    monkeypatch.setattr(daemon, "_last_warp_prewarm_ts", 0.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {"W2": {"sel": 1.0, "ip": "ip-w2"}})
    monkeypatch.setattr(daemon, "count_opencode_since", lambda ts: 0)
    scheduled = []
    monkeypatch.setattr(daemon, "_schedule_idle_refresh",
                        lambda m: scheduled.append(m["tag"]))

    daemon._prewarm_standby_warp(1000.0)
    assert not scheduled, f"无流量不得预热，实际 {scheduled}"

def test_prewarm_refreshes_blocked_line_without_success_record(monkeypatch):
    """被 403/429 标记的 IP 必须重连，哪怕它从没成功服务过。

    旧判据只看「窗口内有无成功流量」：blocked 但没有 last_ok_ts 的地址被当成
    新鲜线跳过，5 条线里的脏 IP 永远刷不掉。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "_last_warp_prewarm_ts", 0.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {
        "W2": {"sel": 500.0, "ip": "burned-ip"},
    })
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "burned-ip": {"ts": 900.0, "blocked": True},   # 从没有 last_ok_ts
    })
    monkeypatch.setattr(daemon, "count_opencode_since", lambda ts: 100)
    scheduled = []
    monkeypatch.setattr(daemon, "_schedule_idle_refresh",
                        lambda m: scheduled.append(m["tag"]))

    daemon._prewarm_standby_warp(1000.0)

    assert scheduled == ["W2"], f"blocked 备线必须重连，实际 {scheduled}"

def test_reconnect_warp_schedules_refresh_for_blocked_standbys(monkeypatch):
    """换到干净线的同时，剩余脏备线也要排进补刷，不必等下一轮预热。"""
    _warp_env(monkeypatch, WARP_TWO + [{"tag": "W3", "port": 1082,
                                        "service": "usque-warp-c.service"}])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: {
        1080: "ip-w1", 1081: "burned-b", 1082: "clean-c"}[port])
    monkeypatch.setattr(daemon.time, "time", lambda: 100_000.0)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-w1": {"ts": 100_000.0, "blocked": True},
        "burned-b": {"ts": 100_000.0, "blocked": True},
        "clean-c": {"ts": 1000.0, "blocked": False},   # 27.5h 前用过
    })
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    scheduled = []
    monkeypatch.setattr(daemon, "_schedule_idle_refresh",
                        lambda m: scheduled.append(m["tag"]))
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: True)

    assert daemon.reconnect_warp("abuse403") is True
    assert scheduled == ["W1", "W2"], \
        f"刚换下的旧线与脏备线都要补刷，实际 {scheduled}"


def test_dirty_janitor_reclaims_all_non_active_lines(monkeypatch):
    """脏线守护：为每条非当前出口的线起动重连，当前出口不动。

    当前出口重启会掐断在途连接，只能等它被换下后由换线路径刷。
    """
    _warp_env(monkeypatch, WARP_TWO + [{"tag": "W3", "port": 1082,
                                        "service": "usque-warp-c.service"}])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W2")
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_WARP_REFRESH_PENDING", set())
    launched = []

    class T:
        def __init__(self, target=None, args=(), **k):
            self._t, self._a = target, args
        def start(self):
            launched.append(self._a[0]["tag"])

    monkeypatch.setattr(daemon.threading, "Thread", T)
    daemon._reclaim_dirty_pass()

    assert launched == ["W1", "W3"], f"当前出口 W2 不得重连，实际 {launched}"
    assert "W2" not in daemon._WARP_REFRESHING


def test_dirty_worker_aborts_when_member_becomes_active(monkeypatch):
    """探测期间这条线被切成当前出口 → 放弃重连，不得重启。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: "burned-ip")
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {"burned-ip": {"ts": 1000.0, "blocked": True}})
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)
    # 探测完成后它已是当前出口
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    restarted = []
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda s, tag=None: restarted.append(s) or True)
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())

    daemon._reclaim_dirty_warp_worker({"tag": "W1", "port": 1080,
                                       "service": "usque-warp.service"})

    assert not restarted, f"已成当前出口的线不得重启，实际 {restarted}"


def test_dirty_worker_waits_for_idle_before_restart(monkeypatch):
    """脏线仍在传数据时不得重启 —— 重启会掐断在途 SSE（upstream connection lost）。

    错误路径（同步刷新）会等 3s 后改刷别的线；脏线守护没有别的线可换，
    就下一轮再试。旧实现拿到名额直接重启，实测 02:44 换线后 19 秒把
    CF/B/C 全部重启，正好掐在刚被换下、仍在传流的连接上。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: "burned-ip")
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {"burned-ip": {"ts": 1000.0, "blocked": True}})
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W2")
    monkeypatch.setitem(daemon.CFG["warp"], "ip_refresh_retry_sec", 0)
    restarted = []
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda s, tag=None: restarted.append(s) or True)
    monkeypatch.setattr(daemon, "warp_line_busy", lambda tag, window=2.0: True)

    daemon._reclaim_dirty_warp_worker({"tag": "W1", "port": 1080,
                                       "service": "usque-warp.service"})

    assert not restarted, f"仍在传数据的线不得重启，实际 {restarted}"
    assert "W1" not in daemon._WARP_REFRESHING, "提前退出也必须释放名额"


def test_acquire_refresh_slot_is_exclusive_per_tag(monkeypatch):
    """同一 tag 二次获取必须失败 —— 否则两条线程同时刷同一条隧道。

    实测 WARP-CF-C 02:31:49~02:32:03 出现 #1#1#2#2#3：两个刷新循环各跑
    一遍重启，白烧一次「疑似脏 IP」探测。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    assert daemon._acquire_refresh_slot("W1") is True
    assert daemon._acquire_refresh_slot("W1") is False, "同一 tag 不得重入"
    assert daemon._acquire_refresh_slot("W2") is True
    daemon._release_refresh_slot("W1")
    assert daemon._acquire_refresh_slot("W1") is True


def test_egress_label_shows_warp_with_ip(monkeypatch):
    """走 WARP 时标签须带 tag 与出口 IP —— 排查第一步是知道流量走哪条路。"""
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "members": WARP_TWO})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W2")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "2a09:bac5::1")
    assert daemon._egress_label("some-node") == "WARP:W2(2a09:bac5::1)"


def test_egress_label_shows_proxy_when_on_pool(monkeypatch):
    """停在轮换池时标签须显示 proxy 与节点名。"""
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)
    assert daemon._egress_label("pq.aws50.yydjc.top:443-vl") == "PROXY:pq.aws50.yydjc.top:443-vl"
    assert daemon._egress_label(None) == "PROXY:?"


def test_egress_label_unknown_ip_marked(monkeypatch):
    """IP 未知时标 '?'，不得为了一条日志去探测（会阻塞主循环 12s）。"""
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "members": WARP_TWO})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", None)
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda p: pytest.fail("日志标签不得触发 SOCKS 探测"))
    assert daemon._egress_label() == "WARP:W1(?)"


def test_current_mode_distinguishes_unknown_from_pool(monkeypatch):
    """查不到 ≠ 在轮换池：前者返回 unknown，避免重启时把 WARP 静默换成池。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "singbox_api", lambda *a, **kw: (0, None))
    monkeypatch.setattr(daemon, "_clash_snapshot", lambda sel: None)
    assert daemon._current_mode() == ("unknown", None), "无直查也无快照应为 unknown"

    # 看门狗重启前 sing-box 已死：退回快照里的最后已知模式
    monkeypatch.setattr(daemon, "_clash_snapshot", lambda sel: {"now": "W1"})
    assert daemon._current_mode() == ("warp", "W1"), "查不到时应退回快照的已知模式"
    monkeypatch.setattr(daemon, "_clash_snapshot", lambda sel: {"now": "proxy"})
    assert daemon._current_mode() == ("proxy", None), "快照停在池应判为 pool"


def test_switch_warp_logs_mode_transition_only(monkeypatch):
    """模式切换（warp<->proxy）必须留日志，同模式内换线（W1->W2）不得刷屏。

    before 走直查，所以 stub singbox_api 的 GET 返回的 "now" 就是当前生效的 selector。
    """
    _warp_env(monkeypatch, WARP_TWO)
    logs = []
    state = {"now": "W1"}
    monkeypatch.setattr(daemon, "log", logs.append)
    monkeypatch.setattr(daemon, "_publish_clash_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(daemon, "singbox_api",
                        lambda *a, method="GET", **kw: (200, {"now": state["now"]}))

    daemon._switch_warp("W2")
    assert not logs, f"同模式内换线不该产生模式日志，实际 {logs}"

    state["now"] = "proxy"
    daemon._switch_warp("W1")
    assert len(logs) == 1 and "proxy -> warp" in logs[0], f"实际 {logs}"

    state["now"] = "W1"
    daemon._switch_warp("proxy")
    assert len(logs) == 2 and "warp -> proxy" in logs[1], f"实际 {logs}"

def test_warp_failover_refreshes_blocked_line_instead_of_proxy(monkeypatch):
    """备线出口全被标记 → 刷新一条拿新 IP 再切，不直接落 proxy 池。

    实测 19:16：5 条线的当前 IP 全部标脏后候选池恒为 0，daemon 落进
    「选线 0 条 → 切 proxy → 池也 403」的兜底循环。
    """
    _warp_env(monkeypatch, WARP_TWO + [{"tag": "W3", "port": 1082,
                                        "service": "usque-warp-c.service"}])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: True)
    egress = {"1080": "ip-w1", "1081": "burned-b", "1082": "burned-c"}
    restarted = []

    def _restart(service, tag=None):
        restarted.append(service)
        if "warp-b" in service:
            egress["1081"] = "new-b"        # 重启换出全新会话 IP
        if "warp-c" in service:
            egress["1082"] = "new-c"
        return True

    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: egress[str(port)])
    monkeypatch.setattr(daemon, "_restart_service", _restart)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-w1": {"ts": 1000.0, "blocked": True},
        "burned-b": {"ts": 1000.0, "blocked": True},
        "burned-c": {"ts": 1000.0, "blocked": True},
    })
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    assert daemon.reconnect_warp("abuse403") is True
    assert len(restarted) == 1, f"只刷新一条备线即可，实际 {restarted}"
    assert switched == ["W2"], f"应切到刷新后的 W2，实际 {switched}"

def test_warp_failover_falls_back_when_blocked_refresh_fails(monkeypatch):
    """刷新后仍是同一批被标记地址 → 此时才落 proxy 池。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", True)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: True)
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: {1080: "ip-w1", 1081: "ip-w2"}[port])
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-w1": {"ts": 999.0, "blocked": True},
        "ip-w2": {"ts": 999.0, "blocked": True},
    })
    monkeypatch.setattr(daemon, "_restart_service", lambda s, tag=None: True)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    assert daemon.reconnect_warp("abuse403") is True
    assert switched == ["proxy"], f"刷新不出新地址才落 proxy，实际 {switched}"


def test_warp_failover_uses_unblocked_line_when_mixed(monkeypatch):
    """一条被标记、另一条干净 → 用干净的那条，不必切 proxy。

    ip_reuse_guard_sec=86400 (24h)，所以 dry-b 必须至少 24h+ 前使用过才算新鲜。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: {
        "1080": "burned-a", "1081": "clean-b"}[str(port)])
    # 当前时间 = 100_000s，clean-b 使用于 1000s → 已过 99000s ≈ 27.5h > 24h
    monkeypatch.setattr(daemon.time, "time", lambda: 100_000.0)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "burned-a": {"ts": 100_000.0, "blocked": True},
        "clean-b": {"ts": 1000.0, "blocked": False},   # 24h+ 前用过，直接可切
    })
    monkeypatch.setattr(daemon, "_restart_service", lambda s, tag=None: True)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    assert daemon.reconnect_warp("test") is True
    assert switched == ["W2"], f"应切到未标记的 W2，实际 {switched}"


def test_proxy_pool_healthy_true_when_any_node_probes(monkeypatch):
    """池里只要有一个节点能通就仍可兜底。"""
    monkeypatch.setattr(daemon, "singbox_api",
                        lambda path, **kw: (200, {"all": ["node-a", "node-b", "node-c"]}))
    monkeypatch.setattr(daemon, "_probe_node", lambda node, t, **kw: 100 if node == "node-b" else None)
    assert daemon._proxy_pool_healthy() is True


def test_proxy_pool_healthy_false_when_all_samples_fail(monkeypatch):
    """采样节点全不通 → 池不可用，应切回 WARP。"""
    monkeypatch.setattr(daemon, "singbox_api",
                        lambda path, **kw: (200, {"all": ["node-a", "node-b", "node-c"]}))
    monkeypatch.setattr(daemon, "_probe_node", lambda node, t, **kw: None)
    assert daemon._proxy_pool_healthy() is False


def test_proxy_pool_healthy_none_when_query_fails(monkeypatch):
    """查不到节点列表时返回 None，不得据此判定不可用。"""
    monkeypatch.setattr(daemon, "singbox_api", lambda path, **kw: (500, None))
    assert daemon._proxy_pool_healthy() is None


def test_proxy_pool_health_ignores_warp_members(monkeypatch):
    """WARP 成员不是轮换池节点，不应参与池健康判定。"""
    seen = []
    monkeypatch.setattr(daemon, "singbox_api",
                        lambda path, **kw: (200, {"all": ["WARP-CF", "WARP-CF-B", "node-a"]}))
    monkeypatch.setattr(daemon, "_probe_node",
                        lambda node, t, **kw: seen.append(node) or 100)
    assert daemon._proxy_pool_healthy() is True
    assert all(not x.startswith("WARP-") for x in seen), f"不应探测 WARP 成员: {seen}"


def test_warp_any_reachable(monkeypatch):
    """三条隧道任一能出 IP 即可用。"""
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: "2a09::1" if port == 1082 else None)
    assert daemon._warp_any_reachable() is True

    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: None)
    assert daemon._warp_any_reachable() is False


def _failback_env(monkeypatch, *, healthy, blocked=(0, 0), dead=0, nodes=80,
                  reachable=True, dwell=1000.0):
    """搭好 _maybe_failback_to_warp 的最小环境，返回被记录的切回 reason 列表。"""
    monkeypatch.setattr(daemon, "opencode_out_on_warp", lambda: False)
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda **kw: healthy)
    # limit=None 是回切调用点的签名：回切要数「落池以来累计」，不受
    # recent_window=2 限制（旧桩不收 limit → 10 个回切用例全 TypeError）。
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts, limit=None: {"abuse403": blocked[0],
                                                "rate429": blocked[1]})
    pool = [f"n{i}" for i in range(nodes)]
    monkeypatch.setattr(daemon, "_pool_nodes", lambda: pool)
    # 黑名单必须是池内节点，否则占比恒为 0
    monkeypatch.setattr(daemon, "_DEAD_NODES", set(pool[:dead]))
    monkeypatch.setattr(daemon, "_warp_any_reachable", lambda: reachable)
    monkeypatch.setattr(daemon, "_last_proxy_pool_check", 0.0)
    monkeypatch.setattr(daemon, "_proxy_dwell_since", dwell)
    monkeypatch.setattr(daemon, "_proxy_fail_streak", 0)
    called = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now: called.append(reason) or True)
    return called


def test_failback_after_consecutive_pool_probe_failures(monkeypatch):
    """池健康探测必须**连续**失败到阈值才回切，单次失败不切。"""
    called = _failback_env(monkeypatch, healthy=False)
    for _ in range(daemon.CFG["intervals"]["proxy_fail_streak"] - 1):
        daemon._last_proxy_pool_check = 0.0
        assert daemon._maybe_failback_to_warp(1e6) is False, "未到阈值不应回切"
    daemon._last_proxy_pool_check = 0.0
    assert daemon._maybe_failback_to_warp(1e6) is True
    assert called and "连续失败" in called[0], called


def test_failback_streak_resets_when_pool_recovers(monkeypatch):
    """中间探测成功一次就把连续失败计数清零 —— 抖动不该被累积成判死。"""
    _failback_env(monkeypatch, healthy=False)
    streak = daemon.CFG["intervals"]["proxy_fail_streak"]
    for _ in range(streak - 1):
        daemon._last_proxy_pool_check = 0.0
        daemon._maybe_failback_to_warp(1e6)
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda **kw: True)
    daemon._last_proxy_pool_check = 0.0
    daemon._maybe_failback_to_warp(1e6)
    assert daemon._proxy_fail_streak == 0, "池恢复后连续失败计数须清零"


def test_failback_when_still_blocked_after_landing_on_pool(monkeypatch):
    """落到池上后仍被 403/429 拦截到阈值 → 池节点 IP 也被烧了，回切 WARP。"""
    thr = daemon.CFG["intervals"]["proxy_blocked_threshold"]
    called = _failback_env(monkeypatch, healthy=True, blocked=(thr, 0))
    assert daemon._maybe_failback_to_warp(1e6) is True
    assert called and "仍拦截" in called[0], called


def test_failback_not_triggered_by_few_blocked(monkeypatch):
    """零星拦截不触发回切 —— 池还在服务流量。"""
    thr = daemon.CFG["intervals"]["proxy_blocked_threshold"]
    _failback_env(monkeypatch, healthy=True, blocked=(thr - 1, 0))
    assert daemon._maybe_failback_to_warp(1e6) is False


def test_failback_when_blacklist_ratio_high(monkeypatch):
    """黑名单节点占比到阈值 → 池可用容量耗光，回切 WARP。"""
    called = _failback_env(monkeypatch, healthy=True, nodes=80, dead=60)
    assert daemon._maybe_failback_to_warp(1e6) is True
    assert called and "黑名单" in called[0], called


def test_failback_stays_on_healthy_pool(monkeypatch):
    """WARP 复活本身不是回切依据：池健康就留在池上，把兜底资源用完。"""
    _failback_env(monkeypatch, healthy=True)
    assert daemon._maybe_failback_to_warp(1e6) is False, \
        "池健康时不得仅因 WARP 可达就切回"


def test_failback_skipped_when_already_on_warp(monkeypatch):
    """已经走 WARP 时不得重复触发。"""
    monkeypatch.setattr(daemon, "opencode_out_on_warp", lambda: True)
    monkeypatch.setattr(daemon, "_last_proxy_pool_check", 0.0)
    called = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now: called.append(reason) or True)
    assert daemon._maybe_failback_to_warp(1e6) is False
    assert not called


def test_failback_skipped_when_warp_unreachable(monkeypatch):
    """池挂了但 WARP 也不通——切过去没有意义。"""
    called = _failback_env(monkeypatch, healthy=False, reachable=False)
    for _ in range(daemon.CFG["intervals"]["proxy_fail_streak"]):
        daemon._last_proxy_pool_check = 0.0
        daemon._maybe_failback_to_warp(1e6)
    assert not called


def test_failback_respects_probe_interval(monkeypatch):
    """探测间隔内不重复探测，避免每 tick 都打 83 节点池。"""
    probed = []
    _failback_env(monkeypatch, healthy=True)
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda **kw: probed.append(1) or True)

    daemon._maybe_failback_to_warp(1e6)
    n1 = len(probed)
    daemon._maybe_failback_to_warp(1e6)   # 同 tick，应被间隔挡住
    assert len(probed) == n1, f"间隔内不应重复探测，实际 {n1} -> {len(probed)}"


def test_unknown_pool_state_does_not_erase_streak(monkeypatch):
    """池状态查不到（None）时既不算失败也不算成功：不得清零已积累的连续失败。

    Clash API 抖动会返回 None；若按「非 False 就清零」处理，一次 API 抖动
    就把攒够的证据抹掉，池真的挂了也永远回切不了。
    """
    _failback_env(monkeypatch, healthy=None)
    thr = daemon.CFG["intervals"]["proxy_fail_streak"]
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda **kw: False)
    for _ in range(thr - 1):
        daemon._last_proxy_pool_check = 0.0
        daemon._maybe_failback_to_warp(1e6)
    assert daemon._proxy_fail_streak == thr - 1

    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda **kw: None)
    daemon._last_proxy_pool_check = 0.0
    daemon._maybe_failback_to_warp(1e6)
    assert daemon._proxy_fail_streak == thr - 1, "未知状态不得改动连续失败计数"


def test_switch_stamps_ip_with_actual_switch_time(monkeypatch):
    """换线成功时记 IP 使用时刻必须取当下，而非可能已过几分钟的调用方 now。

    now 取自探测/重启之前；拿它当 1h 复用窗口起点会让地址被提前放回候选池。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: {
        1080: "ip-w1", 1081: "ip-w2"}[port])
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: True)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)
    monkeypatch.setattr(daemon.time, "time", lambda: 9_000_000.0)

    daemon.reconnect_warp("test")
    assert daemon._WARP_IP_USED["ip-w2"]["ts"] == 9_000_000.0, \
        f"须记录切换生效时刻，实际 {daemon._WARP_IP_USED['ip-w2']['ts']}"


def test_failback_keeps_evidence_when_switch_not_started(monkeypatch):
    """冷却中/有换线在跑导致没发起切换时，不得清空已积累的判据。

    _reconnect_warp_now 在冷却或 _WARP_LOCK 抢不到时返回 False；此时清计数
    等于把证据丢掉，下次还得重新攒够阈值。
    """
    _failback_env(monkeypatch, healthy=False)
    monkeypatch.setattr(daemon, "_reconnect_warp_now", lambda reason, now: False)
    thr = daemon.CFG["intervals"]["proxy_fail_streak"]
    for _ in range(thr):
        daemon._last_proxy_pool_check = 0.0
        daemon._maybe_failback_to_warp(1e6)
    assert daemon._proxy_fail_streak >= thr, "未发起切换时连续失败计数不得清零"


def test_fallback_stamps_actual_landing_time_not_stale_caller_now(monkeypatch):
    """落池时刻须取切换生效的当下，而非可能已过去几分钟的调用方 now。

    reconnect_warp 的 now 取自探测/重启之前；用它当 _proxy_dwell_since 会让
    「落池后仍拦截」判据把落池前的旧 403/429 也算进去。
    """
    monkeypatch.setattr(daemon, "opencode_out_on_warp", lambda: True)
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", True)
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: True)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)
    monkeypatch.setattr(daemon, "_proxy_dwell_since", 0.0)
    monkeypatch.setattr(daemon.time, "time", lambda: 5_000_000.0)

    daemon._fallback_to_proxy({"tag": "W1"}, "ip-1", now=1_000_000.0)
    assert daemon._proxy_dwell_since == 5_000_000.0, \
        f"须记录真实落池时刻，实际 {daemon._proxy_dwell_since}"


def test_reconnect_now_bypasses_error_threshold(monkeypatch):
    """绕阈值入口不查 count_warp_blocked_since，冷却与锁仍生效。"""
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0.0)
    monkeypatch.setattr(daemon, "_WARP_LOCK", threading.Lock())
    called = []
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: called.append("blocked") or {"abuse403": 0, "rate429": 0})
    spawned = []
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda self: spawned.append(1)})())
    assert daemon._reconnect_warp_now("测试", 1e6) is True
    assert not called, "不应查错误计数"
    assert spawned

    # 冷却内不得重复发起
    assert daemon._reconnect_warp_now("测试", 1e6) is False


def test_warp_ip_records_served_sec_on_first_block(monkeypatch):
    """IP 被标记时记录存活时长，供事后评估；重复标记不覆盖首次值。

    只存"最后使用时刻"无法回答"这个地址活了多久"——实测休息时长与存活无关，
    真正要收集的是每次使用的寿命。
    """
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "ip_reuse_guard_sec": 86400})
    ip = "2a09:bac5:1efa:1173::1bd:e7"
    daemon._mark_warp_ip_used(ip, 1_000.0)
    daemon._mark_warp_ip_used(ip, 1_064.0, blocked=True, kind="abuse403")
    entry = daemon._WARP_IP_USED[ip]
    assert entry["served_sec"] == 64.0, f"存活时长应为 64s，实际 {entry.get('served_sec')}"
    assert entry["blocked_ts"] == 1_064.0
    assert entry["blocked_kind"] == "abuse403"
    assert entry["blocked_ts_by_kind"] == {"abuse403": 1_064.0}
    # 同一轮里再被 429 烧：两类各自计时、并存，展示字段仍取最早的一类
    daemon._mark_warp_ip_used(ip, 2_000.0, blocked=True, kind="rate429")
    assert entry["served_sec"] == 64.0, "重复标记不得用更晚的时间覆盖首次存活时长"
    assert entry["blocked_ts"] == 1_064.0, "展示字段保留最早的惩罚起点"
    assert entry["blocked_kind"] == "abuse403", "展示字段保留最早的 kind"
    assert entry["blocked_ts_by_kind"] == {"abuse403": 1_064.0, "rate429": 2_000.0}, \
        f"两类惩罚须各自留档，实际 {entry['blocked_ts_by_kind']}"


def test_retry_refresh_skips_restart_when_already_fresh(monkeypatch):
    """已有窗口外新鲜 IP 时不再重启隧道，避免把预热好的会话白白刷掉。"""
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "ip_refresh_retry_sec": 600})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_warp_line_ip", lambda tag: "fresh-ip")
    monkeypatch.setattr(daemon, "_warp_ip_recently_used", lambda ip, now: False)
    restarted = []
    monkeypatch.setattr(daemon, "_refresh_idle_warp",
                        lambda m: restarted.append(m) or "x")
    member = {"tag": "W2", "port": 1081, "service": "s"}
    assert daemon._retry_refresh_idle_warp(member) is True
    assert not restarted, "IP 已经新鲜就不该再重启隧道"


def test_reconnect_worker_does_not_leak_lock_when_switch_raises(monkeypatch):
    """reconnect_warp 抛异常时锁必须释放，否则换线路径永久卡死。"""
    monkeypatch.setattr(daemon, "_WARP_LOCK", threading.Lock())
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "cooldown_fail_sec": 60})
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0.0)
    monkeypatch.setattr(daemon, "reconnect_warp",
                        lambda reason: (_ for _ in ()).throw(RuntimeError("boom")))
    daemon._WARP_LOCK.acquire()
    daemon._warp_reconnect_worker("测试", 20.0)
    assert daemon._WARP_LOCK.acquire(blocking=False), "异常路径未释放 _WARP_LOCK"
    daemon._WARP_LOCK.release()


def test_second_instance_is_rejected_by_lock(tmp_path, monkeypatch):
    """第二个 daemon 实例必须被 PID 锁拒绝 —— 两实例各自整份覆写 IP 表会让去重失效。"""
    lock = tmp_path / "daemon.lock"
    monkeypatch.setattr(daemon, "_DAEMON_LOCK_FILE", str(lock))
    monkeypatch.setattr(daemon, "_DAEMON_LOCK_FD", None)
    import fcntl as _fcntl
    holder = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    _fcntl.flock(holder, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    try:
        with pytest.raises(SystemExit):
            daemon._acquire_single_instance_lock()
    finally:
        _fcntl.flock(holder, _fcntl.LOCK_UN)
        os.close(holder)


def test_rest_time_selection_is_only_a_fallback(monkeypatch):
    """所有备线 IP 都在窗口内时按「最久没用过」兜底，且仍走刷新路径。

    休息时长不参与"该不该复用"的判断（实测休息越久存活并不更长），
    只用来在被迫复用时挑最老的那个。
    """
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "ip_reuse_guard_sec": 86400})
    now = 1_000_000.0
    a = daemon._warp_rest_time("old-ip", now)
    b = daemon._warp_rest_time("new-ip", now)
    monkeypatch.setattr(daemon, "_warp_ip_last_ts",
                        lambda ip: 100.0 if ip == "old-ip" else 900_000.0)
    assert daemon._warp_rest_time("old-ip", now) > daemon._warp_rest_time("new-ip", now)
    assert a >= 0 and b >= 0


# ============= 周期样本与最小可用周期 =============
def test_warp_ip_reuse_window_24h(monkeypatch):
    """复用窗口是 24h：23h 前用过仍算「窗口内」，25h 前的条目被清理后可再用。"""
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "ip_reuse_guard_sec": 86400})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    now = 1_000_000.0
    daemon._mark_warp_ip_used("recent-23h", now - 23 * 3600)
    # stale-25h 在写入时就会被清理，先塞成功时刻没有意义 —— 它本来就该消失
    daemon._mark_warp_ip_used("stale-25h", now - 25 * 3600)
    daemon._mark_warp_ip_used("current", now)
    daemon._WARP_IP_USED["recent-23h"]["last_ok_ts"] = now - 23 * 3600

    assert daemon._warp_ip_recently_used("recent-23h", now), "23h 前成功过仍在 24h 窗口内"
    assert not daemon._warp_ip_recently_used("stale-25h", now), "25h 前成功过应视为未使用"
    assert "stale-25h" not in daemon._WARP_IP_USED, "超 24h 的条目应被清理"


def test_reconnect_warp_picks_longest_rest_when_none_fresh(monkeypatch):
    """所有备线 IP 都在 24h 内时，兜底选休息时间最长的那条（而非 probes 顺序）。"""
    three = WARP_TWO + [{"tag": "W3", "port": 1082, "service": "usque-warp-c.service"}]
    egress = _reuse_env(monkeypatch, {})
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "members": three,
                                             "ip_reuse_guard_sec": 86400})
    now = 1_000_000.0
    egress.update({"1080": "ip-w1", "1081": "ip-w2", "1082": "ip-w3"})
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port, timeout_sec=20.0: egress[str(port)])
    # 三条都在 24h 窗口内；W3 休息最久，W2 次之，W1 最新
    # 三条都在 24h 内成功过（窗口内），W3 最久未成功 → 应选它
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-w1": {"ts": now - 60, "blocked": False, "last_ok_ts": now - 60},
        "ip-w2": {"ts": now - 3600, "blocked": False, "last_ok_ts": now - 3600},
        "ip-w3": {"ts": now - 7200, "blocked": False, "last_ok_ts": now - 7200},
    })
    monkeypatch.setattr(daemon, "warp_line_busy", lambda tag, window=2.0: False)
    refreshed = []
    monkeypatch.setattr(daemon, "_refresh_idle_warp",
                        lambda m: refreshed.append(m["tag"]) or f"fresh-{m['tag']}")
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)

    assert daemon.reconnect_warp("test") is True
    assert switched == ["W3"], f"应兜底选休息最久的 W3，实际 {switched}"
    assert refreshed == ["W3"], "兜底路径仍须先刷新该线会话 IP"


def test_fallback_selection_prefers_non_cooling_line(monkeypatch):
    """兜底选线必须在「不在离渠冷却中的线」里挑最久未用的那条。

    选到冷却中的线 `_refresh_idle_warp` 会直接返回 None，白把这次换线推到落池。
    用例守的是判据方向：`max()` 的 key 取「未冷却」为真，写反了就会优先挑
    冷却线（codex P2）。
    """
    three = WARP_TWO + [{"tag": "W3", "port": 1082, "service": "usque-warp-c.service"}]
    egress = _reuse_env(monkeypatch, {})
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "members": three,
                                             "ip_reuse_guard_sec": 86400})
    now = 1_000_000.0
    egress.update({"1080": "ip-w1", "1081": "ip-w2", "1082": "ip-w3"})
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: egress[str(port)])
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-w1": {"ts": now - 60, "blocked": False, "last_ok_ts": now - 60},
        "ip-w2": {"ts": now - 7200, "blocked": False, "last_ok_ts": now - 7200},
        "ip-w3": {"ts": now - 3600, "blocked": False, "last_ok_ts": now - 3600},
    })
    # W2 休息最久，但它刚被换下；W3 次之且可用 → 应选 W3
    # 离渠时刻按真实时钟记：`_standby_cooldown_left` 用的是 time.time()，
    # 而本用例的 now 是虚构值，两套时间轴不能混。
    real = time.time()
    monkeypatch.setattr(daemon, "_WARP_OFF_AGE",
                        {"W2": real - 10, "W3": real - 999})
    monkeypatch.setattr(daemon, "warp_line_busy", lambda tag, window=2.0: False)
    monkeypatch.setattr(daemon, "_refresh_idle_warp",
                        lambda m, **kw: f"fresh-{m['tag']}")
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)

    assert daemon.reconnect_warp("test") is True
    assert switched == ["W3"], f"应跳过离渠冷却中的 W2，实际 {switched}"


def test_error_path_uses_short_busy_window(monkeypatch):
    """错误换线路径判闲必须用 2s 短窗，不能吃后台的 busy_window_sec(12s)。

    12s 是给后台补刷的（那条路径可以慢慢等）。错误路径按候选逐条睡 12s，
    最坏 ~120s 才把坏出口切走，期间坏出口一直在服务失败请求。
    """
    three = WARP_TWO + [{"tag": "W3", "port": 1082, "service": "usque-warp-c.service"}]
    egress = _reuse_env(monkeypatch, {})
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "members": three,
                                             "ip_reuse_guard_sec": 86400})
    now = 1_000_000.0
    egress.update({"1080": "ip-w1", "1081": "ip-w2", "1082": "ip-w3"})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        ip: {"ts": now - 60, "blocked": False, "last_ok_ts": now - 60}
        for ip in ("ip-w1", "ip-w2", "ip-w3")})
    busy_windows: list = []
    monkeypatch.setattr(daemon, "warp_line_busy",
                        lambda tag, window=None: busy_windows.append(window) or True)
    wait_windows: list = []
    monkeypatch.setattr(
        daemon, "_wait_warp_idle",
        lambda m, timeout_sec=None, window=None:
            wait_windows.append(window) or False)
    monkeypatch.setattr(daemon, "_fallback_to_proxy", lambda *a: False)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)

    daemon.reconnect_warp("test")

    assert busy_windows, "兜底路径应先判候选是否在传数据"
    assert set(busy_windows) == {daemon._ERROR_PATH_BUSY_WINDOW_SEC}, \
        f"错误路径判闲窗必须统一为 2s，实际 {busy_windows}"
    assert wait_windows == [daemon._ERROR_PATH_BUSY_WINDOW_SEC], \
        f"等空闲的轮询窗也必须传 2s，实际 {wait_windows}"


def test_cached_standby_skips_probe_when_fresh(monkeypatch):
    """脏线守护刚验证过的出口地址直接用于选线，不再同步探测。

    WHY：换线耗时全部花在探测上（p50 3s），而首错延迟 p50 只有 3s —— 不把
    探测移出关键路径，主动换线永远赶在报错之后。
    """
    egress = _reuse_env(monkeypatch, {})
    now = 1000.0
    egress.update({"1080": "ip-w1", "1081": "ip-b"})
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE",
                        {"W2": {"ip": "ip-b", "ip_ts": now - 2}})
    monkeypatch.setattr(daemon, "_WARP_LAST_ACCEPT",
                        {"W2": (now - 2, "ip-b")})  # Must match _WARP_LINE_STATE for consistency check
    monkeypatch.setattr(daemon, "_port_open", lambda p: True)
    probed = []

    def _probe(members):
        probed.append(members)
        return {m["tag"]: egress[str(m["port"])] for m in members}

    monkeypatch.setattr(daemon, "_probe_warp_egress", _probe)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)

    assert daemon.reconnect_warp("test") is True
    assert switched == ["W2"], "应直接切到缓存命中的备线"
    assert probed == [], "缓存新鲜时不得同步探测"


def test_cached_standby_keeps_all_candidates_not_just_first(monkeypatch):
    """缓存命中时每条可用备线都要进候选池，不能只取第一条。

    旧实现 `m, ip = fast[0]` 把其余备线全丢了：哪条被选中纯看缓存排序，
    其余的一律报「备线 1 条」。隧道从 5 条扩到 10 条后这条代价被放大 ——
    缓存里有 10 条新鲜线，选线却只看到 1 条，候选耗尽就直接落 proxy 池。
    """
    egress = _reuse_env(monkeypatch, {"1080": "ip-w1", "1081": "ip-b", "1082": "ip-c"})
    members = [
        {"tag": "W1", "port": 1080, "service": "s1"},
        {"tag": "W2", "port": 1081, "service": "s2"},
        {"tag": "W3", "port": 1082, "service": "s3"},
    ]
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "members": members})
    now = 1000.0
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {
        "W2": {"ip": "ip-b", "ip_ts": now - 2},
        "W3": {"ip": "ip-c", "ip_ts": now - 2},
    })
    monkeypatch.setattr(daemon, "_WARP_LAST_ACCEPT", {
        "W2": (now - 2, "ip-b"),
        "W3": (now - 2, "ip-c"),
    })
    monkeypatch.setattr(daemon, "_port_open", lambda p: True)
    monkeypatch.setattr(daemon, "_probe_warp_egress",
                        lambda ms: egress)  # 不应被调用
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)

    # 直接盯选线日志里的「备线 N 条」：这是候选池大小，唯一能区分
    # 「只取 fast[0]」（N=1）与「全部收下」（N=2）的观测点。
    lines = []
    monkeypatch.setattr(daemon, "log", lambda msg: lines.append(msg))

    assert daemon.reconnect_warp("test") is True
    assert len(switched) == 1, "一次换线只切一条"
    sel = [l for l in lines if "选线:" in l]
    assert sel and "备线 2 条" in sel[0], f"候选池应有 2 条，实际日志: {sel}"


def test_single_cached_candidate_probes_remainder(monkeypatch):
    """缓存只命中 1 条时必须补探其余备线，不能把候选池压成 1 条。

    `if len(fast) > 1: pass` 那版是空操作；真正需要的是反向分支 —— 命中
    不足 2 条时唯一候选一旦模型层不过就直接落池，其余健康线全被丢掉。
    """
    egress = _reuse_env(monkeypatch, {"1080": "ip-w1", "1081": "ip-b",
                                     "1082": "ip-c"})
    members = [
        {"tag": "W1", "port": 1080, "service": "s1"},
        {"tag": "W2", "port": 1081, "service": "s2"},
        {"tag": "W3", "port": 1082, "service": "s3"},
    ]
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "members": members})
    now = 1000.0
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE",
                        {"W2": {"ip": "ip-b", "ip_ts": now - 2}})
    monkeypatch.setattr(daemon, "_WARP_LAST_ACCEPT",
                        {"W2": (now - 2, "ip-b")})
    monkeypatch.setattr(daemon, "_port_open", lambda p: True)
    probed = []

    def _probe(ms):
        probed.append([m["tag"] for m in ms])
        return {m["tag"]: egress[str(m["port"])] for m in ms}

    monkeypatch.setattr(daemon, "_probe_warp_egress", _probe)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp",
                        lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)
    lines = []
    monkeypatch.setattr(daemon, "log", lambda msg: lines.append(msg))

    assert daemon.reconnect_warp("test") is True
    assert probed == [["W3"]], f"只补探未命中的 W3，实际 {probed}"
    sel = [l for l in lines if "选线:" in l]
    assert sel and "备线 2 条" in sel[0], f"候选池应有 2 条，实际: {sel}"


def test_cached_standby_rejects_stale_blocked_and_closed_port(monkeypatch):
    """缓存过期、出口已被标记、端口不在监听时都不得采信。"""
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "probe_cache_ttl_sec": 12})
    members = [{"tag": "W2", "port": 1081, "service": "s2"},
               {"tag": "W3", "port": 1082, "service": "s3"},
               {"tag": "W4", "port": 1083, "service": "s4"}]
    now = 1000.0
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-blocked": {"ts": now - 100, "blocked": True, "last_ok_ts": now - 100},
    })
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {
        "W2": {"ip": "ip-stale", "ip_ts": now - 60},     # 过期
        "W3": {"ip": "ip-blocked", "ip_ts": now - 1},    # 被标记
        "W4": {"ip": "ip-ok", "ip_ts": now - 1},         # 端口关闭
    })
    monkeypatch.setattr(daemon, "_port_open", lambda p: p != 1083)
    assert daemon._cached_warp_standby(members, now, 86400) == []


def test_warp_cycle_pruning_24h_window():
    """_prune_warp_cycles 只保留 24h 内的样本，丢弃更旧的。"""
    now = 1_000_000.0
    cycles = [
        {"ip": "a", "cycle": 100.0, "ended_at": now - 1000, "ok": 1},    # 24h 内
        {"ip": "b", "cycle": 50.0, "ended_at": now - 50000, "ok": 1},    # 24h 内
        {"ip": "c", "cycle": 30.0, "ended_at": now - 100000, "ok": 1},   # 24h 外
        {"ip": "d", "cycle": 0, "ended_at": now - 1000, "ok": 0},        # cycle=0 也保留
        {"ip": "e", "cycle": -1, "ended_at": now - 1000, "ok": 1},       # 非法 cycle 丢弃
    ]
    kept = daemon._prune_warp_cycles(cycles, now)
    assert len(kept) == 3, f"应保留 3 条，实际 {len(kept)}"
    assert {c["ip"] for c in kept} == {"a", "b", "d"}


def test_error_switch_resets_proactive_timer(monkeypatch):
    """错误触发的换线生效后，主动轮换的计时必须重新开始。

    WHY：两种换线不能交错。旧实现只有 reconnect_warp 成功切 WARP 才更新
    `_last_warp_switch_ok_ts`，`_fallback_to_proxy` 与 `_restart_warp_inplace`
    这两条错误路径都不更新 —— 刚落池或刚原地重连，主动轮换仍按上一段的
    已服务时长算账，立刻再切一次（实测 00:12:00 落池，22s 后就报 27s≥27s）。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG["warp"], "proxy_fallback_enabled", True)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 1000.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", lambda ip, ts, **k: None)
    monkeypatch.setattr(daemon.time, "time", lambda: 5000.0)

    assert daemon._fallback_to_proxy({"tag": "W1"}, "ip-w1", 4000.0) is True
    assert daemon._last_warp_switch_ok_ts == 5000.0, \
        "落池即本段结束，主动轮换计时必须重置为落池时刻"

def test_record_warp_cycle_requires_positive_cycle():
    """cycle <= 0 不记录；ok=0 可以记但重算时排除。"""
    daemon._WARP_IP_CYCLES = []
    daemon._record_warp_cycle("x", -1.0, 1000.0, ok=1)
    assert len(daemon._WARP_IP_CYCLES) == 0
    daemon._record_warp_cycle("x", 0.0, 1000.0, ok=0)
    assert len(daemon._WARP_IP_CYCLES) == 1
    assert daemon._WARP_IP_CYCLES[0]["ok"] == 0


def test_maybe_reconnect_marks_ip_blocked_inside_cooldown(monkeypatch):
    """冷却期内出现的 403 也必须把出口 IP 标脏并采到周期样本。

    403 多在换线后 1 秒内出现（实测首错延迟中位 1s、16/45 段首请求即错），
    此时冷却尚未结束。旧实现把采样放在冷却判断之后，这些段被整体丢弃：
    IP 不标脏，下次换线会把它当「干净地址」再次切上去；周期样本也只剩冷却
    期之后的，最小值恒等于冷却值。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "block_threshold": 1,
                         "rate429_threshold": 5, "members": WARP_TWO})
    monkeypatch.setattr(daemon, "_WARP_IP_CYCLES", [])
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 5000.0)   # 仍在冷却
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 1000.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "fresh-ip")
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 1, "rate429": 0, "ok": 3, "consec_err": 1})
    monkeypatch.setattr(daemon, "_record_warp_cycle",
                        lambda ip, cyc, now, ok=0, blocked=0: daemon._WARP_IP_CYCLES.append(
                            {"ip": ip, "cycle": cyc, "ended_at": now, "ok": ok}))
    # 段内成功数改为整段累计查询；本用例只关心采样是否发生，给个固定值。
    monkeypatch.setattr(daemon, "count_segment_requests", lambda ts: (3, 1))

    class T:
        def __init__(self, target=None, args=(), **k): pass
        def start(self): raise AssertionError("冷却期内不得发起换线")

    monkeypatch.setattr(daemon.threading, "Thread", T)
    daemon.maybe_reconnect_warp(1010.0)
    assert daemon._WARP_IP_USED.get("fresh-ip", {}).get("blocked") is True, \
        "冷却期内被 403 的 IP 必须标脏，否则下次换线会切回它"
    assert len(daemon._WARP_IP_CYCLES) == 1, "冷却期的段也必须采到周期样本"
    assert daemon._WARP_IP_CYCLES[0]["cycle"] == 10.0
    # 同一段每隔一秒都在报错，不得重复采样
    daemon.maybe_reconnect_warp(1011.0)
    assert len(daemon._WARP_IP_CYCLES) == 1, "每段只采一条样本"


def _proactive_mode_env(monkeypatch, mode, **over):
    """固定主动轮换判据的可控环境，两种模式共用。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "proactive_mode": mode,
                         "proactive_fixed_sec": 60, "proactive_after_dsv4_ok": 60,
                         **over})
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 1000.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 1000.0)
    fired = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now, ignore_cooldown=False:
                        fired.append(reason) or True)
    return fired


def test_proactive_mode_fixed_ignores_traffic_and_samples(monkeypatch):
    """fixed 模式按固定秒数换线，不查配额。"""
    fired = _proactive_mode_env(monkeypatch, "fixed")
    monkeypatch.setattr(daemon, "count_dsv4_ok_since",
                        lambda ts: (_ for _ in ()).throw(AssertionError("fixed 不得查配额")))

    daemon._maybe_proactively_rotate_warp(1055.0)   # 55s < 60s - 3s = 57s
    assert fired == [], "未到固定间隔不得换线"
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    daemon._maybe_proactively_rotate_warp(1058.0)   # 58s ≥ 57s
    assert len(fired) == 1 and "固定" in fired[0]


def test_proactive_mode_requests_counts_successful_dsv4_only(monkeypatch):
    """requests 模式只看成功的 dsv4 调用数，不看已用时长。"""
    fired = _proactive_mode_env(monkeypatch, "requests")
    monkeypatch.setattr(daemon, "count_dsv4_ok_since", lambda ts: 59)
    daemon._maybe_proactively_rotate_warp(1100.0)   # 已用 100s，但只用了 59 格
    assert fired == [], "配额未到不得换线"
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "count_dsv4_ok_since", lambda ts: 60)
    daemon._maybe_proactively_rotate_warp(1100.0)
    assert len(fired) == 1 and "60 格 dsv4" in fired[0]


def test_proactive_mode_unknown_falls_back_to_fixed(monkeypatch):
    """配置写错不得让轮换静默停摆：非法值按 fixed（不查库）处理。"""
    fired = _proactive_mode_env(monkeypatch, "nonsense")
    monkeypatch.setattr(daemon, "count_dsv4_ok_since",
                        lambda ts: (_ for _ in ()).throw(
                            AssertionError("非法模式应回退 fixed，不得查库")))
    daemon._maybe_proactively_rotate_warp(1100.0)   # 100s ≥ 60s - 3s
    assert len(fired) == 1 and "固定" in fired[0]


def test_proactive_mode_lifetime_is_gone(monkeypatch):
    """lifetime 已移除：旧配置值静默回退 fixed，轮换不会停摆。

    该模式实测测不到 IP 寿命 —— 段跨度跟的是请求间隔（中位 3.8s，而请求
    本身 p50 6.5s），生效值恒被 60s 地板顶住，等于一个测不准的固定 60s。
    """
    fired = _proactive_mode_env(monkeypatch, "lifetime")
    daemon._maybe_proactively_rotate_warp(1100.0)
    assert len(fired) == 1 and "固定" in fired[0]


def test_proactive_modes_are_mutually_exclusive(monkeypatch):
    """两种判据互斥：选中一个时，另一个的必要条件成立也不得触发。

    fixed 与 requests 只能一个生效。若实现把两个分支叠在一起，固定时长到点
    那次换线会把 requests 模式的配额计数一起清零 —— 阈值 150 永远跑不到。
    """
    # fixed 生效：时钟到点，但配额早就超过 150，仍只能报「固定」
    fired = _proactive_mode_env(monkeypatch, "fixed", proactive_after_dsv4_ok=150)
    monkeypatch.setattr(daemon, "count_dsv4_ok_since",
                        lambda ts: (_ for _ in ()).throw(
                            AssertionError("fixed 生效时不得读配额")))
    daemon._maybe_proactively_rotate_warp(2000.0)   # 距生效 1000s
    assert len(fired) == 1 and "固定" in fired[0], f"fixed 只能按时间切，实际 {fired}"

    # requests 生效：时钟远超 60s，但配额未满 → 不得换线
    fired = _proactive_mode_env(monkeypatch, "requests", proactive_after_dsv4_ok=150)
    monkeypatch.setattr(daemon, "count_dsv4_ok_since", lambda ts: 149)
    daemon._maybe_proactively_rotate_warp(3000.0)   # 距生效 2000s，远超 60s
    assert fired == [], f"requests 生效时不得因时长换线，实际 {fired}"
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "count_dsv4_ok_since", lambda ts: 150)
    daemon._maybe_proactively_rotate_warp(3000.0)
    assert len(fired) == 1 and "150" in fired[0], f"满 150 格必须换线，实际 {fired}"


def test_proactive_rotate_keeps_fail_cooldown(monkeypatch):
    """上次换线失败（reconnect 晚于 switch_ok）时，主动轮换也必须守失败冷却。

    cooldown_fail_sec 是防「连切连败」把隧道重启成雷暴的。绕过条件只认
    「上一次换线成功」，失败后仍按 5s 一轮重试等于把隧道刷成雷暴。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 1000.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 1030.0)  # 之后失败
    # 本用例测的是冷却，不是配额：桩掉计数，免得真查生产库、也免得配额不足
    # 时压根走不到换线分支。
    monkeypatch.setattr(daemon, "count_dsv4_ok_since", lambda ts: 150)
    seen = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now, ignore_cooldown=False:
                        seen.append(ignore_cooldown) or True)

    daemon._maybe_proactively_rotate_warp(1100.0)
    assert seen == [False], "换线失败过就不得绕失败冷却"



def test_ignore_cooldown_still_requires_warp_lock(monkeypatch):
    """ignore_cooldown=True 时仍需抢 _WARP_LOCK，防并发换线。"""
    _warp_env(monkeypatch, WARP_TWO)
    # 用独立锁：前面的用例给 threading.Thread 打桩后 _WARP_LOCK 可能仍被持有，
    # 直接 acquire 会挂死（不是本用例要测的行为）。
    monkeypatch.setattr(daemon, "_WARP_LOCK", threading.Lock())
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0.0)
    daemon._WARP_LOCK.acquire()
    try:
        ok = daemon._reconnect_warp_now("test", 1000.0, ignore_cooldown=True)
        assert ok is False, "持锁时 ignore_cooldown 也不得发起换线"
    finally:
        daemon._WARP_LOCK.release()


def test_count_dsv4_ok_since_counts_only_successful_quota_model(monkeypatch):
    """只数「该 provider + 该模型 + status=ok」且时间严格大于起点的行。

    这个数就是配额已消耗的格数；多算（别的模型/provider、失败行）会让主动
    轮换提前切走健康线，少算则等到 429 才切。
    """
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE usageHistory "
               "(timestamp TEXT, provider TEXT, model TEXT, status TEXT)")
    db.execute("CREATE INDEX idx_uh_ts ON usageHistory(timestamp)")
    # 列里的真实格式是 '...SS.mmmZ'（生产表 226211/226211 全带毫秒）。
    # 起点 1760000000.750s = 2025-10-09T08:53:20.750Z，同一秒内 .749 在前、
    # .751 在后 —— 前者的调用发生在换线之前，绝不能算进新段的配额。
    db.executemany("INSERT INTO usageHistory VALUES (?,?,?,?)", [
        ("2025-10-09T08:53:20.000Z", "opencode", "deepseek-v4.1-flash-free", "ok"),  # 同秒，早于起点
        ("2025-10-09T08:53:20.749Z", "opencode", "deepseek-v4.1-flash-free", "ok"),  # 差 1ms，早于起点
        ("2025-10-09T08:53:20.750Z", "opencode", "deepseek-v4.1-flash-free", "ok"),  # 恰在边界，严格大于不计
        ("2025-10-09T08:53:20.751Z", "opencode", "deepseek-v4.1-flash-free", "ok"),  # 起点之后
        ("2025-10-09T08:53:21.000Z", "opencode", "deepseek-v4.1-flash-free", "ok"),
        ("2025-10-09T09:00:00.000Z", "opencode", "deepseek-v4.1-flash-free", "error"),
        ("2025-10-09T09:00:00.000Z", "opencode", "mimo-v2.5-free", "ok"),
        ("2025-10-09T09:00:00.000Z", "nvidia", "deepseek-v4.1-flash-free", "ok"),
    ])
    monkeypatch.setattr(daemon, "_get_db", lambda: db)
    monkeypatch.setitem(daemon.CFG["warp"], "proactive_quota_model",
                        "deepseek-v4.1-flash-free")
    assert daemon.count_dsv4_ok_since(1760000000.750) == 2


def test_count_dsv4_ok_since_skips_db_for_nonpositive_ts(monkeypatch):
    """起点非正时直接返回 0，不查库：0 表示「本进程还没换过线」。"""
    monkeypatch.setattr(daemon, "_get_db",
                        lambda: pytest.fail("ts<=0 不得查库"))
    assert daemon.count_dsv4_ok_since(0.0) == 0
    assert daemon.count_dsv4_ok_since(-5.0) == 0


def test_count_dsv4_ok_since_returns_zero_and_logs_on_error(monkeypatch):
    """查库失败返回 0 并留日志：静默返回 0 会让轮换永不触发且无从察觉。"""
    def boom():
        raise sqlite3.OperationalError("no such index: idx_uh_ts")

    monkeypatch.setattr(daemon, "_get_db", boom)
    logged = []
    monkeypatch.setattr(daemon, "log", lambda m: logged.append(m))
    assert daemon.count_dsv4_ok_since(1760000000.0) == 0
    assert any("dsv4" in m for m in logged), f"失败未记日志: {logged}"


def test_count_dsv4_ok_since_does_not_swallow_config_errors(monkeypatch):
    """配置缺键必须炸出来，不能当成「0 格」。

    requests 模式的整个判据就是这个数。键写错时若被 except 吞成 0，换线
    永不触发，而日志只有一行「统计失败」，看起来和查库失败一样正常。
    """
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE usageHistory "
               "(timestamp TEXT, provider TEXT, model TEXT, status TEXT)")
    db.execute("CREATE INDEX idx_uh_ts ON usageHistory(timestamp)")
    monkeypatch.setattr(daemon, "_get_db", lambda: db)
    warp = {k: v for k, v in daemon.CFG["warp"].items() if k != "proactive_quota_model"}
    monkeypatch.setitem(daemon.CFG, "warp", warp)
    with pytest.raises(KeyError):
        daemon.count_dsv4_ok_since(1760000000.0)


def test_prewarm_gate_uses_24h_window(monkeypatch):
    """预热门控看最近 24h 而非 10 分钟：只要一天内有流量就预热。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "prewarm_sec": 120})
    monkeypatch.setattr(daemon, "_last_warp_prewarm_ts", 0.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {"W2": {"sel": 1.0, "ip": "ip-w2"}})
    seen = []
    monkeypatch.setattr(daemon, "count_opencode_since",
                        lambda ts: seen.append(ts) or 0)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh",
                        lambda m: pytest.fail("无流量不得预热"))
    now = 1_000_000.0
    daemon._prewarm_standby_warp(now)
    assert seen == [now - 86400], f"门控窗口应为 24h，实际传 ts={seen}"
    assert now - seen[0] == 86400.0


def test_persistence_write_is_atomic_and_logs_failure(monkeypatch, tmp_path):
    """两份落盘记录必须原子写：失败时不得损坏原文件，且必须留日志。

    它们是进程重启后唯一能恢复「哪些地址已被烧」的记录。直接 open(w) 会先把
    原文件清零，此刻崩溃就留下半截 JSON，下次启动解析失败 → 记录全丢 → 窗口内
    的脏地址被当干净地址再切回去（实测 22:22 重启正是这么丢了记录，22:27 切回
    已烧地址，403 持续 144s）。静默 `except: pass` 则让这种丢失无从察觉。
    """
    p = tmp_path / "rec.json"
    daemon._write_json_atomic(str(p), {"used": {"ip-x": {"ts": 1.0}}})
    assert json.loads(p.read_text(encoding="utf-8")) == {"used": {"ip-x": {"ts": 1.0}}}
    assert not (tmp_path / "rec.json.tmp").exists(), "残留 .tmp"

    p.write_text('{"used": {"keep": true}}', encoding="utf-8")   # 已有记录
    tmp_path.chmod(0o500)                                        # 使其无法建 .tmp
    logged = []
    monkeypatch.setattr(daemon, "log", lambda msg: logged.append(msg))
    try:
        daemon._write_json_atomic(str(p), {"used": {"new": True}})
    finally:
        tmp_path.chmod(0o700)
    assert json.loads(p.read_text(encoding="utf-8")) == {"used": {"keep": True}}, \
        "写入失败时覆盖了原文件"
    assert any("写 rec.json 失败" in m for m in logged), f"失败未记日志: {logged}"


def test_proactive_rotate_recovers_segment_start_after_restart(monkeypatch):
    """service 重启后段起点必须从 IP 使用记录恢复，不能从零重算。

    重启会让 `_last_warp_switch_ok_ts` / `_last_warp_reconnect_ts` 归零。
    旧实现拿 0 当窗口起点，`count_opencode_since(0)` 会把重启**之前**已经
    服务过的那一段整段算进来 —— 实测 16:31:08 重启、16:32:18（70 秒后）
    就报「已服务 63 条」并换线，而那条线实际只服务了几条。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"], "proactive_mode": "requests",
        "proactive_after_dsv4_ok": 60, "cooldown_ok_sec": 20})
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0.0)
    # 重启后状态：两个内存时间戳都是 0
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 0.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    # 段起点从 `_ACTIVE_WARP_IP` 查 IP 表恢复，不走 SOCKS 现探（主循环不得阻塞）。
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "ip-w1")
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: pytest.fail("主循环不得同步探测出口 IP"))
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {"ip-w1": {"ts": 1000.0, "blocked": False}})
    seen = []
    monkeypatch.setattr(daemon, "count_dsv4_ok_since",
                        lambda ts: seen.append(ts) or 5)
    fired = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now, ignore_cooldown=False:
                        fired.append(reason) or True)

    daemon._maybe_proactively_rotate_warp(1100.0)
    assert seen == [1000.0], f"段起点必须取自 IP 记录（1000），实际 {seen}"
    assert not fired, "只用了 5 格，不该换线"


def test_guard_window_depends_on_trigger_reason():
    """403 用 6h 窗口、429 用 60s 默认窗口，其它触发用默认窗口。"""
    w = daemon._warp()
    assert daemon._warp_guard_for_reason("abuse403 ×1") == w["blocked_guard_sec"]
    assert daemon._warp_guard_for_reason("rate429 ×3") == w["rate429_guard_sec"]
    assert daemon._warp_guard_for_reason(
        "主动轮换 已用配额 150 格 dsv4 ≥ 150") == w["ip_reuse_guard_sec"]
    # 两者同时命中时按 403 处理（403 恢复期长于 429 默认，信誉标记更严格、优先）
    assert daemon._warp_guard_for_reason("abuse403 ×1 + rate429 ×2") == w["blocked_guard_sec"]
    # 429 的默认冷却短于 403 恢复期：长封禁靠解析写入的 ban_until 承载
    assert w["blocked_guard_sec"] > w["rate429_guard_sec"], "403 恢复期应长于 429 默认冷却"
    assert w["blocked_guard_sec"] == 6 * 3600, "403 恢复期应为 6h"
    assert w["rate429_guard_sec"] == 60, "429 默认冷却应为 60s"


def test_blocked_ip_recovers_after_6h_for_403_and_60s_for_429(monkeypatch):
    """惩罚期按 blocked_kind 分别计时：403 六小时、429 默认六十秒。

    403 是出口 IP 信誉标记，休息满 6h 即可再试；429 无解析值时按 60s 默认
    回池（长恢复由 ban_until 精确值承载）。kind 缺失的旧记录按 24h 保守处理。
    """
    now = 1_000_000.0
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_append_warp_ip_history",
                        lambda *a, **k: None)
    daemon._mark_warp_ip_used("ip-403", now, blocked=True, kind="abuse403")
    daemon._mark_warp_ip_used("ip-429", now, blocked=True, kind="rate429")
    daemon._mark_warp_ip_used("ip-legacy", now, blocked=True)   # 无 kind 的旧记录
    assert daemon._WARP_IP_USED["ip-403"]["blocked_kind"] == "abuse403"
    assert daemon._WARP_IP_USED["ip-429"]["blocked_kind"] == "rate429"

    h = 3600
    # 403：6h 内仍挡，6h 后放行
    assert daemon._warp_ip_is_blocked("ip-403", now + 5 * h + 59 * 60) is True
    assert daemon._warp_ip_is_blocked("ip-403", now + 6 * h + 60) is False
    # 429：默认 60s 内仍挡、61s 后放行
    assert daemon._warp_ip_is_blocked("ip-429", now + 30) is True
    assert daemon._warp_ip_is_blocked("ip-429", now + 61) is False
    # 旧记录没有 kind → 按 24h 保守处理
    assert daemon._warp_ip_is_blocked("ip-legacy", now + 6 * h + 60) is True
    assert daemon._warp_ip_is_blocked("ip-legacy", now + 24 * h + 60) is False
    # 未标记 / 空 IP 一律 False
    daemon._mark_warp_ip_used("ip-clean", now)
    assert daemon._warp_ip_is_blocked("ip-clean", now) is False
    assert daemon._warp_ip_is_blocked(None) is False


def test_403_then_429_blocks_until_later_expiry(monkeypatch):
    """先 403 后 429 的 IP 按晚的那条解禁，两条时钟互不覆盖。

    单 (kind, start) 会让后一次 429 把 403 的起点一并清掉；按 kind 分账后
    解禁时刻取 max。新默认下 429 的 60s 远短于 403 的 6h —— 较晚的一条恰是
    403；解析出长 Recover-After 时由 ban_until 拉长 429 那条（见精确封禁用例）。
    """
    now = 1_000_000.0
    h = 3600
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(daemon, "_append_warp_ip_history", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    daemon._mark_warp_ip_used("ip-y", now, blocked=True, kind="abuse403")
    daemon._mark_warp_ip_used("ip-y", now + 2 * h, blocked=True, kind="rate429")
    entry = daemon._WARP_IP_USED["ip-y"]
    assert entry["blocked_ts_by_kind"] == {"abuse403": now, "rate429": now + 2 * h}
    # 429 默认早已在 T+2h+60s 到期，但 403 要到 T+6h
    assert daemon._warp_ip_is_blocked("ip-y", now + 4 * h) is True, \
        "429 虽已满 60s，403 的 6h 仍未到期，须继续挡住"
    assert daemon._warp_ip_is_blocked("ip-y", now + 6 * h + 60) is False
    # 从没成功过的地址 recently_used 恒 False（它只看成功流量），由 is_blocked 兜住
    assert daemon._warp_ip_recently_used("ip-y", now + 4 * h) is False
    # 成功服务过再被两类烧掉：窗口取最晚的 403 6h，5h 后仍算「刚用过」
    entry["last_ok_ts"] = now
    assert daemon._warp_ip_recently_used("ip-y", now + 5 * h) is True, \
        "有 403 标记时窗口须取 6h，不能按 429 的 60s 提前放行"
    assert daemon._warp_ip_recently_used("ip-y", now + 6 * h + 60) is False


def test_reburned_after_recovery_starts_new_penalty(monkeypatch):
    """惩罚期过后再被烧 → 新一轮计时，不得沿用旧 blocked_ts 判成「已恢复」。

    否则一个恢复过一次的地址以后再被 429 烧掉会立刻被放行，默认冷却形同虚设。
    同一轮内的重复标记仍保留最早的 kind 与 blocked_ts（见上一个用例）。
    """
    now = 1_000_000.0
    h = 3600
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(daemon, "_append_warp_ip_history", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    daemon._mark_warp_ip_used("ip-x", now, blocked=True, kind="abuse403")
    assert daemon._warp_ip_is_blocked("ip-x", now + 6 * h + 60) is False, "6h 后应已恢复"

    daemon._mark_warp_ip_used("ip-x", now + 7 * h, blocked=True, kind="rate429")
    entry = daemon._WARP_IP_USED["ip-x"]
    assert entry["blocked_kind"] == "rate429", "新一轮按本次错误类型计时"
    assert entry["blocked_ts"] == now + 7 * h, "新一轮起点为本次标记时刻"
    assert daemon._warp_ip_is_blocked("ip-x", now + 7 * h + 30) is True, "应在新的 60s 冷却内"
    assert daemon._warp_ip_is_blocked("ip-x", now + 8 * h) is False, "默认 60s 到期即回池"


def test_retry_after_sets_exact_ban_until(monkeypatch):
    """上游 429 的 Retry-After 是确切封禁期，替换 rate429 的默认 60s 猜测。

    探针在 429 响应头里拿到 retry-after 后写入 ban_until；到期即放行，
    既不会因默认多封数小时，也不会提前放回。
    """
    now = 1_000_000.0
    h = 3600
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(daemon, "_append_warp_ip_history", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    daemon._mark_warp_ip_used("ip-ban", now, blocked=True, kind="rate429",
                              ban_after=2 * h)
    assert daemon._WARP_IP_USED["ip-ban"]["ban_until"] == now + 2 * h
    assert daemon._WARP_IP_USED["ip-ban"]["ban_kind"] == "rate429"
    assert daemon._warp_ip_is_blocked("ip-ban", now + h) is True
    assert daemon._warp_ip_is_blocked("ip-ban", now + 2 * h - 1) is True
    assert daemon._warp_ip_is_blocked("ip-ban", now + 2 * h + 1) is False


def test_ban_until_does_not_shorten_parallel_403(monkeypatch):
    """Retry-After 只替换同类型窗口；并行 403 的 6h 仍按「按多的来」。"""
    now = 1_000_000.0
    h = 3600
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(daemon, "_append_warp_ip_history", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    daemon._mark_warp_ip_used("ip-mix", now, blocked=True, kind="abuse403")
    daemon._mark_warp_ip_used("ip-mix", now + h, blocked=True, kind="rate429",
                              ban_after=2 * h)
    assert daemon._warp_ip_is_blocked("ip-mix", now + 6 * h - 60) is True
    assert daemon._warp_ip_is_blocked("ip-mix", now + 6 * h + 60) is False


def test_expired_block_becomes_secondary_tier(monkeypatch):
    """过了惩罚期的 IP 降级为「次级优质」，重新进入候选；未过期的仍被排除。

    403 标记、7 小时前成功过 → 惩罚期已过，档位 1（次级优质），
    _warp_ip_recently_used 不再按 blocked 窗口排除它。
    同样条件下 429 标记仍在 24h 惩罚期内 → 被排除。
    """
    now = 1_000_000.0
    h = 3600
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"], "blocked_guard_sec": 6 * h,
        "rate429_guard_sec": 24 * h, "ip_reuse_guard_sec": 24 * h})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-403": {"ts": now - 7 * h, "blocked": True, "blocked_kind": "abuse403",
                   "blocked_ts": now - 8 * h, "last_ok_ts": now - 7 * h},
        "ip-429": {"ts": now - 7 * h, "blocked": True, "blocked_kind": "rate429",
                   "blocked_ts": now - 7 * h, "last_ok_ts": now - 7 * h},
    })
    # 403：惩罚期已过 → 档 1 次级优质，且不算「窗口内用过」
    assert daemon._warp_ip_is_blocked("ip-403", now) is False
    assert daemon._warp_ip_rank("ip-403", now, 24 * h)[0] == 1, "应为次级优质（档 1）"
    assert daemon._warp_ip_recently_used("ip-403", now) is False, "已过 6h 窗口，应可复用"
    # 429：24h 惩罚期内 → 仍被排除
    assert daemon._warp_ip_is_blocked("ip-429", now) is True
    assert daemon._warp_ip_recently_used("ip-429", now) is True, "仍在 24h 窗口内"
    # 未被标记、但 7h 前成功过：沿用调用方的默认窗口（24h）→ 仍算最近用过
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {"plain": {"ts": now - 7 * h, "blocked": False,
                                   "last_ok_ts": now - 7 * h}})
    assert daemon._warp_ip_recently_used("plain", now) is True


def test_warp_ip_never_used_distinguishes_from_old_records(monkeypatch):
    """从未用过的地址必须能与「用过但记录已清理」区分开。

    两者在 _warp_ip_last_ts 上都返回 0，只按时间排序会把清掉的旧地址当成
    新地址优先选。never_used 直接查记录是否存在。
    """
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {"old": {"ts": 100.0, "blocked": False}})
    assert daemon._warp_ip_never_used("brand-new") is True
    assert daemon._warp_ip_never_used("old") is False
    assert daemon._warp_ip_never_used(None) is False
    # 两者 last_ts 都是 0 / 100，但 never_used 语义不同
    assert daemon._warp_ip_last_ts("brand-new") == 0.0


def test_reconnect_prefers_never_used_over_recently_used(monkeypatch):
    """选线时未用过的地址优先于「窗口外但用过」的地址。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"], "blocked_guard_sec": 21600,
        "rate429_guard_sec": 86400, "ip_reuse_guard_sec": 86400})
    now = 100000.0
    # W1: 用过且在 24h 窗口内；W2: 从没用过
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {"ip-old": {"ts": now - 600, "blocked": False}})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)   # 在 proxy 上
    monkeypatch.setattr(daemon, "_probe_warp_egress",
                        lambda members: {"W1": "ip-old", "W2": "ip-new"})
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: {"1080": "ip-old", "1081": "ip-new"}.get(str(port)))
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    picked = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: picked.append(tag) or True)
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", lambda ip, ts, blocked=False: None)
    monkeypatch.setattr(daemon, "_warp_line_selected", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda *a: None)
    monkeypatch.setattr(daemon, "_refresh_idle_warp", lambda m, **kw: "ip-new")

    daemon.reconnect_warp("abuse403 ×1")
    assert picked == ["W2"], f"应选未用过的 W2，实际 {picked}"


def test_recently_used_is_based_on_last_success_not_selection(monkeypatch):
    """「3 小时内未使用」按最近一次**成功流量**判定，不按被选中的时刻。

    一条被切上去就 403 的线：`ts`（被选时刻）很新，但 `last_ok_ts` 为空 ——
    它从没服务成功过，不该被当成「刚用过的干净地址」而排除，也不该被当成
    「刚刚还成功」而信任。区分这两个时刻是分级选线的前提。
    """
    now = 100000.0
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "ip_reuse_guard_sec": 10800})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        # 刚被选中，但从未成功过
        "never-ok": {"ts": now - 60, "blocked": True, "last_ok_ts": None},
        # 很久前被选中，但刚刚还在成功服务
        "still-ok": {"ts": now - 5000, "blocked": False, "last_ok_ts": now - 30},
        # 3 小时前成功过，早已过期
        "stale": {"ts": now - 50000, "blocked": False, "last_ok_ts": now - 20000},
    })
    assert daemon._warp_ip_recently_used("never-ok", now, 10800) is False, \
        "从未成功过的地址不适用「最近有成功流量」判据"
    assert daemon._warp_ip_recently_used("still-ok", now, 10800) is True, \
        "正在成功服务的地址应算「最近有流量」"
    assert daemon._warp_ip_recently_used("stale", now, 10800) is False, \
        "5.5 小时前成功过，已超出 3h 窗口"


def test_record_active_ip_success_uses_window_delta(monkeypatch):
    """成功流量按窗口内成功数增量判定，不能被滑动窗口里的旧成功反复触发。"""
    now = 100000.0
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "ip-a")
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {"ip-a": {"ts": now - 100, "blocked": False, "last_ok_ts": None}})
    monkeypatch.setattr(daemon, "_last_success_probe", None)
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(daemon.time, "time", lambda: now)
    # 成功时刻取自请求自己的 timestamp（r 新→旧），不是读库时刻
    monkeypatch.setattr(daemon, "recent_success_ts", lambda n: [now - 4.0, now - 9.0][:n])

    daemon._record_active_ip_success(10, 4)      # 基线：ok=6
    assert daemon._WARP_IP_USED["ip-a"]["last_ok_ts"] is None, "首轮只建基线"
    daemon._record_active_ip_success(10, 4)      # ok 未变 → 无新增成功
    assert daemon._WARP_IP_USED["ip-a"]["last_ok_ts"] is None, \
        "窗口里那条旧成功不能反复算作新成功"
    daemon._record_active_ip_success(10, 3)      # ok 6→7
    assert daemon._WARP_IP_USED["ip-a"]["last_ok_ts"] == now - 4.0, \
        "新增成功应把 last_ok_ts 推进到请求自己的时间戳"
    assert daemon._WARP_IP_USED["ip-a"]["first_ok_ts"] == now - 4.0, "首个成功即段起点"

    # 换线到新出口：窗口里的成功大多来自旧出口，不能算到新 IP 头上
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "ip-b")
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {**daemon._WARP_IP_USED,
                         "ip-b": {"ts": now - 10, "blocked": False, "last_ok_ts": None}})
    daemon._record_active_ip_success(10, 2)      # ok 8，但属于旧出口
    assert daemon._WARP_IP_USED["ip-b"]["last_ok_ts"] is None, \
        "换线后第一轮只重建基线，不能凭旧出口的成功给新 IP 记一笔"


def test_record_active_ip_success_uses_request_timestamps(monkeypatch):
    """跨度用请求自己的 timestamp，不能用读库时刻。

    9router 每 5s 批量落库（或攒满 20 条），一批成功在同一个 tick 才可见。
    旧实现拿 `time.time()` 记 last_ok_ts：一个活 7s 的出口整段只被打点 1~2 次，
    first_ok_ts == last_ok_ts，span 恒为 0 —— 实测 89 条样本里 80 条如此，
    中位被顶成 0，寿命判据退化成固定下限 30s。用请求时间戳复算中位 6.9s。
    """
    now = 100000.0
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "ip-a")
    # 条目必须带 ts：first_ok_ts 比 ts 还旧说明是上一个 stint 留下的（同址
    # 出窗口后被再次选中），会触发重置 —— 这里 ts 早于成功时刻，属同一段。
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {"ip-a": {"ts": now - 100, "blocked": False}})
    monkeypatch.setattr(daemon, "_last_success_probe", ("ip-a", 6))
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(daemon.time, "time", lambda: now)
    # 一批落库的 3 条成功，请求时刻相隔 4s —— 同一 tick 被看到，但时刻不同
    monkeypatch.setattr(daemon, "recent_success_ts", lambda n: [now - 2.0, now - 4.0, now - 6.0])

    daemon._record_active_ip_success(10, 1)   # ok 6→9，新增 3 条

    e = daemon._WARP_IP_USED["ip-a"]
    assert e["first_ok_ts"] == now - 6.0, "段起点应是这批里最早那条请求的时刻"
    assert e["last_ok_ts"] == now - 2.0, "段终点应是这批里最晚那条请求的时刻"
    assert e["last_ok_ts"] - e["first_ok_ts"] == 4.0, "同批落库也要能算出真实跨度"


def test_warp_ip_rank_orders_unseen_before_ok_before_no_success(monkeypatch):
    """选线四档：从没用过 > 撑过生产 > 探针验过 > 一次 OK 都没拿到。

    第 1 档必须排在第 2 档前面：有生产成功记录的地址已验证能服务，优于「切上去
    就 403、从没服务成功过」的地址。第 2 档（探针验过、没撑过生产）排第 3 档之
    前：它此刻确实能服务，只是还没轮到上场，比「验过且失败」强 —— 而「从未出现
    过」仍排最前，因为它至少可能是个新鲜好地址。
    """
    now = 100000.0
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "burned-no-ok": {"ts": now - 30, "blocked": False,
                         "last_ok_ts": None, "probe_ok_ts": None},
        "probe-only-old": {"ts": now - 6000, "blocked": False,
                           "last_ok_ts": None, "probe_ok_ts": now - 6000},
        "probe-only-new": {"ts": now - 60, "blocked": False,
                           "last_ok_ts": None, "probe_ok_ts": now - 60},
        "old-ok":      {"ts": now - 5000, "blocked": False,
                        "last_ok_ts": now - 5000, "probe_ok_ts": None},
        "recent-ok":   {"ts": now - 100, "blocked": False,
                        "last_ok_ts": now - 100, "probe_ok_ts": None},
    })

    ranks = {ip: daemon._warp_ip_rank(ip, now, 86400) for ip in (
        "brand-new", "burned-no-ok", "probe-only-old", "probe-only-new",
        "old-ok", "recent-ok")}
    assert ranks["brand-new"][0] == 0, "没见过的地址应第 0 档"
    assert ranks["old-ok"][0] == 1 and ranks["recent-ok"][0] == 1
    assert (ranks["probe-only-old"][0] == 2
            and ranks["probe-only-new"][0] == 2), "探针验过但没服务过应第 2 档"
    assert ranks["burned-no-ok"][0] == 3, "一次 OK 都没拿到的地址应第 3 档"
    # 第 1 档内部：越久没成功越优先（排序值越小，因为调用方用 min()）
    assert ranks["old-ok"][2] < ranks["recent-ok"][2]
    # 整体排序：brand-new < old-ok < recent-ok < 验过未用 < burned-no-ok
    ordered = sorted(ranks, key=lambda ip: ranks[ip])
    assert ordered == ["brand-new", "old-ok", "recent-ok",
                       "probe-only-old", "probe-only-new",
                       "burned-no-ok"], ordered


def test_warp_ip_rank_prefers_unseen_ip_from_rested_prefix(monkeypatch):
    """同档内避开刚烧过的 /64：新 /128 落在热段里首条请求照样 429。

    9router 近 2h 失败流量按段静置分组：<5min 有成功比例 73%（n=41），
    5-15min 100%（n=21）。档位仍是主判据，段静置只排同档内先后。
    """
    now = 100000.0
    hot_used = "2a09:bac5:1f05:1250::1"
    hot_new = "2a09:bac5:1f05:1250::2"
    cool_used = "2a09:bac5:1eff:1846::1"
    cool_new = "2a09:bac5:1eff:1846::2"
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        hot_used: {"ts": now - 60},
        cool_used: {"ts": now - 3600},
    })
    monkeypatch.setitem(daemon.CFG["warp"], "prefix_rest_sec", 300)
    hot_rank = daemon._warp_ip_rank(hot_new, now, 86400)
    cool_rank = daemon._warp_ip_rank(cool_new, now, 86400)
    assert hot_rank[0] == cool_rank[0] == 0, "两者都是没见过的新地址"
    assert hot_rank > cool_rank, "热段里的新地址应排在静置段之后"


def test_rd_watermark_tracks_max_rowid_not_timestamp(monkeypatch):
    """水位线用 max(rowid)：timestamp 由 9router 客户端写入，与插入序不同步。

    实测 439/2000 行 timestamp 与 rowid 反序。若拿 timestamp 当水位线，
    一条迟到写入的旧时间戳行会被判成「没有新数据」，它带来的 403 就被整段
    跳过。rowid 单调，且 9router 的清理是 `DELETE ... ORDER BY timestamp ASC`
    （删最旧），max(rowid) 不会因此回落。
    """
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE requestDetails (provider TEXT, status TEXT, timestamp TEXT, data TEXT)")
    mem.executemany("INSERT INTO requestDetails VALUES (?,?,?,?)", [
        ("opencode", "success", "2026-10-19T07:00:03.000Z", "{}"),
        ("opencode", "error",   "2026-10-19T07:00:01.000Z", "{}"),   # 迟到的旧时间戳
    ])
    monkeypatch.setattr(daemon, "_get_db", lambda: mem)

    assert daemon._rd_watermark() == 2, "水位线应取 max(rowid)，与 timestamp 顺序无关"
    # 清理最旧两行不影响水位线：删的是低 rowid 端
    mem.execute("DELETE FROM requestDetails WHERE rowid = 1")
    assert daemon._rd_watermark() == 2, "删旧行不得让水位线回落（否则会重判已处理的数据）"
    mem.execute("INSERT INTO requestDetails VALUES (?,?,?,?)",
                ("opencode", "error", "2026-10-19T07:00:09.000Z", "{}"))
    assert daemon._rd_watermark() == 3, "新行必须抬高水位线"


def test_submit_once_skips_reentry_until_task_finishes(monkeypatch):
    """同名后台任务不许重入：上一轮没回来时再发，只会排出一串读同一份过时现场的工作。

    主循环 20Hz 调用 `_submit_once`，而池探测最长 15s —— 没有这个闸门，
    一次探测期间会堆出 300 个线程。
    """
    release = threading.Event()
    started = []
    done = threading.Event()

    def slow():
        started.append(1)
        release.wait(2)
        done.set()

    try:
        assert daemon._submit_once("t-reentry", slow) is True
        assert daemon._submit_once("t-reentry", slow) is False, "运行中的同名任务应被跳过"
        release.set()
        assert done.wait(2)
        # 上一轮结束后必须能再次启动，否则这个 key 被永久锁死
        release.set()
        for _ in range(50):
            if daemon._submit_once("t-reentry", lambda: None):
                break
            time.sleep(0.01)
        else:
            pytest.fail("任务结束后同名 key 未释放")
    finally:
        release.set()
    assert len(started) == 1, f"重入被挡时只应执行一次，实际 {len(started)} 次"


def test_publish_clash_snapshot_keeps_selectors_apart(monkeypatch):
    """`proxy`（轮换池）与 `opencode-out`（WARP 出口）是两个 selector，快照不得混用。

    混用一个槽会让池轮换的 `now`（某个节点名）被 `active_warp_tag()` 读到，
    判定成「opencode 不在 WARP 上」，WARP 换线路径整条失效。
    """
    monkeypatch.setattr(daemon, "_CLASH_SNAP", {})
    monkeypatch.setitem(daemon.CFG, "singbox", {**daemon.CFG["singbox"], "selector": "proxy"})
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "selector": "opencode-out"})

    daemon._publish_clash_snapshot("proxy", {"all": ["n1", "n2"], "now": "n1"})
    daemon._publish_clash_snapshot("opencode-out", {"all": ["WARP-CF", "proxy"], "now": "WARP-CF"})

    assert daemon._pool_snapshot() == (["n1", "n2"], "n1")
    # 换线只写 now 时不得把 all 清空（刷新线程和换线线程谁先到都可能）
    daemon._publish_clash_snapshot("opencode-out", {"now": "WARP-CF-B"})
    snap = daemon._clash_snapshot("opencode-out")
    assert snap["now"] == "WARP-CF-B"
    assert snap["all"] == ["WARP-CF", "proxy"], "只写 now 不应清掉 all"
    assert daemon.active_warp_tag() == "WARP-CF-B"


# ============= 24. 出口 IP 历史池（只追加、永不清理） =============
def test_ip_history_counts_reconnects_and_last_ts(monkeypatch, tmp_path):
    """同一 IP 多次被选为出口 → uses = 重连次数，last_ts 取最大值。

    last_ts 不能取「最后写入的那行」：并发换线线程的写入顺序不保证与时间
    顺序一致，按读取顺序覆盖会让「上次连接时间」倒退。
    """
    hist = tmp_path / "history.jsonl"
    monkeypatch.setenv("WARP_IP_HISTORY_FILE_PATH", str(hist))
    for t, b in ((1000.0, 0), (1200.0, 1), (1100.0, 0)):
        daemon._append_warp_ip_history("ip-a", t, bool(b))
    daemon._append_warp_ip_history("ip-b", 900.0, False)
    # kind 可选：带标记时写 k，未带标记/旧读取方的行不受影响
    daemon._append_warp_ip_history("ip-a", 1300.0, True, "rate429")
    daemon._append_warp_ip_history("ip-c", 1300.0, True, "abuse403")
    assert json.loads(hist.read_text(encoding="utf-8").splitlines()[-1])["k"] == "403"
    assert json.loads(hist.read_text(encoding="utf-8").splitlines()[-2])["k"] == "429"
    with open(hist, "a", encoding="utf-8") as f:      # 坏行不得毁掉整体统计
        f.write("{not json\n")

    stats = daemon._warp_ip_history_stats(str(hist))
    assert stats["ip-a"]["uses"] == 4, "uses 即重连次数"
    assert stats["ip-a"]["last_ts"] == 1300.0, "last_ts 取最大而非最后一次写入"
    assert stats["ip-a"]["last_blocked_ts"] == 1300.0
    assert stats["ip-b"] == {"uses": 1, "last_ts": 900.0, "last_blocked_ts": None}
    assert daemon._warp_ip_history_stats(str(tmp_path / "missing.jsonl")) == {}, \
        "文件不存在返回空，不抛异常"


def test_ip_history_survives_hot_table_cleanup(monkeypatch, tmp_path):
    """热表按窗口清理后，历史记录必须仍在 —— 锁住「永不刷新」这条语义。

    热表是选线用的滑动窗口（必须清理，否则每次换线全量重写整年数据）；
    历史是只追加日志，两者生命周期不同，不能因为热表清理而丢历史。
    """
    hist = tmp_path / "history.jsonl"
    monkeypatch.setenv("WARP_IP_HISTORY_FILE_PATH", str(hist))
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "ip_reuse_guard_sec": 300,
                         "blocked_guard_sec": 300, "rate429_guard_sec": 300,
                         "blocked_ttl_sec": 300})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})

    daemon._mark_warp_ip_used("old-ip", 1000.0)
    daemon._mark_warp_ip_used("new-ip", 2000.0)       # old-ip 已出 300s 窗口

    assert "old-ip" not in daemon._WARP_IP_USED, "热表须按窗口清理"
    stats = daemon._warp_ip_history_stats()
    assert stats["old-ip"]["uses"] == 1, "热表清理不得带走历史"
    assert stats["old-ip"]["last_ts"] == 1000.0
    assert stats["new-ip"]["uses"] == 1


def test_multiple_lines_can_hold_slots_at_once(monkeypatch):
    """全池并发放开：五条线一起刷必须全部拿到名额。

    全池上限已随 `refresh_concurrency` 一起删除（互斥粒度改为单条线），
    本用例守住「多线并发不被挡」这条新语义。
    """
    members = [{"tag": f"W{i}", "port": 1080 + i, "service": "s"} for i in range(5)]
    monkeypatch.setitem(daemon.CFG["warp"], "members", members)
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())

    assert all(daemon._acquire_refresh_slot(m["tag"]) for m in members), \
        "五条线互不依赖，必须全部放行"
    assert daemon._acquire_refresh_slot("W0") is False, \
        "放开全池并发不等于同一条线可以重入"


def test_refresh_has_no_pool_wide_cap(monkeypatch):
    """池内各线互不依赖：已在刷 19 条时，第 20 条仍须放行。

    曾有一个全池上限（`refresh_concurrency`，0 时等于成员数）。它平时不生效；
    生效时日志里报的又是上限常量而非实际集合大小 —— 134 次「名额已满」里
    128 次其实是同一个 tag 自己撞自己，读日志完全分不清是哪种。互斥粒度只能
    是单条线。
    """
    members = [{"tag": f"W{i}", "port": 1080 + i, "service": "s"} for i in range(20)]
    monkeypatch.setitem(daemon.CFG["warp"], "members", members)
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    for m in members[:19]:
        assert daemon._acquire_refresh_slot(m["tag"]) is True
    assert daemon._acquire_refresh_slot(members[19]["tag"]) is True, \
        "已有 19 条在重启时第 20 条仍须放行"


def test_classify_egress_error_recognizes_current_429_wording():
    """429 文案换过版本：现版是 "Free usage limit reached"，旧版是 "Rate limit exceeded"。

    只认旧文案时实测最近 2000 条错误里 1751 条 429 全被判成「非出口级错误」
    → consec_err 恒 0 → 87% 错误率下也永不换线（生产宕机 2026-10-22 13:45）。
    """
    new_wording = ('{"type":"error","error":{"type":"FreeUsageLimitError",'
                   '"message":"Free usage limit reached. Subscribe to OpenCode Go '
                   'to keep going: https://opencode.ai/go"}}')
    old_wording = '{"error":{"message":"Rate limit exceeded"}}'
    abuse = '{"error":{"message":"abusive traffic from this IP"}}'
    other = '{"error":{"message":"Your request resulted in more tokens than this model"}}'

    assert daemon._classify_egress_error(new_wording) == "rate429"
    assert daemon._classify_egress_error(old_wording) == "rate429"
    assert daemon._classify_egress_error(abuse) == "abuse403"
    assert daemon._classify_egress_error(other) is None, "请求超长与出口无关"
    assert daemon._classify_egress_error(None) is None


def test_cached_warp_standby_dedups_same_ip(monkeypatch):
    """同一 IP 被多条隧道同时持有时，只保留最优的一条。

    重连竞态会让两条隧道指向同一会话 IP；两条都进候选池就等于把
    「换了个隧道但 IP 没变」当成换线 —— 切过去照样 429。
    """
    members = [{"tag": "W-A", "port": 1080},
               {"tag": "W-B", "port": 1081},
               {"tag": "W-C", "port": 1082}]
    ips = {"W-A": "1.1.1.1", "W-B": "1.1.1.1", "W-C": "2.2.2.2"}
    # 全部走 monkeypatch：直接赋值会在用例结束后留在模块上，污染后续用例
    # （实测 test_warp_refresh 的 4 个 IP 历史判据用例因此全红）。
    monkeypatch.setattr(daemon, "_warp_line_ip", lambda tag: ips[tag])
    monkeypatch.setattr(daemon, "_warp_line_ip_ts", lambda tag: 1000.0)
    monkeypatch.setattr(daemon, "_ip_disqualified", lambda ip, now, guard: False)
    monkeypatch.setattr(daemon, "_port_open", lambda port: True)
    monkeypatch.setattr(daemon, "_warp_ip_rank", lambda ip, now, window: (0, 0, 0.0))
    out = daemon._cached_warp_standby(members, 1000.0, guard=86400)
    got = [ip for _, ip in out]
    assert len(got) == len(set(got)), f"候选里出现重复 IP: {got}"
    assert got == ["1.1.1.1", "2.2.2.2"], f"去重后应各留一条: {got}"


def test_startup_switches_away_from_low_quality_egress(monkeypatch):
    """启动时当前出口若被判低质，必须立刻换线，不能把流量打在脏 IP 上。

    sing-box 重启后 selector 复位到配置 default（主隧道 WARP-CF），而它可能
    正处 429 惩罚期 —— 不校验就等于「重启后先白烧一段 IP，等第一条 429 才
    纠正」。实测 2026-10-22 13:45 重启后 WARP-CF 立即 429 ×3 才被换走。
    """
    calls = []
    probes = []

    def fake_reconnect(reason, now, ignore_cooldown=False):
        calls.append(reason)
        return True

    def fake_probe(port, *a, **k):
        probes.append(port)
        return "1.1.1.1"

    monkeypatch.setattr(daemon, "warp_egress_ip", fake_probe)
    monkeypatch.setattr(daemon, "_warp_ip_needs_refresh",
                        lambda ip, now, tag=None: True)
    monkeypatch.setattr(daemon, "_reconnect_warp_now", fake_reconnect)

    # 直接调 main() 调用的那个函数：用例原先复刻了分支体，main 里删掉整段
    # 也照样通过（codex P3）。
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "1.1.1.1")
    daemon._validate_startup_egress(
        {"tag": "WARP-CF", "port": 1080, "service": "x.service"})

    assert calls == ["启动校验 当前出口低质"], "启动时低质出口必须触发换线"
    assert probes == [], \
        f"换线是异步的，启动分支不得复探（读到旧线且白等 20s）: {probes}"


def test_startup_keeps_healthy_egress(monkeypatch):
    """出口健康时不得换线 —— 启动校验不能变成无条件切线。"""
    calls = []
    monkeypatch.setattr(daemon, "_warp_ip_needs_refresh",
                        lambda ip, now, tag=None: False)
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now, ignore_cooldown=False: calls.append(reason))

    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "1.1.1.1")
    daemon._validate_startup_egress(
        {"tag": "WARP-CF", "port": 1080, "service": "x.service"})

    assert calls == [], "健康出口不得在启动时被换走"


def test_startup_keeps_egress_when_unknown(monkeypatch):
    """探不到出口时不得盲换 —— 未知不是低质的证据。"""
    calls = []
    monkeypatch.setattr(daemon, "_warp_ip_needs_refresh",
                        lambda ip, now, tag=None: True)
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now, ignore_cooldown=False: calls.append(reason))

    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", None)
    daemon._validate_startup_egress(
        {"tag": "WARP-CF", "port": 1080, "service": "x.service"})

    assert calls == [], "出口未知时不得触发启动换线"


def test_probe_refresh_egress_polls_until_tunnel_warms(monkeypatch):
    """首探超时的隧道要轮询到真通，而不是两次预算后判死。

    实测同一批生产 device 配置、无守护进程干扰下，从进程启动到出口可用是
    5.0/7.6/7.8/7.9/7.9/8.3s；而复刻原「7s 再 12s」两次探测的对照里 6 条有
    5 条首探超时、靠第二探才拿到。首探预算压在可用时间中位数上等于抛硬币，
    抛输就重启、计时归零，隧道永远停在冷启动段。
    """
    calls = []

    def fake(port, timeout_sec=20.0):
        calls.append(timeout_sec)
        return "2a09:bac5::1" if len(calls) >= 3 else None

    monkeypatch.setattr(daemon, "warp_egress_ip", fake)
    monkeypatch.setattr(daemon, "time", _FakeTime())
    monkeypatch.setattr(daemon, "_warp", lambda: {"refresh_probe_timeout_sec": 7,
                                                  "ready_timeout_sec": 12,
                                                  "max_attempts": 3})

    assert daemon._probe_refresh_egress(1080) == "2a09:bac5::1"
    assert calls == [7, 7, 7], f"每次都用短预算轮询，实际 {calls}"


def test_probe_refresh_egress_gives_up_after_deadline(monkeypatch):
    """轮询也有上限：到点还不通就是真不通，返回 None 交调用方重启。"""
    calls = []
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: calls.append(timeout_sec))
    monkeypatch.setattr(daemon, "time", _FakeTime())
    monkeypatch.setattr(daemon, "_warp", lambda: {"refresh_probe_timeout_sec": 7,
                                                  "ready_timeout_sec": 12,
                                                  "max_attempts": 3})

    assert daemon._probe_refresh_egress(1080) is None
    # 上限 36s，每轮+睡眠推进 8s → 最多 5 次；多探几次也比过早判死强
    assert 2 <= len(calls) <= 6, f"轮询次数应受截止时间约束，实际 {len(calls)}"


def test_probe_refresh_egress_skips_retry_on_success(monkeypatch):
    """首探就成功时不得再探 —— 每次探测都走一次出口，白探会多烧配额。"""
    calls = []
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: calls.append(timeout_sec) or "1.1.1.1")
    monkeypatch.setattr(daemon, "time", _FakeTime())
    monkeypatch.setattr(daemon, "_warp", lambda: {"refresh_probe_timeout_sec": 7,
                                                  "ready_timeout_sec": 12,
                                                  "max_attempts": 3})

    assert daemon._probe_refresh_egress(1080) == "1.1.1.1"
    assert calls == [7], f"首探成功不得重探，实际 {calls}"


def test_exclude_active_ip_drops_same_as_current():
    """新出口与旧出口相同等于没换 —— 必须滤掉，否则白白重启目标线。

    实测 817 次换线里 111 次（13.7%）新出口 == 旧出口，全是 rate429 触发。
    """
    pairs = [({"tag": "A"}, "1.1.1.1"), ({"tag": "B"}, "2.2.2.2")]
    assert daemon._exclude_active_ip(pairs, "1.1.1.1") == [({"tag": "B"}, "2.2.2.2")]


def test_exclude_active_ip_dedups_across_tunnels():
    """两条隧道可能拿到同一 IP（CF 会话重复分配），只留首条（已按优先级排序）。"""
    pairs = [({"tag": "A"}, "1.1.1.1"), ({"tag": "B"}, "1.1.1.1"),
             ({"tag": "C"}, "2.2.2.2")]
    assert [m["tag"] for m, _ in daemon._exclude_active_ip(pairs, None)] == ["A", "C"]


def test_exclude_active_ip_falls_back_when_all_filtered():
    """全被滤空时退回原列表：宁可换到旧地址，也不能因此落轮换池（3h 冷却）。"""
    pairs = [({"tag": "A"}, "1.1.1.1")]
    assert daemon._exclude_active_ip(pairs, "1.1.1.1") == pairs


def test_dedupe_probe_pairs_skips_active_and_duplicates():
    """实测前先按 IP 收敛：同址多线只探一次，当前出口不探（马上会被排除）。"""
    pairs = [({"tag": "A"}, "1.1.1.1"), ({"tag": "B"}, "1.1.1.1"),
             ({"tag": "C"}, "2.2.2.2"), ({"tag": "D"}, "3.3.3.3")]
    out = daemon._dedupe_probe_pairs(pairs, "3.3.3.3")
    assert [m["tag"] for m, _ in out] == ["A", "C"]


# ============= blind 模式（observe_9router=false，纯时间轮换） =============

def test_observe_9router_toggle(monkeypatch):
    """开关三态：缺 key 默认开、显式 true/false 生效。"""
    assert daemon._observe_9router() is True
    monkeypatch.setitem(daemon.CFG, "traffic", {"observe_9router": False})
    assert daemon._observe_9router() is False
    monkeypatch.setitem(daemon.CFG, "traffic", {})
    assert daemon._observe_9router() is True, "缺 observe_9router 键应视为开启"


def test_proactive_mode_forced_fixed_when_observe_off(monkeypatch):
    """blind 模式强制 fixed：requests 判据要查 usageHistory，无数据=永不换线。

    config 里 proactive_mode=requests 时，blind 模式必须无视它 —— 要么退化为
    永不主动换线（计数恒 0），要么每 tick 查一次空库，两者都不对。
    """
    fired = _proactive_mode_env(monkeypatch, "requests")
    monkeypatch.setitem(daemon.CFG, "traffic", {"observe_9router": False})
    monkeypatch.setattr(daemon, "count_dsv4_ok_since",
                        lambda ts: (_ for _ in ()).throw(
                            AssertionError("blind 模式不得查 usageHistory")))
    daemon._maybe_proactively_rotate_warp(1100.0)   # 100s ≥ 57s
    assert len(fired) == 1 and "固定" in fired[0], \
        f"blind 强制 fixed 时间轮换，实际 {fired}"


def test_prewarm_skips_opencode_count_when_observe_off(monkeypatch):
    """blind 模式下预热不等流量闸门：流量不经 9router，计数恒 0。

    若照搬「count_opencode_since==0 就跳过」，9router 之外的流量会被当成
    「没人用」，备线永远不预热，换线候选全是旧会话。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "traffic", {"observe_9router": False})
    monkeypatch.setattr(daemon, "_last_warp_prewarm_ts", 0.0)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {"W2": {"sel": 1.0, "ip": "ip-w2"}})
    monkeypatch.setattr(daemon, "_warp_ip_recently_used", lambda ip, now: True)
    monkeypatch.setattr(daemon, "count_opencode_since",
                        lambda ts: (_ for _ in ()).throw(
                            AssertionError("blind 模式不得查 requestDetails")))
    scheduled = []
    monkeypatch.setattr(daemon, "_schedule_idle_refresh",
                        lambda m: scheduled.append(m["tag"]))

    daemon._prewarm_standby_warp(1000.0)
    assert scheduled == ["W2"], f"blind 下预热照常执行，实际 {scheduled}"


def test_failback_skips_blocked_counts_when_observe_off(monkeypatch):
    """blind 模式回切只看池探测/黑名单占比，不数落池后的 403/429（无数据）。"""
    thr = daemon.CFG["intervals"]["proxy_blocked_threshold"]
    called = _failback_env(monkeypatch, healthy=True, blocked=(thr, 0))
    monkeypatch.setitem(daemon.CFG, "traffic", {"observe_9router": False})
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts, limit=None: (_ for _ in ()).throw(
                            AssertionError("blind 模式不得查 requestDetails")))
    assert daemon._maybe_failback_to_warp(1e6) is False, \
        "blind 下判据 2 无数据，池健康+黑名单低时不得回切"


def test_record_node_ip_used_persists_and_reload(monkeypatch):
    """台账写 → 落盘 → 清内存 → 重载，一条链路。"""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("NODE_IP_USED_FILE_PATH", str(Path(d) / "n.json"))
        ts = time.time() - 100.0    # 台账落盘会裁掉窗口外条目，用近期时刻
        daemon._record_node_ip_used("1.2.3.4", ts=ts)
        monkeypatch.setattr(daemon, "_NODE_IP_USED", {})          # 模拟重启清内存
        daemon._load_node_ip_used()
        assert daemon._NODE_IP_USED.get("1.2.3.4") == ts
        # 空 ip / None 不打点不应炸
        daemon._record_node_ip_used(None)


def test_skip_recent_node_ips_blind_uses_6h_window(monkeypatch):
    """blind 模式脏 IP 避让：窗口内用过的跳过、过期的放行、无历史 IP 的放行。"""
    now = 10000.0
    monkeypatch.setitem(daemon.CFG, "traffic",
                        {"observe_9router": False, "ip_reuse_window_sec": 21600})
    monkeypatch.setattr(daemon, "_NODE_IP_USED",
                        {"fresh-ip": now - 100.0, "old-ip": now - 65000.0})
    skip = daemon._skip_recent_node_ips(
        ["n-hot", "n-cold", "n-new"], {"n-hot": "fresh-ip", "n-cold": "old-ip"}, now=now)
    assert skip == {"n-hot"}, f"6h 内用过的 n-hot 应跳过，过期的 n-cold 放行，实际 {skip}"


def test_skip_recent_node_ips_noop_when_observe_on(monkeypatch):
    """traffic-aware 模式不做时间维度避让：那里有实时错误信号验 IP。"""
    now = 10000.0
    monkeypatch.setattr(daemon, "_NODE_IP_USED", {"fresh-ip": now - 1.0})
    assert daemon._skip_recent_node_ips(["n1"], {"n1": "fresh-ip"}, now=now) == set()


def test_skip_recent_node_ips_window_zero_disables(monkeypatch):
    """ip_reuse_window_sec=0 显式关闭（默认仍开）。"""
    now = 10000.0
    monkeypatch.setitem(daemon.CFG, "traffic",
                        {"observe_9router": False, "ip_reuse_window_sec": 0})
    monkeypatch.setattr(daemon, "_NODE_IP_USED", {"fresh-ip": now - 1.0})
    assert daemon._skip_recent_node_ips(["n1"], {"n1": "fresh-ip"}, now=now) == set()


def test_pick_next_node_respects_dirty_ip_skip(monkeypatch):
    """集成：两个候选里脏 IP 节点被避让，切到干净的。"""
    now = time.time()
    _warp_env(monkeypatch, WARP_TWO)   # 复用现有 env 桩（节点/池/DB 假装可用）
    monkeypatch.setitem(daemon.CFG, "traffic",
                        {"observe_9router": False, "ip_reuse_window_sec": 21600})
    monkeypatch.setattr(daemon, "_NODE_IP_USED", {"dirty-ip": now - 60.0})
    monkeypatch.setattr(daemon, "_filter_candidates",
                        lambda cur, all_nodes, fail: ["n1", "n2"])
    monkeypatch.setattr(daemon, "_load_candidate_stats",
                        lambda cands: ({}, {"n1": "dirty-ip", "n2": "clean-ip"}))
    monkeypatch.setattr(daemon, "test_node_quick", lambda n, timeout_ms: True)
    monkeypatch.setattr(daemon, "_recent_nodes", deque())
    chosen = daemon.pick_next_node("", [], set(), count_call=False)
    assert chosen == "n2", f"脏 IP 节点 n1 应被避让选 n2，实际 {chosen}"


# ============= 按时间换线的进度标注（间隔 + 下次换线时刻） =============

def test_warp_fixed_progress_marks_interval_and_next_due(monkeypatch):
    """WARP fixed 判据的进度标签必须自带换线间隔与下次换线时刻。

    blind 模式下健康摘要是运维唯一的时间线来源，标签缺间隔或下次时刻就
    要回去翻 config 才知道多久换一次。
    """
    fired = _proactive_mode_env(monkeypatch, "fixed")
    daemon._maybe_proactively_rotate_warp(1055.0)      # 55s < 57s：只刷标签
    assert fired == [], "未到间隔不该换线，先把标签看清"
    label = daemon._proactive_progress
    assert "间隔60s" in label, f"标签需标换线间隔（配置值），实际 {label!r}"
    assert "已55s" in label, f"标签需标已过时长，实际 {label!r}"
    assert "下次≈" in label, f"标签需标下次换线时刻，实际 {label!r}"


def test_pool_proactive_label_marks_interval_and_next_due():
    """池侧定时轮换标签：间隔、已过时长、下次换线时刻三件套齐全。"""
    label = daemon._pool_proactive_label(1000.0, last_rotate=970.0, rotate_gap=60.0)
    assert "间隔60s" in label, f"标签需标换线间隔，实际 {label!r}"
    assert "已过30s" in label, f"标签需标已过时长，实际 {label!r}"
    assert "下次≈" in label, f"标签需标下次换线时刻，实际 {label!r}"


def test_pool_proactive_label_cold_start_counts_from_next_due():
    """刚启动（还没换过线，last_rotate=0）不得打出 5 位数的已过秒数。"""
    label = daemon._pool_proactive_label(1000.0, last_rotate=0.0, rotate_gap=60.0)
    assert "已过60s" in label, f"基准应退到 now − 间隔，实际 {label!r}"


def test_env_bool_parses_observe_override(monkeypatch):
    """ASIP_OBSERVE_9ROUTER：未设走默认，true/1/ON 为真，false 为假。"""
    monkeypatch.delenv("ASIP_OBSERVE_9ROUTER", raising=False)
    assert config._env_bool("ASIP_OBSERVE_9ROUTER", True) is True, "未设应走默认"
    monkeypatch.setenv("ASIP_OBSERVE_9ROUTER", "false")
    assert config._env_bool("ASIP_OBSERVE_9ROUTER", True) is False, "false 应关"
    monkeypatch.setenv("ASIP_OBSERVE_9ROUTER", "1")
    assert config._env_bool("ASIP_OBSERVE_9ROUTER", False) is True, "1 应开"
    monkeypatch.setenv("ASIP_OBSERVE_9ROUTER", "  ")
    assert config._env_bool("ASIP_OBSERVE_9ROUTER", True) is True, "空串应走默认"


def _stub_warp_cfg(monkeypatch):
    monkeypatch.setattr(daemon, "_warp", lambda: {
        "refresh_model_check": True,
        "refresh_model_timeout_sec": 15,
        "probe_ok_ttl_sec": 600,
        "probe_persistent_ttl_sec": 21600,
        "probe_transient_ttl_sec": 120,
        "blocked_guard_sec": 6 * 3600,
        "rate429_guard_sec": 24 * 3600,
        "blocked_ttl_sec": 24 * 3600,
    })
    monkeypatch.setattr(daemon, "_PROBE_BLOCKED_AT", {})


def test_cached_ok_verdict_does_not_skip_probe(monkeypatch):
    """缓存里的 OK 不得当作「已实测」跳过探测。

    旧 OK 的 TTL 是 600s，地址被生产流量烧掉只需几秒；命中旧 OK 直接切上去
    就是一轮 429 爆发。只有「地址已被拒」的持久负结论允许跳过。
    """
    _stub_warp_cfg(monkeypatch)
    monkeypatch.setattr(daemon, "_warp_model_usable", _REAL_WARP_MODEL_USABLE)
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN",
                        {("tag-a", "1.2.3.4"): ("OK", time.time())})
    called = []
    import probe_warp
    monkeypatch.setattr(probe_warp, "probe", lambda port, timeout: (
        called.append(port) or ("err", 0.1, "boom", None)))
    ok = daemon._warp_model_usable({"tag": "tag-a", "port": 10001}, "1.2.3.4")
    assert called == [10001], f"缓存 OK 时仍须实测，实际探测 {len(called)} 次"
    assert ok is False, "实测 err 应判不可用"


def test_cached_quota_verdict_skips_probe(monkeypatch):
    """持久负结论且封禁仍生效时跳过探测：它只驱动刷新，不会放行坏地址。"""
    _stub_warp_cfg(monkeypatch)
    monkeypatch.setattr(daemon, "_warp_model_usable", _REAL_WARP_MODEL_USABLE)
    now = time.time()
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN",
                        {("tag-b", "5.6.7.8"): ("quota", now)})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "5.6.7.8": {"ts": now, "blocked": True,
                    "blocked_ts_by_kind": {"rate429": now}}})
    import probe_warp
    monkeypatch.setattr(probe_warp, "probe", lambda port, timeout: (
        (_ for _ in ()).throw(AssertionError("持久负结论命中时不应再探测"))))
    assert daemon._warp_model_usable(
        {"tag": "tag-b", "port": 10002}, "5.6.7.8") is False


def test_cached_persistent_verdict_reprobes_after_exact_ban(monkeypatch):
    """缓存 TTL 6h 长于上游给的精确 ban：到期后必须重探，不能多锁 1.4h。

    旧实现见 `cached in _WARP_PROBE_PERSISTENT` 就返回 False；上游给 4.6h
    Retry-After 时，缓存仍会挡住到第 6 小时 —— 精确解禁在模型模式下失效。
    """
    _stub_warp_cfg(monkeypatch)
    monkeypatch.setattr(daemon, "_warp_model_usable", _REAL_WARP_MODEL_USABLE)
    now = time.time()
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN",
                        {("tag-c", "9.9.9.9"): ("quota", now - 5 * 3600)})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "9.9.9.9": {"ts": now - 5 * 3600, "blocked": True,
                    "blocked_ts_by_kind": {"rate429": now - 5 * 3600},
                    "ban_until": now - 1800, "ban_kind": "rate429"}})
    called = []
    import probe_warp
    monkeypatch.setattr(probe_warp, "probe", lambda port, timeout: (
        called.append(port) or ("OK", 0.2, "ok", None)))
    assert daemon._warp_model_usable(
        {"tag": "tag-c", "port": 10003}, "9.9.9.9") is True
    assert called == [10003], "精确封禁已到期，缓存负结论不得继续挡住实测"


def test_flagged_retry_after_does_not_shorten_403(monkeypatch):
    """403（flagged）不应用 Retry-After：6h 窗口不能被响应头缩成几十秒。"""
    now = 1_000_000.0
    h = 3600
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(daemon, "_append_warp_ip_history", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    daemon._mark_warp_ip_used("ip-403", now, blocked=True, kind="abuse403",
                              ban_after=30)
    entry = daemon._WARP_IP_USED["ip-403"]
    assert entry["ban_until"] is None, "403 不应写入精确封禁"
    assert daemon._warp_ip_is_blocked("ip-403", now + 5 * h) is True, \
        "6h 窗口内仍须挡住，不能被 Retry-After 缩短"
    assert daemon._warp_ip_is_blocked("ip-403", now + 6 * h + 1) is False


def test_legacy_403_ban_until_does_not_shorten_default(monkeypatch):
    """旧版把 Retry-After 写到 403 上留下的 ban_until 必须被忽略。

    否则同 kind 仍在 stamps 里时，`_warp_block_window` 会拿那个更短的
    ban_until 当窗口，把 403 的 6h 默认封禁缩短成响应头给的几十秒。
    """
    now = 1_000_000.0
    h = 3600
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-legacy403": {"ts": now, "blocked": True,
                         "blocked_ts_by_kind": {"abuse403": now},
                         "ban_until": now + h, "ban_kind": "abuse403"}})
    assert daemon._warp_ip_is_blocked("ip-legacy403", now + 5 * h) is True
    assert daemon._warp_ip_is_blocked("ip-legacy403", now + 6 * h + 1) is False


def test_expired_exact_ban_does_not_swallow_new_429(monkeypatch):
    """精确封禁到期后再被 429：必须重开一轮冷却，不能被旧 ban_until 放行。

    回归 d41cd74：旧实现保留同一类型的 stamp（窗口内）并把过期 ban_until
    当成「已恢复」，于是新 429 写进去后 `_warp_ip_is_blocked` 立刻返回 False。
    """
    now = 1_000_000.0
    h = 3600
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(daemon, "_append_warp_ip_history", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    daemon._mark_warp_ip_used("ip-re", now, blocked=True, kind="rate429",
                              ban_after=2 * h)
    assert daemon._warp_ip_is_blocked("ip-re", now + 2 * h + 1) is False
    daemon._mark_warp_ip_used("ip-re", now + 3 * h, blocked=True, kind="rate429")
    entry = daemon._WARP_IP_USED["ip-re"]
    assert entry["ban_until"] is None, "上一轮的精确封禁必须清掉"
    assert entry["blocked_ts_by_kind"] == {"rate429": now + 3 * h}
    assert daemon._warp_ip_is_blocked("ip-re", now + 3 * h + 1) is True
    assert daemon._warp_ip_is_blocked("ip-re", now + 3 * h + 61) is False


def test_bad_ban_until_does_not_break_hot_path(monkeypatch):
    """损坏的 ban_until 只丢精确封禁，仍按默认窗口挡住，且不抛异常。"""
    now = 1_000_000.0
    h = 3600
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-bad": {"ts": now, "blocked": True,
                   "blocked_ts_by_kind": {"rate429": now},
                   "ban_until": "not-a-number", "ban_kind": "rate429"}})
    assert daemon._warp_ip_is_blocked("ip-bad", now + 30) is True
    assert daemon._warp_ip_is_blocked("ip-bad", now + 61) is False

    # 非有限值同样是损坏台账：不能当「永久封禁」把 IP 锁死。
    for bad in (float("inf"), float("nan")):
        monkeypatch.setattr(daemon, "_WARP_IP_USED", {
            "ip-bad": {"ts": now, "blocked": True,
                       "blocked_ts_by_kind": {"rate429": now},
                       "ban_until": bad, "ban_kind": "rate429"}})
        assert daemon._warp_ip_is_blocked("ip-bad", now + 30) is True
        assert daemon._warp_ip_is_blocked("ip-bad", now + 61) is False
