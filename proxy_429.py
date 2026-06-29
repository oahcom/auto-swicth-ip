#!/usr/bin/env python3
"""
429 拦截型 HTTP 正向代理。
监听 :7891 → sing-box:7890。遇到 429/502/503/504 或连接错误时最多重试 5 次，
每次重试切换 Clash 节点。
"""

import asyncio
import json
import os
import random
import socket
import sys
import threading
import time
import logging
import logging.handlers
import urllib.request
import urllib.error

import sqlite3

from config import CFG, SINGBOX_SECRET, BAD_NODES_TTL, SINGBOX_API, SELECTOR, MIXED_PORT, LISTEN_PORT, LOG_FORMAT

UPSTREAM = ("127.0.0.1", MIXED_PORT)
LISTEN = ("127.0.0.1", LISTEN_PORT)

# 重试配置
MAX_RETRIES = 5
RETRY_ON_STATUS = {429, 500, 501, 502, 503, 504, 505}
RETRY_SLEEP_BASE = 1  # 秒，每次重试翻倍

# 尺寸/超时限制
REQUEST_LINE_TIMEOUT = 30
UPSTREAM_CONNECT_TIMEOUT = 10
UPSTREAM_READLINE_TIMEOUT = 30
SINGBOX_API_TIMEOUT = 5
CONNECT_RESP_TIMEOUT = 10
MAX_LINE_BYTES = 8 * 1024
MAX_HEADER_BYTES = 32 * 1024
MAX_CONTENT_LENGTH = 64 * 1024 * 1024
HEADER_READ_DEADLINE = 10
BODY_READ_DEADLINE = 60
PIPE_READ_TIMEOUT = 600  # 数据块间最长空闲时间
DRAIN_MAX_SEC = 10  # 重试时 drain 上限
PIPE_BUF_SIZE = 65536

# 共享坏节点文件：proxy_429 写入，daemon 读取
BAD_NODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bad_nodes.json")

# 节点切换锁，防止并发竞争
_SWITCH_LOCK = asyncio.Lock()

# --- 绕过状态（proxy_429 控制 9router outboundProxyEnabled）---
_bypass_active = False
_bypass_lock = asyncio.Lock()
_bypass_check_task: asyncio.Task | None = None

# --- 坏节点追踪（通过 .bad_nodes.json 与 daemon 共享）---
# 内存缓存 + 定期写入；原子写入通过 rename 实现。
_bad_nodes_lock = asyncio.Lock()
_bad_nodes_cache: dict[str, float] = {}  # 节点 -> 过期时间戳
_bad_nodes_dirty = False
_bad_nodes_flush_task: asyncio.Task | None = None
_bad_nodes_file_mtime: float = 0  # 追踪文件修改时间，用于检测 daemon 写入的新条目


async def _load_bad_nodes() -> dict:
    """从文件加载坏节点，mtime 变化时重新读取。
    daemon 会向同一文件写入新条目；通过 mtime 检测来拾取。
    读取时过滤过期条目，防止文件膨胀。"""
    global _bad_nodes_cache, _bad_nodes_file_mtime
    try:
        cur_mtime = os.path.getmtime(BAD_NODES_FILE) if os.path.exists(BAD_NODES_FILE) else 0
    except OSError:
        cur_mtime = 0
    async with _bad_nodes_lock:
        if cur_mtime != _bad_nodes_file_mtime:
            now = time.time()
            if os.path.exists(BAD_NODES_FILE):
                def _sync_load():
                    with open(BAD_NODES_FILE) as f:
                        data = json.load(f)
                    # 过滤过期条目，保留未过期的
                    return {n: exp for n, exp in data.items() if exp > now}
                _bad_nodes_cache = await asyncio.to_thread(_sync_load)
            else:
                _bad_nodes_cache = {}
            _bad_nodes_file_mtime = cur_mtime
    return _bad_nodes_cache


async def _flush_bad_nodes() -> None:
    """将脏缓存写入文件（原子写入：先写临时文件再 rename）。
    写入前过滤已过期条目，避免文件无限增长。"""
    global _bad_nodes_dirty
    if not _bad_nodes_dirty:
        return
    async with _bad_nodes_lock:
        if not _bad_nodes_dirty:
            return
        # 写入前过滤过期条目
        now = time.time()
        fresh = {n: exp for n, exp in _bad_nodes_cache.items() if exp > now}
        if len(fresh) < len(_bad_nodes_cache):
            _bad_nodes_cache.clear()
            _bad_nodes_cache.update(fresh)
        tmp = BAD_NODES_FILE + ".tmp"
        try:
            def _sync_write():
                with open(tmp, "w") as f:
                    json.dump(_bad_nodes_cache, f)
            await asyncio.to_thread(_sync_write)
            os.replace(tmp, BAD_NODES_FILE)
            _bad_nodes_dirty = False
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass


