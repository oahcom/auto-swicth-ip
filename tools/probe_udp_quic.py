#!/usr/bin/env python3
"""拆开测三件事：节点的 UDP 能力、QUIC 能力、到 CF 端点的能力。

WHY 需要这个工具：44 个节点用 MASQUE 借道时通率只有 ~20%，但「不通」
这个现象至少混了四种完全不同的原因，不拆开就会得出错误结论：

    1. 节点根本不转 UDP            → DNS 探测也不通
    2. 节点转 UDP 但不转 UDP/443   → DNS 通，QUIC/443 不通
    3. 节点转 UDP/443 但拦 QUIC     → DNS 通、QUIC 到任何家都不通
    4. 节点只拦 CF 的 IP/网段       → DNS 通、QUIC 到别人通、到 CF 不通
                                     （这条最乐观：换端点 IP 就能解）

探测手法：发一个 version = 0x0a0a0a0a 的 QUIC long-header 包。
RFC 9000 要求服务端收到不支持版本时回 Version Negotiation —— 这是
不需要握手、不需要凭证、不解密就能触发的最小 UDP 往返，正好当探针。

用法
----
    python3 tools/probe_udp_quic.py --node-cfg /tmp/mq_node_7.json --socks-port 20600
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DIR))
import warp_wg  # noqa: E402

# unsupported version：任何 QUIC 实现都应回 Version Negotiation
QUIC_PROBE_VER = 0x0A0A0A0A


def quic_vn_packet() -> bytes:
    """构造触发 Version Negotiation 的最小 QUIC 包（补齐 1200 字节）。"""
    b = bytearray()
    b.append(0xC0)                       # long header + Initial
    b += struct.pack(">I", QUIC_PROBE_VER)
    b.append(8)
    b += os.urandom(8)                   # DCID
    b.append(8)
    b += os.urandom(8)                   # SCID
    b += b"\x00" * max(0, 1200 - len(b))  # QUIC 要求最小 1200 字节
    return bytes(b)


def dns_query(name: str) -> bytes:
    q = b""
    for part in name.encode().split(b"."):
        q += bytes([len(part)]) + part
    q += b"\x00"
    return struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + q + b"\x00\x01\x00\x01"


def parse_a(resp: bytes) -> list[str]:
    """从 DNS 应答里抠 A 记录（够用即可，不做完整解析）。"""
    ips = []
    try:
        qd, an = struct.unpack(">HH", resp[4:8])
        i = 12
        for _ in range(qd):
            while resp[i] != 0:
                i += resp[i] + 1
            i += 5
        for _ in range(an):
            if i + 12 > len(resp):
                break
            if resp[i] & 0xC0:            # 压缩指针
                i += 2
            else:
                while resp[i] != 0:
                    i += resp[i] + 1
                i += 1
            typ, cls, ttl, rdlen = struct.unpack(">HHIH", resp[i:i + 10])
            i += 10
            if typ == 1 and rdlen == 4:
                ips.append(socket.inet_ntoa(resp[i:i + 4]))
            i += rdlen
    except Exception:
        pass
    return ips


def probe(port: int, dst: tuple[str, int], payload: bytes,
          timeout: float = 6.0, tries: int = 2) -> tuple[bool, str]:
    """经节点 SOCKS-UDP 发一次探测，返回 (是否有回包, 描述)。"""
    for _ in range(tries):
        r = warp_wg._socks_udp(port, dst, payload, timeout=timeout)
        if r:
            # Version Negotiation 的特征：首字节高位为 1 且 version 字段为 0
            if len(r) >= 5 and (r[0] & 0x80) and r[1:5] == b"\x00\x00\x00\x00":
                return True, "Version Negotiation"
            return True, f"{len(r)}B 回包"
        time.sleep(0.3)
    return False, "无回包"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socks-port", type=int, required=True, help="节点 SOCKS5 端口")
    ap.add_argument("--extra", default="", help="额外 ip:port 目标，逗号分隔")
    args = ap.parse_args()
    port = args.socks_port

    print(f"节点 socks: {port}\n")

    # 1) UDP 基本能力：DNS
    ok, desc = probe(port, ("8.8.8.8", 53), dns_query("www.google.com"))
    print(f"[1] UDP 基本能力   DNS 8.8.8.8:53      -> {'通' if ok else '不通'} ({desc})")
    if not ok:
        print("    ⇒ 节点不转 UDP，后面不用测了")
        return 1

    # 2) 借节点 DNS 找一个非 CF 的 QUIC 目标（对照用）
    r = warp_wg._socks_udp(port, ("8.8.8.8", 53), dns_query("www.google.com"), timeout=6)
    g_ip = parse_a(r)[0] if r else None
    r2 = warp_wg._socks_udp(port, ("8.8.8.8", 53), dns_query("quic.rocks"), timeout=6)
    q_ip = parse_a(r2)[0] if r2 else None

    targets = [
        ("CF MASQUE 当前端点", "162.159.198.2", 443),
        ("CF 162.159.192.1", "162.159.192.1", 443),
        ("CF 188.114.97.1", "188.114.97.1", 443),
        ("CF 1.1.1.1", "1.1.1.1", 443),
    ]
    if g_ip:
        targets.append((f"非CF google {g_ip}", g_ip, 443))
    if q_ip:
        targets.append((f"非CF quic.rocks {q_ip}", q_ip, 4433))
    for kv in filter(None, args.extra.split(",")):
        ip, _, p = kv.rpartition(":")
        targets.append((f"自定义 {ip}", ip, int(p or 443)))

    print("\n[2] QUIC 探测（unsupported-version 触发 Version Negotiation）")
    verdict = {}
    for label, ip, p in targets:
        ok, desc = probe(port, (ip, p), quic_vn_packet(), timeout=6)
        verdict[label] = ok
        print(f"    {label:28s} {ip}:{p:<5d} -> {'通 ' if ok else '不通'} ({desc})")

    cf_ok = any(v for k, v in verdict.items() if k.startswith("CF"))
    noncf = [v for k, v in verdict.items() if not k.startswith("CF")]
    print("\n=== 判定 ===")
    if not any(verdict.values()):
        print("  QUIC 到任何目标都不通 → 节点拦 UDP/443 或拦 QUIC（原因 2 或 3）")
    elif cf_ok and all(noncf):
        print("  CF 与非 CF 都通 → 节点的 UDP/443 + QUIC 完全放行")
        print("  ⇒ 借道失败另有原因（usque 超时/设备/桥），不是节点封锁")
    elif not cf_ok and any(noncf):
        print("  只拦 CF：非 CF 通、CF 不通 → 换 CF 端点 IP 可能直接解开（原因 4）")
    else:
        print(f"  混合结果，逐项看上面：{verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
