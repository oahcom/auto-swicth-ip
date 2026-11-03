#!/usr/bin/env python3
"""SNI 改写转发器：把客户端 TLS ClientHello 里的 SNI 换成另一个域名再经节点转发。

WHY 存在这个工具：机场中继按 ClientHello 的 SNI 做七层路由（实测证书对照：
SNI=apple 回真 Apple 证书、SNI=cloudflareclient.com 黑名单掐断、目的 IP 被
无视）。而 CF WARP 的 API 域名 engage.cloudflareclient.com 解析到
162.159.192.1 —— 正是 MASQUE-over-H2 端点，且**不在黑名单**（黑名单只匹配
consumer-masque-proxy 这一个域名）。于是：

    warp-svc（SNI 硬编码 consumer-masque-proxy，被黑）
      → 本转发器把 SNI 改写成 engage.cloudflareclient.com
        → 中继按 SNI 把连接送到 engage 的真实 IP = 162.159.192.1 = MASQUE 端点
          → CF 回 cloudflareclient.com 同链证书 → warp-svc 校验通过 → 出口

改写不是简单替换字符串：SNI 变短后必须同步修正四层长度字段
（host_name / server_name_list / extension / handshake / record），
否则 CF 侧解析直接失败。

用法
----
    sudo python3 tools/sni_relay.py --listen 443 --node-socks 20900 \
        --sni engage.cloudflareclient.com --dst 162.159.192.1:443
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading

NEW_SNI = "engage.cloudflareclient.com"


def rewrite_sni(data: bytes, new_name: str) -> bytes | None:
    """把 ClientHello 的 SNI 替换为 new_name，返回改写后的完整记录。

    返回 None 表示这不是可解析的单记录 ClientHello（调用方按原样透传）。
    """
    if len(data) < 5 or data[0] != 0x16:
        return None
    rec_len = struct.unpack(">H", data[3:5])[0]
    if len(data) < 5 + rec_len:
        return None                      # 分片记录，不做（实测 warp-svc 单包发完）
    hs = data[5:5 + rec_len]
    if hs[0] != 0x01:
        return None
    hs_len = int.from_bytes(hs[1:4], "big")
    body = hs[4:4 + hs_len]
    if len(body) < 2 + 32 + 1:
        return None
    p = 2 + 32
    sid_len = body[p]; p += 1 + sid_len
    if p + 2 > len(body):
        return None
    cs_len = struct.unpack(">H", body[p:p + 2])[0]; p += 2 + cs_len
    comp_len = body[p]; p += 1 + comp_len
    if p + 2 > len(body):
        return None
    ext_total = struct.unpack(">H", body[p:p + 2])[0]; p += 2
    ext_end = p + ext_total
    delta = None
    out_ext = bytearray(body[p:ext_end])
    q = 0
    while q + 4 <= len(out_ext):
        etype, elen = struct.unpack(">HH", out_ext[q:q + 4])
        if etype == 0x0000:              # server_name
            lst = out_ext[q + 4:q + 4 + elen]
            # lst = list_len(2) + name_type(1) + name_len(2) + name + 余下条目
            if len(lst) >= 7 and lst[2] == 0:
                nlen = struct.unpack(">H", lst[3:5])[0]
                delta = len(new_name) - nlen
                new_lst = (struct.pack(">H", elen - 2 + delta) + b"\x00"
                           + struct.pack(">H", len(new_name))
                           + new_name.encode() + lst[5 + nlen:])
                out_ext[q:q + 4 + elen] = (struct.pack(">HH", 0x0000,
                                                       elen + delta) + new_lst)
                break
        q += 4 + elen
    if delta is None:
        return None                      # 没有 SNI，不改
    new_body = bytearray(body[:p]) + out_ext + body[ext_end:]
    struct.pack_into(">H", new_body, p - 2, ext_total + delta)
    new_hs = bytes([0x01]) + (hs_len + delta).to_bytes(3, "big") + bytes(new_body)
    rec = data[:3] + struct.pack(">H", rec_len + delta) + new_hs + data[5 + rec_len:]
    return rec


def socks_connect(proxy, dst, timeout=10.0) -> socket.socket:
    s = socket.create_connection(proxy, timeout=timeout)
    s.sendall(b"\x05\x01\x00")
    if s.recv(2) != b"\x05\x00":
        raise RuntimeError("socks 握手失败")
    s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(dst[0])
              + struct.pack(">H", dst[1]))
    r = s.recv(10)
    if len(r) < 2 or r[1] != 0:
        raise RuntimeError(f"socks connect 被拒: {r!r}")
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, required=True)
    ap.add_argument("--node-socks", type=int, required=True)
    ap.add_argument("--dst", default="162.159.192.1:443")
    ap.add_argument("--sni", default=NEW_SNI)
    args = ap.parse_args()
    ip, _, port = args.dst.rpartition(":")
    dst = (ip, int(port or 443))
    proxy = ("127.0.0.1", args.node_socks)
    n = [0]

    def handle(c: socket.socket) -> None:
        try:
            # 缓冲直到拿到完整第一条消息（ClientHello）
            c.settimeout(10)
            buf = b""
            while True:
                d = c.recv(16384)
                if not d:
                    c.close(); return
                buf += d
                if len(buf) >= 5 and buf[0] == 0x16:
                    rec_len = struct.unpack(">H", buf[3:5])[0]
                    if len(buf) >= 5 + rec_len:
                        break
                if len(buf) > 32768:
                    break
            rewritten = rewrite_sni(buf, args.sni)
            up = socks_connect(proxy, dst)
            if rewritten:
                up.sendall(rewritten)
                n[0] += 1
                print(f"连接#{n[0]} SNI→{args.sni} ({len(buf)}B→{len(rewritten)}B)",
                      flush=True)
            else:
                up.sendall(buf)
                n[0] += 1
                print(f"连接#{n[0]} SNI 未改写（透传 {len(buf)}B）", flush=True)
        except Exception as e:
            print("失败:", e, flush=True)
            c.close()
            return
        c.settimeout(None)
        t = threading.Thread(target=pump, args=(c, up), daemon=True)
        t.start()
        pump(up, c)
        t.join(timeout=5)
        c.close()
        up.close()

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.listen))
    srv.listen(16)
    print(f"sni_relay 127.0.0.1:{args.listen} → socks {args.node_socks} → {dst} "
          f"(SNI→{args.sni})", flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
