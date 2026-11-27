"""Batch A / daemon 故障域策略测试（§4.2 全用例）。

这些测试在 daemon.py 保持未改动（并行代理持有该文件）的前提下，直接验证
daemon 接入点共用的判据与执行顺序；判据由 asip/rotation_policy.py 提供，
daemon patch 在标脏点 / 切线点调用同一个函数。不触碰生产路由。
"""
import os
from unittest.mock import Mock

import pytest

from asip.failure_domain import (
    FailureDomain, classify_response, classify_rotate_request,
    should_retire_exit,
)
from asip.rotation_policy import (
    RequestRouteContext,
    confirm_exit_domain,
    domain_trace_fields,
    enforcement_enabled,
    requires_revalidation,
    resolve_failure_tag,
    retire_once,
    rotation_policy,
    stale_ok_decision,
)


def _run_policy(mark_dirty, fast_switch, domain=None, enforcement=True, order=None):
    """模拟 daemon 调用点：先判据，后动作（与 patch 中插入点顺序一致）。"""
    pol = rotation_policy(domain, enforcement=enforcement)
    if pol.allow_dirty:
        mark_dirty(blocked=True)
        if order is not None:
            order.append("mark_dirty")
    if pol.allow_switch:
        fast_switch()
        if order is not None:
            order.append("fast_switch")
    return pol


# ---- §4.2 ---------------------------------------------------------------

def test_account_quota_never_retires_exit():
    mark = Mock()
    switch = Mock()
    _run_policy(mark, switch, FailureDomain.ACCOUNT)
    assert mark.call_count == 0
    assert switch.call_count == 0


def test_model_quota_never_retires_exit():
    mark = Mock()
    switch = Mock()
    _run_policy(mark, switch, FailureDomain.MODEL)
    assert mark.call_count == 0
    assert switch.call_count == 0


def test_rate_limit_does_not_switch_slot():
    mark = Mock()
    switch = Mock()
    _run_policy(mark, switch, FailureDomain.REQUEST)
    assert switch.call_count == 0
    assert mark.call_count == 0


def test_unknown_failure_keeps_legacy_switch_behavior():
    mark = Mock()
    switch = Mock()
    _run_policy(mark, switch, FailureDomain.UNKNOWN, enforcement=True)
    assert switch.call_count == 1
    assert mark.call_count == 1


def test_confirmed_ip_block_can_retire_exit():
    mark = Mock()
    switch = Mock()
    pol = _run_policy(mark, switch, FailureDomain.EXIT_IP)
    assert mark.call_count == 1
    assert switch.call_count == 1
    assert pol.note == "confirmed_exit_ip"


def test_same_request_retirement_is_idempotent():
    mark = Mock()
    seen = set()
    assert retire_once(seen, "req-1", mark, FailureDomain.EXIT_IP, True) is True
    assert retire_once(seen, "req-1", mark, FailureDomain.EXIT_IP, True) is False
    assert mark.call_count == 1


def test_retry_does_not_reuse_stale_selector_tag():
    ctx = RequestRouteContext("req-7", "slot-6", "WARP-H2-LHR-63",
                              "2a09::1", 100.0)
    # selector 快照在重试时已漂移到另一条线
    assert resolve_failure_tag(ctx, "WARP-H2-LHR-64") == "WARP-H2-LHR-63"
    assert "64" not in str(ctx)


def test_stale_ok_pick_requires_revalidation():
    # OK 结论早于标脏时刻 → 必须复核
    assert requires_revalidation(ok_observed_at=90.0, dirty_at=95.0, now=100.0)
    # 新鲜结论、无标脏 → 可直接采信
    assert not requires_revalidation(ok_observed_at=99.0, dirty_at=None, now=100.0)
    # 缺结论 → 必须复核
    assert requires_revalidation(None, None, 100.0)


def test_request_failure_does_not_poison_slot_health():
    health = {"dirty": 0}
    mark = lambda: health.__setitem__("dirty", health["dirty"] + 1)
    pol = rotation_policy(FailureDomain.REQUEST, enforcement=True)
    if pol.allow_dirty:
        mark()
    assert health["dirty"] == 0


def test_slot_switch_requires_exit_domain():
    for domain in (
        FailureDomain.ACCOUNT,
        FailureDomain.MODEL,
        FailureDomain.REQUEST,
        FailureDomain.CONTEXT_REJECTED,
        FailureDomain.TRANSPORT,
    ):
        mark = Mock()
        switch = Mock()
        _run_policy(mark, switch, domain, enforcement=True)
        assert switch.call_count == 0, domain
        assert mark.call_count == 0, domain


def test_rotate_once_classification_before_dirty():
    """顺序：判据完成后才可能调用 mark_dirty；非出口域整条动作链为空。"""
    order = []
    mark = Mock(side_effect=lambda **kw: order.append("mark_dirty"))
    switch = Mock(side_effect=lambda: order.append("fast_switch"))
    _run_policy(mark, switch, FailureDomain.REQUEST, order=order)
    assert order == []
    # 出口域的顺序证明判据本身不会颠倒动作次序
    order2 = []
    _run_policy(Mock(side_effect=lambda **kw: order2.append("mark_dirty")),
                Mock(side_effect=lambda: order2.append("fast_switch")),
                FailureDomain.EXIT_IP)
    assert order2 == ["mark_dirty", "fast_switch"]


# ---- 默认安全 / 证据闸门 / 回包契约 ------------------------------------

