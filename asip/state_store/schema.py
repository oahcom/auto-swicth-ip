"""状态权威 schema：字段白名单 + 护栏不变量（旧护栏在新存储里的等价物）。

WHY 单独一层 schema 而不是「store 里随手写判据」：阶段 2 的迁移清单（附件
A3/A4/A7 + 附件 C 全表）本质是**一组必须原样存活的语义**。它们今天散在
daemon 的写函数里（`_save_warp_ip_used` / `_save_warp_line_state` /
`_export_dead_nodes_locked`），一旦换成库就无处安放 —— 换库那次提交要是漏了
其中一条，事故不是「库不好用」而是「脏地址被当干净地址复用」。所以先在这里
把每条不变量写成**有名字的、可断言的**东西，store 调用它、契约测试钉它，
迁移与切读都不许绕过。

不变量清单（每条注明原作用与等价形式）：

  INV-EMPTY      空表拒写。原作用：并发测试进程/误启动实例用空内存覆盖盘上
                 16h 的 ban 台账（已实际发生），抹掉后切回烧掉的 IP。等价形式：
                 replace_rows 在「新集合为空 且 库内非空 且 未显式放行」时拒写。
  INV-SHRINK     条数缩水拒写。原作用：内存表未加载就落盘，N 条写成 1 条
                 （2026-11-11 实测 3→1 无日志）。等价形式：n_new < n_old 拒写。
  INV-BAN        ban_until 保护。原作用：精确封禁（rate429 的 Retry-After）在
                 清理/裁剪时绝不提前出池，否则刚封的地址 60s 后回池再烧。
                 等价形式：delete/prune 遇到 `ban_until > now` 的行拒绝删除，
                 并列进 blocked 名单返回给调用方计数。
  INV-RATIO      占比阀。原作用：Clash API 抖动会让 daemon 把全池判死，
                 死节点 >70%（1 - _POOL_MIN_RATIO=0.3）时跳过导出，防误判封死
                 池。等价形式：`ratio_gate(allows)` 常量取自 config/daemon 的
                 `_POOL_MIN_RATIO`，本模块不重新发明比例。
  INV-WHITELIST  白名单。原作用：`_LINE_DEFAULTS` 漏声明=静默丢键
                 （penalty_restart_streak 熔断清零事故：16 连重启 152 条线
                 streak 归零、10 分钟 1225 次重启 0 熔断）。等价形式：
                 **读**只还原声明字段（与今天 daemon 的行为逐字对齐），
                 **存**保留原始 body（未声明字段进 quarantine，不删除）。
  INV-UNMIGRATED 未迁移条目不静默删除。原作用：无时间锚点的条目可能带着
                 blocked 标记，删了就等于把仍在惩罚期的地址当「从未出现过」。
                 等价形式：prune 只按调用方给的判据删，本模块不自己算 TTL；
                 判不出来的条目由调用方计入 unmigrated 并告警，不落删除。
  INV-ORDER      schema 版本单调。原作用：库文件比代码新时不能按旧 schema 解释
                 （会静默丢列/丢表）。等价形式：on-disk version > 代码版本即
                 拒绝打开，提示先升级代码或回滚库（errno 性质，不静默）。

三分法：全部 A 类保真契约。改这里任何一条 = 事故复活，只许钉住；
若必须改，先在契约测试里给出等价性证明再动 store。
"""
from __future__ import annotations

# ── 存储种类（kind） ────────────────────────────────────────────────
# kind 是「一类状态」的名字，同时是投影条目名与告警 key 前缀的来源。
KIND_IP_USED = "ip_used"          # .warp_ip_used.json      出口 IP 台账
KIND_LINE_STATE = "line_state"    # .warp_line_state.json   线状态表
KIND_BAD_NODES = "bad_nodes"      # .bad_nodes.json         共享坏节点
KIND_API_STATS = "api_stats"      # .api_stats.json         /rotate 计数
KIND_LEDGER_SHADOW = "ledger_shadow"  # .ledger_shadow.json P2 门槛证据
KIND_POOL_HISTORY = "pool_history"    # .pool_history.json  池容量样本
KIND_NODE_IP_USED = "node_ip_used"    # .node_ip_used.json  池节点 IP 复用窗
KIND_SEGMENT = "segment"              # .warp_segment.json  段起点

# 文件形态的 kind → 旧文件名。投影（回滚契约）必须还原成**旧代码读得懂的
# 文件名与封套**，否则「导出→回读」只是自娱：回读出来的东西老 daemon 不认。
FILE_NAMES = {
    KIND_IP_USED: ".warp_ip_used.json",
    KIND_LINE_STATE: ".warp_line_state.json",
    KIND_BAD_NODES: ".bad_nodes.json",
    KIND_API_STATS: ".api_stats.json",
    KIND_LEDGER_SHADOW: ".ledger_shadow.json",
    KIND_POOL_HISTORY: ".pool_history.json",
    KIND_NODE_IP_USED: ".node_ip_used.json",
    KIND_SEGMENT: ".warp_segment.json",
}

