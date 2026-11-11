#!/usr/bin/env python3
"""静态校验 H2 渲染产物（**不启动任何进程、不碰网络**）。

检查项：
  1. 端口不重叠：入站 / relay / 出口 / 生产已知端口
  2. 每个入站恰有一条 override 规则，且 outbound 存在
  3. 每条渠道的节点定义能从 dormant-outbounds.json 找到
  4. 设备目录有 config.json 且没被 active 单元占用
  5. 单元引用的脚本存在且可执行
  6. 设备**争用**检查：一台设备被多个 active 单元持有 = 两线共用一个 CF
     会话身份，会互相顶掉对方的 H2 会话

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
    outbounds = {o["tag"]: o for o in cfg["outbounds"]}
    seen = set()
    for rule in cfg["route"]["rules"]:
        for ib in rule.get("inbound") or []:
            if ib not in inbounds:
                err(f"规则引用不存在的入站 {ib}")
            if ib in seen:
                err(f"入站 {ib} 有多条规则")
            seen.add(ib)
        tag = rule.get("outbound")
        if tag not in outbounds:
            err(f"规则引用不存在的出站 {tag}")
        elif outbounds[tag].get("type") != "selector":
            # 规则必须落在 selector 上：直连某个节点时，换节点只能重启整个
            # asip-h2-singbox，31 条线全断（既定事实 13d 的第 2 条限制）。
            err(f"规则 {rule.get('inbound')} 的出口 {tag} 不是 selector")
        if rule.get("action") != "route":
            err(f"规则 {rule.get('inbound')} 缺 action=route（缺省会被当 reject）")
        if rule.get("override_address") != "162.159.198.2":
            err(f"规则 {rule.get('inbound')} 的 override_address 不是 CF MASQUE")
    missing = inbounds - seen
    if missing:
        err(f"入站没有对应规则: {sorted(missing)}")
    # selector 的成员必须是已声明的真实出站，且不能指回 selector：
    # `PUT /proxies/<selector>` 只能切到 `all` 里已有的名字。
    for o in cfg["outbounds"]:
        if o.get("type") != "selector":
            continue
        members = o.get("outbounds") or []
        if not members:
            err(f"selector {o['tag']} 没有成员")
        if o.get("default") not in members:
            err(f"selector {o['tag']} 的 default 不在成员里")
        for m in members:
            if m not in outbounds:
                err(f"selector {o['tag']} 成员 {m} 不是已声明的出站")
            elif outbounds[m].get("type") == "selector":
                err(f"selector {o['tag']} 成员 {m} 又是 selector")
    # Clash API 是 daemon 换节点的唯一通道，缺了就只能退回重启。
    api = (cfg.get("experimental") or {}).get("clash_api") or {}
    if not api.get("external_controller"):
        err("缺 experimental.clash_api（daemon 无法无重启换节点）")
    if not api.get("secret"):
        err("experimental.clash_api 没有 secret（与主配置共用，不应为空）")


def check_selectors(cfg: dict, ledger: list[dict]) -> None:
    """每条线一个 selector，且当前节点在它的成员里。

    daemon 换节点 = `PUT /proxies/<selector>` + 节点出站 tag，两者都从台账里
    取；台账与配置对不上时 PUT 会被 sing-box 拒（400），而现象只是「换线没
    生效」—— 必须在这里静态拦住。
    """
    sels = {o["tag"]: o for o in cfg["outbounds"] if o.get("type") == "selector"}
    for r in ledger:
        sel = r.get("selector") or f"h2-sel-{r['index']}"
        o = sels.get(sel)
        if not o:
            err(f"{r['unit']}: 配置里没有 {sel}")
            continue
        want = f"h2-{r['node']}"[:120]
        if want not in (o.get("outbounds") or []):
            err(f"{sel}: 当前节点 {r['node']} 不在候选池里")
        if o.get("default") != want:
            err(f"{sel}: default={o.get('default')} 不是当前节点 {want}")
        if r.get("selector") and r["selector"] != sel:
            err(f"{r['unit']}: 台账 selector={r['selector']} 与编号不符")


def check_nodes(ledger: list[dict]) -> None:
    """节点必须能在**生产配置或沉睡池**里找到。

    只查 `dormant-outbounds.json` 是过时的判据：那份是 okz 旧入口的手工快照，
    新订阅的节点（etcloud 的 `*.greatgameupload.org`、yyd2tett 的
    `*.dankenet.help`）从来不在里面。实测 2026-11-04 渲染出 18 个 etcloud
    活节点后，校验器把其中 8 条判成错误 —— 而它们在
    `singbox-config.json` 里好好地躺着。渲染器的 `node_outbound()` 本来就是
    「主配置优先、沉睡池兜底」，校验器必须用同一个口径。
    """
    known: set[str] = set()
    for path in (PROJECT / "singbox-config.json",
                 PROJECT / "dormant-outbounds.json"):
        try:
            data = json.load(open(path))
        except (OSError, ValueError):
            continue
        items = data if isinstance(data, list) else data.get("outbounds") or []
        known |= {n["tag"] for n in items if isinstance(n, dict) and n.get("tag")}
    for r in ledger:
        if r["node"] not in known:
            err(f"{r['unit']}: 节点 {r['node']} 既不在生产配置也不在沉睡池")


def check_devices(ledger: list[dict]) -> None:
    """设备必须存在，且**不能被别的组占用**。

    判据不能是 `free_devices()`：它按定义排除 active 单元占着的设备，而
    重渲染后重跑 h2-install.sh 时，19 条 usque-h2-* 正占着台账里的 19 台 ——
    于是每条线都报「已被 active 单元占用」（2026-11-05 实测 19 条全报错），
    恰恰把要支持的幂等重装挡在门外。台账里的设备属于**本组**，只要求占用者
    是这条线自己的单元。
    """
    import render_h2_lines as rd
    owner = rd.device_owner()
    for r in ledger:
        cfg = PROJECT / ".warp" / r["device"] / "config.json"
        if not cfg.exists():
            err(f"{r['unit']}: 设备配置缺失 {cfg}")
        held = owner.get(r["device"])
        if held and held != r["unit"]:
            err(f"{r['unit']}: 设备 {r['device']} 已被 {held} 占用")

    # **设备争用**：一台设备被多个 active 单元持有 = 两个 usque 进程共用
    # 一个 CF 会话身份 → 它们会互相顶掉对方的 H2 会话。
    #
    # WHY 旧实现看不到：`device_owner()` 是覆盖式赋值，一台设备只留**最后
    # 一个**持有者，于是「多持有」这个事实被彻底吞掉 —— 既不报错、也不告警。
    # 实测 2026-11-11：6 台设备被 `usque-h2-*` 与 `usque-warp-*` 同时持有
    # （usque3/5/6/7/8/9），其中 4 台是 H2 线与 CF 线互抢。症状像「配额不够」，
    # 实际是自相残杀。
    #
    # 这里报**全部**持有者，不猜谁该让谁：让 CF 线迁走还是让 H2 线退让，
    # 是人的决定。校验器的职责是把事实摆出来。
    holders = rd.device_holders()
    for dev, hs in sorted(holders.items()):
        if len(hs) > 1:
            err(f"设备 {dev} 被 {len(hs)} 个 active 单元同时持有: "
                f"{', '.join(sorted(hs))} —— 它们共用同一个 CF 会话身份，"
                f"会互相顶掉对方的 H2 会话")


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
    check_selectors(cfg, ledger)
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
