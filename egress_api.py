"""出口 API：一个进程 = 一条出口，换出口 = 重启。

为什么要"一条出口一个进程"（2026-10-29 定）：
进程内轮换会让「这一发到底从哪个 IP 出去的」变成一个隐藏状态 —— 出问题时
日志里看不出、复现不了。改成「启动参数决定出口」之后：
    - 想换线：改 --socks 重启，出口是确定的；
    - 想并行：起多个进程，各自一条线，端口不同；
    - 想扩容：换台机器重启，机器 IP 就是出口。
调用方只看得到 HTTP，看不到这层。

刻意**不做**的事（都是上一版删掉的）：
    - 不做池子、不做轮转、不做 429 自动换线。上游回什么就原样回什么，
      让调用方自己决定要不要重试 —— 服务端偷偷换 IP 才是真的难查。
    - 不碰 asip 的代理逻辑（sing-box / WARP 的换线仍然是 asip 的事）。

出口用 curl 而不是 requests：curl 自带 SOCKS5（--socks5-hostname），
不用往环境里装 PySocks；-N 能原样流式透传，SSE 不会被攒成一块。
只用标准库 + curl。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_UPSTREAM = "https://opencode.ai/inference/v1/chat/completions"

# curl 自己会重算这些头，原样透传会打架。
HOP_BY_HOP = {
    "host", "content-length", "connection", "accept-encoding", "transfer-encoding",
    "keep-alive", "proxy-connection", "te", "trailer", "upgrade",
}
STATUS_RE = re.compile(rb"^HTTP/\d(?:\.\d)?\s+(\d{3})")


def curl_header_args(raw: dict[str, str], auth: str, ua: str) -> list[str]:
    out: list[str] = []
    lower = set()
    for k, v in raw.items():
        lk = k.lower()
        lower.add(lk)
        if lk in HOP_BY_HOP:
            continue
        out += ["-H", f"{k}: {v}"]
    # 上游要的那几个头，调用方没给就补（Bearer public 是 free lane 的固定值）。
    if "authorization" not in lower:
        out += ["-H", f"Authorization: {auth}"]
    if "x-opencode-client" not in lower:
        out += ["-H", "x-opencode-client: cli"]
    if "user-agent" not in lower:
        out += ["-H", f"User-Agent: {ua}"]
    return out


def start_curl(socks: str, upstream: str, headers: list[str]) -> subprocess.Popen:
    cmd = ["curl", "-sS", "-N"]
    if socks:
        cmd += ["--socks5-hostname", socks]
    cmd += ["-X", "POST", upstream, "-D", "-", "--data-binary", "@-", "--max-time", "600"]
    cmd += headers
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)


def read_status(proc: subprocess.Popen, timeout: float = 120.0) -> tuple[int, list[bytes]]:
    """读响应头就停，不碰 body —— 状态码要早于第一块内容知道。"""
    head: list[bytes] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        head.append(line)
        if line in (b"\r\n", b"\n"):
            break
    status = 0
    for line in head:
        m = STATUS_RE.match(line)
        if m:
            status = int(m.group(1))
            break
    return status, head


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "asip-egress/2"

    def do_GET(self) -> None:
        cfg = self.server.cfg
        if self.path.startswith("/status"):
            return self._json({
                "egress": cfg["socks"] or "direct",
                "upstream": cfg["upstream"],
                "started_at": cfg["started_at"],
                "requests": cfg["requests"],
            })
        if self.path.startswith("/ip"):
            return self._json({"ip": self._probe_ip(), "egress": cfg["socks"] or "direct"})
        self._json({"service": "asip egress api", "endpoints": ["/status", "/ip", "/v1/chat/completions"]})

    def do_POST(self) -> None:
        if not self.path.startswith("/v1/"):
            return self._json({"error": "unknown path"}, 404)
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        cfg = self.server.cfg
        cfg["requests"] += 1
        headers = curl_header_args({k: v for k, v in self.headers.items()},
                                   cfg["auth"], cfg["ua"])
        proc = start_curl(cfg["socks"], cfg["upstream"], headers)
        try:
            proc.stdin.write(body)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        status, head = read_status(proc)
        if status == 0:
            err = b""
            try:
                err = proc.stderr.read() or b""
            except Exception:  # noqa: BLE001
                pass
            return self._json({"error": "upstream_unreachable",
                               "detail": err.decode(errors="replace")[:300]}, 502)
        self._stream(proc, status, head)

    def _probe_ip(self) -> str:
        cfg = self.server.cfg
        cmd = ["curl", "-sS"]
        if cfg["socks"]:
            cmd += ["--socks5-hostname", cfg["socks"]]
        cmd += ["--max-time", "20", "https://api.ipify.org"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=25)
            return r.stdout.decode(errors="replace").strip()[:40]
        except Exception:  # noqa: BLE001
            return ""

    def _stream(self, proc: subprocess.Popen, status: int, head: list[bytes]) -> None:
        self.send_response(status)
        for line in head[1:]:
            if b":" not in line:
                continue
            k, _, v = line.partition(b":")
            name = k.strip().decode(errors="replace")
            if name.lower() in HOP_BY_HOP:
                continue
            self.send_header(name, v.strip().decode(errors="replace"))
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _json(self, obj: dict, status: int = 200) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[egress] " + fmt % args + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="asip 出口 API（一条出口一个进程）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--socks", default="", help="这条进程的出口，如 127.0.0.1:1080；空=直连")
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    ap.add_argument("--auth", default="Bearer public")
    ap.add_argument("--ua", default="opencode/latest/2.0.26/cli")
    args = ap.parse_args(argv)

    if not shutil.which("curl"):
        print("需要 curl", file=sys.stderr)
        return 1

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.cfg = {
        "socks": args.socks, "upstream": args.upstream, "auth": args.auth,
        "ua": args.ua, "started_at": int(time.time()), "requests": 0,
    }
    print(f"出口 API http://{args.host}:{args.port} 出口={args.socks or 'direct'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
