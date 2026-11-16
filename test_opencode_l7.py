#!/usr/bin/env python3
"""第八闸口 L7 请求拦截器（asip/opencode_l7.py）契约测试——全 loopback 假件。

不碰真网络 / 真 UDS / 真日志：假上游 HTTP、假 CONNECT 出口、假 rotate UDS
全部跑在 127.0.0.1 与 tmp_path；TLS 缝 _wrap_tls 恒等（假件不玩证书）。
任务书测试单的硬用例：
  ① 2xx+SSE 分块逐块流式透传（头部原样，反证整包缓冲）
  ② 429 → rotate（抓请求体：reason/egress_proxy_url）→ 换 ok 重试成功
     —— 换 ok 语义核心用例
  ③ transport 断连 → reason=transport → 重试成功（switched 无 url 仍走默认出口）
  ④ 两次换 ok 仍 429 → 透传 429 + 日志 final=no_ok_channel + rotate 恰好 2 次
  ⑤ coalesced → 重试一次当前通道不换新
  ⑥ 非故障 500 → 透传且 rotate 零调用
  ⑦ GET /healthz → 200
红绿反证（改条件必红）在自审报告贴尾行，用例内不做自残断言。
"""
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading

import pytest

from asip import opencode_l7


# ── 假上游（HTTP，脚本化应答；记录收到的请求）─────────────────────────

class _UpstreamHandler(socketserver.BaseRequestHandler):
    def handle(self):
        srv = self.server
        conn = self.request
        conn.settimeout(5)
        try:
            req = self._read_request(conn)
        except (OSError, ValueError):
            return
        srv.requests.append(req)
        resp = srv.script[min(srv.take_index(), len(srv.script) - 1)]
        if resp.get("close_immediately"):
            return  # transport 故障假件：收完请求就断，响应头都不给
        self._write_response(conn, resp)

    def _read_request(self, conn):
        f = conn.makefile("rb")
        raw = f.readline(65536)
        parts = raw.decode("latin-1").split()
        headers = {}
        while True:
            ln = f.readline(65536)
            if not ln or ln in (b"\r\n", b"\n"):
                break
            if b":" in ln:
                k, v = ln.split(b":", 1)
                headers[k.strip().lower().decode("latin-1")] = \
                    v.strip().decode("latin-1")
        n = int(headers.get("content-length") or 0)
        body = f.read(n) if n else b""
        return {"method": parts[0] if parts else "",
                "path": parts[1] if len(parts) > 1 else "",
                "headers": headers, "body": body}

    def _write_response(self, conn, resp):
        status = resp.get("status", 200)
        reason = resp.get("reason", "OK")
        head = ["HTTP/1.1 %d %s" % (status, reason)]
        head += ["%s: %s" % (k, v) for k, v in (resp.get("headers") or [])]
        if "chunks" in resp:
            # SSE/分块应答：逐块发，gate 项在 gate 置位前不发（流式证明用）
            head.append("Transfer-Encoding: chunked")
            conn.sendall(("\r\n".join(head)
                          + "\r\nConnection: close\r\n\r\n").encode("latin-1"))
            for data, gate in resp["chunks"]:
                if gate is not None:
                    gate.wait(30)
                conn.sendall(b"%x\r\n" % len(data) + data + b"\r\n")
            conn.sendall(b"0\r\n\r\n")
        else:
            body = resp.get("body", b"")
            head.append("Content-Length: %d" % len(body))
            conn.sendall(("\r\n".join(head)
                          + "\r\nConnection: close\r\n\r\n").encode("latin-1")
                         + body)


class FakeUpstream:
    """脚本化假上游：每个隧道连接应答一条脚本（超出重复最后一条）。"""

    def __init__(self):
        self.script = []
        self.requests = []
        self._lock = threading.Lock()
        self._idx = 0
        self._srv = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), _UpstreamHandler)
        self._srv.daemon_threads = True
        self._srv.script = self.script
        self._srv.requests = self.requests
        self._srv.take_index = self._take_index
        self.addr = ("127.0.0.1", self._srv.server_address[1])
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def _take_index(self):
        with self._lock:
            i = self._idx
            self._idx += 1
        return i

    def set_script(self, *responses):
        self.script[:] = list(responses)

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


# ── 假出口代理（HTTP CONNECT → 中继到假上游；记录 CONNECT 目标）────────

