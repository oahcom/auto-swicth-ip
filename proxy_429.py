#!/usr/bin/env python3
"""
429-intercepting HTTP forward proxy.
Listens :7891 → sing-box:7890. Retries on 429/502/503/504/errors up to 5 times
with Clash node switching.
"""

import asyncio
import json
import os
import random
import sys
import threading
import time
import logging
import logging.handlers
import urllib.request
import urllib.error

from config import CFG, SINGBOX_SECRET, BAD_NODES_TTL, SINGBOX_API, SELECTOR, MIXED_PORT, LISTEN_PORT, LOG_FORMAT

UPSTREAM = ("127.0.0.1", MIXED_PORT)
LISTEN = ("127.0.0.1", LISTEN_PORT)

# Retry config
MAX_RETRIES = 5
RETRY_ON_STATUS = {429, 502, 503, 504}
RETRY_SLEEP_BASE = 1  # seconds, will multiply by attempt

# Size / timeout limits
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
PIPE_READ_TIMEOUT = 600  # max idle between chunks
DRAIN_MAX_SEC = 10  # cap drain on retry
PIPE_BUF_SIZE = 65536

# Shared bad-node file: proxy_429 writes, daemon reads
BAD_NODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bad_nodes.json")

# Switch lock for concurrent node switch coordination
_SWITCH_LOCK = asyncio.Lock()

# --- Bad node tracking (shared with daemon via .bad_nodes.json) ---
# In-memory cache with periodic flush; atomic write via rename.
_bad_nodes_lock = threading.Lock()
_bad_nodes_cache: dict[str, float] = {}  # node -> expire_ts
_bad_nodes_dirty = False
_bad_nodes_flush_task: asyncio.Task | None = None



def _load_bad_nodes() -> dict:
    """Load bad nodes from file. Returns {node: expire_ts}."""
    global _bad_nodes_cache
    # If cache is populated and not stale, skip file I/O
    if _bad_nodes_cache:
        return _bad_nodes_cache
    try:
        with _bad_nodes_lock:
            if _bad_nodes_cache:  # double-check
                return _bad_nodes_cache
            if os.path.exists(BAD_NODES_FILE):
                with open(BAD_NODES_FILE) as f:
                    _bad_nodes_cache = json.load(f)
            else:
                _bad_nodes_cache = {}
    except Exception:
        _bad_nodes_cache = {}
    return _bad_nodes_cache

def _flush_bad_nodes() -> None:
    """Flush dirty cache to file (atomic write via temp + rename)."""
    global _bad_nodes_dirty
    if not _bad_nodes_dirty:
        return
    with _bad_nodes_lock:
        if not _bad_nodes_dirty:
            return
        tmp = BAD_NODES_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(_bad_nodes_cache, f)
            os.replace(tmp, BAD_NODES_FILE)  # atomic on POSIX
            _bad_nodes_dirty = False
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass

def _mark_bad_node(node: str) -> None:
    """Mark a node as bad with TTL (memory only; flush scheduled)."""
    if not node:
        return
    global _bad_nodes_dirty
    _load_bad_nodes()  # ensure cache loaded
    now = time.time()
    with _bad_nodes_lock:
        old = _bad_nodes_cache.get(node, 0)
        if old > now:
            return  # already bad and not expired
        _bad_nodes_cache[node] = now + BAD_NODES_TTL
        _bad_nodes_dirty = True
    log(f"  🚫 Bad node: {node} (TTL {BAD_NODES_TTL}s)")

def _get_current_bad_nodes() -> set:
    """Get currently bad nodes from cache (file-backed)."""
    now = time.time()
    nodes = _load_bad_nodes()
    return {n for n, exp in nodes.items() if exp > now}

async def _periodic_flush_bad_nodes():
    """Flush dirty cache to disk every 3 seconds."""
    global _bad_nodes_flush_task
    try:
        while True:
            await asyncio.sleep(3)
            _flush_bad_nodes()
    except asyncio.CancelledError:
        _flush_bad_nodes()  # one last flush on cancel
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
    print(f"[{time.strftime(LOG_FORMAT)}] {msg}", flush=True)

async def switch_clash_node() -> None:
    """Pick a random live node via Clash API and switch to it. Async-safe via asyncio.Lock.
    Marks the old node as bad after switching."""
    async with _SWITCH_LOCK:
        data = await asyncio.to_thread(_clash_api_sync, "GET", f"/proxies/{SELECTOR}")
        if not data:
            return
        all_nodes = data.get("all", [])
        current = data.get("now", "")
        bad = _get_current_bad_nodes()
        candidates = [n for n in all_nodes if n != current and n != "direct" and n not in bad]
        if not candidates:
            log(f"Clash switch: all {len(all_nodes)} nodes bad, cannot switch")
            return
        chosen = random.choice(candidates)
        body = json.dumps({"name": chosen}).encode()
        ok = await asyncio.to_thread(_clash_api_sync, "PUT", f"/proxies/{SELECTOR}", body)
        if ok is not None:
            log(f"Clash switch: {current} -> {chosen}")
            # Mark old node as bad so daemon avoids it too
            _mark_bad_node(current)

