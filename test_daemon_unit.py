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
import threading
import time
from datetime import datetime, timezone

import pytest

import daemon


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
    monkeypatch.setattr(daemon, "_WARP_IP_CYCLES", [])
    monkeypatch.setattr(daemon, "_WARP_MIN_CYCLE_SEC", 0.0)
    monkeypatch.setattr(daemon, "_last_cycle_recalc_ts", 0.0)
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
def test_错误率两层检测_窗口与连续():
    schema = "CREATE TABLE requestDetails (provider TEXT, model TEXT, status TEXT, timestamp REAL, data TEXT)"
    mem = sqlite3.connect(":memory:")
    mem.execute(schema)
    mem.row_factory = sqlite3.Row
    now = 1_700_000_000
    rows = [
        # 最近 10 条：3 errors + 连续 2 失败结尾
        ("opencode", "mimo", "error", now),          # 连续 1
        ("opencode", "mimo", "error", now - 1),       # 连续 2
        ("opencode", "mimo", "success", now - 2),
        ("opencode", "mimo", "success", now - 3),
        ("opencode", "mimo", "error", now - 4),       # 第 3 个 error
        ("opencode", "mimo", "success", now - 5),
        ("opencode", "mimo", "success", now - 6),
        ("opencode", "mimo", "success", now - 7),
        ("opencode", "mimo", "success", now - 8),
        ("opencode", "mimo", "success", now - 9),
        ("opencode", "mimo", "success", now - 10),
        # 非 opencode provider：必须被排除
        ("kiro", "x", "error", now - 0.5),
        ("nvidia", "x", "error", now - 0.6),
    ]
    mem.executemany("INSERT INTO requestDetails (provider,model,status,timestamp) VALUES (?,?,?,?)", rows)

    orig = daemon._get_db
    daemon._get_db = lambda: mem
    try:
        total, errors, _, consecutive, _ = daemon.get_9router_error_rate()
    finally:
        daemon._get_db = orig

    assert total == 10, f"total 应为 10（仅 opencode），实际 {total}"
    assert errors == 3, f"errors 应为 3，实际 {errors}"
    assert consecutive == 2, f"consecutive 应为 2，实际 {consecutive}"


def test_错误率_全成功返回零():
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



def test_错误率_切换点过滤_旧节点错误不背锅():
    """新节点上线后，切换点之前的旧节点错误不应触发拉黑"""
    schema = "CREATE TABLE requestDetails (provider TEXT, model TEXT, status TEXT, timestamp REAL, data TEXT)"
    mem = sqlite3.connect(":memory:")
    mem.execute(schema)
    mem.row_factory = sqlite3.Row
    # 生产表 timestamp 为 ISO TEXT（'2026-10-16T17:33:48.877Z'），fixture 须对齐
    iso = lambda epoch: datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    switch_ts = 1_700_000_000 - 5  # 5 秒前切换
    rows = [
        # 切换点之后：2 success + 1 error
        ("opencode", "m", "success", iso(switch_ts + 1)),
        ("opencode", "m", "success", iso(switch_ts + 2)),
        ("opencode", "m", "error",   iso(switch_ts + 3)),
        # 切换点之前：全 error（旧节点的锅）
        ("opencode", "m", "error", iso(switch_ts - 1)),
        ("opencode", "m", "error", iso(switch_ts - 2)),
    ]
    mem.executemany("INSERT INTO requestDetails (provider,model,status,timestamp) VALUES (?,?,?,?)", rows)
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

