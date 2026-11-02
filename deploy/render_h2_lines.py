#!/usr/bin/env python3
"""把块模型里的 H2 渠道渲染成可部署的产物（**只写文件，不启动任何进程**）。

产物目录 `.warp/h2/`（已 gitignore）：
  singbox.json      1 个 sing-box：N 个 direct 入站 + N 个节点出站 + override 规则
  conf/N.json       每条线一份派生设备配置（endpoint_h2_v4 → 127.0.0.1）
  usque/N.sh        每条线的 usque 启动命令
  units/*.service   每条线一个 systemd 单元（未 enable）
  channels.json     渠道 ↔ 端口 ↔ 设备 ↔ 节点的台账

WHY 只渲染 H2 渠道：块模型 30 条渠道里 17 条节点的 carrier=h2（只能走 H2 承载），
其余 13 条是原有的 WG 节点，由 `deploy/wg-lines-install.sh` 负责。两条渲染器
各管各的一半，同一渠道不会被两套单元同时占用。

WHY 不要 relay：sing-box 的 `direct` 入站收的就是**原始 TCP**，override 规则把
目标改写成 CF MASQUE 端点后直接经节点出站 —— 这正是 H2 需要的「裸 TCP 经节点」。
早期版本在中间加了一层 tcp_relay.py，它按 SOCKS 协议去连 direct 入站，握手必然
失败（`上游失败: socks 握手失败`）。usque 直连 direct 入站即可。

WHY 一条线一个 usque 进程：usque 的 SOCKS 出口与设备身份一一对应，同一设备
不能开两个会话（CF 侧互踢）。所以「渠道数 = usque 进程数 = 设备数」是硬约束，
不能像 sing-box 那样合并。

用法：
    python3 deploy/render_h2_lines.py            # 渲染
    python3 deploy/render_h2_lines.py --print    # 只看会渲染什么
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

# 必须先于 `from config import CFG`：config 在 import 时就把环境变量读进 CFG。
# 生产的 auto-switch-ip.service 用 EnvironmentFile=.env，手跑 python 不经过它，
# 不补这一步就会用 config.py 的默认值渲染（blocks 关 → 0 条渠道；渠道数 2 而非 3），
# 产物与生产语义对不上。`.env` 可能不存在（全新 clone）：缺文件就按默认值走，
# 不能在这里抛 —— 远端第一次部署时 .env 往往还没生成。
_env_file = PROJECT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import warp_blocks  # noqa: E402
from config import CFG  # noqa: E402

# 端口规划（与生产错开）
BASE_INBOUND = 22700      # sing-box direct 入站（usque 直连这里，不经 relay）
BASE_DEVICE_PORT = 1180   # usque SOCKS 出口，落入 warp.members 的 1080 段之外
BASE_UNIT = "usque-h2"

# H2 承载的固定目标：只能 v4，因为 direct 入站只监听 127.0.0.1，而 -6 时 usque
# 会连 endpoint_h2_v6（::1）—— 那条路要么不通、要么绕过节点直连 CF。v6 直连 CF
# 已验证可用，但那是**不经节点**的路径，与「经机场节点承载」这个目标无关。
H2_DST = "162.159.198.2:443"


def h2_channels() -> list[dict]:
    """块模型展开后，节点的 carrier == 'h2' 的那些渠道。

    顺序按「块内第几条」轮转，而不是按块表顺序平铺：设备数少于渠道数时，
    平铺会让排在表尾的块整块落空（实测 15 台设备时 SLC 三条全丢，而 SLC 正是
    当天新发现的块）。轮转保证每个块先各拿一条，再谈第二条。
    """
    nodes_map = warp_blocks.load_map()
    table = CFG["warp"]["blocks"]["table"]
    blk_pos = {b["colo"]: i for i, b in enumerate(table)}
    out = []
    for ch in warp_blocks.channels(CFG, nodes_map):
        meta = nodes_map.get(ch["node"]) or {}
        if meta.get("carrier") == "h2":
            ch["carrier"] = "h2"
            ch["colo_measured"] = meta.get("colo")
            out.append(ch)
    # v4 块（colo=ALL）没有自己的节点，节点与 SIN 那三条重复，排最后。
    out.sort(key=lambda c: (c["index"], blk_pos.get(c["colo"], len(table))))
    return out


def free_devices() -> list[str]:
    """未被**正在运行**的 systemd 单元占用的设备目录名。

    只排除 active 单元：20 个 usque-warp*.service 单元把全部 20 台设备都写进了
    WorkingDirectory，但其中 15 个是 `disable` 的历史单元（2026-10-28 缩容时
    留作回滚，文档 §"服务托管"）。按「单元文件存在就排除」会得出空闲 0 台 ——
    那 15 台设备实际没有进程在用，白白浪费。

    真正的判据是有没有进程持有该目录下的 usque：直接用 `systemctl is-active`。
    """
    import subprocess
    used = set()
    for svc in Path("/etc/systemd/system").glob("usque-warp*.service"):
        try:
            r = subprocess.run(["systemctl", "is-active", svc.name],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.stdout.strip() != "active":
            continue
        for line in svc.read_text(errors="ignore").splitlines():
            if line.startswith("WorkingDirectory="):
                used.add(Path(line.split("=", 1)[1].strip()).name)
    all_dev = sorted(
        p.name for p in (PROJECT / ".warp").glob("usque*")
        if p.is_dir() and (p / "config.json").exists())
    return [d for d in all_dev if d not in used]


def node_outbound(tag: str) -> dict:
    import warp_wg
    dorm = {n["tag"]: n for n in json.load(open(PROJECT / "dormant-outbounds.json"))}
    return warp_wg.prepare_node(dorm[tag])


def render(print_only: bool = False) -> int:
    chs = h2_channels()
    devs = free_devices()
    # 主配置可能还没生成（全新 clone / 远端首次部署）。H2 出站经机场节点，
    # 节点域名只有机场 DoH 解析得了，所以缺主配置时用一个最小 DNS 段兜底，
    # 而不是在这里 FileNotFoundError 把整次渲染打断。
    try:
        prod_dns = json.load(open(PROJECT / "singbox-config.json"))["dns"]
    except (OSError, ValueError, KeyError):
        print("⚠ 读不到 singbox-config.json 的 dns，用最小 DNS 段兜底"
              "（先跑 parse_subscription.py 可消除此告警）", file=sys.stderr)
        prod_dns = {"servers": [
            {"type": "https", "tag": "dns-airport", "server": "custom.juhazf.cn",
             "path": "/inst", "domain_resolver": "dns-default"},
            {"type": "local", "tag": "dns-default"},
        ]}

    if not chs:
        print("没有 carrier=h2 的渠道（先跑 tools/merge_h2_blockmap.py）",
              file=sys.stderr)
        return 1

    # 渠道数先按「不同节点」收敛，再与设备数取小。
    n = min(len({c["node"] for c in chs}), len(devs))
    if n == 0:
        print(f"⚠ 没有空闲设备（渠道 {len(chs)} 条）；"
              f"停掉不用的 usque-warp*.service 或新注册设备后重试",
              file=sys.stderr)
        return 1
    if n < len(chs):
        print(f"⚠ 渠道 {len(chs)} 条（去重 {len({c['node'] for c in chs})} 个节点），"
              f"空闲设备 {len(devs)} 台 —— 只渲染前 {n} 条；其余等设备释放",
              file=sys.stderr)

    # 同一节点不能开两台设备：CF 侧同一 device 只能有一个会话，同一**节点**承载
    # 多条渠道则共用一条 TCP 链路（一条挂全线挂），且同块两条渠道本就共用段池。
    seen_node, picked = set(), []
    for ch in chs:
        if ch["node"] in seen_node:
            continue
        seen_node.add(ch["node"])
        picked.append(ch)
        if len(picked) == n:
            break
    if len(picked) < n:
        print(f"⚠ 去重后只剩 {len(picked)} 个不同节点（要 {n} 台设备）", file=sys.stderr)

    inbounds, outbounds, rules, ledger = [], [], [], []
    for i, ch in enumerate(picked):
        dev = devs[i]
        inbound_port = BASE_INBOUND + i
        socks_port = BASE_DEVICE_PORT + i
        tag = ch["node"]
        ob = node_outbound(tag)
        ob["tag"] = f"h2-{i}-{tag}"[:120]
        outbounds.append(ob)
        inbounds.append({"type": "direct", "tag": f"h2in-{i}",
                         "listen": "127.0.0.1", "listen_port": inbound_port})
        # `action` 必须显式写 "route"。sing-box 1.13 里缺省 action 的规则被当作
        # `reject` —— 连接建立后立刻关，日志里只有一行 inbound connection，
        # 看起来像节点/CF 侧断流，实际是本机把连接拒了（实测踩到）。
        rules.append({"inbound": [f"h2in-{i}"], "action": "route",
                      "override_address": H2_DST.split(":")[0],
                      "override_port": int(H2_DST.split(":")[1]),
                      "outbound": ob["tag"]})
        ledger.append({
            "index": i, "channel": ch["tag"], "colo": ch.get("colo_measured"),
            "node": tag, "device": dev,
            "inbound_port": inbound_port,
            "socks_port": socks_port,
            "unit": f"{BASE_UNIT}-{i}.service",
            "member_tag": f"WARP-H2-{ch['colo']}-{i}",
        })

    cfg = {
        "log": {"level": "warn"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "dns": prod_dns,
        "route": {"rules": rules, "final": outbounds[0]["tag"],
                  "default_domain_resolver": {"server": "dns-default"}},
    }

    if print_only:
        for row in ledger:
            print(f"{row['unit']:22s} {row['member_tag']:22s} "
                  f"colo={row['colo'] or '?':4s} node={row['node'][:40]:40s} "
                  f"in={row['inbound_port']} "
                  f"socks={row['socks_port']} dev={row['device']}")
        print(f"\n共 {len(ledger)} 条（sing-box 1 进程 + usque {len(ledger)} 进程）",
              file=sys.stderr)
        return 0

    out = PROJECT / ".warp" / "h2"
    (out / "units").mkdir(parents=True, exist_ok=True)
    (out / "usque").mkdir(exist_ok=True)
    (out / "conf").mkdir(exist_ok=True)

    # sing-box 本身也要有单元：它承载全部 N 个 direct 入站，手工 `nohup` 起的进程
    # 重启即消失，19 条 usque 会对着一个关掉的端口无限重连。daemon 的
    # `_singbox_pkill_argv` 精确匹配 `-c <主配置>`，不会误杀这个。
    owner = PROJECT.stat().st_uid
    user = pwd.getpwuid(owner).pw_name
    h2_unit = "asip-h2-singbox.service"
    (out / "units" / h2_unit).write_text(f"""[Unit]