async def _mark_bad_node(node: str) -> None:
    """标记节点为坏节点，带 TTL（仅内存，写入由定期任务处理）。"""
    if not node:
        return
    global _bad_nodes_dirty
    await _load_bad_nodes()
    now = time.time()
    async with _bad_nodes_lock:
        old = _bad_nodes_cache.get(node, 0)
        if old > now:
            return
        _bad_nodes_cache[node] = now + BAD_NODES_TTL
        _bad_nodes_dirty = True
        _bad_nodes_flush_event.set()
    log(f"  🚫 Bad node: {node} (TTL {BAD_NODES_TTL}s)")


async def _get_current_bad_nodes() -> set:
    """获取当前坏节点集合（从文件支持的缓存中读取）。"""
    now = time.time()
    nodes = await _load_bad_nodes()
    return {n for n, exp in nodes.items() if exp > now}


_bad_nodes_flush_event = asyncio.Event()  # 有脏数据时 set，_periodic_flush_bad_nodes 等待


async def _periodic_flush_bad_nodes():
    """脏数据写入磁盘，由 Event 触发（去轮询）。"""
    global _bad_nodes_flush_task
    try:
        while True:
            await _bad_nodes_flush_event.wait()
            _bad_nodes_flush_event.clear()
            await _flush_bad_nodes()
    except asyncio.CancelledError:
        await _flush_bad_nodes()
        raise


log_file = os.path.join(os.path.dirname(__file__), "proxy_429.log")
_logger = logging.getLogger("proxy_429")
_logger.setLevel(logging.INFO)
_fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
_fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt=LOG_FORMAT))
_logger.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt=LOG_FORMAT))
_logger.addHandler(_sh)


def log(msg):
    _logger.info(msg)


async def _set_9router_bypass(enabled: bool) -> bool:
    """通过 SQLite json_set 原子更新 9router outboundProxyEnabled。成功返回 True。"""
    db_path = CFG["9router"]["db_path"]
    try:
        def _sync_write():
            with sqlite3.connect(db_path, timeout=5) as con:
                con.execute(
                    "UPDATE settings SET data = json_set(data, '$.outboundProxyEnabled', ?) WHERE id=1",
                    (1 if enabled else 0,),
                )
                con.commit()
            return True
        return await asyncio.to_thread(_sync_write)
    except Exception as e:
        log(f"  WARN: Failed to set 9router bypass: {e}")
        return False


async def _enable_bypass() -> None:
    """禁用 9router 代理（切换到直连模式）。上游全部不可用时调用。
    整段持有锁，防止并发请求同时写入 SQLite。"""
    global _bypass_active
    async with _bypass_lock:
        if _bypass_active:
            return
        if await _set_9router_bypass(False):
            _bypass_active = True
            log("  BYPASS enabled: 9router outboundProxyEnabled=False (direct mode)")


async def _disable_bypass() -> None:
    """重新启用 9router 代理。sing-box 恢复时调用。
    整段持有锁，防止并发监控任务同时写入 SQLite。"""
    global _bypass_active
    async with _bypass_lock:
        if not _bypass_active:
            return
        if await _set_9router_bypass(True):
            _bypass_active = False
            log("  BYPASS restored: 9router outboundProxyEnabled=True (proxy mode)")


async def _check_upstream_health() -> bool:
    """快速检查 sing-box 上游是否可达（端口 + Clash API）。"""
    # 1. 检查代理端口（异步）
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", MIXED_PORT), timeout=2
        )
        writer.close()
        await writer.wait_closed()
    except (ConnectionRefusedError, OSError, TimeoutError, asyncio.TimeoutError):
        return False
    # 2. 检查 Clash API
    data = await asyncio.to_thread(_clash_api_sync, "GET", "/proxies")
    return data is not None


async def _bypass_monitor():
    """定期检查上游健康状态；上游恢复时禁用 bypass。"""
    global _bypass_check_task
    try:
        while True:
            await asyncio.sleep(10)
            async with _bypass_lock:
                active = _bypass_active
            if not active:
                continue
            if await _check_upstream_health():
                await _disable_bypass()
            else:
                log("  BYPASS maintained: upstream still unreachable")
    except asyncio.CancelledError:
        raise


