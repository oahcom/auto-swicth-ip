"""共享状态写入口：`.bad_nodes.json` 是**唯一**有两个写者的状态，收编在这里。

WHY 必须有这一层（而不是两边各写一遍）：`.bad_nodes.json` 是 daemon
（`_export_dead_nodes_locked`，append-only）与 cron（`self_evolve_checkpoint`，
覆盖自己归因的节点）共写的文件。今天两边各写一份 merge 逻辑，靠
「lock_path 恰好拼成同一个路径」保持一致 —— 这是**约定**，不是机制：任一侧
改了 `.with_suffix()` 的写法，锁就分家，事故形态是「cron 刚写进的坏节点被
daemon 的旧快照抹掉」（A7 共享锁事故）。收编后只有一个实现、一把锁，
两侧语义差异（append-only vs overwrite）是**参数**，不是两份代码。

为什么是 `.bad_nodes.json` 而不是全部状态：其余状态都是 daemon 单写者，
多封一层只会多一处可漂移的中间层（And 它们已经可以直接换成库）。这里保留
JSON 有明确理由 —— 这个文件**还被 proxy-global.js（Node 侧）读**，形态变更
要等 W3 一起换协议；现在只统一写口，不动格式。

`lock_path` 由本模块唯一计算，调用方不得自行拼接：这就是「一把锁」的机制形态。
"""
from __future__ import annotations

import os

from ..atomic_io import update_json

LOCK_SUFFIX = ".lock"


def lock_path_for(path: str) -> str:
    """共享锁路径（唯一计算点）。改这里=改所有写者的锁，不会再分家。"""
    return os.path.abspath(path) + LOCK_SUFFIX


def append_only_merge(entries: dict, *, expire_of, reason: str):
    """daemon 侧语义：只追加自己发现的节点，**不覆盖**他方已有条目。

    原作用（A7 三护栏之一）：cron/proxy-global 写进去的坏节点，daemon 的旧快照
    不能抹掉 —— 抹掉就等于把刚判死的节点放回池，下一发再烧一次。
    """
    def _merge(shared):
        if not isinstance(shared, dict):
            shared = {}
        for node in entries:
            if node not in shared:
                shared[node] = {"expire": expire_of(node), "reason": reason}
        return shared
    return _merge


def overwrite_merge(entries: dict, *, expire_of, reason: str):
    """cron 侧语义：它是这些节点的归因方，重写自己的条目（刷新 expire）。

    与 append-only 的差别是**有意的**：cron 每次归因都是对同一节点的新观测，
    expire 必须往后推，否则反复命中的节点会在旧 expire 到点时被释放。
    """
    def _merge(shared):
        if not isinstance(shared, dict):
            shared = {}
        for node in entries:
            shared[node] = {"expire": expire_of(node), "reason": reason}
        return shared
    return _merge


def update_bad_nodes(path: str, entries, *, expire_of, reason: str,
                     overwrite: bool) -> dict:
    """经共享锁做一次 read-modify-write；返回写后的表（便于调用方记账）。

    ponytail: 仍是全量读改写（文件 17KB / 202 条，不是瓶颈）。升级路径=等
    proxy-global.js 换协议时一起搬进库（那时它变成 SHARED kind，走
    `Store.update_rows`），本函数是那时唯一要改的地方。
    """
    nodes = list(entries or [])
    if not nodes:
        return {}
    merge = ((overwrite_merge if overwrite else append_only_merge)
             (set(nodes), expire_of=expire_of, reason=reason))
    return update_json(path, merge, default={}, lock_path=lock_path_for(path))
