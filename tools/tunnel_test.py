#!/usr/bin/env python3
"""单节点 + 指定设备的隧道测试，用于把「节点不行」和「设备不行」分开。

批量扫描器按 `DEVICES[i % 11]` 分配设备，失败会同时混进两种原因：
节点承载不了 MASQUE，或那台 usque 设备本身鉴权/注册坏了。
本工具把两个变量都显式指定，交叉验证才能定性。

用法：
    sudo -u administrator python3 tools/tunnel_test.py --tag hk-aws-122.acczzz.com:443-any --device 12
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DIR))

USQUE = str(DIR / ".warp/usque3/usque")
BRIDGE = str(DIR / "tools/masque_bridge.py")
SINGBOX = str(DIR / "sing-box")
DST = "162.159.198.2:443"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--device", type=int, required=True)
    ap.add_argument("--enroll", action="store_true")
    ap.add_argument("--wait", type=int, default=16)
    a = ap.parse_args()

    nodes = {x["tag"]: x for x in json.loads((DIR / "dormant-outbounds.json").read_text())}
    node = nodes[a.tag]
    from tools.screen_udp_matrix import node_cfg
    node_port, bridge_port, out_port = 20200, 44600, 11600
    cfg_path = f"/tmp/tt_node.json"
    json.dump(node_cfg(node, node_port), open(cfg_path, "w"))
    sb = subprocess.Popen([SINGBOX, "run", "-c", cfg_path],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True)
    time.sleep(3)

    bridge = subprocess.Popen(
        [sys.executable, BRIDGE, "--node-socks", str(node_port),
         "--listen", str(bridge_port), "--dst", DST],
        stdout=open("/tmp/tt_bridge.log", "w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    time.sleep(2)

    dev_cfg = DIR / f".warp/usque{a.device}" / "config.json"
    cfg = json.load(open(dev_cfg))
    cfg["endpoint_v4"] = "127.0.0.2"
    json.dump(cfg, open(dev_cfg, "w"))
    if a.enroll:
        subprocess.run(f"echo y | {USQUE} enroll -c {dev_cfg}", shell=True,
                       capture_output=True, text=True, timeout=90)
    u = subprocess.Popen(
        [USQUE, "socks", "-c", str(dev_cfg), "-P", str(bridge_port),
         "-b", "127.0.0.1", "-p", str(out_port)],
        stdout=open("/tmp/tt_usque.log", "w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    time.sleep(a.wait)

    r = subprocess.run(
        ["curl", "-sS", "--max-time", "20", "--noproxy", "",
         "--proxy", f"socks5h://127.0.0.1:{out_port}",
         "https://www.cloudflare.com/cdn-cgi/trace"],
        capture_output=True, text=True)
    t = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    print(f"tag={a.tag}\ndevice=usque{a.device}")
    if r.returncode != 0:
        print(f"curl rc={r.returncode} err={r.stderr.strip()[:200]}")
    print(f"warp={t.get('warp','?')} colo={t.get('colo','?')} loc={t.get('loc','?')} "
          f"ip={t.get('ip','?')}")
    if t.get("warp") != "on":
        print("--- usque tail ---")
        print(subprocess.run(["tail", "-6", "/tmp/tt_usque.log"],
                             capture_output=True, text=True).stdout)

    for p in (u, bridge, sb):
        try:
            os.killpg(os.getpgid(p.pid), 15)
        except OSError:
            p.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
