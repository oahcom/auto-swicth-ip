#!/usr/bin/env python3
"""TCP 转发器：本机端口 → 经机场节点 SOCKS5 → CF MASQUE 端点。

WHY 这个东西是「救 44 个节点」的关键：官方 WARP 客户端（warp-svc）的
h2-only 模式让 MASQUE 跑在 **HTTP/2 over TCP** 上（实测连 162.159.192.1:443），
而那批「不能承载 WARP」的机场节点 **TCP 是通的、UDP 大半不通**。走 TCP 就
绕开了整个 UDP 封锁问题 —— 不需要 UDP 桥、不需要 usque，34 个「不转 UDP」
的节点理论上都能用。

配合一条 iptables 把 warp-svc 的流量吸过来：

    sudo iptables -t nat -A OUTPUT -p tcp -d 162.159.192.0/24 \
        --dport 443 -j REDIRECT --to-port <listen>

TLS 是 warp-svc ↔ Cloudflare 端到端的，这里只搬字节，不改证书链。

用法
----
    python3 tools/tcp_relay.py --listen 9443 --node-socks 20800 \
        --dst 162.159.192.1:443
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading


def socks_connect(proxy: tuple[str, int], dst: tuple[str, int],
                  timeout: float = 10.0) -> socket.socket:
    s = socket.create_connection(proxy, timeout=timeout)
    s.sendall(b"\x05\x01\x00")
    if s.recv(2) != b"\x05\x00":
        raise RuntimeError("socks 握手失败")
    s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(dst[0])
              + struct.pack(">H", dst[1]))
    r = s.recv(10)
    if len(r) < 2 or r[1] != 0:
        raise RuntimeError(f"socks connect 被拒: {r!r}")
    # create_connection 的超时会留在 socket 上，pump 里空闲 10s 就抛
    # TimeoutError（OSError 子类），被当成 EOF 关掉长连接。数据面必须阻塞。
    s.settimeout(None)
    return s


def pump(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def serve(listen: int, node_socks: int, dst: tuple[str, int]) -> None:
    """单端口监听。多端口由多个线程各跑一份，见 --port-map。"""
    proxy = ("127.0.0.1", node_socks)

    n_ok = [0]

    def handle(c: socket.socket) -> None:
        peer = c.getpeername() if c.fileno() >= 0 else ("?", 0)
        try:
            up = socks_connect(proxy, dst)
        except Exception as e:
            print(f"[{listen}] 上游失败: {e}", flush=True)
            c.close()
            return
        n_ok[0] += 1
        print(f"[{listen}] 连接#{n_ok[0]} 来自 {peer} → 经节点 → {dst}",
              flush=True)
        t = threading.Thread(target=pump, args=(c, up), daemon=True)
        t.start()
        pump(up, c)
        t.join(timeout=5)
        c.close()
        up.close()

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", listen))
    srv.listen(16)
    print(f"relay 127.0.0.1:{listen} → socks {node_socks} → {dst}", flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser()
    # 单端口（旧用法）与多端口（--port-map）二选一。多端口是为了让 N 条 H2 线
    # 共用一个进程：每条线只要一个本地监听，44 条线不该起 44 个 python。
    ap.add_argument("--listen", type=int, help="本机监听端口（单端口模式）")
    ap.add_argument("--port-map", action="append", default=[],
                    metavar="LISTEN=NODE_SOCKS",
                    help="多端口模式，可重复；与 --listen/--node-socks 互斥")
    ap.add_argument("--node-socks", type=int, help="节点 SOCKS5 端口")
    ap.add_argument("--dst", default="162.159.192.1:443", help="CF MASQUE 端点")
    args = ap.parse_args()

    ip, _, port = args.dst.rpartition(":")
    dst = (ip, int(port or 443))

    if args.port_map:
        if args.listen or args.node_socks:
            ap.error("--port-map 与 --listen/--node-socks 不能同时用")
        pairs = []
        for spec in args.port_map:
            l, _, n = spec.partition("=")
            pairs.append((int(l), int(n)))
        for listen, node_socks in pairs:
            threading.Thread(target=serve, args=(listen, node_socks, dst),
                             daemon=True).start()
        print(f"多端口 relay：{len(pairs)} 条 → {dst}", flush=True)
        threading.Event().wait()
        return 0

    if not args.listen or not args.node_socks:
        ap.error("单端口模式需要 --listen 与 --node-socks")
    serve(args.listen, args.node_socks, dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
