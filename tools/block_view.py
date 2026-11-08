#!/usr/bin/env python3
"""块视图：每个 colo 块里有哪些节点、延迟多少、谁正被这个块用着。

回答三个问题（此前只能靠读 `block-map.json` + 台账 + 手工对照）：

  1. **这个块有哪些节点** —— 实测表 `.warp/block-map.json` 里 colo 相同的全部
     节点，连同它的类别（regular/irregular）、实测握手 ok/n、出口 IP。
  2. **延迟多少** —— 默认读 sing-box `/proxies/<tag>` 里 daemon 每 5 分钟
     一轮健康检查留下的 `history`。**不要默认现测**：sing-box 只有一条出口，
     一次测几十个节点每个 3~15s，纯属白占。`--delay` 才现测。
  3. **谁正被这个块用着** —— WG 线（`.warp/warp_wg_state.json` 的 `node`）、
     H2 线（`.warp/h2/channels.json` 的 `node`），以及块模型当前会挑哪几个
     （`warp_blocks.channels()`，与 `render_h2_lines` / `warp_wg.apply` 同一份）。

用法：
    python3 tools/block_view.py                    # 全部块
    python3 tools/block_view.py --colo NRT         # 只看一个块
    python3 tools/block_view.py --delay            # 现测延迟（慢，且会丢一部分）
    python3 tools/block_view.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

_env = PROJECT / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import warp_blocks  # noqa: E402
from config import CFG, get_singbox_secret  # noqa: E402

COLO_LOC = {b["colo"]: b.get("loc") for b in CFG["warp"]["blocks"]["table"]}
# 块表没收录的 colo（BOM 是 2026-11-04 新订阅实测出来的）。
COLO_FALLBACK = {"BOM": "孟买", "AMS": "阿姆斯特丹", "FRA": "法兰克福",
                 "LHR": "伦敦", "PIT": "匹兹堡"}


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _entry(raw) -> dict:
    """sources 台账的一条 → `{sub, label, name}`；旧台账是纯字符串。"""
    if isinstance(raw, dict):
        return {"sub": str(raw.get("sub") or ""),
                "label": str(raw.get("label") or ""),
                "name": str(raw.get("name") or "")}
    return {"sub": "", "label": str(raw or ""), "name": ""}


def node_identity() -> dict[str, tuple]:
    """节点 tag → **物理出口**指纹（SNI, 类型, 密码）。

    订阅里有两种 tag 指向同一台机器：沉睡池的旧入口快照（`bilibili-...:50001`）
    与订阅里的新入口（`cn.bing.com:443~21`）。按 tag 去重看不出重复，按这台机器
    的身份才看得出 —— 机场换域名时只会换入口，SNI 与密码不变。实测 55 条渠道
    只占 38 个物理出口，12 组渠道挤在同一台上。
    """
    try:
        cfg = _read_json(CFG["singbox"]["config_file"]) or {}
    except Exception:
        return {}
    out = {}
    for o in cfg.get("outbounds") or []:
        tag = o.get("tag")
        if not tag or not o.get("server"):
            continue
        tls = o.get("tls") or {}
        # 不含 server/port：机场换入口换的**正是**这两项，它们是我们要看穿的变量。
        out[tag] = (tls.get("server_name") or "", o.get("type") or "",
                    o.get("password") or "")
    return out


def load_sources() -> dict[str, dict]:
    """节点 tag → {sub, label, name}，外加**无歧义** host:port 索引。

    同 `channel_report.source_index`：一台机器上常挂十几个不同名字的节点，
    多义的 host:port 不能反查 —— 那会把别的节点的名字挂过来。只有该
    host:port 在台账里唯一时才建索引。
    """
    # 跟随 `SINGBOX_CONFIG`，不写死文件名：换配置路径后仍能读到归属台账。
    raw = _read_json(Path(CFG["singbox"]["config_file"] + ".sources.json")) or {}
    idx = {tag: _entry(v) for tag, v in raw.items()}
    by_hp: dict[str, list[str]] = {}
    for tag in idx:
        m = re.match(r"^(.+:\d+)(?:~\d+)?-", tag)
        if m:
            by_hp.setdefault(m.group(1), []).append(tag)
    for hp, tags in by_hp.items():
        if len(tags) == 1:
            idx.setdefault(hp, idx[tags[0]])
    return idx


def lookup_source(idx: dict[str, dict], node: str) -> dict:
    hit = idx.get(node)
    if hit:
        return hit
    m = re.match(r"^(.+:\d+)(?:~\d+)?-", node)
    return (idx.get(m.group(1)) if m else None) or {
        "sub": "-", "label": "-", "name": "-"}


def colo_name(colo: str | None) -> str:
    return COLO_LOC.get(colo or "") or COLO_FALLBACK.get(colo or "", "?")


# 承载方式的**对外叫法**。配置里的 `carrier` 是「这个 colo 允许走哪条路」，
# 词面（proxy / both）会被读成「只有代理」，而它实际包含 WG-over-UDP 与
# H2/MASQUE 两种 WARP 承载。纯代理（不承载 WARP）是另一回事，必须分开叫。
CARRIER_LABEL = {
    "proxy": "warp-proxy",     # WG over UDP，经机场节点中转
    "both": "全部承载",         # 该 colo 允许 WG 与 H2 两条路
    "h2": "h2-warp",           # MASQUE over HTTP/2
    "direct": "warp-direct",   # 直连 WARP（usque 隧道，不经节点）
}


def bearer(colo: str | None) -> str:
    """块表里这个块允许的承载路径（配置口径 → 对外叫法）。"""
    for b in CFG["warp"]["blocks"]["table"]:
        if b.get("colo") == colo:
            return CARRIER_LABEL.get(str(b.get("carrier") or "both"), "?")
    return "?"


def _tier_disabled() -> set[str]:
    """当前档位下停用的 WG 线 tag（与 daemon._TIER_DISABLED 同一份口径）。

    台账记的是「这条线绑在哪个节点」，**不等于单元在跑**。档位停用后
    台账不变，若照样算进 `current_bindings`，块标题会写成「在用 7」而
    实际一条都没跑 —— 与 established-facts 第 26 条「members 是声明」
    同一类陷阱。
    """
    import warp_blocks
    return warp_blocks.tier_disabled()


def current_bindings() -> dict[str, list[dict]]:
    """节点 tag → [{channel, carrier, unit}]，当前实际绑在这台节点上的线。"""
    out: dict[str, list[dict]] = {}
    off = _tier_disabled()

    def add(node, ch, carrier, unit):
        if node and ch not in off:
            out.setdefault(node, []).append(
                {"channel": ch, "carrier": carrier, "unit": unit})

    for tag, line in ((_read_json(PROJECT / ".warp/warp_wg_state.json") or {})
                      .get("lines") or {}).items():
        add(line.get("node"), tag, "WG", line.get("unit"))
    for row in _read_json(PROJECT / ".warp/h2/channels.json") or []:
        add(row.get("node"), row.get("member_tag") or row.get("channel"),
            "H2", row.get("unit"))
    return out


def planned() -> dict[str, str]:
    """块模型**当前会挑**的节点 → 渠道 tag（与渲染器同一份模型）。"""
    try:
        chs = warp_blocks.channels(CFG)
    except Exception:
        return {}
    return {ch["node"]: ch["tag"] for ch in chs}


def blocks(nodes_map: dict) -> dict[str, list[str]]:
    """colo → 该块的全部节点 tag（按实测类别排序：regular 在前）。"""
    out: dict[str, list[str]] = {}
    for tag, e in nodes_map.items():
        if not isinstance(e, dict) or not e.get("colo"):
            continue
        out.setdefault(e["colo"], []).append(tag)
    for v in out.values():
        v.sort(key=lambda t: (nodes_map[t].get("category") != "regular",
                              nodes_map[t].get("carrier") == "h2", t))
    return out


def measure(nodes: list[str], timeout_ms: int) -> dict[str, int | None]:
    """向 sing-box 现测节点延迟。失败/不可用返回 None。

    `timeout_ms` 不给默认值：跨洋节点 5s 会误判（实测 15s 与 5s 结果相近，
    但 5s 对慢节点偏紧），让调用方显式决定。
    """
    if not nodes:
        return {}
    from asip.clash_client import ClashClient
    client = ClashClient(CFG["singbox"]["clash_api"], get_singbox_secret)
    return client.delays(nodes, workers=16, timeout_ms=timeout_ms)


def cached_delays() -> dict[str, int]:
    """读 sing-box 里**已有**的延迟记录 → `{tag: ms}`。

    daemon 每 `node_health_sec`(300s) 把代理池节点测一轮，结果就写在
    `/proxies/<tag>` 的 `history` 里。块视图原来只在 `--delay` 时现测，
    默认整列 `—`，而这些结果一直躺在 sing-box 里没人读。

    只收成功记录：sing-box 的 history 本来也只在成功时追加。**覆盖不全会
    导致误读** —— 不在 `proxy` selector 组里的节点（实测 29/112，多数是
    H2）daemon 不测、`/delay` 也返回 503，永远显示 `—`，不是它们不通。
    """
    try:
        from asip.clash_client import ClashClient
        client = ClashClient(CFG["singbox"]["clash_api"], get_singbox_secret)
        status, data = client.get_json("/proxies")
    except Exception:
        return {}
    if status != 200 or not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for tag, v in (data.get("proxies") or {}).items():
        hist = (v or {}).get("history") if isinstance(v, dict) else None
        last = hist[-1] if isinstance(hist, list) and hist else None
        d = last.get("delay") if isinstance(last, dict) else None
        if isinstance(d, int) and not isinstance(d, bool):
            out[tag] = d
    return out


def _verdict(e: dict, bound: list[dict]) -> str:
    """该节点能不能承载 WARP。判据优先级：正在服务 > 实测握手 > 没测过。

    「正在服务」是最强证据：`build_map` 只对跑过 diag 的节点记 `wg_*`，
    而实网里 22 条 WG 线有一大半的节点根本没有这项 —— 它们靠的是线路
    跑起来之后的出口探测。只看 wg_* 会把在跑的线全标成「未测」。

    **H2 线也算正在服务**：它经 `direct` 入站把裸 TCP 交给节点出站
    （`render_h2_lines` 的 override 规则），节点同样是数据路径的一环。
    只认 WG 会让 16 条在跑的 H2 节点显示成「H2-only / 未测」，看起来
    像没验证过 —— 而它们每条都在承载流量。两者分开标，因为承载能力
    不是一回事（H2 只用 TCP，WG 要 UDP）。
    """
    carriers = {x["carrier"] for x in bound}
    # 三种承载分开标，不合并成「在用」：它们在故障时的表现完全不同
    # （WG 要 UDP、H2 只要 TCP、直连根本不经过节点）。
    if "WG" in carriers:
        return "✓warp-proxy"
    if "H2" in carriers:
        return "✓h2-warp"
    if "DIRECT" in carriers:
        return "✓warp-direct"
    n = e.get("wg_n")
    if not n:
        return "h2-only" if e.get("carrier") == "h2" else "未测"
    return "✓" if e.get("wg_ok") else "✗"


def build(nodes_map: dict, delays: dict[str, int | None] | None = None,
          only: str | None = None, sources: dict | None = None) -> list[dict]:
    bound = current_bindings()
    plan = planned()
    off = _tier_disabled()
    idx = load_sources() if sources is None else sources
    out = []
    for colo, tags in sorted(blocks(nodes_map).items()):
        if only and colo != only:
            continue
        rows = []
        for tag in tags:
            e = nodes_map[tag]
            bnd = bound.get(tag) or []
            src = lookup_source(idx, tag)
            rows.append({
                "node": tag,
                "name": src["name"] or "-",
                "sub": src["sub"] or "-",
                "category": e.get("category"),
                "carrier": "h2-warp" if e.get("carrier") == "h2" else "warp-proxy",
                "verdict": _verdict(e, bnd),
                "ok": e.get("ok"), "n": e.get("n"),
                "wg_ok": e.get("wg_ok"), "wg_n": e.get("wg_n"),
                "v4_ok": e.get("v4_ok"), "v4_n": e.get("v4_n"),
                "egress_ip": e.get("egress_ip"),
                "delay": (delays or {}).get(tag),
                "bound": bnd,
                "planned": plan.get(tag),
            })
        out.append({
            "colo": colo, "loc": colo_name(colo), "bearer": bearer(colo),
            "ban_rate": warp_blocks.ban_rate(CFG, colo),
            "nodes": rows,
            "in_use": sum(1 for r in rows if r["bound"]),
            "planned_n": sum(1 for r in rows if r["planned"]),
            # 该块是否因档位失去了 WG 承载（块里本来有 warp-proxy 节点，全被停用）
            "tier_off": bool(off) and any(
                r["carrier"] == "warp-proxy" and not r["bound"] for r in rows
                if r["planned"] or r["ok"]),
        })
    return out


def _fmt_delay(delays: dict[str, int | None], tag: str) -> str:
    """延迟列：没测过用 `—`，测了但不通明确写「不通」。

    两者必须区分 —— 把「没测」画成「不通」会让人以为节点全挂了。
    `delays` 里**键不存在** = 没测过；**值为 None** = 测了不通。
    """
    if tag not in delays:
        return "—"
    d = delays[tag]
    return f"{d}ms" if isinstance(d, int) else "不通"


def _delay_view(cache: dict[str, int],
                live: dict[str, int | None] | None,
                tags: list[str]) -> dict[str, int | None]:
    """合成延迟列的数据源。**键不在表里 = 没测过**（画 `—`），值为 None = 不通。

    原来只有 `--delay` 才显示延迟，其余整列 `—`，但 daemon 每 5 分钟就在
    测同一批节点、结果一直躺在 sing-box 里。默认读缓存后，不现测也能看到
    真实数字。

    `--delay` 现测过的节点**以现测为准**，失败就写「不通」。实测串行复测
    并发失败的节点仍然全失败（503/504 是真的不通，不是排队超时），拿缓存
    盖掉会让 `--delay` 看起来像没生效。只有没请求现测的节点才回退缓存。
    """
    out: dict[str, int | None] = {}
    for t in tags:
        if live is not None and t in live:
            out[t] = live[t]          # 现测过：成功给数字，失败给 None
        elif t in cache:
            out[t] = cache[t]
    return out


def _w(s: str) -> int:
    """终端显示宽度：全角/emoji 占 2 列，其余 1 列。

    `f"{s:<26}"` 按**字符数**补齐，而中文/国旗 emoji 在终端里占两列 ——
    节点名几乎全是中文，不按显示宽度算的话每行都会错位，表头和数据对不上。
    """
    import unicodedata
    n = 0
    for ch in str(s):
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            n += 2
        elif ord(ch) >= 0x1F000:          # emoji（国旗、符号）在多数终端占两列
            n += 2
        else:
            n += 1
    return n


def _pad(s: str, width: int, left: bool = True) -> str:
    """按显示宽度补空格（不是按字符数）。超宽就截断，保证不撑破表格。"""
    out = ""
    used = 0
    for ch in str(s):
        w = _w(ch)
        if used + w > width:
            break
        out += ch
        used += w
    return out + " " * (width - used) if left else " " * (width - used) + out


def print_blocks(blocks_: list[dict], delays: dict[str, int | None]) -> None:
    head = (f"{_pad('订阅', 8)} {_pad('节点名（机场）', 30)} "
            f"{_pad('节点地址', 44)} {_pad('类别', 9)} "
            f"{_pad('承载', 11)} {_pad('状态', 13)} {_pad('出口 IP', 26)} "
            f"{_pad('延迟', 7, False)}  绑定 / 计划")
    for b in blocks_:
        ban = b["ban_rate"]
        ban_s = f" ban={ban:.0%}" if isinstance(ban, (int, float)) else ""
        off_s = " · WG档位停用" if b.get("tier_off") else ""
        print(f"\n■ {b['colo']} {b['loc']}  承载={b['bearer']}{ban_s}{off_s}  "
              f"节点 {len(b['nodes'])} · 在用 {b['in_use']} · 块模型选中 {b['planned_n']}")
        print("-" * max(120, _w(head) + 10))
        print(head)
        for r in b["nodes"]:
            marks = []
            for x in r["bound"]:
                marks.append(f"{x['channel']}({x['carrier']})")
            if r["planned"] and r["planned"] not in {x["channel"] for x in r["bound"]}:
                marks.append(f"计划:{r['planned']}")
            print(f"{_pad(r['sub'], 8)} {_pad(r['name'], 30)} "
                  f"{_pad(r['node'], 44)} {_pad(r['category'] or '-', 9)} "
                  f"{_pad(r['carrier'], 11)} {_pad(r['verdict'], 13)} "
                  f"{_pad(r['egress_ip'], 26)} "
                  f"{_pad(_fmt_delay(delays, r['node']), 7, False)}  "
                  f"{' '.join(marks) or '-'}")


def block_pool_view(only: str | None = None) -> list[dict]:
    """**块池视图**：一个块 = 该 colo 下所有适配节点，渠道只是里面的线位。

    与 `channels_view` 的区别是「先有块还是先有渠道」。用户对渠道块的定义是
    「同 colo 的节点放一块」，块里所有适配节点都算成员；`channels_per_block`
    只决定从池子里拉几条线，不决定谁能进块。

    旧视图逐渠道列，读起来像「一个块只有 3 个节点」，其余 18 个看着像没进块
    —— 实测 HKG 块 21 个候选、只显示 3 行，就是这个误会。
    """
    rows = channels_view(only)
    by: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["colo"], r["carrier"])
        b = by.setdefault(key, {
            "colo": r["colo"], "loc": r["loc"], "carrier": r["carrier"],
            "nodes": [], "channels": [],
        })
        b["channels"].append(r)
        for t in (r["pool"] or [r["node"]]):
            if t not in b["nodes"]:
                b["nodes"].append(t)
    out = []
    for b in by.values():
        wired = {r["node"] for r in b["channels"]}
        out.append({
            **b,
            "pool_n": len(b["nodes"]),
            "wired_n": len(wired),
            # 池子里没被排成线位的节点 = 替补，不是「没进块」
            "bench_n": len(b["nodes"]) - len(wired),
        })
    out.sort(key=lambda b: (b["colo"] or "", b["carrier"]))
    return out


def print_block_pools(blocks_: list[dict]) -> None:
    for b in blocks_:
        print(f"\n■ {b['colo']} {b['loc']} · {b['carrier']}  "
              f"块内节点 {b['pool_n']} · 排成线位 {b['wired_n']} · "
              f"候补 {b['bench_n']}")
        print("-" * 100)
        wired = {r["node"]: r["channel"] for r in b["channels"]}
        for t in b["nodes"]:
            ch = wired.get(t)
            mark = f"→ {ch}" if ch else "  候补"
            print(f"  {mark:26s} {t}")


def channels_view(only: str | None = None) -> list[dict]:
    """块模型的**渠道**视图：每条渠道 + 它从该块池子里挑中的节点 + 整个候选池。

    与 `blocks()` 的区别是视角：`blocks()` 从「节点」出发（这个块有哪些节点），
    这里从「渠道」出发（这个渠道选了谁、还能选谁）。块模型一轮会为同一块生成
    多条渠道（每类每块 `channels_per_block` 条），每条各绑一个节点；池子不够时
    多条渠道会指向同一个节点，那也是事实，不合并。
    """
    try:
        chs = warp_blocks.channels(CFG)
    except Exception as e:
        print(f"✗ 块模型不可用：{e}", file=sys.stderr)
        return []
    idx = load_sources()
    bound = current_bindings()
    ident = node_identity()
    rows = []
    for ch in chs:
        if only and ch.get("colo") != only:
            continue
        rows.append((ch, ch.get("node")))
    # 同一物理出口被几条渠道选中：>1 就是「换了一条线，出口其实没变」
    used: dict[tuple, list[str]] = {}
    for ch, node in rows:
        key = ident.get(node or "")
        if key:
            used.setdefault(key, []).append(ch.get("tag"))
    out = []
    for ch, node in rows:
        src = lookup_source(idx, node) if node else {"sub": "-", "name": "-"}
        peers = used.get(ident.get(node or "") or None, [])
        peers = [p for p in peers if p != ch.get("tag")]
        out.append({
            "channel": ch.get("tag"),
            "type": ch.get("type"),
            "carrier": "h2-warp" if ch.get("carrier") == "h2" else "warp-proxy",
            "colo": ch.get("colo"),
            "loc": colo_name(ch.get("colo")),
            "category": ch.get("category"),
            "index": ch.get("index"),
            "node": node,
            "name": src["name"] or "-",
            "sub": src["sub"] or "-",
            "pool": list(ch.get("nodes") or []),
            "pool_n": len(ch.get("nodes") or []),
            "bound": bound.get(node) or [],
            "shared_with": peers,
        })
    return out


def print_channels(rows: list[dict], delays: dict[str, int | None]) -> None:
    if not rows:
        print("（块模型未启用，或没有匹配的渠道）")
        return
    head = (f"{_pad('渠道', 26)} {_pad('承载', 11)} {_pad('colo', 4)} "
            f"{_pad('机房', 8)} {_pad('类别', 9)} {_pad('池', 5, False)}  "
            f"{_pad('选中节点', 44)} {_pad('订阅', 8)} {_pad('节点名', 26)} "
            f"{_pad('延迟', 7, False)}  状态")
    print(head)
    print("-" * _w(head))
    for r in rows:
        marks = [f"{x['channel']}({x['carrier']})" for x in r["bound"]]
        print(f"{_pad(r['channel'], 26)} {_pad(r['carrier'], 11)} "
              f"{_pad(str(r['colo'] or '-'), 4)} {_pad(r['loc'], 8)} "
              f"{_pad(str(r['category'] or '-'), 9)} {_pad(str(r['pool_n']), 5, False)}  "
              f"{_pad(r['node'] or '-', 44)} {_pad(r['sub'], 8)} "
              f"{_pad(r['name'], 26)} "
              f"{_pad(_fmt_delay(delays, r['node'] or ''), 7, False)}  "
              f"{' '.join(marks) or '未上线'}"
              + (f"  ⚠与 {len(r['shared_with'])} 条渠道同一台机器"
                 if r["shared_with"] else ""))


def print_pools(rows: list[dict]) -> None:
    """每块一条渠道的完整候选池（按块去重，池子内容块内通用）。"""
    seen = set()
    for r in rows:
        key = (r["colo"], r["carrier"])
        if key in seen:
            continue
        seen.add(key)
        print(f"\n■ {r['colo']} {r['loc']} · {r['carrier']} 候选池 {r['pool_n']} 个")
        for i, t in enumerate(r["pool"]):
            mark = "  ← 本块多条渠道都从这里挑" if i == 0 else ""
            star = "*" if t == r["node"] else " "
            print(f"  {star} {i + 1:2d}. {t}{mark}")


def main() -> int:
    ap = argparse.ArgumentParser(description="块视图：节点清单 / 延迟 / 谁在用")
    ap.add_argument("--colo", help="只看一个块（如 NRT）")
    ap.add_argument("--delay", action="store_true",
                    help="向 sing-box 现测延迟（慢，会占用出口）")
    ap.add_argument("--timeout-ms", type=int, default=5000)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--channels", action="store_true",
                    help="按渠道看：每条渠道选中哪个节点、候选池有多大")
    ap.add_argument("--pools", action="store_true",
                    help="附带打印每块的完整候选节点池")
    ap.add_argument("--blocks", action="store_true",
                    help="按块看：块内全部节点，标出哪几个排成了线位、哪些是候补")
    args = ap.parse_args()

    if args.blocks:
        data = block_pool_view(args.colo)
        if args.json:
            print(json.dumps({"generated": int(time.time()), "blocks": data},
                             ensure_ascii=False, indent=1))
        else:
            print_block_pools(data)
        return 0

    if args.channels or args.pools:
        rows = channels_view(args.colo)
        tags = sorted({r["node"] for r in rows if r["node"]})
        live = None
        if args.delay:
            print(f"测 {len(tags)} 个节点的延迟…", file=sys.stderr)
            live = measure(tags, args.timeout_ms)
        delays = _delay_view(cached_delays(), live, tags)
        if args.json:
            print(json.dumps({"generated": int(time.time()),
                              "channels": rows}, ensure_ascii=False, indent=1))
        else:
            print_channels(rows, delays)
            if args.pools:
                print_pools(rows)
        return 0

    nodes_map = warp_blocks.load_map()
    if not nodes_map:
        raise SystemExit("✗ 实测表为空（先跑 warp_blocks.py build-map）")
    by_colo = blocks(nodes_map)
    tags = by_colo.get(args.colo, []) if args.colo else [
        t for v in by_colo.values() for t in v]
    live = None
    if args.delay:
        print(f"测 {len(tags)} 个节点的延迟…", file=sys.stderr)
        live = measure(tags, args.timeout_ms)
    delays = _delay_view(cached_delays(), live, tags)
    data = build(nodes_map, delays, args.colo)
    if args.json:
        print(json.dumps({"generated": int(time.time()), "blocks": data},
                         ensure_ascii=False, indent=1))
    else:
        print_blocks(data, delays)
        if args.delay:
            print("\n注：延迟是 sing-box 用**节点出站**打 gstatic 测的。H2 线的"
                  "真实路径是 usque→direct 入站→节点，与这条测量不同 —— "
                  "H2 节点显示「不通」不等于它跑不了 H2。", file=sys.stderr)
        else:
            print("\n注：延迟来自 daemon 每 5 分钟一轮的健康检查缓存（现测用 "
                  "--delay）。`—` = 没测过 —— 不在 `proxy` selector（纯代理"
                  "轮换池）里的节点 daemon 不测、sing-box 也不许现测，"
                  "不是它们不通。\n"
                  "承载口径：`warp-proxy` = WG over UDP 经节点中转 · "
                  "`h2-warp` = MASQUE over HTTP/2 · `warp-direct` = 直连 WARP"
                  "（不经节点）· 纯 `proxy` = 只走机场代理、不承载 WARP。",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
