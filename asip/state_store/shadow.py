"""影子对账（T2b）：双写 + 周期 diff + 差异告警——分型。

WHY 必须分型而不是「所有状态一起对」：差异能不能归因，取决于**写者有几个**。
一个值在两处不一致，如果有两个写者，你无法判断是「投影写错」还是「另一个写者
刚写了一半」—— 那样的 diff 只会制造噪声，最后被人关掉（观测被污染后
`violations == 0` 就失去证据力，与 ledger_policy.shadow_bucket 分人群同一个
教训）。所以对账按写者类型分三档：

  DAEMON_SOLE   daemon 单写者状态（ip_used / line_state / api_stats /
                ledger_shadow / pool_history / node_ip_used / segment）
                → 可影子：双写 + 周期 diff + 差异告警。差异只可能来自投影
                  逻辑本身，归因清晰。
  SHARED        .bad_nodes.json：daemon 与 cron（self_evolve_checkpoint）两个
                写者，共用 flock 做 read-modify-write。**先收编 cron 写者**
                （改经同一写口）之前不影子 —— 否则差异无法归因。
  SIGNAL        信号文件（.dormant_probe_attempt / .daemon.lock 等）：随 W3
                换协议，不影子。
  EXTERNAL      9router requestDetails / usageHistory：外部只读源，只做**只读
                副本**（写进 docs 表留证，不参与 diff、不参与切读）。

差异=0 的判据落在 store.shadow 表里（每 kind 每 tick 一行），`shadow_window_clean`
是 T2c 切读门槛的机器判据 —— 不靠人眼看日志。
"""
from __future__ import annotations

import time

from .. import alerting as _alerting
from . import schema
from .store import Store

# 写者分型：kind → 类型。新增 kind 必须显式登记，默认不影子（fail-closed）。
DAEMON_SOLE = "daemon_sole"
SHARED = "shared"
SIGNAL = "signal"
EXTERNAL = "external"

WRITER_TYPES = {
    schema.KIND_IP_USED: DAEMON_SOLE,
    schema.KIND_LINE_STATE: DAEMON_SOLE,
    schema.KIND_API_STATS: DAEMON_SOLE,
    schema.KIND_LEDGER_SHADOW: DAEMON_SOLE,
    schema.KIND_POOL_HISTORY: DAEMON_SOLE,
    schema.KIND_NODE_IP_USED: DAEMON_SOLE,
    schema.KIND_SEGMENT: DAEMON_SOLE,
    schema.KIND_BAD_NODES: SHARED,
}

SHADOWABLE = frozenset(k for k, t in WRITER_TYPES.items() if t == DAEMON_SOLE)


def writer_type(kind: str) -> str:
    return WRITER_TYPES.get(kind, SIGNAL)


def diff_rows(kind: str, memory: dict, db: dict) -> dict:
    """逐行逐字段对比；返回可归因的差异（只报事实，不猜原因）。

    比较前两侧都过 `schema.filter_fields`：未声明字段不参与 diff（它们在库里
    也是原文保管，不参与读语义）。这样「差异=0」的含义精确等于「读语义一致」。
    """
    mem = {str(k): v for k, v in (memory or {}).items()}
    dbb = {str(k): v for k, v in (db or {}).items()}
    only_mem = sorted(set(mem) - set(dbb))
    only_db = sorted(set(dbb) - set(mem))
    changed = []
    for key in sorted(set(mem) & set(dbb)):
        a = mem[key] if isinstance(mem[key], dict) else {"_value": mem[key]}
        b = dbb[key] if isinstance(dbb[key], dict) else {"_value": dbb[key]}
        a, _ = schema.filter_fields(kind, a)
        b, _ = schema.filter_fields(kind, b)
        fields = sorted(set(a) | set(b))
        delta = {f: [a.get(f), b.get(f)] for f in fields
                 if a.get(f) != b.get(f)}
        if delta:
            changed.append({"key": key, "fields": delta})
    return {
        "kind": kind,
        "writer": writer_type(kind),
        "checked": len(set(mem) | set(dbb)),
        "only_memory": only_mem[:20],
        "only_db": only_db[:20],
        "changed": changed[:20],
        "diffs": len(only_mem) + len(only_db) + len(changed),
    }


