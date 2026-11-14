"""台账生命周期策略 —— 判定侧、写入侧、影子比对、报表四者共用的单一事实源（D-1）。

纯函数模块：不 import daemon / config，所有配置值由调用方传参。
设计文档：docs/design-20261113-ledger-lifecycle.md §3。

三个核心函数：
  - block_release_ts(entry, ttl)      —— 该轮最晚解禁时刻
  - is_effectively_blocked(entry, now, ttl) —— 当前是否仍处于惩罚期
  - compute_expires_at(entry, event, now, retention_sec, ttl) —— 生命周期到期时刻
  - backfill_expires_at(entry, retention_sec, ttl) —— 迁移补齐（缺字段条目）
  - cleanup_verdict(entry, now, retention_sec, ttl) —— P2 清理谓词 + 影子干跑

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

    注意：这是**不分人群**的原始口径。干净条目（未 blocked）没有「解禁时刻」，
    锚点来自 `now`/`last_ok_ts`，偏差会等于时间戳本身（实测 1.79e9 秒 ≈ 56 年），
    这是统计假象而不是违例。需要分人群的观测请用 `shadow_bucket`。
    """
    exp = _as_ts(entry.get("expires_at"))
    if exp is None:
        return None
    release = block_release_ts(entry, ttl)
    return (exp - retention_sec) - release


# ---- 迁移补齐与 P2 清理谓词 ----

def _entry_anchor(entry) -> float | None:
    """条目的「最近事件时刻」（ts / last_ok_ts 取最大）；无可用值返回 None。

    兼容旧 float 条目（值为最后使用时刻）。只认 >0 的有限数：0 等价于缺失，
    否则一个坏条目会被算成「1970 年碰过」而立刻满足删除条件。
    """
    if isinstance(entry, dict):
        vals = [_as_ts(entry.get("ts")), _as_ts(entry.get("last_ok_ts"))]
    else:
        vals = [_as_ts(entry)]
    vals = [v for v in vals if v is not None and v > 0]
    return max(vals) if vals else None


def backfill_expires_at(entry, retention_sec: float, ttl: dict) -> float | None:
    """迁移补齐：给缺 `expires_at` 的条目按「最近事件」推算一个到期时刻。

    `expires_at = max(最近事件时刻, 该轮最晚解禁时刻) + retention_sec`

    - 最近事件时刻取 `max(ts, last_ok_ts)`：`ts` 是最后一次被碰过（含探测记账），
      `last_ok_ts` 用于兜住「ts 缺失但有过成功流量」的畸形条目，保证 INV-2
      （≥ 成功时刻 + 复用窗口）不被跌破；
    - 不用 `now`：那等于凭空续一个完整窗口（且每次重启重新续，永不过期）；
    - 口径是「宁短勿长」：锚点来自真实事件，最长不超过「最近一次事件 + 窗口」；
    - 仍包含该轮最晚解禁时刻，保证 INV-1（绝不早于封禁解除）。

    返回 None 表示条目没有任何可用时间锚点、也没有解禁时刻（时间戳全坏）——
    调用方必须**保留**该条目并计数告警，不能当「已过期」删掉：里面可能带着
    `blocked` 标记，删了就会把仍在惩罚期的地址当「从未出现过」放回候选池。
    """
    anchor = _entry_anchor(entry)
    release = block_release_ts(entry, ttl) if isinstance(entry, dict) else 0.0
    if anchor is None and release <= 0:
        return None
    return max(anchor or 0.0, release) + retention_sec


def cleanup_verdict(entry, now: float, retention_sec: float,
                    ttl: dict) -> tuple[bool, str]:
    """P2 清理谓词（INV-4：唯一判据 `expires_at` < now）。

    返回 (是否删除, 判据来源)：

    - `"expires_at"`：条目已带 `expires_at`，按它判定；
    - `"backfilled"`：条目缺 `expires_at`（未迁移）→ 用 `backfill_expires_at`
      现场推算后判定。P2 下这类条目**先补齐再判**，不再「无字段 = 立即删」；
    - `"unmigrated"`：补齐也定不出值（时间戳全坏）→ 永不删除，由调用方计数告警。

    本函数是纯函数（不改 entry），清理与影子 would-delete 干跑共用同一份谓词，
    因此干跑结果就是 P2 实际会删的集合。
    """
    exp = _as_ts(entry.get("expires_at")) if isinstance(entry, dict) else None
    if exp is not None:
        return (exp < now, "expires_at")
    backfill = backfill_expires_at(entry, retention_sec, ttl)
    if backfill is None:
        return (False, "unmigrated")
    return (backfill < now, "backfilled")


def shadow_bucket(entry, retention_sec: float, ttl: dict) -> dict:
    """影子抽样（P0）：按人群分档计算不变量偏差。

    返回 {"bucket", "deviation", "violation", "exact"}：

    - `bucket == "clean"`（未 blocked）：检查 INV-2，
      `deviation = (expires_at - retention_sec) - last_ok_ts`；没有 `last_ok_ts`
      时 `deviation=None`（只计数，不参与偏差统计）。
    - `bucket == "blocked"`（曾被标记）：检查 INV-1，
      `deviation = (expires_at - retention_sec) - block_release_ts`。
    - `bucket == "unmigrated"`：缺 `expires_at`，不产生偏差（另由
      `missing_expires` 计数）。

    分人群的理由：干净条目没有解禁时刻，旧口径把它的锚点（时间戳）当成偏差，
    把 `deviation_max` 抬到 1.79e9 秒并稀释 `exact_match` —— 观测被污染后
    「violations == 0」就失去了证据力。
    """
    if not isinstance(entry, dict):
        return {"bucket": "unmigrated", "deviation": None,
                "violation": False, "exact": False}
    exp = _as_ts(entry.get("expires_at"))
    if exp is None:
        return {"bucket": "unmigrated", "deviation": None,
                "violation": False, "exact": False}
    anchor = exp - retention_sec
    if entry.get("blocked"):
        dev = anchor - block_release_ts(entry, ttl)
        return {"bucket": "blocked", "deviation": dev,
                "violation": dev < 0, "exact": dev == 0}
    last_ok = _as_ts(entry.get("last_ok_ts"))
    if last_ok is None:
        return {"bucket": "clean", "deviation": None,
                "violation": False, "exact": False}
    dev = anchor - last_ok
    return {"bucket": "clean", "deviation": dev,
            "violation": dev < 0, "exact": dev == 0}
