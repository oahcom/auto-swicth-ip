"""状态权威（P3）：单库 + schema 不变量 + 影子对账 + 管理面三件套。

依赖方向：本包只依赖 stdlib 与 `asip.atomic_io` / `asip.ledger_policy` /
`asip.health`；**不 import daemon、不 import config**（路径与阈值全部由调用方
传参）。这条规则让本包可以在测试里独立跑、也让 daemon 侧的接线只剩「传参数」
一件事，不产生反向依赖。

中间态语义（阶段 2 内必须写在明处）：切读前有 5 条写路径共写一库 —— 写入不
损坏（库事务 + 进程锁），但**决策仍然竞争**（谁先换线、哪条台账为准）。仲裁
归 W3 决策核，本阶段不假装解决了它。

导出：Store / schema / shadow / mgmt。用法最短路径见 docs/state-store-contract.md。
"""
from . import mgmt, schema, shadow, writers
from .store import Store, StoreError, current_fstype, is_remote_fs

__all__ = ["Store", "StoreError", "schema", "shadow", "mgmt", "writers",
           "current_fstype", "is_remote_fs"]