class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        conn = self.request
        conn.settimeout(5)
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, rest = buf.split(b"\r\n\r\n", 1)
            first = head.split(b"\r\n", 1)[0].decode("latin-1")
            assert first.startswith("CONNECT"), first
            target = first.split()[1]
            self.server.owner.targets.append(target)
            up = socket.create_connection(self.server.owner.upstream_addr,
                                          timeout=5)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                up.sendall(rest)
        except (OSError, AssertionError):
            return

        def pump(src, dst):
            try:
                while True:
                    b = src.recv(65536)
                    if not b:
                        break
                    dst.sendall(b)
            except OSError:
                pass
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        t = threading.Thread(target=pump, args=(up, conn), daemon=True)
        t.start()
        pump(conn, up)
        t.join(5)
        up.close()


class FakeEgress:
    """假 slot-8 出口：CONNECT 协议与真 sing-box 同面，目标记账用于断言换 ok。"""

    def __init__(self, upstream_addr):
        self.upstream_addr = upstream_addr
        self.targets = []          # ["opencode.ai:443", ...]
        self._srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0),
                                                    _ProxyHandler)
        self._srv.daemon_threads = True
        self._srv.owner = self
        self.port = self._srv.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


# ── 假 rotate UDS（POST /rotate-opencode-gate，抓请求体/token）──────────

class _ThreadingUnix(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class _RotateHandler(socketserver.BaseRequestHandler):
    def handle(self):
        conn = self.request
        conn.settimeout(5)
        try:
            f = conn.makefile("rb")
            req_line = f.readline(65536)
            headers = {}
            while True:
                ln = f.readline(65536)
                if not ln or ln in (b"\r\n", b"\n"):
                    break
                if b":" in ln:
                    k, v = ln.split(b":", 1)
                    headers[k.strip().lower().decode("latin-1")] = \
                        v.strip().decode("latin-1")
            n = int(headers.get("content-length") or 0)
            raw = f.read(n) if n else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            self.server.owner.calls.append({
                "path": req_line.decode("latin-1").split()[1],
                "token": headers.get("x-asip-token", ""),
                "body": body,
            })
            resp = self.server.owner.next_response()
            payload = json.dumps(resp).encode("utf-8")
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\nConnection: close\r\n\r\n"
                % len(payload) + payload)
        except (OSError, ValueError, IndexError):
            pass


class FakeRotate:
    """假换 ok 端点：应答脚本化（缺省=真换了但没给 switch_to_url）。"""

    DEFAULT = {"ok": True, "switched": True, "reason": "ok",
               "switch_to_node": "nX", "switch_to_url": None}

    def __init__(self, sock_dir):
        self.sock_path = os.path.join(str(sock_dir), "rotate-test.sock")
        self.calls = []
        self._responses = []
        self._lock = threading.Lock()
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        self._srv = _ThreadingUnix(self.sock_path, _RotateHandler)
        self._srv.owner = self
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def respond(self, resp):
        with self._lock:
            self._responses.append(resp)

    def next_response(self):
        with self._lock:
            if self._responses:
                return self._responses.pop(0)
        return dict(self.DEFAULT)

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass


# ── 沙箱：假件 + monkeypatch 配置面 ───────────────────────────────────

@pytest.fixture
def l7(monkeypatch, tmp_path):
    up = FakeUpstream()
    proxy1 = FakeEgress(up.addr)   # 默认出口（生产=127.0.0.1:7898）
    proxy2 = FakeEgress(up.addr)   # 换 ok 后的 switch_to_url 出口
    rot = FakeRotate(tmp_path)
    token_file = tmp_path / "api-token"
    token_file.write_text("tok-abc", encoding="utf-8")
    log_path = tmp_path / "opencode-l7.log"

    monkeypatch.setattr(opencode_l7, "DEFAULT_EGRESS_URL", proxy1.url)
    monkeypatch.setattr(opencode_l7, "ROTATE_SOCK", rot.sock_path)
    monkeypatch.setattr(opencode_l7, "ROTATE_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(opencode_l7, "LOG_PATH", str(log_path))
    monkeypatch.setattr(opencode_l7, "_wrap_tls",
                        lambda sock, server_hostname: sock)

    srv = opencode_l7.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class Env:
        pass

    env = Env()
    env.port = srv.server_address[1]
    env.upstream = up
    env.proxy1 = proxy1
    env.proxy2 = proxy2
    env.rotate = rot
    env.log_path = log_path
    env.token = "tok-abc"
    yield env
    srv.shutdown()
    srv.server_close()
    up.close()
    proxy1.close()
    proxy2.close()
    rot.close()


# ── 客户端假件（raw socket；测试请求都带 Connection: close）────────────

def _req(path="/v1/chat/completions", body=b'{"m":1}'):
    head = ["POST %s HTTP/1.1" % path,
            "Host: 127.0.0.1:7899",
            "Content-Type: application/json",
            "Authorization: Bearer sk-test",
            "Content-Length: %d" % len(body),
            "Connection: close"]
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


def _raw_request(port, raw, timeout=15.0):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(raw)
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)
    finally:
        s.close()


