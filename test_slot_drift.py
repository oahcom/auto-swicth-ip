#!/usr/bin/env python3
"""配置漂移自愈：热重载不更新 selector 成员 → 校验 / 单槽重启 / keeper 兜底。

回归 2026-11-13 的生产事实：磁盘 7 份配置均含 147 条成员，槽位 sing-box
进程 10:36 启动后运行时只有 108 条，`PUT /configs?force=true` 返回 204
却不会重建 outbound —— 新线 PUT 进去只拿 400 not found，10 条新鲜 OK
线一条也补不进。自愈分两层：方一（订阅刷新后校验+滚动重启）、
方二（keeper 补闸连续被挡时兜底自愈）。
"""
import types

import pytest

import daemon


WARP_SLOTS = [
    {"name": "slot-%d" % i, "selector": "opencode-out-%d" % i,
     "port": 7890 if i == 1 else 7890 + i,
     "config_file": "/tmp/asip-drift-test/cfg-slot%d.json" % i}
    for i in range(1, 7)
]
GATE = {"name": "slot-7", "selector": "opencode-out-7", "port": 7897,
        "kind": "proxy",
        "config_file": "/tmp/asip-drift-test/cfg-slot7.json"}
ALL_SLOTS = WARP_SLOTS + [GATE]
REAL_LINES = {"WARP-L%d" % i for i in range(1, 7)}


class _NoopThread:
    def __init__(self, target=None, args=(), kwargs=None, **kw):
        self.target = target

    def start(self):
        pass


@pytest.fixture
def drift_sandbox(monkeypatch, tmp_path):
    logdir = tmp_path / "log"
    logdir.mkdir()
    monkeypatch.setattr(daemon, "CFG", {
        "singbox": {"binary": "sing-box", "slot_drift_heal": True},
        "log_file": str(logdir / "singbox.log"),
        "intervals": {},
    })
    monkeypatch.setattr(daemon.threading, "Thread", _NoopThread)
    monkeypatch.setattr(daemon, "consumer_slots", lambda: ALL_SLOTS)
    monkeypatch.setattr(daemon, "_warp_member",
                        lambda t: t in REAL_LINES)
    monkeypatch.setattr(daemon, "_slot_serving_name", lambda: "slot-1")
    monkeypatch.setattr(daemon, "_warp_line_fresh_candidates",
                        lambda exclude, now: [("WARP-L1", "2606::1")])
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)

    # 运行时 selector 成员（按槽隔离）+ /version 就绪开关
    rt = {s["name"]: (set(REAL_LINES) | {"proxy"}) for s in WARP_SLOTS}
    rt["slot-7"] = {"proxy"}
    state = {"versions_ok": True, "api_broken": False, "popen": []}

    def fake_api(slot, path, method="GET", body=None, timeout=3,
                 silent=False):
        if state["api_broken"]:
            return 0, None
        if path == "/version":
            return (200, {}) if state["versions_ok"] else (0, None)
        return 200, {"all": sorted(rt.get(slot["name"], set()))}
    monkeypatch.setattr(daemon, "slot_singbox_api", fake_api)

    # 磁盘配置成员：默认与运行时一致
    disk = {s["name"]: (set(REAL_LINES) | {"proxy"}) for s in WARP_SLOTS}
    disk["slot-7"] = {"proxy"}
    monkeypatch.setattr(daemon, "_slot_disk_selector_members",
                        lambda slot: set(disk.get(slot["name"], set())))

    class FakePopen:
        def __init__(self, cmd, **kw):
            state["popen"].append(list(cmd))

    monkeypatch.setattr(daemon.subprocess, "Popen", FakePopen)

    daemon._SLOT_HEALTH.clear()
    daemon._MAIN_SLOT_NAME["name"] = "slot-1"
    daemon._SLOT_DRIFT.update({"checked": 0, "healed": 0, "warn_only": 0,
                               "fail": 0, "last_heal_ts": 0.0,
                               "skip_since": 0.0, "last_filled": 0,
                               "slot_last_restart": {}})
    daemon._SLOT_REFILL_STATS.update({"rounds": 0, "filled": 0,
                                      "last_ts": 0.0,
                                      "skipped_not_member": 0})
    return types.SimpleNamespace(rt=rt, disk=disk, state=state)


