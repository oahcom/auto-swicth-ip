#!/usr/bin/env python3
"""MASQUE-over-UDP 桥：把 usque 的 QUIC 包经机场节点转发到 CF 的 MASQUE 端点。

链路
----
    usque socks(QUIC)  ──UDP──▶  127.0.0.2:<listen>
                                      │ 本桥
                                      ▼  SOCKS5 UDP ASSOCIATE
                                 机场节点  ──▶  162.159.198.2:443 (CF MASQUE)
    usque socks(QUIC)  ◀──UDP──  127.0.0.2:<listen>（回包伪装成原地址）

WHY 需要这个桥：usque 不支持上游代理，而「不能跑 WireGuard」的机场节点
（44 个）其 UDP 转发表其实是通的 —— 它们拦的是 WireGuard 的协议特征，
MASQUE 长得和普通 QUIC/HTTP3 一样，中继放行。桥让 usque 无感地借道节点，
出口 colo 因此跟着节点地区走（香港节点→HKG，日本节点→NRT…）。

WHY 必须固定回包源地址：QUIC 用四元组标识连接，usque 侧看到的源必须是它
发包的目的地址（127.0.0.2:<listen>），否则连接立刻失效。

用法
----
    python3 tools/masque_bridge.py --node-socks 20023 --listen 44443
    python3 tools/masque_bridge.py --node-socks 20023 --listen 44443 \
        --dst 162.159.198.2:443 --verbose
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import struct
import sys
import threading
import time

LOG = logging.getLogger("masque_bridge")


def socks_udp_channel(proxy: tuple[str, int], timeout: float = 10.0):
    """SOCKS5 UDP ASSOCIATE：返回 (控制TCP, 本地UDP socket, 节点中继地址)。

    WHY 控制 TCP 必须常驻：RFC 1928 规定 UDP 关联在控制连接关闭时终止，
    机场中继普遍按这个实现 —— 控制连接一断，转发立刻静默失效（这个坑
    在早期版本里表现为「跑一段就没流量了」）。
    """
    ctl = socket.create_connection(proxy, timeout=timeout)
    ctl.sendall(b"\x05\x01\x00")
    if ctl.recv(2) != b"\x05\x00":
        raise RuntimeError("socks 握手失败：服务端不接受无认证")
    # UDP ASSOCIATE，客户端声明 0.0.0.0:0（接受任意来源）
    ctl.sendall(b"\x05\x03\x00\x01" + socket.inet_aton("0.0.0.0") + struct.pack(">H", 0))
    rep = ctl.recv(64)
    if len(rep) < 8 or rep[1] != 0:
        raise RuntimeError(f"UDP ASSOCIATE 被拒: {rep!r}")
    atyp = rep[3]
    if atyp == 1:
        bnd = (socket.inet_ntoa(rep[4:8]), struct.unpack(">H", rep[8:10])[0])
    elif atyp == 4:
        bnd = (socket.inet_ntop(socket.AF_INET6, rep[4:20]),
               struct.unpack(">H", rep[20:22])[0])
    else:
        n = rep[4]
        bnd = (socket.inet_ntoa(rep[5:9]), struct.unpack(">H", rep[9:11])[0])
    if bnd[0] in ("0.0.0.0", ""):
        bnd = (proxy[0], bnd[1])
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("0.0.0.0", 0))
    return ctl, udp, bnd


def wrap(dst: tuple[str, int], payload: bytes) -> bytes:
    return (b"\x00\x00\x00\x01" + socket.inet_aton(dst[0])
            + struct.pack(">H", dst[1]) + payload)


def unwrap(d: bytes):
    """剥 SOCKS UDP 头，返回 (payload, 来源地址)。"""
    atyp = d[3]
    if atyp == 1:
        return d[10:], (socket.inet_ntoa(d[4:8]), struct.unpack(">H", d[8:10])[0])
    if atyp == 4:
        return d[22:], (socket.inet_ntop(socket.AF_INET6, d[4:20]),
                        struct.unpack(">H", d[20:22])[0])
    n = d[4]
    return d[5 + n + 2:], (socket.inet_ntoa(d[5:9]), struct.unpack(">H", d[9:11])[0])


def main() -> int:
    ap = argparse.ArgumentParser(description="usque QUIC → 机场节点 → CF MASQUE 的 UDP 桥")
    ap.add_argument("--node-socks", type=int, required=True,
                    help="机场节点的 SOCKS5 端口（需支持 UDP ASSOCIATE）")
    ap.add_argument("--listen", type=int, required=True,
                    help="本机监听端口（usque 的 endpoint 指向 127.0.0.2:<listen>）")
    ap.add_argument("--dst", default=os.environ.get("BRIDGE_DST", "162.159.198.2:443"),
                    help="CF MASQUE 端点，默认 162.159.198.2:443")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ip, _, port = args.dst.rpartition(":")
    dst = (ip, int(port or 443))
    proxy = ("127.0.0.1", args.node_socks)

    ctl, udp, relay = socks_udp_channel(proxy)
    LOG.info("节点 UDP 中继: %s", relay)

    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.2", args.listen))
    LOG.info("桥就绪: 127.0.0.2:%d ↔ 节点:%d ↔ %s", args.listen, args.node_socks, dst)

    usque_addr: list[tuple[str, int] | None] = [None]
    stats = {"up": 0, "down": 0}

    def node_to_usque():
        """CF 回包（经节点中继）→ 剥头 → 从 127.0.0.2:<listen> 送回 usque。"""
        while True:
            try:
                d, _ = udp.recvfrom(65536)
            except OSError:
                return
            try:
                payload, src = unwrap(d)
            except Exception:
                continue
            if usque_addr[0]:
                try:
                    srv.sendto(payload, usque_addr[0])
                    stats["down"] += 1
                except OSError:
                    pass

    threading.Thread(target=node_to_usque, daemon=True).start()

    while True:
        try:
            d, addr = srv.recvfrom(65536)
        except OSError as e:
            LOG.error("监听 socket 出错: %s", e)
            return 1
        usque_addr[0] = addr
        try:
            udp.sendto(wrap(dst, d), relay)
            stats["up"] += 1
        except OSError as e:
            LOG.error("发往节点失败: %s", e)
            time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