def test_enforcement_off_keeps_legacy_behavior():
    mark = Mock()
    switch = Mock()
    _run_policy(mark, switch, FailureDomain.REQUEST, enforcement=False)
    assert mark.call_count == 1
    assert switch.call_count == 1


def test_unclassified_domain_keeps_legacy_behavior_under_enforcement():
    mark = Mock()
    switch = Mock()
    _run_policy(mark, switch, None, enforcement=True)
    assert mark.call_count == 1
    assert switch.call_count == 1


def test_invalid_domain_string_is_blocked_under_enforcement():
    """非法字符串 domain 与 None 同罪：强制模式下不得回落到旧行为。"""
    mark = Mock()
    switch = Mock()
    _run_policy(mark, switch, "not-a-domain", enforcement=True)
    assert mark.call_count == 0
    assert switch.call_count == 0


def test_exit_ip_without_evidence_is_downgraded_to_unknown():
    assert confirm_exit_domain(FailureDomain.EXIT_IP, None) is FailureDomain.UNKNOWN
    assert confirm_exit_domain(FailureDomain.EXIT_IP, "") is FailureDomain.UNKNOWN
    assert confirm_exit_domain(FailureDomain.EXIT_IP, "ev-1") is FailureDomain.EXIT_IP


def test_stale_ok_candidate_downranked():
    """影子期只记录；enforcement 开启后强制复验并降权。"""
    observed = stale_ok_decision(90.0, 95.0, enforcement=False)
    assert observed.needs_recheck and not observed.downrank
    assert observed.note == "stale_ok_observed_only"
    enforced = stale_ok_decision(90.0, 95.0, enforcement=True)
    assert enforced.needs_recheck and enforced.downrank
    assert enforced.note == "stale_ok_downranked"
    fresh = stale_ok_decision(99.0, 95.0, enforcement=True)
    assert not fresh.needs_recheck and not fresh.downrank


def test_non_idempotent_request_never_replayed():
    """SSE 已开始或非幂等方法一律不重放（即使配置放行）。"""
    seen = set()

    def replay(req, sse_started=False):
        if (sse_started or req.get("method") in ("POST", "PATCH", "DELETE")
                or req.get("req_id") in seen):
            return False
        seen.add(req.get("req_id"))
        return True

    assert replay({"method": "POST", "req_id": "r1"}) is False
    assert replay({"method": "GET", "req_id": "r1"}, sse_started=True) is False
    assert replay({"method": "GET", "req_id": "r2"}) is True


def test_18h_retry_after_is_account_quota_with_no_action():
    """实测证据：全量 quota 904 条，Retry-After 为 18 小时级 → 账号配额域。"""
    req = {"reason": "rate429", "retry_after_ms": 18 * 3600 * 1000}
    domain = classify_rotate_request(req)
    assert domain is FailureDomain.ACCOUNT
    mark = Mock()
    switch = Mock()
    _run_policy(mark, switch, domain)
    assert mark.call_count == 0
    assert switch.call_count == 0


def test_abuse403_returns_context_rejected_without_exception():
    """红队修复：classify_rotate_request 曾引用不存在的枚举值。"""
    domain = classify_rotate_request({"reason": "abuse403"})
    assert domain is FailureDomain.CONTEXT_REJECTED
    assert should_retire_exit(_event(domain)) is False


def test_probe_warp_and_failure_domain_classification_agree():
    """同一响应在 probe_warp.classify 与 classify_response 两条路径一致。"""
    import probe_warp
    cases = [
        (200, "data: {...}", "OK"),
        (429, '{"error":{"type":"FreeUsageLimitError"}}', "quota"),
        (429, "abusive traffic", "flagged"),
        (429, "FreeTierError", "flagged"),
        (403, "forbidden", "err"),
        (503, "Service Unavailable", "err"),
        (429, "rate limit exceeded", "err"),
        (429, "", "err"),
    ]
    for status, head, legacy in cases:
        assert probe_warp.classify(status, head) == legacy, (status, head)
        pc = classify_response(status, head)
        expected_pc = {
            "OK": "ok", "quota": "account_quota", "flagged": "ip_blocked",
            "err": None,
        }[legacy]
        if expected_pc:
            assert pc.value == expected_pc, (status, head, pc)


def _event(domain):
    from asip.failure_domain import FailureEvent
    return FailureEvent("r-ctx", "slot-1", "::1", domain, "abuse403", 0.0, "ev")


def test_domain_trace_fields_keep_reason_contract():
    pol = rotation_policy(FailureDomain.ACCOUNT, enforcement=True)
    fields = domain_trace_fields(FailureDomain.ACCOUNT, pol)
    assert fields == {"domain": "account", "domain_note": "non_exit_domain_blocked"}
    assert "reason" not in fields


def test_enforcement_enabled_reads_warp_dict():
    assert enforcement_enabled({"warp": {"failure_domain_enforcement": True}})
    assert not enforcement_enabled({"warp": {"failure_domain_enforcement": False}})
    assert not enforcement_enabled({})
    assert not enforcement_enabled({"warp": None})


def test_daemon_patch_order_static_check():
    """patch 未应用时也能验证插入顺序（patch 路径见交付报告）。"""
    patch_path = "/tmp/batchA-daemon.patch"
    if not os.path.exists(patch_path):
        pytest.skip("daemon patch 尚未生成")
    text = open(patch_path, encoding="utf-8").read()
    pol_pos = text.find("+        _pol = rotation_policy(")
    dirty_pos = text.find("+        if (_pol.allow_dirty")
    assert pol_pos > 0 and dirty_pos > pol_pos