async def switch_clash_node() -> bool:
    """通过 Clash API 随机选一个活跃节点并切换。使用 asyncio.Lock 保证异步安全。
    切换后将旧节点标记为坏节点。成功返回 True。
    ponytail: 调用方在返回 False 时继续重试（当前节点可能恢复）。"""
    async with _SWITCH_LOCK:
        data = await asyncio.to_thread(_clash_api_sync, "GET", f"/proxies/{SELECTOR}")
        if not data:
            return False
        all_nodes = data.get("all", [])
        current = data.get("now", "")
        bad = await _get_current_bad_nodes()
        candidates = [n for n in all_nodes if n != current and n != "direct" and n not in bad]
        if not candidates:
            log(f"Clash switch: all {len(all_nodes)} nodes bad, cannot switch")
            return False
        chosen = random.choice(candidates)
        body = json.dumps({"name": chosen}).encode()
        ok = await asyncio.to_thread(_clash_api_sync, "PUT", f"/proxies/{SELECTOR}", body)
        if ok is not None:
            log(f"Clash switch: {current} -> {chosen}")
            await _mark_bad_node(current)
            return True
        return False


def _clash_api_sync(method: str, path: str, body: bytes | None = None) -> dict | None:
    """同步 Clash API 调用（在线程池中运行）。
    204（成功无 body）返回 {}，失败返回 None。"""
    url = f"{SINGBOX_API}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SINGBOX_SECRET}"}, method=method, data=body)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=SINGBOX_API_TIMEOUT) as resp:
            if resp.status == 204:
                return {}
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as e:
        # 脱敏：异常信息中可能泄露 Authorization Bearer token
        safe_msg = str(e).replace(SINGBOX_SECRET, "***") if SINGBOX_SECRET else str(e)
        log(f"Clash API {method} {path} failed: {type(e).__name__}: {safe_msg}")
        return None


async def pipe(r, w, timeout=PIPE_READ_TIMEOUT):
    try:
        while True:
            chunk = await asyncio.wait_for(r.read(PIPE_BUF_SIZE), timeout=timeout)
            if not chunk:
                break
            w.write(chunk)
            await w.drain()
    except (asyncio.CancelledError, asyncio.TimeoutError,
            ConnectionResetError, BrokenPipeError, OSError):
        pass
    except Exception as e:
        log(f"Pipe error: {e}")
        raise


async def _drain_reader(r) -> None:
    """排空读取器，上限 DRAIN_MAX_SEC 秒。捕获预期异常。"""
    drain_deadline = asyncio.get_running_loop().time() + DRAIN_MAX_SEC
    try:
        while True:
            remaining = drain_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            c = await asyncio.wait_for(r.read(PIPE_BUF_SIZE), timeout=remaining)
            if not c:
                break
    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
        pass


async def cancel_pipe(t):
    if not t.done():
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, OSError):
            pass


def _has_te_chunked(headers_raw: bytes) -> bool:
    """检查头部是否包含 Transfer-Encoding: chunked（处理多值）。"""
    for line in headers_raw.split(b"\r\n"):
        val = line.lower().strip()
        if val.startswith(b"transfer-encoding:"):
            te_value = val.split(b":", 1)[1].strip()
            for part in te_value.split(b","):
                if part.strip() == b"chunked":
                    return True
    return False


def _strip_header(headers_raw: bytes, name: bytes) -> bytes:
    """移除指定名称的所有头部行（不区分大小写）。"""
    lines = []
    for line in headers_raw.split(b"\r\n"):
        if not line or line.lower().startswith(name.lower() + b":"):
            continue
        lines.append(line)
    return b"\r\n".join(lines)


def _get_content_length(headers_raw: bytes) -> int:
    """解析 Content-Length。不存在时返回 0。
    校验：正数、单一值、不为负、不超限。
    无效时返回 -1（多个 CL、负值、溢出）。"""
    cl_vals = []
    for line in headers_raw.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            val_str = line.split(b":", 1)[1].strip().decode()
            try:
                v = int(val_str)
                if v < 0:
                    return -1
                cl_vals.append(v)
            except ValueError:
                return -1
    if not cl_vals:
        return 0
    if len(cl_vals) > 1:
        return -1
    if cl_vals[0] > MAX_CONTENT_LENGTH:
        return -1
    return cl_vals[0]