def _parse(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    hdrs = {}
    for ln in lines[1:]:
        if b":" in ln:
            k, v = ln.split(b":", 1)
            hdrs[k.strip().lower().decode("latin-1")] = \
                v.strip().decode("latin-1")
    return status, hdrs, body


def _read_log(path):
    if not os.path.exists(str(path)):
        return []
    out = []
    with open(str(path), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ── ⑦ /healthz ────────────────────────────────────────────────────────

def test_healthz(l7):
    raw = _raw_request(l7.port,
                       b"GET /healthz HTTP/1.1\r\nHost: x\r\n"
                       b"Connection: close\r\n\r\n")
    status, _, body = _parse(raw)
    assert (status, body) == (200, b"ok\n")
    assert l7.upstream.requests == []   # 不转发上游
    assert l7.rotate.calls == []
    assert _read_log(l7.log_path) == []  # 检查端点不进 JSONL


# ── ① 2xx + SSE 分块：逐块流式透传，头部原样 ──────────────────────────

def test_2xx_sse_chunked_stream_passthrough(l7):
    gate = threading.Event()
    l7.upstream.set_script({
        "status": 200, "reason": "OK",
        "headers": [("Content-Type", "text/event-stream"),
                    ("X-Upstream", "u1")],
        "chunks": [(b"data: a\n\n", None),
                   (b"data: b\n\n", gate),
                   (b"data: c\n\n", None)],
    })
    s = socket.create_connection(("127.0.0.1", l7.port), timeout=8)
    try:
        s.sendall(_req())
        buf = b""
        while b"data: a" not in buf:
            buf += s.recv(65536)
        # 流式铁证：上游的第 2 块此刻还没发出（卡在 gate 上），反代若整包
        # 缓冲这里必然超时红；能走到这行说明首块已逐块到达客户端。
        assert b"data: b" not in buf
        gate.set()
        while True:
            b = s.recv(65536)
            if not b:
                break
            buf += b
    finally:
        s.close()

    assert buf.index(b"data: a") < buf.index(b"data: b") < buf.index(b"data: c")
    low = buf.lower()
    assert low.startswith(b"http/1.1 200")
    assert b"content-type: text/event-stream" in low   # 头部原样
    assert b"x-upstream: u1" in low
    assert l7.rotate.calls == []
    rows = _read_log(l7.log_path)
    assert len(rows) == 1
    assert rows[0]["final"] == "ok" and rows[0]["retried"] == 0
    assert rows[0]["status"] == 200


# ── ② 429 → rotate → 换 ok 重试成功（换 ok 语义核心）──────────────────

def test_429_rotate_switch_ok_retry_succeeds(l7):
    l7.upstream.set_script(
        {"status": 429, "reason": "Too Many Requests",
         "headers": [("Content-Type", "text/plain"), ("Retry-After", "5"),
                     ("X-Upstream", "u1")],
         "body": b"rate limited"},
        {"status": 200, "reason": "OK",
         "headers": [("Content-Type", "application/json")],
         "body": b'{"ok":true}'},
    )
    l7.rotate.respond({"ok": True, "switched": True, "reason": "ok",
                       "switch_to_node": "n2", "switch_to_kind": "warp",
                       "switch_to_url": l7.proxy2.url})
    raw = _raw_request(l7.port, _req())
    status, _, body = _parse(raw)
    assert (status, body) == (200, b'{"ok":true}')

    # rotate 被调且请求体契约字段齐全（reason/egress_proxy_url/…）
    assert len(l7.rotate.calls) == 1
    call = l7.rotate.calls[0]
    assert call["path"] == "/rotate-opencode-gate"
    assert call["token"] == l7.token          # X-Asip-Token 接线
    rb = call["body"]
    assert rb["reason"] == "rate429"
    assert rb["egress_proxy_url"] == l7.proxy1.url
    assert rb["req_id"] and rb["request_started_at"] > 0
    assert rb["retry_after_ms"] == 5000       # Retry-After: 5（秒）→ 毫秒

    # 换 ok 落点：重试走 switch_to_url（proxy2），原出口只被打过一次
    assert l7.proxy1.targets == ["opencode.ai:443"]
    assert l7.proxy2.targets == ["opencode.ai:443"]
    # 请求头原样：Authorization 透传到上游，Host 重写为上游域名
    assert l7.upstream.requests[1]["headers"]["authorization"] \
        == "Bearer sk-test"
    assert l7.upstream.requests[1]["headers"]["host"] == "opencode.ai"

    rows = _read_log(l7.log_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["final"] == "ok" and r["retried"] == 1 and r["status"] == 200
    assert r["upstream_via"] == l7.proxy2.url
    assert r["rotate_actions"] == [{"reason": "rate429", "outcome": "ok",
                                    "switched": True,
                                    "switch_to_url": l7.proxy2.url,
                                    "switch_to_node": "n2",
                                    "switch_to_kind": "warp"}]


# ── ③ transport 断连 → rotate → 重试成功 ──────────────────────────────

def test_transport_fault_rotate_retry_succeeds(l7):
    l7.upstream.set_script(
        {"close_immediately": True},
        {"status": 200, "reason": "OK", "headers": [], "body": b"recovered"},
    )
    # switched=true 但回包没给 switch_to_url：重试仍走默认出口（换 ok 协议）
    l7.rotate.respond({"ok": True, "switched": True, "reason": "ok",
                       "switch_to_node": "n3", "switch_to_url": None})
    raw = _raw_request(l7.port, _req())
    status, _, body = _parse(raw)
    assert (status, body) == (200, b"recovered")

    assert len(l7.rotate.calls) == 1
    assert l7.rotate.calls[0]["body"]["reason"] == "transport"
    assert l7.rotate.calls[0]["body"]["egress_proxy_url"] == l7.proxy1.url
    assert len(l7.proxy1.targets) == 2        # 两次尝试都在默认出口
    assert l7.proxy2.targets == []
    rows = _read_log(l7.log_path)
    assert rows[0]["final"] == "ok" and rows[0]["retried"] == 1


# ── ④ 两次换 ok 仍 429 → 透传 + final=no_ok_channel，rotate 恰好 2 次 ──

def test_two_switch_ok_attempts_still_429_no_ok_channel(l7):
    l7.upstream.set_script(
        {"status": 429, "reason": "Too Many Requests", "headers": [],
         "body": b"err1"},
        {"status": 429, "reason": "Too Many Requests", "headers": [],
         "body": b"err2"},
        {"status": 429, "reason": "Too Many Requests", "headers": [],
         "body": b"err3"},
    )
    l7.rotate.respond({"ok": True, "switched": True, "reason": "ok",
                       "switch_to_node": "n2", "switch_to_kind": "warp",
                       "switch_to_url": l7.proxy2.url})
    l7.rotate.respond({"ok": True, "switched": True, "reason": "ok",
                       "switch_to_node": "n3",
                       "switch_to_url": l7.proxy2.url})
    raw = _raw_request(l7.port, _req())
    status, _, body = _parse(raw)
    assert status == 429
    assert body == b"err3"                 # 最后一个上游错误 429 原样透传
    assert len(l7.rotate.calls) == 2       # 恰好 2 次，不许多打
    assert len(l7.upstream.requests) == 3  # 3 次尝试：首打 + 2 换 ok 重试

    rows = _read_log(l7.log_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["final"] == "no_ok_channel"   # 判「无可用通道」
    assert r["status"] == 429 and r["retried"] == 2
    assert len(r["rotate_actions"]) == 2


# ── ⑤ coalesced：重试一次当前通道，不换新 ─────────────────────────────

def test_coalesced_retries_same_channel(l7):
    l7.upstream.set_script(
        {"status": 429, "reason": "Too Many Requests", "headers": [],
         "body": b"limited"},
        {"status": 200, "reason": "OK", "headers": [], "body": b"recovered"},
    )
    l7.rotate.respond({"ok": True, "switched": False, "reason": "coalesced",
                       "switch_to_node": None, "switch_to_url": None})
    raw = _raw_request(l7.port, _req())
    status, _, body = _parse(raw)
    assert (status, body) == (200, b"recovered")

    assert len(l7.rotate.calls) == 1       # 只报障一次（合并窗内不重复换）
    assert len(l7.proxy1.targets) == 2     # 两次尝试都走原通道
    assert l7.proxy2.targets == []         # 没换新出口
    r = _read_log(l7.log_path)[0]
    assert r["final"] == "ok" and r["retried"] == 1
    assert r["rotate_actions"][0]["outcome"] == "coalesced"
    assert r["rotate_actions"][0]["switched"] is False


# ── ⑥ 非故障 500：透传不干预，rotate 零调用 ───────────────────────────

def test_non_fault_500_passthrough_no_rotate(l7):
    l7.upstream.set_script(
        {"status": 500, "reason": "Internal Server Error",
         "headers": [("X-Upstream", "u9")], "body": b"boom"},
    )
    raw = _raw_request(l7.port, _req())
    status, hdrs, body = _parse(raw)
    assert (status, body) == (500, b"boom")
    assert hdrs.get("x-upstream") == "u9"   # 头部原样
    assert l7.rotate.calls == []            # 非故障不干预
    assert len(l7.upstream.requests) == 1   # 没有重试
    r = _read_log(l7.log_path)[0]
    assert r["final"] == "ok" and r["retried"] == 0
    assert r["rotate_actions"] == []
    assert r["upstream_via"] == l7.proxy1.url


# ── 直跑导入回归：asip/http.py 不得遮蔽标准库 http 包 ─────────────────

def test_standalone_import_not_shadowed_by_asip_http():
    """ExecStart 同款直跑的导入路径回归（2026-11-15 真机 systemd 启动即炸）：

    `python3 asip/opencode_l7.py` 时 sys.path[0]=asip/，仓内 asip/http.py
    遮蔽标准库 http 包 → `import http.server` 打到在仓模块（连锁 import
    requests 崩），服务起不来。pytest 从仓库根导入测不出这一层，必须按
    直跑的 sys.path 语义在子进程里复现。
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(opencode_l7.__file__)))
    code = ("import sys; sys.path.insert(0, 'asip'); "
            "import opencode_l7; import http.server; print('IMPORT_OK')")
    r = subprocess.run([sys.executable, "-c", code], cwd=repo,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "IMPORT_OK" in r.stdout


# ── ⑧ 请求流量字段（req_bytes/resp_bytes/err）────────────────────────

def test_log_includes_traffic_bytes_and_err(l7):
    l7.upstream.set_script(
        {"status": 200, "reason": "OK",
         "headers": [("Content-Type", "text/plain")],
         "body": b"0123456789"},                    # 响应体 10 字节
    )
    raw = _raw_request(l7.port, _req(body=b"hello-world"))   # 请求体 11 字节
    status, _, body = _parse(raw)
    assert (status, body) == (200, b"0123456789")
    rows = _read_log(l7.log_path)
    r = rows[0]
    assert r["req_bytes"] == 11
    assert r["resp_bytes"] == 10
    assert "err" not in r                          # 正常行无 err

    # 换 ok 耗尽的故障行：err 摘要含上游错误内容
    l7.upstream.set_script(
        {"status": 429, "reason": "Too Many Requests", "headers": [],
         "body": b'{"error":"Free usage limit reached"}'},
        {"status": 429, "reason": "Too Many Requests", "headers": [],
         "body": b'{"error":"Free usage limit reached"}'},
        {"status": 429, "reason": "Too Many Requests", "headers": [],
         "body": b'{"error":"Free usage limit reached"}'},
    )
    l7.rotate.respond({"ok": True, "switched": False,
                       "reason": "no_healthy_node"})
    raw = _raw_request(l7.port, _req())
    status, _, _ = _parse(raw)
    rows = _read_log(l7.log_path)
    r = rows[-1]
    assert r["final"] == "no_ok_channel" and r["status"] == 429
    assert "Free usage limit" in r["err"]


# ── ⑨ 流量视图（asip oc l7 呈现层）─────────────────────────────────────

def _run_view(argv, rows):
    import io
    buf_in = io.StringIO(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in rows))
    out = io.StringIO()
    old = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = buf_in, out
    try:
        opencode_l7.view_main(argv)
    finally:
        sys.stdin, sys.stdout = old
    return [l for l in out.getvalue().splitlines() if l]


def test_view_main_formats_and_filters():
    rows = [
        {"ts": 1794733431.055, "method": "GET", "path": "/v1/models",
         "status": 404, "ms": 790, "req_bytes": 0, "resp_bytes": 12,
         "upstream_via": "http://127.0.0.1:7898",
         "rotate_actions": [], "retried": 0, "final": "ok"},
        {"ts": 1794733455.442, "method": "POST", "path": "/v1/chat/completions",
         "status": 200, "ms": 3120, "req_bytes": 536, "resp_bytes": 45056,
         "upstream_via": "http://127.0.0.1:7898",
         "rotate_actions": [{"reason": "rate429", "outcome": "ok",
                             "switched": True}],
         "retried": 1, "final": "ok"},
        {"ts": 1794733460.0, "method": "POST", "path": "/v1/chat/completions",
         "status": 200, "ms": 900, "req_bytes": 6, "resp_bytes": 2,
         "upstream_via": "http://127.0.0.1:7898",
         "rotate_actions": [], "retried": 0, "final": "ok"},
    ]
    pretty = _run_view([], rows)
    assert len(pretty) == 3
    assert "GET" in pretty[0] and "/v1/models" in pretty[0]
    assert "404" in pretty[0] and "↑0B" in pretty[0] and "↓12B" in pretty[0]
    assert "×1换ok" in pretty[1] and "[rate429→ok]" in pretty[1]
    assert "44.0K" in pretty[1]                  # 45056B → 44.0K
    # 过滤：--rot 只留换过渠道的；--fail 只留失败（404）
    assert _run_view(["pretty", "rot"], rows) == [pretty[1]]
    fails = _run_view(["pretty", "fail"], rows)
    assert len(fails) == 1 and "404" in fails[0]
    raw = _run_view(["raw"], rows)
    assert raw[0].lstrip().startswith("{") and '"final"' in raw[0]


def test_view_reads_old_rows_without_new_fields():
    # 旧日志行（req_bytes/resp_bytes/err 之前的 schema）照常可读
    old_row = {"ts": 1794733431.055, "method": "GET", "path": "/v1/models",
               "status": 404, "ms": 790,
               "upstream_via": "http://127.0.0.1:7898",
               "rotate_actions": [], "retried": 0, "final": "ok"}
    out = _run_view([], [old_row])
    assert len(out) == 1 and "404" in out[0] and "GET" in out[0]


# ── ⑧ request_started_at 语义锁：每次尝试报本发时刻 ─────────────────────
# gate 的 already_retired 判据 = switched_at > request_started_at。重试若报
# 首发时刻，第二跳恒判迟到、永远不换渠道。这个锁保证换回旧语义必红。

def test_retry_reports_per_attempt_start_time(l7):
    l7.upstream.set_script(
        {"status": 429, "reason": "Too Many Requests", "headers": [],
         "body": b"e1"},
        {"status": 429, "reason": "Too Many Requests", "headers": [],
         "body": b"e2"},
        {"status": 200, "reason": "OK", "headers": [], "body": b"ok"},
    )
    l7.rotate.respond({"ok": True, "switched": True, "reason": "ok",
                       "switch_to_node": "n2"})
    l7.rotate.respond({"ok": True, "switched": True, "reason": "ok",
                       "switch_to_node": "n3"})
    raw = _raw_request(l7.port, _req())
    status, _, body = _parse(raw)
    assert (status, body) == (200, b"ok")
    assert len(l7.rotate.calls) == 2
    t1 = l7.rotate.calls[0]["body"]["request_started_at"]
    t2 = l7.rotate.calls[1]["body"]["request_started_at"]
    # 第二跳必须报自己的时刻（≥ 第一跳的；严格递增由时间保证，
    # 相等说明两跳共用了同一个时间戳 = 回到首发时刻语义）
    assert t2 >= t1
    # 两跳之间有真实间隔（各自 _open_tunnel 往返）→ 时刻必须不同
    assert t2 != t1
