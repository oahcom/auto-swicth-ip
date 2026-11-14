"""daemon ↔ 状态库接线（T2b 影子双写）。**只做搬运，不含任何判据。**

WHY 单列一层而不是把双写散进 `_save_*`：双写必须满足三条硬约束，散着写
必然在某处漏掉一条 ——

  ① **绝不改变调用链语义**：库写失败只告警，不抛、不 return、不影响落盘结果。
     影子阶段库是观察对象，它坏了不能反过来影响生产（顺序：JSON 先成功，
     库后尽力）。
  ② **护栏复用，不复制**：空表/缩水由 `Store.replace_rows` 按 schema 不变量
     判定（INV-EMPTY / INV-SHRINK），这里不写第二份阈值。
  ③ **差异可归因**：只影子 daemon 单写者的 kind（见 shadow.WRITER_TYPES），
     共享写者（bad_nodes）在收编 cron 之后才可能进 diff。

**库必须先由迁移器建出来**（`deploy/migrations/`）：daemon 启动时自己建库会让
「库的 schema 版本」变隐式（谁先启动谁定），与迁移器的存在意义直接冲突。
所以这里只「打不开就跳过」，从不建库。

阶段中间态（必须写在明处）：本模块让库与 JSON **同时**被写，写入不损坏，
但决策仍以 JSON/内存为准 —— 切读是 T2c 的事，且以「影子差异=0 一个观察窗」
为门槛。
"""
from __future__ import annotations

import os

from .. import alerting as _alerting
from . import schema
from . import shadow as shadow_mod
from .store import Store, StoreError


def open_store(cfg: dict) -> Store | None:
    """按配置打开状态库；任何异常都退化成「没有库」，不阻断启动。

    `shadow` 关 = 完全不碰库（上线默认零行为变化）；库文件不存在 = 还没
    跑迁移器，同样不碰 —— 这两种情况下 daemon 的行为与改造前逐字相同。
    """
    sc = (cfg or {}).get("state_store") or {}
    if not sc.get("shadow"):
        return None
    path = sc.get("db_path")
    if not path or not os.path.exists(path):
        return None
    try:
        return Store(path)
    except (StoreError, OSError):
        return None


def mirror_rows(store: Store | None, kind: str, mapping: dict, *,
                allow_empty: bool = False, alert=None) -> dict:
    """把一次全量落盘的 rows 镜像进库；返回 store 的结论（调用方只用于记账）。

    库侧拒写（空表/缩水）**不改变**调用方已完成的 JSON 落盘 —— 那正是影子
    阶段要观测的差异来源之一，所以这里把它当**证据**上报，不当异常。
    """
    if store is None:
        return {"skipped": True}
    try:
        out = store.replace_rows(kind, mapping, allow_empty=allow_empty)
    except Exception as exc:
        _emit(alert, _alerting.CATEGORY_STATE_WRITE_FAILED,
              "镜像 %s 失败: %s" % (kind, exc), kind=kind)
        return {"error": str(exc)}
    if out.get("refused"):
        # 注意：这里报的是「库拒写」不是「数据不一致」。两者归因完全不同，
        # 混在一起会让真正的投影 bug 淹没在护栏噪声里。类目复用
        # ledger_anomaly —— 拒写正是「台账这一层出现异常写入意图」。
        _emit(alert, _alerting.CATEGORY_LEDGER_ANOMALY,
              "库拒写 %s（%s）：%s" % (kind, out.get("refused"),
                                      out.get("detail")),
              kind=kind, invariant=out.get("refused"), ctx=out.get("ctx"))
    return out


def mirror_doc(store: Store | None, kind: str, body, *, alert=None) -> dict:
    """文档式 kind（api_stats / ledger_shadow / pool_history / segment）。"""
    if store is None:
        return {"skipped": True}
    try:
        store.write_doc(kind, body)
        return {"written": True}
    except Exception as exc:
        _emit(alert, _alerting.CATEGORY_STATE_WRITE_FAILED,
              "镜像 %s 失败: %s" % (kind, exc), kind=kind)
        return {"error": str(exc)}


def snapshot_of(rows: dict, docs: dict) -> dict:
    """装配 `ShadowRunner` 需要的 {kind: rows|doc} 快照（形状同 Store.snapshot）。"""
    snap = {k: v for k, v in (rows or {}).items() if k in schema.ROW_KINDS}
    for k, v in (docs or {}).items():
        if k in schema.DOC_KINDS:
            snap[k] = v
    return snap


def make_runner(store: Store | None, providers, *, alert=None,
                interval_sec: float = 300.0):
    """构造对账器；库没开就返回 None（调用方据此跳过整个 tick）。"""
    if store is None:
        return None
    return shadow_mod.ShadowRunner(store, providers, alert=alert,
                                   interval_sec=interval_sec)


def _emit(alert, category: str, message: str, **fields) -> None:
    """告警不得反向影响镜像：任何异常都吞掉（与 alerting 的既有约束同源）。

    `alert` 默认取 alerting 的进程默认 dispatcher（daemon 已 configure）；
    未配置时是 no-op —— 测试与手工跑不会因此落生产文件。
    """
    target = alert if alert is not None else _alerting
    try:
        target.emit(category, message, severity="error",
                    source="state_store.bridge", **fields)
    except Exception:
        pass