# 行式（key → body）语义：投影时包成 {"<envelope>": {key: body}}。
# 空串 = 顶层就是 {key: body}（bad_nodes / node_ip_used 的既有形态）。
ROW_ENVELOPES = {
    KIND_IP_USED: "used",
    KIND_LINE_STATE: "lines",
    KIND_BAD_NODES: "",
    KIND_NODE_IP_USED: "",
}

# 行式封套里的**静态兄弟键**（live 复核：`.warp_line_state.json` 顶层是
# {"version":1,"lines":{...}}）。投影缺了它，老 daemon 写回时 version 就消失 ——
# 那不是「读得懂」的回滚点，而是形态漂移。
ROW_STATIC = {
    KIND_LINE_STATE: {"version": 1},
}

# 文档式（整份 JSON 一个 body）语义：投影时原样输出 body。
DOC_KINDS = frozenset({KIND_API_STATS, KIND_LEDGER_SHADOW, KIND_POOL_HISTORY,
                       KIND_SEGMENT})
ROW_KINDS = frozenset(ROW_ENVELOPES)

# 文档体里的**易变标记字段**：每次落盘都会变（`persisted_at` 就是落盘时刻），
# 不参与影子 diff —— 否则「差异永远 ≠0」，观察窗永远无法变干净，判据失效。
# 注意这不影响投影：回滚点里它们照旧原样保留。
DOC_VOLATILE = {
    KIND_API_STATS: frozenset({"persisted_at"}),
    KIND_LEDGER_SHADOW: frozenset({"persisted_at"}),
}

# ── 白名单：声明字段才进读结果 ───────────────────────────────────────
# 与 daemon._LINE_DEFAULTS 逐字对齐（契约测试钉「两处集合相等」，任一侧新增
# 字段而另一侧没跟上就会红 —— 这就是「新字段显式登记」的机器形态）。
# live 复核 2026-11-14：盘上还有 last_verdict_ip / last_probe_sec /
# last_probe_sec_ip / last_probe_sec_ts / egress_error / quota_until 等
# **未声明**字段（daemon 载入时本就丢弃它们）。这里的处理是「存原文、不读、
# 不删」——比今天的「丢了就没了」更保守，且不改变任何读语义。
LINE_STATE_FIELDS = frozenset({
    "ip", "ip_ts", "last_ok_ts", "last_ok_ip", "last_verdict",
    "last_probe_ts", "next_probe_ts", "fail_streak", "ever_ip",
    "restart_hist", "restart_ts", "next_restart_ts", "warming", "port_ok",
    "sel", "ban_probes", "blocked_notes", "ban_noaddr_since",
    "ban_hold_until", "stop_probe", "ban_hold_home", "ban_hold_node",
    "ban_hold_ready", "ban_backfill_tag", "ban_backfill_node",
    "penalty_restart_streak", "ip_stuck_until", "restart_ip_hist",
    "futile_restart_streak",
})

# 出口 IP 台账的字段全集（live 38,386 行实测：这些就是全部出现过的键）。
IP_USED_FIELDS = frozenset({
    "ts", "blocked", "blocked_kind", "first_seen", "blocked_ts",
    "blocked_ts_by_kind", "served_sec", "last_ok_ts", "probe_ok_ts",
    "ban_until", "ban_kind", "expires_at", "first_ok_ts",
})

# bad_nodes 条目字段（daemon 写 daemon_dead，cron 写 egress_error_rate）。
BAD_NODES_FIELDS = frozenset({"expire", "reason"})

WHITELISTS = {
    KIND_LINE_STATE: LINE_STATE_FIELDS,
    KIND_IP_USED: IP_USED_FIELDS,
    KIND_BAD_NODES: BAD_NODES_FIELDS,
}
# 未列出的 kind（文档式）不做字段级过滤：整体 body 就是它的 schema。

# 行级过滤只对这些 kind 生效；list 类型的默认值（ban_probes=None 等）由
# daemon 侧的 _LINE_DEFAULTS 提供，本模块不复制默认值 —— 复制就是第二个真值源。
FILTERED_KINDS = frozenset(WHITELISTS)

# ── schema 版本 ─────────────────────────────────────────────────────
# 单调递增：一次迁移一个号，迁移器登记在 deploy/migrations/NNN_*.py。
SCHEMA_VERSION = 1
# 建库时的基线版本：升到 BASE_VERSION 不需要迁移实现（表结构由 _init_tables
# 的 CREATE TABLE IF NOT EXISTS 给出），再往上每一级都必须是真迁移。
BASE_VERSION = 1

