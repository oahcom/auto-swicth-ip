#!/usr/bin/env python3
"""备线可服务性探针 — 经目标隧道直连 opencode 免费层，判这条线此刻能不能用。

用法:
    python3 probe_warp.py                    # 探 config 里全部成员
    python3 probe_warp.py --tag WARP-CF-C    # 只探一条
    python3 probe_warp.py --repeat 3         # 连探三次（判 429 是真限流还是抖动）
    python3 probe_warp.py --check-fingerprint  # 校验指纹定义仍与 9router 源码一致

只读探测：每条隧道一次 POST，不切线、不碰生产状态文件。退出码非 0 表示有隧道不可用。

背景：主动轮换此前是盲切（config.warp 注释），因为直连上游会被 403
FreeTierError 拒掉。实测定位到唯一门控项是请求体里 `read`/`shell` 两个工具
的逐字定义（消融：含工具 200 / 去工具 403），以及系统提示里的 `<env>` 块和
**TLS 栈**：同一出口、同一分钟、同一份 body，Bun 200 / curl 429（9router
commit bfd5ad66 实测）。因此请求必须走 Bun 的 HTTP 栈，不能用 curl。

per-tunnel 是 SOCKS5（1080-1099），Bun fetch 只认 HTTP 代理，故每次探测先在
本地起一个 CONNECT→SOCKS5 的桥（probe_fetch.mjs 负责 Bun 侧）。会话按「端口」
稳定复用（9router 的 stable session 语义，TTL 2h），遇到 quota 才换一代重试一次
（对应 opencode.js 的 rotateSession + 一次重试）。会话表落 `.probe_sessions.json`。
429 响应会解析 `Retry-After`（头或体），输出该出口的确切封禁剩余秒数。
"""
import argparse
import glob
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

from asip.atomic_io import write_json
from asip.egress import probe_egress
from asip.proc import run as run_proc
from config import CFG

# 只复刻 open-sse/executors/opencode.js 的 UA 与指纹工具定义（后者见下方
# FINGERPRINT_TOOLS，由 --check-fingerprint 自动比对源码）。其余头（x-opencode-project
# / b3 / traceparent）本文件现在同步 9router 的派生逻辑：project 按身份稳定派生，
# b3/traceparent 每请求新生成（标准追踪语义）。
UA = "opencode/latest/2.0.26/cli"
FREEMODEL = "deepseek-v4.1-flash-free"
URL = "https://opencode.ai/inference/v1/chat/completions"
ENV_MARKER = ("<env>\n"
              "  Working directory: /workspace\n"
              "  Workspace root folder: /workspace\n"
              "</env>")
BUN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_fetch.mjs")
SESSION_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  ".probe_sessions.json")
SESSION_TTL_SEC = 2 * 60 * 60
ROTATE_DELAY_SEC = 1.5
# 429 封禁时长：头优先于体，对齐 9router parseRetryAfterMs 的取数顺序。
RETRY_HEADERS = ("retry-after", "x-ratelimit-reset-after", "reset-after",
                 "x-retry-after", "x-ratelimit-reset")
RETRY_BODY_KEYS = ("retry_after", "retryAfter", "retry_after_ms", "retryAfterMs",
                   "reset_after", "resetAfter", "reset_after_ms", "resetAfterMs")
MAX_RETRY_SEC = 86400   # 与 9router 的 maxCooldownMs 一致，防上游给出荒谬值
_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_MASK48 = (1 << 48) - 1

def _derive_project_id(identity: str) -> str:
    """Stable project id derived from downstream identity (session/connection)."""
    material = (identity or "").strip()
    if not material:
        return "global"
    digest = hashlib.sha256(f"opencode-project\0{material}".encode()).hexdigest()[:24]
    return f"ocp_{digest}"

def _build_trace_headers() -> dict:
    """Fresh W3C trace headers per request."""
    import secrets as _secrets
    def _hex(n): return _secrets.token_hex(n)
    return {
        "b3": f"{_hex(16)}-{_hex(8)}-1-{_hex(8)}",
        "traceparent": f"00-{_hex(16)}-{_hex(8)}-01",
    }

