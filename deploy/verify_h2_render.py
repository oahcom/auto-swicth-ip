#!/usr/bin/env python3
"""静态校验 H2 渲染产物（**不启动任何进程、不碰网络**）。

检查项：
  1. 端口不重叠：入站 / relay / 出口 / 生产已知端口
  2. 每个入站恰有一条 override 规则，且 outbound 存在
  3. 每条渠道的节点定义能从 dormant-outbounds.json 找到
  4. 设备目录有 config.json 且没被 active 单元占用
  5. 单元引用的脚本存在且可执行

用法: python3 deploy/verify_h2_render.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
H2 = PROJECT / ".warp" / "h2"
errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def check_ports(cfg: dict, ledger: list[dict]) -> None:
    in_ports = [i["listen_port"] for i in cfg["inbounds"]]
    if len(set(in_ports)) != len(in_ports):
        err("入站端口有重复")
    if len({r["socks_port"] for r in ledger}) != len(ledger):
        err("出口 socks 端口有重复")
    # 与本机已监听端口的冲突。**本组自己的进程要排除**：重渲染后重跑
    # h2-install.sh 时，asip-h2-singbox 还占着 22700+、19 条 usque 还占着 1180+，
    # 不排除的话每次幂等重装都报「端口已被占用」而中止 —— 恰恰是它要支持的场景。
    busy = set()
    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True,
                             timeout=5).stdout
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4 or ":" not in parts[3]:
                continue
            m = re.search(r"pid=(\d+)", parts[-1])
            if m and _is_own_pid(int(m.group(1))):
                continue
            try:
                busy.add(int(parts[3].rsplit(":", 1)[1]))
            except ValueError:
                pass
    except (OSError, subprocess.TimeoutExpired):
        pass
    clash = (set(in_ports) | {r["socks_port"] for r in ledger}) & busy
    if clash:
        err(f"端口已被占用: {sorted(clash)}")


def _is_own_pid(pid: int) -> bool:
    """这个进程是不是本组 H2 自己起的。

    判据是命令行里带 `.warp/h2/`：sing-box 用 `-c .../.warp/h2/singbox.json`，
    usque 用 `-c .../.warp/h2/conf/N.json`。比其他任何标记都直接 —— 端口号本身
    不能当判据（那正是要检查的东西）。
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b".warp/h2/" in f.read()
    except OSError:
        return False


def check_rules(cfg: dict) -> None:
    inbounds = {i["tag"] for i in cfg["inbounds"]}
    outbounds = {o["tag"] for o in cfg["outbounds"]}
    seen = set()
    for rule in cfg["route"]["rules"]:
        for ib in rule.get("inbound") or []:
            if ib not in inbounds:
                err(f"规则引用不存在的入站 {ib}")
            if ib in seen:
                err(f"入站 {ib} 有多条规则")
            seen.add(ib)
        if rule.get("outbound") not in outbounds:
            err(f"规则引用不存在的出站 {rule.get('outbound')}")
        if rule.get("action") != "route":
            err(f"规则 {rule.get('inbound')} 缺 action=route（缺省会被当 reject）")
        if rule.get("override_address") != "162.159.198.2":
            err(f"规则 {rule.get('inbound')} 的 override_address 不是 CF MASQUE")
    missing = inbounds - seen
    if missing:
        err(f"入站没有对应规则: {sorted(missing)}")


def check_nodes(ledger: list[dict]) -> None:
    dorm = {n["tag"] for n in json.load(open(PROJECT / "dormant-outbounds.json"))}
    for r in ledger:
        if r["node"] not in dorm:
            err(f"{r['unit']}: 节点 {r['node']} 不在 dormant-outbounds.json")


def check_devices(ledger: list[dict]) -> None:
    import render_h2_lines as rd
    free = set(rd.free_devices())
    for r in ledger:
        cfg = PROJECT / ".warp" / r["device"] / "config.json"
        if not cfg.exists():
            err(f"{r['unit']}: 设备配置缺失 {cfg}")
        if r["device"] not in free:
            err(f"{r['unit']}: 设备 {r['device']} 已被 active 单元占用")


def check_units(ledger: list[dict]) -> None:
    # sing-box 单元必须存在，否则重启后入站全空、usque 全线重连失败。
    if not (H2 / "units" / "asip-h2-singbox.service").exists():
        err("缺 asip-h2-singbox.service（H2 入站没有单元托管）")
    for r in ledger:
        unit = H2 / "units" / r["unit"]
        script = H2 / "usque" / f"{r['index']}.sh"
        if not unit.exists():
            err(f"缺单元文件 {unit}")
        if not script.exists():
            err(f"缺启动脚本 {script}")
        elif not (script.stat().st_mode & 0o111):
            err(f"启动脚本不可执行 {script}")
        else:
            body = script.read_text()
            if "--http2" not in body or "-s ''" not in body:
                err(f"{script} 缺少 --http2 或空 SNI")
            conf = H2 / "conf" / f"{r['index']}.json"
            exp = str(conf)
            if exp not in body:
                err(f"{script} 没有引用派生配置 {conf}")
                continue
            if not conf.exists():
                err(f"缺派生配置 {conf}")
                continue
            c = json.load(open(conf))
            if c.get("endpoint_h2_v4") != "127.0.0.1":
                err(f"{conf}: endpoint_h2_v4={c.get('endpoint_h2_v4')!r} "
                    f"不是 relay 的 127.0.0.1")
            if c.get("endpoint_h2_v6"):
                err(f"{conf}: endpoint_h2_v6 非空，--http2 下会走 v6 绕过 relay")


def main() -> int:
    if not (H2 / "singbox.json").exists():
        print("没有渲染产物，先跑 deploy/render_h2_lines.py", file=sys.stderr)
        return 2
    cfg = json.load(open(H2 / "singbox.json"))
    ledger = json.load(open(H2 / "channels.json"))

    check_ports(cfg, ledger)
    check_rules(cfg)
    check_nodes(ledger)
    check_devices(ledger)
    check_units(ledger)

    if errors:
        print(f"✗ {len(errors)} 个问题：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"✓ {len(ledger)} 条线路静态校验通过 "
          f"(入站 {len(cfg['inbounds'])} / 出站 {len(cfg['outbounds'])} / "
          f"规则 {len(cfg['route']['rules'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
