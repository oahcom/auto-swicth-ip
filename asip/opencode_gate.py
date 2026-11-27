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
               "borrowed_skip": 0, "already_retired": 0,
               "no_healthy_node": 0},
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


_BAN_CAP_SEC = 24 * 3600.0


def _ban_until(t0, retry_after_ms=None):
    """报障渠道的冷却截止：按封禁窗冷（学闸口 _rotate_slot_once 的 ban_after）。

    429 的 Retry-After 是上游给的封禁窗（实测 FreeUsageLimitError ~9-10h）；
    固定 300s 冷却 = 5 分钟后把刚烧的渠道放回候选 —— 实测同一条线
    （LAX-175/SJC-178）8 分钟内被换出去又换回来，「换渠道」空转。
    冷却 = max(cooldown_sec, Retry-After)，钳制 <=24h；无 Retry-After 退回
    cooldown_sec（transport 等非限流故障不长冷）。
    """
    ban = _cooldown_sec()
    if retry_after_ms:
        try:
            ban = max(ban, float(retry_after_ms) / 1000.0)
        except (TypeError, ValueError):
            pass
    return t0 + min(ban, _BAN_CAP_SEC)


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

# 选点超时上限：ccs 侧 rotate 客户端 5s 预算，留 ~1s 给 PUT+回读。常态
# （有新鲜延迟样本）P2C 毫秒级作答；病理场景（daemon 忙锁/连环重探）宁可
# 退均匀随机也不挂调用方 —— 2026-11-15 03:36 实测：选点无超时把 rotate
# 挂死 40s+，所有换节点请求全卡在死节点上。
_PICKER_TIMEOUT_SEC = 4.0

# 全进程最多一个 picker worker。超时但仍存活的 worker 继续占用名额：
# 不能把仍在跑的 picker 当作已释放，再启动第二条可能同样泄漏的线程。
_PICKER_INFLIGHT = 0
_PICKER_INFLIGHT_LOCK = threading.Lock()


def _pick_with_timeout(picker, cur, pool, cooled):
    """带超时的选点调用 → (值, 是否按时答)。

    全进程最多一个 worker：已有 worker 未退出（含已超时但卡住的）时立即
    返回 (None, False)，由调用方回退均匀随机。名额由 worker 自己在 finally
    里归还；start 失败由调用线程归还。
    """
    global _PICKER_INFLIGHT

    with _PICKER_INFLIGHT_LOCK:
        if _PICKER_INFLIGHT >= 1:
            return None, False
        _PICKER_INFLIGHT = 1

    box: dict = {}

    def _run():
        global _PICKER_INFLIGHT
        try:
            try:
                box["v"] = picker(cur, pool, cooled)
            except Exception:
                box["errored"] = True
        finally:
            # 必须由 worker 自己释放：超时返回不代表 worker 已结束。
            with _PICKER_INFLIGHT_LOCK:
                _PICKER_INFLIGHT = 0

    t = threading.Thread(
        target=_run,
        name="opencode-gate-picker",
        daemon=True,
    )

    try:
        t.start()
    except Exception:
        # start 失败时 worker 不会执行 finally，由当前线程归还名额。
        with _PICKER_INFLIGHT_LOCK:
            _PICKER_INFLIGHT = 0
        return None, False

    t.join(_PICKER_TIMEOUT_SEC)

    if t.is_alive() or box.get("errored"):
        return None, False

    return box.get("v"), True


def _prune_cooldown_locked() -> None:
    now = time.time()
    for n in [n for n, until in _STATE["cooldown"].items() if until <= now]:
        _STATE["cooldown"].pop(n, None)