def test_error_rate_counts_403_client_rejections():
    schema = "CREATE TABLE requestDetails (provider TEXT, model TEXT, status TEXT, timestamp REAL, data TEXT)"
    mem = sqlite3.connect(":memory:")
    mem.execute(schema)
    mem.row_factory = sqlite3.Row
    import json as _json
    e403 = _json.dumps({"status": "error", "response": {"status": 403, "error": "{}"}})
    e429 = _json.dumps({"status": "error", "response": {"status": 429, "error": "{}"}})
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
    orig_tag = daemon.active_warp_tag
    daemon.subprocess.run = lambda *a, **k: None
    daemon.subprocess.Popen = lambda *a, **k: None
    daemon._set_outbound_proxy = lambda *a, **k: None
    daemon.is_singbox_alive = lambda: True  # 重启后探测成功
    daemon.time.sleep = lambda *a, **k: None
    # 重启成功后 daemon 会恢复重启前的 WARP 线路（_switch_warp → PUT selector）。
    # 测试不得写到真实 sing-box，此处打桩。
    daemon.active_warp_tag = lambda: None
    daemon._switch_warp = lambda tag: True
    try:
        ok = daemon.restart_singbox()
        assert ok is True, "重启后探测成功应返回 True"
        assert daemon._RESTART_COUNT == 0, f"成功应清零计数，实际 {daemon._RESTART_COUNT}"
    finally:
        daemon.subprocess.run = orig_sub
        daemon.subprocess.Popen = orig_popen
        daemon._set_outbound_proxy = orig_set
        daemon.is_singbox_alive = orig_alive
        daemon.time.sleep = orig_sleep
        daemon._switch_warp = orig_switch
        daemon.active_warp_tag = orig_tag


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


def test_warp_failover_not_gated_by_node_switch(monkeypatch):
    """WARP 换线不得受 _last_successful_switch_ts 门控。

    实测事故：主循环里主动轮换（每 60s）排在错误触发之前，它刷新该时间戳，
    使 errors_after_switch 恒为 0，15:47~15:52 的 252 条 abuse 403 一次换线都没触发。
    这里断言判定不读那个时间戳：命中一次 abuse 403 就必须调用换线。
    """
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "block_threshold": 1})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0)
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 1, "rate429": 0, "ok": 0})
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
                        lambda ts: {"abuse403": 0, "rate429": 4, "ok": 0})
    daemon.maybe_reconnect_warp(1000.0)
    assert not launched, "429 未达阈值不应换线"

    # 5 条 429 达阈值，必须换线，且冷却按 2^n 从 20s 起算
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 0, "rate429": 5, "ok": 0})
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
                        lambda ts: {"abuse403": 1, "rate429": 9, "ok": 0})
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
    assert daemon.count_warp_blocked_since(cutoff) == {"abuse403": 1, "rate429": 1, "ok": 1}, \
        "两类拦截分开计数；success 不计入拦截但计入 ok（判断出口是否在服务）"
    assert daemon.count_warp_blocked_since(0) == {"abuse403": 2, "rate429": 1, "ok": 1}


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
    assert daemon.count_warp_blocked_since(0) == {"abuse403": 1, "rate429": 1, "ok": 1}, \
        "生产嵌套 JSON 形态必须同样计入两类拦截"


def test_failover_switches_to_standby_and_refreshes_idle(monkeypatch):
    """备线可用时必须换线（不原地重启），并异步刷新被换下的主线上会话。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: {1080: "1.1.1.1", 1081: "2.2.2.2"}[port])
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_restart_service", lambda s: pytest.fail("换线成功不应重启隧道"))
    monkeypatch.setattr(daemon, "_refresh_idle_warp", lambda m: None)
    assert daemon.reconnect_warp("test") is True
    assert switched == ["W2"], f"应切到备线 W2, 实际 {switched}"


def test_failover_skips_dead_standby(monkeypatch):
    """备线探测不通且拉不起来时跳过它，改切轮换池 proxy。

    不得切到不通的备线；也不再原地重连 —— 原地重连要 systemctl restart，
    D-Bus 不可用时必败，流量会停在坏出口上（实测 2026-10-19 19:09）。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: "1.1.1.1" if port == 1080 else None)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_restart_service", lambda s: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: False)
    assert daemon.reconnect_warp("test") is True
    assert switched == ["proxy"], f"应切轮换池 proxy，实际 {switched}"


def test_failover_without_standby_falls_back_to_proxy(monkeypatch):
    """单隧道部署（无备线）切到轮换池 proxy，而非原地重连当前隧道。"""
    _warp_env(monkeypatch, WARP_TWO[:1])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: "1.1.1.1")
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)
    monkeypatch.setattr(daemon, "_restart_service", lambda s: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: True)
    assert daemon.reconnect_warp("test") is True
    assert switched == ["proxy"], f"应切 proxy，实际 {switched}"


