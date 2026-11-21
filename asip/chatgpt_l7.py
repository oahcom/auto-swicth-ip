# -*- coding: utf-8 -*-
"""asip chatgpt_l7 — ChatGPT 429 适配闸口（L7 反代 + 出口 IP 验证换线 + 账号窗退避）。

链路：
  chatgpt_send.py (CHATGPT_BASE=http://127.0.0.1:17892)
    → 本闸口 :17892
    → egress http://127.0.0.1:17891（外部 sing-box，不受 asip 管辖）
    → https://chatgpt.com

429 语义（2026-11-19 实测校准，三轮诱导实验）：
  **账号/会话窗，不是纯 IP 层** —— 证据：429 窗内切三个全新出口 IP
  （54.250.32.156 / 104.251.123.25 / 67.159.48.150）连续 429；而短暂静默
  后同 IP 即恢复 → 账号级快速回填小桶（约 22 次/1.5min history GET 触发，
  浏览器同会话轮询共享配额）。换 IP 对窗内无效。
  策略（防御性交流，不狂轰）：
    见 429 → 验证性换 IP 保险一跳（防偶发 IP 层）→ 仍 429 = 账号窗
      → 退避短路 + 透传，不再换线：
      Retry-After 严格采信，45s 起步 ×2 递增，480s 封顶，窗内本地短路
      （上游零流量）。换 IP 成功也清窗（防止窗口记账过期残留）。
    429 是服务端明确拒绝（请求未被处理）→ 重试安全，**POST 也可换 ok 重试**
      （区别于 transport：传输中断不知道请求是否已被处理，变更请求绝不重试防双发）
    换节点 ≠ 换 IP！池里大量节点共享出口 → 换线必须出口 IP 验证
      （chatgpt.com/cdn-cgi/trace 探针走 pool；同 IP/探不通=死节点 → 换下一候选）；
      429 链禁用 auto（urltest 不认禁点名单，会选回被禁节点，实测踩过）
  transport/CF 掐（TLS kill / reset）= 节点层 → 换节点有效
    → 优先切 chatgpt-auto（urltest 避死节点）；换线后 60s 冷却；
      只在请求体未发出或幂等方法时重试，POST /conversation 传输中断绝不重试（防双发）

防御性交流（对上游保守，不给 CF 制造触发面）：
  - 上游请求最小间隔 1~2s 随机抖动，不连珠
  - 换 ok 重试间隔 0.5~2s 抖动；429 保险一跳封顶，不连环换线
  - 账号窗内短路 = 本地应答，上游零流量
  - Retry-After 改写为闸口自己的窗口（对外统一口径）

复用（学习 asip / 复用部分）：
  - asip/opencode_l7.py 流式转发底座：_open_tunnel/_read_head/_split_head/
    _ConnReader/_pump_*/_framing_of/_relay_head_body 等，按文件加载复用；
    UPSTREAM_HOST/PORT/LOG_PATH 运行期 patch 成 chatgpt 专属。
  - asip/clash_client.py 轮换契约：本模块 _ClashMini 与其同语义（200/204=ok），
    纯 stdlib 实现保持闸口零第三方依赖（同 opencode_l7 之约定）。

日志：run/chatgpt-l7.log JSONL（同构 opencode-l7.log + gate 字段）。
端点：GET /healthz → 200；GET /_gate/status → 退避窗/节点/统计 JSON。

纯 stdlib。`python3 asip/chatgpt_l7.py` 可直跑（systemd ExecStart 同款）。
"""
import os
import sys

# 直跑修复（同 opencode_l7）：`python3 asip/chatgpt_l7.py` 时 sys.path[0] 是
# asip/ 本身，仓内 asip/http.py 会遮蔽标准库 http 包——先摘出搜索路径。
_ASIP_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_ASIP_DIR)
if sys.path and os.path.abspath(sys.path[0]) == _ASIP_DIR:
    sys.path.pop(0)