async def _read_body(reader, headers, peername) -> tuple[bytes, bytes]:
    """读取 HTTP body（chunked 或 Content-Length）。返回 (body, 修改后的 headers)。"""
    cl = _get_content_length(headers)
    if cl < 0:
        return (None, None)
    has_chunked = _has_te_chunked(headers)
    body = b""
    if has_chunked:
        chunks = []
        total_chunked = 0
        while True:
            chunk_line = await asyncio.wait_for(reader.readline(), timeout=BODY_READ_DEADLINE)
            chunk_line_stripped = chunk_line.strip()
            chunk_size_hex = chunk_line_stripped.split(b";")[0]
            if not chunk_line_stripped or chunk_size_hex == b"0":
                if chunk_size_hex == b"0":
                    while True:
                        t = await asyncio.wait_for(reader.readline(), timeout=BODY_READ_DEADLINE)
                        if t in (b"\r\n", b"\n", b""):
                            break
                break
            try:
                chunk_size = int(chunk_size_hex, 16)
            except ValueError:
                return (None, None)
            if total_chunked + chunk_size > MAX_CONTENT_LENGTH:
                return (None, None)
            data = await asyncio.wait_for(reader.readexactly(chunk_size + 2), timeout=BODY_READ_DEADLINE)
            chunks.append(data[:chunk_size])
            total_chunked += chunk_size
        body = b"".join(chunks)
        headers = _strip_header(headers, b"Transfer-Encoding")
        headers = _strip_header(headers, b"Content-Length")
        headers += f"\r\nContent-Length: {len(body)}\r\n\r\n".encode()
    elif cl > 0:
        body = await asyncio.wait_for(reader.readexactly(cl), timeout=BODY_READ_DEADLINE)
    return (body, headers)


async def _handle_connect(reader, writer, line, headers):
    """CONNECT 方法处理，带重试+节点切换。
    重试耗尽时启用 bypass + 直连目标服务器，保证当前请求不报错。"""
    target = None
    try:
        target = line.decode(errors="replace").strip().split(" ")[1]
    except Exception:
        pass
    for attempt in range(MAX_RETRIES):
        try:
            u_r, u_w = await asyncio.wait_for(
                asyncio.open_connection(*UPSTREAM), timeout=UPSTREAM_CONNECT_TIMEOUT)
        except (asyncio.TimeoutError, OSError) as e:
            log(f"CONNECT upstream connect fail: {e}")
            if attempt == MAX_RETRIES - 1:
                await _enable_bypass()
                if target:
                    return await _direct_connect_connect(reader, writer, line, headers, target)
                return
            if not await switch_clash_node():
                await _enable_bypass()
                if target:
                    return await _direct_connect_connect(reader, writer, line, headers, target)
                return
            await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
            continue
        try:
            u_w.write(line + headers)
            await u_w.drain()
            resp = await asyncio.wait_for(u_r.readuntil(b"\r\n\r\n"), timeout=CONNECT_RESP_TIMEOUT)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
            log(f"CONNECT upstream write/read fail: {e}")
            if attempt == MAX_RETRIES - 1:
                await _enable_bypass()
                if target:
                    return await _direct_connect_connect(reader, writer, line, headers, target)
                return
            u_w.close()
            if not await switch_clash_node():
                await _enable_bypass()
                if target:
                    return await _direct_connect_connect(reader, writer, line, headers, target)
                return
            await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
            continue
        try:
            status_code = int(resp.split(b" ")[1])
        except (IndexError, ValueError):
            status_code = 502
        if status_code in RETRY_ON_STATUS:
            if attempt < MAX_RETRIES - 1:
                log(f"CONNECT {status_code} → switching node, retrying... (attempt {attempt + 1}/{MAX_RETRIES})")
                await _drain_reader(u_r); u_w.close()
                if not await switch_clash_node():
                    await _enable_bypass()
                    if target:
                        return await _direct_connect_connect(reader, writer, line, headers, target)
                    return
                await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
                continue
            await _drain_reader(u_r); u_w.close()
            await _enable_bypass()
            if target:
                return await _direct_connect_connect(reader, writer, line, headers, target)
            return
        if status_code != 200:
            try:
                writer.write(resp); await writer.drain()
            except OSError:
                pass
            await _drain_reader(u_r); u_w.close(); return
        try:
            writer.write(resp); await writer.drain()
        except OSError:
            u_w.close(); return
        t1 = asyncio.create_task(pipe(reader, u_w))
        t2 = asyncio.create_task(pipe(u_r, writer))
        done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        await cancel_pipe(t1); await cancel_pipe(t2)
        u_w.close(); writer.close(); return
    # 全部重试耗尽 + target 解析失败（无 target 变量），静默关闭
    writer.close()