# open-sse/utils/opencodeFingerprint.js — 上游逐字比对 description 与 parameters，
# 不得改写或重排。只有 CLI 升级才变，且会先打挂 9router 自身。
FINGERPRINT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read the contents of a file or directory. Supports text files, images, and PDFs. Images and PDFs are presented directly to the model. Each text line is prefixed by its 1-based line number as <line>: <content>. The prefix is for reference and is not part of the file content. Directory entries are returned one per line. Use offset and limit to read large files or directories in sections. Prefer one larger read over many small slices, and use grep to find specific content in large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory to read"},
                    "offset": {"type": "integer", "minimum": 0,
                               "description": "The line or directory entry to start reading from (1-based)"},
                    "limit": {"type": "integer", "minimum": 0,
                              "description": "The maximum number of lines or directory entries to read (defaults to and capped at 2000)"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute a shell command and return its output. Commands run on Linux using bash. Quote file paths containing spaces or special characters. Prefer dedicated tools over shell commands when possible. When output is large, the full result is saved to a file and a truncated preview is returned. Rely on automatic truncation unless filtering the output is more useful. Commands accept an optional timeout, background commands have no timeout by default. Background commands return immediately, and you will be notified when they complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command string to execute"},
                    "workdir": {"type": "string",
                                "description": "Working directory to execute the command in. Defaults to the current working directory. When possible, avoid changing directories in the command and set the working directory here instead."},
                    "timeout": {"type": "integer", "minimum": 0,
                                "description": "Timeout in milliseconds. Set to 0 to disable the timeout. Defaults to 120000 for foreground commands. Background commands have no timeout by default."},
                    "background": {"type": "boolean",
                                   "description": "Run the command in the background and return immediately (useful for dev servers and long-running builds). You do not need to use '&' at the end of the command when using this parameter. You will be notified when it completes. DO NOT poll for completion."},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
]


def _session_id() -> str:
    """上游解码前 12 位 hex 并拒绝过大的 counter（实测 0..16 通过，>=100 拒），必须是 1。"""
    prefix = ~(int(time.time() * 1000) * 0x1000 + 1) & _MASK48
    return "ses_" + format(prefix, "012x") + "".join(secrets.choice(_B62) for _ in range(14))


_SESS_LOCK = threading.Lock()


