"""健康快照契约（状态权威管理面三件套之一）。

WHY 是「快照函数」而不是「一个端点」：状态权威（P3）与管理 API 共用同一份
字段和新鲜度语义，端点只是它的序列化外壳。契约定在这里，P3 接线时不必
重定义字段、也不必再写第二份状态汇总逻辑。

固定四字段（4a 约定，缺一即 ``missing``）：
  health            综合健康（ok / 槽位或探测结论）
  ledger_headroom   台账余量（行数/字节与保留上限的差额，越少越接近「停更」）
  queue_depth       队列深度（探测/IO 线程池积压）
  inflight_commands 在途命令（已发出未回报的 sing-box / 服务操作）

新鲜度（liveness ≠ freshness）：条目自带 ``ts`` 与 ``ttl``，``age > ttl``
即 ``stale``，默认把整体判成 ``degraded`` —— 「探针活着但证据过期」必须
在健康面上可见。完整的新鲜度契约（读方拒用超龄证据）在 4b。

本模块是纯函数：不读盘、不 import daemon/config，探针由调用方注入。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

SCHEMA_VERSION = 1

CHECK_HEALTH = "health"
CHECK_LEDGER_HEADROOM = "ledger_headroom"
CHECK_QUEUE_DEPTH = "queue_depth"
CHECK_INFLIGHT_COMMANDS = "inflight_commands"

# 状态权威管理面的约定字段；少一个就不算「标准快照」
STANDARD_CHECKS = (CHECK_HEALTH, CHECK_LEDGER_HEADROOM,
                   CHECK_QUEUE_DEPTH, CHECK_INFLIGHT_COMMANDS)

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Check:
    """一个检查项的结论 + 它的证据年龄。

    ``ok=None`` 表示「判不出来」（探针异常/数据缺失），不是 False ——
    把未知算成坏会制造假告警，算成好会掩盖失明。
    """

    name: str
    value: Any = None
    ok: bool | None = None
    ts: float | None = None     # 证据产生时刻（不是采集时刻）
    ttl: float | None = None    # 该证据的有效期；None = 不判过期
    detail: str = ""

    def age(self, now: float | None = None) -> float | None:
        if self.ts is None:
            return None
        return max(0.0, (time.time() if now is None else now) - self.ts)

    def stale(self, now: float | None = None) -> bool:
        age = self.age(now)
        return bool(self.ttl is not None and age is not None and age > self.ttl)


def _coerce(name: str, raw: Any, *, now: float,
            ttl: float | None) -> Check:
    """接受三种探针返回：Check / (value, ts) / 裸值。"""
    if isinstance(raw, Check):
        return Check(name=name, value=raw.value, ok=raw.ok, ts=raw.ts,
                     ttl=raw.ttl if raw.ttl is not None else ttl,
                     detail=raw.detail)
    if isinstance(raw, tuple) and len(raw) == 2:
        value, ts = raw
        return Check(name=name, value=value, ts=ts, ttl=ttl)
    return Check(name=name, value=raw, ts=now, ttl=ttl)


def collect(probes: Mapping[str, Callable[[], Any]], *,
            ttls: Mapping[str, float] | None = None,
            now: float | None = None) -> dict[str, Check]:
    """跑一遍探针；**单个探针异常不拖垮整张快照**，只把该项记为未知。"""
    moment = time.time() if now is None else now
    ttls = ttls or {}
    out: dict[str, Check] = {}
    for name, probe in probes.items():
        ttl = ttls.get(name)
        try:
            out[name] = _coerce(name, probe(), now=moment, ttl=ttl)
        except Exception as exc:
            out[name] = Check(name=name, ok=None, ts=moment, ttl=ttl,
                              detail="%s: %s" % (type(exc).__name__, exc))
    return out


def build_health_snapshot(checks: Mapping[str, Check], *,
                          now: float | None = None,
                          stale_degrades: bool = True) -> dict:
    """把检查项汇总成管理面快照（可直接 json.dumps）。

    状态优先级：degraded > unknown > ok。
      - 任一 ``ok is False`` → degraded
      - 任一 ``ok is None``（判不出来）或标准字段缺失 → unknown（已被
        degraded 覆盖时仍记 degraded）
      - 任一证据 stale 且 ``stale_degrades`` → degraded
    """
    moment = time.time() if now is None else now
    rows: dict[str, dict] = {}
    degraded: list[str] = []
    unknown: list[str] = []
    stale: list[str] = []
    for name, check in checks.items():
        age = check.age(moment)
        is_stale = check.stale(moment)
        rows[name] = {
            "value": check.value,
            "ok": check.ok,
            "ts": check.ts,
            "age_s": None if age is None else round(age, 3),
            "ttl": check.ttl,
            "stale": is_stale,
            "detail": check.detail,
        }
        if check.ok is False:
            degraded.append(name)
        elif check.ok is None:
            unknown.append(name)
        if is_stale:
            stale.append(name)
    if stale and stale_degrades:
        degraded.extend(n for n in stale if n not in degraded)
    missing = [n for n in STANDARD_CHECKS if n not in checks]

    if degraded:
        status = STATUS_DEGRADED
    elif unknown or missing:
        status = STATUS_UNKNOWN
    else:
        status = STATUS_OK

    return {
        "schema": SCHEMA_VERSION,
        "ts": round(moment, 3),
        "status": status,
        "checks": rows,
        "missing": missing,
        "degraded_checks": degraded,
        "unknown_checks": unknown,
        "stale_checks": stale,
    }


__all__ = [
    "SCHEMA_VERSION", "Check",
    "CHECK_HEALTH", "CHECK_LEDGER_HEADROOM", "CHECK_QUEUE_DEPTH",
    "CHECK_INFLIGHT_COMMANDS", "STANDARD_CHECKS",
    "STATUS_OK", "STATUS_DEGRADED", "STATUS_UNKNOWN",
    "collect", "build_health_snapshot",
]