# 以下 import 必须在路径修正之后：http.server 依赖标准库 http 包。
import http.server  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402


def _load_sibling(name):
    """按文件路径加载同目录模块（不走包导入，避开 asip/http.py 遮蔽）。"""
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ASIP_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_l7 = _load_sibling("opencode_l7")

# ── ChatGPT 专属配置（patch 复用底座的模块级常量）────────────────────
_l7.UPSTREAM_HOST = "chatgpt.com"
_l7.UPSTREAM_PORT = 443
_l7.LOG_PATH = os.path.join(_PROJECT, "run", "chatgpt-l7.log")

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 17892              # chatgpt_send.py CHATGPT_BASE 指到这
DEFAULT_EGRESS_URL = "http://127.0.0.1:17891"  # 外部 sing-box（不受 asip 管辖）
CLASH_API = "http://127.0.0.1:19090"           # 外部 sing-box Clash API
CLASH_SECRET_FILE = "/home/ubuntu/sing-box/clash.secret"
POOL_SELECTOR = "chatgpt-pool"
POOL_AUTO = "chatgpt-auto"

MAX_ROTATE_RETRIES = 2            # 429 / transport 换 ok 重试上限（学 opencode_l7）
RATE429_BASE_SEC = 45.0           # 兜底窗起步（实测初始 retry-after）
RATE429_BAN_SEC = 90.0            # 429 后禁点该节点时长（≥2×起步窗，防轮转转回去）
RATE429_MAX_SEC = 480.0           # 实测清窗 ≤8min，封顶 480
RATE429_ESCALATE = 2.0            # 连续触发 ×2 递增
RATE429_RESET_SEC = 300.0         # 静默 5min 回起步档
TRANSPORT_BAN_SEC = 300.0         # 学 asip transport_ban_sec
SWITCH_COOLDOWN_SEC = 60.0        # 学 asip cooldown_after_switch_sec
MIN_UPSTREAM_GAP_SEC = 1.0        # 防御性交流：上游最小间隔
GAP_JITTER_SEC = 1.0              # 间隔抖动上限
RETRY_JITTER_SEC = (0.5, 2.0)     # 换 ok 重试抖动

_429_KINDS = ("rate429",)


# ── Clash API 迷你客户端（契约对齐 asip.clash_client.ClashClient）────
#   select(): PUT /proxies/<sel> {"name": v}，200/204=成功
#   current()/proxy_names(): GET /proxies/<sel> → now / all
#   与 ClashClient 同语义，纯 stdlib（闸口零第三方依赖）。