def test_inplace_restart_gives_up_after_max_attempts(monkeypatch):
    """出口一直不变时必须放弃，不能无限重启隧道。

    直接测 _restart_warp_inplace —— reconnect_warp 现已优先切 proxy，
    只有 proxy 切换也失败时才退回原地重连。
    """
    calls = []
    _warp_env(monkeypatch, WARP_TWO[:1], max_attempts=3)
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: "1.1.1.1")
    monkeypatch.setattr(daemon, "_restart_service", lambda s: calls.append(1) or True)
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
                        lambda ts: {"abuse403": 2, "rate429": 0, "ok": 0})
    monkeypatch.setattr(daemon.threading, "Thread", lambda **k: pytest.fail("未达阈值不应换线"))
    daemon.maybe_reconnect_warp(1000.0)

    launched = []

    class T:                       # 同步跑 target：等价于 worker 立刻结束
        def __init__(self, target=None, args=(), **k): self._t, self._a = target, args
        def start(self): launched.append(1) or self._t(*self._a)

    monkeypatch.setattr(daemon.threading, "Thread", T)
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 3, "rate429": 0, "ok": 0})
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
    # 快速选线读 `_WARP_LINE_STATE`（守护探到的 tag→IP+时刻）与 `_ACTIVE_WARP_IP`，
    # 都是进程级全局：不隔离时上一用例的缓存会被本用例当成「刚验证过」，
    # 直接跳过探测/刷新，断言全歪。
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {})
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", None)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon.time, "time", lambda: now)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: egress[str(port)])
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda port, t: True)
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

    daemon._record_active_ip_success(total=5, errors=1)   # 成功数 3 → 4

    assert daemon._WARP_IP_USED["fresh-ip"]["last_ok_ts"] == 1000.0
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
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: "probed-ip")
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
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: {"1080": "ip-a", "1081": "ip-b"}[str(port)])
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
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: "burned-ip")
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 1, "rate429": 0, "ok": 0})

    class T:
        def __init__(self, target=None, args=(), **k): pass
        def start(self): pass

    monkeypatch.setattr(daemon.threading, "Thread", T)
    daemon.maybe_reconnect_warp(1000.0)
    assert daemon._WARP_IP_USED.get("burned-ip", {}).get("blocked") is True, \
        "被 403 拦下的出口 IP 必须记为 blocked"

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

    def fake_restart(service):
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
                        lambda service: restarted.append(service) or True)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    assert daemon.reconnect_warp("test") is True
    assert "usque-warp-b.service" not in restarted, "新鲜 IP 不该触发备线重启"
    assert switched == ["W2"]

def test_warp_failover_skips_target_when_refresh_keeps_stale_ip(monkeypatch):
    """刷新后仍是窗口内用过的地址 → 跳过该备线，改切轮换池 proxy。"""
    egress = _reuse_env(monkeypatch, {"1080": "ip-w1", "1081": "stale-ip"})
    daemon._WARP_IP_USED["stale-ip"] = {"ts": 900.0, "blocked": False, "last_ok_ts": 900.0}
    monkeypatch.setattr(daemon, "_restart_service", lambda service: True)
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
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: {
        1080: "ip-w1", 1081: "ip-w2-stale", 1082: "ip-w3-fresh"}[port])
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {"ip-w1": {"ts": 1000.0, "blocked": False},
                         "ip-w2-stale": {"ts": 900.0, "blocked": False}})
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", {"W2"})   # W2 正在补刷
    monkeypatch.setattr(daemon, "_restart_service", lambda s: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: True)
    monkeypatch.setattr(daemon, "_wait_warp_idle", lambda m: False)
    monkeypatch.setattr(daemon, "_refresh_idle_warp", lambda m: None)
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

    def _probe(port):
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
                        lambda s: restarted.append(s) or True)
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
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: {
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
    monkeypatch.setattr(daemon, "_restart_service", lambda s: True)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    assert daemon.reconnect_warp("test") is True
    assert switched == ["W3"], f"应选最久没被用过的 W3，实际 {switched}"
    assert daemon._WARP_LINE_STATE["W3"]["sel"] == 100_000.0, \
        "选中的线要记下选择时刻，否则下次仍会被重复选中"


def test_proactive_rotate_switches_after_n_requests_without_errors(monkeypatch):
    """出口还在成功服务时，累计 N 条请求就主动换线，不等 403 冲断对话。

    实测同一出口 IP 在首次 403/429 前的请求数中位数 28；报错后才切意味着
    对话已经中断。这里断言：没有任何错误信号，仅凭请求计数也要发起换线。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"], "proactive_after_requests": 20,
        "cooldown_ok_sec": 20})
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 1000.0)
    monkeypatch.setattr(daemon, "count_opencode_since", lambda ts: 20)
    fired = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now: fired.append(reason) or True)

    daemon._maybe_proactively_rotate_warp(2000.0)
    assert fired == ["主动轮换 已服务 20 条"], f"应主动换线，实际 {fired}"
    # 换线路径本来就会打 `↻ WARP 换线 (主动轮换 已服务 20 条)`，这里不得重复
    assert daemon._last_warp_reconnect_ts == 1000.0, "本函数不该自己改换线窗口起点"


def test_proactive_rotate_counts_from_switch_effective_time(monkeypatch):
    """配额窗口从「换线生效」算起，不从「换线发起」算起。

    发起早于生效数秒，期间旧出口仍在服务流量；用发起时刻会把旧线的消耗
    算进新线配额，导致新线刚切上去就被主动换掉。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "proactive_after_requests": 1})
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 1000.0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 1010.0)
    seen = []
    monkeypatch.setattr(daemon, "count_opencode_since",
                        lambda ts: seen.append(ts) or 0)
    fired = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now: fired.append(reason) or True)

    daemon._maybe_proactively_rotate_warp(1100.0)
    assert seen == [1010.0], f"须以生效时刻为窗口起点，实际 {seen}"
    assert not fired, "计数为 0 不得换线"


