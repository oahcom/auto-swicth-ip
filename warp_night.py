#!/usr/bin/env python3
"""夜间刷新锁的策略层：窗口算术 + 白名单组装。

从 `daemon.py` 拆出来的原因不是行数，而是**测试面**：这里的每个函数都是
「输入时间 + 配置 → 布尔」，不需要隧道、不需要 Clash API、不需要起线程。
留在 daemon 里时，验证一个「窗口反了会怎样」的用例要先 import 一个 8000 行
的模块并伪造 `_warp()`、`_clash_snapshot()`、`active_warp_tag()` 三处状态。

daemon 侧保留同名门面（`_night_window` / `_night_active` / `_night_block_restart`
/ `_night_label`），因为它们要读全局 CFG、写日志、取当前出口 —— 那些是 IO，
不该混进策略里。策略函数一律**不读配置、不打日志、不碰状态**。

WHY 夜间要锁：上游免费层配额按出口 IP 计，次日 08:00 才重置。20:00→08:00
之间被烧的地址，重启只会换到另一个同样被烧的新 /128 —— 10-31 夜间实测
17036 次重启、0 次刷新成功，全部白烧，还把每条线在途连接掐断了一万多次。

**根本原因不在我们这边**：WARP 的出口 IP 池是全用户共享的，我们不用这些地址，
它们照样被别人烧进 ban 位。池子的健康度随时段变化 —— 08:00~16:00 相对好，
傍晚开始恶化，深夜只剩少数 IP 存活。所以夜里刷不出新地址不是"重启坏了"，
是池子干了。**别据深夜数据下"换 IP 无效"的结论**（见 docs/established-facts.md
第 0 条）。
"""
from __future__ import annotations

import time

DEFAULT_START = 20
DEFAULT_END = 8


def window(cfg: dict) -> dict:
    """从整份配置里取 `warp.night_window`（缺省给空 dict）。"""
    return (cfg.get("warp") or {}).get("night_window") or {}


def active(nw: dict, now: float | None = None) -> bool:
    """当前是否在夜间锁窗口。按主机本地时区算 —— 上游 08:00 重置也是本地时间。

    跨零点的窗口（20→8）不能写成 `start <= hour < end`：那会得到一个空区间，
    夜里一条也不锁，而白天全天在锁。
    """
    if not nw.get("enabled"):
        return False
    hour = time.localtime(now if now is not None else time.time()).tm_hour
    start, end = int(nw.get("start_hour", DEFAULT_START)), int(nw.get("end_hour", DEFAULT_END))
    return start <= hour < end if start < end else (hour >= start or hour < end)


def restart_allowed(nw: dict, now: float, tag: str, active_tag: str | None,
                    allow: set[str]) -> bool:
    """这条线此刻允不允许重启。True = 放行。

    只管重启，不管探测：探到 quota 照样记账，只是不花一次重启去换一个同样
    quota 的地址。当前出口例外 —— 它不走重启换线，而 `_restart_warp_inplace`
    是「所有备线都不新鲜」时的最后兜底，锁住它等于夜里彻底断流。
    """
    if not active(nw, now):
        return True
    if tag == active_tag:
        return True
    if tag in allow:
        return True
    return False


def label(nw: dict, now: float, allow_count: int) -> str:
    """健康行里的夜锁摘要：`关` / `20-8:3线` / `待20点/3线`。"""
    if not nw.get("enabled"):
        return "关"
    start = int(nw.get("start_hour", DEFAULT_START))
    end = int(nw.get("end_hour", DEFAULT_END))
    return f"{start}-{end}:{allow_count}线" if active(nw, now) else f"待{start}点/{allow_count}线"