# 迁移登记表（version → 迁移文件名）。空 = 初始版本，无需迁移即可建库。
# 加字段/加表时在这里登记，并从 SCHEMA_VERSION 起逐级 up()/down()。
MIGRATIONS = {
    # 2: "002_line_state_add_example_field.py",
}

# 事件表 wire schema：与 asip.alerting 的事件 dict 逐字一致（九字段）。
# P3 的事件表就是「未来消费方按字段查询」的那张表，改这里必须同步
# tests/contracts/test_fidelity_alerting_interfaces.py。
EVENT_FIELDS = ("schema", "ts", "category", "severity", "message",
                "source", "key", "suppressed", "fields")


def whitelist(kind: str) -> frozenset[str] | None:
    """该 kind 的声明字段；None = 不做字段级过滤（文档式）。"""
    return WHITELISTS.get(kind)


def filter_fields(kind: str, body: dict) -> tuple[dict, dict]:
    """把一行 body 拆成 (声明字段, 未声明残留)。

    读语义：**只返回声明字段**（与 daemon 载入逐字一致）；残留原样交给调用方
    保管，绝不在这里丢弃 —— 「漏声明=静默丢键」正是 INV-WHITELIST 要防的。
    """
    allowed = whitelist(kind)
    if allowed is None or not isinstance(body, dict):
        # 非 dict 的 body（node_ip_used 的行就是 `ip → float`）原样返回：
        # 这里的语义是「不做字段级过滤」，不是「必须是 dict」。
        return body, {}
    kept = {k: v for k, v in body.items() if k in allowed}
    extra = {k: v for k, v in body.items() if k not in allowed}
    return kept, extra


def missing_declared(kind: str) -> frozenset[str]:
    """该 kind 的默认值由调用方（daemon._LINE_DEFAULTS）提供，这里只给键名。"""
    return whitelist(kind) or frozenset()


# ── 护栏不变量（判定与副作用分离：这里只出判定，不写盘） ──────────────

class GuardRefused(Exception):
    """护栏拒写。调用方按原语义处理：记日志 + 告警，继续跑（不掀翻调用链）。"""

    def __init__(self, invariant: str, detail: str, **ctx):
        super().__init__("%s: %s" % (invariant, detail))
        self.invariant = invariant
        self.detail = detail
        self.ctx = ctx


def check_empty_write(n_new: int, n_old: int, *, allow_empty: bool) -> None:
    """INV-EMPTY：新集合为空、库内非空、未显式放行 → 拒写。"""
    if n_new == 0 and n_old > 0 and not allow_empty:
        raise GuardRefused("INV-EMPTY",
                           f"空表覆盖被拒（库内 {n_old} 条）",
                           on_disk=n_old, in_memory=0)


def check_shrink(n_new: int, n_old: int, *, allow_empty: bool = False) -> None:
    """INV-SHRINK：条数缩水 → 拒写（含「未加载就落盘」这一族未来变体）。

    `allow_empty` 同时放行本条：两个护栏防的是同一件事（未加载/误判空就落盘），
    而「显式清空」必然同时触发两者。要求调用方为同一个意图传两个开关，只会
    诱使人为了少传一个参数去改护栏本身。
    """
    if n_new < n_old and not allow_empty:
        raise GuardRefused("INV-SHRINK",
                           f"条数缩水被拒（{n_old} → {n_new}）",
                           on_disk=n_old, in_memory=n_new)


def protected_by_ban(body: dict, now: float) -> bool:
    """INV-BAN：`ban_until > now` 的行不许删（精确封禁保护）。

    解析口径与 `asip.ledger_policy._as_ts` 一致（唯一语义基准）：能 float()
    成有限正数才采信，NaN/Inf/非数值不构成保护（坏值本来就不该挡路）。
    数字字符串（"9e9"）**算有效** —— 手工编辑/旧版本写出的字符串时间戳是
    真实存在的脏数据形态，daemon 的 `_as_ts` 也接受它；这里若更严格，清理
    就会比 daemon 多删，正是「刚封地址回池再烧」那次事故的成因。
    """
    if not isinstance(body, dict):
        return False
    try:
        until = float(body.get("ban_until"))
    except (TypeError, ValueError, OverflowError):
        return False
    if not (until > 0) or not (until < float("inf")) or until != until:
        return False
    return body.get("ban_kind") == "rate429" and until > now


def ratio_gate(dead: int, total: int, min_ratio: float) -> bool:
    """INV-RATIO：是否允许导出坏节点。等价于 daemon 的
    `dead <= total * (1 - _POOL_MIN_RATIO)`；total==0 时先由调用方跳过
    （Clash API 不通不等于全池死，那道闸在 daemon 侧，不在这里复制）。
    """
    if total <= 0:
        return False
    return dead <= total * (1.0 - float(min_ratio))