async def _close_gracefully(writer):
    """关闭连接，不发送错误响应 — bypass 已启用，下次重试走直连。"""
    try:
        writer.close()
    except OSError:
        pass


async def _direct_connect_connect(reader, writer, line, headers, target: str) -> bool:
    """上游全部不可用时，直接连接目标服务器（CONNECT 隧道）。
    成功转发数据，否则关闭连接。"""
    host, _, port_str = target.partition(":")
    port = int(port_str) if port_str else 443
    try:
        u_r, u_w = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=UPSTREAM_CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        writer.close()
        return False
    try:
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
    except OSError:
        u_w.close()
        return False
    t1 = asyncio.create_task(pipe(reader, u_w))
    t2 = asyncio.create_task(pipe(u_r, writer))
    done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
    await cancel_pipe(t1)
    await cancel_pipe(t2)
    u_w.close()
    return True


async def _direct_connect_http(writer, method: str, target: str, headers: bytes, body: bytes) -> bool:
    """上游全部不可用时，直接连接目标服务器（HTTP 转发）。
    从 target 中提取 host，建立直连 TCP，发送完整请求。"""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(target)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
    except Exception:
        writer.close()
        return False
    if not host:
        writer.close()
        return False
    try:
        u_r, u_w = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=UPSTREAM_CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        writer.close()
        return False
    raw_req = f"{method} {path} HTTP/1.1\r\n{headers.decode(errors='replace')}\r\n{body.decode(errors='replace')}"
    try:
        u_w.write(raw_req.encode())
        await u_w.drain()
    except OSError:
        u_w.close()
        writer.close()
        return False
    # 把上游响应 pipe 回客户端
    t = asyncio.create_task(pipe(u_r, writer))
    try:
        await t
    except Exception:
        pass
    finally:
        cancel_pipe(t)
        u_w.close()
    return True


async def _handle_http(reader, writer, line, method, target, headers, body):
    """HTTP 转发，带重试+节点切换。将 line+headers+body 作为请求发送。"""
    for attempt in range(MAX_RETRIES):
        try:
            u_r, u_w = await asyncio.wait_for(
                asyncio.open_connection(*UPSTREAM), timeout=UPSTREAM_CONNECT_TIMEOUT)
        except (asyncio.TimeoutError, OSError) as e:
            log(f"Upstream connect fail: {e}")
            if attempt == MAX_RETRIES - 1:
                await _enable_bypass()
                return await _direct_connect_http(writer, method, target, headers, body)
            if not await switch_clash_node():
                await _enable_bypass()
                return await _direct_connect_http(writer, method, target, headers, body)
            await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
            continue

        try:
            u_w.write(line + headers + body)
            await u_w.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            log(f"Upstream write fail: {e}")
            u_w.close()
            if attempt == MAX_RETRIES - 1:
                await _enable_bypass()
                return await _direct_connect_http(writer, method, target, headers, body)
            if not await switch_clash_node():
                await _enable_bypass()
                return await _direct_connect_http(writer, method, target, headers, body)
            await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
            continue

        status_line = await asyncio.wait_for(u_r.readline(), timeout=UPSTREAM_READLINE_TIMEOUT)
        if not status_line:
            log(f"Empty upstream response on {method} {target}")
            u_w.close()
            if attempt == MAX_RETRIES - 1:
                await _enable_bypass()
                return await _direct_connect_http(writer, method, target, headers, body)
            if not await switch_clash_node():
                await _enable_bypass()
                return await _direct_connect_http(writer, method, target, headers, body)
            await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
            continue
        try:
            status_code = int(status_line.split(b" ")[1])
        except (IndexError, ValueError):
            log(f"Malformed status line: {status_line!r}")
            u_w.close()
            if attempt == MAX_RETRIES - 1:
                await _enable_bypass()
                return await _direct_connect_http(writer, method, target, headers, body)
            if not await switch_clash_node():
                await _enable_bypass()
                return await _direct_connect_http(writer, method, target, headers, body)
            await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
            continue

        if status_code in RETRY_ON_STATUS:
            if attempt < MAX_RETRIES - 1:
                log(f"{status_code} {method} {target} → switching node, retrying... (attempt {attempt + 1}/{MAX_RETRIES})")
                await _drain_reader(u_r); u_w.close()
                if not await switch_clash_node():
                    await _enable_bypass()
                    return await _direct_connect_http(writer, method, target, headers, body)
                await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
                continue
            await _drain_reader(u_r); u_w.close()
            await _enable_bypass()
            return await _direct_connect_http(writer, method, target, headers, body)

        # 成功：转发响应给客户端
        try:
            writer.write(status_line); await writer.drain()
        except OSError:
            u_w.close(); return
        await pipe(u_r, writer)
        u_w.close(); return

    # for 循环耗尽或 switch_clash_node() 所有重试失败，bypass 直连
    await _enable_bypass()
    return await _direct_connect_http(writer, method, target, headers, body)


