#!/usr/bin/env python3
"""asip 第八闸口 L7 请求拦截器：opencode 专属请求级 429/transport 判定与换 ok。

背景（用户裁决）：429 监控从「ccs 侧日志轮询」改为「asip 在闸口层拦截请求
直接判」；判出故障换 ok 渠道（与其它闸口换 ok 语义一致：故障 → 换可用通道
→ 请求落到可用通道成功），落不到可用通道才把错误透传给会话。本模块只服务
第八闸口（slot-8，opencode 专属），不触碰其它闸口 / daemon / config。

形态：L7 反代。slot-8 的 sing-box mixed 入口在 127.0.0.1:7898，TLS 隧道内
看不到 HTTP 状态码；opencode 的 baseURL 指到本进程（127.0.0.1:7899）后，
请求在明文侧被本进程看到状态码 —— 不 MITM 的唯一请求级拦截点：

  opencode ──HTTP──▶ 127.0.0.1:7899（本模块）
                     │  HTTP CONNECT 127.0.0.1:7898（slot-8 出口）
                     ▼
  sing-box(anytls) ──TLS──▶ opencode.ai

行为契约：
  1. 任意方法/路径透明转发 https://opencode.ai + 原路径（baseURL 语义：
     http://127.0.0.1:7899/inference/v1/x → https://opencode.ai/inference/v1/x）。请求头/体
     原样（含 Authorization），仅两处重写：Host → 上游域名（反代必需，上游
     虚拟主机按它路由）、Connection: close（隧道一请求一连接，重试要新隧道）。
     响应头/体流式原样回传，SSE/chunked 逐块转发（按上游块帧节奏），不整包
     缓冲；Content-Length/块帧都精确消费，支持客户端 keep-alive 复用。
  2. 上游出口走 slot-8：CONNECT 到 DEFAULT_EGRESS_URL（http://127.0.0.1:7898）
     再 TLS 到 opencode.ai；换 ok 后按 rotate 回包的 switch_to_url 改出口。
  3. 判据（只在「尚未向客户端回传任何响应字节」时才换 ok 重试）：
     - 上游响应 429 → reason=rate429；
     - 上游连接失败 / TLS 失败 / 响应头没收全（socket 中途断）→ reason=transport；
     - 其它一切响应（含其它 4xx/5xx）一律透传不干预。
  4. 换 ok 动作：POST /rotate-opencode-gate（UDS run/rotate.sock + 头
     X-Asip-Token，token 在 run/api-token）。响应 switch_to_url 非空 → 用它
     作重试出口（CONNECT 过它的 host:port）；switch_to_url 空但 switched=true
     → 重试仍走默认出口（selector 已换到健康节点）；coalesced 等 switched=false
     → 重试当前通道（探测窗语义，失败照旧计入次数）。每次请求最多
     MAX_ROTATE_RETRIES 次换 ok 重试；再失败把最后一个上游错误透传给客户端
     （429 原样 / transport 包装 502），日志记 final=no_ok_channel（判「无可用
     通道」）。换 ok 的「ok」判定 = 重试请求成功（请求级验证）。
  5. 日志：run/opencode-l7.log JSONL 每请求一行——请求流量日志（正常流量
     也记全，换 ok 记录只是其中一个字段，不是主体）：
     {ts, method, path, status, ms, req_bytes, resp_bytes, upstream_via,
      rotate_actions[], retried, final, err?}
     req_bytes/resp_bytes = 请求/响应体字节数；err 仅故障行有（≤160 字符：
     上游错误摘要或收尾说明）。人类可读流量视图 asip oc l7（--raw 原始
     JSONL，--fail/--rot 过滤），格式化与过滤在本模块 view_main（契约单一
     解释点）。
     final: ok = 响应完整送达（透传或重试成功）
            no_ok_channel = 换 ok 重试耗尽，透传错误
            stream_broken = 已开始回传后上游断流（不可重试，连接收尾）
  6. GET /healthz → 200（systemd/检查用；本地应答，不进 JSONL）。

部署（deploy/asip-opencode-l7.service，systemd 用户单元；工作目录=仓库）：
    cp deploy/asip-opencode-l7.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now asip-opencode-l7
  再把 opencode 的 provider baseURL 指到 http://127.0.0.1:7899/v1
  （~/.config/opencode/opencode.jsonc → provider.oc-native.options.baseURL）。
  run/rotate.sock、run/api-token、run/opencode-l7.log 均按仓库根解析。

纯 stdlib：不 import daemon / config / asip.*，`python3 asip/opencode_l7.py`
可直跑（systemd ExecStart 同款）。模块级常量即配置面，测试 monkeypatch 即可
全 loopback 假件运行（含 _wrap_tls 这个 TLS 缝）。
"""
import os
import sys