def test_reconnect_warp_records_switch_effective_time(monkeypatch):
    """换线成功后记下生效时刻，主动轮换据此切窗口。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port: {1080: "ip-w1", 1081: "ip-w2"}[port])
    monkeypatch.setattr(daemon.time, "time", lambda: 7_000_000.0)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: True)

    assert daemon.reconnect_warp("test") is True
    assert daemon._last_warp_switch_ok_ts == 7_000_000.0


def test_proactive_rotate_respects_cooldown(monkeypatch):
    """换线冷却期内即使满足阈值也不得再换，否则隧道变永动机。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"], "proactive_after_requests": 1})
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 1030.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 1000.0)
    monkeypatch.setattr(daemon, "count_opencode_since", lambda ts: 999)
    fired = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now: fired.append(reason) or True)

    daemon._maybe_proactively_rotate_warp(1010.0)   # 冷却中
    assert not fired, "冷却期内不得主动换线"
    daemon._maybe_proactively_rotate_warp(1031.0)
    assert fired, "冷却结束后就该换"


def test_prewarm_picks_least_recently_selected_stale_line(monkeypatch):
    """预热最久没被选过、且已知 IP 不新鲜的那条备线，跳过当前线和新鲜线。

    ip_reuse_guard_sec=1h，不预热则每次换线可选的新鲜线只有刚补刷的一条，
    C/D/E 永远轮不到（实测流量长期在两条线之间往返）。
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
        "W3": {"sel": 300.0, "ip": "ip-w3"},     # 旧 IP，且最久没被选
    })
    monkeypatch.setattr(daemon, "_warp_ip_recently_used",
                        lambda ip, now: ip != "fresh-ip")
    monkeypatch.setattr(daemon, "count_opencode_since", lambda ts: 100)
    scheduled = []
    monkeypatch.setattr(daemon, "_schedule_idle_refresh",
                        lambda m: scheduled.append(m["tag"]))

    daemon._prewarm_standby_warp(1000.0)
    assert scheduled == ["W3"], f"应预热最久没被选过的 W3，实际 {scheduled}"

    # 已知 IP 仍新鲜 → 不重启它（白换掉一个好会话）
    scheduled.clear()
    daemon._WARP_LINE_STATE["W3"]["ip"] = "fresh-ip"
    monkeypatch.setattr(daemon, "_last_warp_prewarm_ts", 0.0)
    daemon._prewarm_standby_warp(1000.0)
    assert scheduled == ["W2"], f"新鲜线应跳过，实际 {scheduled}"


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
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: {
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
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: "burned-ip")
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {"burned-ip": {"ts": 1000.0, "blocked": True}})
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)
    # 探测完成后它已是当前出口
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    restarted = []
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda s: restarted.append(s) or True)
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())

    daemon._reclaim_dirty_warp_worker({"tag": "W1", "port": 1080,
                                       "service": "usque-warp.service"})

    assert not restarted, f"已成当前出口的线不得重启，实际 {restarted}"


def test_egress_label_shows_warp_with_ip(monkeypatch):
    """走 WARP 时标签须带 tag 与出口 IP —— 排查第一步是知道流量走哪条路。"""
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "members": WARP_TWO})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W2")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "2a09:bac5::1")
    assert daemon._egress_label("some-node") == "W2(2a09:bac5::1)"


def test_egress_label_shows_proxy_when_on_pool(monkeypatch):
    """停在轮换池时标签须显示 proxy 与节点名。"""
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)
    assert daemon._egress_label("pq.aws50.yydjc.top:443-vl") == "proxy(pq.aws50.yydjc.top:443-vl)"
    assert daemon._egress_label(None) == "proxy(?)"


def test_egress_label_unknown_ip_marked(monkeypatch):
    """IP 未知时标 '?'，不得为了一条日志去探测（会阻塞主循环 12s）。"""
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "members": WARP_TWO})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", None)
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda p: pytest.fail("日志标签不得触发 SOCKS 探测"))
    assert daemon._egress_label() == "W1(?)"


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

    def _restart(service):
        restarted.append(service)
        if "warp-b" in service:
            egress["1081"] = "new-b"        # 重启换出全新会话 IP
        if "warp-c" in service:
            egress["1082"] = "new-c"
        return True

    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: egress[str(port)])
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
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda p, t: True)
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port: {1080: "ip-w1", 1081: "ip-w2"}[port])
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-w1": {"ts": 999.0, "blocked": True},
        "ip-w2": {"ts": 999.0, "blocked": True},
    })
    monkeypatch.setattr(daemon, "_restart_service", lambda s: True)
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
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: {
        "1080": "burned-a", "1081": "clean-b"}[str(port)])
    # 当前时间 = 100_000s，clean-b 使用于 1000s → 已过 99000s ≈ 27.5h > 24h
    monkeypatch.setattr(daemon.time, "time", lambda: 100_000.0)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "burned-a": {"ts": 100_000.0, "blocked": True},
        "clean-b": {"ts": 1000.0, "blocked": False},   # 24h+ 前用过，直接可切
    })
    monkeypatch.setattr(daemon, "_restart_service", lambda s: True)
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
                        lambda port: "2a09::1" if port == 1082 else None)
    assert daemon._warp_any_reachable() is True

    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: None)
    assert daemon._warp_any_reachable() is False


def _failback_env(monkeypatch, *, healthy, blocked=(0, 0), dead=0, nodes=80,
                  reachable=True, dwell=1000.0):
    """搭好 _maybe_failback_to_warp 的最小环境，返回被记录的切回 reason 列表。"""
    monkeypatch.setattr(daemon, "opencode_out_on_warp", lambda: False)
    monkeypatch.setattr(daemon, "_proxy_pool_healthy", lambda **kw: healthy)
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": blocked[0], "rate429": blocked[1]})
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
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: {
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
    daemon._mark_warp_ip_used(ip, 1_064.0, blocked=True)
    entry = daemon._WARP_IP_USED[ip]
    assert entry["served_sec"] == 64.0, f"存活时长应为 64s，实际 {entry.get('served_sec')}"
    assert entry["blocked_ts"] == 1_064.0
    daemon._mark_warp_ip_used(ip, 2_000.0, blocked=True)
    assert entry["served_sec"] == 64.0, "重复标记不得用更晚的时间覆盖首次存活时长"


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
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: egress[str(port)])
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


def test_proactive_rotation_leads_by_switch_cost(monkeypatch):
    """主动轮换必须提前 switch_lead_sec 发起，否则新线在旧线到寿之后才生效。"""
    monkeypatch.setattr(daemon, "_WARP_MIN_CYCLE_SEC", 10.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 100.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0)
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_last_cycle_recalc_ts", 1e12)
    calls = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now, ignore_cooldown=False: calls.append(reason) or True)

    # 寿命 10s − 换线提前量 3s = 7s；已服务 6s 还不该动，7s 必须动
    daemon._maybe_proactively_rotate_warp(106.0)
    assert calls == [], "未到提前量就换线等于把地址寿命提前烧掉"
    daemon._last_proactive_warp_check = 0.0
    daemon._maybe_proactively_rotate_warp(107.0)
    assert len(calls) == 1 and "换线耗时" in calls[0]


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


def test_recompute_cycle_excludes_zero_ok_zero():
    """只统计 cycle>0 且 ok>0 的样本，取中位数作为典型周期。

    cycle=0/ok=0 是「切上去第一条请求就错」，不反映可用寿命；它会把统计
    拉向 0。取值用中位而非最小：最小值由最倒霉的一条线决定（实测 98 条
    样本 min 0.1s / 中位 20.2s），按最小值换线等于每几秒切一次。
    """
    daemon._WARP_IP_CYCLES = [
        {"ip": "a", "cycle": 5.0, "ended_at": time.time() - 100, "ok": 1},
        {"ip": "b", "cycle": 0.0, "ended_at": time.time() - 200, "ok": 0},  # 首错即废
        {"ip": "c", "cycle": 10.0, "ended_at": time.time() - 300, "ok": 1},
    ]
    res = daemon._recompute_min_warp_cycle(time.time())
    assert res == 10.0, f"应取可用样本中位数 10，实际 {res}"
    assert daemon._WARP_MIN_CYCLE_SEC == 10.0


def test_recompute_min_cycle_empty_when_no_usable():
    """全是 ok=0 或 cycle=0 时返回 0 并回退到请求数判据。"""
    daemon._WARP_IP_CYCLES = [
        {"ip": "a", "cycle": 0.0, "ended_at": time.time() - 100, "ok": 0},
        {"ip": "b", "cycle": 0.0, "ended_at": time.time() - 200, "ok": 0},
    ]
    res = daemon._recompute_min_warp_cycle(time.time())
    assert res == 0.0
    assert daemon._WARP_MIN_CYCLE_SEC == 0.0


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
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: "fresh-ip")
    monkeypatch.setattr(daemon, "count_warp_blocked_since",
                        lambda ts: {"abuse403": 1, "rate429": 0, "ok": 3})
    monkeypatch.setattr(daemon, "_record_warp_cycle",
                        lambda ip, cyc, now, ok=0: daemon._WARP_IP_CYCLES.append(
                            {"ip": ip, "cycle": cyc, "ended_at": now, "ok": ok}))

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


def test_proactive_rotate_uses_min_cycle_sample(monkeypatch):
    """有周期样本时按最短可用周期换线，并绕开冷却。

    403 一出现对话就已中断，只能赶在它前面；阈值取 24h 内成功服务过的段
    的最短周期（下限 5s）。它短于 cooldown_ok_sec(20s)，受冷却就永远等不到
    触发，故绕过；换线失败后的冷却不能绕（见下一个用例）。
    无样本时才回退到请求数判据。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "proactive_after_requests": 999,
                         "min_cycle_recalc_sec": 1,
                         "cooldown_ok_sec": 20})
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 9999.0)   # 冷却中也要能触发
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 1000.0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 1000.0)
    monkeypatch.setattr(daemon, "_WARP_IP_CYCLES",
                        [{"ip": "seed", "cycle": 7.0, "ended_at": 1000.0, "ok": 1}])
    monkeypatch.setattr(daemon, "_WARP_MIN_CYCLE_SEC", 0.0)
    monkeypatch.setattr(daemon, "_last_cycle_recalc_ts", 0.0)
    seen_sql = []
    monkeypatch.setattr(daemon, "count_opencode_since",
                        lambda ts: seen_sql.append(ts) or 0)
    fired = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now, ignore_cooldown=False:
                        fired.append((reason, ignore_cooldown)) or True)

    daemon._maybe_proactively_rotate_warp(1003.0)   # 3s < 7s 阈值
    assert not fired, "周期未到不得换线"
    assert daemon._WARP_MIN_CYCLE_SEC == 7.0

    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    daemon._maybe_proactively_rotate_warp(1010.0)   # 10s ≥ 7s
    assert len(fired) == 1, f"应按最短周期换线，实际 {fired}"
    assert "周期" in fired[0][0]
    assert fired[0][1] is True, "时间判据必须绕开成功换线后的 20s 冷却"
    assert seen_sql == [], "有样本时不再查请求计数"


