"""asip 专属 opencode 闸口（kind="opencode"）——自包含独立模块。

职责（任务书契约 C1）：
  make_slot(base_port, base_clash, slot_index) -> dict
      产出槽位 dict（name/kind/selector/port/clash_api_port/config_file，
      格式与 config.consumer_slots() 一致），供 consumer_slots() 委托。
  handle_rotate(req: dict) -> dict
      POST /rotate-opencode-gate 的请求体 → 响应体；内部自带合并窗与锁。
  snapshot() -> dict
      只读快照（当前节点/冷却/账本摘要），供诊断。

红线（本模块自证）：
  - 不 import daemon：不碰它的锁、状态、重启函数（_warp_line_restart 等）；
  - 只动本闸口自己的 pool selector。换节点 = PUT selector（等价「换线」
    reconnect_warp 语义），**不中断在途**：不重启进程、不断既有连接；
  - 选中态、冷却、记账全部独立：冷却在本模块内存表，账本落
    opencode_gate_ledger.jsonl（asip.atomic_io），绝不写公共 node_info；
  - 节点池与 slot-7 共用（D1 完全共享）：候选来自同一份订阅渲染的池
    selector 成员；但选谁、谁在冷却、花了多少全由本模块自己记。

配置（config.CFG["opencode_gate"]，缺项显式降级为默认值）：
  slots          槽位数（consumer_slots() 产出几个 kind="opencode"）
  coalesce_ms    合并窗（毫秒），一批并发报障只换一次
  cooldown_sec   报障节点的独立冷却（秒）
  ledger_path    专属账本路径（JSONL）；缺失时只记内存，不静默改写别处
  log_path       专属流量日志路径（slot-8 sing-box 的 log.output）；
                 缺键时回退常量 PROJECT_DIR/opencode-gate.log
  opencode_log   opencode CLI 应用级日志路径（asip oc app；env OPENCODE_LOG
                 可覆盖，缺键回退 ~/.local/share/opencode/log/opencode.log）
"""
import json
import os
import random
import threading
import time

from asip.atomic_io import append_line as _atomic_append_line
from asip.clash_client import ClashClient

# ── 独立状态（不共享 daemon 的任何锁/表）────────────────────────────────
_LOCK = threading.Lock()
_CLIENTS: dict = {}          # clash_api_port -> ClashClient
_STATE = {
    "current": None,         # 当前选中池节点（独立选中态）
    "switched_at": 0.0,      # 最近一次真换节点的时刻
    "last_attempt_ts": 0.0,  # 合并窗判据：最近一次真发起轮换的时刻
    "cooldown": {},          # node -> 冷却截止 ts（独立冷却）
    "egress_by_node": {},    # node -> 请求上报的最近出口（独立记账）
    "ledger_entries": 0,     # 账本条目计数（摘要）
    "counts": {"calls": 0, "ok": 0, "coalesced": 0,
               "borrowed_skip": 0, "no_healthy_node": 0},
}

# reason 白名单与 daemon._REASON_WHITELIST 同口径：未知一律按 rate429 记。
_REASON_WHITELIST = {"rate429": "rate429", "abuse403": "abuse403",
                     "transport": "transport", "proactive": "proactive"}


def _cfg() -> dict:
    # 延迟 import：config.consumer_slots() 会 import 本模块，顶层 import config
    # 会造环。运行期 config 必已加载完。
    from config import CFG
    return CFG.get("opencode_gate") or {}


def _coalesce_win() -> float:
    """合并窗（秒）；缺项按 800ms 降级，与 api.coalesce_ms 同值。"""
    try:
        return float(_cfg().get("coalesce_ms", 800)) / 1000.0
    except (TypeError, ValueError):
        return 0.8


def _cooldown_sec() -> float:
    try:
        return float(_cfg().get("cooldown_sec", 300))
    except (TypeError, ValueError):
        return 300.0


def _ledger_path():
    p = _cfg().get("ledger_path")
    return str(p) if p else None


def log_path() -> str | None:
    """slot-8 专属流量日志路径（渲染进 sing-box log.output，asip oc log 读它）。

    读 CFG["opencode_gate"]["log_path"]；缺键时回退常量
    PROJECT_DIR/opencode-gate.log —— 与 ledger_path 同风格的常量回退：
    闸口永远有专属流量日志，不退回混写公共 singbox.log（连接行不带槽位
    归属，8 槽混流无法可靠过滤）。
    """
    p = _cfg().get("log_path")
    if p:
        return str(p)
    from config import PROJECT_DIR
    return os.path.join(PROJECT_DIR, "opencode-gate.log")


