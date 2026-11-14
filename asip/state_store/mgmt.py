"""管理面三件套：手动解封 / 状态快照 / restart --dry-run 副作用分级。

WHY 这三件而不是「先做个漂亮的管理 UI」：状态权威切到单写者之后，运维手上
那三个动作会**立刻变成写协议破口** —— 手改 JSON 解封、靠 grep 猜健康、
`systemctl restart` 之前不看副作用。不先把这三条正经入口交出来，「CLI 一律经
API」就是空话，逼着人回去手改库（比手改 JSON 更危险）。

三条各自的「原作用」与设计约束：

  手动解封    现状：运维直接编辑 `.warp_ip_used.json` 把 blocked 改掉。问题是
              ①与 daemon 的全量落盘竞态（daemon 内存旧值会覆盖回去，白改）
              ②没留痕（谁在什么时候解封了哪个 IP、依据是什么，事后查不到）
              ③可能把**上游仍在惩罚期**的地址放回池 → 立刻再烧一次。
              所以这里：只出**判定与变换**（纯函数），落盘必须走 daemon 的
              既有写口（持锁 + 空表/缩水护栏 + 全量落盘一致性），并强制写
              事件流水（谁/何时/为什么）。排队与执行归 W3 决策核。

  状态快照    复用 `asip.health.build_health_snapshot`（P3 契约已钉：行 7 字段
              含 age_s、顶层 8 字段、状态优先级 degraded>unknown>ok）。本模块
              不重定义字段，只负责把库侧的台账余量/库体积/影子差异接成 probe。

  restart --dry-run
              重启是**业务动作**不是运维动作：restart 一条 WARP 隧道 = 换出口
              地址 = 烧掉当前地址剩余额度（`_POOL_MIN_RATIO` 那套护栏的存在
              前提就是「地址是消耗品」）。所以 dry-run 要回答的不是「能不能
              重启」，而是「这一下会烧掉什么」。分级见 GRADES。
"""
from __future__ import annotations

import time

from . import schema
from . import shadow as shadow_mod
from .store import Store

# ── 副作用分级 ──────────────────────────────────────────────────────
# 数值越大越不可逆；verdict 取最坏一档。
GRADE_SAFE = 0            # 纯逻辑：只重建线程/连接池，不动隧道、不换地址
GRADE_CONN_DROP = 1       # 断连：进程内连接被切断，地址不变（sing-box 热重载类）
GRADE_BURNS_ADDRESS = 2   # 烧地址：隧道实体重启 → 新出口 IP，旧地址额度作废

GRADES = {GRADE_SAFE: "safe", GRADE_CONN_DROP: "connection_drop",
          GRADE_BURNS_ADDRESS: "burns_address"}

# 目标类型 → 分级 + 理由。**类型必须显式登记**：新 kind 默认按最坏档处理
# （fail-closed），防止「不知道它会干嘛就当它安全」。
TARGET_GRADES = {
    "runtime": (GRADE_SAFE,
                "只重建进程内线程/连接池；不触碰 sing-box 与隧道实体，不变更出口地址"),
    "probe": (GRADE_SAFE,
              "探测结论缓存随重启清空，代价是下一轮重探（耗一格配额），地址不变"),
    "daemon": (GRADE_CONN_DROP,
               "重启 daemon 进程：在途 /rotate 与后台修整被中断（KillMode=process"
               " 不连带杀 sing-box，数据面存活），地址不变；内存态从库/JSON 恢复"),
    "singbox": (GRADE_CONN_DROP,
                "sing-box 进程重启：所有经它建立的连接断开（客户端要重连），"
                "出口地址不变"),
    "slot": (GRADE_CONN_DROP,
             "槽位 sing-box 重启：该端口上的连接断开，地址不变，9router 会换端口"),
    "tunnel": (GRADE_BURNS_ADDRESS,
               "隧道实体重启 = 换出口 IP：当前地址剩余可用额度作废，且新地址要"
               "重新预热/探活；这是业务动作，不是运维动作"),
    "line": (GRADE_BURNS_ADDRESS,
             "WARP 线重启 = 换出口 IP（与 tunnel 同语义）；惩罚期内重启还会被"
             " 熔断闸拦住，dry-run 只报后果不代替闸门判定"),
    "unknown": (GRADE_BURNS_ADDRESS,
                "未登记的目标类型按最坏档处理（fail-closed）：不确认是否换地址"
                "就当作会换"),
}


