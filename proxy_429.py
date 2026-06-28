#!/usr/bin/env python3
"""
429-intercepting HTTP forward proxy.
Listens :7891 → sing-box:7890. Retries on 429/502/503/504/errors up to 5 times
with Clash node switching.
"""

import asyncio, json, os, random, sys, threading, time, logging, urllib.request

UPSTREAM = ("127.0.0.1", 7890)
LISTEN = ("127.0.0.1", 7891)
CLASH_API = "http://127.0.0.1:9090"
SELECTOR = "proxy"

# Retry config
MAX_RETRIES = 5
RETRY_ON_STATUS = {429, 502, 503, 504}
RETRY_SLEEP_BASE = 1  # seconds, will multiply by attempt

# Size / timeout limits
REQUEST_LINE_TIMEOUT = 30
UPSTREAM_CONNECT_TIMEOUT = 10
UPSTREAM_READLINE_TIMEOUT = 30
CLASH_API_TIMEOUT = 5
CONNECT_RESP_TIMEOUT = 10
MAX_LINE_BYTES = 8 * 1024
MAX_HEADER_BYTES = 32 * 1024
MAX_CONTENT_LENGTH = 64 * 1024 * 1024
HEADER_READ_DEADLINE = 10
BODY_READ_DEADLINE = 60
PIPE_READ_TIMEOUT = 600  # max idle between chunks
DRAIN_MAX_SEC = 10  # cap drain on retry
PIPE_BUF_SIZE = 65536

# Secret lazy-load
_SINGBOX_SECRET: str | None = None
_SINGBOX_SECRET_LOCK = threading.Lock()
_SWITCH_LOCK = asyncio.Lock()  # async lock for concurrent node switch coordination

def _get_secret() -> str:
    global _SINGBOX_SECRET
    if _SINGBOX_SECRET is None:
        with _SINGBOX_SECRET_LOCK:
            if _SINGBOX_SECRET is None:  # double-check
                with open(os.path.expanduser("~/.singbox_secret")) as _f:
                    _SINGBOX_SECRET = _f.read().strip()
    return _SINGBOX_SECRET

log_file = os.path.join(os.path.dirname(__file__), "proxy_429.log")
logging.basicConfig(filename=log_file, level=logging.INFO,
    format="%(asctime)s %(message)s")

def log(msg):
    logging.info(msg)
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

async def switch_clash_node() -> None:
    """Pick a random live node via Clash API and switch to it. Async-safe via asyncio.Lock."""
    async with _SWITCH_LOCK:
        data = await asyncio.to_thread(_clash_api_sync, "GET", f"/proxies/{SELECTOR}")
        if not data:
            return
        all_nodes = data.get("all", [])
        current = data.get("now", "")
        candidates = [n for n in all_nodes if n != current and n != "direct"]
        if not candidates:
            log("Clash switch: no other nodes available")
            return
        chosen = random.choice(candidates)
        body = json.dumps({"name": chosen}).encode()
        ok = await asyncio.to_thread(_clash_api_sync, "PUT", f"/proxies/{SELECTOR}", body)
        if ok is not None:
            log(f"Clash switch: {current} -> {chosen}")

def _clash_api_sync(method: str, path: str, body: bytes | None = None) -> dict | None:
    """Synchronous Clash API call (runs in thread pool)."""
    secret = _get_secret()
    url = f"{CLASH_API}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {secret}"}, method=method, data=body)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=CLASH_API_TIMEOUT) as resp:
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

