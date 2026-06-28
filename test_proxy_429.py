#!/usr/bin/env python3
"""Tests for proxy_429.py — unit tests for helper functions + integration mock test.

$ cd ~/dkk-projects/auto-switch-ip
$ PYTHONPATH=. python3 test_proxy_429.py
"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from proxy_429 import (
    _has_te_chunked, _strip_header, _get_content_length,
    MAX_CONTENT_LENGTH,
)

PASS = 0
FAIL = 0

def _check(name: str, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}: expected={want!r}, got={got!r}")

def _check_bool(name: str, cond: bool):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")

# ================================================================
# _has_te_chunked
# ================================================================
def test_has_te_chunked():
    _check("chunked", _has_te_chunked(b"Host: a\r\nTransfer-Encoding: chunked\r\n\r\n"), True)
    _check("no chunked", _has_te_chunked(b"Host: a\r\nContent-Length: 5\r\n\r\n"), False)
    _check("case insensitive", _has_te_chunked(b"transfer-encoding: chunked\r\n"), True)
    _check("empty", _has_te_chunked(b""), False)
    _check("te not chunked", _has_te_chunked(b"Transfer-Encoding: gzip\r\n"), False)
    _check("multi-value chunked,gzip", _has_te_chunked(b"Transfer-Encoding: chunked, gzip\r\n"), True)
    _check("multi-value gzip,chunked", _has_te_chunked(b"Transfer-Encoding: gzip, chunked\r\n"), True)

# ================================================================
# _strip_header
# ================================================================
def test_strip_header():
    _check("strip one", _strip_header(b"Host: a\r\nX-Foo: bar\r\n\r\n", b"X-Foo"), b"Host: a")
    _check("missing", _strip_header(b"Host: a\r\n\r\n", b"X-Foo"), b"Host: a")
    _check("case", _strip_header(b"Host: a\r\ncontent-length: 5\r\n", b"Content-Length"), b"Host: a")
    _check("empty", _strip_header(b"", b"X"), b"")

# ================================================================
# _get_content_length
# ================================================================
def test_get_content_length():
    _check("single CL", _get_content_length(b"Content-Length: 42\r\n"), 42)
    _check("absent", _get_content_length(b"Host: a\r\n"), 0)
    _check("multiple CL", _get_content_length(b"Content-Length: 5\r\nContent-Length: 10\r\n"), -1)
    _check("negative", _get_content_length(b"Content-Length: -1\r\n"), -1)
    _check("overflow", _get_content_length(f"Content-Length: {MAX_CONTENT_LENGTH + 1}\r\n".encode()), -1)
    _check("non-numeric", _get_content_length(b"Content-Length: abc\r\n"), -1)
    _check("zero", _get_content_length(b"Content-Length: 0\r\n"), 0)

# ================================================================
# 集成测试：mock upstream 返回 429，验证重试
# ================================================================
async def _read_request(reader):
    """Read a full HTTP request from reader, return method line + headers + body."""
    data = b""
    # Read until end of headers
    while True:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
        if not chunk:
            return None, b""
        data += chunk
        if b"\r\n\r\n" in data:
            break
    header_end = data.index(b"\r\n\r\n") + 4
    first_line = data[:header_end]
    rest = data[header_end:]
    # Parse Content-Length from headers
    cl = 0
    for h in first_line.split(b"\r\n"):
        if h.lower().startswith(b"content-length:"):
            cl = int(h.split(b":", 1)[1].strip())
    if cl > 0:
        body = rest[:cl]
        while len(body) < cl:
            more = await asyncio.wait_for(reader.read(cl - len(body)), timeout=5)
            if not more:
                break
            body += more
    else:
        body = b""
    return first_line, body


async def test_429_retry():
    """Mock upstream 返回 429，验证 proxy_429 重试一次后得 200。"""
    import proxy_429 as p

    clash_called = asyncio.Event()
    mock_upstream_count = 0
    got_429 = asyncio.Event()

    async def mock_clash(reader, writer):
        # Handle both GET (list nodes) and PUT (switch)
        method_line = await asyncio.wait_for(reader.readline(), timeout=5)
        if method_line.startswith(b"GET"):
            # Drain headers
            while True:
                h = await asyncio.wait_for(reader.readline(), timeout=5)
                if h in (b"\r\n", b"\n", b""):
                    break
            body = json.dumps({"all": ["node-a", "node-b"], "now": "node-a"}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        else:
            # PUT — read full body
            while True:
                h = await asyncio.wait_for(reader.readline(), timeout=5)
                if h in (b"\r\n", b"\n", b""):
                    break
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        clash_called.set()
        writer.close()

    async def mock_upstream(reader, writer):
        nonlocal mock_upstream_count
        method_line, _ = await _read_request(reader)
        if method_line is None:
            writer.close()
            return
        mock_upstream_count += 1
        if mock_upstream_count == 1:
            got_429.set()
            writer.write(b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\n\r\n")
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
        writer.close()

    clash_server = await asyncio.start_server(mock_clash, "127.0.0.1", 0)
    clash_port = clash_server.sockets[0].getsockname()[1]
    upstream_server = await asyncio.start_server(mock_upstream, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    proxy_server = await asyncio.start_server(p.handle, "127.0.0.1", 0)
    proxy_port = proxy_server.sockets[0].getsockname()[1]

    old_upstream = p.UPSTREAM
    old_clash = p.SINGBOX_API
    p.UPSTREAM = ("127.0.0.1", upstream_port)
    p.SINGBOX_API = f"http://127.0.0.1:{clash_port}"

    async with clash_server, upstream_server, proxy_server:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", proxy_port), timeout=5)
            writer.write(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()
            resp_line = await asyncio.wait_for(reader.readline(), timeout=10)
            code = int(resp_line.split(b" ")[1])
            _check("429 retry → 200", code, 200)
            _check_bool("第一次收到 429", got_429.is_set())
            # drain rest of response
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=3)
                if not chunk:
                    break
            writer.close()
            # verify Clash API called
            await asyncio.wait_for(clash_called.wait(), timeout=3)
            _check_bool("Clash API 被调用", clash_called.is_set())
        except Exception as e:
            _check_bool(f"429 测试异常: {e}", False)

    p.UPSTREAM = old_upstream
    p.SINGBOX_API = old_clash


# ================================================================
# Run
# ================================================================
def main():
    unit_tests = [
        ("_has_te_chunked", test_has_te_chunked),
        ("_strip_header", test_strip_header),
        ("_get_content_length", test_get_content_length),
    ]
    integration_tests = [
        ("429 retry", test_429_retry),
    ]

    for name, fn in unit_tests:
        print(f"\n{'='*50}\n单元测试: {name}\n{'='*50}")
        try:
            fn()
            print(f"  ✅ 通过")
        except Exception as e:
            global FAIL
            FAIL += 1
            print(f"  ❌ 异常: {e}")
            import traceback; traceback.print_exc()

    for name, fn in integration_tests:
        print(f"\n{'='*50}\n集成测试: {name}\n{'='*50}")
        try:
            asyncio.run(fn())
            print(f"  ✅ 通过")
        except Exception as e:
            FAIL += 1
            print(f"  ❌ 异常: {e}")
            import traceback; traceback.print_exc()

    total = PASS + FAIL
    print(f"\n{'='*50}")
    print(f"汇总: {PASS}/{total} 通过")
    if FAIL:
        print(f"❌ {FAIL} 个失败")
    else:
        print("✅ 全部通过")
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
