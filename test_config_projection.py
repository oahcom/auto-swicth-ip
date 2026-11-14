"""`asip.config_projection` 三态纯比对的语义测试（D-1 单一事实源）。

这个模块是「产物面 / 生效面 / 台账已知线」三面比对的唯一判据，daemon 闭环、
巡检分类七、审计 --runtime-drift 三处都要委托它。判据错一处，三处一起错，
所以语义必须被测死：

① 一致 / 缺失 / 多余 三种结果；
② 休眠/占位 tag 不构成漂移（防误杀，三方审查修订 #1）；
③ 运行时读不到 = 无法判定，**绝不判一致**（假阴性防护，设计 T5）。
"""
import pytest

from asip.config_projection import (
    VERDICT_CONSISTENT,
    VERDICT_DRIFT,
    VERDICT_UNKNOWN,
    compare_members,
)

KNOWN = {"WARP-H2-NRT-1", "WARP-H2-SIN-2", "WARP-WG-1"}
DORMANT = {"slot-reserve", "DORMANT-PLACEHOLDER", "DIRECT", "REJECT"}


# ------------------------------------------------------------ ① 三态结果


def test_consistent_when_equal():
    """产物面与生效面一致 → 一致，两个差集都为空。"""
    r = compare_members(KNOWN, set(KNOWN), KNOWN)
    assert r.verdict == VERDICT_CONSISTENT
    assert r.missing == frozenset()
    assert r.extra == frozenset()


def test_missing_reported_and_filtered():
    """产物面有、生效面缺的真实线 → 漂移，缺失集只含台账真实线。"""
    expected = set(KNOWN) | {"WARP-H2-NRT-1"}
    runtime = {"WARP-H2-SIN-2", "WARP-WG-1"}
    r = compare_members(expected, runtime, KNOWN)
    assert r.verdict == VERDICT_DRIFT
    assert r.missing == frozenset({"WARP-H2-NRT-1"})
    assert r.extra == frozenset()


def test_extra_reported_and_filtered():
    """生效面有、产物面缺的真实线 → 漂移（多余集，审计/巡检的扩展输出）。"""
    expected = {"WARP-H2-NRT-1"}
    runtime = {"WARP-H2-NRT-1", "WARP-WG-1"}
    r = compare_members(expected, runtime, KNOWN)
    assert r.verdict == VERDICT_DRIFT
    assert r.missing == frozenset()
    assert r.extra == frozenset({"WARP-WG-1"})


def test_missing_and_extra_together():
    """双向差集同时报出。"""
    expected = {"WARP-H2-NRT-1"}
    runtime = {"WARP-WG-1"}
    r = compare_members(expected, runtime, KNOWN)
    assert r.verdict == VERDICT_DRIFT
    assert r.missing == frozenset({"WARP-H2-NRT-1"})
    assert r.extra == frozenset({"WARP-WG-1"})


# ------------------------------------------------- ② 休眠/占位 tag 防误杀


def test_dormant_or_placeholder_tags_are_not_drift():
    """缺的/多的全是休眠、占位、非 WARP tag → 不构成漂移。

    WHY：磁盘配置里合法存在休眠 tag / 占位 tag，运行时没有它们是正常的
    （`_warp_member` 过滤的动机）；运行时多出的 DIRECT/REJECT 之类
    非线 tag 同理。只有台账真实线的差才叫漂移。
    """
    expected = {"WARP-H2-NRT-1"} | DORMANT
    runtime = {"WARP-H2-NRT-1", "GHOST-IN-RUNTIME"}
    r = compare_members(expected, runtime, KNOWN)
    assert r.verdict == VERDICT_CONSISTENT
    assert r.missing == frozenset()
    assert r.extra == frozenset()


def test_mixed_gap_keeps_only_known_lines():
    """差集里混着真实线与占位 tag 时，只有真实线进差集。"""
    expected = {"WARP-H2-NRT-1", "slot-reserve"}
    runtime = {"DIRECT"}
    r = compare_members(expected, runtime, KNOWN)
    assert r.verdict == VERDICT_DRIFT
    assert r.missing == frozenset({"WARP-H2-NRT-1"})   # slot-reserve 被过滤
    assert r.extra == frozenset()                      # DIRECT 被过滤


# --------------------------------------- ③ 读不到 = 无法判定（不假阴性）


def test_runtime_unknown_is_never_consistent():
    """运行时读不到（None）→ 无法判定，绝不判一致（设计 T5 假阴性防护）。"""
    r = compare_members(KNOWN, None, KNOWN)
    assert r.verdict == VERDICT_UNKNOWN
    assert r.missing == frozenset()
    assert r.extra == frozenset()


def test_runtime_unknown_with_empty_expected_still_unknown():
    """产物面为空也一样：未知 ≠ 一致，空对空不能洗成「对得上」。"""
    r = compare_members(set(), None, KNOWN)
    assert r.verdict == VERDICT_UNKNOWN


def test_expected_unknown_also_unjudgeable():
    """产物面读不到（审计层传 None）→ 同样无法判定。

    daemon 委托方传空集保持旧行为（读不到=空集=无漂移），审计层防
    假阴性传 None —— 两种调用约定都成立，语义对照表见模块 docstring。
    """
    r = compare_members(None, {"WARP-H2-NRT-1"}, KNOWN)
    assert r.verdict == VERDICT_UNKNOWN
    assert r.missing == frozenset()
    assert r.extra == frozenset()


def test_empty_runtime_set_is_drift_not_unknown():
    """读到了但一条成员都没有（空集）是真差异，不是无法判定。

    「空 all → None」的映射在读层（`_slot_selector_members` /
    `read_runtime_members`）做；纯函数收到空集就按空集比。
    """
    r = compare_members({"WARP-H2-NRT-1"}, set(), KNOWN)
    assert r.verdict == VERDICT_DRIFT
    assert r.missing == frozenset({"WARP-H2-NRT-1"})


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