def _clash_secret() -> str:
    try:
        with open(CLASH_SECRET_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _clash_req(method: str, path: str, body=None):
    url = CLASH_API.rstrip("/") + "/" + path.lstrip("/")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    rq = urllib.request.Request(url, data=data, method=method)
    sec = _clash_secret()
    if sec:
        rq.add_header("Authorization", "Bearer " + sec)
    if data is not None:
        rq.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(rq, timeout=5.0) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001 — Clash API 挂了不掀翻闸口
        return 0, None


def clash_select(selector: str, value: str) -> bool:
    st, _ = _clash_req("PUT", "proxies/" + urllib.parse.quote(selector, safe=""),
                       {"name": value})
    return st in (200, 204)


def clash_current(selector: str):
    st, data = _clash_req("GET", "proxies/" + urllib.parse.quote(selector, safe=""))
    if st != 200 or not isinstance(data, dict):
        return None
    now = data.get("now")
    return str(now) if now else None


def clash_proxy_names(selector: str):
    st, data = _clash_req("GET", "proxies/" + urllib.parse.quote(selector, safe=""))
    if st != 200 or not isinstance(data, dict):
        return None
    allp = data.get("all")
    return [str(x) for x in allp] if isinstance(allp, list) else None


# ── 兜底退避窗（仅换不动 IP 时启用；429 首选换 IP）───────────────────

class _Backoff:
    __slots__ = ("until", "level", "last429", "count")

    def __init__(self):
        self.until = 0.0
        self.level = 0
        self.last429 = 0.0
        self.count = 0

    def snapshot(self, now: float) -> dict:
        return {
            "until": round(self.until, 1),
            "remaining": round(max(0.0, self.until - now), 1),
            "level": self.level,
            "count": self.count,
        }


_HISTORY = _Backoff()   # GET /backend-api/conversation*（history 独立限速）
_CHAT = _Backoff()      # POST /conversation + /sentinel/*


def _backoff_for(method: str, path: str) -> _Backoff:
    if method == "GET" and path.startswith("/backend-api/conversation"):
        return _HISTORY
    return _CHAT


def _note_429(bo: _Backoff, now: float, server_ra) -> float:
    """记一次兜底退避（换不动 IP 时才用）：窗口单调不减，连续触发指数递增。返回窗口秒数。"""
    if bo.last429 and now - bo.last429 > RATE429_RESET_SEC:
        bo.level = 0
    window = min(RATE429_BASE_SEC * (RATE429_ESCALATE ** bo.level),
                 RATE429_MAX_SEC)
    if server_ra and server_ra > 0:
        window = max(window, float(server_ra))
    bo.until = max(bo.until, now + window)      # INV：窗口不回退
    bo.level = min(bo.level + 1, 5)
    bo.last429 = now
    bo.count += 1
    return window


def _server_retry_after(headers: dict):
    """服务端退避秒数：Retry-After 优先，退 x-oai-history-initial-retry-after。"""
    for key in ("retry-after", "x-oai-history-initial-retry-after"):
        v = headers.get(key)
        if v:
            try:
                f = float(v.strip())
                if f > 0:
                    return f
            except ValueError:
                pass
    return None


def _rewrite_retry_after(header_lines, remaining: int):
    """回传头改写 Retry-After = 闸口窗口（对外统一口径），其余原样。"""
    out = []
    for ln in header_lines:
        name = _l7._name_of(ln)
        if name in (b"retry-after", b"x-oai-history-initial-retry-after"):
            continue
        out.append(ln)
    out.append(b"Retry-After: %d" % int(remaining))
    return out


# ── 换 ok（transport → 切 chatgpt-pool 非 WARP 非 HK 节点）───────────

_NODE_BAN: dict = {}        # tag → ban_until（transport 禁点）
_STATS = {"requests": 0, "short_circuits": 0, "rate429": 0,
          "rotates": 0, "transport_faults": 0}
_STATE_LOCK = threading.Lock()
_last_switch = 0.0


# ── 出口 IP 指纹（换节点 ≠ 换 IP！429 必须真换出口 IP）────────────────
# 实测教训：池里大量节点共享出口；随机换节点名可能同一 IP 同一配额，
# 429 重试照样 429。而 urltest(auto) 会选回被禁节点，429 链上禁用 auto。
# 探针走 chatgpt.com/cdn-cgi/trace（域名规则命中 chatgpt-pool = 真实出口）。
# 切勿用 api.ipify.org：非 chatgpt 域名走 final=pick（默认 warp），测错对象。
_EGRESS_MAP: dict = {}     # node tag -> exit ip
_IP_BAN: dict = {}         # exit ip -> ban_until（429 禁整簇：同出口=同配额）
_EGRESS_MAP_PATH = os.path.join(_PROJECT, "run", "gate-egress-map.json")


def _probe_egress_ip(timeout: float = 6.0):
    """经 egress 探 chatgpt-pool 当前出口 IP。None = 探不通（节点死）。"""
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", str(int(timeout)), "--proxy",
             DEFAULT_EGRESS_URL, "https://chatgpt.com/cdn-cgi/trace"],
            capture_output=True, text=True, timeout=timeout + 3)
        for line in (r.stdout or "").splitlines():
            if line.startswith("ip="):
                return line[3:].strip() or None
        return None
    except Exception:  # noqa: BLE001
        return None


