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
    assert daemon._warp_ip_recently_used("a", 1100.0), "100s 前用过，仍在窗口内"
    assert not daemon._warp_ip_recently_used("b", 1100.0), "500s 前用过，应视为未使用"
    assert "b" not in daemon._WARP_IP_USED, "出窗口的条目应被清理"
    assert not daemon._warp_ip_recently_used("never", 1100.0)
    assert not daemon._warp_ip_recently_used(None, 1100.0)

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
    assert daemon._warp_ip_recently_used("burned-ip", 1100.0), "使用时间也须恢复"

def test_warp_failover_refreshes_target_ip_before_switch(monkeypatch):
    """目标线当前出口在窗口内用过 → 必须先重启那条隧道换会话 IP 再切过去。

    否则等于从一个被限流的出口切到另一个同样被限流的出口，白换一次。
    """
    egress = _reuse_env(monkeypatch, {"1080": "ip-w1", "1081": "stale-ip"})
    daemon._WARP_IP_USED["stale-ip"] = {"ts": 900.0, "blocked": False}  # 100s 前用过，窗口内
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
    daemon._WARP_IP_USED["stale-ip"] = {"ts": 900.0, "blocked": False}
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


def test_warp_ip_exhausted_when_all_seen_blocked(monkeypatch):
    """见过的 IP 全被标记 → 地址池耗尽，刷新也是白刷。"""
    monkeypatch.setattr(daemon, "_WARP_IP_SEEN", {"a", "b", "c"})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "a": {"ts": 1.0, "blocked": True},
        "b": {"ts": 1.0, "blocked": True},
        "c": {"ts": 1.0, "blocked": True},
    })
    assert daemon._warp_ip_exhausted() is True


def test_warp_ip_not_exhausted_when_some_clean_or_unknown(monkeypatch):
    """还有未标记的、或根本没见过的 → 未耗尽，仍值得刷。"""
    monkeypatch.setattr(daemon, "_WARP_IP_SEEN", {"a", "b"})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "a": {"ts": 1.0, "blocked": True},
        "b": {"ts": 1.0, "blocked": False},
    })
    assert daemon._warp_ip_exhausted() is False

    # 空集合不得判耗尽：刚启动时还没见过任何 IP
    monkeypatch.setattr(daemon, "_WARP_IP_SEEN", set())
    assert daemon._warp_ip_exhausted() is False


def test_warp_ip_seen_accumulates_across_window_cleanup(monkeypatch, tmp_path):
    """累积集不受窗口清理影响 —— 否则"用过多少 IP"会被窗口大小截断。"""
    monkeypatch.setenv("WARP_IP_USED_FILE_PATH", str(tmp_path / "u.json"))
    monkeypatch.setitem(daemon.CFG, "warp", {**daemon.CFG["warp"], "ip_reuse_guard_sec": 300})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_WARP_IP_SEEN", set())

    daemon._mark_warp_ip_used("old-ip", 1000.0)
    daemon._mark_warp_ip_used("new-ip", 5000.0)   # 触发清理，old-ip 出窗口
    assert "old-ip" not in daemon._WARP_IP_USED, "窗口外的条目应被清理"
    assert daemon._WARP_IP_SEEN == {"old-ip", "new-ip"}, "累积集须保留两者"

    # 落盘后重载仍保留
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_WARP_IP_SEEN", set())
    daemon._load_warp_ip_used()
    assert daemon._WARP_IP_SEEN == {"old-ip", "new-ip"}, "累积集须跨重启保留"


def test_reconnect_warp_skips_probe_when_pool_exhausted(monkeypatch):
    """地址池耗尽 → 连备线都不探，直接切 proxy。

    这里的断言是「探测次数为 0」而非只看结果：不短路时走「遍历备线 → 全被标记
    → fallback」也能得到同样的 switched == ['proxy']，但那要在每条线上各花
    最多 12s 的 SOCKS 探测（实测 22:36 就是这样白等了三次）。
    """
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    probes = []

    def _count_probe(port):
        probes.append(port)
        return "ip-w1"

    monkeypatch.setattr(daemon, "warp_egress_ip", _count_probe)
    monkeypatch.setattr(daemon, "_WARP_IP_SEEN", {"ip-w1", "ip-w2"})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-w1": {"ts": 1000.0, "blocked": True},
        "ip-w2": {"ts": 1000.0, "blocked": True},
    })
    restarted = []
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda s: restarted.append(s) or True)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    assert daemon.reconnect_warp("test") is True
    assert switched == ["proxy"], f"池耗尽应直接切 proxy，实际 {switched}"
    assert not restarted, f"池耗尽时不该再刷新隧道，实际 {restarted}"
    # 只允许一次探测：取当前线出口 IP 那一次（旧 IP 要标记进 _WARP_IP_USED）。
    # 备线的 1081 不该被探。
    assert probes.count(1081) == 0, f"池耗尽时不该探备线，实际探测了 {probes}"


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


def test_warp_failover_skips_blocked_ip_and_early_falls_back(monkeypatch):
    """备线出口已被标记 → 直接跳过，不再白试；三条全标记时立即切 proxy。

    实测 22:27~22:38：换 3 个 IP 仍 403 100%，而每条备线探测最长 12s，
    依次试完要 36s。已被标记的 IP 试了也白试，必须跳过。
    """
    _warp_env(monkeypatch, WARP_TWO + [{"tag": "W3", "port": 1082,
                                        "service": "usque-warp-c.service"}])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: {
        "1080": "ip-w1", "1081": "burned-b", "1082": "burned-c"}[str(port)])
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-w1": {"ts": 1000.0, "blocked": True},
        "burned-b": {"ts": 1000.0, "blocked": True},   # 整段被标记
        "burned-c": {"ts": 1000.0, "blocked": True},
    })
    restarted = []
    monkeypatch.setattr(daemon, "_restart_service",
                        lambda s: restarted.append(s) or True)
    switched = []
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: switched.append(tag) or True)

    assert daemon.reconnect_warp("test") is True
    assert switched == ["proxy"], f"全被标记应直接切 proxy，实际 {switched}"
    assert not restarted, f"已被标记的线不该白白重启刷新，实际 {restarted}"


def test_warp_failover_uses_unblocked_line_when_mixed(monkeypatch):
    """一条被标记、另一条干净 → 用干净的那条，不必切 proxy。"""
    _warp_env(monkeypatch, WARP_TWO)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: {
        "1080": "burned-a", "1081": "clean-b"}[str(port)])
    monkeypatch.setattr(daemon.time, "time", lambda: 10000.0)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "burned-a": {"ts": 10000.0, "blocked": True},
        "clean-b": {"ts": 1000.0, "blocked": False},   # 早已出 3600s 窗口，直接可切
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
    monkeypatch.setattr(daemon, "_WARP_IP_SEEN", set())
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_switch_warp", lambda tag: True)
    monkeypatch.setattr(daemon, "_schedule_idle_refresh", lambda m: None)
    monkeypatch.setattr(daemon, "_warp_ip_exhausted", lambda: False)
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