def _clash_api_sync(method: str, path: str, body: bytes | None = None) -> dict | None:
    """Synchronous Clash API call (runs in thread pool).
    Returns {} on 204 (success with no body), None on failure."""
    url = f"{SINGBOX_API}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SINGBOX_SECRET}"}, method=method, data=body)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=SINGBOX_API_TIMEOUT) as resp:
            if resp.status == 204:
                return {}  # 204 No Content — success, ok is not None
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as e:
        log(f"Clash API {method} {path} failed: {e}")
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
    """Drain reader with cap at DRAIN_MAX_SEC. Catches expected exceptions."""
    drain_deadline = asyncio.get_running_loop().time() + DRAIN_MAX_SEC
    try:
        while True:
            remaining = drain_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            c = await asyncio.wait_for(r.read(PIPE_BUF_SIZE), timeout=remaining)
            if not c: break
    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
        pass

async def cancel_pipe(t):
    if not t.done():
        t.cancel()
        try: await t
        except (asyncio.CancelledError, OSError):
            pass

def _has_te_chunked(headers_raw: bytes) -> bool:
    """Check if headers contain Transfer-Encoding: chunked (handles multi-value)."""
    for line in headers_raw.split(b"\r\n"):
        val = line.lower().strip()
        if val.startswith(b"transfer-encoding:"):
            te_value = val.split(b":", 1)[1].strip()
            for part in te_value.split(b","):
                if part.strip() == b"chunked":
                    return True
    return False

def _strip_header(headers_raw: bytes, name: bytes) -> bytes:
    """Strip all header lines with given name (case-insensitive)."""
    lines = []
    for line in headers_raw.split(b"\r\n"):
        if not line or line.lower().startswith(name.lower() + b":"):
            continue
        lines.append(line)
    return b"\r\n".join(lines)

def _get_content_length(headers_raw: bytes) -> int:
    """Parse Content-Length. Returns 0 if absent.
    Validates: positive, single value, not negative, not > MAX.
    Returns -1 on invalid (multiple CL, negative, overflow).
    """
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
        return -1  # R2: multiple CL headers is invalid
    if cl_vals[0] > MAX_CONTENT_LENGTH:
        return -1  # too large
    return cl_vals[0]

async def _read_body(reader, headers, peername) -> tuple[bytes, bytes]:
    """Read HTTP body (chunked or Content-Length). Returns (body, modified_headers)."""
    cl = _get_content_length(headers)
    if cl < 0:
        return (None, None)  # invalid
    has_chunked = _has_te_chunked(headers)
    body = b""
    if has_chunked:
        chunks = []
        while True:
            chunk_line = await asyncio.wait_for(reader.readline(), timeout=BODY_READ_DEADLINE)
            chunk_line_stripped = chunk_line.strip()
            if not chunk_line_stripped or chunk_line_stripped == b"0":
                if chunk_line_stripped == b"0":
                    await reader.readline()  # trailing CRLF
                    while True:  # consume trailer headers
                        t = await asyncio.wait_for(reader.readline(), timeout=BODY_READ_DEADLINE)
                        if t in (b"\r\n", b"\n", b""):
                            break
                break
            chunk_size_hex = chunk_line_stripped.split(b";")[0]
            try:
                chunk_size = int(chunk_size_hex, 16)
            except ValueError:
                return (None, None)
            if sum(len(c) for c in chunks) + chunk_size > MAX_CONTENT_LENGTH:
                return (None, None)
            data = await asyncio.wait_for(reader.readexactly(chunk_size + 2), timeout=BODY_READ_DEADLINE)
            chunks.append(data[:chunk_size])
        body = b"".join(chunks)
        headers = _strip_header(headers, b"Transfer-Encoding")
        headers = _strip_header(headers, b"Content-Length")
        headers += f"Content-Length: {len(body)}\r\n".encode()
    elif cl > 0:
        body = await asyncio.wait_for(reader.readexactly(cl), timeout=BODY_READ_DEADLINE)
    return (body, headers)


