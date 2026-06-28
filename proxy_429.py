#!/usr/bin/env python3
"""
429-intercepting HTTP forward proxy.
Listens :7891 → sing-box:7890. On 429 → switch node → retry once.

Codex review Round 1 fixes:
  P1: header loop total timeout, readexactly timeout, header/body size cap
  P2: switch_clash_node payload fix, CONNECT one-side-close, bare except → asyncio.CancelledError
"""

import asyncio, json, os, sys, time, logging, urllib.request
from logging.handlers import RotatingFileHandler

UPSTREAM = ("127.0.0.1", 7890)
LISTEN = ("0.0.0.0", 7891)
CLASH_API = "http://127.0.0.1:9090"
SECRET = open("/home/administrator/.singbox_secret").read().strip()

MAX_HEADER_BYTES = 32 * 1024       # 32 KB total headers
MAX_CONTENT_LENGTH = 64 * 1024 * 1024  # 64 MB body
HEADER_READ_DEADLINE = 10          # seconds total to read all headers
BODY_READ_DEADLINE = 60            # seconds to read request body

log_file = os.path.join(os.path.dirname(__file__), "proxy_429.log")
logging.basicConfig(filename=log_file, level=logging.INFO,
    format="%(asctime)s %(message)s")

def log(msg):
    logging.info(msg)
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def switch_clash_node():
    """PUT /proxies/proxy with {} triggers sing-box to cycle to next in group."""
    try:
        req = urllib.request.Request(
            f"{CLASH_API}/proxies/proxy",
            headers={"Authorization": f"Bearer {SECRET}"},
            method="PUT",
            data=b"{}",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log(f"Clash switch failed: {e}")

async def pipe(r, w, timeout=600):
    """Bidirectional pipe with per-read timeout. Breaks on any error."""
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
    except Exception:
        pass

async def _cancel_pipe(pipe_task):
    """Cancel a pipe task and close its writer."""
    if not pipe_task.done():
        pipe_task.cancel()
        try:
            await pipe_task
        except (asyncio.CancelledError, Exception):
            pass

async def handle(reader, writer):
    peername = writer.get_extra_info('peername')
    u_w = None  # track upstream writer for cleanup
    try:
        # P1 FIX: read first line with timeout
        line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not line:
            writer.close()
            return
        parts = line.decode().strip().split(" ")
        if len(parts) < 2:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        method, target = parts[0], parts[1]

        # P1 FIX: read headers with overall deadline + size cap
        headers = b""
        deadline = asyncio.get_event_loop().time() + HEADER_READ_DEADLINE
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                log(f"Header read timeout from {peername}")
                writer.close()
                return
            h = await asyncio.wait_for(reader.readline(), timeout=min(remaining, 10))
            headers += h
            if len(headers) > MAX_HEADER_BYTES:
                log(f"Header overflow from {peername} ({len(headers)} bytes)")
                writer.close()
                return
            if h in (b"\r\n", b"\n"):
                break

        if method == "CONNECT":
            try:
                u_r, u_w = await asyncio.wait_for(
                    asyncio.open_connection(*UPSTREAM), timeout=10)
            except (asyncio.TimeoutError, OSError) as e:
                log(f"Upstream connect failed: {e}")
                writer.close()
                return
            u_w.write(line + headers)
            await u_w.drain()
            resp = await asyncio.wait_for(
                u_r.readuntil(b"\r\n\r\n"), timeout=10)
            writer.write(resp)
            await writer.drain()
            # P2 FIX: cancel peer pipe on one-side close instead of waiting 600s
            t1 = asyncio.create_task(pipe(reader, u_w))
            t2 = asyncio.create_task(pipe(u_r, writer))
            done, pending = await asyncio.wait(
                [t1, t2], return_when=asyncio.FIRST_COMPLETED)
            await _cancel_pipe(t1)
            await _cancel_pipe(t2)
            u_w.close()
            writer.close()
            return

        # HTTP request
        cl = 0
        chunked = False
        for h in headers.split(b"\r\n"):
            hl = h.lower()
            if hl.startswith(b"content-length:"):
                cl = int(h.split(b":", 1)[1].strip())
            elif hl.strip() == b"transfer-encoding: chunked":
                chunked = True

        # P1 FIX: cap Content-Length
        if cl > MAX_CONTENT_LENGTH:
            log(f"Body too large: {cl} bytes from {peername}")
            writer.write(b"HTTP/1.1 413 Payload Too Large\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # P1 FIX: timeout on body read
        if cl > 0:
            body = await asyncio.wait_for(
                reader.readexactly(cl), timeout=BODY_READ_DEADLINE)
        elif chunked:
            # P2 FIX: consume chunked request body (don't silently drop)
            body = b""
            while True:
                chunk_line = await asyncio.wait_for(
                    reader.readline(), timeout=BODY_READ_DEADLINE)
                chunk_line_stripped = chunk_line.strip()
                if not chunk_line_stripped or chunk_line_stripped == b"0":
                    # consume trailing CRLF
                    if chunk_line_stripped == b"0":
                        await reader.readline()
                    break
                chunk_size = int(chunk_line_stripped, 16)
                data = await asyncio.wait_for(
                    reader.readexactly(chunk_size + 2), timeout=BODY_READ_DEADLINE)
                body += data[:chunk_size]
                # consume trailing CRLF if not included
        else:
            body = b""

        for attempt in range(2):
            try:
                u_r, u_w = await asyncio.wait_for(
                    asyncio.open_connection(*UPSTREAM), timeout=10)
            except (asyncio.TimeoutError, OSError) as e:
                log(f"Upstream connect failed: {e}")
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
                break

            # Forward request
            req_data = line + headers + body
            u_w.write(req_data)
            await u_w.drain()

            # Read status line
            status_line = await asyncio.wait_for(
                u_r.readline(), timeout=30)
            if not status_line:
                log(f"Empty upstream response on {method} {target}")
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
                break
            status_code = int(status_line.split(b" ")[1])

            if status_code == 429 and attempt == 0:
                log(f"429 {method} {target} → switching node, retrying...")
                # Close upstream cleanly (drain body first to avoid RST)
                try:
                    while True:
                        c = await asyncio.wait_for(u_r.read(4096), timeout=5)
                        if not c:
                            break
                except:
                    pass
                u_w.close()
                await asyncio.get_event_loop().run_in_executor(
                    None, switch_clash_node)
                await asyncio.sleep(1)
                continue

            writer.write(status_line)
            await writer.drain()
            await pipe(u_r, writer)
            u_w.close()
            break

    except asyncio.CancelledError:
        pass
    except Exception as e:
        log(f"Error: {e}")
    finally:
        if u_w and not u_w.is_closing():
            try:
                u_w.close()
            except:
                pass
        try:
            if not writer.is_closing():
                writer.close()
        except:
            pass

async def main():
    log(f"Proxy on :{LISTEN[1]} → {UPSTREAM[0]}:{UPSTREAM[1]}")
    server = await asyncio.start_server(handle, *LISTEN)
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Shutdown")
