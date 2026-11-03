#!/usr/bin/env python3
"""WARP 渠道分配模型：块 = colo，渠道 = (类型, 块, 序号)。

纯模型：不测量、不碰网络、不写文件（CLI 子命令除外）。设计依据见
`docs/warp-channel-allocation.md`，改这里之前先读那三节。

三条硬约束：
  1. **块按 colo 切，不按落地 /64**。段是会话建立时的抽签结果：同一节点连测
     5 轮落到 5 个不同 /64，7/16 组连 /48 都会变。按段切块，今天分的块明天
     重连就散架。
  2. **colo 取实测 trace 值，不取订阅桶标签**。实测 CFB 桶落地是 SJC、PHX 桶
     有 4/11 是 LAX、OLD 桶是 LAX —— 桶名是机场自己的编号习惯，不是机房。
  3. **延迟只读缓存**。本模块一次测量都不做：换线路径每多等一秒，坏出口就多
     服务一秒失败请求。缓存由 `_health_pool` 每 300s 刷新、探测失败即从表里
     pop，所以「过期值」在生产者侧已经消掉 —— 这里只负责按值排序。

块的成员来自实测表 `.warp/block-map.json`（gitignored，由 `build-map` 生成），
配置里只放块的静态属性（colo / 机房 / 当前封禁率）。两处各存一份节点清单
必然会漂移，所以配置里没有 `nodes[]`。

三个消费点，各自只拿自己需要的那一半：
  - `plan_lines()`     → `warp_wg.apply`：渠道落成线路（节点 + 族 + 块内后备顺序）
  - `night_line_tags()`→ `daemon._night_allow_tags`：夜间白名单按块生成
  - `plan` CLI         → 人：当前会开几条什么渠道，不写盘
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

from config import CFG, PROJECT_DIR

PROJECT = Path(PROJECT_DIR)

# 常规 / 非常规的分界是**稳定性**，不是承载方式：代理承载的节点只要
# success/attempts == 1 就进常规块，和本机直连 WARP 的线同等待遇。
CATEGORIES = ("regular", "irregular")

# v4 只有一个合并块：v4 出口段高度收敛。实测 2026-11-02 四条 v4 线的出口
# 全部落在 104.28.x.x（243.105 / 237.117 / 205.115 / 205.117，只有三个 /24），
# 逐块开渠道只是同段重复 —— 拆到 11 个 colo 块各开 3 条并不会多出地址多样性，
# 反而让同一节点在 v6/v4 两条线里各占一份设备。
V4_COLO = "ALL"


# ---------------------------------------------------------------- 配置 / 实测表

def _blocks(cfg: dict) -> dict:
    return (cfg.get("warp") or {}).get("blocks") or {}


def _table(cfg: dict) -> list[dict]:
    return list(_blocks(cfg).get("table") or [])


def channels_per_block(cfg: dict) -> int:
    return max(1, int(_blocks(cfg).get("channels_per_block") or 2))


def enabled(cfg: dict) -> bool:
    return bool(_blocks(cfg).get("enabled"))


def map_path() -> Path:
    return PROJECT / ".warp" / "block-map.json"


def load_map() -> dict:
    try:
        data = json.loads(map_path().read_text())
    except (OSError, ValueError):
        return {}
    nodes = data.get("nodes")
    return nodes if isinstance(nodes, dict) else {}


# ---------------------------------------------------------------- 排序（纯读）

def order_nodes(nodes, delays: dict | None = None) -> list[str]:
    """按缓存延迟升序；没有延迟的排尾，同档按 tag 定序。

    未知排尾而不是排头：一条没测过的线可能是最慢的，把它排在候选首位等于
    拿生产流量去试错。同档按 tag 定序是为了让结果可复现 —— 排序不稳定会让
    两次 `plan` 给出不同拓扑，排查时分不清是配置变了还是顺序抖了。
    """
    delays = delays or {}

    def key(tag: str):
        d = delays.get(tag)
        if isinstance(d, int) and d > 0:
            return (0, d, tag)
        return (1, 0, tag)

    return sorted(nodes, key=key)


def _pool(nodes_map: dict, colo: str | None, category: str | None) -> list[str]:
    """实测表里属于该 (colo, 类别) 的节点 tag。

    `colo=None` 表示不筛块（v4 合并块用）。类别筛选是精确匹配：
    `category` 缺失的节点既不进常规也不进非常规 —— 没测出稳定性的节点
    不该被默认当成稳定的。
    """
    out = []
    for tag, e in nodes_map.items():
        if not isinstance(e, dict):
            continue
        if colo is not None and e.get("colo") != colo:
            continue
        if category is not None and e.get("category") != category:
            continue
        out.append(tag)
    return out


# ---------------------------------------------------------------- 渠道

def channel_tag(ch: dict) -> str:
    cat = {"regular": "REG", "irregular": "IRR", "proxy": "V4"}[ch["category"]]
    return f"WARP-BLK-{ch['colo']}-{cat}-{ch['index']}"


def excluded_colos(cfg: dict) -> set[str]:
    """结构性不可用的块（封禁率 100%）。设备不往这些块上挂。

    与 `keep_at_night` 的区别：那个是「夜里值不值得花一次重启」，这个连白天
    都不占设备 —— 一个段全封的 colo，新设备拿到的 /128 必然也在限额里。
    """
    return {str(c) for c in (_blocks(cfg).get("exclude_colos") or []) if c}


def channels(cfg: dict, nodes_map: dict | None = None,
             delays: dict | None = None) -> list[dict]:
    """展开成渠道清单；未启用返回空列表（默认关闭，不改变现网拓扑）。

    每块每类最多 `channels_per_block` 条。池子不够就少给，不拿同一个节点凑
    两条 —— 两条渠道绑同一节点等于一条（重启换的是同一个 colo 的同一个池）。

    `exclude_colos` 里的块整块跳过：封禁率 100% 的 colo 每条 /128 都在限额里，
    占一条设备就是占着不用（第一阶段的目的是测**还有余量**的块）。
    """
    if not enabled(cfg):
        return []
    # **不做 UDP 能力过滤**。`probe_node_udp` 用 STUN(3478) 判定，而 WARP 走的
    # 是 2408 —— 节点完全可以只放行 2408。实测 `pq.aws48` STUN=False，但它
    # 承载的 WARP-WG-5 正在正常出 v6 流量（colo=NRT）。拿它过滤会把健康节点
    # 整批判死（实测 22 条线里 13 条被误伤），v4 合并块更是直接清空。
    # 节点是否真能承载 WARP，只能靠**实际握手**验证 —— 也就是线路跑起来之后
    # 的出口探测，`warp_wg.apply()` 与 daemon 已经各自有那套判据。
    per = channels_per_block(cfg)
    skip = excluded_colos(cfg)
    nodes_map = load_map() if nodes_map is None else nodes_map
    out: list[dict] = []
    for blk in _table(cfg):
        colo = blk.get("colo")
        if colo in skip:
            continue
        # `pin`：块里没有「众数是这个 colo」的节点时，显式指定用哪几个节点来
        # 采样（当前只有 PIT 这么干）。它不改变节点的归属，只是允许同一台节点
        # 在另一个块里再挂一台设备 —— 所以这类块的块名是「预期落点」，实测
        # 落点以 trace 为准，`build_map` 会把漂移记进 `drift`。
        pin = [t for t in (blk.get("pin") or []) if t in nodes_map]
        for cat in CATEGORIES:
            pool = ([t for t in pin
                     if (nodes_map.get(t) or {}).get("category") == cat]
                    if pin else _pool(nodes_map, colo, cat))
            # H2 承载的节点走的是 TCP/HTTP2 隧道（见 deploy/render_h2_lines.py），
            # 不占 WG 线位。必须在**排序之前**剔掉：`apply()` 事后还会按
            # `carrier != 'h2'` 过滤一遍，若这里先把 h2 节点排进前 `per` 名，
            # 过滤后就只剩更少的渠道 —— 实测 2026-11-02 NRT 块的前三名全是
            # `bilibili-50037/50041`（carrier=h2），过滤后 REG 渠道整个消失。
            pool = [t for t in pool
                    if (nodes_map.get(t) or {}).get("carrier") != "h2"]
            # 排序先按**实测握手**分层，再按延迟：实测 2026-11-02
            # `us.bing.com:443~5-any` 和 `~7-any` 两轮 v6 全 handshake 失败，
            # 而它们的延迟很靠前 —— 按延迟排会把两条死线放进前几名。
            # 同层内保持延迟序（sorted 稳定）。
            ordered = sorted(order_nodes(pool, delays),
                             key=lambda t: _alive_rank(t, nodes_map, "v6"))
            for i in range(min(per, len(ordered))):
                out.append({"type": "v6", "colo": colo, "category": cat,
                            "index": i, "family": "v6", "node": ordered[i],
                            "nodes": ordered, "pinned": bool(pin)})
    # v4 单个合并块，且**排在清单最前面**。
    # WHY 排最前：它原先排在末尾，线路数不足时整块被截断 —— 实测 29 条线时
    # v4 渠道一条都分不到，v4 承载等于死代码。排在前面才能保证只要有 v4 渠道
    # 就一定先被分到线。v6 渠道数量远多于线路数，少几条不影响块覆盖。
    #
    # 候选池 = 走**机场代理**的节点（carrier != h2），不含直连 WARP 的那些：
    # 直连 WARP（usque 隧道）不经过机场，它的 v4 出口是本机 MASQUE 会话直接
    # 拿到的，与「代理承载」是两条完全不同的链路，混进来会让 v4 块里一半的
    # 渠道根本不该由 WG 线承担。
    # 排序先看 **v4 实测**，再看延迟：v6 稳定不等于能跑 v4 —— 实测
    # `cn.bing.com:443`（v6 regular）两轮 v4 全 handshake 失败，而 `pq.us02`
    # 两轮都拿到 warp=on。按延迟排会把前三名全给跑不了 v4 的节点。
    cand = [t for t in _pool(nodes_map, None, None)
            if (nodes_map.get(t) or {}).get("carrier") != "h2"]
    merged = sorted(order_nodes(cand, delays),
                    key=lambda t: _alive_rank(t, nodes_map, "v4"))
    for i in range(min(per, len(merged))):
        out.insert(i, {"type": "v4", "colo": V4_COLO, "category": "proxy",
                       "index": i, "family": "v4", "node": merged[i],
                       "nodes": merged})
    for ch in out:
        ch["tag"] = channel_tag(ch)
    return out


def plan_lines(chs: list[dict], total: int, base: int = 1,
               nodes_map: dict | None = None, delays: dict | None = None,
               rotation_size: int = 8) -> list[dict]:
    """渠道 → 线路：第 N 条线拿哪个节点、哪个族、坏了往哪切。

    只分配 `total` 条（线路数由 `warp.wg.count` 决定，不由块数决定）：渠道比
    线多时按 `channels()` 的顺序截断，少时报错 —— 少一条线等于少一个出口，
    静默用空节点补会导致该线永远连不上。

    同一节点只当一条线的 home（见下面的 `used`）；去重后不足 `total` 时
    返回**实际**条数 —— 调用方按 len(plan) 建线，不要拿空节点凑数。

    `rotation` 是本模块唯一「越界」的地方：块内后备顺序优先同块。同块切换大概率
    拿到同一 colo 的另一个段（v4 地址相同、v6 段不同），跨块切换等于换了个机房，
    应该排在后面 —— 与 `warp_wg._rotation_for` 的「先跨组」相反，那里按
    region_key 分组是为了分散，这里按块收敛是为了「同块内的段」能被试完。
    """
    if len(chs) < total:
        raise SystemExit(
            f"渠道不足：需要 {total} 条线路，实测表只展开出 {len(chs)} 条渠道"
            f"（{map_path()} 可能过期，或 warp.blocks.channels_per_block 太小）")
    nodes_map = load_map() if nodes_map is None else nodes_map
    global_pool = order_nodes(list(nodes_map), delays)
    out = []
    sibling_idx: dict[str, int] = {}      # 同一块里这是第几条兄弟线
    # 去重的粒度是 **(块, 出口族, 节点)**，不是节点本身。
    # 同一节点出现在两个块里是 `pin` 的**设计意图**，不是重复：PIT 没有自己
    # 的节点，只能借 ORD 那条来采样，而它的漂移轮次里 CF 确实把出口落在 PIT
    # —— 两条线共用一台落地机、但**出口地域不同**，这正是 pin 要抓的东西。
    # 同一节点上的 v4 与 v6 也是两条独立的线（不同设备、不同出口族），不算重复。
    # 真正要挡的是同一块里两条**同族**线绑同一节点：那才是同一个地址池，
    # 挂两根设备只是多一个地址，没有任何多样性。
    used: set[tuple[str, str, str]] = set()
    for i, ch in enumerate(chs[:total]):
        key = (ch["colo"], ch["family"], ch["node"])
        if key in used:
            continue
        used.add(key)
        # 同块（同 colo）的其余节点排前面，跨块的按全局延迟序垫后。
        same = [t for t in ch["nodes"] if t != ch["node"]]
        rotation = [ch["node"]]
        for t in same + [t for t in global_pool if t not in same]:
            if t != ch["node"] and t not in rotation:
                rotation.append(t)
            if len(rotation) >= max(1, rotation_size):
                break
        # 同块兄弟线错开后置顺序：三条线绑同一块时，若 rotation 一模一样，
        # 节点失效后它们会在同一轮全跳到同一个后备节点 —— 那台节点瞬间被
        # 三条线共抢，且这不是「分散风险」而是「集中风险」。
        # 旧路径 `apply()` 里的 `off = (i - 1) % per` 就是干这个的，块模型
        # 迁移时漏掉了。错开量按**块内序号**而不是全局序号：全局 i 会让不同
        # 块的线互相错位，同一块的两条线反而可能对齐。
        k = sibling_idx.get(ch["colo"], 0)
        sibling_idx[ch["colo"]] = k + 1
        if len(rotation) > 1 and k:
            head, tail = rotation[0], rotation[1:]
            off = k % len(tail)
            rotation = [head] + tail[off:] + tail[:off]
        out.append({"line": base + i, "tag": ch["tag"], "node": ch["node"],
                    "family": ch["family"], "colo": ch["colo"],
                    "category": ch["category"], "rotation": rotation})
    return out


# ---------------------------------------------------------------- 夜间白名单

def ban_rate(cfg: dict, colo: str | None) -> float | None:
    for blk in _table(cfg):
        if blk.get("colo") == colo:
            return blk.get("ban_rate")
    return None


def keep_at_night(cfg: dict, colo: str | None) -> bool:
    """该块夜里还值不值得花一次重启。

    「只要不是 100% 失败就用」—— 夜里产出率再低也比空着强；而 100% 的块
    （当前 NRT 12/12 段全封）重启只会换到另一个同样被封的 /128，白烧一次重启。
    封禁率未知（None，含 colo 不认识）的块必须留下：HKG/TPE/ORD/PIT 是当天才
    出现的池子，从没经历过夜间，第一阶段正是要测它们。
    """
    return ban_rate(cfg, colo) != 1.0


def night_channel_tags(cfg: dict, nodes_map: dict | None = None,
                       delays: dict | None = None) -> set[str]:
    """按块生成的渠道白名单（渠道名，不是线路名）。"""
    return {ch["tag"] for ch in channels(cfg, nodes_map, delays)
            if keep_at_night(cfg, ch["colo"])}


def night_line_tags(cfg: dict, line_nodes: dict[str, str],
                    nodes_map: dict | None = None) -> set[str]:
    """按块生成的**线路**白名单：`{线路 tag: 该线当前节点}` → 该留的线路 tag。

    模板是块，落点是线路：块决定「哪个 colo 夜里还值得刷」，线路台账决定
    「哪条线现在落在那个 colo」。拿渠道名当白名单是空的 —— 现网成员叫
    `WARP-WG-7`，不是 `WARP-BLK-NRT-REG-0`。节点不在实测表里（新节点、表过期）
    按「未知 colo」处理：未知一律保留，宁可白烧一次重启也不要在夜里把新段漏掉。
    """
    nodes_map = load_map() if nodes_map is None else nodes_map
    out = set()
    for line_tag, node_tag in line_nodes.items():
        colo = (nodes_map.get(node_tag) or {}).get("colo")
        if keep_at_night(cfg, colo):
            out.add(line_tag)
    return out


# ---------------------------------------------------------------- 实测 → 块表

def category_for(ok: int, n: int) -> str | None:
    """success/attempts == 1 → regular；有过失败也有过成功 → irregular。

    0 次成功不入块：不是「非常规」，是不可用 —— 放进去只会让换线每轮都先
    试一条注定失败的线。
    """
    if n <= 0 or ok <= 0:
        return None
    return "regular" if ok >= n else "irregular"


def merge_diag(path: Path, rows: list[dict]) -> dict[str, int]:
    """把 warp_diag 的握手结果并进已有的映射表，按出口族分别记。

    返回 `{"v6": n, "v4": m}`：各更新了几个节点。

    这是**合并**不是重建：diag 只回答「这台机器能不能承载 WARP」，回答不了
    colo / 类别（那是出口 trace 的产物）。整表重建会把没跑 diag 的节点一起丢掉。

    v6 与 v4 分开存：实测 `cn.bing.com:443` 的 v6 两轮全通，v4 两轮全
    handshake —— 合并成一份会把「能跑 v6」当成「能跑 v4」。
    """
    data = json.loads(path.read_text())
    nodes = data.setdefault("nodes", {})
    seen: dict[str, dict[str, list[int]]] = {"v6": {}, "v4": {}}
    for x in rows:
        rounds = [r for r in (x.get("rounds") or [])
                  if r.get("stage") in ("ok", "handshake")]
        if not rounds:
            continue
        fam = "v4" if (x.get("family") or "v6") == "v4" else "v6"
        hit = seen[fam].setdefault(x["tag"], [0, 0])
        hit[0] += sum(1 for r in rounds if r.get("ok"))
        hit[1] += len(rounds)
    for fam, tbl in seen.items():
        pre = "wg_" if fam == "v6" else "v4_"
        for tag, (ok, n) in tbl.items():
            e = nodes.setdefault(tag, {})
            e[pre + "ok"] = ok
            e[pre + "n"] = n
    from asip.atomic_io import write_json
    data["updated"] = int(time.time())
    write_json(str(path), data, mode=0o600, indent=2)
    return {k: len(v) for k, v in seen.items()}


def _alive_rank(tag: str, nodes_map: dict, family: str) -> tuple[int, int]:
    """排序键：实测能承载(0) < 没测过(1) < 实测不能(2)。

    这是**排序**不是准入：未知节点照样会被选中（新节点还没跑过 diag），只是
    排在已有实测且能用的之后 —— 拿没测过的线顶掉已验证的出口，等于用猜测
    换掉事实。实测不能的排最后，但也不剔除：节点会恢复，剔了下次还得重建表。
    """
    e = nodes_map.get(tag) or {}
    pre = "wg_" if family == "v6" else "v4_"
    n = e.get(pre + "n")
    if not n:
        return (1, 0)
    return (0 if e.get(pre + "ok") else 2, 0)


def build_map(probe_dir: str) -> dict:
    """把一轮实测（4 个结果文件）汇总成 `.warp/block-map.json`。

    按**出口 IP** 聚合而不是按节点：一个出口 IP 常被 2 个节点共用（95 个 IP /
    159 个节点），按节点聚合会把同一份观测算两遍，或在两个节点上得出不同结论。
    colo 用的是该 IP 上所有成功轮次的众数 —— colo 本身会漂（ORD 5 轮漂到 PIT
    一次），单轮值不能代表这个出口。
    """
    d = Path(probe_dir)
    inputs: dict[str, list[str]] = {}
    for name in ("tunnel_inputs.json", "dead44_inputs.json"):
        try:
            for item in json.loads((d / name).read_text()):
                inputs.setdefault(item["egress_ip"], []).append(item["tag"])
        except (OSError, ValueError, KeyError):
            continue
    attempts: Counter = Counter()
    oks: Counter = Counter()
    colos: dict[str, Counter] = {}

    def note(ip: str, ok_count: int, n: int, colo: str | None = None):
        attempts[ip] += n
        oks[ip] += ok_count
        if colo and ok_count:
            colos.setdefault(ip, Counter())[colo] += ok_count

    for name in ("warp_probe.json", "stab_result.json"):
        try:
            for x in json.loads((d / name).read_text()):
                note(x["egress_ip"], 1 if x.get("ok") else 0, 1, x.get("colo"))
        except (OSError, ValueError, KeyError):
            continue
    try:
        for x in json.loads((d / "retry_result.json").read_text()).values():
            note(x["egress_ip"], 1 if x.get("ok") else 0, 1, x.get("colo"))
    except (OSError, ValueError, KeyError):
        pass
    # 单节点隧道诊断（warp_diag.py）优先于旧 udp_probe：后者把「端口没绑上」也
    # 记成握手失败，44 个节点因此全判死。这里只数**握手层**的轮次 —— config/
    # port 两类是探针自己的故障，记进稳定性等于拿工具 bug 判节点死刑。
    diag = d / "diag44.jsonl"
    try:
        rows = [json.loads(l) for l in diag.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        rows = []
    # 按出口族分开记：节点能承载 v6 不等于能承载 v4。实测（2026-11-02）
    # `cn.bing.com:443`、`bilibili…50025` 的 v6 是 regular，但同一条节点跑 v4
    # 两轮全是 handshake 失败；反过来 `pq.us02` 两轮都拿到 warp=on。
    # 合并计数会让排序把「v6 好、v4 死」的节点排进 v4 合并块前三。
    v4_ok: dict[str, int] = {}
    v4_n: dict[str, int] = {}
    if rows:
        for x in rows:
            # 只剩握手层的轮次。全是 config/port 时**一次都不记**，不能记成
            # 「跑了 1 次、失败 1 次」—— 那会把探针自己的故障写进节点履历。
            rounds = [r for r in (x.get("rounds") or [])
                      if r.get("stage") in ("ok", "handshake")]
            if not rounds:
                continue
            # v6 走原路径（按出口 IP 聚合），v4 单独记在节点上 —— v4 的出口
            # 由落地 colo 决定，一个 colo 只有几个地址池，按 IP 聚合会把不同
            # 节点混成一条，反而看不出「这台机器能不能跑 v4」。
            if (x.get("family") or "v6") == "v4":
                v4_ok[x["tag"]] = v4_ok.get(x["tag"], 0) + sum(
                    1 for r in rounds if r.get("ok"))
                v4_n[x["tag"]] = v4_n.get(x["tag"], 0) + len(rounds)
            else:
                note(x["egress_ip"], sum(1 for r in rounds if r.get("ok")), len(rounds))
    else:
        # udp_probe 的 warp_rounds 每个 null 也是一次尝试（没建成），不能只数成功。
        try:
            for x in json.loads((d / "udp_probe_result.json").read_text()):
                rounds = x.get("warp_rounds") or []
                note(x["egress_ip"], sum(1 for r in rounds if r), len(rounds) or 1)
        except (OSError, ValueError, KeyError):
            pass

    nodes: dict[str, dict] = {}
    for ip, tags in inputs.items():
        ok, n = oks[ip], attempts[ip]
        cat = category_for(ok, n)
        if cat is None:
            continue
        colo = colos[ip].most_common(1)[0][0] if colos.get(ip) else None
        if not colo:
            continue
        # 漂移记录：colo 取众数（ORD 的节点 5 轮里 3 轮落 ORD、3 轮落 PIT、
        # 1 轮落 SIN，按众数归 ORD 是对的），但**丢掉其余落点就等于假装它没漂过**。
        # PIT 至今没有自己的节点，它就活在这条字段里 —— 哪天要单独开 PIT 块，
        # 得先有一个「众数是 PIT」的出口，而不是把 ORD 的线改个标签。
        mix = dict(colos[ip].most_common())
        drift = [c for c in mix if c != colo]
        for tag in tags:
            nodes[tag] = {"colo": colo, "category": cat, "ok": ok, "n": n,
                          "egress_ip": ip, "colo_mix": mix, "drift": drift}
    # v4 实测是**节点属性**，不是出口 IP 的属性（v4 出口由落地 colo 的小池子
    # 抽签，同 colo 的不同节点共用那几个地址）。所以它只挂在跑过 v4 diag 的
    # 节点上，不进按 IP 聚合的那张表。
    for tag, n in v4_n.items():
        nodes.setdefault(tag, {})["v4_ok"] = v4_ok.get(tag, 0)
        nodes[tag]["v4_n"] = n
    return {"updated": int(time.time()), "nodes": nodes}


# ---------------------------------------------------------------- CLI

def _plan(cfg: dict, delays: dict | None) -> int:
    chs = channels(cfg, delays=delays)
    if not chs:
        print("块模型未启用（warp.blocks.enabled=false）", file=sys.stderr)
        return 1
    for ch in chs:
        print(f"{ch['tag']:26s} {ch['family']:2s} node={ch['node']}")
    by_type: dict[str, int] = {}
    for ch in chs:
        by_type[ch["type"]] = by_type.get(ch["type"], 0) + 1
    print(f"共 {len(chs)} 条渠道 {by_type}", file=sys.stderr)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="WARP 渠道分配模型")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan", help="按实测表展开渠道清单（纯读，不写任何文件）")
    p = sub.add_parser("build-map", help="由实测结果生成 .warp/block-map.json")
    p.add_argument("probe_dir", help="含 warp_probe/stab_result/… 的目录")
    p.add_argument("--dry-run", action="store_true")
    q = sub.add_parser("merge-diag",
                       help="把 warp_diag.py --out 的握手结果并进 block-map.json")
    q.add_argument("diag_jsonl", help="warp_diag.py --out 的输出文件")
    args = ap.parse_args()

    if args.cmd == "plan":
        raise SystemExit(_plan(CFG, None))
    if args.cmd == "merge-diag":
        rows = [json.loads(l) for l in
                Path(args.diag_jsonl).read_text().splitlines() if l.strip()]
        n = merge_diag(map_path(), rows)
        print(f"更新 v6 {n['v6']} 个、v4 {n['v4']} 个节点的握手结论",
              file=sys.stderr)
        return
    out = build_map(args.probe_dir)
    by_cat = Counter(v["category"] for v in out["nodes"].values())
    by_colo = Counter(v["colo"] for v in out["nodes"].values())
    print(f"{len(out['nodes'])} 个节点入块 {dict(by_cat)}", file=sys.stderr)
    print(f"块分布 {dict(by_colo)}", file=sys.stderr)
    if not args.dry_run:
        from asip.atomic_io import write_json
        write_json(str(map_path()), out, mode=0o600, indent=2)
        print(f"→ {map_path()}", file=sys.stderr)


if __name__ == "__main__":
    main()
