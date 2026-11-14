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
本地起一个 CONNECT→SOCKS5 的桥（probe_fetch.mjs 负责 Bun 侧）。会话按「出口 IP」
维护：一个 IP 固定 15 个随机会话，IP 内每 10 次切一个，IP 一变立刻重新生成 15 个
（绝不跨 IP 共用；同 IP 重访复用同一组，distinct 会话数恒 ≤15 < 40）。遇到 quota
重试时在组内立即切下一个（对应 opencode.js 的 rotateSession + 一次重试）。
会话池在内存里按 IP 维护，不落盘。
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
# 9router 运行期拼的 ENV_FINGERPRINT_PROMPT（open-sse/utils/opencodeFingerprint.js）。
# 上游把这一整块当门控项，**缺 heading 行或任一行都回 429 FreeUsageLimitError**——
# 那个 429 带 Retry-After 到 UTC 零点，看起来像固定时间窗限流，实测与出口 IP
# 无关。旧版本这里只抄了 4 行（漏掉 heading、git repo、Platform、Prefer tmp），于是
# 每一条探测都被判 quota，19 条线全被打成坏线。
# 值取 9router 服务进程的取值：platform = linux、tmp = os.tmpdir()/opencode。
# --check-fingerprint 会与源码逐行比对。
# 默认值必须与 9router 生产 marker 完全一致（open-sse/utils/
# opencodeFingerprint.js 的 OPENCODE_ENV_WORKING_DIR 默认 /home/user/project）。
# 上游按这一行路径做门控，且是**精确字符串**判定：2026-11-10 实测同一隧道、
# 同一分钟、同一 session，只把 Working directory 写成 /home/administrator/.9router
# 就 429 FreeUsageLimitError（伪装成固定时间窗限流，带 Retry-After 到次日），
# 改成 /home/user/project 立刻 200；把同一个字符串加个后缀 .9router2 也 200 ——
# 是本机这条路径被拉黑，不是用户名或配额。旧默认值取家目录推导，家目录在
# /home/administrator 下的机器（本机、软路由）探测全被判 quota，台账一天烧掉
# 1.3 万个「从未成功过」的脏 IP，把池子打空、还反过来触发无限换线。探测必须
# 发与生产同一份 marker，否则测的不是同一条腿。
# 每次请求现生成一条随机路径：base 只提供「看起来像真实项目」的前缀，后缀是
# 2~3 位 CSPRNG 随机字符（secrets，不是时间取模那种可预测值）。目的是让路径
# 永远不落进上游的静态黑名单 —— INC-018 的 /home/administrator/.9router 就是
# 精确字符串拉黑，任何固定值都可能哪天被收进去。
# 代价（已知并接受）：探测与生产不再发同一个值。若上游只按「路径是否在黑名单」
# 判定，两边都能过；若它校验路径存在性/格式，随机后缀本身就是破绽，且探测结论
# 也不再代表生产。需要钉死单值时用 OPENCODE_PROBE_WORKDIR / OPENCODE_ENV_WORKING_DIR。
ENV_WORKDIR_BASES = (
    "/home/user/project",
    "/home/user/workspace",
    "/home/user/src",
    "/workspace/app",
    "/srv/app",
)
ENV_TAG = "<env>"
_RAND_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_ENV_WORKDIR_PIN = (os.environ.get("OPENCODE_PROBE_WORKDIR")
                    or os.environ.get("OPENCODE_ENV_WORKING_DIR") or "")
ENV_TMP_DIR = os.environ.get("OPENCODE_PROBE_TMPDIR", "/tmp/opencode")


def env_workdir(now: float | None = None) -> str:
    """每次调用生成一条新的随机工作目录（CSPRNG）。"""
    if _ENV_WORKDIR_PIN:
        return _ENV_WORKDIR_PIN
    base = secrets.choice(ENV_WORKDIR_BASES)
    width = 2 + secrets.randbelow(2)          # 2~3 位
    suffix = "".join(secrets.choice(_RAND_ALPHABET) for _ in range(width))
    return f"{base}-{suffix}"


