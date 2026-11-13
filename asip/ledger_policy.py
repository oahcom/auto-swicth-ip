"""台账生命周期策略 —— 判定侧、写入侧、影子比对、报表四者共用的单一事实源（D-1）。

纯函数模块：不 import daemon / config，所有配置值由调用方传参。
设计文档：docs/design-20261113-ledger-lifecycle.md §3。

三个核心函数：
  - block_release_ts(entry, ttl)      —— 该轮最晚解禁时刻
  - is_effectively_blocked(entry, now, ttl) —— 当前是否仍处于惩罚期
  - compute_expires_at(entry, event, now, retention_sec, ttl) —— 生命周期到期时刻

事件契约（§3.3）：只有「承载过成功流量」或「状态真的变了」才续命。
expires_at 单调不减（INV-3）。
"""
from __future__ import annotations

import math

# ---- 事件类型（§3.3 事件契约） ----
EVENT_SUCCESS = "success"        # 真实成功流量打点（last_ok_ts 更新）→ 续命
EVENT_NEW_ROUND = "new_round"    # 新一轮惩罚开启（403/429 新轮）→ 续命
EVENT_BAN_EXTEND = "ban_extend"  # 精确封禁延长（ban_until 更新）→ 续命
EVENT_SAME_ROUND = "same_round"  # 同一轮内重复标脏 → 不续命
EVENT_PROBE = "probe"            # 探测结论记账 → 不续命
EVENT_TOUCH = "touch"            # 被选中 / 换线登记 / 只读 → 不续命

_EXTENDING = frozenset({EVENT_SUCCESS, EVENT_NEW_ROUND, EVENT_BAN_EXTEND})


def is_extending(event: str) -> bool:
    """该事件是否续命。"""
    return event in _EXTENDING


def make_ttl(cfg_warp: dict) -> dict:
    """从 warp 配置字典提取三档 TTL（调用方传参，本模块不读全局配置）。"""
    return {
        "abuse403": float(cfg_warp.get("blocked_guard_sec", 21600)),
        "rate429": float(cfg_warp.get("rate429_guard_sec", 60)),
        "legacy": float(cfg_warp.get("blocked_ttl_sec", 86400)),
    }


# ---- 底层解析（与 daemon._as_ts / _warp_ban_until / _warp_block_stamps 同语义） ----

def _as_ts(value) -> float | None:
    """时间字段解析；损坏/NaN/Inf 一律当缺失。"""
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _block_ttl(kind, ttl: dict) -> float:
    """按烧掉该 IP 的错误类型给出惩罚期（秒）；kind 缺失/其它 → legacy。"""
    if kind == "abuse403":
        return ttl["abuse403"]
    if kind == "rate429":
        return ttl["rate429"]
    return ttl["legacy"]


def block_stamps(entry: dict) -> dict[str, float]:
    """各类惩罚期的起点（epoch 秒）。与 daemon._warp_block_stamps 同语义。"""
    stamps = entry.get("blocked_ts_by_kind")
    if isinstance(stamps, dict) and stamps:
        return {k: float(v) for k, v in stamps.items() if v is not None}
    if not entry.get("blocked"):
        return {}
    start = entry.get("blocked_ts")
    if start is None:
        start = entry.get("last_ok_ts")
    if start is None:
        start = entry.get("ts")
    if start is None:
        return {}
    kind = entry.get("blocked_kind")
    return {(kind if kind in ("abuse403", "rate429") else "legacy"): float(start)}


def parse_ban_until(entry: dict) -> tuple[float | None, str]:
    """解析 ban_until / ban_kind；坏值当作没有精确封禁。

    只有 ban_kind=="rate429" 且数值为有限正数时采信，否则作废。
    与 daemon._warp_ban_until 同语义。
    """
    until = _as_ts(entry.get("ban_until"))
    if until is None or until <= 0:
        return None, ""
    kind = entry.get("ban_kind")
    if kind != "rate429":
        return None, ""
    return until, kind


# ---- 三个核心函数 ----

def block_release_ts(entry: dict, ttl: dict) -> float:
    """给定条目返回「该轮最晚解禁时刻」（epoch 秒）。

    各类型 TTL 由 ttl 参数给出；ban_until 只在 ban_kind=="rate429" 且数值为
    有限正数时采信，且必须晚于该类型起点（否则按默认窗口保守处理）。
    多类型并存取最晚。未标记时返回 0。
    """
    stamps = block_stamps(entry)
    if not stamps:
        return 0.0
    bu, bk = parse_ban_until(entry)
    release = 0.0
    for k, t in stamps.items():
        if bu is not None and k == bk and bu > t:
            release = max(release, bu)
        else:
            release = max(release, t + _block_ttl(k, ttl))
    return release


def is_effectively_blocked(entry, now: float, ttl: dict) -> bool:
    """该条目当前是否仍处于惩罚期。与 daemon._warp_ip_is_blocked 逐字节同语义。"""
    if not isinstance(entry, dict) or not entry.get("blocked"):
        return False
    stamps = block_stamps(entry)
    if not stamps:
        # daemon._warp_block_remaining 对无起点的标记条目返回 legacy TTL > 0
        return True
    return block_release_ts(entry, ttl) > now


def compute_expires_at(entry: dict, event: str, now: float,
                       retention_sec: float, ttl: dict) -> float:
    """按事件契约计算 expires_at（INV-3 单调不减）。

    续命事件：max(现值, max(now, 该轮最晚解禁) + retention_sec)
    不续命事件：返回现值（不变）。
    条目尚无 expires_at（新建 / 迁移）时，无论事件类型都计算初始值。
    """
    current = _as_ts(entry.get("expires_at"))
    release = block_release_ts(entry, ttl)
    candidate = max(now, release) + retention_sec
    if current is None:
        # 新建条目或迁移补齐：计算初始生命周期
        return candidate
    if event not in _EXTENDING:
        return current
    return max(current, candidate)


def shadow_deviation(entry: dict, retention_sec: float, ttl: dict) -> float | None:
    """影子 expires_at 锚点与判定侧释放时刻的偏差（INV-1 监控）。

    偏差 = (expires_at - retention_sec) - block_release_ts(entry)。
    恒 ≥ 0 表示 INV-1 成立；负值即违例。返回 None 表示条目无 expires_at
    （影子阶段尚未写入），不参与统计。
    """
    exp = _as_ts(entry.get("expires_at"))
    if exp is None:
        return None
    release = block_release_ts(entry, ttl)
    return (exp - retention_sec) - release