async def handle(reader, writer):
    peername = writer.get_extra_info('peername')
    u_w = None
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=REQUEST_LINE_TIMEOUT)
        if not line:
            writer.close(); return
        if len(line) > MAX_LINE_BYTES:
            log(f"Request line too long {peername} ({len(line)}B)")
            writer.write(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
            await writer.drain(); writer.close(); return
        parts = line.decode(errors="replace").strip().split(" ")
        if len(parts) < 2:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain(); writer.close(); return
        method, target = parts[0], parts[1]

        # Read headers with deadline + size cap
        headers = b""
        deadline = asyncio.get_running_loop().time() + HEADER_READ_DEADLINE
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                log(f"Header read timeout {peername}"); writer.close(); return
            h = await asyncio.wait_for(reader.readline(), timeout=max(remaining, 0.1))
            headers += h
            if len(headers) > MAX_HEADER_BYTES:
                log(f"Header overflow {peername} ({len(headers)}B)"); writer.close(); return
            if h in (b"\r\n", b"\n"):
                break

        if method == "CONNECT":
            for attempt in range(MAX_RETRIES):
                try:
                    u_r, u_w = await asyncio.wait_for(
                        asyncio.open_connection(*UPSTREAM), timeout=UPSTREAM_CONNECT_TIMEOUT)
                except (asyncio.TimeoutError, OSError) as e:
                    log(f"CONNECT upstream connect fail: {e}")
                    if attempt == MAX_RETRIES - 1:
                        try:
                            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                            await writer.drain()
                        except OSError:
                            pass
                        writer.close(); return
                    await switch_clash_node()
                    await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
                    continue
                try:
                    u_w.write(line + headers)
                    await u_w.drain()
                    resp = await asyncio.wait_for(
                        u_r.readuntil(b"\r\n\r\n"), timeout=CONNECT_RESP_TIMEOUT)
                except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
                    log(f"CONNECT upstream write/read fail: {e}")
                    if attempt == MAX_RETRIES - 1:
                        try:
                            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                            await writer.drain()
                        except OSError:
                            pass
                        writer.close(); return
                    u_w.close()
                    await switch_clash_node()
                    await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
                    continue
                try:
                    status_code = int(resp.split(b" ")[1])
                except (IndexError, ValueError):
                    status_code = 502
                if status_code in RETRY_ON_STATUS and attempt < MAX_RETRIES - 1:
                    log(f"CONNECT {status_code} → switching node, retrying... (attempt {attempt + 1}/{MAX_RETRIES})")
                    await _drain_reader(u_r)
                    u_w.close()
                    await switch_clash_node()
                    await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
                    continue
                try:
                    writer.write(resp); await writer.drain()
                except OSError:
                    u_w.close(); writer.close(); return
                t1 = asyncio.create_task(pipe(reader, u_w))
                t2 = asyncio.create_task(pipe(u_r, writer))
                done, pending = await asyncio.wait(
                    [t1, t2], return_when=asyncio.FIRST_COMPLETED)
                await cancel_pipe(t1); await cancel_pipe(t2)
                u_w.close(); writer.close(); return
            # Unreachable: last attempt with retryable status always returns in success branch above
            # Kept for structural symmetry with HTTP path
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
            except OSError:
                pass
            writer.close(); return

        # HTTP request body parsing
        cl = _get_content_length(headers)
        if cl < 0:
            log(f"Invalid Content-Length from {peername}")
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain(); writer.close(); return

        has_chunked = _has_te_chunked(headers)

        body = b""
        if has_chunked:
            # Read chunked body, track size
            while True:
                chunk_line = await asyncio.wait_for(
                    reader.readline(), timeout=BODY_READ_DEADLINE)
                chunk_line_stripped = chunk_line.strip()
                if not chunk_line_stripped or chunk_line_stripped == b"0":
                    if chunk_line_stripped == b"0":
                        # consume trailing CRLF
                        await reader.readline()
                        # consume optional trailer headers until final CRLF
                        while True:
                            t = await asyncio.wait_for(reader.readline(), timeout=BODY_READ_DEADLINE)
                            if t in (b"\r\n", b"\n", b""):
                                break
                    break
                # RFC 7230: chunk-size may have extensions after semicolon
                # e.g., "5;ext=value" -> parse only hex part before ';'
                chunk_size_hex = chunk_line_stripped.split(b";")[0]
                try:
                    chunk_size = int(chunk_size_hex, 16)
                except ValueError:
                    log(f"Invalid chunk size from {peername}: {chunk_line_stripped}")
                    writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await writer.drain(); writer.close(); return
                if len(body) + chunk_size > MAX_CONTENT_LENGTH:
                    log(f"Chunked body too large from {peername}")
                    writer.write(b"HTTP/1.1 413 Payload Too Large\r\n\r\n")
                    await writer.drain(); writer.close(); return
                # chunk_data is chunk-size bytes + trailing CRLF
                data = await asyncio.wait_for(
                    reader.readexactly(chunk_size + 2), timeout=BODY_READ_DEADLINE)
                body += data[:chunk_size]
            # R2 P1 FIX: strip Transfer-Encoding, add Content-Length
            headers = _strip_header(headers, b"Transfer-Encoding")
            headers = _strip_header(headers, b"Content-Length")
            cl = len(body)
            headers += f"Content-Length: {cl}\r\n".encode()
        elif cl > 0:
            body = await asyncio.wait_for(
                reader.readexactly(cl), timeout=BODY_READ_DEADLINE)

        for attempt in range(MAX_RETRIES):
            try:
                u_r, u_w = await asyncio.wait_for(
                    asyncio.open_connection(*UPSTREAM), timeout=UPSTREAM_CONNECT_TIMEOUT)
            except (asyncio.TimeoutError, OSError) as e:
                log(f"Upstream connect fail: {e}")
                if attempt == MAX_RETRIES - 1:
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain(); break
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
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain(); break
                await switch_clash_node()
                await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
                continue

            status_line = await asyncio.wait_for(u_r.readline(), timeout=UPSTREAM_READLINE_TIMEOUT)
            if not status_line:
                log(f"Empty upstream response on {method} {target}")
                u_w.close()
                if attempt == MAX_RETRIES - 1:
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain(); break
                await switch_clash_node()
                await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
                continue
            try:
                status_code = int(status_line.split(b" ")[1])
            except (IndexError, ValueError):
                log(f"Malformed status line: {status_line!r}")
                u_w.close()
                if attempt == MAX_RETRIES - 1:
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain(); break
                await switch_clash_node()
                await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
                continue

            # Retry on bad status codes
            if status_code in RETRY_ON_STATUS and attempt < MAX_RETRIES - 1:
                log(f"{status_code} {method} {target} → switching node, retrying... (attempt {attempt + 1}/{MAX_RETRIES})")
                await _drain_reader(u_r)
                u_w.close()
                await switch_clash_node()
                await asyncio.sleep(RETRY_SLEEP_BASE * (attempt + 1))
                continue

            # Success - forward response
            try:
                writer.write(status_line); await writer.drain()
            except OSError:
                u_w.close(); break
            await pipe(u_r, writer)
            u_w.close(); break

    except (asyncio.CancelledError, asyncio.IncompleteReadError):
        pass
    except Exception as e:
        log(f"Error: {e}")
    finally:
        if u_w and not u_w.is_closing():
            try: u_w.close()
            except OSError:
                pass
        try:
            if not writer.is_closing():
                writer.close()
        except OSError:
            pass

async def main():
    log(f"Proxy on :{LISTEN[1]} → {UPSTREAM[0]}:{UPSTREAM[1]}")
    server = await asyncio.start_server(handle, *LISTEN)
    async with server: await server.serve_forever()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log("Shutdown")