def opencode_log_path() -> str:
    """opencode CLI 应用级日志路径（asip oc app 读它；连接级流量日志的互补面）。

    解析顺序：env OPENCODE_LOG > CFG["opencode_gate"]["opencode_log"] >
    常量默认 ~/.local/share/opencode/log/opencode.log。文件缺位/不可读由
    调用方按「能读就读」优雅降级（opencode_log_info() 给结构化状态）。
    """
    p = os.environ.get("OPENCODE_LOG") or _cfg().get("opencode_log")
    if p:
        return str(p)
    return os.path.expanduser("~/.local/share/opencode/log/opencode.log")


def opencode_log_info() -> dict:
    """只读摘要：应用日志的可读性 + 最近一条异常行（供 asip oc st/app）。

    「能读就读」：文件缺失/不可读/读中出错都降级成结构化字段，绝不抛
    异常 —— 诊断命令不能因日志文件缺位崩掉。last_alert = 最近一条含
    ERROR 或 Free usage limit 的行（429 监测同口径），没有则 None。
    """
    path = opencode_log_path()
    info = {"path": path, "exists": False, "readable": False,
            "lines": None, "size": None, "last_alert": None}
    try:
        info["size"] = os.stat(path).st_size
        info["exists"] = True
    except OSError:
        return info
    try:
        n = 0
        last_alert = None
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                n += 1
                if "ERROR" in line or "Free usage limit" in line:
                    last_alert = line.rstrip()
        info["readable"] = True
        info["lines"] = n
        info["last_alert"] = last_alert
    except OSError:
        pass
    return info


def _client(slot) -> ClashClient:
    """该槽位 sing-box 的 Clash API 客户端（按端口缓存）。"""
    port = slot["clash_api_port"]
    c = _CLIENTS.get(port)
    if c is None:
        from config import get_singbox_secret
        c = ClashClient("http://127.0.0.1:%d" % port, get_singbox_secret)
        _CLIENTS[port] = c
    return c


# ── 槽位产出（config.consumer_slots() 委托）────────────────────────────

def make_slot(base_port: int, base_clash: int, slot_index: int) -> dict:
    """产出 opencode 专属闸口槽位 dict（kind="opencode"）。

    端口 / Clash API 端口**不自己编号**：与 consumer_slots() 同一套避让
    规则继续顺延（7891 被 LAN 入站占、9091 被 h2 线 Clash API 占，从这里
    起整体后移 1）；config_file 走 consumer_slot_config_path 命名。
    selector 与 slot-7 同结构：warp selector + pool_selector（轮换只碰
    pool_selector，daemon 的 WARP 线逻辑按 kind 跳过本闸口）。
    """
    from config import CFG, consumer_slot_config_path
    sb = CFG.get("singbox") or {}
    clash_p = base_clash + slot_index
    if clash_p >= 9091:
        clash_p += 1
    port_p = base_port + slot_index
    if port_p >= 7891:
        port_p += 1
    sel = (CFG.get("warp") or {}).get("selector") or "opencode-out"
    return {
        "name": "slot-%d" % (slot_index + 1),
        "kind": "opencode",
        # 与常规闸口同名的 warp selector：default_to_pool 渲染时指向池。
        "selector": sel,
        # 轮换作用面：本闸口自己的池 selector（与 slot-7 的 pool_selector
        # 同名同成员——同一份订阅渲染，节点池完全共享）。
        "pool_selector": sb.get("selector") or "proxy",
        "port": port_p,
        "clash_api_port": clash_p,
        "config_file": consumer_slot_config_path(
            str(sb.get("config_file") or ""), slot_index),
        # slot-8 专属流量日志：渲染时进 sing-box log.output，连接日志不再
        # 混写公共 singbox.log。归属逻辑在本模块（log_path()），渲染器只接线。
        "log_output": log_path(),
    }


def _find_slot():
    """当前配置里的 opencode 闸口槽位；没有则 None（显式降级）。"""
    from config import consumer_slots
    for s in consumer_slots():
        if s.get("kind") == "opencode":
            return s
    return None


# ── 节点池（与 slot-7 共享的候选池）───────────────────────────────────

def _is_pool_node(tag) -> bool:
    """池节点判据（裁剪 daemon._is_pool_node：模块不读 daemon 的成员表）。

    池 selector 的成员 = 订阅池节点 + direct；warp 线/选择器本身不是池节点。
    """
    if not tag or not isinstance(tag, str):
        return False
    if tag == "direct" or tag.startswith("WARP-"):
        return False
    return True


def _pool_candidates(slot) -> list[str]:
    names, _now = _client(slot).selector_state(slot["pool_selector"])
    if not names:
        return []
    skip = {slot["pool_selector"], slot.get("selector")}
    return [n for n in names if _is_pool_node(n) and n not in skip]


