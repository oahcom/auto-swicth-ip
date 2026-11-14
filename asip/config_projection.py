"""产物面 / 生效面（运行时 selector 成员）/ 台账已知线 三面纯比对 —— 单一事实源（D-1）。

从 `daemon._slot_members_drift` 提取的**纯比较语义**：闭环（daemon 自愈）、
巡检（`_config_consistency_check` 分类七）、审计（`tools/asip_audit.py
--runtime-drift`）三处共用本模块，**禁止第二份比较逻辑**。
设计文档：docs/design-20261113-config-drift.md §4 D-1、§5.2 D-1。

纯函数模块：不 import daemon / config，不碰网络、不碰磁盘 ——
调用方显式传三个集合，本模块只做集合比对。

三态输出（绝不把「未知」算成「一致」，假阴性防护见设计 T5）：

  - ``consist``（一致）   —— 过滤后既无缺失也无多余
  - ``drift``（漂移）     —— 缺失集 + 多余集（都只含台账真实线）
  - ``unknown``（无法判定）—— 产物面或运行时任一侧读不到

与 `daemon._slot_members_drift` 现行为的语义对照（逐条）：

  | 语义点        | `_slot_members_drift` 现行为                  | 本模块                              | 关系 |
  |---------------|-----------------------------------------------|-------------------------------------|------|
  | 运行时读不到  | `_slot_selector_members` 返回 None → 函数返回 None（调用方按无法判定处理，不得当漂移去重启） | runtime=None → verdict=unknown，绝不判一致 | 一致 |
  | 缺失集        | `disk - rt` 后过 `_warp_member`（只留台账真实 WARP 线；休眠/占位 tag 不构成漂移，防误杀） | `expected - runtime` 后过 `known_line_tags`（调用方传 `{m["tag"] for m in CFG["warp"]["members"]}` 即与 `_warp_member` 等价） | 一致 |
  | 多余集        | 不计算（返回值只有缺失集）                    | `runtime - expected`，同样过 `known_line_tags` | **扩展点**：新输出，供巡检/审计报告「产物面 ⊄ 生效面」的双向差集；daemon 委托后仍只取 missing，行为不变 |
  | 「一致」判据  | 返回空集                                      | missing 与 extra 均空               | 缺失口径一致；多余非空时本模块判 drift，但委托方只取 missing 时旧口径不变 |
  | 产物面读不到  | `_slot_disk_selector_members` 返回空集 → 缺失集为空 → 按无漂移（历史行为） | expected=None → verdict=unknown（审计层防假阴性扩展） | **扩展点**：daemon 委托传空集（行为不变），审计传 None |
"""
from __future__ import annotations

from typing import AbstractSet, NamedTuple, Optional

VERDICT_CONSISTENT = "consistent"
VERDICT_DRIFT = "drift"
VERDICT_UNKNOWN = "unknown"


class ProjectionResult(NamedTuple):
    """三态比对结果。unknown 时两个集合恒为空（比不出来，不编造差集）。"""

    verdict: str
    missing: frozenset[str]   # 产物面有、生效面缺（仅台账真实线）
    extra: frozenset[str]     # 生效面有、产物面缺（仅台账真实线）


def compare_members(
    expected: Optional[AbstractSet[str]],
    runtime: Optional[AbstractSet[str]],
    known_line_tags: AbstractSet[str],
) -> ProjectionResult:
    """产物面 vs 生效面 vs 台账已知线，三态纯比对。

    - ``expected``：产物面（磁盘渲染配置里 selector 声明的成员 tag 集）。
      None = 读不到（审计层防假阴性；daemon 现行为传空集，见模块对照表）。
    - ``runtime``：生效面（进程内 selector 成员 tag 集）。
      None = 读不到（API 不可达 / 未重载完 / `all` 为空 —— 与
      ``daemon._slot_selector_members`` 同约定）。**读不到绝不判一致。**
    - ``known_line_tags``：台账已知真实线 tag 集。只有这里的 tag 才构成
      漂移 —— 磁盘里合法存在的休眠 tag / 占位 tag、运行时的非 WARP tag
      都被过滤掉，避免误杀（三方审查修订 #1）。

    漂移判定：过滤后的缺失集或多余集任一非空 → drift。
    """
    if runtime is None or expected is None:
        return ProjectionResult(VERDICT_UNKNOWN, frozenset(), frozenset())
    missing = frozenset(t for t in expected - runtime if t in known_line_tags)
    extra = frozenset(t for t in runtime - expected if t in known_line_tags)
    verdict = VERDICT_DRIFT if (missing or extra) else VERDICT_CONSISTENT
    return ProjectionResult(verdict, missing, extra)
