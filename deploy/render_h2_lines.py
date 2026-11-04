#!/usr/bin/env python3
"""把块模型里的 H2 渠道渲染成可部署的产物（**只写文件，不启动任何进程**）。

产物目录 `.warp/h2/`（已 gitignore）：
  singbox.json      1 个 sing-box：N 个 direct 入站 + 每条线一个 selector
                    （候选池全预写）+ override 规则 + 专用 Clash API
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

块模型里多出来的渠道（新 colo、同 colo 多一条）**自动续号补成员**，设备不够
时自动调 `deploy/h2-add-devices.sh` 注册（CF 限流约 9 台/窗口，注册到几台用几台，
下轮再补）。既有成员的编号/顺序一位不动。

用法：
    python3 deploy/render_h2_lines.py            # 渲染（会补成员、补设备）
    python3 deploy/render_h2_lines.py --print    # 只看会渲染什么（不写文件、不注册）
    python3 deploy/render_h2_lines.py --check    # 只列待补成员，有差集退出 1
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
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
from config import CFG, get_singbox_secret  # noqa: E402

# 端口规划（与生产错开）
BASE_INBOUND = 22700      # sing-box direct 入站（usque 直连这里，不经 relay）
BASE_DEVICE_PORT = 1180   # usque SOCKS 出口，落入 warp.members 的 1080 段之外
BASE_UNIT = "usque-h2"

# H2 承载的固定目标：只能 v4，因为 direct 入站只监听 127.0.0.1，而 -6 时 usque
# 会连 endpoint_h2_v6（::1）—— 那条路要么不通、要么绕过节点直连 CF。v6 直连 CF
# 已验证可用，但那是**不经节点**的路径，与「经机场节点承载」这个目标无关。
H2_DST = "162.159.198.2:443"

# 只给 daemon 换线用的 Clash API（主配置占 9090）。sing-box **没有**运行时
# 重载配置的接口，节点不预写进配置就切不过去 —— 所以每条线的候选池全量声明，
# 换节点 = `PUT /proxies/h2-sel-<i>`，进程不重启、其余 30 条线零影响。
H2_CLASH_PORT = 9091


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
    # 台账兜底：`.warp/h2/channels.json` 是**已投产**的渠道 ↔ 设备 ↔ 端口分配。
    # 块模型从 block-map 里筛 h2 会随实测表漂移（`merge_h2_blockmap` 只往原有
    # 条目里塞 `h2` 字段，不覆盖 `carrier`），于是同一份台账在某个时点重渲染会
    # 得到 0 条。台账存在时以台账为准 —— 它记录的是线上正在跑的那 19 条。
    if not out:
        ledger_path = PROJECT / ".warp" / "h2" / "channels.json"
        if ledger_path.exists():
            try:
                saved = json.loads(ledger_path.read_text())
            except (OSError, ValueError):
                saved = []
            for row in saved:
                out.append({
                    "tag": row.get("channel"),
                    "node": row.get("node"),
                    "colo": row.get("colo"),
                    "colo_measured": row.get("colo"),
                    "carrier": "h2",
                    "index": row.get("index", 0),
                    "family": "v4",
                    "_saved_device": row.get("device"),
                })
            print(f"块模型未筛出 h2 渠道，改用台账 {len(out)} 条"
                  f"（.warp/h2/channels.json）", file=sys.stderr)
    # **台账/块模型都只是起点，不是天花板**：设备够的时候（远端实测 16 台空闲
    # + 22 台在册）新 colo 的 h2 节点应当能开新线。否则台账既是渲染器的输入
    # 又是它的输出，渲染器永远扩张不了 —— 自锁。这里把 block-map 里还没被
    # 覆盖的 h2 节点补成新渠道，编号接在既有最大 index 之后（与 `_member_order`
    # 同一约定）。
    #
    # **不能放在 `if not out:` 里面**（原样）：那样只有「块模型一条也没筛出」
    # 时才扩张，而块模型通常总能筛出几条 —— 于是扩张逻辑在生产上从没执行过。
    # 实测 2026-11-05 两台机器同一份代码：本地 31 条、远端 22 条，差的 9 条正是
    # 从来没被补出来的新 colo。远端 block-map 里有 43 个 h2 节点、8 个 colo，
    # 实际投产的只有块模型那一份。
    have = {c["node"] for c in out}
    cap = int((CFG["warp"].get("blocks") or {}).get("channels_per_block") or 0)
    per_colo: dict[str, int] = {}
    for c in out:
        # 台账里的 `colo` 字段是 null（只有 `member_tag` 里有块名），从 tag 兜底，
        # 否则 per_colo 恒空、上限永远不生效（同一 colo 会被补出任意多条）。
        colo = c.get("colo") or c.get("colo_measured")
        if not colo:
            parts = str(c.get("tag") or "").split("-")
            colo = parts[2] if len(parts) > 3 else None
        if colo:
            c["colo"] = c["colo_measured"] = colo
            per_colo[colo] = per_colo.get(colo, 0) + 1
    # 按 colo 分组、组内按名字排序：同一批新增在两次渲染间保持稳定。
    cand: dict[str, list[str]] = {}
    for node, meta in nodes_map.items():
        if not isinstance(meta, dict) or meta.get("carrier") != "h2":
            continue
        if node not in have:
            cand.setdefault(str(meta.get("colo") or "?"), []).append(node)
    nxt = max([c["index"] for c in out] or [-1]) + 1
    added = 0
    for colo in sorted(cand):
        for node in sorted(cand[colo]):
            if cap and per_colo.get(colo, 0) >= cap:
                break
            out.append({"tag": f"WARP-H2-{colo}-{nxt}",
                        "node": node, "colo": colo,
                        "colo_measured": colo, "carrier": "h2",
                        "index": nxt, "family": "v4",
                        "_saved_device": None})
            per_colo[colo] = per_colo.get(colo, 0) + 1
            nxt += 1
            added += 1
    if added:
        print(f"台账/块模型之外按 block-map 补 {added} 条 h2 渠道",
              file=sys.stderr)
    # v4 块（colo=ALL）没有自己的节点，节点与 SIN 那三条重复，排最后。
    out.sort(key=lambda c: (c["index"], blk_pos.get(c["colo"], len(table))))
    return out


def _alive(node: dict, timeout: float = 6) -> bool:
    """节点入口 TCP 可达。

    H2 的承载链路是「usque → direct 入站 → **节点的裸 TCP** → CF MASQUE」，
    所以判据是 TCP，不是 WG 那边的 UDP 2408 握手。
    """
    import socket
    try:
        with socket.create_connection((node["server"], int(node["server_port"])),
                                      timeout=timeout):
            return True
    except (OSError, KeyError, ValueError, TypeError):
        return False


def _alive_many(nodes: dict[str, dict], timeout: float = 6) -> dict[str, bool]:
    """并发探活。**只**用于「该先用谁」的排序，不用于承载结论。

    串行 60 个节点 × 6s 超时 = 6 分钟；并发只是为了把渲染的等待时间压到
    10 秒级。真正的「能不能承载 WARP」仍由 `tools/probe_h2_nodes.py` 串行
    实测（那必须串行：同一台机器争用自己的出口）。
    """
    import concurrent.futures as cf
    out: dict[str, bool] = {}
    if not nodes:
        return out
    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(_alive, n, timeout): t for t, n in nodes.items()}
        for f in cf.as_completed(futs):
            try:
                out[futs[f]] = bool(f.result())
            except Exception:
                out[futs[f]] = False
    return out


def _reachable_order(pool: list[str], live: dict[str, dict], probe: bool,
                     reach: dict[str, bool] | None = None) -> list[str]:
    """池内排序：**能连通**的排前面，其余保持原序垫后。

    WH 不能直接用块模型的顺序：`warp_blocks` 按 `category` 排（regular 在前），
    而 okz 那批旧节点的 `regular` 是它们还活着时的结论。实测 2026-11-04，
    19 条 H2 线的池子第一个全是 `bilibili-image-cdn.juhazf.cn`（17 条超时 +
    2 条 DNS 不解析），18 个能承载的 etcloud 节点因为 category=irregular
    排在后面，于是每条线的默认节点都是死的。**可达性是当下的，category
    是历史的** —— 只是排序，不改渠道编号。
    """
    if not probe:
        return pool
    reach = _alive_many({t: live[t] for t in pool if t in live}) \
        if reach is None else reach
    ok = [t for t in pool if reach.get(t)]
    bad = [t for t in pool if not reach.get(t)]
    return ok + bad


def device_owner(unit_dir: str | Path = "/etc/systemd/system") -> dict[str, str]:
    """设备目录名 → 占用它的 active 单元名（没被占用就不在表里）。"""
    owner: dict[str, str] = {}
    units = Path(unit_dir)
    for svc in list(units.glob("usque-warp*.service")) + \
            list(units.glob("usque-h2-*.service")):
        try:
            r = subprocess.run(["systemctl", "is-active", svc.name],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.stdout.strip() != "active":
            continue
        for line in svc.read_text(errors="ignore").splitlines():
            if line.startswith("WorkingDirectory="):
                owner[Path(line.split("=", 1)[1].strip()).name] = svc.name
    return owner


def free_devices(unit_dir: str | Path = "/etc/systemd/system") -> list[str]:
    """未被**正在运行**的 systemd 单元占用的设备目录名。

    只排除 active 单元：20 个 usque-warp*.service 单元把全部 20 台设备都写进了
    WorkingDirectory，但其中 15 个是 `disable` 的历史单元（2026-10-28 缩容时
    留作回滚，文档 §"服务托管"）。按「单元文件存在就排除」会得出空闲 0 台 ——
    那 15 台设备实际没有进程在用，白白浪费。

    真正的判据是有没有进程持有该目录下的 usque：直接用 `systemctl is-active`。

    **探测设备永远不算空闲**（`usque-probe*`）：它们是 `tools/probe_h2_nodes.py`
    的固定设备池，测一个节点就要独占一台。把它们分给生产 H2 线之后，两边会
    抢同一个 CF 设备身份 —— 探测把线踢下线，线把探测踢下线，表现为双方都
    「建不起来」，而日志里只会看到两侧各自的重连失败。
    """
    used = set()
    # **两种单元都要扫**。只扫 usque-warp* 是漏的：19 条 H2 线由
    # `usque-h2-*.service` 跑，它们同样占着设备身份。漏掉它们，free_devices
    # 会把正在跑 H2 的 19 台全报成「空闲」，随后把新渲染的线绑到同一批设备上
    # —— 两条 usque 抢同一个 CF 设备，双方都掉线，日志里只有彼此的重连。
    # （2026-11-04 实测：free_devices 返回的 19 台正好全是生产在用的那 19 台。）
    units = Path(unit_dir)
    used = set(device_owner(units))
    all_dev = sorted(
        p.name for p in (PROJECT / ".warp").glob("usque*")
        if p.is_dir() and (p / "config.json").exists())
    return [d for d in all_dev
            if d not in used and not d.startswith("usque-probe")]


def _member_order(chs: list[dict]) -> tuple[list[tuple[str, str]], list[str]]:
    """既有成员顺序 + 块模型里还没成线的新渠道。

    返回 `([(member_tag, colo)], [新增的 member_tag])`。

    既有成员一律原样保留 —— 顺序、编号都不动（docs/established-facts.md 13c：
    `WARP-H2-<COLO>-<N>` ↔ SOCKS 1180+N 是写进 selector 的契约）。块模型里某个
    colo 的渠道数多于既有成员数时，多出来的按「既有最大编号 + 1」续号补成员。
    新测出的 colo（BOM/FRA/GIG/LHR…）就是靠这一步进来的：只读 warp-outbound
    的话，没有既有成员的 colo 永远渲染不出来（2026-11-05 实测 29 条渠道只有
    19 个成员）。
    """
    order: list[tuple[str, str]] = []
    try:
        wo = json.load(open(PROJECT / ".warp" / "warp-outbound.json"))
        for o in wo:
            t = str(o.get("tag") or "")
            if t.startswith("WARP-H2-"):
                order.append((t, t.split("-")[2]))
    except (OSError, ValueError, KeyError):
        pass
    have: dict[str, int] = {}
    for _, colo in order:
        have[colo] = have.get(colo, 0) + 1
    want: dict[str, int] = {}
    for ch in chs:
        want[ch["colo"]] = want.get(ch["colo"], 0) + 1
    nums = [int(t.rsplit("-", 1)[1]) for t, _ in order
            if t.rsplit("-", 1)[1].isdigit()]
    nxt = max(nums, default=-1) + 1
    added: list[str] = []
    for colo, cnt in want.items():
        for _ in range(cnt - have.get(colo, 0)):
            tag = f"WARP-H2-{colo}-{nxt}"
            nxt += 1
            order.append((tag, colo))
            added.append(tag)
    return order, added


def node_outbound(tag: str) -> dict:
    """节点出站：**当前主配置优先**，沉睡池只作兜底。

    原先只读 `dormant-outbounds.json`（沉睡池快照），新插进来的订阅节点不在
    那份文件里，于是 KeyError —— 而它们恰恰是最该被渲染的一批。主配置是
    refresh 的产物，任何在池的节点都在里面；沉睡池的节点如果不在池子里
    （被解析器滤掉）仍然能从这里取到，所以两处都留着。
    """
    import warp_wg
    try:
        cfg = json.load(open(PROJECT / "singbox-config.json"))
        live = {o["tag"]: o for o in cfg.get("outbounds", [])
                if o.get("server") and o.get("server_port")}
        if tag in live:
            return warp_wg.prepare_node(live[tag])
    except (OSError, ValueError, KeyError):
        pass
    dorm = {n["tag"]: n for n in json.load(open(PROJECT / "dormant-outbounds.json"))}
    return warp_wg.prepare_node(dorm[tag])


def render(print_only: bool = False) -> int:
    chs = h2_channels()
    unit_dir = os.environ.get("ASIP_SYSTEM_UNIT_DIR") or "/etc/systemd/system"
    free_devs = free_devices(unit_dir)
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

    # **台账里的设备本来就属于这批线**：原地重渲染换的是节点，不是设备。
    # 只按 `free_devices()` 算容量会得出「空闲 1 台」，n=1，然后被下面的护栏
    # 拒掉 —— 而线上 19 条线其实一台设备都不用动（实测 2026-11-04：
    # free_devices() 只返回 usque-h2new1，19 台设备全在台账里）。
    ledger_path = PROJECT / ".warp" / "h2" / "channels.json"
    try:
        ledger_rows = json.loads(ledger_path.read_text())
        if not isinstance(ledger_rows, list):
            ledger_rows = []
    except (OSError, ValueError):
        ledger_rows = []
    live_n = len(ledger_rows)
    # 设备目录可能已经被删：台账还记着它，渲染出来只会得到一个起不来的单元。
    on_disk = {p.name for p in (PROJECT / ".warp").glob("usque*")
               if (p / "config.json").exists()}
    saved_dev: dict[int, str] = {}
    for r in ledger_rows:
        try:
            idx, dev = int(r["index"]), str(r["device"])
        except (KeyError, TypeError, ValueError):
            continue
        if dev in on_disk:
            saved_dev[idx] = dev
    # 容量 = 台账设备 + 空闲设备。前者按 index 认领（见下面的分配），后者
    # 留给扩容出来的新线。
    devs = list(dict.fromkeys([*saved_dev.values(), *free_devs]))
    free_pool = [d for d in devs if d not in set(saved_dev.values())]

    live_nodes = {}
    try:
        _cfg = json.load(open(PROJECT / "singbox-config.json"))
        live_nodes = {o["tag"]: o for o in _cfg.get("outbounds", [])
                      if o.get("server") and o.get("server_port")}
    except (OSError, ValueError, KeyError):
        pass
    reach = _alive_many(live_nodes) if live_nodes else {}

    # **编号（=端口偏移）是既有部署的一部分，不能重算**。
    # `WARP-H2-<COLO>-<N>` ↔ SOCKS 1180+N，这套映射写进了
    # `.warp/warp-outbound.json` 和主 `opencode-out` 的成员表，daemon 台账
    # 与 `backfill_for` 也按它认线。渲染器一换顺序，19 条里 17 条就对不上
    # （实测交集只剩 2 个）——出口在跑，selector 指不到。
    # 所以：**顺序取既有成员的顺序，只有节点按 colo 池子重选**。
    # 详见 docs/established-facts.md 第 13c 条。
    # 既有成员原序 + 块模型里多出来的渠道自动续号（新 colo 靠这条进线）。
    fixed_order, added_members = _member_order(chs)
    if added_members:
        print(f"＋ 新增 {len(added_members)} 条 H2 成员（块模型有、成员表没有）: "
              f"{' '.join(added_members)}", file=sys.stderr)

    # 按 colo 建池：该 colo 下所有 h2 渠道的候选节点并集，按可达性排序。
    pools_by_colo: dict[str, list[str]] = {}
    for ch in chs:
        pool = _reachable_order(
            [t for t in (ch.get("nodes") or [ch["node"]]) if t],
            live_nodes, bool(live_nodes), reach)
        if not pool:
            continue
        acc = pools_by_colo.setdefault(ch["colo"], [])
        for t in pool:
            if t not in acc:
                acc.append(t)
    # 可达性再排一次：各渠道的池子拼起来后，顺序可能不是全局最优。
    for colo in pools_by_colo:
        pools_by_colo[colo] = _reachable_order(
            pools_by_colo[colo], live_nodes, bool(live_nodes), reach)

    # 想渲染多少条 = 每个 colo「成员数」与「池子大小」取小后求和。
    # 不能拿「块模型渠道去重后的节点数」当上限：同一 colo 的多条线是从
    # 该块**整个池子**里轮转取节点的（`nodes` 是整块池，`per` 只决定渠道数），
    # 池子通常比渠道多。用渠道数当上限会把已经扩出来的成员白白截掉。
    demand: dict[str, int] = {}
    for _, colo in fixed_order:
        demand[colo] = demand.get(colo, 0) + 1
    want_n = sum(min(cnt, len(pools_by_colo.get(colo) or []))
                 for colo, cnt in demand.items())
    if want_n == 0:
        print(f"⚠ 没有可渲染的渠道（渠道 {len(chs)} 条，成员 {len(fixed_order)} 条）；"
              f"先跑 tools/merge_h2_blockmap.py", file=sys.stderr)
        return 1

    # **设备不够就自动建**（用户规则 2026-11-05）。新块冒出来时渠道数会超过
    # 设备数，过去这一步靠人跑 h2-add-devices.sh，于是新 colo 永远停在
    # 「块有了、线没有」。--print 不注册：那是只读预览，注册会消耗 CF 配额。
    if want_n > len(devs) and not print_only:
        short = want_n - len(devs)
        print(f"＋ 设备不足（需要 {want_n} 台，现有 {len(devs)} 台），"
              f"自动注册 {short} 台…", file=sys.stderr)
        try:
            r = subprocess.run(
                [str(PROJECT / "deploy" / "h2-add-devices.sh"), str(short)],
                capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                # 多半是 CF 限流：已经注册到几台就用几台，下轮再补。
                print(f"⚠ 自动注册未全部成功（{short} 台）："
                      f"{(r.stderr or r.stdout).strip().splitlines()[-1][:160] if (r.stderr or r.stdout).strip() else ''}",
                      file=sys.stderr)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"⚠ 自动注册失败: {type(e).__name__}", file=sys.stderr)
        free_devs = free_devices(unit_dir)
        devs = list(dict.fromkeys([*saved_dev.values(), *free_devs]))
        free_pool = [d for d in devs if d not in set(saved_dev.values())]

    n = min(want_n, len(devs))
    # **不许因为设备不够而缩小线上规模**。设备数 < 台账条数时，n 会变小，
    # 渲染出来的单元是 0..n-1 —— 与线上 0..18 的编号重叠，`h2-install.sh`
    # 一装就把后 16 条停掉（单元文件不在了），而台账/daemon 成员仍以为它们
    # 在跑。实测：加 3 台空闲设备后 n=3，正好会砍掉 16 条生产线路。
    # 正确做法是扩容设备（h2-add-devices.sh），不是降级渲染。
    if live_n and n < live_n and not print_only:
        print(f"✗ 拒绝渲染：线上已有 {live_n} 条 H2，本次只能渲染 {n} 条"
              f"（可用设备 {len(devs)} 台）。直接安装会停掉其余 "
              f"{live_n - n} 条线路。\n"
              f"  扩容设备：./deploy/h2-add-devices.sh {live_n - n}\n"
              f"  只预览：加 --print（不写文件）", file=sys.stderr)
        return 1
    if n < want_n:
        print(f"⚠ 渠道 {len(chs)} 条（去重 {len({c['node'] for c in chs})} 个节点），"
              f"成员 {len(fixed_order)} 条，可用设备 {len(devs)} 台 —— "
              f"只渲染前 {n} 条；其余等设备释放", file=sys.stderr)

    seen_node, picked = set(), []
    # 每个 colo 已分出去几条：同块多条线各自从池子不同位置起步。
    per_colo: dict[str, int] = {}
    for member, colo in fixed_order:
        pool = pools_by_colo.get(colo) or []
        if not pool:
            print(f"⚠ {member}: colo={colo} 没有可用节点，跳过", file=sys.stderr)
            continue
        k = per_colo.get(colo, 0)
        per_colo[colo] = k + 1
        rot = pool[k % len(pool):] + pool[:k % len(pool)]
        node = next((t for t in rot if t not in seen_node), None)
        if node is None:
            continue
        seen_node.add(node)
        picked.append({"tag": member, "member_tag": member, "colo": colo,
                       "node": node, "nodes": pool})
        if len(picked) == n:
            break
    if len(picked) < n:
        print(f"⚠ 只排到 {len(picked)} 条（{n} 台设备）", file=sys.stderr)

    inbounds, outbounds, rules, ledger = [], [], [], []
    written: set[str] = set()           # 已写过的节点（出站全局去重）
    for j, ch in enumerate(picked):
        # **编号取自成员名**（`WARP-H2-<COLO>-<N>` 的 N），不是循环位置：
        # 两者在「设备数 < 渠道数」时必然不同，而端口 1180+N 是写进
        # `warp-outbound.json` 与主 selector 的既有契约。
        i = int(ch["member_tag"].rsplit("-", 1)[1])
        # **设备按编号认领，不按循环位置**。台账里记着这条线用的哪台设备，
        # 换渲染顺序/跳过某条线都不该让它改绑别人的设备 —— 每台设备是一个
        # CF 会话身份，重绑等于两条线互换身份（双方都要重连）。新扩容出来的
        # 线才从空闲池里取。
        dev = ch.get("_saved_device") or saved_dev.get(i)
        if not dev and free_pool:
            dev = free_pool.pop(0)
        if not dev:
            print(f"⚠ {ch['member_tag']}: 没有可用设备，跳过", file=sys.stderr)
            continue
        inbound_port = BASE_INBOUND + i
        socks_port = BASE_DEVICE_PORT + i
        tag = ch["node"]
        # 整个候选池都写进配置：sing-box 不支持运行时追加出站，只有已声明的
        # 出站才能被 selector 切过去。池子里取不到定义的节点跳过（订阅漂移）。
        # 出站按**节点**全局只写一份，多条线的 selector 共用：anytls 出站是
        # 无状态的，复制 N 份只是把配置撑大 3 倍（实测 224 份 → 71 份）。
        cands: list[str] = []
        for t in [tag, *(ch.get("nodes") or [])]:
            ob_tag = f"h2-{t}"[:120] if t else ""
            if not ob_tag or ob_tag in cands:
                continue
            if t not in written:
                try:
                    c = node_outbound(t)
                except KeyError:
                    if t == tag:
                        # 节点既不在主配置也不在沉睡池：这条线的落地节点已经从订阅
                        # 里消失（远端 2026-11-05 实测 `normal.cn.greatgameupload.org:25526-any`）。
                        # 一条线缺节点不该让整批 H2 渲染失败 —— 原来 KeyError 冒出去，
                        # 22 条线一条都没写，报错只有一行 traceback 混在部署输出里。
                        print(f"⚠ {ch['member_tag']}: 节点 {tag} 不在主配置/沉睡池，"
                              f"跳过", file=sys.stderr)
                    continue
                c["tag"] = ob_tag
                outbounds.append(c)
                written.add(t)
            cands.append(ob_tag)
        # 当前节点拿不到定义就整条跳过：默认必须落在台账记的那个节点上，否则
        # 台账 → selector 的对应关系一渲染就偏（校验器会报「当前节点不在池里」）。
        if not cands or cands[0] != f"h2-{tag}"[:120]:
            continue
        default_tag = cands[0]          # 池首 = 块模型选定的落地节点
        inbounds.append({"type": "direct", "tag": f"h2in-{i}",
                         "listen": "127.0.0.1", "listen_port": inbound_port})
        sel = f"h2-sel-{i}"
        outbounds.append({"type": "selector", "tag": sel,
                          "outbounds": cands, "default": default_tag})
        # `action` 必须显式写 "route"。sing-box 1.13 里缺省 action 的规则被当作
        # `reject` —— 连接建立后立刻关，日志里只有一行 inbound connection，
        # 看起来像节点/CF 侧断流，实际是本机把连接拒了（实测踩到）。
        rules.append({"inbound": [f"h2in-{i}"], "action": "route",
                      "override_address": H2_DST.split(":")[0],
                      "override_port": int(H2_DST.split(":")[1]),
                      "outbound": sel})
        ledger.append({
            "index": i, "channel": ch["tag"], "colo": ch.get("colo_measured"),
            "node": tag, "device": dev,
            "inbound_port": inbound_port,
            "socks_port": socks_port,
            "unit": f"{BASE_UNIT}-{i}.service",
            # 成员名原样沿用既有成员表（见上面 fixed_order），不在这里重造：
            # 名字 ↔ 端口 1180+N 是写进 `warp-outbound.json` 与主 selector 的
            # 契约，重造就会让 19 条里大半对不上。
            "member_tag": ch["member_tag"],
            # 池子写进台账：这条线节点失效时该换到谁。同块多条渠道共用同一个
            # 候选序列（块内池子通用），各自从不同起点开始。
            "pool": [t for t in (ch.get("nodes") or []) if t],
            # 换节点时 daemon 只 PUT 这个 selector（节点 → 出站 tag 加 `h2-`
            # 前缀），不重启任何进程。
            "selector": sel,
            "candidates": cands,
        })

    cfg = {
        "log": {"level": "warn"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "dns": prod_dns,
        "route": {"rules": rules, "final": outbounds[0]["tag"],
                  "default_domain_resolver": {"server": "dns-default"}},
        "experimental": {"clash_api": {
            "external_controller": f"127.0.0.1:{H2_CLASH_PORT}",
            "secret": get_singbox_secret(),
        }},
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
        # 同理不带节点名：脚本只描述「哪个设备 → 哪个入站 → 哪个 socks 端口」。
        s.write_text(
            "#!/bin/bash\n"
            f"cd {PROJECT}/.warp/{r['device']} || exit 1\n"
            f"exec ./usque -c {conf} socks --http2 -s '' -P {r['inbound_port']} "
            f"-b 127.0.0.1 -p {r['socks_port']}\n")
        s.chmod(0o755)

        # User= 不能写死某个用户名：远端主机的仓库属主不是 administrator。
        # 用 stat 取仓库属主，与 install.sh/wg-lines-install.sh 的 RUN_USER 同源。
        unit = out / "units" / r["unit"]
        # Description 里**不能带节点名**：换节点只改 sing-box 的出站，usque 本身
        # 一点关系都没有。带上节点名会让单元文件逐字节变化，安装脚本据此判定
        # 「这条线变了」并重启 usque —— 自动同步每轮重选节点就成了全量抖动。
        unit.write_text(f"""[Unit]
Description=WARP H2 line {r['index']} ({r['colo']})
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


def check_gap() -> int:
    """只读：块模型里有、成员表里没有的 H2 线。有差集退出 1。

    给 daemon 的定时任务用 —— 它只需要知道「要不要补」，不该为此跑一次
    带网络探活的完整渲染。
    """
    chs = h2_channels()
    if not chs:
        return 0
    _, added = _member_order(chs)
    for t in added:
        print(t)
    return 1 if added else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="print_only")
    ap.add_argument("--check", action="store_true",
                    help="只列出待补成员，有差集退出 1（不写文件、不联网）")
    args = ap.parse_args()
    if args.check:
        return check_gap()
    return render(args.print_only)


if __name__ == "__main__":
    sys.exit(main())
