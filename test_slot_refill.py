#!/usr/bin/env python3
"""探测驱动的补闸：探测判 OK 的游离线必须被回填到坏槽位。

回归 2026-11-13 的生产故障：六个槽全 bad（no_candidate），同一时刻
/debug/state 里有 10 条新鲜 OK 的游离线。根因是旧补闸分支挂在
_probe_line_for_slots(tag) 里，而所有生产调用点传的都是刚 claim 进槽的
线，holder 恒不为空 —— 分支结构上不可达，日志「探测补闸」出现 0 次。
"""
import types

import pytest

import daemon


WARP_SLOTS = [
    {"name": "slot-%d" % i, "selector": "opencode-out-%d" % i,
     "port": 7890 if i == 1 else 7890 + i}
    for i in range(1, 7)
]
GATE = {"name": "slot-7", "selector": "opencode-out-7", "port": 7897,
        "kind": "proxy"}
ALL_SLOTS = WARP_SLOTS + [GATE]

FRESH = [({"tag": "WARP-OK-%d" % i}, "2606:ok:%d::1" % i)
         for i in range(1, 7)]


class _NoopThread:
    def __init__(self, target=None, args=(), kwargs=None, **kw):
        self.target = target
        self.args = args

    def start(self):
        pass

    def join(self, timeout=None):
        pass


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setattr(daemon.threading, "Thread", _NoopThread)
    monkeypatch.setattr(daemon, "consumer_slots", lambda: ALL_SLOTS)

    current = {s["name"]: ("WARP-OLD-%d" % i, "2606:old:%d::1" % i)
               for i, s in enumerate(WARP_SLOTS, 1)}
    current[GATE["name"]] = ("pool-node-1", None)
    monkeypatch.setattr(daemon, "_slot_current",
                        lambda slot: current.get(slot["name"], (None, None)))

    puts = []

    def fake_put(slot, tag):
        puts.append((slot["name"], tag))
        if slot["name"] in current:
            current[slot["name"]] = (tag, "2606:new:%s::1" % tag[-1])
        return True
    monkeypatch.setattr(daemon, "_slot_put", fake_put)

    main = {"tag": "WARP-MAIN"}
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: main["tag"])
    monkeypatch.setattr(daemon, "_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked",
                        lambda ip, now=None: False)

    fresh = list(FRESH)
    monkeypatch.setattr(daemon, "_warp_line_fresh_candidates",
                        lambda exclude, now: list(fresh))

    verdicts = {}

    def snapshot(tag, ip):
        return {"tag": tag, "ip": ip, "verdict": verdicts.get(tag, "OK"),
                "sec": 1.0, "cached": True, "source": "channel"}
    monkeypatch.setattr(daemon, "_channel_verdict_snapshot", snapshot)
    monkeypatch.setattr(daemon, "_probe_line_before_rotate",
                        lambda tag, ip, fresh=False: snapshot(tag, ip))
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    for n in ("_publish_clash_snapshot", "_mark_warp_ip_used",
              "_warp_line_selected", "_set_warp_line_ip", "_begin_segment",
              "_save_warp_line_state", "_record_line_ip_drift"):
        monkeypatch.setattr(daemon, n, lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_warp_member", lambda t: bool(t))

    # 运行时 selector 成员快照：默认六条候选线都在，个别用例会把它缩小，
    # 模拟「磁盘配置更新了但 sing-box 进程没重载」的配置漂移。
    members = {"WARP-OK-%d" % i for i in range(1, 7)}
    members.update("WARP-OLD-%d" % i for i in range(1, 7))

    def fake_selector(slot, path, method="GET", body=None, timeout=3,
                      silent=False):
        return 200, {"all": sorted(members)}
    monkeypatch.setattr(daemon, "slot_singbox_api", fake_selector)

    daemon._SLOT_HEALTH.clear()
    daemon._MAIN_SLOT_NAME["name"] = "slot-1"
    daemon._SLOT_REFILL_EVENT.clear()
    daemon._SLOT_REFILL_STATS.update({"rounds": 0, "filled": 0,
                                      "last_ts": 0.0,
                                      "skipped_not_member": 0})
    daemon._SLOT_REFILL_LAST_WARN["ts"] = 0.0
    for s in ALL_SLOTS:
        daemon._slot_mark(s["name"], "bad")

    return types.SimpleNamespace(current=current, puts=puts, main=main,
                                 fresh=fresh, verdicts=verdicts,
                                 members=members)


# ── 1. 探测器出的 OK 游离线要真的进槽 ──────────────────────────────────

def test_refill_fills_bad_slots_with_fresh_ok_lines(sandbox):
    filled = daemon._refill_slots_from_probe(reason="probe")
    # slot-1 是 serving，池闸不收 WARP 线 → 最多补 5 个常规槽。
    assert len(filled) == 5
    tags = [tag for tag, _name in filled]
    assert len(set(tags)) == len(tags), "同一条线不能塞进两个槽"
    assert all(tag.startswith("WARP-OK-") for tag in tags)
    assert all(name != "slot-7" and name != "slot-1"
               for _tag, name in filled)
    for _tag, name in filled:
        assert daemon._slot_health(name)["status"] == "ok"
        assert daemon._slot_health(name)["verdict"] == "OK"
    assert daemon._SLOT_REFILL_STATS["filled"] == 5


def test_refill_does_not_touch_healthy_or_serving_or_repairing(sandbox):
    daemon._slot_mark("slot-2", "ok")
    daemon._slot_mark("slot-3", "repairing")
    filled = daemon._refill_slots_from_probe(reason="probe")
    names = [name for _tag, name in filled]
    assert names == ["slot-4", "slot-5", "slot-6"]
    assert all(name not in ("slot-1", "slot-2", "slot-3", "slot-7")
               for name, _tag in sandbox.puts)
    assert daemon._slot_health("slot-3")["status"] == "repairing"


def test_refill_never_reuses_active_or_occupied_lines(sandbox):
    # 主出口 + 一个已 ok 的槽各占住一条候选线，这两条不能进其它槽。
    sandbox.main["tag"] = "WARP-OK-1"
    sandbox.current["slot-2"] = ("WARP-OK-2", "2606:ok:2::1")
    daemon._slot_mark("slot-2", "ok")
    filled = daemon._refill_slots_from_probe(reason="probe")
    tags = [tag for tag, _name in filled]
    assert "WARP-OK-1" not in tags
    assert "WARP-OK-2" not in tags
    assert all(name != "slot-2" for _tag, name in filled)


# ── 2. 池闸、非 OK 结论、PUT 失败 ───────────────────────────────────────

def test_refill_skips_proxy_gate_even_when_it_is_bad(sandbox):
    filled = daemon._refill_slots_from_probe(reason="probe")
    assert all(name != "slot-7" for name, _tag in sandbox.puts)
    assert all(name != "slot-7" for _tag, name in filled)
    assert daemon._slot_health("slot-7")["status"] == "bad"


def test_refill_skips_lines_without_ok_verdict(sandbox):
    sandbox.verdicts["WARP-OK-1"] = "quota"
    sandbox.verdicts["WARP-OK-2"] = "unknown"
    filled = daemon._refill_slots_from_probe(reason="probe")
    tags = [tag for tag, _name in filled]
    assert "WARP-OK-1" not in tags and "WARP-OK-2" not in tags
    assert len(filled) == 4


def test_refill_failed_put_tries_next_line_without_marking_ok(sandbox, monkeypatch):
    def flaky_put(slot, tag):
        sandbox.puts.append((slot["name"], tag))
        if tag == "WARP-OK-1":
            return False
        sandbox.current[slot["name"]] = (tag, "2606:new::1")
        return True
    monkeypatch.setattr(daemon, "_slot_put", flaky_put)
    filled = daemon._refill_slots_from_probe(reason="probe")
    assert ("slot-2", "WARP-OK-1") in sandbox.puts
    assert ("slot-2", "WARP-OK-2") in sandbox.puts
    assert filled[0] == ("WARP-OK-2", "slot-2")
    assert daemon._slot_health("slot-2")["status"] == "ok"


def test_refill_no_candidates_returns_empty(sandbox):
    sandbox.fresh.clear()
    assert daemon._refill_slots_from_probe(reason="probe") == []
    assert sandbox.puts == []


def test_refill_no_bad_slots_returns_empty(sandbox):
    for s in ALL_SLOTS:
        daemon._slot_mark(s["name"], "ok")
    assert daemon._refill_slots_from_probe(reason="probe") == []
    assert sandbox.puts == []


# ── 3. _probe_line_for_slots 的游离线路径仍要即时补闸 ───────────────────

def test_probe_line_free_tag_refills_immediately(sandbox):
    """兼容既有契约：游离线判 OK 时优先塞进坏闸，且池闸不被塞。"""
    sandbox.fresh.clear()
    sandbox.members.add("WARP-FREE")
    daemon._probe_line_for_slots("WARP-FREE", "2606:free::1")
    assert ("slot-2", "WARP-FREE") in sandbox.puts
    assert all(name != "slot-7" for name, _tag in sandbox.puts)
    assert daemon._slot_health("slot-2")["status"] == "ok"


# ── 5. 配置漂移：磁盘有新线、运行时 selector 没有 ──────────────────────

def test_refill_skips_lines_missing_from_runtime_selector(sandbox):
    """配置已更新但进程未重载时，PUT 这些线只会拿 400；必须跳过而不是空转。"""
    sandbox.members.intersection_update({"WARP-OK-1", "WARP-OK-2",
                                         "WARP-OK-3"})
    filled = daemon._refill_slots_from_probe(reason="probe")
    tags = [tag for tag, _name in filled]
    assert tags == ["WARP-OK-1", "WARP-OK-2", "WARP-OK-3"]
    assert daemon._SLOT_REFILL_STATS["skipped_not_member"] > 0
    assert all(tag in sandbox.members for _name, tag in sandbox.puts)


def test_refill_unreadable_selector_snapshot_falls_back(sandbox, monkeypatch):
    """Clash API 读不到成员时不过滤，退回原行为（宁可多试一次）。"""
    monkeypatch.setattr(daemon, "slot_singbox_api",
                        lambda slot, path, **kw: (0, None))
    filled = daemon._refill_slots_from_probe(reason="probe")
    assert len(filled) == 5
    assert daemon._SLOT_REFILL_STATS["skipped_not_member"] == 0


# ── 4. 探测写出 OK 结论必须唤醒补闸 ────────────────────────────────────

def test_probe_observation_ok_wakes_refill(sandbox):
    daemon._SLOT_REFILL_EVENT.clear()
    assert daemon._warp_line_record_observation(
        "WARP-OK-9", "2606:ok:9::1", "OK") is True
    assert daemon._SLOT_REFILL_EVENT.is_set()


def test_probe_observation_non_ok_does_not_wake_refill(sandbox):
    daemon._SLOT_REFILL_EVENT.clear()
    daemon._warp_line_record_observation(
        "WARP-BAD-9", "2606:bad:9::1", "quota")
    assert not daemon._SLOT_REFILL_EVENT.is_set()


def test_production_ok_wakes_refill(sandbox):
    daemon._SLOT_REFILL_EVENT.clear()
    daemon._warp_line_note_ok("WARP-OK-9", "2606:ok:9::1", 1.0)
    assert daemon._SLOT_REFILL_EVENT.is_set()


def test_refill_is_serialized_by_its_lock(sandbox):
    assert daemon._SLOT_REFILL_LOCK.acquire(blocking=False)
    try:
        assert daemon._refill_slots_from_probe(reason="probe") == []
        assert sandbox.puts == []
    finally:
        daemon._SLOT_REFILL_LOCK.release()


def test_refill_scan_interval_has_safe_default(sandbox):
    assert daemon._slot_refill_scan_sec() >= 2.0