def diff_doc(kind: str, memory, db) -> dict:
    """整份文档对比；`schema.DOC_VOLATILE` 里的落盘标记字段不参与。"""
    drop = schema.DOC_VOLATILE.get(kind) or frozenset()
    a = {k: v for k, v in (memory or {}).items() if k not in drop} \
        if isinstance(memory, dict) else memory
    b = {k: v for k, v in (db or {}).items() if k not in drop} \
        if isinstance(db, dict) else db
    equal = a == b
    return {"kind": kind, "writer": writer_type(kind),
            "checked": 0 if a is None and b is None else 1,
            "only_memory": [] if b is not None else [kind],
            "only_db": [] if a is not None else [kind],
            "changed": [] if equal else [{"key": kind, "fields": {"_value": [a, b]}}],
            "diffs": 0 if equal else 1}


class ShadowRunner:
    """周期对账器：拿「内存态快照提供者」与库对比，落痕 + 差异告警。

    不持有 daemon 引用（依赖方向单向）：调用方传 `snapshot()` 回调。
    回调返回 {kind: rows|doc}，形状与 `Store.snapshot()` 相同。
    """

    def __init__(self, store: Store, snapshot, *, alert=None,
                 interval_sec: float = 300.0, clock=time.time):
        self._store = store
        self._snapshot = snapshot
        self._alert = alert
        self._interval = float(interval_sec)
        self._clock = clock
        self._last = 0.0
        self.last_report: dict = {}

    def due(self) -> bool:
        return (self._clock() - self._last) >= self._interval

    def tick(self, *, force: bool = False) -> dict:
        now = self._clock()
        if not force and (now - self._last) < self._interval:
            return {"skipped": True}
        self._last = now
        try:
            memory = self._snapshot() or {}
        except Exception as exc:            # 快照失败=观测失明，要报不要静默
            self._emit("影子快照失败: %s" % exc, key="shadow_snapshot")
            return {"error": str(exc)}
        reports = []
        for kind in sorted(SHADOWABLE):
            if kind in schema.ROW_KINDS:
                if kind not in memory:
                    continue
                rep = diff_rows(kind, memory.get(kind) or {},
                                self._store.read_rows(kind))
                self._store.record_shadow(kind, rep["checked"], rep["diffs"],
                                          rep["changed"] + [
                                              {"only_memory": rep["only_memory"],
                                               "only_db": rep["only_db"]}])
            else:
                if kind not in memory:
                    continue
                rep = diff_doc(kind, memory.get(kind), self._store.read_doc(kind))
                self._store.record_shadow(kind, rep["checked"], rep["diffs"], [])
            reports.append(rep)
            if rep["diffs"]:
                # 类目用 observation_drift（「产物/生效/台账三面不一致」正是它），
                # 不新造类目：消费方按类目查询，新类目=查询口径劈叉。
                self._emit(
                    "影子差异 %s: %d 处（only_memory=%d only_db=%d changed=%d）"
                    % (kind, rep["diffs"], len(rep["only_memory"]),
                       len(rep["only_db"]), len(rep["changed"])),
                    key="shadow_diff:%s" % kind,
                    kind=kind,
                    only_memory=rep["only_memory"][:5],
                    only_db=rep["only_db"][:5],
                    changed=rep["changed"][:5])
        self.last_report = {"ts": now, "kinds": reports,
                            "total_diffs": sum(r["diffs"] for r in reports)}
        return self.last_report

    def _emit(self, message: str, *, key: str, **fields) -> None:
        if self._alert is None:
            return
        try:
            self._alert.emit(_alerting.CATEGORY_OBSERVATION_DRIFT, message,
                             severity="error", key=key,
                             source="state_store.shadow", **fields)
        except Exception:
            pass


def fresh_peer_snapshot(kind: str, memory: dict) -> dict:
    """把「内存里刚写的值」整形成可比形态（文档式 kind 直接返回 body）。"""
    if kind in schema.DOC_KINDS:
        return memory
    return {str(k): v for k, v in (memory or {}).items()}