Description=WARP H2 lines — sing-box direct inbounds (deploy/render_h2_lines.py)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={PROJECT}
ExecStart={PROJECT}/sing-box run -c {out}/singbox.json
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
""")
    H2_SINGBOX_UNIT = h2_unit

    (out / "singbox.json").write_text(json.dumps(cfg, indent=1, ensure_ascii=False))
    (out / "channels.json").write_text(
        json.dumps(ledger, indent=2, ensure_ascii=False))

    for r in ledger:
        # usque 的 -P 只换端口、host 仍取 config 的 endpoint_h2_v4。设备原始配置
        # 指向真实的 CF IP（直连，绕过节点），必须改成环回让它走 direct 入站。
        dev_cfg = json.load(open(PROJECT / ".warp" / r["device"] / "config.json"))
        dev_cfg["endpoint_h2_v4"] = "127.0.0.1"
        dev_cfg["endpoint_h2_v6"] = ""
        conf = out / "conf" / f"{r['index']}.json"
        conf.write_text(json.dumps(dev_cfg, indent=1))
        conf.chmod(0o600)

        s = out / "usque" / f"{r['index']}.sh"
        s.write_text(
            "#!/bin/bash\n"
            f"cd {PROJECT}/.warp/{r['device']} || exit 1\n"
            f"exec ./usque -c {conf} socks --http2 -s '' -P {r['inbound_port']} "
            f"-b 127.0.0.1 -p {r['socks_port']}\n")
        s.chmod(0o755)

        # User= 不能写死某个用户名：远端主机的仓库属主不是 administrator。
        # 用 stat 取仓库属主，与 install.sh/wg-lines-install.sh 的 RUN_USER 同源。
        unit = out / "units" / r["unit"]
        unit.write_text(f"""[Unit]
Description=WARP H2 line {r['index']} ({r['colo']}) via {r['node'][:40]}
After=network-online.target {H2_SINGBOX_UNIT}
Wants=network-online.target {H2_SINGBOX_UNIT}
# daemon 重启线路时每轮都可能发，默认 start-limit 会把单元锁成 start-limit-hit。
StartLimitIntervalSec=0

[Service]
Type=simple
User={user}
WorkingDirectory={PROJECT}/.warp/{r['device']}
ExecStart={out}/usque/{r['index']}.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
""")

    print(f"渲染完成 → {out}", file=sys.stderr)
    print(f"  sing-box 入站 {len(ledger)} 个 (direct)，单元 {H2_SINGBOX_UNIT}")
    print(f"  usque 单元 {len(ledger)} 个 ({BASE_UNIT}-0…{len(ledger)-1})")
    print(f"  出口 socks 端口 {BASE_DEVICE_PORT}…{BASE_DEVICE_PORT+len(ledger)-1}")
    print(f"  **未启用任何单元**；启用：deploy/h2-install.sh")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="print_only")
    args = ap.parse_args()
    return render(args.print_only)


if __name__ == "__main__":
    sys.exit(main())