async def _read_request_line_and_headers(reader, peername) -> tuple[bytes, bytes, str, str] | None:
    """读取并解析请求行 + 头部。返回 (行, 头部, 方法, 目标地址)，出错返回 None。"""
    line = await asyncio.wait_for(reader.readline(), timeout=REQUEST_LINE_TIMEOUT)
    if not line:
        return None
    if len(line) > MAX_LINE_BYTES:
        log(f"Request line too long {peername} ({len(line)}B)")
        return None

    parts = line.decode(errors="replace").strip().split(" ")
    if len(parts) < 2:
        return None

    headers = b""
    deadline = asyncio.get_running_loop().time() + HEADER_READ_DEADLINE
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        h = await asyncio.wait_for(reader.readline(), timeout=max(remaining, 0.1))
        headers += h
        if len(headers) > MAX_HEADER_BYTES:
            return None
        if h in (b"\r\n", b"\n"):
            break

    return (line, headers, parts[0], parts[1])


async def handle(reader, writer):
    """主请求处理：解析 → 分发到 CONNECT 或 HTTP。
    永不返回 5xx — 所有失败触发 bypass，下一个请求走直连。"""
    peername = writer.get_extra_info('peername')
    try:
        result = await _read_request_line_and_headers(reader, peername)
        if result is None:
            log(f"Bad request from {peername}")
            await _close_gracefully(writer); return
        line, headers, method, target = result

        if method == "CONNECT":
            await _handle_connect(reader, writer, line, headers)
            return

        body, headers = await _read_body(reader, headers, peername)
        if body is None:
            log(f"Invalid body from {peername}")
            await _close_gracefully(writer); return

        await _handle_http(reader, writer, line, method, target, headers, body)

    except (asyncio.CancelledError, asyncio.IncompleteReadError):
        pass
    except Exception:
        log(f"Handler error from {peername}", exc_info=True)
        await _enable_bypass()
        await _close_gracefully(writer)
    finally:
        try:
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
        except OSError:
            pass


async def main():
    log(f"Proxy on :{LISTEN[1]} → {UPSTREAM[0]}:{UPSTREAM[1]}")
    global _bad_nodes_flush_task, _bypass_check_task

    # 启动时先启用 BYPASS，确保启动期间所有请求走直连（不产生报错）
    await _enable_bypass()

    _bad_nodes_flush_task = asyncio.create_task(_periodic_flush_bad_nodes())
    _bypass_check_task = asyncio.create_task(_bypass_monitor())

    # Startup reconciliation: check if SQLite state matches in-memory state
    try:
        def _check_sqlite_bypass():
            with sqlite3.connect(CFG["9router"]["db_path"], timeout=3) as con:
                row = con.execute('SELECT data FROM settings WHERE id=1').fetchone()
                if row:
                    s = json.loads(row[0])
                    return not s.get("outboundProxyEnabled", True)
            return False
        sqlite_bypass_active = await asyncio.to_thread(_check_sqlite_bypass)
        if sqlite_bypass_active:
            _bypass_active = True
            log("  BYPASS sync: SQLite outboundProxyEnabled=False detected at startup")
    except Exception:
        pass

    server = await asyncio.start_server(handle, *LISTEN)

    # 服务就绪后检查上游，健康时禁用 bypass
    async def _startup_health_check():
        await asyncio.sleep(1)  # 给 server 一点启动时间
        if await _check_upstream_health():
            await _disable_bypass()
            log("  Startup: upstream healthy, BYPASS disabled")
        else:
            log("  Startup: upstream not ready, keeping BYPASS enabled")

    asyncio.create_task(_startup_health_check())

    async with server: await server.serve_forever()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log("Shutdown")