def _normalize_reason(raw) -> str:
    """白名单收敛（与 daemon._normalize_rotate_reason 同口径）。"""
    v = str(raw or "").strip().lower()
    if v in _REASON_WHITELIST:
        return _REASON_WHITELIST[v]
    if "403" in v or "abuse" in v:
        return "abuse403"
    if "transport" in v or "timeout" in v or "stall" in v:
        return "transport"
    if "proactive" in v:
        return "proactive"
    return "rate429"


# ── 轮换（契约 C1）────────────────────────────────────────────────────

def _rotate_node(slot, cur, picker=None):
    """池内换节点：独立冷却过滤 + 选一个非当前节点；失败返回 None。

    裁剪自 daemon._proxy_gate_rotate_node 的骨架，但：
      - 候选过滤只用**本模块自己的**冷却表（不读 daemon._DEAD_NODES /
        _COOLDOWN_NODES —— 冷却独立）；
      - 选点：picker 优先（依赖注入点，daemon 委托传 pick_next_node 的
        P2C + 自适应分数，签名 (current, all_nodes, fail_set) -> str|None，
        冷却中的节点经 fail_set 交出去）；缺省 None 回退均匀随机（单测
        独立可跑，不依赖 daemon 私有状态）。两条路的返回都必须落在池内
        候选（非当前、不在冷却中），不然宁可不换；
      - 不预检/不延迟择优（那是 daemon 私有状态，模块红线禁止 import）；
      - 换节点 = PUT 自己的 pool selector + 回读验证（不中断在途）；
      - 记账进本模块账本，绝不调 daemon.update_node_info_async。
    """
    now = time.time()
    cooled = {n for n, until in _STATE["cooldown"].items() if until > now}
    pool = _pool_candidates(slot)
    elig = [n for n in pool if n != cur and n not in cooled]
    if not elig:
        return None
    nxt = picker(cur or "", pool, cooled) if picker is not None \
        else random.choice(elig)
    if nxt not in elig:
        return None
    c = _client(slot)
    if not c.select(slot["pool_selector"], nxt):
        return None
    # 回读验证：PUT 200 但 selector 仍指旧节点时不能算换成功。
    if c.current(slot["pool_selector"]) != nxt:
        return None
    return nxt


def _prune_cooldown_locked() -> None:
    now = time.time()
    for n in [n for n, until in _STATE["cooldown"].items() if until <= now]:
        _STATE["cooldown"].pop(n, None)


def _payload(outcome, switched_node, cur_at_report, egress_at_report):
    """C1 响应体：七个字段，一字不差。ms 恒 0（与 /rotate 现回包一致）。"""
    return {
        "ok": True,
        "switched": outcome == "ok",
        "reason": outcome,
        "switch_to_node": switched_node,
        "switched_at": _STATE["switched_at"],
        # active_egress = 事发时本闸口的出口（与 /rotate 的 active_egress 同义：
        # 请求刚在上面吃到报错的那个出口）。请求报来的 egress_ip 优先，
        # 缺省取账本里事发节点的最近上报值。
        "active_egress": egress_at_report or _STATE["egress_by_node"].get(
            cur_at_report or ""),
        "ms": 0,
    }


def _note_ledger(entry: dict) -> None:
    path = _ledger_path()
    if not path:
        # 显式降级：账本路径缺失时不静默改写别处，只留内存计数（snapshot 可见）。
        return
    _atomic_append_line(path, entry)
    _STATE["ledger_entries"] += 1