# 直跑修复（2026-11-15 真机 systemd 启动实测）：`python3 asip/opencode_l7.py`
# 时 sys.path[0] 是 asip/ 本身，仓内 asip/http.py 会遮蔽标准库 http 包——
# `import http.server` 打到在仓模块上（再连锁 import requests 崩），服务起不来。
# 本模块不 import asip.*，把脚本目录摘出搜索路径即可根治；包导入
# （asip.opencode_l7 / pytest）时 sys.path[0] 不是脚本目录，不受影响。
if sys.path and os.path.abspath(sys.path[0]) == \
        os.path.dirname(os.path.abspath(__file__)):
    sys.path.pop(0)

# 以下 import 必须在路径修正之后：http.server 依赖 stdlib http 包。
import http.server  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402
import ssl  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402
import uuid  # noqa: E402

# ── 配置面（模块级常量；测试 monkeypatch，生产不改）────────────────────
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LISTEN_HOST = "127.0.0.1"          # 仅本地
LISTEN_PORT = 7899                 # opencode baseURL 指到 http://127.0.0.1:7899/v1

UPSTREAM_HOST = "opencode.ai"  # 透明转发目标（HTTPS）——真实免费端点
#   https://opencode.ai/inference/v1/chat/completions（2026-11-15 实测取证；
#   历史误填 opencode.ai，根路径只是 Hello world，/v1/* 与 /inference/*
#   全 404）
UPSTREAM_PORT = 443

DEFAULT_EGRESS_URL = "http://127.0.0.1:7898"   # slot-8 出口（供 rotate 报告同值）

ROTATE_SOCK = os.path.join(PROJECT_DIR, "run", "rotate.sock")
ROTATE_TOKEN_FILE = os.path.join(PROJECT_DIR, "run", "api-token")
LOG_PATH = os.path.join(PROJECT_DIR, "run", "opencode-l7.log")

MAX_BODY_BYTES = 20 * 1024 * 1024   # 请求体缓冲上限（便于重试）
MAX_ROTATE_RETRIES = 2              # 每请求换 ok 重试上限
HEAD_TIMEOUT_SEC = 60.0             # 上游响应头等待上限（SSE 在 body 阶段无超时）
ROTATE_TIMEOUT_SEC = 10.0           # UDS rotate 调用预算
_ERR_BODY_CAP = 1024 * 1024         # 429 错误体缓冲上限（超出退 close-delimited）

# 请求侧剔除的逐跳头（换隧道要重建）；Host 单独重写。
_REQ_STRIP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization",
              "proxy-authenticate", "te", "trailer", "upgrade", "expect"}
# 响应侧剔除的逐跳头；Transfer-Encoding 保留（块帧原样转发，客户端要按它解析）。
_RESP_STRIP = {"connection", "keep-alive", "proxy-connection",
               "proxy-authenticate", "upgrade"}


class _TransportFault(Exception):
    """上游传输层故障（连接失败 / TLS 失败 / 响应头没收全 / 中途断）。"""


class _BodyTooLarge(Exception):
    """请求体超过 MAX_BODY_BYTES。"""


# ── 出口隧道（HTTP CONNECT + TLS）─────────────────────────────────────

def _parse_proxy_url(url: str) -> tuple[str, int]:
    """http://host:port → (host, port)；缺端口按 scheme 默认（http→80）。"""
    parts = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return host, port


