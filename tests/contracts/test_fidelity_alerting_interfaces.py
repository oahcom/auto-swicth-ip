"""接口契约基线 · 4a 告警/健康模块的 P3 对接面（p6a 接口契约通知落钉）。

【防什么事故】P3（状态权威）对接 `asip.alerting.configure(journal=...)` 与
管理面「状态快照三件套」复用 `asip.health.build_health_snapshot` 时，事件
dict / 快照字段被静默改名、增删、换语义 → 事件表 schema 与消费方劈叉、
管理面快照与健康端点两份字段各说各话（元根因 #2「打算做 vs 做了」家族）；
「先落流水再扇出」被打乱 → 流水丢失或磁盘满告警风暴（alerting 模块
docstring 三条硬约束之二）。
【三分法】接口契约基线（新接口的对接判据，非附件 A/B/C 既有机制；接口
演进必须同步更新本契约，这正是钉它的目的）。其中「journal 先于 sink」
带事故锚定（硬约束），按 A 级对待。
【覆盖边界】模块行为面（sink 插拔/节流/隔离/轮转/快照状态机）已由
p6a 的 tests/test_alerting_4a.py 27 例覆盖，不重造；本文件只钉 **P3 对接
契约面**（wire schema 与注入点），并放进 tests/contracts/ 收集通道。
【live 复核 2026-11-14】事件字段 9 个（asip/alerting/__init__.py:247-257）；
快照行字段 7 个含 age_s（asip/health/__init__.py:113-123，p6a 通知漏列
age_s，此处以 live 为准）；状态优先级 degraded > unknown > ok（:101-139，
与通知「ok > unknown > degraded」是最好→最差的同一语义）。
"""
import time

import pytest

from asip import alerting
from asip import health as health_mod


EVENT_WIRE_KEYS = {"schema", "ts", "category", "severity", "message",
                   "source", "key", "suppressed", "fields"}
SNAPSHOT_ROW_KEYS = {"value", "ok", "ts", "age_s", "ttl", "stale", "detail"}
SNAPSHOT_TOP_KEYS = {"schema", "ts", "status", "checks", "missing",
                     "degraded_checks", "unknown_checks", "stale_checks"}


class _JournalSpy:
    """鸭子类型事件流水：P3 事件表 writer 只需同款 append(event: dict)。"""

    def __init__(self):
        self.events = []

    def append(self, event: dict) -> None:
        self.events.append(event)


def _fresh(spy=None):
    alerting.reset()
    return alerting.configure(journal=spy or _JournalSpy(), throttle_sec=0.0)


def test_configure_accepts_duck_typed_journal():
    """configure(journal=...) 接受任何 append(event: dict) 对象（P3 事件表注入点）。

    防什么事故：对接时把注入点改成具体 EventJournal 类型 → SQLite 事件表
    writer 接不进来，调用点被迫改（通知约定「调用点零改动」）。
    """
    spy = _JournalSpy()
    disp = _fresh(spy)
    disp.emit("ledger_anomaly", "x", source="contract-test")
    assert len(spy.events) == 1
    alerting.reset()


def test_event_wire_field_set_stable():
    """事件 dict 九字段即事件表 schema：缺一/多名=消费方查询口径坏。"""
    spy = _JournalSpy()
    disp = _fresh(spy)
    disp.emit("state_write_failed", "m", severity="error",
              key="k1", source="s1", path="/tmp/x", error="boom")
    ev = spy.events[0]
    assert set(ev) == EVENT_WIRE_KEYS
    assert ev["schema"] == alerting.SCHEMA_VERSION
    assert ev["fields"] == {"path": "/tmp/x", "error": "boom"}
    assert ev["suppressed"] == 0
    alerting.reset()


def test_journal_appended_before_sink_fanout():
    """事件先落流水再外发（硬约束二）：防流水丢失/磁盘满告警风暴。"""
    order = []

    class _J:
        def append(self, event):
            order.append("journal")

    class _S:
        name = "s"

        def emit(self, event):
            order.append("sink")

    alerting.reset()
    disp = alerting.configure(journal=_J(), throttle_sec=0.0)
    disp.add_sink(_S())
    disp.emit("thread_dead", "m")
    assert order == ["journal", "sink"]
    alerting.reset()


def test_health_snapshot_row_and_top_level_keys():
    """快照行 7 字段（含 age_s）与顶层 8 字段全集：管理面/端点共用不得裁剪。"""
    now = time.time()
    checks = {
        n: health_mod.Check(name=n, value=1, ok=True, ts=now, ttl=60.0)
        for n in health_mod.STANDARD_CHECKS
    }
    snap = health_mod.build_health_snapshot(checks, now=now)
    assert set(snap) == SNAPSHOT_TOP_KEYS
    for n in health_mod.STANDARD_CHECKS:
        assert set(snap["checks"][n]) == SNAPSHOT_ROW_KEYS
    assert snap["missing"] == []
    assert snap["status"] == health_mod.STATUS_OK


def test_health_status_priority_degraded_over_unknown():
    """状态优先级 degraded > unknown > ok；stale 默认降级（liveness≠freshness）。"""
    now = time.time()
    checks = {
        "health": health_mod.Check("health", ok=True, ts=now, ttl=60.0),
        "ledger_headroom": health_mod.Check("ledger_headroom", ok=None,
                                            ts=now, ttl=60.0),
        "queue_depth": health_mod.Check("queue_depth", ok=False,
                                        ts=now, ttl=60.0),
        "inflight_commands": health_mod.Check("inflight_commands", ok=True,
                                              ts=now, ttl=60.0),
    }
    snap = health_mod.build_health_snapshot(checks, now=now)
    assert snap["status"] == health_mod.STATUS_DEGRADED  # degraded 压过 unknown
    assert "queue_depth" in snap["degraded_checks"]
    assert "ledger_headroom" in snap["unknown_checks"]
    # stale 默认降级（stale_degrades=True 是默认语义）
    stale_checks = dict(checks)
    stale_checks["health"] = health_mod.Check("health", ok=True,
                                              ts=now - 100.0, ttl=10.0)
    snap2 = health_mod.build_health_snapshot(stale_checks, now=now)
    assert snap2["status"] == health_mod.STATUS_DEGRADED
    assert "health" in snap2["stale_checks"]


def test_health_missing_standard_field_is_unknown():
    """标准字段缺失 → unknown 且 missing 列出（缺一即不算标准快照）。"""
    now = time.time()
    checks = {
        "health": health_mod.Check("health", ok=True, ts=now, ttl=60.0),
        "ledger_headroom": health_mod.Check("ledger_headroom", ok=True,
                                            ts=now, ttl=60.0),
        "queue_depth": health_mod.Check("queue_depth", ok=True,
                                        ts=now, ttl=60.0),
    }
    snap = health_mod.build_health_snapshot(checks, now=now)
    assert snap["status"] == health_mod.STATUS_UNKNOWN
    assert snap["missing"] == [health_mod.CHECK_INFLIGHT_COMMANDS]