def grade_target(target: str, kind: str | None = None) -> dict:
    kind = (kind or "").strip().lower() or "unknown"
    grade, reason = TARGET_GRADES.get(kind, TARGET_GRADES["unknown"])
    return {"target": target, "kind": kind, "grade": grade,
            "grade_name": GRADES[grade], "reason": reason,
            "burns_address": grade >= GRADE_BURNS_ADDRESS}


def restart_dry_run(targets, *, budget: dict | None = None) -> dict:
    """返回副作用分级结论；**不执行任何动作**、不 import daemon。

    targets: ["slot-1", ...] 或 [{"target": "usque-h2-3", "kind": "line"}, ...]
    budget:  {"limit": N, "used": M}（全局重启预算，4b 落表前的过渡参数）
    """
    graded = []
    for item in targets:
        if isinstance(item, dict):
            graded.append(grade_target(str(item.get("target") or ""),
                                       item.get("kind")))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            graded.append(grade_target(str(item[0]), str(item[1])))
        else:
            graded.append(grade_target(str(item), None))
    worst = max((g["grade"] for g in graded), default=GRADE_SAFE)
    burns = sum(1 for g in graded if g["burns_address"])
    out = {
        "dry_run": True,
        "ts": round(time.time(), 3),
        "targets": graded,
        "verdict": GRADES[worst],
        "burns_address_count": burns,
        "requires_explicit_approval": worst >= GRADE_BURNS_ADDRESS,
        "notes": [
            "本结论不代替闸门判定：惩罚期熔断/夜间锁/渠道锁/额度在真正重启时才评估",
            "预算按「烧地址」目标计数，安全/断连档不占预算",
        ],
    }
    if budget is not None:
        limit = int(budget.get("limit") or 0)
        used = int(budget.get("used") or 0)
        out["budget"] = {"limit": limit, "used": used,
                         "need": burns,
                         "ok": (used + burns) <= limit if limit else None}
    return out


# ── 手动解封（纯变换 + 留痕） ────────────────────────────────────────
def unban_transform(entry: dict, *, now: float,
                    ttl: dict, retention_sec: float) -> dict:
    """把一个台账条目从「封禁中」翻成「可复用」，返回 before/after 与改动清单。

    语义约束（每条都对着一个事故）：
    - 清 `blocked` / `blocked_kind` / `blocked_ts` / `blocked_ts_by_kind` /
      `ban_until` / `ban_kind`：这是 `_warp_ip_is_blocked` 的全部判据，漏一个
      就等于没解封。
    - `expires_at` **只增不减**（INV-3）：解封不延长保留期，但也绝不缩短 ——
      缩短等于提前把条目交给清理谓词删掉，删了就可能被当「从未出现过」。
    - 不动 `ts` / `first_seen` / `served_sec` / `last_ok_ts` / `probe_ok_ts`：
      它们是历史事实，解封不改变历史。
    - 保留 `probe_ok_ts` 与否**不猜**：这里不动它，由调用方（是否同时作废旧
      结论）决定 —— 那是判据，不归管理面。
    """
    from asip import ledger_policy

    before = dict(entry or {})
    after = dict(before)
    changed = []
    for key in ("blocked", "blocked_kind", "blocked_ts", "blocked_ts_by_kind",
                "ban_until", "ban_kind"):
        if after.get(key) not in (None, False, 0, {}, ""):
            changed.append(key)
        after.pop(key, None)
    after["blocked"] = False
    backfill = ledger_policy.backfill_expires_at(after, retention_sec, ttl)
    cur = ledger_policy._as_ts(before.get("expires_at"))
    if backfill is not None:
        after["expires_at"] = max(cur, backfill) if cur is not None else backfill
    return {
        "before": before,
        "after": after,
        "cleared_fields": changed,
        "was_blocked": bool(ledger_policy.is_effectively_blocked(before, now, ttl)),
        "expires_at_kept_monotone": True,
    }


