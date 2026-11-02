#!/usr/bin/env python3
"""UDP 能力矩阵筛选：区分「不转 UDP」和「转 UDP 但只放行某些端口」。

上一轮的筛选用 DNS:53 + STUN 当判据，得出 12/44 可承载。那个判据不完整：
节点完全可能放行 UDP/443（QUIC/MASQUE 的实际端口）而掐掉 53/3478，
这类节点会被误判成「不转 UDP」。本脚本按端口分开测，把这个假阴性暴露出来。

判定（对每个节点）：
    dns53   8.8.8.8:53      A 记录回包      → 基础 UDP 转发能力
    stun    19302 / 3478    STUN 绑定回包   → 通用 UDP 转发（非 53 端口）
    cf443   162.159.198.2:443  QUIC VN     → MASQUE 端口是否可达（决定性判据）

分级：
    A   cf443 通                 → 可承载 MASQUE，进隧道复测
    B   dns53/stun 通、cf443 不通 → 转 UDP 但掐 443（上次被正确排除的那类）
    C   三者全不通                → 确实不转 UDP

用法：
    sudo -u administrator python3 tools/screen_udp_matrix.py --limit 5
    sudo -u administrator python3 tools/screen_udp_matrix.py --workers 6
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DIR))

import warp_wg  # noqa: E402

SINGBOX = str(DIR / "sing-box")
DORMANT = DIR / "dormant-outbounds.json"
DOH_SERVER = "custom.juhazf.cn"
DOH_PATH = "/inst"

QUIC_VER = 0x0A0A0A0A


def quic_vn() -> bytes:
    b = bytearray()
    b.append(0xC0)
    b += struct.pack(">I", QUIC_VER)
    b.append(8)
    b += os.urandom(8)
    b.append(8)
    b += os.urandom(8)
    b += b"\x00" * max(0, 1200 - len(b))
    return bytes(b)


def dns_q(name: str) -> bytes:
    q = b""
    for p in name.encode().split(b"."):
        q += bytes([len(p)]) + p
    q += b"\x00"
    return struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + q + b"\x00\x01\x00\x01"


def stun_req() -> bytes:
    # RFC 5389 binding request，无属性
    return struct.pack(">HHI", 0x0001, 0x0000, 0x2112A442) + os.urandom(12)


def node_cfg(node: dict, port: int) -> dict:
    out = warp_wg.prepare_node(node)
    return {
        "log": {"level": "error"},
        "inbounds": [{"type": "socks", "tag": "in",
                      "listen": "127.0.0.1", "listen_port": port}],
        "outbounds": [out],
        "dns": {
            "servers": [
                {"type": "https", "tag": "dns-airport", "server": DOH_SERVER,
                 "path": DOH_PATH, "domain_resolver": "dns-default"},
                {"type": "local", "tag": "dns-default"},
            ],
            "rules": [{"domain_suffix": ["okzdns.com", "juhazf.cn", "hi2026.net",
                                         "acczzz.com", "bing.com"],
                       "server": "dns-airport", "strategy": "ipv4_only"}],
            "final": "dns-default",
        },
        "route": {"final": out["tag"],
                  "default_domain_resolver": {"server": "dns-default"}},
    }


def wait_port(port: int, deadline: float = 8.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def strip_socks_udp(r: bytes) -> bytes:
    """剥掉 SOCKS5 UDP 应答头：RSV(2) FRAG(1) ATYP(1) ADDR(4|16|变长) PORT(2)。"""
    if len(r) < 4:
        return b""
    atyp = r[3]
    if atyp == 1:
        off = 4 + 4
    elif atyp == 4:
        off = 4 + 16
    elif atyp == 3:
        off = 4 + 1 + (r[4] if len(r) > 4 else 0)
    else:
        return b""
    return r[off + 2:]


def probe(port: int, dst: tuple[str, int], payload: bytes,
          timeout: float = 5.0, tries: int = 2) -> tuple[bool, str]:
    for _ in range(tries):
        r = warp_wg._socks_udp(port, dst, payload, timeout=timeout)
        if r:
            # _socks_udp 回的是整条 SOCKS5 UDP 报文。不剥头直接看 r[0] 会把
            # 恒为 0x00 的 RSV 当首字节，Version Negotiation 永远判不出来
            # （旧工具 probe_udp_quic.py 同样如此，表现为「37B 回包」）。
            data = strip_socks_udp(r)
            if dst[1] == 443 and len(data) >= 5 \
                    and (data[0] & 0x80) and data[1:5] == b"\x00\x00\x00\x00":
                return True, f"QUIC VN {len(data)}B"
            return True, f"{len(r)}B"
        time.sleep(0.2)
    return False, "无回包"


def screen_one(idx: int, node: dict, base_port: int) -> dict:
    tag = node.get("tag") or node.get("server") or f"node{idx}"
    port = base_port + idx
    cfg = node_cfg(node, port)
    tf = Path(tempfile.mkdtemp(prefix=f"scr{idx}_")) / "cfg.json"
    tf.write_text(json.dumps(cfg))
    proc = subprocess.Popen([SINGBOX, "run", "-c", str(tf)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    res = {"tag": tag, "idx": idx, "server": node.get("server"), "port": port}
    try:
        if not wait_port(port):
            res["grade"] = "X"
            res["note"] = "sing-box 未起来"
            return res
        ok53, d53 = probe(port, ("8.8.8.8", 53), dns_q("www.google.com"))
        okst, dst_ = probe(port, ("stun.l.google.com", 19302), stun_req(), timeout=4)
        okcf, dcf = probe(port, ("162.159.198.2", 443), quic_vn(), timeout=5)
        res.update(dns53=f"{ok53}:{d53}", stun=f"{okst}:{dst_}", cf443=f"{okcf}:{dcf}")
        if okcf:
            res["grade"] = "A"
        elif ok53 or okst:
            res["grade"] = "B"
        else:
            res["grade"] = "C"
        return res
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except OSError:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except OSError:
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default=str(DORMANT))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--base-port", type=int, default=30000)
    ap.add_argument("--out", default="/tmp/opencode/udp_matrix.json")
    a = ap.parse_args()

    nodes = json.loads(Path(a.inputs).read_text())
    if a.limit:
        nodes = nodes[: a.limit]
    print(f"节点数 {len(nodes)}，并发 {a.workers}\n")

    t0 = time.time()
    out = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(screen_one, i, n, a.base_port): i
                for i, n in enumerate(nodes)}
        for f in futs:
            pass
        for f in futs:
            try:
                out.append(f.result())
            except Exception as e:  # 单点异常不能中断整批
                out.append({"grade": "E", "note": repr(e)})

    out.sort(key=lambda r: r.get("idx", 999))
    for r in out:
        print(f"[{r['grade']}] {r['tag'][:44]:44s} dns53={r.get('dns53','-'):12s} "
              f"stun={r.get('stun','-'):12s} cf443={r.get('cf443','-'):16s} {r.get('note','')}")

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1))
    g: dict[str, int] = {}
    for r in out:
        g[r["grade"]] = g.get(r["grade"], 0) + 1
    print(f"\n汇总 {g}  耗时 {time.time()-t0:.0f}s  → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