def env_marker(now: float | None = None) -> str:
    wd = env_workdir(now)
    return "\n".join([
        "Here is some useful information about the environment you are running in:",
        "<env>",
        f"  Working directory: {wd}",
        f"  Workspace root folder: {wd}",
        "  Is directory a git repo: no",
        "  Platform: linux",
        f"  Prefer {ENV_TMP_DIR} over generic system temporary directories such as /tmp; "
        "it is pre-created and approved for external access.",
        "</env>",
    ])


# 模块级快照，仅供 --check-fingerprint / 测试读取形状；请求路径一律现算。
ENV_WORKING_DIR = env_workdir()
ENV_MARKER = env_marker()
BUN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_fetch.mjs")
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


SESSION_POOL_SIZE = 15
SESSION_POOL_SWITCH_EVERY = 10
SESSION_POOL_MAX_ENTRIES = 8192

_SESSION_POOLS: dict[str, dict] = {}
_SESSION_POOL_LOCK = threading.Lock()


def _session_id() -> str:
    """上游解码前 12 位 hex 并拒绝过大的 counter（实测 0..16 通过，>=100 拒），必须是 1。"""
    prefix = ~(int(time.time() * 1000) * 0x1000 + 1) & _MASK48
    return "ses_" + format(prefix, "012x") + "".join(secrets.choice(_B62) for _ in range(14))


def _mint_pool() -> list[str]:
    pool = [_session_id() for _ in range(SESSION_POOL_SIZE)]
    secrets.SystemRandom().shuffle(pool)
    return pool


def _session_pool(ip: str) -> dict:
    """按出口 IP 取（或新建）一组会话。调用方须持有 _SESSION_POOL_LOCK。"""
    now = time.time()
    entry = _SESSION_POOLS.get(ip)
    if entry is None:
        entry = {"sessions": _mint_pool(), "idx": 0, "used": 0, "ts": now}
        _SESSION_POOLS[ip] = entry
        overflow = len(_SESSION_POOLS) - SESSION_POOL_MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(_SESSION_POOLS, key=lambda k: _SESSION_POOLS[k]["ts"])
            for k in oldest[:overflow]:
                _SESSION_POOLS.pop(k, None)
    return entry


def _pick_session(ip: str | None = None, rotate: bool = False) -> str:
    """取当前出口 IP 的会话：一 IP 一组 15 个随机 id，IP 内每 10 次切一个。

    - IP 不变复用同一组；同 IP 重访也不重新生成，distinct 会话数恒为 ≤15（<40）。
    - IP 一变（换线/重连）立刻生成新的 15 个随机会话，绝不跨 IP 共用。
    - 组内每 SESSION_POOL_SWITCH_EVERY 次切下一个；15×10=150 正好覆盖单出口
      一轮约 150 格的探测额度，使每个会话只承担约 10 次请求。
    - 150 次后回绕到组内第 1 个（均分循环），不重新生成；IP 不变时复用同一组。
    - rotate=True（quota 重试）立即切到组内下一个。
    """
    key = (ip or "").strip()
    if not key or key == "?":
        key = "-"
    now = time.time()
    with _SESSION_POOL_LOCK:
        entry = _SESSION_POOLS.get(key)
        if entry is None:
            entry = _session_pool(key)
        entry["ts"] = now
        if rotate or entry["used"] >= SESSION_POOL_SWITCH_EVERY:
            entry["idx"] = (entry["idx"] + 1) % len(entry["sessions"])
            entry["used"] = 0
        entry["used"] += 1
        return entry["sessions"][entry["idx"]]