def unban_plan(store: Store, ip: str, *, now: float | None = None,
               ttl: dict | None = None, retention_sec: float = 86400.0,
               reason: str = "", actor: str = "") -> dict:
    """生成解封计划（**不改库**）。执行由调用方经 daemon 写口落地。

    返回 {"ok", "ip", "found", "transform", "audit"}；`ok=False` 时
    reason 给出「为什么不动」——管理面必须能拒绝，不能什么都照做。
    """
    now = time.time() if now is None else now
    ttl = ttl or {"abuse403": 21600.0, "rate429": 60.0, "legacy": 86400.0}
    rows = store.read_raw(schema.KIND_IP_USED)
    entry = rows.get(ip)
    if entry is None:
        return {"ok": False, "ip": ip, "found": False,
                "reason": "台账里没有这个地址（不在池里，无需解封）"}
    tf = unban_transform(entry, now=now, ttl=ttl, retention_sec=retention_sec)
    if not tf["was_blocked"]:
        return {"ok": False, "ip": ip, "found": True, "reason": "该地址当前不在"
                "惩罚期（未被标记或已过期），解封无意义",
                "transform": tf}
    return {
        "ok": True, "ip": ip, "found": True, "transform": tf,
        "audit": {"ip": ip, "actor": actor or "unknown", "ts": round(now, 3),
                  "reason": reason or "(未填写)",
                  "cleared_fields": tf["cleared_fields"],
                  "expires_at": tf["after"].get("expires_at")},
    }


# ── 状态快照（复用 asip.health 的契约） ──────────────────────────────
def ledger_headroom_check(store: Store, *, cap_bytes: int = 32 * 1024 * 1024,
                          now: float | None = None):
    """台账余量：行数/字节与保留上限的差额（health 契约的第二项）。"""
    from asip import health as health_mod

    now = time.time() if now is None else now
    st = store.stats()
    return health_mod.Check(
        name=health_mod.CHECK_LEDGER_HEADROOM,
        value={"rows": st["rows"].get(schema.KIND_IP_USED, 0),
               "bytes": st["bytes"] + st["wal_bytes"],
               "cap_bytes": cap_bytes,
               "db": st["db"]},
        ok=None if st["bytes"] <= 0 else (st["bytes"] + st["wal_bytes"]) < cap_bytes,
        ts=now,
        detail="" if st["bytes"] > 0 else "状态库不可读")


def state_snapshot(store: Store, probes: dict, *, now: float | None = None,
                   ttls: dict | None = None) -> dict:
    """标准快照 = 健康四项（复用 asip.health）+ 库侧附加块 `store`。

    底层字段一个不裁：`checks` 行仍是 7 字段（含 age_s），顶层仍是 8 字段；
    `store` 是**附加**的兄弟键（消费方读不到就忽略，不影响契约）。
    """
    from asip import health as health_mod

    now = time.time() if now is None else now
    all_probes = dict(probes or {})
    all_probes.setdefault(health_mod.CHECK_LEDGER_HEADROOM,
                          lambda: ledger_headroom_check(store, now=now))
    checks = health_mod.collect(all_probes, ttls=ttls or {}, now=now)
    snap = health_mod.build_health_snapshot(checks, now=now)
    try:
        snap["store"] = store.stats()
    except Exception as exc:
        snap["store"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
    return snap


def shadow_summary(store: Store, *, window_sec: float = 3600.0,
                   now: float | None = None) -> dict:
    """影子对账摘要：每 kind 的最近差异与观察窗是否干净（切读门槛判据）。"""
    now = time.time() if now is None else now
    out = {}
    for kind in sorted(shadow_mod.SHADOWABLE):
        out[kind] = {
            "window": store.shadow_window_clean(kind, now - window_sec),
            "recent": store.shadow_history(kind, limit=3),
        }
    return out