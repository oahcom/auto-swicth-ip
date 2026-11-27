#!/usr/bin/env python3
"""故障域分类（P0 安全修复）。

请求级 429 / 账号配额 / 模型配额 / 请求上下文错误 **不是** 出口 IP 故障。
本模块只做分类，不执行任何路由动作；策略层决定是否退役出口。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProbeClass(Enum):
    OK = "ok"
    IP_BLOCKED = "ip_blocked"
    RATE_LIMITED = "rate_limited"
    ACCOUNT_QUOTA = "account_quota"
    MODEL_QUOTA = "model_quota"
    CONTEXT_REJECTED = "context_rejected"
    TRANSPORT_ERROR = "transport_error"
    UNKNOWN = "unknown"


class FailureDomain(Enum):
    REQUEST = "request"
    ACCOUNT = "account"
    MODEL = "model"
    CONTEXT_REJECTED = "context_rejected"
    EXIT_IP = "exit_ip"
    TRANSPORT = "transport"
    UNKNOWN = "unknown"


# 这些域不允许标脏出口、不允许触发出口退役。
NON_EXIT_DOMAINS = frozenset({
    FailureDomain.REQUEST,
    FailureDomain.ACCOUNT,
    FailureDomain.MODEL,
    FailureDomain.CONTEXT_REJECTED,
    FailureDomain.TRANSPORT,  # 必须先独立确认
})


@dataclass(frozen=True)
class FailureEvent:
    req_id: str
    slot: str
    exit_ip: str | None
    domain: FailureDomain
    reason: str
    observed_at: float
    evidence_id: str | None = None


def should_retire_exit(event: FailureEvent) -> bool:
    """只有带独立证据的出口级故障才允许退役出口。"""
    return event.domain == FailureDomain.EXIT_IP and bool(event.evidence_id)


def classify_response(status, body=None, headers=None) -> ProbeClass:
    """把一次上游响应映射到 ProbeClass。

    UNKNOWN 一律不解释为 IP 故障；403 不自动等于 IP 封锁；
    503 不自动等于出口断线；200 只说明本次探测请求成功。
    """
    headers = headers or {}
    text = str(body or "").lower()
    try:
        code = int(status)
    except (TypeError, ValueError):
        code = None
    # 信誉类拉黑信号：与 probe_warp.classify 的历史字符串语义一致（flagged）。
    # 这只是分类；IP_BLOCKED 要触发退役仍需独立的 evidence_confirmed。
    if "abusive traffic" in text or "freetiererror" in text:
        return ProbeClass.IP_BLOCKED
    # 401/403 属请求上下文失效：不自动等于 IP 封锁（保住既有测试契约）。
    if code in (401, 403):
        return ProbeClass.CONTEXT_REJECTED
    # 200 但没读到 data: ——本质是可用线，只是首个 chunk 比 read 窗口慢。
    if code == 200 or text.startswith("data:"):
        return ProbeClass.OK
    if code is None:
        return ProbeClass.UNKNOWN
    if code == 503:
        return ProbeClass.TRANSPORT_ERROR
    if code == 429:
        if "model quota" in text:
            return ProbeClass.MODEL_QUOTA
        if "freeusagelimiterror" in text or "daily limit" in text:
            return ProbeClass.ACCOUNT_QUOTA
        if "rate limit" in text:
            return ProbeClass.RATE_LIMITED
        return ProbeClass.UNKNOWN
    return ProbeClass.UNKNOWN


# Retry-After 到 UTC 零点的 FreeUsageLimitError 通常是小时级；
# 普通请求限流是秒级。1800s 只是初始分界，需影子期用真实分布校准。
LONG_RETRY_AFTER_SEC = 1800.0


def classify_rotate_request(req) -> FailureDomain:
    """从 9router /rotate 请求体分类故障域（影子阶段只读）。

    请求体可用字段：reason / retry_after_ms / req_id / egress_ip /
    request_started_at / egress_since / borrowed。没有响应体，
    因此长 Retry-After 只作为账号配额的**证据**，不是定论。
    """
    if not isinstance(req, dict):
        return FailureDomain.UNKNOWN
    raw = str(req.get("reason") or "").strip().lower()
    if not raw:
        return FailureDomain.UNKNOWN
    if "403" in raw or "abuse" in raw or "context" in raw:
        return FailureDomain.CONTEXT_REJECTED
    if "transport" in raw or "timeout" in raw or "stall" in raw:
        return FailureDomain.TRANSPORT
    if "model" in raw:
        return FailureDomain.MODEL
    if "quota" in raw or "account" in raw:
        return FailureDomain.ACCOUNT
    if "429" in raw or "rate" in raw:
        try:
            ms = float(req.get("retry_after_ms") or 0)
        except (TypeError, ValueError):
            ms = 0.0
        if ms >= LONG_RETRY_AFTER_SEC * 1000:
            return FailureDomain.ACCOUNT
        return FailureDomain.REQUEST
    return FailureDomain.UNKNOWN