def _wrap_tls(raw_sock, server_hostname):
    """TLS 缝：生产包 SNI 到上游；测试 monkeypatch 成恒等（假件不玩证书）。"""
    ctx = ssl.create_default_context()
    return ctx.wrap_socket(raw_sock, server_hostname=server_hostname)


def _open_tunnel(egress_url: str, target_host: str, target_port: int):
    """经出口代理 CONNECT 建隧道 → TLS 到上游；失败一律 _TransportFault。

    换 ok 后 switch_to_url 就是「另一个出口代理」——只是 CONNECT 的落点不同，
    隧道协议与默认出口完全一致（与其它闸口换 ok 后换 egress 同语义）。
    """
    proxy_host, proxy_port = _parse_proxy_url(egress_url)
    try:
        sock = socket.create_connection((proxy_host, proxy_port), timeout=15.0)
    except OSError as e:
        raise _TransportFault("egress_connect: %s" % e)
    try:
        sock.settimeout(HEAD_TIMEOUT_SEC)
        sock.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
                      % (target_host, target_port,
                         target_host, target_port)).encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise _TransportFault("connect_eof")
            buf += chunk
            if len(buf) > 16384:
                raise _TransportFault("connect_head_too_large")
        status_line = buf.split(b"\r\n", 1)[0]
        if b" 200" not in status_line:
            raise _TransportFault("connect_refused: %s" % status_line[:80])
    except _TransportFault:
        sock.close()
        raise
    except OSError as e:
        sock.close()
        raise _TransportFault("connect_io: %s" % e)
    try:
        tun = _wrap_tls(sock, target_host)
    except Exception as e:  # noqa: BLE001 — 证书/握手失败都算 transport
        sock.close()
        raise _TransportFault("tls: %s" % e)
    return tun


# ── 上游响应头解析（保留原始字节，回传时除逐跳头外原样）───────────────

def _read_head(sock, prefix: bytes = b"", max_bytes: int = 65536):
    """读到 \\r\\n\\r\\n 为止 → (原始头字节, 头之后已读到的前缀)。头没收全抛
    _TransportFault（中途断 / 超时 / 超长）。"""
    buf = bytearray(prefix)
    while b"\r\n\r\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except OSError as e:
            raise _TransportFault("head_read: %s" % e)
        if not chunk:
            raise _TransportFault("head_eof")
        buf += chunk
        if len(buf) > max_bytes:
            raise _TransportFault("head_too_large")
    head, rest = bytes(buf).split(b"\r\n\r\n", 1)
    return head, rest


def _split_head(raw: bytes):
    """原始头 → (状态行原始字节, status, 头行原始字节表, 名→值小写表)。"""
    lines = raw.split(b"\r\n")
    status_line = lines[0]
    header_lines = [ln for ln in lines[1:] if ln.strip()]
    headers = {}
    for ln in header_lines:
        if b":" in ln:
            k, v = ln.split(b":", 1)
            headers[k.strip().lower().decode("latin-1")] = \
                v.strip().decode("latin-1")
    try:
        status = int(status_line.split()[1])
    except (IndexError, ValueError):
        raise _TransportFault("bad_status_line")
    return status_line, status, header_lines, headers


def _name_of(raw_line: bytes) -> bytes:
    return raw_line.split(b":", 1)[0].strip().lower()


def _framing_of(method: str, status: int, headers: dict) -> tuple[str, int]:
    """响应体帧类型 → ("chunked"|"fixed"|"eof"|"none", 长度)。

    none = 无体（HEAD/204/304），可安全 keep-alive；eof = 连接关闭定界，
    回传后必须关客户端连接（HTTP/1.1 close-delimited 合法）。
    """
    if method == "HEAD" or status in (204, 304):
        return "none", 0
    te = (headers.get("transfer-encoding") or "").lower()
    if "chunked" in te:
        return "chunked", 0
    if "content-length" in headers:
        try:
            return "fixed", int(headers["content-length"])
        except ValueError:
            raise _TransportFault("bad_content_length")
    return "eof", 0


# ── 字节泵（流式：收到多少转多少，不整包缓冲）────────────────────────