def _apply_env_marker(body: dict) -> dict:
    """Mirror opencodeFingerprint.js applyFingerprintSystemPrompt for messages bodies.

    The upstream free lane rejects a request whose system prompt lacks the full
    workspace block (workdir AND root). 9router appends/upgrades it in place; the
    probe has to send the exact same shape or it measures the wrong thing.
    """
    marker = env_marker()
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body
    first = messages[0] if messages else None
    if isinstance(first, dict) and first.get("role") == "system":
        content = first.get("content")
        if isinstance(content, str):
            if ENV_TAG not in content:
                merged = f"{content}\n\n{marker}" if content.strip() else marker
                messages[0] = {**first, "content": merged}
            return body
        if isinstance(content, list):
            marked = any(isinstance(part, dict) and ENV_TAG in (part.get("text") or "")
                         for part in content)
            if not marked:
                messages[0] = {**first, "content": [*content,
                                                    {"type": "text", "text": marker}]}
            return body
    body["messages"] = [{"role": "system", "content": marker}, *messages]
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
    """Bun binary: env override → PATH → ~/.bun → ~/.local/bin → nvm.

    systemd 的 PATH 没有 nvm/~/.local/bin，所以这里按家目录兜底找。
    """
    for name in ("PROBE_BUN_BIN", "OPENCODE_BUN_BIN"):
        value = os.environ.get(name)
        if value and os.path.exists(value):
            return value
    found = shutil.which("bun")
    if found:
        return found
    homes = {os.path.expanduser("~"), pwd.getpwuid(os.getuid()).pw_dir}
    for home in homes:
        for pattern in (".bun/bin/bun", ".local/bin/bun",
                        ".nvm/versions/node/*/bin/bun"):
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


def probe(port: int, timeout: float, ip: str | None = None) -> tuple[str, float, str, int | None]:
    t0 = time.time()
    if not ip or ip == "?":
        try:
            ip = egress_ip(port) or ""
        except Exception:
            ip = ""
        # egress_ip 失败时返回 "?"（不是空串）。它必须当成未知出口，按端口分组；
        # 否则所有探测失败的端口会共用同一个 "?" 会话池，等于跨出口复用会话。
        if not ip or ip == "?":
            ip = f"port:{port}"
    bun = _bun_bin()
    if not bun:
        # 返回独立结论而不是 unreachable：没装 bun 是探测器缺失，不是这条线
        # 不能出流量。混进 unreachable 会让整批隧道被判死、被反复重启（实测
        # 每 5 分钟一轮 20 条全部 unreachable，白烧重启与配额）。
        return "noprobe", 0.0, "bun not found (set PROBE_BUN_BIN)", None
    try:
        with _LocalBridge(port) as proxy_url:
            session = _pick_session(ip)
            status, head, retry, err = _bun_probe(bun, proxy_url, session, timeout)
            if err is not None:
                return "unreachable", round(time.time() - t0, 1), f"bun: {err}"[:100], None
            verdict = classify(status, head)
            if verdict != "quota":
                return (verdict, round(time.time() - t0, 1), head.strip()[:100],
                        parse_retry_after(retry, head))
            # quota 一律按出口判定；换一代会话只为排除上游对单次请求的门控，
            # 重试仍 quota 即确认该出口不可用。对齐 opencode.js 的一次重试。
            session = _pick_session(ip, rotate=True)
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


def _expected_env_marker(text: str) -> list[str] | None:
    """从 9router 源码重建 ENV_FINGERPRINT_PROMPT 的行列表。

    运行期取值（cwd / platform / tmp）在源码里是模板插值，这里归一成 {runtime}，
    与 _normalize_our_env_marker() 对齐后再逐行比 —— 值本身由本机决定，形状才是
    上游门控的东西。
    """
    import re
    if "ENV_FINGERPRINT_PROMPT" not in text:
        return None
    body = re.search(r"const ENV_FINGERPRINT_PROMPT = \[(.*?)\]\.join", text, re.S)
    if not body:
        return None
    consts = {}
    for name in ("ENV_HEADING", "ENV_TAG"):
        found = re.search(rf'const {name} = "([^"]*)"', text)
        if not found:
            return None
        consts[name] = found.group(1)
    lines = []
    for raw in (line.strip() for line in body.group(1).splitlines()):
        if not raw:
            continue
        raw = raw.rstrip(",").strip()
        if raw in consts:
            lines.append(consts[raw])
        elif raw.startswith("`") and raw.endswith("`"):
            # `${ENV_WORKING_DIR}` / `${process.platform}` 一类插值归一成占位符
            lines.append(re.sub(r"\$\{[^}]*\}", "{runtime}", raw[1:-1]))
        elif raw.startswith('"') and raw.endswith('"'):
            lines.append(raw[1:-1])
        else:
            lines.append("{runtime}")
    return lines