class _NoLock:
    """warp_lock 未注入时的空锁（单测/降级路径照常跑，不做互斥）。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NOLOCK = _NoLock()


def _select(slot, selector, name) -> bool:
    """换出口的唯一动作：PUT selector + 回读验证（不中断在途）。

    PUT 200 但 selector 仍指旧值时不能算换成功 —— 假成功会让调用方以为
    渠道已换、实际流量还走老路。
    """
    c = _client(slot)
    if not c.select(selector, name):
        return False
    return c.current(selector) == name


def _rotate_node(slot, cur, picker=None, warp_candidates=None, warp_lock=None):
    """错误换渠道：WARP 隧道优先，池节点兜底；返回 (tag, kind, ip) 或 None。

    渠道「能不能用」的判据 100% 来自探测 —— daemon 经 warp_candidates 注入
    _warp_line_fresh_candidates 的结论（OK 且未被他人占用），本模块不探活、
    不判死活、不重排顺序：**只做「取一条 + PUT selector」**。挑与切放在
    warp_lock 临界区里，避免与 slot-1..6 的挑线互撞（实测曾有两槽落到同一条
    WARP 线、同一个出口 IP）。池节点兜底仍是原 P2C 路径，判据不变。
    """
    now = time.time()
    cooled = {n for n, until in _STATE["cooldown"].items() if until > now}

    # ① WARP 隧道优先：按探测给的顺序取第一条可用的
    if warp_candidates is not None:
        with (warp_lock if warp_lock is not None else _NOLOCK):
            for tag, ip in warp_candidates(cur or ""):
                if not tag or tag == cur or tag in cooled:
                    continue
                if _select(slot, slot["selector"], tag):
                    return tag, "warp", ip

    # ② 池节点兜底：P2C + 自适应分数（picker 由 daemon 注入）
    pool = _pool_candidates(slot)
    on_pool = bool(cur) and cur in pool
    elig = [n for n in pool if n != cur and n not in cooled]
    if not elig:
        return None
    if picker is not None:
        nxt, answered = _pick_with_timeout(picker, cur or "", pool, cooled)
        if not answered:
            # 超时/异常兜底：均匀随机（缺省策略）快换，不挂调用方。
            nxt = random.choice(elig)
    else:
        nxt = random.choice(elig)
    if nxt not in elig:
        return None
    # 从隧道回落到池：先把 warp selector 指回池 selector，流量才进池。
    if not on_pool and not _select(slot, slot["selector"],
                                   slot["pool_selector"]):
        return None
    if not _select(slot, slot["pool_selector"], nxt):
        return None
    return nxt, "pool", None


def _payload(outcome, switched_node, cur_at_report, egress_at_report,
             switched_kind=None):
    """C1 响应体。除 switch_to_kind（2026-11-16 加，标换到的是隧道还是池
    节点）外七个字段一字不差。ms 恒 0（与 /rotate 现回包一致）。"""
    return {
        "ok": True,
        "switched": outcome == "ok",
        "reason": outcome,
        "switch_to_node": switched_node,
        "switch_to_kind": switched_kind,
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


def handle_rotate(req: dict, picker=None, warp_candidates=None,
                  warp_lock=None) -> dict:
    """POST /rotate-opencode-gate 的处理（契约 C1）。

    请求：{"req_id","reason"(rate429|abuse403|transport|proactive),
           "egress_ip","request_started_at","retry_after_ms","session_id"}
          （后四者可空；borrowed 为可选附加字段，9router 借道标记，D14 语义）
    响应：{"ok":true,"switched":bool,
           "reason":"ok|coalesced|borrowed_skip|already_retired|no_healthy_node",
           "switch_to_node":str|null,"switch_to_kind":"warp|pool"|null,
           "switched_at":float,"active_egress":str|null,"ms":0}

    picker：池节点选点策略的依赖注入点（守模块红线的接线缝）。签名
          (current, all_nodes, fail_set) -> str | None。daemon 的委托点传
          pick_next_node（P2C + 自适应分数，count_call=False，与
          _proxy_gate_rotate_node 同用法）；None 回退均匀随机（单测独立
          可跑，不依赖 daemon 私有状态）。
    warp_candidates：WARP 隧道候选的依赖注入点，签名 (exclude_tag) ->
          [(tag, ip)]。daemon 传 _warp_candidates_for_gate —— 判据全部来自
          探测台账（_warp_line_fresh_candidates：OK 且新鲜），本模块不探活、
          不判死活。None = 不启用隧道候选，只走池节点。
    warp_lock：挑线+切线的互斥锁（daemon 传 _SLOT_PICK_LOCK，与 slot-1..6
          的挑线同锁，避免两槽撞同一条 WARP 线）。

    语义硬约束：
      1. 只动本闸口自己的 selector（warp selector / pool selector）——
         不 _pick_healthy_slot、不指回其它闸口、不触发任何别的闸口轮换
         （模块不 import daemon）；
      2. 换渠道走「不中断在途」路径：只 PUT selector，绝不调重启；
      3. 合并窗 coalesce_ms：一批并发报障只换一次，重复调用回 coalesced；
      4. 渠道池：WARP 隧道优先（探测认定可用的），池节点兜底（P2C）；
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
            # 进程重启后选中态归零：先看 warp selector（WARP 线挂在它上面），
            # 回落时它指向 pool selector 名；再看 pool selector 的当前节点。
            now_node = _client(slot).current(slot["selector"])
            if now_node and now_node != slot["pool_selector"]:
                cur = now_node
            else:
                now_node = _client(slot).current(slot["pool_selector"])
                if _is_pool_node(now_node):
                    cur = now_node
            if cur:
                _STATE["current"] = cur
        cur_at_report = cur

        switched_node = None
        switched_kind = None
        # 迟到证据（学闸口 _rotate_slot_once 的 already_retired）：请求发出后
        # 本闸口已换过渠道（switched_at > request_started_at），这个 429 属于
        # 换线前的旧渠道 —— 新渠道已就位，为旧证据烧新渠道 = 连环烧线
        # （2026-11-15 23:45 风暴期 8 秒烧掉 6 条 WARP 同源）。只记证据、
        # 不换渠道，调用方原地重试（新渠道已在 selector 上）。
        # 事实否决（闸口 _rotate_slot_once 同款「否定已换走」）：报障出口就是
        # 当前渠道已知出口时「已换走」不可能成立 —— 该证据是当前渠道自己的，
        # 走真换线。已知出口取 egress_by_node 的前值（写入前先留）；渠道从未
        # 报过出口时无从否决，判不出保持 retired（宁可不烧新渠道）。
        # started_at 缺失时判不出迟到，保持原行为走换渠道。
        prev_egress = (_STATE["egress_by_node"].get(cur_at_report)
                       if cur_at_report else None)
        retired = (started_at is not None and started_at > 0
                   and _STATE["switched_at"] > started_at
                   and cur is not None)
        if retired and reason in ("proactive", "dead"):
            retired = False   # 主动巡检/终态判定不是迟到事故证据
        if retired and egress and prev_egress and egress == prev_egress:
            retired = False   # 报障出口=当前出口：否定「已换走」，走真换线
        # 出口记账只记当前渠道自己的出口：迟到报告的 egress 属于换线前的
        # 旧渠道，写进来会把新渠道的出口记成旧 IP（active_egress 失真、
        # 后续事实否决被污染）。retired 时跳过写入。
        if egress and cur_at_report and not retired:
            _STATE["egress_by_node"][cur_at_report] = egress
        if slot is None:
            # 显式降级：闸口没开/找不到槽位，不换渠道。
            outcome = "no_healthy_node"
        elif borrowed:
            # D14 借道：body/session 都是原线的，单发报障不足以判本闸口的
            # 渠道烧了 —— 不冷渠道、不换渠道。
            outcome = "borrowed_skip"
        elif retired:
            outcome = "already_retired"
        elif (_STATE["last_attempt_ts"]
              and t0 - _STATE["last_attempt_ts"] < _coalesce_win()):
            # 合并窗内只记证据不重复换线（闸口同款）：把报障渠道按封禁窗记入
            # 冷却，别把刚吃 429 的地址当干净地址放回候选；换线由先到者做。
            if cur and reason not in ("proactive", "dead"):
                _STATE["cooldown"][cur] = _ban_until(t0, retry_after_ms)
            outcome = "coalesced"
        else:
            _STATE["last_attempt_ts"] = t0
            if cur and reason not in ("proactive", "dead"):
                # 报障换渠道才冷却旧渠道（与第七闸口同口径）：proactive 不冷；
                # dead 已是终态判定，再冷却无意义。独立冷却表，不写公共表。
                # 冷却按封禁窗（Retry-After）而非固定 300s：烧过的渠道封禁期
                # 内不回候选，「换渠道」才真的换到新渠道而不是换出去又换回。
                _STATE["cooldown"][cur] = _ban_until(t0, retry_after_ms)
            picked = _rotate_node(slot, cur, picker, warp_candidates, warp_lock)
            if picked:
                switched_node, switched_kind = picked[0], picked[1]
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
            "to_kind": switched_kind,
            "outcome": outcome, "switched": outcome == "ok",
        })
        return _payload(outcome, switched_node, cur_at_report, egress,
                        switched_kind)


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