def test_proactive_rotate_keeps_fail_cooldown(monkeypatch):
    """上次换线失败（reconnect 晚于 switch_ok）时，周期判据也必须守失败冷却。

    cooldown_fail_sec 是防「连切连败」把隧道重启成雷暴的，绕过它会让周期判据
    在换线一直失败时每 5s(最小周期下限) 重试一次。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_WARP_MIN_CYCLE_SEC", 5.0)
    monkeypatch.setattr(daemon, "_last_cycle_recalc_ts", 9999.0)   # 不重算
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 1000.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 1030.0)  # 之后失败
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


def test_proactive_rotate_falls_back_to_request_count(monkeypatch):
    """无周期样本（_WARP_MIN_CYCLE_SEC=0）时保留旧的按请求数判据。

    回退路径必须继续受冷却约束（ignore_cooldown=False）：按请求数触发是旧的
    保守行为，不能因为新增了时间判据就顺手绕开冷却。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setitem(daemon.CFG, "warp", {
        **daemon.CFG["warp"], "proactive_after_requests": 20,
        "min_cycle_recalc_sec": 1, "cooldown_ok_sec": 20})
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0.0)
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 1000.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 1000.0)
    monkeypatch.setattr(daemon, "_WARP_MIN_CYCLE_SEC", 0.0)
    monkeypatch.setattr(daemon, "_WARP_IP_CYCLES", [])
    monkeypatch.setattr(daemon, "_last_cycle_recalc_ts", 0.0)
    fired = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now, ignore_cooldown=False:
                        fired.append((reason, ignore_cooldown)) or True)

    monkeypatch.setattr(daemon, "count_opencode_since", lambda ts: 19)
    daemon._maybe_proactively_rotate_warp(1010.0)
    assert not fired, "19 条未达阈值 20，不得换线"

    monkeypatch.setattr(daemon, "count_opencode_since", lambda ts: 20)
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    daemon._maybe_proactively_rotate_warp(1020.0)
    assert len(fired) == 1, f"20 条应触发一次换线，实际 {fired}"
    assert "已服务" in fired[0][0], f"回退路径 reason 应含「已服务」，实际 {fired[0][0]}"
    assert fired[0][1] is False, "回退路径必须受冷却约束（ignore_cooldown=False）"


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
        **daemon.CFG["warp"], "proactive_after_requests": 60,
        "min_cycle_recalc_sec": 99999, "cooldown_ok_sec": 20})
    monkeypatch.setattr(daemon, "_last_proactive_warp_check", 0.0)
    monkeypatch.setattr(daemon, "_warp_cooldown_until", 0.0)
    # 重启后状态：两个内存时间戳都是 0
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 0.0)
    monkeypatch.setattr(daemon, "_last_warp_reconnect_ts", 0.0)
    monkeypatch.setattr(daemon, "_WARP_MIN_CYCLE_SEC", 0.0)
    monkeypatch.setattr(daemon, "_last_cycle_recalc_ts", 0.0)
    monkeypatch.setattr(daemon, "_WARP_IP_CYCLES", [])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: "ip-w1")
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {"ip-w1": {"ts": 1000.0, "blocked": False}})
    seen = []
    monkeypatch.setattr(daemon, "count_opencode_since",
                        lambda ts: seen.append(ts) or 5)
    fired = []
    monkeypatch.setattr(daemon, "_reconnect_warp_now",
                        lambda reason, now, ignore_cooldown=False:
                        fired.append(reason) or True)

    daemon._maybe_proactively_rotate_warp(1100.0)
    assert seen == [1000.0], f"段起点必须取自 IP 记录（1000），实际 {seen}"
    assert not fired, "只服务了 5 条，不该换线"


