#!/usr/bin/env python3
"""
429-intercepting HTTP forward proxy.
Listens :7891 → sing-box:7890. On 429 → switch node → retry once.
"""

import asyncio, json, os, sys, time, logging, urllib.request
from logging.handlers import RotatingFileHandler

UPSTREAM = ("127.0.0.1", 7890)
LISTEN = ("0.0.0.0", 7891)
CLASH_API = "http://127.0.0.1:9090"
SECRET = open("/home/administrator/.singbox_secret").read().strip()

log_file = os.path.join(os.path.dirname(__file__), "proxy_429.log")
logging.basicConfig(filename=log_file, level=logging.INFO,
    format="%(asctime)s %(message)s")

def log(msg):
    logging.info(msg)
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def switch_clash_node():
    try:
        req = urllib.request.Request(f"{CLASH_API}/proxies/proxy",
            headers={"Authorization": f"Bearer {SECRET}"},
            method="PUT", data=json.dumps({"name": "proxy"}).encode())
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log(f"Clash switch failed: {e}")

async def pipe(r, w, timeout=600):
    try:
        while True:
            chunk = await asyncio.wait_for(r.read(65536), timeout=timeout)
            if not chunk: break
            w.write(chunk); await w.drain()
    except: pass

async def handle(reader, writer):
    try:
        # Read first line (method target HTTP/x.x)
        line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not line: writer.close(); return
        method, target = line.decode().strip().split(" ")[:2]

        # Read headers
        headers = b""
        while True:
            h = await asyncio.wait_for(reader.readline(), timeout=30)
            headers += h
            if h in (b"\r\n", b"\n"): break

        if method == "CONNECT":
            try:
                u_r, u_w = await asyncio.wait_for(
                    asyncio.open_connection(*UPSTREAM), timeout=10)
            except:
                writer.close(); return
            # Connect upstream first (CONNECT tunnel)
            u_w.write(line + headers)
            await u_w.drain()
            resp = await asyncio.wait_for(
                u_r.readuntil(b"\r\n\r\n"), timeout=10)
            writer.write(resp); await writer.drain()
            await asyncio.gather(pipe(reader, u_w), pipe(u_r, writer))
            u_w.close(); writer.close()
            return

        # HTTP request
        cl = 0
        for h in headers.split(b"\r\n"):
            if h.lower().startswith(b"content-length:"):
                cl = int(h.split(b":", 1)[1].strip())
                break
        body = await reader.readexactly(cl) if cl > 0 else b""

        for attempt in range(2):
            try:
                u_r, u_w = await asyncio.wait_for(
                    asyncio.open_connection(*UPSTREAM), timeout=10)
            except:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain(); break

            # Forward request (absolute URI, HTTP proxy format)
            u_w.write(line + headers + body)
            await u_w.drain()

            # Read status line
            status_line = await asyncio.wait_for(
                u_r.readline(), timeout=30)
            status_code = int(status_line.split(b" ")[1])

            if status_code == 429 and attempt == 0:
                log(f"429 {method} {target} → switching node, retrying...")
                u_w.close()
                await asyncio.get_event_loop().run_in_executor(
                    None, switch_clash_node)
                await asyncio.sleep(1)
                continue

            writer.write(status_line); await writer.drain()
            await pipe(u_r, writer)
            u_w.close(); break

    except Exception as e:
        log(f"Error: {e}")
    finally:
        try: writer.close()
        except: pass

async def main():
    log(f"Proxy on :{LISTEN[1]} → {UPSTREAM[0]}:{UPSTREAM[1]}")
    server = await asyncio.start_server(handle, *LISTEN)
    async with server: await server.serve_forever()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log("Shutdown")