def handle_rotate(req: dict, picker=None) -> dict:
    """POST /rotate-opencode-gate 的处理（契约 C1）。

    请求：{"req_id","reason"(rate429|abuse403|transport|proactive),
           "egress_ip","request_started_at","retry_after_ms","session_id"}
          （后四者可空；borrowed 为可选附加字段，9router 借道标记，D14 语义）
    响应：{"ok":true,"switched":bool,
           "reason":"ok|coalesced|borrowed_skip|no_healthy_node",
           "switch_to_node":str|null,"switched_at":float,
           "active_egress":str|null,"ms":0}

    picker：选点策略的依赖注入点（守模块红线的接线缝）。签名
          (current, all_nodes, fail_set) -> str | None。daemon 的委托点传
          pick_next_node（P2C + 自适应分数，count_call=False，与
          _proxy_gate_rotate_node 同用法）；None 回退均匀随机（单测独立
          可跑，不依赖 daemon 私有状态）。

    语义硬约束：
      1. 只动本闸口自己的 pool selector —— 不 _pick_healthy_slot、不指回
         其它闸口、不触发任何别的闸口轮换（模块不 import daemon）；
      2. 换节点走「不中断在途」路径：只 PUT selector（等价 reconnect_warp
         的换线语义），绝不调 _warp_line_restart 或等价重启；
      3. 合并窗 coalesce_ms：一批并发报障只换一次，重复调用回 coalesced；
      4. 节点池与 slot-7 共享，选中态/冷却/记账独立；
      5. 记账落 opencode 专属账本，不写公共 node_info。
    """
    t0 = time.time()
    req = req if isinstance(req, dict) else {}
    reason = _normalize_reason(req.get("reason"))
    borrowed = bool(req.get("borrowed"))
    rid = str(req.get("req_id") or "").strip()[:64] or None
    sid = str(req.get("session_id") or "").strip()[:64] or None
    egress = str(req.get("egress_ip") or "").strip() or None
    try:
        retry_after_ms = float(req.get("retry_after_ms") or 0) or None
    except (TypeError, ValueError):
        retry_after_ms = None
    try:
        started_at = float(req.get("request_started_at") or 0) or None
    except (TypeError, ValueError):
        started_at = None

    with _LOCK:
        _STATE["counts"]["calls"] += 1
        _prune_cooldown_locked()
        slot = _find_slot()
        cur = _STATE["current"]
        if slot is not None and cur is None:
            # 进程重启后选中态归零：从自己 selector 回读（独立选中态的恢复）。
            now_node = _client(slot).current(slot["pool_selector"])
            if _is_pool_node(now_node):
                cur = now_node
                _STATE["current"] = cur
        cur_at_report = cur
        if egress and cur_at_report:
            _STATE["egress_by_node"][cur_at_report] = egress

        switched_node = None
        if slot is None:
            # 显式降级：闸口没开/找不到槽位，不换节点。
            outcome = "no_healthy_node"
        elif borrowed:
            # D14 借道：body/session 都是原线的，单发报障不足以判本闸口的
            # 池节点烧了 —— 不冷节点、不换节点。
            outcome = "borrowed_skip"
        elif (_STATE["last_attempt_ts"]
              and t0 - _STATE["last_attempt_ts"] < _coalesce_win()):
            outcome = "coalesced"
        else:
            _STATE["last_attempt_ts"] = t0
            if cur and reason not in ("proactive", "dead"):
                # 报障换节点才冷却旧节点（与第七闸口同口径）：proactive 不冷；
                # dead 已是终态判定，再冷却无意义。独立冷却表，不写公共表。
                _STATE["cooldown"][cur] = t0 + _cooldown_sec()
            switched_node = _rotate_node(slot, cur, picker)
            if switched_node:
                outcome = "ok"
                _STATE["current"] = switched_node
                _STATE["switched_at"] = t0
            else:
                outcome = "no_healthy_node"

        st = _STATE["counts"]
        st[outcome] = st.get(outcome, 0) + 1
        _note_ledger({
            "ts": t0, "req_id": rid, "reason": reason, "session_id": sid,
            "egress_ip": egress, "request_started_at": started_at,
            "retry_after_ms": retry_after_ms,
            "from_node": cur_at_report, "to_node": switched_node,
            "outcome": outcome, "switched": outcome == "ok",
        })
        return _payload(outcome, switched_node, cur_at_report, egress)


def snapshot() -> dict:
    """只读快照：当前节点 / 冷却 / 账本摘要，供诊断。不产生任何写入。"""
    with _LOCK:
        _prune_cooldown_locked()
        return {
            "current": _STATE["current"],
            "switched_at": _STATE["switched_at"],
            "last_attempt_ts": _STATE["last_attempt_ts"],
            "cooldown": dict(_STATE["cooldown"]),
            "egress_by_node": dict(_STATE["egress_by_node"]),
            "counts": dict(_STATE["counts"]),
            "ledger_path": _ledger_path(),
            "ledger_entries": _STATE["ledger_entries"],
            "coalesce_win_sec": _coalesce_win(),
            "cooldown_sec": _cooldown_sec(),
        }


# ── 只读辅助（asip oc CLI 诊断；不产生任何写入）──────────────────────

def recent_rotations(n: int | None = None) -> list[dict]:
    """轮换账本最近 n 条（时间序，旧→新）；n=None 取全部。

    文件缺失/单行损坏都显式降级（空表/跳行）：诊断命令不能因账本半截崩掉。
    """
    path = _ledger_path()
    if not path:
        return []
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    out.append(e)
    except OSError:
        return []
    if n is None:
        return out
    return out[-n:] if n > 0 else []


def rotation_summary(window_sec: float = 3600.0) -> dict:
    """只读摘要：最近一次轮换 + 窗口内真换节点/报障次数（供 asip oc st）。"""
    now = time.time()
    entries = recent_rotations(None)
    in_win = [e for e in entries if float(e.get("ts") or 0) >= now - window_sec]
    return {
        "last": entries[-1] if entries else None,
        "rotations_in_window": sum(1 for e in in_win if e.get("switched")),
        "calls_in_window": len(in_win),
    }