def test_guard_window_depends_on_trigger_reason():
    """403 用 3h 窗口，429 用 24h 窗口，其它触发用默认窗口。"""
    w = daemon._warp()
    assert daemon._warp_guard_for_reason("abuse403 ×1") == w["blocked_guard_sec"]
    assert daemon._warp_guard_for_reason("rate429 ×3") == w["rate429_guard_sec"]
    assert daemon._warp_guard_for_reason("主动轮换 已服务 60 条") == w["ip_reuse_guard_sec"]
    # 两者同时命中时按 403 处理（更保守的信誉标记优先）
    assert daemon._warp_guard_for_reason("abuse403 ×1 + rate429 ×2") == w["blocked_guard_sec"]
    assert w["blocked_guard_sec"] < w["rate429_guard_sec"], "403 窗口应短于 429"


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
        **daemon.CFG["warp"], "blocked_guard_sec": 10800,
        "rate429_guard_sec": 86400, "ip_reuse_guard_sec": 86400})
    now = 100000.0
    # W1: 用过且在 3h 内；W2: 从没用过
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {"ip-old": {"ts": now - 600, "blocked": False}})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)   # 在 proxy 上
    monkeypatch.setattr(daemon, "_probe_warp_egress",
                        lambda members: {"W1": "ip-old", "W2": "ip-new"})
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port: {"1080": "ip-old", "1081": "ip-new"}.get(str(port)))
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    picked = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: picked.append(tag) or True)
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", lambda ip, ts, blocked=False: None)
    monkeypatch.setattr(daemon, "_warp_line_selected", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda *a: None)
    monkeypatch.setattr(daemon, "_refresh_idle_warp", lambda m: "ip-new")

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

    daemon._record_active_ip_success(10, 4)      # 基线：ok=6
    assert daemon._WARP_IP_USED["ip-a"]["last_ok_ts"] is None, "首轮只建基线"
    daemon._record_active_ip_success(10, 4)      # ok 未变 → 无新增成功
    assert daemon._WARP_IP_USED["ip-a"]["last_ok_ts"] is None, \
        "窗口里那条旧成功不能反复算作新成功"
    daemon._record_active_ip_success(10, 3)      # ok 6→7
    assert daemon._WARP_IP_USED["ip-a"]["last_ok_ts"] == now, "新增成功应刷新 last_ok_ts"

    # 换线到新出口：窗口里的成功大多来自旧出口，不能算到新 IP 头上
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "ip-b")
    monkeypatch.setattr(daemon, "_WARP_IP_USED",
                        {**daemon._WARP_IP_USED,
                         "ip-b": {"ts": now - 10, "blocked": False, "last_ok_ts": None}})
    daemon._record_active_ip_success(10, 2)      # ok 8，但属于旧出口
    assert daemon._WARP_IP_USED["ip-b"]["last_ok_ts"] is None, \
        "换线后第一轮只重建基线，不能凭旧出口的成功给新 IP 记一笔"