class _ConnReader:
    """socket + 已读前缀 的读取器；行读/定长读保证不丢前缀字节。"""

    def __init__(self, sock, prefix: bytes = b""):
        self._sock = sock
        self._buf = bytearray(prefix)

    def _fill(self) -> bool:
        try:
            chunk = self._sock.recv(65536)
        except OSError as e:
            raise _TransportFault("body_read: %s" % e)
        if not chunk:
            return False
        self._buf += chunk
        return True

    def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            if not self._fill():
                raise _TransportFault("eof_mid_body")
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def read_line(self, max_len: int = 4096) -> bytes:
        while b"\n" not in self._buf:
            if len(self._buf) > max_len:
                raise _TransportFault("line_too_long")
            if not self._fill():
                raise _TransportFault("eof_mid_body")
        i = self._buf.index(b"\n") + 1
        out = bytes(self._buf[:i])
        del self._buf[:i]
        return out

    def read_some(self, n: int = 65536) -> bytes:
        """有多少拿多少（流式泵用）；EOF 返回 b''。"""
        if self._buf:
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out
        try:
            return self._sock.recv(n)
        except OSError as e:
            raise _TransportFault("body_read: %s" % e)

    def read_until_eof(self, cap: int) -> tuple[bytes, bool]:
        """收尾读（close-delimited 体）；cap 截断并置 truncated。"""
        parts = [bytes(self._buf)]
        del self._buf[:]
        total = len(parts[0])
        while total <= cap:
            try:
                chunk = self._sock.recv(65536)
            except OSError as e:
                raise _TransportFault("body_read: %s" % e)
            if not chunk:
                break
            parts.append(chunk)
            total += len(chunk)
        body = b"".join(parts)
        if len(body) > cap:
            return body[:cap], True
        return body, False


def _pump_chunked(reader: _ConnReader, write) -> None:
    """逐块流式转发（块帧/尾部原样写给 write）；块完成即出，不攒整包。"""
    while True:
        line = reader.read_line()
        write(line)
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            raise _TransportFault("bad_chunk_size")
        if size == 0:
            while True:  # 尾部（trailers）到空行，原样转发
                t = reader.read_line()
                write(t)
                if t in (b"\r\n", b"\n"):
                    return
        write(reader.read_exact(size + 2))  # 块体 + CRLF


def _pump_fixed(reader: _ConnReader, write, n: int) -> None:
    while n > 0:
        data = reader.read_some(min(n, 65536))
        if not data:
            raise _TransportFault("eof_mid_body")
        write(data)
        n -= len(data)


def _pump_until_eof(reader: _ConnReader, write) -> None:
    """close-delimited 体：来多少转多少（SSE 无帧时的关键路径）。"""
    while True:
        data = reader.read_some(65536)
        if not data:
            return
        write(data)


# ── rotate 客户端（HTTP over UDS，与 state_store CLI 同协议）───────────