# ── 1. 判据 ─────────────────────────────────────────────────────────────

def test_drift_detects_missing_real_lines(drift_sandbox):
    """磁盘有、运行时没有的真实线 → 漂移集。"""
    drift_sandbox.rt["slot-2"].discard("WARP-L2")
    drift_sandbox.rt["slot-2"].discard("proxy")   # 非 WARP tag 不算漂移
    d = daemon._slot_members_drift(WARP_SLOTS[1])
    assert d == {"WARP-L2"}


def test_drift_empty_when_members_consistent(drift_sandbox):
    for s in WARP_SLOTS:
        assert daemon._slot_members_drift(s) == set()
    assert daemon._slot_members_drift(GATE) == set()


def test_drift_none_when_api_unreadable(drift_sandbox):
    """运行时读不到按「无法判定」处理，绝不能当成漂移去重启进程。"""
    drift_sandbox.state["api_broken"] = True
    assert daemon._slot_members_drift(WARP_SLOTS[0]) is None


def test_drift_ignores_tags_not_in_ledger(drift_sandbox):
    """缺失集里没有真实线（只剩休眠/占位 tag）→ 不判漂移（审查修订 #1）。"""
    drift_sandbox.rt["slot-3"] = {"proxy"}
    drift_sandbox.disk["slot-3"] = {"dormant-x", "proxy"}
    assert daemon._slot_members_drift(WARP_SLOTS[2]) == set()


# ── 2. 方一：_reload_config_set 热载后校验 ──────────────────────────────

def test_reload_config_set_restarts_drifted_slot(drift_sandbox, monkeypatch):
    monkeypatch.setattr(daemon, "reload_singbox_config",
                        lambda path=None, slot=None: True)
    drift_sandbox.rt["slot-2"].discard("WARP-L2")
    drift_sandbox.rt["slot-4"].discard("WARP-L4")
    ok = daemon._reload_config_set(
        [(WARP_SLOTS[1]["config_file"], "x"),
         (WARP_SLOTS[3]["config_file"], "x")])
    assert ok is True
    assert daemon._SLOT_DRIFT["healed"] == 2
    restarted = [c[3] for c in drift_sandbox.state["popen"]]
    assert WARP_SLOTS[1]["config_file"] in restarted
    assert WARP_SLOTS[3]["config_file"] in restarted
    # slot-7 没在 changed 里，不该被碰
    assert GATE["config_file"] not in restarted


def test_reload_config_set_warns_for_serving_and_repairing(
        drift_sandbox, monkeypatch):
    """serving / repairing 槽漂移只告警，绝不主动制造出口空窗（修订 #3）。"""
    monkeypatch.setattr(daemon, "reload_singbox_config",
                        lambda path=None, slot=None: True)
    drift_sandbox.rt["slot-1"].discard("WARP-L1")   # serving
    drift_sandbox.rt["slot-3"].discard("WARP-L3")
    daemon._slot_mark("slot-3", "repairing")
    daemon._reload_config_set([(WARP_SLOTS[0]["config_file"], "x"),
                               (WARP_SLOTS[2]["config_file"], "x")])
    assert drift_sandbox.state["popen"] == []
    assert daemon._SLOT_DRIFT["warn_only"] == 2
    assert daemon._SLOT_DRIFT["healed"] == 0


# ── 3. 方二：keeper 兜底自愈 ────────────────────────────────────────────

def _arm_heal(sandbox):
    """构造「补闸连续被挡 3min+、有 OK 候选、五个坏槽全漂移」的现场。"""
    daemon._SLOT_REFILL_STATS["skipped_not_member"] = 5
    daemon._SLOT_DRIFT["skip_since"] = daemon.time.time() - 200
    for s in WARP_SLOTS:
        daemon._slot_mark(s["name"], "bad")
    # 逐槽挖掉一条真实线，制造「磁盘有、运行时无」
    for i, s in enumerate(WARP_SLOTS):
        sandbox.rt[s["name"]].discard("WARP-L%d" % ((i % 6) + 1))


def test_drift_heal_restarts_bad_drifted_slots(drift_sandbox):
    _arm_heal(drift_sandbox)
    daemon._slot_drift_maybe_heal(reason="keeper")
    assert daemon._SLOT_DRIFT["healed"] >= 1
    restarted = [c[3] for c in drift_sandbox.state["popen"]]
    assert all(cfg != GATE["config_file"] for cfg in restarted)
    assert WARP_SLOTS[1]["config_file"] in restarted