def test_warp_ip_rank_orders_unseen_before_no_success_before_ok(monkeypatch):
    """选线三档：从没用过 > 有成功但已久 > 从没成功过。

    第 2 档必须排在第 3 档前面：一个「切上去就 403、从没服务成功过」的地址
    （实测 350 条记录里 97 条）不该被当成新鲜地址直接切，但也不是首选 ——
    它比真正没见过的地址更可疑。第 3 档按距上次成功的时长倒序，越久越优先。
    """
    now = 100000.0
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "never-seen": None,                      # 占位，下面会删
        "burned-no-ok": {"ts": now - 30, "blocked": False, "last_ok_ts": None},
        "old-ok":      {"ts": now - 5000, "blocked": False, "last_ok_ts": now - 5000},
        "recent-ok":   {"ts": now - 100, "blocked": False, "last_ok_ts": now - 100},
    })
    del daemon._WARP_IP_USED["never-seen"]

    ranks = {ip: daemon._warp_ip_rank(ip, now, 86400)
             for ip in ("brand-new", "burned-no-ok", "old-ok", "recent-ok")}
    assert ranks["brand-new"][0] == 0, "没见过的地址应第 0 档"
    assert ranks["burned-no-ok"][0] == 1, "没成功过的地址应第 1 档"
    assert ranks["old-ok"][0] == 2 and ranks["recent-ok"][0] == 2
    # 第 2 档内部：越久没成功越优先（排序值越小，因为调用方用 min()）
    assert ranks["old-ok"][1] < ranks["recent-ok"][1]
    # 整体排序：brand-new < burned-no-ok < old-ok < recent-ok
    ordered = sorted(ranks, key=lambda ip: ranks[ip])
    assert ordered == ["brand-new", "burned-no-ok", "old-ok", "recent-ok"], ordered