def _normalize_our_env_marker() -> list[str]:
    """把本地 ENV_MARKER 的运行期取值替换成 {runtime}，只留形状用于比对。"""
    out = []
    for line in env_marker().split("\n"):
        if line.startswith("  Working directory: "):
            line = "  Working directory: {runtime}"
        elif line.startswith("  Workspace root folder: "):
            line = "  Workspace root folder: {runtime}"
        elif line.startswith("  Platform: "):
            line = "  Platform: {runtime}"
        elif line.startswith("  Prefer "):
            line = line.replace(f"  Prefer {ENV_TMP_DIR} ", "  Prefer {runtime} ", 1)
        out.append(line)
    return out


def check_fingerprint() -> int:
    """冻结的定义是否仍与 9router 源码逐字一致。源码升级后这里会先红。"""
    import os
    import re
    # `db_path` 在 `<数据目录>/db/data.sqlite`，它的 dirname 是数据目录，源码
    # **不在那里**：本机实测 `~/.9router/` 只有 auth/db/logs，源码树在
    # `~/9router/`。旧写法拼出 `~/.9router/open-sse/...`，永远「未找到」，
    # 而返回值 1 没人看 —— 指纹漂移等于完全没检查。
    home = os.path.expanduser("~")
    cands = [os.environ.get("OPENCODE_FINGERPRINT_JS"), os.path.join(
        home, "9router", "open-sse/utils/opencodeFingerprint.js"), os.path.join(
        home, ".9router", "open-sse/utils/opencodeFingerprint.js")]
    src = next((c for c in cands if c and os.path.exists(c)), cands[1])
    src = os.path.normpath(src)
    if not os.path.exists(src):
        print(f"✗ 未找到 9router 源码: {src}")
        return 1
    text = open(src, encoding="utf-8").read()
    # <env> 块和工具定义一样是上游门控项，而且是**逐行**门控：只要块形状不对就回
    # 429 FreeUsageLimitError（伪装成每日配额，与出口 IP 无关）。旧版本这里只检查
    # 两行 "Working directory / Workspace root folder" 是否存在，于是漏掉 heading、
    # git repo、Platform、Prefer tmp 这四行的漂移一路带到线上 —— 每条探测恒 429，
    # 19 条线全被判坏线，换 IP 换了几千次都是量一个假信号。所以现在整块逐行比。
    expected_env = _expected_env_marker(text)
    if expected_env is None:
        print("✗ 9router 源码没有 ENV_FINGERPRINT_PROMPT（或无法解析），指纹规则已变")
        return 1
    ours = _normalize_our_env_marker()
    if ours != expected_env:
        diff = [f"行{i + 1}: 源码={e!r} 本文件={o!r}"
                for i, (e, o) in enumerate(zip(expected_env, ours)) if e != o]
        if len(ours) != len(expected_env):
            diff.append(f"行数不一致: 源码 {len(expected_env)} 行, 本文件 {len(ours)} 行")
        print("✗ <env> 标记与 9router 源码不一致: " + "; ".join(diff))
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
    print(f"✓ {len(FINGERPRINT_TOOLS)} 个工具定义 + {len(expected_env)} 行 <env> 标记与 9router 源码逐字一致")
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
