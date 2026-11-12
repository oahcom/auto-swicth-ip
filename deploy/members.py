#!/usr/bin/env python3
"""把 config.py 的 warp.members 渲染成 shell 可读的行：tag port service dir flag。

dir 由端口推导：1080 → usque，1081 → usque2 … 1099 → usque20（与现有部署一致）。
这样新增隧道只改 config.py 一处，单元与注册目录自动跟上。
flag 是 usque 的地址族开关（-6 = IPv6 MASQUE，否则空）。
"""
import json
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)
from asip.atomic_io import write_text  # noqa: E402
from config import CFG  # noqa: E402


def write_warp_outbound(path: str) -> None:
    """生成 .warp/warp-outbound.json（gitignored，parse_subscription 的唯一数据源）。

    全新 clone 时该文件不存在 → 生成的 sing-box 配置里没有 WARP 出口 →
    `opencode-out` selector 整个缺失，daemon 所有换线都打空。所以这是部署必需步骤。
    """
    host = CFG["warp"]["socks_host"]
    data = [{"type": "socks", "tag": m["tag"], "server": host,
             "server_port": m["port"], "version": "5"} for m in CFG["warp"]["members"]]
    # write_json 不追加末尾换行，而本文件此前带一个；内容必须逐字节等价，
    # 所以序列化到字符串后用 write_text。
    write_text(path, json.dumps(data, indent=2) + "\n", mode=0o600)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--write-warp-outbound":
        out = os.path.join(PROJECT, ".warp", "warp-outbound.json")
        write_warp_outbound(out)
        print(out)
    else:
        # 只渲染 usque 成员：install.sh 用这行输出注册设备、建目录、生成单元。
        # WG 线由 deploy/wg-lines-install.sh、H2 线由 deploy/render_h2_lines.py
        # 各自生成单元；把它们漏进来会凭空注册设备（CF 限流）并在 1180+ 端口
        # 上再起一份 usque，和 H2 线抢同一个出口端口。
        for m in CFG["warp"]["members"]:
            if m.get("kind") in ("wg", "h2"):
                continue
            n = m["port"] - 1079
            # 第 5 列是 usque-launch.sh 的传输参数（install.sh 原样写进 ExecStart）：
            #   h2    → --http2 -s '' -P 443（v4 TCP+TLS，UDP 被 ACL 拦时唯一通路）
            #   -6    → -6（MASQUE over IPv6 / QUIC）
            #   -     → 缺省（MASQUE over IPv4 / QUIC）
            t = m.get("transport")
            if t is None:
                t = "-6" if m.get("ipv6") else "-"
            print(m["tag"], m["port"], m["service"],
                  "usque" if n == 1 else f"usque{n}", t)