async def _handle_connect(reader, writer, line, headers):
    """CONNECT method with retry+node-switch."""
    for attempt in range(MAX_RETRIES):
        try:
            u_r, u_w = await asyncio.wait_for(
                asyncio.open_connection(*UPSTREAM), timeout=UPSTREAM_CONNECT_TIMEOUT)
        except (asyncio.TimeoutError, OSError) as e:
            log(f"CONNECT upstream connect fail: {e}")
            if attempt == MAX_RETRIES - 1:
                return await _write_502_close(writer)
            await switch_clash_node()
            await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
            continue
        try:
            u_w.write(line + headers)
            await u_w.drain()
            resp = await asyncio.wait_for(u_r.readuntil(b"\r\n\r\n"), timeout=CONNECT_RESP_TIMEOUT)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
            log(f"CONNECT upstream write/read fail: {e}")
            if attempt == MAX_RETRIES - 1:
                return await _write_502_close(writer)
            u_w.close()
            await switch_clash_node()
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
                await switch_clash_node()
                await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
                continue
            await _drain_reader(u_r); u_w.close()
            return await _write_502_close(writer)
        if status_code != 200:
            try:
                writer.write(resp); await writer.drain()
            except OSError:
                pass
            u_w.close(); return
        try:
            writer.write(resp); await writer.drain()
        except OSError:
            u_w.close(); return
        t1 = asyncio.create_task(pipe(reader, u_w))
        t2 = asyncio.create_task(pipe(u_r, writer))
        done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        await cancel_pipe(t1); await cancel_pipe(t2)
        u_w.close(); return
    # Unreachable — symmetry guard
    await _write_502_close(writer)


async def _write_502_close(writer):
    """Write 502 Bad Gateway and close."""
    try:
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await writer.drain()
    except OSError:
        pass
    writer.close()


async def _handle_http(reader, writer, line, method, target, headers, body):
    """HTTP forward with retry+node-switch. line+headers+body sent as request."""
    for attempt in range(MAX_RETRIES):
        try:
            u_r, u_w = await asyncio.wait_for(
                asyncio.open_connection(*UPSTREAM), timeout=UPSTREAM_CONNECT_TIMEOUT)
        except (asyncio.TimeoutError, OSError) as e:
            log(f"Upstream connect fail: {e}")
            if attempt == MAX_RETRIES - 1:
                return await _write_502_close(writer)
            await switch_clash_node()
            await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
            continue

        try:
            u_w.write(line + headers + body)
            await u_w.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            log(f"Upstream write fail: {e}")
            u_w.close()
            if attempt == MAX_RETRIES - 1:
                return await _write_502_close(writer)
            await switch_clash_node()
            await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
            continue

        status_line = await asyncio.wait_for(u_r.readline(), timeout=UPSTREAM_READLINE_TIMEOUT)
        if not status_line:
            log(f"Empty upstream response on {method} {target}")
            u_w.close()
            if attempt == MAX_RETRIES - 1:
                return await _write_502_close(writer)
            await switch_clash_node()
            await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
            continue
        try:
            status_code = int(status_line.split(b" ")[1])
        except (IndexError, ValueError):
            log(f"Malformed status line: {status_line!r}")
            u_w.close()
            if attempt == MAX_RETRIES - 1:
                return await _write_502_close(writer)
            await switch_clash_node()
            await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
            continue

        if status_code in RETRY_ON_STATUS:
            if attempt < MAX_RETRIES - 1:
                log(f"{status_code} {method} {target} → switching node, retrying... (attempt {attempt + 1}/{MAX_RETRIES})")
                await _drain_reader(u_r); u_w.close()
                await switch_clash_node()
                await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
                continue
            await _drain_reader(u_r); u_w.close()
            return await _write_502_close(writer)

        # Success
        try:
            writer.write(status_line); await writer.drain()
        except OSError:
            u_w.close(); return
        await pipe(u_r, writer)


async def _read_request_line_and_headers(reader, peername) -> tuple[bytes, bytes, str, str] | None:
    """Read and parse request line + headers. Returns (line, headers, method, target) or None on error."""
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
    """Main request handler: parse → dispatch CONNECT or HTTP."""
    peername = writer.get_extra_info('peername')
    try:
        result = await _read_request_line_and_headers(reader, peername)
        if result is None:
            log(f"Bad request from {peername}")
            await _write_502_close(writer); return
        line, headers, method, target = result

        if method == "CONNECT":
            await _handle_connect(reader, writer, line, headers)
            return

        body, headers = await _read_body(reader, headers, peername)
        if body is None:
            log(f"Invalid body from {peername}")
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain(); writer.close(); return

        await _handle_http(reader, writer, line, method, target, headers, body)

    except (asyncio.CancelledError, asyncio.IncompleteReadError):
        pass
    except Exception as e:
        log(f"Error: {e}")
    finally:
        try:
            if not writer.is_closing():
                writer.close()
        except OSError:
            pass

async def main():
    log(f"Proxy on :{LISTEN[1]} → {UPSTREAM[0]}:{UPSTREAM[1]}")
    # Start periodic bad-node flush task
    global _bad_nodes_flush_task
    _bad_nodes_flush_task = asyncio.create_task(_periodic_flush_bad_nodes())
    server = await asyncio.start_server(handle, *LISTEN)
    async with server: await server.serve_forever()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log("Shutdown")
