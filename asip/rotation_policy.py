#!/usr/bin/env python3
"""故障域 → 出口级动作判据（Batch A 强制策略可落地部分）。

设计约束：
- 默认 failure_domain_enforcement=False：判据返回「允许旧行为」，线上零变化。
- 开启后只有「带独立证据的 EXIT_IP」允许标脏 / 退役出口 / 切线。
- ACCOUNT / MODEL / REQUEST / CONTEXT_REJECTED / TRANSPORT 一律禁止。
- 已分类的 UNKNOWN 因证据不足一并禁止；enforcement 开启时未分类（domain=None
  或非法值）同样禁止，不回落到旧行为。
- 本模块是纯判据，不执行任何路由动作，也不读取运行时状态。
"""
from __future__ import annotations

from dataclasses import dataclass

from .failure_domain import FailureDomain

ENFORCEMENT_KEY = "failure_domain_enforcement"


@dataclass(frozen=True)
class RotationPolicy:
    """allow_* 为 True 表示「不干预旧逻辑」，不表示『一定要执行动作』。

    真正的动作仍由调用点原有的 reason / 健康快照等条件决定。这样开关关闭时
    daemon.py 的行为逐字不变。
    """

    allow_dirty: bool
    allow_switch: bool
    note: str


_LEGACY = RotationPolicy(True, True, "legacy")
_NON_EXIT = RotationPolicy(False, False, "non_exit_domain_blocked")
_UNCLASSIFIED = RotationPolicy(False, False, "unclassified_domain_blocked")
_CONFIRMED = RotationPolicy(True, True, "confirmed_exit_ip")


def _coerce_domain(domain):
    if domain is None:
        return None
    if isinstance(domain, FailureDomain):
        return domain
    if isinstance(domain, str):
        try:
            return FailureDomain(domain)
        except ValueError:
            return None
    return None


def rotation_policy(domain=None, enforcement=False) -> RotationPolicy:
    """统一判据：daemon 标脏点与切线点共用这一处实现。

    enforcement 关闭 → 旧行为（含 domain=None 与非法值）。
    enforcement 开启时 domain=None 或非法值 → _UNCLASSIFIED，禁止出口级动作。
    enforcement=False（默认）→ 旧行为。
    已分类的 UNKNOWN → 证据不足，禁止出口级动作（不会误判成出口故障）。
    """
    if not enforcement:
        return _LEGACY
    d = _coerce_domain(domain)
    if d is None:
        return _UNCLASSIFIED
    if d is FailureDomain.EXIT_IP:
        return _CONFIRMED
    return _NON_EXIT


def confirm_exit_domain(domain, evidence_id=None):
    """证据闸门：无独立证据的 EXIT_IP 降级为 UNKNOWN。

    9router 目前不传 exit_evidence_id，因此分类层永远不会产生可行动的
    EXIT_IP —— 这正是「不得让 UNKNOWN 误判成出口故障」的硬保证。
    """
    d = _coerce_domain(domain)
    if d is FailureDomain.EXIT_IP and not bool(evidence_id):
        return FailureDomain.UNKNOWN
    return d


def enforcement_enabled(cfg) -> bool:
    """从 CFG["warp"] 读开关，缺省 / 结构异常一律 False。"""
    if not isinstance(cfg, dict):
        return False
    warp = cfg.get("warp") or {}
    if not isinstance(warp, dict):
        return False
    return bool(warp.get(ENFORCEMENT_KEY, False))


def domain_trace_fields(domain=None, policy=None) -> dict:
    """回包附加字段：既有字段一概不动，reason 保持 9router 已知值。"""
    pol = policy if policy is not None else rotation_policy(domain)
    d = _coerce_domain(domain)
    return {"domain": d.value if d else None, "domain_note": pol.note}


@dataclass(frozen=True)
class RequestRouteContext:
    """请求开始时绑定的路由上下文（P0-3：不依赖 0.5s 一次的 selector 快照）。"""

    req_id: str
    slot: str
    warp_tag: str | None
    exit_ip: str | None
    started_at: float


def resolve_failure_tag(ctx: RequestRouteContext, selector_snapshot_tag=None):
    """失败请求必须引用原始绑定 tag；selector 快照只作观测，不覆盖。"""
    return ctx.warp_tag


def requires_revalidation(ok_observed_at, dirty_at, now, ttl_sec=0.0) -> bool:
    """陈旧 OK 证据复核：早于标脏时刻或超过 TTL 的 OK 不能直接采信。"""
    if ok_observed_at is None:
        return True
    if dirty_at is not None and ok_observed_at < dirty_at:
        return True
    # ttl_sec=0 表示不做时间衰减检查（只按「是否早于标脏」判定）
    if ttl_sec and ttl_sec > 0 and (now - ok_observed_at) > ttl_sec:
        return True
    return False


def retire_once(seen_req_ids, req_id, mark_dirty, domain=None,
                enforcement=False) -> bool:
    """每个 req_id 最多一次出口级退役（幂等保护）。"""
    if req_id in seen_req_ids:
        return False
    pol = rotation_policy(domain, enforcement=enforcement)
    if pol.allow_dirty:
        mark_dirty()
    seen_req_ids.add(req_id)
    return True


# 429 重放幂等：业务方确认幂等性之前，生产上限恒为 0。
REPLAY_IDEMPOTENCY_CONFIRMED = False




@dataclass(frozen=True)
class StaleOkDecision:
    needs_recheck: bool
    downrank: bool
    note: str


def stale_ok_decision(ok_observed_at, last_used_at, enforcement=False) -> StaleOkDecision:
    """OK 证据早于该线上次使用 = 陈旧。

    默认（enforcement=False）只观测、不降权；开启后强制复验并降权该候选。
    """
    stale = (ok_observed_at is None
             or (last_used_at is not None and last_used_at > ok_observed_at))
    if not stale:
        return StaleOkDecision(False, False, "ok_fresh")
    if not enforcement:
        return StaleOkDecision(True, False, "stale_ok_observed_only")
    return StaleOkDecision(True, True, "stale_ok_downranked")
