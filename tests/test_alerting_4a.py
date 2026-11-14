"""4a 告警/健康 · 故障注入演练（验收第 6 项）。

防什么事故：告警是「写盘失败被吞、线程静默死亡、日志滚失」三类静默失效的
唯一出口——出口自身必须被测试钉住，否则上线后第一次真实故障就是「告警没响」。

覆盖：
  1. AlertSink 可插拔演示（mock sink）
  2. 写盘失败 → state_write_failed 告警 + 事件落流水
  3. 台账缩水拒写 → ledger_anomaly 告警 + 事件落流水
  4. 线程死亡类目 → thread_dead 事件（4b 接线前先钉类目稳定性）
  5. 事件流水可查询（JSONL 逐行可解析）
  6. 通道故障隔离（单 sink 坏不影响其它 sink / 不影响业务路径）
  7. 节流防告警风暴
  8. 健康快照契约（四字段 + 新鲜度判定）
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, "/home/administrator/dkk-projects/auto-switch-ip")
import pytest

from asip import alerting
from asip import health as health_mod


# ── 公共夹具 ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_alerting():
    """每个用例后摘掉默认 dispatcher，防止状态泄漏。"""
    yield
    alerting.reset()


@pytest.fixture
def journal_path(tmp_path):
    return str(tmp_path / "events.jsonl")


@pytest.fixture
def dispatcher(journal_path):
    """带 JSONL 流水的 dispatcher（不配 log_sink，测试环境不污染 stdout）。"""
    return alerting.configure(journal_path=journal_path, throttle_sec=0.0)


class RecordingSink:
    """mock AlertSink：只记不发。"""

    def __init__(self, name="mock"):
        self.name = name
        self.events = []
        self.fail = False

    def emit(self, event):
        if self.fail:
            raise RuntimeError("mock sink 故障")
        self.events.append(event)


# ── 1. AlertSink 可插拔演示 ───────────────────────────────────────

class TestAlertSinkPluggable:
    def test_add_and_remove_sink(self, dispatcher):
        sink = RecordingSink("mock-a")
        dispatcher.add_sink(sink)
        assert len(dispatcher.sinks) == 1

        assert dispatcher.remove_sink("mock-a") is True
        assert len(dispatcher.sinks) == 0

    def test_multiple_sinks_all_receive(self, dispatcher):
        a = RecordingSink("a")
        b = RecordingSink("b")
        dispatcher.add_sink(a)
        dispatcher.add_sink(b)
        dispatcher.emit("thread_dead", "测试事件", source="test")

        assert len(a.events) == 1
        assert len(b.events) == 1
        assert a.events[0]["category"] == "thread_dead"

    def test_duplicate_sink_name_rejected(self, dispatcher):
        dispatcher.add_sink(RecordingSink("dup"))
        with pytest.raises(ValueError, match="重名"):
            dispatcher.add_sink(RecordingSink("dup"))

    def test_function_sink_wraps_callable(self, dispatcher):
        received = []
        dispatcher.add_sink(alerting.FunctionSink("fn", received.append))
        dispatcher.emit("thread_dead", "经由 FunctionSink", source="test")
        assert len(received) == 1
        assert received[0]["message"] == "经由 FunctionSink"


# ── 2. 写盘失败 → 告警 + 事件落流水 ───────────────────────────────

class TestWriteFailureAlert:
    def test_notify_write_failure_emits_alert(self, dispatcher, journal_path):
        exc = OSError("No space left on device")
        result = alerting.notify_write_failure(
            "/fake/path/.warp_ip_used.json", exc, source="test")
        assert result is True

        events = dispatcher._journal.read_recent()
        assert len(events) == 1
        ev = events[0]
        assert ev["category"] == "state_write_failed"
        assert ev["severity"] == "error"
        assert ev["fields"]["path"].endswith(".warp_ip_used.json")
        assert "No space left" in ev["fields"]["error"]

    def test_write_failure_key_distinguishes_paths(self, dispatcher):
        """磁盘满时每条台账都失败，按 path 分键才能看出哪些停更。"""
        alerting.notify_write_failure("/a/ledger1.json", OSError("full"))
        alerting.notify_write_failure("/a/ledger2.json", OSError("full"))
        events = dispatcher._journal.read_recent()
        assert len(events) == 2
        keys = {e["key"] for e in events}
        assert len(keys) == 2  # 不同 path → 不同节流键

    def test_emit_never_raises_on_journal_failure(self, journal_path):
        """流水写不进去时退 stderr，不向调用方抛（业务路径安全）。"""
        j = alerting.EventJournal(journal_path)
        disp = alerting.Dispatcher(j, throttle_sec=0.0)

        # monkey-patch append 模拟写入故障（如磁盘满）
        original_append = j.append
        def broken_append(event):
            raise OSError("No space left on device")
        j.append = broken_append

        # 不应抛异常
        result = disp.emit("state_write_failed", "test", source="test")
        assert result is True  # 事件已尝试投递（扇出仍继续）

        j.append = original_append


# ── 3. 台账缩水拒写 → ledger_anomaly 告警 ─────────────────────────

class TestLedgerAnomalyAlert:
    def test_shrink_refusal_emits_ledger_anomaly(self, dispatcher):
        """模拟 _save_warp_line_state 的缩水拒写分支：只验告警路径。"""
        dispatcher.emit(
            alerting.CATEGORY_LEDGER_ANOMALY,
            "拒绝覆盖状态表：盘上 50 条 > 内存 3 条",
            severity=alerting.SEVERITY_ERROR,
            key="ledger_shrink:/fake/.warp_line_state.json",
            source="daemon._save_warp_line_state",
            on_disk=50, in_memory=3)

        events = dispatcher._journal.read_recent()
        assert len(events) == 1
        ev = events[0]
        assert ev["category"] == "ledger_anomaly"
        assert ev["fields"]["on_disk"] == 50
        assert ev["fields"]["in_memory"] == 3


# ── 4. 线程死亡类目 ───────────────────────────────────────────────

class TestThreadDeadCategory:
    def test_thread_dead_category_stable(self, dispatcher):
        """4b 监督树接线前先钉类目字符串——消费方按它查询，不按 message。"""
        dispatcher.emit(
            alerting.CATEGORY_THREAD_DEAD,
            "探测线程 probe-warp-1 已死亡",
            severity=alerting.SEVERITY_CRITICAL,
            key="thread:probe-warp-1",
            source="test", thread_name="probe-warp-1")
        events = dispatcher._journal.read_recent()
        assert events[0]["category"] == "thread_dead"

    def test_all_known_categories_defined(self):
        assert alerting.KNOWN_CATEGORIES == frozenset({
            "thread_dead", "state_write_failed", "ledger_anomaly",
            "stale_evidence_consumed", "observation_drift"})


# ── 5. 事件流水可查询 ─────────────────────────────────────────────

class TestEventJournal:
    def test_events_queryable_by_category(self, dispatcher):
        """事件流水每行 JSON 可解析，可按 category 过滤（P3 事件表前身）。"""
        dispatcher.emit("state_write_failed", "A", source="t1")
        time.sleep(0.01)
        dispatcher.emit("ledger_anomaly", "B", source="t2")
        time.sleep(0.01)
        dispatcher.emit("thread_dead", "C", source="t3")

        all_events = dispatcher._journal.read_recent()
        assert len(all_events) == 3

        categories = [e["category"] for e in all_events]
        assert categories == ["state_write_failed", "ledger_anomaly", "thread_dead"]

    def test_read_recent_skips_corrupted_lines(self, journal_path):
        """一行坏 JSON 不让整份流水不可读。"""
        with open(journal_path, "w") as f:
            f.write(json.dumps({"category": "ok_event"}) + "\n")
            f.write("this is not json\n")
            f.write(json.dumps({"category": "another_ok"}) + "\n")

        j = alerting.EventJournal(journal_path)
        events = j.read_recent()
        assert len(events) == 2
        assert events[0]["category"] == "ok_event"

    def test_journal_rotate_keeps_one_generation(self, tmp_path):
        path = str(tmp_path / "big.jsonl")
        j = alerting.EventJournal(path, max_bytes=200)
        for i in range(10):
            j.append({"n": i, "pad": "x" * 30})
        # 轮转后原文件重新开始，旧文件在 .1
        assert os.path.exists(path)
        assert os.path.getsize(path) < 500  # 不会无限膨胀


# ── 6. 通道故障隔离 ───────────────────────────────────────────────

class TestFaultIsolation:
    def test_bad_sink_does_not_block_good_sink(self, dispatcher):
        good = RecordingSink("good")
        bad = RecordingSink("bad")
        bad.fail = True
        dispatcher.add_sink(good)
        dispatcher.add_sink(bad)

        result = dispatcher.emit("thread_dead", "故障隔离测试", source="test")
        assert result is True
        assert len(good.events) == 1  # 好通道正常收到

    def test_bad_sink_does_not_block_journal(self, dispatcher, journal_path):
        bad = RecordingSink("bad")
        bad.fail = True
        dispatcher.add_sink(bad)
        dispatcher.emit("thread_dead", "流水优先", source="test")

        events = dispatcher._journal.read_recent()
        assert len(events) == 1  # 流水不受通道故障影响

    def test_emit_without_config_is_noop(self):
        alerting.reset()
        result = alerting.emit("thread_dead", "未配置时静默")
        assert result is False


# ── 7. 节流防告警风暴 ─────────────────────────────────────────────

class TestThrottling:
    def test_throttle_suppresses_duplicates(self, journal_path):
        disp = alerting.configure(journal_path=journal_path, throttle_sec=60.0)
        for i in range(5):
            disp.emit("state_write_failed", "磁盘满 %d" % i,
                      key="write_failed:/a.json", source="test")

        events = disp._journal.read_recent()
        assert len(events) == 1
        assert events[0]["suppressed"] == 0

    def test_throttle_different_keys_pass(self, journal_path):
        disp = alerting.configure(journal_path=journal_path, throttle_sec=60.0)
        disp.emit("state_write_failed", "A", key="write_failed:/a.json")
        disp.emit("state_write_failed", "B", key="write_failed:/b.json")
        events = disp._journal.read_recent()
        assert len(events) == 2

    def test_suppressed_count_accumulates(self, journal_path):
        """窗口内被压掉的条数在下一条放行时带上。"""
        disp = alerting.configure(journal_path=journal_path, throttle_sec=60.0)
        disp.emit("state_write_failed", "first", key="k1")
        disp.emit("state_write_failed", "suppressed1", key="k1")
        disp.emit("state_write_failed", "suppressed2", key="k1")
        events = disp._journal.read_recent()
        assert len(events) == 1  # 只放行了第一条


# ── 8. 健康快照契约 ───────────────────────────────────────────────

class TestHealthSnapshot:
    def _make_checks(self, now=None, **overrides):
        moment = now or time.time()
        base = {
            "health": health_mod.Check(
                name="health", value={"active_ip": "1.2.3.4"},
                ok=True, ts=moment),
            "ledger_headroom": health_mod.Check(
                name="ledger_headroom", value={"bytes": 1000, "cap": 9999},
                ok=True, ts=moment),
            "queue_depth": health_mod.Check(
                name="queue_depth", value={"depth": 2, "capacity": 10},
                ok=True, ts=moment),
            "inflight_commands": health_mod.Check(
                name="inflight_commands", value={"count": 0},
                ok=True, ts=moment),
        }
        base.update(overrides)
        return base

    def test_all_ok_returns_ok(self):
        snap = health_mod.build_health_snapshot(self._make_checks())
        assert snap["status"] == "ok"
        assert snap["missing"] == []

    def test_degraded_when_check_fails(self):
        checks = self._make_checks(
            health=health_mod.Check(
                name="health", value=None, ok=False, ts=time.time(),
                detail="last_egress_error=429"))
        snap = health_mod.build_health_snapshot(checks)
        assert snap["status"] == "degraded"
        assert "health" in snap["degraded_checks"]

    def test_unknown_when_check_undecidable(self):
        checks = self._make_checks(
            health=health_mod.Check(
                name="health", ok=None, ts=time.time(),
                detail="active ip not resolved yet"))
        snap = health_mod.build_health_snapshot(checks)
        assert snap["status"] == "unknown"
        assert "health" in snap["unknown_checks"]

    def test_stale_evidence_degrades(self):
        """liveness != freshness：探针活着但证据过期 → degraded。"""
        old_ts = time.time() - 3600
        checks = self._make_checks(
            health=health_mod.Check(
                name="health", ok=True, ts=old_ts, ttl=300))
        snap = health_mod.build_health_snapshot(checks)
        assert snap["status"] == "degraded"
        assert "health" in snap["stale_checks"]

    def test_missing_standard_field_returns_unknown(self):
        checks = self._make_checks()
        del checks["inflight_commands"]
        snap = health_mod.build_health_snapshot(checks)
        assert snap["status"] == "unknown"
        assert "inflight_commands" in snap["missing"]

    def test_collect_catches_probe_exception(self):
        def bad_probe():
            raise RuntimeError("探针炸了")

        checks = health_mod.collect({"health": bad_probe})
        assert checks["health"].ok is None
        assert "RuntimeError" in checks["health"].detail

    def test_collect_accepts_check_tuple_and_raw(self):
        now = time.time()

        def raw_probe():
            return 42

        def tuple_probe():
            return ("val", now - 10)

        def check_probe():
            return health_mod.Check(name="inner", ok=True, ts=now)

        checks = health_mod.collect({
            "a": raw_probe, "b": tuple_probe, "c": check_probe})
        assert checks["a"].value == 42
        assert checks["b"].value == "val"
        assert checks["c"].ok is True

    def test_snapshot_four_standard_fields_contract(self):
        """P3 状态权威管理面接线依赖的字段契约：四标准字段 + 行 7 字段
        （value/ok/ts/age_s/ttl/stale/detail，P3 序列化不得裁剪 age_s）
        + 顶层 8 字段 + 状态覆盖优先级 degraded > unknown > ok。"""
        snap = health_mod.build_health_snapshot(self._make_checks())
        assert set(health_mod.STANDARD_CHECKS) == {
            "health", "ledger_headroom", "queue_depth", "inflight_commands"}
        for field in health_mod.STANDARD_CHECKS:
            assert field in snap["checks"]
            row = snap["checks"][field]
            for row_key in ("value", "ok", "ts", "age_s", "ttl", "stale", "detail"):
                assert row_key in row, "快照行缺字段 %s" % row_key
        for top_key in ("schema", "ts", "status", "checks", "missing",
                        "degraded_checks", "unknown_checks", "stale_checks"):
            assert top_key in snap, "快照顶层缺字段 %s" % top_key
        assert snap["status"] in ("ok", "degraded", "unknown")