def _load_egress_map() -> None:
    global _EGRESS_MAP
    try:
        with open(_EGRESS_MAP_PATH, encoding="utf-8") as f:
            m = json.load(f)
            if isinstance(m, dict):
                _EGRESS_MAP = m
    except Exception:  # noqa: BLE001
        pass


def _save_egress_map() -> None:
    try:
        with open(_EGRESS_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(_EGRESS_MAP, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _node_current():
    cur = clash_current(POOL_SELECTOR)
    if cur == POOL_AUTO:
        return clash_current(POOL_AUTO)
    return cur


def _clear_windows() -> None:
    """换到新 IP / 请求成功 = 新配额（IP 层限流语义）：清兜底窗。"""
    _HISTORY.until = 0.0
    _CHAT.until = 0.0
    _HISTORY.level = 0
    _CHAT.level = 0


def _retry_allowed(fault: str, method: str, path: str, sent_body: bool) -> bool:
    """换 ok 重试是否安全。

    rate429：服务端明确拒绝（429 = 请求未被处理）→ 一律可重试，含 POST 变更请求。
    transport：中断时不知请求是否已被处理 → 仅未发出或幂等方法可重试（防双发）。
    """
    if fault == "rate429":
        return True
    return ((not sent_body) or method in ("GET", "HEAD")
            or path.startswith("/backend-api/sentinel"))


def _has_healthy_node() -> bool:
    """池里还有未禁点的健康节点吗（429 窗内放行的判据）。"""
    now = time.time()
    names = clash_proxy_names(POOL_SELECTOR) or []
    return any(n != POOL_AUTO and _NODE_BAN.get(n, 0.0) < now for n in names)


def _rotate_once(reason: str, force: bool = True,
                 ban_sec: float = TRANSPORT_BAN_SEC,
                 need_new_ip: bool = False) -> dict:
    """切 chatgpt-pool 到另一个节点；need_new_ip=True 时必须真换出口 IP。

    策略（实测校准，池里死节点多 + 大量节点共享出口）：
      need_new_ip（429 链）→
        - 禁用 POOL_AUTO：urltest 不认禁点名单，会选回被禁节点（实测踩过）
        - 逐候选验证出口 IP（cdn-cgi/trace 探针，走 pool）：
          探不通=死节点、出口 IP 与被禁 IP 相同=同配额簇 → 禁点换下一个
        - 被 429 的出口 IP 整簇禁点（同出口=同配额）
      transport（无 429 上下文）→ 优先 chatgpt-auto（urltest 避死节点）；
        已在 auto 上失效才随机挑具体节点。
    故障换线默认 force=True 跳过换线冷却（当前节点已是坏节点）。
    """
    global _last_switch
    now = time.time()
    if not force:
        with _STATE_LOCK:
            if now - _last_switch < SWITCH_COOLDOWN_SEC:
                return {"switched": False, "reason": "switch_cooldown"}
    cur = _node_current()
    cur_ip = _EGRESS_MAP.get(cur) or _probe_egress_ip()
    if cur and cur_ip:
        _EGRESS_MAP[cur] = cur_ip
    if cur:
        _NODE_BAN[cur] = now + ban_sec   # 故障节点禁点（学 asip transport）
    if need_new_ip and cur_ip:
        _IP_BAN[cur_ip] = now + ban_sec  # 同出口簇同配额，一起禁
    sel_now = clash_current(POOL_SELECTOR)   # 原始选中（不经 auto 解析）

    def _commit(tag: str, probe: bool = True) -> dict:
        global _last_switch
        ok = clash_select(POOL_SELECTOR, tag)
        new_ip = None
        if ok:
            if probe:
                new_ip = _probe_egress_ip()
                if new_ip:
                    _EGRESS_MAP[tag] = new_ip
                    _save_egress_map()
            with _STATE_LOCK:
                _last_switch = now
            _STATS["rotates"] += 1
            _clear_windows()   # 新 IP = 新配额
        return {"switched": bool(ok), "reason": "ok" if ok else "select_failed",
                "switch_to_node": tag, "banned": cur, "new_ip": new_ip}

    def _skip(tag: str) -> bool:
        """候选该不该跳过：已知同被禁 IP / 已知在禁点簇里。"""
        ip = _EGRESS_MAP.get(tag)
        return bool(ip and ip == cur_ip) or \
            bool(ip and _IP_BAN.get(ip, 0.0) >= now)

    def _pick_verified(cands: list) -> dict:
        """挑一个真换 IP 的活节点：已知干净 IP 优先，换完探针验证。"""
        random.shuffle(cands)
        cands.sort(key=lambda n: 0 if (_EGRESS_MAP.get(n) and not _skip(n))
                   else 1)
        for pick in cands[:4]:          # 探针预算：最多 4 个
            if _skip(pick):
                continue
            res = _commit(pick)
            if not res.get("switched"):
                continue
            nip = res.get("new_ip")
            if not nip:
                # 探针探不通 = 死/半死节点（实测 ok 后必撞 transport 白耗一次）：
                # 不算成功，禁点换下一个
                _NODE_BAN[pick] = now + ban_sec
                continue
            if cur_ip and nip == cur_ip:
                # 名字换了 IP 没换 = 白换（同配额簇）：禁点，换下一个
                _NODE_BAN[pick] = now + ban_sec
                continue
            if _IP_BAN.get(nip, 0.0) >= now:
                _NODE_BAN[pick] = now + ban_sec
                continue
            res["reason"] = "ok_new_ip"
            return res
        return {"switched": False, "reason": "no_new_ip_channel", "node": cur}

    # need_new_ip（429 链）：绝不回 auto，必须验证性挑具体节点
    if need_new_ip:
        names = clash_proxy_names(POOL_SELECTOR) or []
        cands = [n for n in names
                 if n != POOL_AUTO and _NODE_BAN.get(n, 0.0) < now]
        if cur and cur in cands and len(cands) > 1:
            cands.remove(cur)
        if not cands:
            return {"switched": False, "reason": "no_healthy_node", "node": cur}
        return _pick_verified(cands)

    # transport：优先 urltest 自动挑健康节点（除非本来就在 auto 上）
    if reason == "transport" and sel_now != POOL_AUTO:
        return _commit(POOL_AUTO)

    # 具体节点随机挑（transport 在 auto 上失效时兜底）
    names = clash_proxy_names(POOL_SELECTOR) or []
    cands = [n for n in names
             if n != POOL_AUTO and _NODE_BAN.get(n, 0.0) < now]
    if cur and cur in cands and len(cands) > 1:
        cands.remove(cur)
    if cands:
        return _commit(random.choice(cands))
    # 没有具体候选了 → 退到 auto
    return _commit(POOL_AUTO)


# ── 防御性节奏：上游请求最小间隔 + 抖动 ─────────────────────────────

_PACE_LOCK = threading.Lock()
_next_slot = 0.0


def _pace() -> None:
    global _next_slot
    with _PACE_LOCK:
        now = time.time()
        wait = _next_slot - now
        _next_slot = max(_next_slot, now) + MIN_UPSTREAM_GAP_SEC \
            + random.uniform(0, GAP_JITTER_SEC)
    if wait > 0:
        time.sleep(wait)


# ── 请求拦截器（复用 opencode_l7._Handler 流式底座）─────────────────

class _Handler(_l7._Handler):
    protocol_version = "HTTP/1.1"
    server_version = "asip-chatgpt-l7"
    sys_version = ""

    def _dbg(self, msg: str) -> None:
        print("chatgpt-l7: %s" % msg, file=sys.stderr)

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        head = (b"HTTP/1.1 %d OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\nConnection: close\r\n\r\n"
                % (code, len(body)))
        try:
            self.wfile.write(head + body)
            self.wfile.flush()
        except OSError:
            pass
        self._resp_bytes = len(body)
        self.close_connection = True

    def _send_gate_429(self, remaining: int) -> None:
        """短路应答：窗内不打上游（防御性交流，上游零流量）。"""
        obj = {"detail": "Too many requests", "gate": "chatgpt_l7",
               "short_circuit": True, "retry_after": int(remaining)}
        body = json.dumps(obj).encode("utf-8")
        head = (b"HTTP/1.1 429 Too Many Requests\r\n"
                b"Content-Type: application/json\r\n"
                b"Retry-After: %d\r\nX-ChatGPT-Gate: short-circuit\r\n"
                b"Content-Length: %d\r\nConnection: close\r\n\r\n"
                % (int(remaining), len(body)))
        try:
            self.wfile.write(head + body)
            self.wfile.flush()
        except OSError:
            pass
        self._resp_bytes = len(body)
        self.close_connection = True

    def _finish_log(self, t0, status, upstream_via, rotate_actions,
                    retried, final) -> None:
        with _STATE_LOCK:
            entry = {
                "ts": round(time.time(), 3),
                "method": self.command,
                "path": self.path,
                "status": status,
                "ms": int((time.time() - t0) * 1000),
                "req_bytes": int(getattr(self, "_req_bytes", 0) or 0),
                "resp_bytes": int(getattr(self, "_resp_bytes", 0) or 0),
                "upstream_via": upstream_via,
                "rotate_actions": rotate_actions,
                "retried": retried,
                "final": final,
                "hist_bo": _HISTORY.snapshot(time.time()),
                "chat_bo": _CHAT.snapshot(time.time()),
            }
        err = str(getattr(self, "_err_snippet", "") or "")
        if err:
            entry["err"] = err
        _l7._log_line(entry)

    # ── 主流程：分类 → 账号层退避短路 / transport 换 ok ──────────────

    def _one(self) -> None:
        t0 = time.time()
        path_only = self.path.split("?", 1)[0]
        if self.command == "GET" and path_only == "/healthz":
            self._send_simple(200, b"ok\n")
            return
        if self.command == "GET" and path_only == "/_gate/status":
            now = time.time()
            cur = _node_current()
            self._send_json(200, {
                "ok": True,
                "gate": "chatgpt_l7",
                "egress": DEFAULT_EGRESS_URL,
                "pool_selector": POOL_SELECTOR,
                "current_node": cur,
                "egress_ip": _EGRESS_MAP.get(cur),
                "history_bo": _HISTORY.snapshot(now),
                "chat_bo": _CHAT.snapshot(now),
                "node_bans": {k: round(v - now, 1)
                              for k, v in _NODE_BAN.items() if v > now},
                "ip_bans": {k: round(v - now, 1)
                            for k, v in _IP_BAN.items() if v > now},
                "stats": dict(_STATS),
            })
            return
        _STATS["requests"] += 1
        try:
            req_body = self._read_req_body()
            self._req_bytes = len(req_body)
        except _l7._BodyTooLarge:
            self._send_simple(413, b"asip-chatgpt-l7: request body too large\n")
            self._finish_log(t0, 413, None, [], 0, "ok")
            return
        except Exception as e:  # noqa: BLE001 — 坏请求不掀翻监听线程
            self._dbg("bad request body: %s" % e)
            self._err_snippet = "bad request body: %s" % type(e).__name__
            self._send_simple(400, b"asip-chatgpt-l7: bad request\n")
            self._finish_log(t0, 400, None, [], 0, "ok")
            return

        # 兜底退避窗（账号窗语义，2026-11-19 实测校准）：窗内无条件短路。
        # 证据：三个全新出口 IP 连续 429 → 窗内换 IP 无效，放行去换只构成
        # 对上游的狂轰；窗内本地短路，上游零流量（防御性交流）。
        bo = _backoff_for(self.command, self.path)
        now = time.time()
        if now < bo.until:
            remaining = max(1, int(bo.until - now + 0.999))
            _STATS["short_circuits"] += 1
            self._send_gate_429(remaining)
            self._finish_log(t0, 429, None, [], 0, "short_circuit")
            return

        up_head = self._build_upstream_head()   # 复用底座（UPSTREAM_HOST 已 patch）
        egress = DEFAULT_EGRESS_URL
        rotate_actions: list = []
        retries = 0
        sent_body = False
        last_429 = None      # (status_line, header_lines, headers, body, trunc, via)
        chain_429 = False    # 本请求链上见过 429 → 后续换线一律 need_new_ip

        while True:
            fault = None     # "rate429" | "transport"
            live = None
            tun = None
            via_at_attempt = egress
            try:
                _pace()      # 防御性节奏：上游不连珠
                tun = _l7._open_tunnel(egress, _l7.UPSTREAM_HOST, _l7.UPSTREAM_PORT)
                tun.sendall(up_head + req_body)
                sent_body = True
                raw_head, rest = _l7._read_head(tun)
                status_line, status, header_lines, headers = _l7._split_head(raw_head)
                while 100 <= status < 200 and status != 101:
                    raw_head, rest = _l7._read_head(tun, rest)
                    status_line, status, header_lines, headers = \
                        _l7._split_head(raw_head)
                if status == 429:
                    fault = "rate429"
                    reader = _l7._ConnReader(tun, rest)
                    sink = bytearray()
                    trunc = False

                    def _cap(data, sink=sink):
                        sink += data

                    try:
                        framing, flen = _l7._framing_of(self.command, status, headers)
                        if framing == "chunked":
                            _l7._pump_chunked(reader, _cap)
                        elif framing == "fixed":
                            _l7._pump_fixed(reader, _cap, flen)
                        elif framing == "eof":
                            raw, trunc = reader.read_until_eof(_l7._ERR_BODY_CAP)
                            sink += raw
                    except (_l7._TransportFault, ValueError):
                        trunc = True
                    if len(sink) > _l7._ERR_BODY_CAP:
                        del sink[_l7._ERR_BODY_CAP:]
                        trunc = True
                    last_429 = (status_line, header_lines, headers,
                                bytes(sink), trunc, via_at_attempt)
                else:
                    # SSE 事件间可长间隔：体泵阶段解除超时
                    tun.settimeout(None)
                    live = (tun, _l7._ConnReader(tun, rest), status_line,
                            status, header_lines, headers)
            except _l7._TransportFault as e:
                fault = "transport"
                self._dbg("transport fault via %s: %s" % (egress, e))
            except Exception as e:  # noqa: BLE001 — 一律按 transport 收敛
                fault = "transport"
                self._dbg("unexpected upstream error via %s: %s: %s"
                          % (egress, type(e).__name__, e))
            if live is None and tun is not None:
                try:
                    tun.close()
                except OSError:
                    pass

            if live is not None:
                tun, reader, status_line, status, header_lines, headers = live
                try:
                    self._relay_head_body(status_line, header_lines, headers,
                                          reader, None, False,
                                          self.command, status, t0, via_at_attempt,
                                          rotate_actions, retries)
                finally:
                    try:
                        tun.close()
                    except OSError:
                        pass
                return

            # ── 429：换 IP 保险一跳 + 账号窗兜底 ──────────────────────
            # 实测（2026-11-19）：三个全新出口 IP 连续 429 → 限速窗内换 IP
            # 无效，429 跟账号/会话配额走（快速回填小桶，浏览器共享配额）。
            # 策略：验证性试 1 跳新 IP（防偶发 IP 层）；仍 429 = 账号窗 →
            # 退避 + 透传，不再换线狂轰（防御性交流）。
            if fault == "rate429":
                sl, hl, hd, body, trunc, via = last_429
                _STATS["rate429"] += 1
                chain_429 = True
                server_ra = _server_retry_after(hd)
                tried_new_ip = any(
                    str(a.get("outcome") or "").startswith("ok_new_ip")
                    for a in rotate_actions)
                if retries < MAX_ROTATE_RETRIES and not tried_new_ip:
                    resp = _rotate_once("rate429", ban_sec=RATE429_BAN_SEC,
                                        need_new_ip=True)
                    rotate_actions.append({
                        "reason": "rate429",
                        "outcome": str(resp.get("reason") or "-"),
                        "switched": bool(resp.get("switched")),
                        "switch_to_node": resp.get("switch_to_node"),
                        "banned": resp.get("banned"),
                        "new_ip": resp.get("new_ip"),
                    })
                    if resp.get("switched"):
                        time.sleep(random.uniform(*RETRY_JITTER_SEC))
                        retries += 1
                        continue      # 新 IP 新配额，直接重试
                # 保险一跳后仍 429（= 账号窗，换 IP 无效）或换不动 →
                # 兜底退避 + 透传，不再换线狂轰
                _note_429(bo, time.time(), server_ra)
                remaining = max(1, int(bo.until - time.time() + 0.999))
                hl2 = _rewrite_retry_after(hl, remaining)
                self._err_snippet = _l7._err_snippet(body)
                try:
                    self._relay_head_body(sl, hl2, hd, None, body, trunc,
                                          self.command, 429, t0, via,
                                          rotate_actions, retries,
                                          final="rate429")
                except Exception:  # noqa: BLE001
                    self._finish_log(t0, 429, via, rotate_actions,
                                     retries, "stream_broken")
                return

            # ── transport 故障：换 ok（切非 WARP 非 HK 节点）─────────
            _STATS["transport_faults"] += 1
            # 传输中断不知请求是否已被处理：仅未发出或幂等方法换 ok 重试（防双发）
            retry_ok = _retry_allowed("transport", self.command, self.path,
                                      sent_body)
            if retries >= MAX_ROTATE_RETRIES or not retry_ok:
                why = ("no_ok_channel" if retries >= MAX_ROTATE_RETRIES
                       else "no_retry_mutating")
                self._err_snippet = (
                    "upstream transport failure (%s after %d retries)"
                    % (why, retries))
                self._send_simple(
                    502,
                    b"asip-chatgpt-l7: upstream transport failure "
                    b"(%s after %d retries)\n" % (why.encode(), retries))
                self._finish_log(t0, 502, via_at_attempt, rotate_actions,
                                 retries, why)
                return

            resp = _rotate_once("transport", need_new_ip=chain_429)
            rotate_actions.append({
                "reason": "transport",
                "outcome": str(resp.get("reason") or "-"),
                "switched": bool(resp.get("switched")),
                "switch_to_node": resp.get("switch_to_node"),
                "banned": resp.get("banned"),
            })
            time.sleep(random.uniform(*RETRY_JITTER_SEC))   # 抖动，不连珠
            retries += 1

    # 方法别名：任意动词都走同一条拦截路径
    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _one


# ── 入口 ──────────────────────────────────────────────────────────────

class _ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(host: str = None, port: int = None) -> _ThreadingServer:
    return _ThreadingServer((host or LISTEN_HOST,
                             LISTEN_PORT if port is None else port), _Handler)


def main() -> None:
    os.makedirs(os.path.join(_PROJECT, "run"), exist_ok=True)
    _load_egress_map()
    srv = make_server(LISTEN_HOST, LISTEN_PORT)
    print("asip-chatgpt-l7 listening on http://%s:%d → https://%s:%d "
          "via %s (pool=%s, clash=%s)"
          % (LISTEN_HOST, srv.server_address[1],
             _l7.UPSTREAM_HOST, _l7.UPSTREAM_PORT,
             DEFAULT_EGRESS_URL, POOL_SELECTOR, CLASH_API), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