def _read_token() -> str:
    try:
        with open(ROTATE_TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _call_rotate(payload: dict) -> dict:
    """POST /rotate-opencode-gate；失败返回 {"ok": False, "reason": ...}（不抛）。

    rotate 侧失败不等于「不换」也不是成功——调用方按 switched=false 处理：
    重试当前通道一次（与 coalesced 同路），失败照旧计入重试次数。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(ROTATE_TIMEOUT_SEC)
    try:
        s.connect(ROTATE_SOCK)
        head = ("POST /rotate-opencode-gate HTTP/1.1\r\nHost: asip\r\n"
                "X-Asip-Token: %s\r\nContent-Type: application/json\r\n"
                "Content-Length: %d\r\nConnection: close\r\n\r\n"
                % (_read_token(), len(body)))
        s.sendall(head.encode("ascii") + body)
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
        raw = b"".join(chunks)
        data = raw.split(b"\r\n\r\n", 1)[-1]
        return json.loads(data.decode("utf-8") or "{}")
    except Exception as e:  # noqa: BLE001 — UDS 挂了也不掀翻请求
        return {"ok": False, "reason": "rotate_error",
                "error": "%s: %s" % (type(e).__name__, e)}
    finally:
        s.close()


# ── JSONL 日志 ────────────────────────────────────────────────────────

_LOG_LOCK = threading.Lock()


def _log_line(entry: dict) -> None:
    line = json.dumps(entry, ensure_ascii=False)
    with _LOG_LOCK:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            print("opencode-l7: log write failed: %s" % e, file=sys.stderr)


# ── 请求拦截器 ────────────────────────────────────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "asip-opencode-l7"
    sys_version = ""

    # ── 工具 ─────────────────────────────────────────────────────────

    def log_message(self, fmt, *args):  # noqa: A003 — 访问日志走 JSONL，不打 stderr
        pass

    def _dbg(self, msg: str) -> None:
        print("opencode-l7: %s" % msg, file=sys.stderr)

    def _send_simple(self, code: int, body: bytes, reason: bytes = b"") -> None:
        reasons = {200: b"OK", 400: b"Bad Request", 413: b"Payload Too Large",
                   502: b"Bad Gateway"}
        reason = reason or reasons.get(code, b"Error")
        head = (b"HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\n"
                b"Content-Length: %d\r\nConnection: close\r\n\r\n"
                % (code, reason, len(body)))
        try:
            self.wfile.write(head + body)
            self.wfile.flush()
        except OSError:
            pass
        self._resp_bytes = len(body)
        self.close_connection = True

    def _read_req_body(self) -> bytes:
        """缓冲客户端请求体（≤MAX_BODY_BYTES）；chunked 帧原样保留可整段重放。"""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            out = bytearray()
            while True:
                line = self.rfile.readline(4096)
                if not line:
                    raise _TransportFault("req_chunk_eof")
                out += line
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise ValueError("bad req chunk")
                if size == 0:
                    while True:
                        t = self.rfile.readline(4096)
                        if not t:
                            break
                        out += t
                        if t in (b"\r\n", b"\n"):
                            break
                    if len(out) > MAX_BODY_BYTES:
                        raise _BodyTooLarge()
                    return bytes(out)
                out += self.rfile.read(size + 2)
                if len(out) > MAX_BODY_BYTES:
                    raise _BodyTooLarge()
        cl = self.headers.get("Content-Length")
        if cl:
            try:
                n = int(cl)
            except ValueError:
                raise ValueError("bad content-length")
            if n < 0:
                raise ValueError("bad content-length")
            if n > MAX_BODY_BYTES:
                raise _BodyTooLarge()
            return self.rfile.read(n)
        return b""

    def _build_upstream_head(self) -> bytes:
        """上游请求头：原样透传（含 Authorization），只重写 Host 与
        Connection: close（逐跳头剔除见 _REQ_STRIP）。"""
        if UPSTREAM_PORT == 443:
            host_hdr = UPSTREAM_HOST
        else:
            host_hdr = "%s:%d" % (UPSTREAM_HOST, UPSTREAM_PORT)
        lines = ["%s %s HTTP/1.1" % (self.command, self.path)]
        seen_host = False
        te_kept = False
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in _REQ_STRIP:
                continue
            if kl == "host":
                lines.append("Host: %s" % host_hdr)
                seen_host = True
                continue
            if kl == "transfer-encoding":
                te_kept = True
            lines.append("%s: %s" % (k, v))
        if not seen_host:
            lines.insert(1, "Host: %s" % host_hdr)
        if te_kept:
            # TE 与 CL 并存时按 RFC 以 TE 为准，CL 丢掉（防 request smuggling）
            lines = [ln for ln in lines
                     if ln.split(":", 1)[0].strip().lower() != "content-length"]
        lines.append("Connection: close")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")

    def _render_head(self, status_line: bytes, header_lines, strip_cl_te: bool) -> bytes:
        """回传头：逐跳头剔除 + 按帧型补 Connection，其余原始字节原样。"""
        out = [status_line]
        for ln in header_lines:
            name = _name_of(ln)
            if name in _RESP_STRIP:
                continue
            if strip_cl_te and name in (b"content-length", b"transfer-encoding"):
                continue
            out.append(ln)
        return b"\r\n".join(out)

    def _relay_head_body(self, status_line, header_lines, headers,
                         reader, body_fixed, truncated,
                         method: str, status: int, t0: float,
                         upstream_via, rotate_actions, retried,
                         final: str = "ok") -> None:
        """把上游响应回传客户端并记 JSONL。body_fixed 非 None 时是终态 429
        的缓冲体（含原始块帧，reader 可传 None）；否则从 reader 流式泵。
        final：正常回传="ok"；换 ok 耗尽的终态透传="no_ok_channel"。"""
        framing, flen = _framing_of(method, status, headers)
        if truncated:
            # 缓冲被截断：CL/TE 都不可信，退 close-delimited（仍逐字节转发）
            framing, flen = "eof", 0
        want_keepalive = not self.close_connection
        keep = want_keepalive and framing != "eof"
        head = self._render_head(status_line, header_lines,
                                 strip_cl_te=truncated)
        conn = b"keep-alive" if keep else b"close"
        out = head + b"\r\nConnection: " + conn + b"\r\n\r\n"

        sent = 0

        def write(data: bytes) -> None:
            nonlocal sent
            self.wfile.write(data)
            sent += len(data)

        broken = False
        try:
            write(out)
            self.wfile.flush()
            if body_fixed is not None:
                write(body_fixed)
                self.wfile.flush()
            elif framing == "chunked":
                _pump_chunked(reader, write)
            elif framing == "fixed":
                _pump_fixed(reader, write, flen)
            elif framing == "eof":
                _pump_until_eof(reader, write)
            self.wfile.flush()
        except (_TransportFault, OSError):
            broken = True
        self._resp_bytes = max(0, sent - len(out))   # 只计响应体
        if broken:
            self._err_snippet = "stream broken mid-response"
        self.close_connection = not keep or broken
        self._finish_log(t0, status, upstream_via, rotate_actions, retried,
                         "stream_broken" if broken else final)

    def _finish_log(self, t0, status, upstream_via, rotate_actions,
                    retried, final) -> None:
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
        }
        err = str(getattr(self, "_err_snippet", "") or "")
        if err:
            entry["err"] = err
        _log_line(entry)

    # ── 主流程 ───────────────────────────────────────────────────────

    def _one(self) -> None:
        t0 = time.time()
        if self.command == "GET" and self.path.split("?", 1)[0] == "/healthz":
            self._send_simple(200, b"ok\n")
            return
        try:
            req_body = self._read_req_body()
            self._req_bytes = len(req_body)
        except _BodyTooLarge:
            self._send_simple(413, b"asip-l7: request body too large\n")
            self._finish_log(t0, 413, None, [], 0, "ok")
            return
        except Exception as e:  # noqa: BLE001 — 坏请求不掀翻监听线程
            self._dbg("bad request body: %s" % e)
            self._err_snippet = "bad request body: %s" % type(e).__name__
            self._send_simple(400, b"asip-l7: bad request\n")
            self._finish_log(t0, 400, None, [], 0, "ok")
            return

        up_head = self._build_upstream_head()
        req_id = uuid.uuid4().hex[:16]
        egress = DEFAULT_EGRESS_URL
        rotate_actions: list = []
        retries = 0
        retry_after_ms = None
        last_429 = None      # (状态行, 头行, headers, 体, truncated, via)
        via_at_attempt = egress

        while True:
            fault = None     # "rate429" | "transport"
            live = None      # (tun, reader, status_line, status, header_lines, headers)
            tun = None
            via_at_attempt = egress
            attempt_started = time.time()
            try:
                tun = _open_tunnel(egress, UPSTREAM_HOST, UPSTREAM_PORT)
                tun.sendall(up_head + req_body)
                raw_head, rest = _read_head(tun)
                status_line, status, header_lines, headers = _split_head(raw_head)
                while 100 <= status < 200 and status != 101:
                    # 1xx 中间响应（100-continue 等）：跳过读下一头，重试仍合法
                    raw_head, rest = _read_head(tun, rest)
                    status_line, status, header_lines, headers = \
                        _split_head(raw_head)
                if status == 429:
                    fault = "rate429"
                    reader = _ConnReader(tun, rest)
                    sink = bytearray()
                    trunc = False

                    def _cap(data, sink=sink):
                        sink += data

                    try:
                        framing, flen = _framing_of(self.command, status, headers)
                        if framing == "chunked":
                            _pump_chunked(reader, _cap)
                        elif framing == "fixed":
                            _pump_fixed(reader, _cap, flen)
                        elif framing == "eof":
                            raw, trunc = reader.read_until_eof(_ERR_BODY_CAP)
                            sink += raw
                    except (_TransportFault, ValueError):
                        # 错误体没读全也无妨：标记截断，终态退 close-delimited
                        trunc = True
                    if len(sink) > _ERR_BODY_CAP:
                        del sink[_ERR_BODY_CAP:]
                        trunc = True
                    retry_after_ms = _retry_after_ms(headers)
                    last_429 = (status_line, header_lines, headers,
                                bytes(sink), trunc, via_at_attempt)
                else:
                    # SSE 事件间可长间隔：体泵阶段解除超时，流式不被 60s 掐断
                    tun.settimeout(None)
                    live = (tun, _ConnReader(tun, rest), status_line,
                            status, header_lines, headers)
            except _TransportFault as e:
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
                                          self.command, status, t0, egress,
                                          rotate_actions, retries)
                finally:
                    try:
                        tun.close()
                    except OSError:
                        pass
                return

            # ── 故障，且尚未向客户端回传任何响应字节 → 可换 ok 重试 ──
            if retries >= MAX_ROTATE_RETRIES:
                if fault == "rate429" and last_429 is not None:
                    sl, hl, hd, body, trunc, via = last_429
                    self._err_snippet = _err_snippet(body)
                    try:
                        self._relay_head_body(sl, hl, hd, None, body, trunc,
                                              self.command, 429, t0, via,
                                              rotate_actions, retries,
                                              final="no_ok_channel")
                    except Exception:  # noqa: BLE001
                        self._finish_log(t0, 429, via, rotate_actions,
                                         retries, "stream_broken")
                else:
                    self._err_snippet = (
                        "upstream transport failure (no ok channel after "
                        "%d retries)" % retries)
                    self._send_simple(
                        502, b"asip-l7: upstream transport failure "
                             b"(no ok channel after %d retries)\n" % retries)
                    self._finish_log(t0, 502, via_at_attempt, rotate_actions,
                                     retries, "no_ok_channel")
                return

            rot_req = {
                "req_id": req_id,
                "reason": fault,
                "egress_proxy_url": egress,
                # 本发尝试的开始时刻（不是首发时刻）：gate 的 already_retired
                # 判据 = switched_at > request_started_at —— 429 属于换线前的
                # 旧渠道才不烧新渠道；重试发出晚于换线时必须报新时刻，
                # 否则恒判迟到、永远不换渠道。
                "request_started_at": attempt_started,
            }
            if retry_after_ms is not None:
                rot_req["retry_after_ms"] = retry_after_ms
            resp = _call_rotate(rot_req)
            outcome = str(resp.get("reason") or "-")
            switched = bool(resp.get("switched"))
            sw_url = str(resp.get("switch_to_url") or "").strip() or None
            rotate_actions.append({
                "reason": fault, "outcome": outcome,
                "switched": switched, "switch_to_url": sw_url,
                # 换到的是 WARP 隧道还是池节点（人工排查用）
                "switch_to_kind": (
                    str(resp.get("switch_to_kind") or "").strip() or None),
                "switch_to_node": (
                    str(resp.get("switch_to_node") or "").strip() or None),
            })
            if switched and sw_url:
                egress = sw_url
            elif switched:
                # 回包没给 url 但 selector 已换：重试仍走默认出口（换 ok 协议）
                egress = DEFAULT_EGRESS_URL
            # switched=false（already_retired/coalesced/no_healthy_node/
            # borrowed_skip/rotate 调用失败）：不换新通道，原地重试一次 ——
            # already_retired 时新渠道已在 selector 上（探测窗语义）。
            retries += 1

    # 方法别名：任意动词都走同一条拦截路径
    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _one


def _retry_after_ms(headers: dict):
    """429 的 Retry-After（秒）→ 毫秒；HTTP-date/非法值返回 None（字段可省）。"""
    ra = headers.get("retry-after")
    if not ra:
        return None
    try:
        return int(float(ra.strip()) * 1000)
    except ValueError:
        return None


# ── 流量视图（asip oc l7 的呈现层）────────────────────────────────

def _err_snippet(body: bytes, limit: int = 160) -> str:
    """上游错误体摘要：去不可打印字符、折叠空白，截 limit 字符。"""
    txt = body.decode("utf-8", "replace")
    txt = "".join(ch for ch in txt if ch.isprintable() or ch in " \t")
    return " ".join(txt.split())[:limit]


def _fmt_bytes(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    if n < 1024:
        return "%dB" % n
    if n < 1024 * 1024:
        return "%.1fK" % (n / 1024.0)
    return "%.1fM" % (n / 1024.0 / 1024.0)


def format_entry(d: dict, mode: str = "pretty") -> str:
    """JSONL 条目 → 流量视图行。mode: pretty（访问日志风格）| raw（JSONL）。"""
    if mode == "raw":
        return json.dumps(d, ensure_ascii=False)
    st = d.get("status") or 0
    final = str(d.get("final") or "")
    ras = d.get("rotate_actions") or []
    tstr = time.strftime("%H:%M:%S", time.localtime(d.get("ts") or 0))
    out = "%s  %-6s %-38s %3s %6dms" % (
        tstr, d.get("method") or "?", str(d.get("path") or "?")[:38],
        st, int(d.get("ms") or 0))
    if d.get("req_bytes") is not None or d.get("resp_bytes") is not None:
        out += "  ↑%s ↓%s" % (_fmt_bytes(d.get("req_bytes") or 0),
                              _fmt_bytes(d.get("resp_bytes") or 0))
    if d.get("retried"):
        out += "  ×%d换ok" % int(d["retried"])
    if ras:
        acts = ",".join(
            "%s→%s" % (a.get("reason") or "?", a.get("outcome") or "?")
            for a in ras if isinstance(a, dict))
        out += "  [%s]" % acts
    if final and final != "ok":
        out += "  %s" % final
    err = str(d.get("err") or "")
    if err:
        out += "  | %s" % err[:120]
    return out


def view_main(argv: list) -> int:
    """JSONL stdin → 流量视图 stdout（asip oc l7 的过滤/呈现）。

    argv = [mode, filt]：mode: pretty|raw；filt: all|fail|rot
    （fail = status≥400 或 final≠ok；rot = 换过渠道的请求）。
    旧日志行（无 req_bytes/resp_bytes/err）照常可读。"""
    mode = argv[0] if argv and argv[0] in ("pretty", "raw") else "pretty"
    filt = argv[1] if len(argv) > 1 and argv[1] in ("all", "fail", "rot") \
        else "all"
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            if mode == "raw":
                print(line)
            continue
        st = d.get("status") or 0
        final = str(d.get("final") or "")
        ras = d.get("rotate_actions") or []
        if filt == "fail" and not (isinstance(st, int) and st >= 400) \
                and final == "ok":
            continue
        if filt == "rot" and not ras:
            continue
        print(format_entry(d, mode))
    return 0


# ── 入口 ──────────────────────────────────────────────────────────────

class _ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(host: str = None, port: int = None) -> _ThreadingServer:
    """绑定监听套接字（host/port 缺省读模块常量；port=0 → 系统分配，测试用）。"""
    return _ThreadingServer((host or LISTEN_HOST,
                             LISTEN_PORT if port is None else port), _Handler)


def main() -> None:
    srv = make_server(LISTEN_HOST, LISTEN_PORT)
    print("asip-opencode-l7 listening on http://%s:%d → https://%s:%d "
          "via %s (rotate=%s)"
          % (LISTEN_HOST, srv.server_address[1], UPSTREAM_HOST, UPSTREAM_PORT,
             DEFAULT_EGRESS_URL, ROTATE_SOCK), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--view":
        sys.exit(view_main(sys.argv[2:]))
    main()
