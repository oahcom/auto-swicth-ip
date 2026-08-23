#!/usr/bin/env python3
"""auto-switch-ip 确定性单元测试（无网络、<30s）。

覆盖核心决策逻辑，替代 test_auto_switch.py 的慢速 live 集成测试：
- get_9router_error_rate：两层错误检测
- _update_strategy_mode：自适应回滞切换
- _delay_score / _p2c_select：节点选择评分
- _apply_test_results：坏节点标记 + TTL 升级 + 安全阀
- is_singbox_alive / restart_singbox：看门狗 + 重启互斥
"""
import sqlite3
import threading

import daemon


# ============= 1. get_9router_error_rate（mock _get_db） =============
def test_错误率两层检测_窗口与连续():
    schema = "CREATE TABLE requestDetails (provider TEXT, model TEXT, status TEXT, timestamp REAL)"
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
    mem.executemany("INSERT INTO requestDetails VALUES (?,?,?,?)", rows)

    orig = daemon._get_db
    daemon._get_db = lambda: mem
    try:
        total, errors, _, consecutive = daemon.get_9router_error_rate()
    finally:
        daemon._get_db = orig

    assert total == 10, f"total 应为 10（仅 opencode），实际 {total}"
    assert errors == 3, f"errors 应为 3，实际 {errors}"
    assert consecutive == 2, f"consecutive 应为 2，实际 {consecutive}"


def test_错误率_全成功返回零():
    schema = "CREATE TABLE requestDetails (provider TEXT, model TEXT, status TEXT, timestamp REAL)"
    mem = sqlite3.connect(":memory:")
    mem.execute(schema)
    mem.row_factory = sqlite3.Row
    mem.executemany(
        "INSERT INTO requestDetails VALUES (?,?,?,?)",
        [("opencode", "m", "success", 1_700_000_000 - i) for i in range(10)],
    )
    orig = daemon._get_db
    daemon._get_db = lambda: mem
    try:
        total, errors, _, consecutive = daemon.get_9router_error_rate()
    finally:
        daemon._get_db = orig
    assert total == 10 and errors == 0 and consecutive == 0, (total, errors, consecutive)


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
    daemon.subprocess.run = lambda *a, **k: None
    daemon.subprocess.Popen = lambda *a, **k: None
    daemon._set_outbound_proxy = lambda *a, **k: None
    daemon.is_singbox_alive = lambda: True  # 重启后探测成功
    daemon.time.sleep = lambda *a, **k: None
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  💥 {t.__name__}: 异常 {e}")
            failed += 1
    print(f"\n结果: ✅ {passed} 通过 / ❌ {failed} 失败 ({len(tests)} 个用例)")
    import sys
    sys.exit(1 if failed else 0)