def test_drift_heal_respects_cooldown_and_backoff(drift_sandbox):
    _arm_heal(drift_sandbox)
    daemon._SLOT_DRIFT["last_heal_ts"] = daemon.time.time()
    daemon._slot_drift_maybe_heal(reason="keeper")
    assert drift_sandbox.state["popen"] == []
    # 连续失败两次 → 退避 60min
    daemon._SLOT_DRIFT.update({"last_heal_ts": daemon.time.time() - 1000,
                               "fail": 2})
    daemon._slot_drift_maybe_heal(reason="keeper")
    assert drift_sandbox.state["popen"] == []


def test_drift_heal_skips_without_ok_candidates(drift_sandbox, monkeypatch):
    """台账没有 OK 候选时不白重启：重启解决不了无线可补。"""
    _arm_heal(drift_sandbox)
    monkeypatch.setattr(daemon, "_warp_line_fresh_candidates",
                        lambda exclude, now: [])
    daemon._slot_drift_maybe_heal(reason="keeper")
    assert drift_sandbox.state["popen"] == []
    assert daemon._SLOT_DRIFT["healed"] == 0


def test_drift_heal_disabled_by_switch(drift_sandbox):
    _arm_heal(drift_sandbox)
    daemon.CFG["singbox"]["slot_drift_heal"] = False
    daemon._slot_drift_maybe_heal(reason="keeper")
    assert drift_sandbox.state["popen"] == []


def test_drift_heal_needs_sustained_skip(drift_sandbox):
    """刚出现 skipped（<3min）不触发，避免探测风暴时乱重启。"""
    _arm_heal(drift_sandbox)
    daemon._SLOT_DRIFT["skip_since"] = daemon.time.time() - 30
    daemon._slot_drift_maybe_heal(reason="keeper")
    assert drift_sandbox.state["popen"] == []


def test_drift_heal_resets_timer_after_successful_refill(drift_sandbox):
    """skipped 是累计口径：补闸刚成功回填过 = 挡路不持续，重新计时。"""
    _arm_heal(drift_sandbox)
    daemon._SLOT_DRIFT["skip_since"] = daemon.time.time() - 200
    daemon._SLOT_REFILL_STATS["filled"] = 3   # 回填刚成功过
    daemon._slot_drift_maybe_heal(reason="keeper")
    assert drift_sandbox.state["popen"] == []
    # skip_since 被重置后因 skipped 仍 >0 重新起算（当前时刻），3min 内不会触发
    assert daemon._SLOT_DRIFT["last_filled"] == 3
    assert daemon._SLOT_DRIFT["skip_since"] > 0


def test_reload_config_set_skips_proxy_gate(drift_sandbox, monkeypatch):
    """池闸与方二同口径：成员归池管线管，方一不重启它。"""
    monkeypatch.setattr(daemon, "reload_singbox_config",
                        lambda path=None, slot=None: True)
    drift_sandbox.disk["slot-7"] = set(REAL_LINES) | {"proxy"}
    drift_sandbox.rt["slot-7"] = {"proxy"}
    daemon._reload_config_set([(GATE["config_file"], "x")])
    assert drift_sandbox.state["popen"] == []
    assert daemon._SLOT_DRIFT["healed"] == 0
    assert daemon._SLOT_DRIFT["warn_only"] == 0


# ── 4. helper：互斥与限速 ────────────────────────────────────────────────

def test_restart_slot_process_respects_restart_lock(drift_sandbox):
    """restart_singbox 持锁期间绝不拉新进程（审查修订 #2）。"""
    assert daemon._restart_lock.acquire(blocking=False)
    try:
        assert daemon._restart_slot_process(WARP_SLOTS[1]) is False
        assert drift_sandbox.state["popen"] == []
    finally:
        daemon._restart_lock.release()


def test_restart_slot_process_per_slot_rate_limit(drift_sandbox):
    daemon._SLOT_DRIFT["slot_last_restart"]["slot-2"] = (
        daemon.time.time() - 60)
    assert daemon._restart_slot_process(WARP_SLOTS[1]) is False
    assert drift_sandbox.state["popen"] == []
