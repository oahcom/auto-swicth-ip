#!/usr/bin/env python3
"""
429-intercepting HTTP forward proxy.
Listens :7891 → sing-box:7890. On 429 → switch node → retry once.

Codex review Round 1: 3P1 4P2 fixed
Codex review Round 2: 1P1 chunked headers + 5P2 fixed
Next Round 3 CLEAN check required before production use.
"""

import asyncio, json, os, random, sys, threading, time, logging, urllib.request

UPSTREAM = ("127.0.0.1", 7890)
LISTEN = ("127.0.0.1", 7891)
CLASH_API = "http://127.0.0.1:9090"
SELECTOR = "proxy"

_SINGBOX_SECRET: str | None = None
_SINGBOX_SECRET_LOCK = threading.Lock()

def _get_secret() -> str:
    global _SINGBOX_SECRET
    if _SINGBOX_SECRET is None:
        with _SINGBOX_SECRET_LOCK:
            if _SINGBOX_SECRET is None:  # double-check
                with open(os.path.expanduser("~/.singbox_secret")) as _f:
                    _SINGBOX_SECRET = _f.read().strip()
    return _SINGBOX_SECRET

MAX_HEADER_BYTES = 32 * 1024
MAX_LINE_BYTES = 8 * 1024
MAX_CONTENT_LENGTH = 64 * 1024 * 1024
HEADER_READ_DEADLINE = 10
BODY_READ_DEADLINE = 60

log_file = os.path.join(os.path.dirname(__file__), "proxy_429.log")
logging.basicConfig(filename=log_file, level=logging.INFO,
    format="%(asctime)s %(message)s")

def log(msg):
    logging.info(msg)
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def switch_clash_node():
    """Pick a random live node via Clash API and switch to it."""
    try:
        secret = _get_secret()
        req = urllib.request.Request(
            f"{CLASH_API}/proxies/{SELECTOR}",
            headers={"Authorization": f"Bearer {secret}"},
            method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        all_nodes = data.get("all", [])
        current = data.get("now", "")
        candidates = [n for n in all_nodes if n != current and n != "direct"]
        if not candidates:
            log("Clash switch: no other nodes available")
            return
        chosen = random.choice(candidates)
        body = json.dumps({"name": chosen}).encode()
        req = urllib.request.Request(
            f"{CLASH_API}/proxies/{SELECTOR}",
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            method="PUT", data=body)
        urllib.request.urlopen(req, timeout=5)
        log(f"Clash switch: {current} -> {chosen}")
    except Exception as e:
        log(f"Clash switch failed: {e}")

async def pipe(r, w, timeout=600):
    try:
        while True:
            chunk = await asyncio.wait_for(r.read(65536), timeout=timeout)
            if not chunk:
                break
            w.write(chunk)
            await w.drain()
    except (asyncio.CancelledError, asyncio.TimeoutError,
            ConnectionResetError, BrokenPipeError, OSError):
        pass
    except Exception as e:
        log(f"Pipe error: {e}")

async def _cancel_pipe(t):
    if not t.done():
        t.cancel()
        try: await t
        except (asyncio.CancelledError, Exception):
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
        line = await asyncio.wait_for(reader.readline(), timeout=30)
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
            try:
                u_r, u_w = await asyncio.wait_for(
                    asyncio.open_connection(*UPSTREAM), timeout=10)
            except (asyncio.TimeoutError, OSError) as e:
                log(f"Upstream connect fail: {e}"); writer.close(); return
            u_w.write(line + headers); await u_w.drain()
            resp = await asyncio.wait_for(u_r.readuntil(b"\r\n\r\n"), timeout=10)
            writer.write(resp); await writer.drain()
            t1 = asyncio.create_task(pipe(reader, u_w))
            t2 = asyncio.create_task(pipe(u_r, writer))
            done, pending = await asyncio.wait(
                [t1, t2], return_when=asyncio.FIRST_COMPLETED)
            await _cancel_pipe(t1); await _cancel_pipe(t2)
            u_w.close(); writer.close(); return

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

        for attempt in range(2):
            try:
                u_r, u_w = await asyncio.wait_for(
                    asyncio.open_connection(*UPSTREAM), timeout=10)
            except (asyncio.TimeoutError, OSError) as e:
                log(f"Upstream connect fail: {e}")
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain(); break

            u_w.write(line + headers + body)
            await u_w.drain()

            status_line = await asyncio.wait_for(u_r.readline(), timeout=30)
            if not status_line:
                log(f"Empty upstream response on {method} {target}")
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain(); break
            try:
                status_code = int(status_line.split(b" ")[1])
            except (IndexError, ValueError):
                log(f"Malformed status line: {status_line!r}")
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain(); break

            if status_code == 429 and attempt == 0:
                log(f"429 {method} {target} → switching node, retrying...")
                # R2 FIX: explicit exception types in drain loop
                try:
                    while True:
                        c = await asyncio.wait_for(u_r.read(4096), timeout=5)
                        if not c: break
                except (asyncio.TimeoutError, ConnectionResetError,
                        BrokenPipeError, OSError):
                    pass
                u_w.close()
                await asyncio.get_running_loop().run_in_executor(
                    None, switch_clash_node)
                await asyncio.sleep(1)
                continue

            writer.write(status_line); await writer.drain()
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