def _load_sessions() -> dict:
    try:
        with open(SESSION_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_sessions(data: dict) -> None:
    try:
        write_json(SESSION_STATE_FILE, data)
    except OSError as e:
        print(f"⚠ 会话表写入失败: {e}", file=sys.stderr)


def _session_for(port: int, rotate: bool = False) -> str:
    """一条隧道一个稳定会话，TTL 2h（对齐 9router stableSessionId）。

    新会话只在首次、过期或 quota 轮换时生成。每请求换会话会白烧配额，
    且上游会按会话前缀的时钟拒收旧 id —— 所以不能长期复用，必须带 TTL。
    """
    now = time.time()
    key = str(port)
    with _SESS_LOCK:
        data = _load_sessions()
        entry = data.get(key)
        if not isinstance(entry, dict):
            entry = None
        expired = entry is None or now - float(entry.get("ts") or 0) > SESSION_TTL_SEC
        if rotate or expired:
            if rotate and entry is not None and not expired:
                rotations = int(entry.get("rotations") or 0) + 1
            else:
                rotations = 0
            entry = {"session": _session_id(), "ts": now, "rotations": rotations}
            data[key] = entry
            _save_sessions(data)
        return entry["session"]


def _apply_env_marker(body: dict) -> dict:
    """Mirror opencodeFingerprint.js applyFingerprintSystemPrompt for messages bodies.

    The upstream free lane rejects a request whose system prompt lacks the full
    workspace block (workdir AND root). 9router appends/upgrades it in place; the
    probe has to send the exact same shape or it measures the wrong thing.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body
    first = messages[0] if messages else None
    if isinstance(first, dict) and first.get("role") == "system":
        content = first.get("content")
        if isinstance(content, str):
            if ENV_MARKER not in content:
                merged = f"{content}\n\n{ENV_MARKER}" if content.strip() else ENV_MARKER
                messages[0] = {**first, "content": merged}
            return body
        if isinstance(content, list):
            marked = any(isinstance(part, dict) and ENV_MARKER in (part.get("text") or "")
                         for part in content)
            if not marked:
                messages[0] = {**first, "content": [*content,
                                                    {"type": "text", "text": ENV_MARKER}]}
            return body
    body["messages"] = [{"role": "system", "content": ENV_MARKER}, *messages]
    return body


def _headers(sid: str) -> dict:
    proj = _derive_project_id(sid)
    trace = _build_trace_headers()
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer public",
        "User-Agent": UA,
        "x-opencode-client": "cli",
        "x-opencode-session": sid,
        "x-opencode-session-id": sid,
        "x-session-affinity": sid,
        "x-session-id": sid,
        "x-opencode-project": proj,
        "b3": trace["b3"],
        "traceparent": trace["traceparent"],
        "Accept": "text/event-stream",
    }


def _body() -> dict:
    body = {
        "model": FREEMODEL,
        "messages": [{"role": "user", "content": "say ok"}],
        "stream": True,
        "tools": FINGERPRINT_TOOLS,
    }
    return _apply_env_marker(body)


# egress_ip 仍走 curl（经 asip.egress）：Cloudflare trace 不校 TLS 指纹。模型层
# 请求不能走 curl：同一出口、同一分钟、同一份 body，Bun 200 / curl 429
# （9router bfd5ad66）。daemon 跑在 systemd 的 /usr/bin/python3，没有 PySocks，
# requests 遇 socks5h:// 会立刻 InvalidSchema；curl --proxy 只留给出口 IP 探测。

def egress_ip(port: int, timeout: float = 10.0) -> str:
    try:
        result = probe_egress(socks_port=port, timeout=timeout)
    except Exception:
        return "?"
    return result.ip or "?"


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("socks connection closed")
        buf += chunk
    return buf


def _socks5_connect(socks_port: int, host: str, port: int, timeout: float = 15.0) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", socks_port), timeout=timeout)
    try:
        s.settimeout(30)
        s.sendall(b"\x05\x01\x00")
        if _recv_exact(s, 2) != b"\x05\x00":
            raise OSError("socks auth rejected")
        name = host.encode("idna")
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack("!H", port))
        reply = _recv_exact(s, 4)
        if reply[1] != 0:
            raise OSError(f"socks connect failed ({reply[1]})")
        atyp = reply[3]
        if atyp == 1:
            _recv_exact(s, 4)
        elif atyp == 4:
            _recv_exact(s, 16)
        elif atyp == 3:
            _recv_exact(s, _recv_exact(s, 1)[0])
        else:
            raise OSError(f"socks bad address type {atyp}")
        _recv_exact(s, 2)
        return s
    except Exception:
        s.close()
        raise


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class _BridgeHandler(socketserver.BaseRequestHandler):
    """Minimal HTTP CONNECT proxy that dials through the tunnel's SOCKS5 port."""

    def handle(self) -> None:
        self.request.settimeout(30)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 16384:
                return
        parts = buf.split(b"\r\n", 1)[0].decode("latin1").split()
        if len(parts) < 2 or parts[0] != "CONNECT" or ":" not in parts[1]:
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            return
        host, _, port_text = parts[1].rpartition(":")
        upstream = None
        established = False
        try:
            upstream = _socks5_connect(self.server.socks_port, host, int(port_text))
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            established = True
            back = threading.Thread(target=_pump, args=(self.request, upstream), daemon=True)
            back.start()
            _pump(upstream, self.request)
            back.join(timeout=5)
        except Exception:
            if not established:
                try:
                    self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass


class _SocksBridge(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, socks_port: int):
        self.socks_port = socks_port
        super().__init__(("127.0.0.1", 0), _BridgeHandler)


class _LocalBridge:
    """Run the CONNECT bridge for the duration of one probe."""

    def __init__(self, socks_port: int):
        self._socks_port = socks_port
        self._server: _SocksBridge | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> str:
        self._server = _SocksBridge(self._socks_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __exit__(self, *exc_info) -> bool:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        return False


def _bun_bin() -> str | None:
    """Bun binary: env override → PATH → ~/.bun → nvm (systemd PATH has no nvm)."""
    for name in ("PROBE_BUN_BIN", "OPENCODE_BUN_BIN"):
        value = os.environ.get(name)
        if value and os.path.exists(value):
            return value
    found = shutil.which("bun")
    if found:
        return found
    homes = {os.path.expanduser("~"), pwd.getpwuid(os.getuid()).pw_dir}
    for home in homes:
        for pattern in (".bun/bin/bun", ".nvm/versions/node/*/bin/bun"):
            hits = sorted(glob.glob(os.path.join(home, pattern)))
            if hits:
                return hits[-1]
    return None


def _retry_seconds(raw, now: float | None = None) -> float | None:
    """Retry-After 值 → 秒。支持数字（秒/ms/epoch）、HTTP 日期、'4h' 短单位。"""
    if raw is None:
        return None
    now = time.time() if now is None else now
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value <= 0:
            return None
        if value > 1e12:
            # epoch 毫秒：已过去的值表示「现在就能重试」，不是 1 秒封禁。
            diff = value / 1000.0 - now
            return diff if diff > 0 else None
        if value > 1e9:
            diff = value - now
            return diff if diff > 0 else None
        return value
    text = str(raw).strip()
    if not text:
        return None
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
        return _retry_seconds(float(text), now)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        diff = parsed.timestamp() - now
        return diff if diff > 0 else None
    units = {"ms": 0.001, "s": 1, "sec": 1, "second": 1, "m": 60, "min": 60,
             "h": 3600, "hour": 3600, "d": 86400, "day": 86400}
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(ms|s|sec|second|m|min|h|hour|d|day)s?",
                     text, re.I)
    if not m:
        return None
    return float(m.group(1)) * units[m.group(2).lower()]


def parse_retry_after(retry: dict | None, head: str = "",
                      now: float | None = None) -> int | None:
    """从 429 响应头/体解析封禁剩余秒数；拿不到返回 None（调用方退回默认惩罚期）。

    上游 opencode 的 429 body 只有 FreeUsageLimitError 文案，不带时长；实际
    窗口在响应头 `retry-after`（实测 16671s ≈ 4.6h）。解析顺序对齐
    9router 的 parseRetryAfterMs：retry-after → x-ratelimit-* → 体内字段 → 文本。
    """
    if now is None:
        now = time.time()
    lower = {str(k).lower(): v for k, v in (retry or {}).items()}
    for name in RETRY_HEADERS:
        secs = _retry_seconds(lower.get(name), now)
        if secs is not None:
            return int(min(max(secs, 1.0), MAX_RETRY_SEC))
    if head:
        for key in RETRY_BODY_KEYS:
            m = re.search(rf'"{key}"\s*:\s*([0-9]+(?:\.[0-9]+)?)', head)
            if not m:
                continue
            secs = float(m.group(1)) * (0.001 if key.endswith(("_ms", "Ms")) else 1.0)
            if secs > 0:
                return int(min(max(secs, 1.0), MAX_RETRY_SEC))
        m = re.search(
            r"(?:retry|reset|try\s+again)\s*(?:after|in)\s*([0-9]+(?:\.[0-9]+)?)"
            r"\s*(ms|milliseconds?|s|seconds?|secs?|m|minutes?|mins?|h|hours?|hrs?|d|days?)?",
            head, re.I)
        if m:
            secs = _retry_seconds(f"{m.group(1)}{m.group(2) or 's'}", now)
            if secs is not None and secs > 0:
                return int(min(max(secs, 1.0), MAX_RETRY_SEC))
    return None


def _fmt_retry(sec: int | None) -> str:
    if not sec:
        return ""
    if sec >= 3600:
        return f"{sec / 3600:.1f}h"
    if sec >= 60:
        return f"{sec / 60:.0f}m"
    return f"{sec}s"


def _bun_probe(bun: str, proxy_url: str, sid: str,
               timeout: float) -> tuple[int | None, str, dict, str | None]:
    payload = {
        "url": URL,
        "method": "POST",
        "headers": _headers(sid),
        "body": json.dumps(_body()),
        "proxy": proxy_url,
        "timeoutMs": int(timeout * 1000),
    }
    fd, path = tempfile.mkstemp(prefix="probe_job_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        r = run_proc([bun, BUN_SCRIPT, path], timeout=timeout + 5)
        if r.error:
            return None, "", {}, r.error[:120]
        if r.timed_out:
            return None, "", {}, "bun timed out"
        if r.code != 0:
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            return None, "", {}, (detail[-1] if detail else f"bun rc={r.code}")[:120]
        lines = [line for line in r.stdout.splitlines() if line.strip()]
        if not lines:
            return None, "", {}, "bun produced no output"
        data = json.loads(lines[-1])
        if data.get("error"):
            return None, "", {}, str(data["error"])[:120]
        return (int(data["status"]), str(data.get("head") or ""),
                data.get("retry") or {}, None)
    except Exception as e:
        return None, "", {}, f"{type(e).__name__}: {e}"[:120]
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def classify(status: int | None, head: str) -> str:
    if "abusive traffic" in head or "FreeTierError" in head:
        return "flagged"     # 出口 IP 信誉被拉黑 → 换线有效
    if "FreeUsageLimitError" in head:
        return "quota"       # 出口 IP 配额用尽 → 换线有效
    # 200 但没读到 data: ——本质是可用线，只是首个 chunk 比 read 窗口慢
    if status == 200 or head.startswith("data:"):
        return "OK"
    return "err"


def probe(port: int, timeout: float) -> tuple[str, float, str, int | None]:
    t0 = time.time()
    bun = _bun_bin()
    if not bun:
        return "unreachable", 0.0, "bun not found (set PROBE_BUN_BIN)", None
    try:
        with _LocalBridge(port) as proxy_url:
            session = _session_for(port)
            status, head, retry, err = _bun_probe(bun, proxy_url, session, timeout)
            if err is not None:
                return "unreachable", round(time.time() - t0, 1), f"bun: {err}"[:100], None
            verdict = classify(status, head)
            if verdict != "quota":
                return (verdict, round(time.time() - t0, 1), head.strip()[:100],
                        parse_retry_after(retry, head))
            # quota 可能是会话级（9router 实测轮换 epoch 能恢复），换一代再试一次；
            # 仍是 quota 才算出口级。对齐 opencode.js 的 rotateSession + 一次重试。
            session = _session_for(port, rotate=True)
            time.sleep(ROTATE_DELAY_SEC)
            status, head, retry, err = _bun_probe(bun, proxy_url, session, timeout)
            if err is not None:
                return ("unreachable", round(time.time() - t0, 1),
                        f"bun(retry): {err}"[:100], None)
            return (classify(status, head), round(time.time() - t0, 1),
                    head.strip()[:100], parse_retry_after(retry, head))
    except Exception as e:
        return ("unreachable", round(time.time() - t0, 1),
                f"{type(e).__name__}: {e}"[:100], None)


def check_fingerprint() -> int:
    """冻结的定义是否仍与 9router 源码逐字一致。源码升级后这里会先红。"""
    import os
    import re
    src = os.environ.get("OPENCODE_FINGERPRINT_JS") or os.path.join(
        os.path.dirname(CFG["9router"]["db_path"]), "..", "open-sse/utils/opencodeFingerprint.js")
    src = os.path.normpath(src)
    if not os.path.exists(src):
        print(f"✗ 未找到 9router 源码: {src}")
        return 1
    text = open(src, encoding="utf-8").read()
    # <env> 块和工具定义一样是上游门控项：只带 Working directory 的旧块实测
    # 直接被 429 拒。源码升级后这里要能和工具定义一起先红。
    env_lines = ["Working directory: /workspace", "Workspace root folder: /workspace"]
    if "OPENCODE_ENV_MARKER" not in text:
        print("✗ 9router 源码没有 OPENCODE_ENV_MARKER，指纹规则已变")
        return 1
    missing_env = [line for line in env_lines if line not in text or line not in ENV_MARKER]
    if missing_env:
        print(f"✗ <env> 标记与 9router 源码不一致，缺: {missing_env}")
        return 1
    # 上游比对的是 description 与 parameters 两者（opencodeFingerprint.js 的
    # isFingerprintDefinition）；只比 description 会漏掉 schema 变更 —— 那种
    # 情况下线上探测全 403，而这里仍报「一致」。JS 对象字面量与 json.dumps
    # 只差空白和键的引号，去掉这两样即可逐字比对。
    def _norm(s: str) -> str:
        # 连同逗号一起去掉：JS 对象字面量的尾逗号在 Python 序列化里不存在，
        # 而两边同时去逗号是等价的，不必去追哪几个是尾逗号。
        return re.sub(r"[\s,\"]+", "", s)

    flat = _norm(text)
    missing = [t["function"]["name"] for t in FINGERPRINT_TOOLS
               if _norm(t["function"]["description"]) not in flat
               or _norm(json.dumps(t["function"]["parameters"],
                                   ensure_ascii=False)) not in flat]
    if missing:
        print(f"✗ 以下工具定义与 9router 源码不一致，需重新抄录: {missing}")
        return 1
    print(f"✓ {len(FINGERPRINT_TOOLS)} 个工具定义 + <env> 标记与 9router 源码逐字一致")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", help="只探指定隧道，如 WARP-CF-C")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=25)
    ap.add_argument("--check-fingerprint", action="store_true")
    args = ap.parse_args()

    if args.check_fingerprint:
        return check_fingerprint()

    members = CFG["warp"]["members"]
    if args.tag:
        members = [m for m in members if m["tag"] == args.tag]
        if not members:
            print(f"✗ config 里没有 {args.tag}")
            return 2

    bad = 0
    print(f"{'隧道':<12}{'出口IP':<28}{'判定':<13}{'耗时':<8}")
    for m in members:
        ip = egress_ip(m["port"])
        verdicts = []
        for i in range(args.repeat):
            verdict, sec, _, retry_after = probe(m["port"], args.timeout)
            verdicts.append(verdict)
            label = f"{verdict}（{_fmt_retry(retry_after)}）" if retry_after else verdict
            print(f"{m['tag']:<12}{ip:<28}{label:<13}{sec}s")
            if verdict != "OK" and i < args.repeat - 1:
                time.sleep(1)
        if any(v != "OK" for v in verdicts):
            bad += 1
    print(f"\n{len(members) - bad}/{len(members)} 条可用")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
