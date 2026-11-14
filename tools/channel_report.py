#!/usr/bin/env python3
"""渠道健康总览：ban 率 / 成功率 / 失败数 / 物理落点 / colo / 订阅源 / 订阅地址。

**只读**本机台账，不测量、不写任何运行状态：

    .warp_line_state.json     每条线的模型层实测样本 [(ts, verdict)]、锁定、重启数
    .warp_ip_history.jsonl    出口 IP 换线历史 → 按 colo 现算窗口内 ban 率
    .warp/block-map.json      节点 → colo / 类别 / 出口 IP
    .warp/warp_wg_state.json  WG 线当前绑定的节点
    .warp/h2/channels.json    H2 线 ↔ 节点 ↔ 设备
    <config>.sources.json     节点 → 订阅源（parse_subscription 写的）
    daemon.log[.N]            每条渠道的出口地址 → 「出口段」列（v6 按 /64）
    config.py                 成员表（只认在册渠道）、块表（colo → 机房）

用法：
    python3 tools/channel_report.py                  # 表格（默认 6h）
    python3 tools/channel_report.py --hours 24
    python3 tools/channel_report.py --html /tmp/chan.html
    python3 tools/channel_report.py --json
    python3 tools/channel_report.py --by colo        # colo / source / carrier
    python3 tools/channel_report.py --blocked-ips    # 出口 IP 惩罚期概览
"""
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

# config.py 在 import 期读 .env；手跑不经过 systemd 的 EnvironmentFile。
_env = PROJECT / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import ipv6_segments  # noqa: E402
from asip import ledger_policy  # noqa: E402
from asip.redact import redact_secrets  # noqa: E402
from config import CFG, extra_subscription_urls, subscription_urls  # noqa: E402
from parse_subscription import source_label  # noqa: E402

# verdict 语义：OK 可用；quota/flagged 是出口级的两种拦截；其余是探测自身故障。
BAN_VERDICTS = {"quota", "flagged"}

# 块表没收录的 colo 兜底（BOM 是 2026-11-04 新订阅实测出来的）。
COLO_FALLBACK = {
    'IAD': '华盛顿·达拉斯', 'DUB': '都柏林', 'JNB': '约翰内斯堡', 'GIG': '里约热内卢', 'IST': '伊斯坦布尔', 'SYD': '悉尼',
    "NRT": "东京", "PHX": "凤凰城", "LAX": "洛杉矶", "SJC": "圣何塞",
    "SIN": "新加坡", "TPE": "台北", "HKG": "香港", "ORD": "芝加哥",
    "ICN": "首尔", "SLC": "盐湖城", "PIT": "匹兹堡", "BOM": "孟买",
    "AMS": "阿姆斯特丹", "FRA": "法兰克福", "LHR": "伦敦",
}
# ipv6_segments.colo_of() 里几个值不是落地机房，是地址段批次。
PSEUDO_COLO = {"CF": "CF 直连", "CFB": "CF 直连", "OLD": "旧批次", "UNKNOWN": "未知"}


def _service_of_tag(tag: str) -> str:
    """从 tag 推出 systemd 单元名（与 daemon 的端口/单元契约一致）。

    WHY 需要它：`config.CFG["warp"]["members"]` 里的 WG 线是**按环境变量
    拼出来的**。报表进程若没继承 daemon 的环境（手工跑、cron 里跑），这
    22 条声明就整体缺失 —— 于是它们的单元状态无从查起，`unit_ok` 只能
    默认 True，报表就**不会**印「单元停用（线没跑）」，把「线早就不跑了」
    显示成「一切正常」。

    tag 的命名契约（见 `config.py` 的端口契约）：
      WARP-WG-<N>    → warp-wg-<N>.service
      WARP-H2-<...>  → usque-h2-<N>.service（N 从末尾数字取）
      WARP-CF4[-<x>] → usque-warp[-<x>].service（v4 入口，同上）
      WARP-CF6[-<x>] → usque-warp[-<x>].service（v6 入口，同上）
      WARP-CF-<x>    → usque-warp-<x>.service（旧命名，兼容）
    推不出的一律返回空串 —— 查不到单元名就按「无从查起」处理，不猜。
    """
    m = re.fullmatch(r"WARP-WG-(\d+)", tag)
    if m:
        return f"warp-wg-{m.group(1)}.service"
    m = re.fullmatch(r"WARP-H2-(?:[A-Z]+)-(\d+)", tag)
    if m:
        return f"usque-h2-{m.group(1)}.service"
    m = re.fullmatch(r"WARP-H2-SCAN-(\d+)", tag)
    if m:
        return f"usque-h2-{m.group(1)}.service"
    # 2026-11-13 起 CF 线带入口族标记：WARP-CF4/-4-B..（v4 入口）、
    # WARP-CF6/-6-B..（v6 入口）。后缀为空 = 主单元 usque-warp。
    m = re.fullmatch(r"WARP-CF[46](?:-([A-Za-z0-9]+))?", tag)
    if m:
        suffix = m.group(1)
        return f"usque-warp-{suffix.lower()}.service" if suffix else "usque-warp.service"
    # 旧命名（WARP-CF / WARP-CF-B）：台账与历史日志里还在，保留兼容。
    m = re.fullmatch(r"WARP-CF-([A-Za-z0-9]+)", tag)
    if m:
        return f"usque-warp-{m.group(1).lower()}.service"
    return ""


def _unit_states(tags: dict[str, str]) -> dict[str, bool]:
    """成员 tag → 对应 systemd 单元此刻是否在跑（与 daemon 同一口径）。

    WHY 报表要亲自查：config.py 的 members 是**声明**不是**在跑**。2026-11-09
    本地 usque-warp-c/e/f/g/h/i 六个单元根本没装，状态表里冻着删单元那天的
    quota / 无地址，报表照印就成了「这条线配额耗尽」—— 真实原因是「这条线
    根本不存在」。三种故障的处置完全不同，不能共用一句历史结论。

    查不到（systemctl 缺失/超时）按在跑处理：宁可漏标，也不能把整张表打成
    「全部停用」。
    """
    svcs = sorted({v for v in tags.values() if v})
    if not svcs:
        return {}
    try:
        r = subprocess.run(["systemctl", "is-active", *svcs],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {x: True for x in svcs}
    states = r.stdout.strip().splitlines()
    return {x: (i < len(states) and states[i].strip() == "active")
            for i, x in enumerate(svcs)}


def _tier_disabled() -> set[str]:
    """当前档位下停用的 WG 线 tag（与 daemon._TIER_DISABLED 同一份口径）。

    报表必须标出来：停用的线在日志里还留着半小时前的样本，成功率、ban 率
    全是停用**之前**的 —— 与 `print_table` 里「无样本」那条同一类陷阱，
    不标就会把一份历史数据读成「现在这条线是这个状态」。
    """
    import warp_blocks
    return warp_blocks.tier_disabled()


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def colo_name(colo: str | None) -> str:
    if not colo:
        return "?"
    for b in CFG["warp"]["blocks"]["table"]:
        if b.get("colo") == colo and b.get("loc"):
            return b["loc"]
    return COLO_FALLBACK.get(colo) or PSEUDO_COLO.get(colo, "?")


def _colo_of_ip(ip: str | None) -> str | None:
    """出口 IPv6 → colo。IPv4 没有段指纹，返回 None。"""
    if not ip or ":" not in ip:
        return None
    try:
        return ipv6_segments.colo_of(ipaddress.ip_network(ipv6_segments.net64(ip)))
    except (ValueError, TypeError):
        return None


def colo_ban_rates(window_sec: float, now: float) -> dict[str, tuple[int, int]]:
    """窗口内按 colo 聚合的 (样本, 封禁数)，来自换线历史。只读。

    与 `ipv6_segments.colo_ban_rates` 同口径；单独实现只为「文件不存在 → 空表」，
    不让整个报表挂掉。H2 线拿不到出口地址就没有模型层样本（ban_probes 为空），
    这一列是它们唯一的 ban 信号。
    """
    path = os.environ.get("WARP_IP_HISTORY_FILE_PATH") or \
        str(PROJECT / ".warp_ip_history.jsonl")
    lo = now - float(window_sec)
    out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return {}
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
                ip, t = r["ip"], float(r["t"])
            except (ValueError, KeyError, TypeError):
                continue
            if t < lo or t > now or not isinstance(ip, str):
                continue
            c = _colo_of_ip(ip)
            if not c:
                continue
            d = out[c]
            d[0] += 1
            if r.get("b"):
                d[1] += 1
    return {c: (n, b) for c, (n, b) in out.items()}


# daemon 每次探测 / 重启都会打印该线的出口地址；`·` 是 TTL 命中跳过实测那一行，
# 也带地址。这是**唯一**带渠道归属的出口台账。
_EGRESS_LINE = re.compile(
    r"(?:✓|✗|·)\s+(WARP-[A-Z0-9-]+)\s+(?:重启后出口=|出口\s+)([0-9a-fA-F:.]+)")
_LOG_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def _rotated_logs(log_file: Path) -> list[Path]:
    """`daemon.log` 与 `daemon.log.N`，**新的在前**（N 越大越旧）。"""
    prefix, found = log_file.name, []
    for p in log_file.parent.glob(prefix + "*"):
        suf = p.name[len(prefix):]
        if suf == "":
            rank = 0
        elif suf.startswith(".") and suf[1:].isdigit():
            rank = int(suf[1:])
        else:
            continue
        found.append((rank, p))
    return [p for _, p in sorted(found)]


def _seg_name(net) -> str:
    """`/64` → 完整段名 `2a09:bac5:55f8:18be`（四段全给，不省略）。

    只给第四段（`:18be`）认不出落在哪一批；只给 `bac5:55f8:18be` 又丢了
    第一段。`2a09` 虽然目前所有 WARP 段都一样，但它不是常量保证 ——
    换 CF 账户或换地址族就可能变，省略它就是埋一个会骗人的假设。
    """
    return ":".join(p.lstrip("0") or "0"
                    for p in net.network_address.exploded.split(":")[:4])


_SEGS_CACHE_FILE = Path(CFG.get("log_file", "/tmp/asip.log")).parent / ".chan_segs_cache.json"
_SEGS_CACHE_TTL = 300  # 5 分钟


def channel_segments(log_file: Path | None = None) -> dict[str, dict]:
    """每条渠道落过的出口：`{tag: {"v6": [/64 段名...], "addr": [/128...],
    "v4": [地址...]}}`。

    返回的是**列表**而不是计数：只给个数等于什么都没说，要看的是
    「落过哪几段」，且段名是完整的四段 `2a09:bac5:55f8:18be`。
    `/128` 另外留一份 —— 见下方那条注释。

    **不按时间窗口截断**：段列表是「这条线去过哪些地方」，不是随时间衰减的
    健康指标。限制窗口会让「最近几小时没拿到地址」的线显示 0 段（H2 线在
    11-03 之后长期处于这个状态），看起来像「从没出去过」。

    v6 的 `v6` 按 /64 计 —— 一次抽签得到的是 /64，同 /64 内的不同 /128 不算
    换段；`addr` 按 /128 计；v4 按地址计（v4 没有段的概念，同一个 colo 池里翻牌）。

    WHY 读日志而不是台账：`.warp_ip_history.jsonl` 只有 `ip/t/b/k`，**没有
    渠道归属**（唯一写入点 `daemon.py:_mark_warp_ip_used` 拿到的就是地址）。
    带渠道归属的只有 daemon 打的两类出口行：`出口 <ip> 模型层` / `重启后出口=<ip>`。
    """
    import time as _tc, json as _jc
    try:
        if _SEGS_CACHE_FILE.exists():
            _cached = _jc.loads(_SEGS_CACHE_FILE.read_text())
            if _tc.time() - _cached.get("ts", 0) < _SEGS_CACHE_TTL:
                return _cached["data"]
    except Exception:
        pass
    path = log_file or Path(CFG["log_file"])
    seen: dict[str, dict[str, set]] = defaultdict(
        lambda: {"v6": set(), "addr": set(), "v4": set()})

    def _scan_file(p):
        """扫描单个日志文件，返回 {tag: {v6:set, addr:set, v4:set}}。"""
        local: dict[str, dict[str, set]] = defaultdict(
            lambda: {"v6": set(), "addr": set(), "v4": set()})
        try:
            fh = p.open(encoding="utf-8", errors="replace")
        except OSError:
            return local
        with fh:
            for line in fh:
                # 快速预过滤：不含关键字的行跳过 regex（省 90%+ regex 调用）
                if "出口" not in line:
                    continue
                o = _EGRESS_LINE.search(line)
                if not o:
                    continue
                ip = o.group(2)
                try:
                    addr = ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if addr.version == 6:
                    local[o.group(1)]["v6"].add(
                        _seg_name(ipaddress.ip_network(ip + "/64", strict=False)))
                    local[o.group(1)]["addr"].add(str(addr))
                else:
                    local[o.group(1)]["v4"].add(ip)
        return local

    # 多文件并发读（I/O bound）
    from concurrent.futures import ThreadPoolExecutor, as_completed
    files = list(_rotated_logs(path))
    if len(files) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(files))) as pool:
            futs = {pool.submit(_scan_file, p): p for p in files}
            for fut in as_completed(futs):
                for tag, d in fut.result().items():
                    seen[tag]["v6"].update(d["v6"])
                    seen[tag]["addr"].update(d["addr"])
                    seen[tag]["v4"].update(d["v4"])
    else:
        for p in files:
            for tag, d in _scan_file(p).items():
                seen[tag]["v6"].update(d["v6"])
                seen[tag]["addr"].update(d["addr"])
                seen[tag]["v4"].update(d["v4"])

    result = {tag: {"v6": sorted(v["v6"], key=_seg_sort),
                    "addr": sorted(v["addr"], key=_addr_sort),
                    "v4": sorted(v["v4"])}
              for tag, v in seen.items()}
    try:
        _SEGS_CACHE_FILE.write_text(_jc.dumps({"ts": _tc.time(), "data": result}))
    except Exception:
        pass
    return result


def _addr_sort(ip: str):
    """IPv6 地址按数值排序；解析不了的按字符串垫底。"""
    try:
        return (0, ipaddress.ip_address(ip))
    except ValueError:
        return (1, ip)


def _seg_sort(name: str) -> tuple:
    """段排序：按四段的十六进制值从左到右比，让相邻批次挨在一起。

    纯字符串排序会把 `bac1:19a0:10` 排到 `bac1:9:10` 前面（十六进制当十进制比），
    列出来像乱序 —— 60 段的线尤其明显。解析不了的原样排最后，不让一行怪数据
    毁掉整列。
    """
    try:
        return tuple(int(p, 16) for p in name.split(":"))
    except ValueError:
        return (0xFFFF,) * 4


def _hostport(tag: str) -> str | None:
    """`host:port` ← 节点 tag（`host:port-abbr` 或 `host:port~2-abbr`）。

    订阅每次刷新都可能给同一台机器换 tag（重名时加 `~N`），而台账里绑定的
    还是旧 tag。按 host:port 兜底才认得出「同一个出口换了名字」。
    """
    m = re.match(r"^(.+:\d+)(?:~\d+)?-", tag)
    return m.group(1) if m else None


def _entry(raw) -> dict:
    """sources 台账的一条 → `{sub, label, name}`。

    旧台账是纯字符串（`source_label(url)`），新台账是 dict。两种都要能读：
    升级后第一次刷新前，盘上还是旧的。
    """
    if isinstance(raw, dict):
        return {"sub": str(raw.get("sub") or ""),
                "label": str(raw.get("label") or ""),
                "name": str(raw.get("name") or "")}
    return {"sub": "", "label": str(raw or ""), "name": ""}


def source_index(sources: dict) -> dict[str, dict]:
    """tag → {sub, label, name}，外加 **无歧义** host:port 索引。

    兜底只为 tag 漂移（`~2` → `~3`）而设。但同一台机器上常挂着十几个不同
    名字的节点（`cn.bing.com:443` 一个 host:port 下有 37 个），拿 host:port
    反查会把 A 节点的名字挂到 B 节点上 —— 实测 5 组、77/152 个节点会错。
    所以只有**该 host:port 在台账里唯一**时才建索引；多义的一律视为查不到。
    """
    idx = {tag: _entry(raw) for tag, raw in sources.items()}
    by_hp: dict[str, list[str]] = defaultdict(list)
    for tag in idx:
        hp = _hostport(tag)
        if hp:
            by_hp[hp].append(tag)
    for hp, tags in by_hp.items():
        if len(tags) == 1:
            idx.setdefault(hp, idx[tags[0]])
    return idx


def sub_url_map() -> dict[str, str]:
    """订阅源展示名 → 完整地址（token 已脱敏）。"""
    out: dict[str, str] = {}
    for u in subscription_urls() + extra_subscription_urls():
        out.setdefault(source_label(u), redact_secrets(u))
    return out


def load_channels() -> dict[str, dict]:
    """**在册**渠道 tag → 元信息。状态表里的历史残留不出现。"""
    live = {m["tag"] for m in CFG["warp"]["members"]}
    blockmap = (_read_json(PROJECT / ".warp/block-map.json") or {}).get("nodes") or {}
    out: dict[str, dict] = {}

    for tag, line in ((_read_json(PROJECT / ".warp/warp_wg_state.json") or {})
                      .get("lines") or {}).items():
        if tag not in live:
            continue
        node = line.get("node")
        meta = blockmap.get(node) or {}
        out[tag] = {"node": node, "colo": meta.get("colo"),
                    "carrier": "WG", "family": line.get("family"),
                    "ip": meta.get("egress_ip")}

    for row in _read_json(PROJECT / ".warp/h2/channels.json") or []:
        tag = row.get("member_tag") or row.get("channel")
        if tag not in live:
            continue
        node = row.get("node")
        meta = blockmap.get(node) or {}
        out[tag] = {"node": node, "colo": row.get("colo") or meta.get("colo"),
                    "carrier": "H2", "family": "v4",
                    "device": row.get("device"),
                    "ip": meta.get("egress_ip")}

    # 台账缺行的在册成员照样列出来（carrier 从 kind 推断）：漏掉一条正在跑的
    # 线，报表看起来就像「它不存在」，而不是「它的账没写」。
    for m in CFG["warp"]["members"]:
        tag = m.get("tag")
        if not tag or tag in out:
            continue
        out[tag] = {"node": None, "colo": m.get("colo"),
                    "carrier": {"wg": "WG", "h2": "H2"}.get(
                        m.get("kind") or "usque", "usque"),
                    "family": m.get("family") or
                    ("v6" if m.get("ipv6") else "v4"),
                    "ip": None}
    return out


def _day_start(now: float) -> float:
    """配额日起点（08:00→次日 08:00），与 daemon._quota_day 同口径。

    WHY 08:00：上游配额按 08:00 重置，报表窗口必须跟配额日对齐。
    自然日零点会把凌晨 0-8 点的数据错误归到前一天（2026-11-14 #41）。
    """
    lt = time.localtime(now)
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 8, 0, 0, 0, 0, -1))
    if now < start:
        start -= 86400
    return start


def sources_path() -> Path:
    """`<config>.sources.json` —— 跟随 `SINGBOX_CONFIG`，不写死文件名。

    生产与远端都用同一个默认值，但可移植性回归要求运行期路径由 config 推导；
    写死的话换配置路径后报表会静默读一份不存在的台账，整列变成 `-`。
    """
    return Path(CFG["singbox"]["config_file"] + ".sources.json")


def dormant_tags() -> set[str]:
    """`dormant-outbounds.json` 里的节点 tag —— **okz 的旧入口快照**。

    别按名字读成「已下架的节点」：2026-11-04 实测，这 65 条与 okz 当前订阅按
    (SNI, 协议) 对齐后 63/63 密码相同 —— 机场只是把入口从
    `bilibili-image-cdn.juhazf.cn:<5xxxx>` 换成了 `cn.bing.com:443`，后端没变。
    """
    return {n.get("tag") for n in (_read_json(PROJECT / "dormant-outbounds.json")
                                   or []) if n.get("tag")}


def _expand_ban_probes(samples) -> list[tuple[float, str]]:
    """`ban_probes` 展开成 `[(ts, verdict)]`。

    daemon 侧为了压住状态文件体积改成了**游程** `[起, 止, 条数, 结论]`
    （19k 样本 / 526 KB → 484 段 / 14 KB）。旧的两元组格式仍然认：报表是
    只读工具，不该因为读到昨天写的文件就整张表空掉。
    """
    out: list[tuple[float, str]] = []
    for e in samples or []:
        if not isinstance(e, (list, tuple)) or len(e) < 2:
            continue
        if not isinstance(e[0], (int, float)) or isinstance(e[0], bool):
            continue
        if len(e) >= 4 and isinstance(e[2], int):
            # 段内所有样本同一结论，时间取段尾（保守：宁可少算进窗口）。
            out.extend([(float(e[1]), str(e[3]))] * max(0, int(e[2])))
        else:
            out.append((float(e[0]), str(e[1])))
    return out


def _singbox_outbound_tags() -> set[str]:
    """当前 sing-box 配置里带 tag 的 outbound 集合（读不到返回空集）。

    WHY 需要第二个真相源：`CFG["warp"]["members"]` 里的 WG 线是**按环境变量
    拼出来的**（`config._append_wg_members` 读 `WARP_WG_FAMILIES` /
    `WARP_WG_NODES`）。报表进程若没继承 daemon 的环境（手工跑、
    cron 里跑、systemd 里 `EnvironmentFile` 没带上），这 22 条声明就整体
    缺失 —— 于是**每一条 WG 线都会被误判成「不在探测名单」**。

    sing-box 配置是渲染产物，不依赖环境变量。它里有这条线的 outbound
    就说明这条线**确实在线上**。两个源取并集，判定才不会因为环境缺失而失真。
    """
    path = CFG["singbox"]["config_file"]
    data = _read_json(Path(path)) or {}
    outs = data.get("outbounds") if isinstance(data, dict) else None
    if not isinstance(outs, list):
        return set()
    return {str(o.get("tag")) for o in outs
            if isinstance(o, dict) and o.get("tag")}


def _ip_service_stats(ban_probes: list) -> dict:
    """从 ban_probes 游程提取 per-IP 服务数分布: {max, min, avg, median}。
    每游程 [起, 止, 条数, 结论] 的条数 = 该 IP 在该时段服务的探测次数。"""
    counts = [int(e[2]) for e in ban_probes if isinstance(e, (list, tuple)) and len(e) >= 3 and e[2]]
    if not counts:
        return {"max": 0, "min": 0, "avg": 0, "med": 0}
    counts.sort()
    n = len(counts)
    return {
        "max": counts[-1],
        "min": counts[0],
        "avg": round(sum(counts) / n, 1),
        "med": counts[n // 2] if n % 2 == 1 else round((counts[n//2 - 1] + counts[n//2]) / 2, 1),
    }


def summarize(state: dict, chans: dict, sources: dict, hours: float,
              now: float | None = None, dormant: set[str] | None = None,
              segments: dict[str, dict] | None = None) -> list[dict]:
    """每条渠道一行。`ban_probes` 只留当天，故窗口按当地零点夹一次 ——
    不夹的话「最近 24h」会把今天凌晨那几条样本当成整天的结论。

    `segments` 是 `channel_segments()` 的结果；不传就留空（单测与离线调用
    不必依赖 daemon.log）。"""
    now = time.time() if now is None else now
    # 数据窗口：至少覆盖整个配额日（昨天08:00->今天08:00）。
    # hours 太小时（如4h）会把凌晨数据裁掉导致"数据少"。
    cutoff = max(now - hours * 3600, _day_start(now), now - 24 * 3600)
    urls = sub_url_map()
    idx = source_index(sources)
    colos = colo_ban_rates(hours * 3600, now)
    segs = segments or {}
    declared_of = {m.get("tag"): m.get("service")
                   for m in CFG["warp"]["members"] if m.get("tag")}
    # 声明里没有的线，从 tag 推出单元名补上 —— 否则环境变量缺失时，
    # WG 线既查不到单元状态，`unit_ok` 又默认 True，会被显示成「一切正常」。
    svc_of = dict(declared_of)
    for t in chans:
        if not svc_of.get(t):
            guess = _service_of_tag(t)
            if guess:
                svc_of[t] = guess
    units = _unit_states(svc_of)
    # 「在不在线上」的两个真相源取并集：声明（members，受环境变量影响）
    # 与渲染产物（sing-box outbound，不依赖环境变量）。
    # **不能用 `svc_of`** —— 那是「推出来的单元名」，包含从 tag 猜的，
    # 拿它判「在不在名单」会把所有 WG 线都算成在名单里。
    # **也不能用 `chans` 的键** —— 它是「报表要看的全部已知线」，
    # 包含补位池（WG-23+）与缩容遗留，那些本来就**不该**被探测。
    online_tags = set(declared_of) | _singbox_outbound_tags()
    rows = []
    for tag, meta in chans.items():
        st = state.get(tag) or {}
        samples = [s for s in _expand_ban_probes(st.get("ban_probes"))
                   if s[0] >= cutoff]
        # 「惩罚期短路」观测：daemon 拿到地址后发现该地址已在台账惩罚期，
        # 于是在模型层实测**之前**就 return 了。这些不是 `ban_probes` 的
        # 判据样本（混进去会让渠道永远锁不上），但它们证明这条线**确实
        # 被探过**且结论是 blocked —— 不读它，报表只能印「无样本」。
        blocked_notes = [s for s in _expand_ban_probes(st.get("blocked_notes"))
                         if s[0] >= cutoff]
        ok = sum(1 for _, v in samples if v == "OK")
        ban = sum(1 for _, v in samples if v in BAN_VERDICTS)
        err = len(samples) - ok - ban
        total = len(samples)
        node = meta.get("node")
        # 无绑定的 usque 直连线：落点从最近一次成功出口反推。
        colo = meta.get("colo") or _colo_of_ip(st.get("last_ok_ip"))
        cn, cb = colos.get(colo or "", (0, 0))
        if not node:
            src = {"sub": "-", "label": "-", "name": "-"}
        else:
            src = idx.get(node) or idx.get(_hostport(node) or "") or {
                "sub": "-", "label": "-", "name": "-"}
            if src["label"] == "-" and node in (dormant or set()):
                # 这个 tag 只存在于 `dormant-outbounds.json`。整份文件是 **okz**
                # 的手工快照（2026-11-04 实测 63/63 与 okz 当前订阅密码相同，
                # 只是入口域名换了），所以来源就写 okz，不写「沉睡池」——
                # 后者是内部叫法，读报表的人需要知道是哪家机场。
                # 若将来把别的源的节点也塞进这份文件，这里要跟着改。
                src = {"sub": "okz", "label": "旧入口快照", "name": "-"}
        rows.append({
            "tag": tag,
            "carrier": meta.get("carrier"),
            "family": meta.get("family"),
            "colo": colo or "?",
            "loc": colo_name(colo),
            "node": node or "-",
            "node_name": src["name"] or "-",
            "source": src["sub"] or "-",
            "source_label": src["label"] or "-",
            "source_url": urls.get(src["label"], "-"),
            "egress_ip": st.get("last_ok_ip") or meta.get("ip") or "-",
            "samples": total,
            "ok": ok,
            "ban": ban,
            "err": err,
            "success_rate": (ok / total) if total else None,
            "ban_rate": (ban / total) if total else None,
            "colo_samples": cn,
            "colo_ban": cb,
            "colo_ban_rate": (cb / cn) if cn else None,
            # 这条线**落过的全部**出口段（v6 段名，v4 地址）。**不受 `--hours`
            # 约束** —— 段列表是「这条线去过哪些地方」，不是随时间衰减的指标
            # （见 `channel_segments()`）。不是「池子里有多少段」：池子是机房
            # 共享的，同一时刻所有线在同一个池里抽签；这里说的是这条线自己的覆盖面。
            "ip_svc": _ip_service_stats(st.get("ban_probes") or []),
            "seg_v6": (segs.get(tag) or {}).get("v6") or [],
            # 这条线落过的 /128 个数。和 /64 个数并列才有意义：同样 60 个 /64，
            # 摊着 60 个地址 vs 挤在 1 个 /64 里的 60 个地址，撞车概率差 60 倍。
            "seg_addr": (segs.get(tag) or {}).get("addr") or [],
            "seg_v4": (segs.get(tag) or {}).get("v4") or [],
            # 段指纹反推的落地 colo（`[(colo, /64 个数)]`）。与 `colo` 是两件事：
            # `colo` 是代理节点所在机房，这里说的是出口地址落在哪个机房。
            "colo_fp": _fp_mix((segs.get(tag) or {}).get("v6") or []),
            "held": bool(st.get("ban_hold_until") and st["ban_hold_until"] > now),
            "hold_until": st.get("ban_hold_until") or 0,
            "quota_until": st.get("quota_until") or 0,
            # 窗口内重启次数，不是 `restart_count`。后者是**进程终身累计**
            # （实测 WARP-WG-1 报 38881 次），拿它当健康指标等于把历史总和
            # 印在今天的行上。`restart_hist` 才是带时间戳的那份。
            "restarts": sum(1 for t in (st.get("restart_hist") or [])
                            if isinstance(t, (int, float)) and t >= cutoff),
            "last_verdict": st.get("last_verdict") or "-",
            # 这个结论是不是**属于当前地址**。重启换地址后 `ip_ts` 会晚于
            # `last_probe_ts`，此时 `last_verdict` 说的是上一条地址 ——
            # 两个视图都不能把它当现地址的判定印出来。
            "verdict_stale": bool(
                float(st.get("ip_ts") or 0) > float(st.get("last_probe_ts") or 0) + 1),
            "no_addr_since": st.get("ban_noaddr_since") or 0,
            # 「拿不到出口」的**原因**（daemon 写的人话，见 _EGRESS_HINT）。
            # 只印 `无地址` 的话，端口没监听、节点域名解析失败、隧道被打断
            # 三种故障在报表上完全一样，而处置动作完全不同。
            "egress_error": st.get("egress_error") or "",
            # 单元此刻在不在跑。为 False 时上面那些 quota/无地址/取址失败一律
            # 是停用前冻住的旧值，两个视图都只印这一条。
            "unit_ok": units.get(svc_of.get(tag), True),
            # 「无样本」的成因要分得开。三种完全不同的故障长得一模一样：
            #   ① 从没探过      —— last_probe_ts == 0
            #   ② 探过但没入账 —— last_probe_ts > 0 而窗口内无样本
            #   ③ 探过但取不到地址 —— no_addr_since 有值
            # 不分开的话，处置动作完全不同：① 要去查探测名单，② 是入账漏记
            # 的 bug，③ 查隧道与落地节点。
            "last_probe_ts": st.get("last_probe_ts") or 0,
            # 窗口内「惩罚期短路」次数。>0 表示这条线**探过**且结论是
            # blocked，只是没走到模型层实测 —— 与「没探过」是两回事。
            "blocked_notes": len(blocked_notes),
            # 这条线在不在 daemon 的探测名单。判定用两个源的并集：声明
            # （members）与渲染产物（sing-box outbound）。只用 members 会
            # 在环境变量缺失时把全部 WG 线误判成「不在名单」。
            # 有探测数据(ban_probes/last_probe_ts)的线也算在名单——
            # 有过探测历史 = daemon 知道它、探过它，只是当前渲染台账没覆盖。
            # 「不在探测名单」只给从来没探过、也不在任何清单里的线(2026-11-15)。
            "in_members": tag in online_tags or bool(st.get("last_probe_ts") or st.get("ban_probes")),
            # 报表的基准时刻。窗口外的样本由它裁掉，冻结时长也按它算 ——
            # 用 `time.time()` 自己取会与窗口口径不一致。
            "now": now,
            "window_from": cutoff,
        })
    # 排序移到 print_table（状态算完后再排）—— 判据与显示一致
    rows.sort(key=lambda r: (-(r["success_rate"] or 0), r["colo"] or "", r["source"] or "", r["node_name"] or ""))
    return rows


def _fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _pct(v: float | None) -> str:
    return f"{v * 100:.0f}%" if v is not None else "—"


def _seg32(name: str) -> str:
    """`2a09:bac5:55f8:18be`（/64）→ `2a09:bac5`（/32）。

    /32 是 Cloudflare 的地址批次：`bac1` 与 `bac5` 是两批独立的 anycast 段，
    一个机房在每批里各有一份池子。再往细看（/48）一条线能有 51 个，
    读完什么也没记住；/32 只有 2 个，一眼能比。
    """
    return ":".join(name.split(":")[:2])


def _seg32_groups(r: dict) -> list[tuple[str, int, int, int]]:
    """`[(/32 名, 它下面几个 /48, 几个 /64, 几个 /128)]`，按数值排序。"""
    from collections import Counter
    c48: Counter = Counter()
    c64: Counter = Counter()
    c128: Counter = Counter()
    for s in (r.get("seg_v6") or []):
        p32 = _seg32(s)
        c64[p32] += 1
        c48[(p32, s.rsplit(":", 1)[0])] += 1
    for ip in (r.get("seg_addr") or []):
        c128[_seg32(ip)] += 1
    n48 = Counter(p32 for p32, _ in c48)
    return [(p32, n48[p32], c64[p32], c128[p32])
            for p32 in sorted(c64, key=_seg_sort)]


def _seg_counts(r: dict) -> tuple[int, int, int, int]:
    """`(v6 /32 个数, /48 个数, /64 个数, /128 个数)`。

    三级天然嵌套（/48 是 /64 的父段，/128 落在 /64 里），所以 /48 与 /64
    可以由段名直接数。池子那边不能这么数 —— 见 `_fp_pools`。"""
    v6 = r.get("seg_v6") or []
    return (len({_seg32(s) for s in v6}),
            len({s.rsplit(":", 1)[0] for s in v6}), len(v6),
            len(r.get("seg_addr") or []))


def _merge_segs(rs: list[dict]) -> dict:
    """一组渠道的出口**并集**（去重）。

    段是机房共享的池子：同一个 colo 下多条线抽到的是同一批段，逐条印等于
    把同一份东西印十几遍。实测（2026-11-04）合并后 /48 少 27%~85%、/64
    少 11%~81%（NRT 11 条线 297 个 /48 → 44 个）。合并后每个 colo 只留一份。
    """
    v6: set[str] = set()
    addr: set[str] = set()
    v4: set[str] = set()
    for r in rs:
        v6.update(r.get("seg_v6") or [])
        addr.update(r.get("seg_addr") or [])
        v4.update(r.get("seg_v4") or [])
    return {"seg_v6": sorted(v6, key=_seg_sort),
            "seg_addr": sorted(addr, key=_addr_sort),
            "seg_v4": sorted(v4)}


def _fp_pools(segments: dict[str, dict]) -> dict[str, dict[str, set]]:
    """`{指纹 colo: {"/48": set, "/64": set, "/128": set}}`，全日志合并去重。

    这是**每个机房真正可抽的池子大小**，按 `ipv6_segments.colo_of()` 的
    段指纹归并。三级并列是必需的：NRT 的 /48 和 SIN 一样是 12 个，但 /64
    只有 12 个而 SIN 有 132 —— 只看 /48 会以为两边等宽，而 NRT 的 /128 级
    5 分钟重复率是 36.1%，SIN 只有 0.7%（2026-11-04 18:40 实测）。

    收的是**全日志**（含已下线的 tag），不是当前在册的渠道：池子是机房的
    属性，不会因为某条线退役而变小。只算在册渠道会让池子随成员表缩水，
    读起来像「机房被削掉了一半」。
    """
    pools: dict[str, dict[str, set]] = defaultdict(
        lambda: {"48": set(), "64": set(), "128": set()})
    for v in segments.values():
        for s in v.get("v6") or []:
            # /48 的归属由**它下面的 /64** 决定，不能直接 `colo_of(/48)`：
            # `bac1:7680/76a0/76c0/76e0` 是 PHX 与 SJC 共用的，`colo_of(/48)`
            # 恒返回 PHX，会把 `:28` 那批 SJC 的 /64 记到 PHX 名下、同时让
            # SJC 的 /48 少算 4 个（实测旧口径 SJC 8 个 /48，实际 12 个）。
            # 三级必须**嵌套**（/48 ⊇ /64 ⊇ /128），否则三个数彼此对不上。
            try:
                colo = ipv6_segments.colo_of(
                    ipaddress.ip_network(s + "::/64"))
            except ValueError:
                continue
            pools[colo]["48"].add(s.rsplit(":", 1)[0])
            pools[colo]["64"].add(s)
        for ip in v.get("addr") or []:
            try:
                colo = ipv6_segments.colo_of(
                    ipaddress.ip_network(ip + "/64", strict=False))
            except ValueError:
                continue
            pools[colo]["128"].add(ip)
    return pools


def print_segments(rows: list[dict], segments: dict[str, dict]) -> None:
    """**按声明 colo 分组**，一组一行：出口段、池子、指纹落点、成员渠道。

    三样东西放一起，是因为它们回答的是同一个问题、维度也相同（都是 colo）：
      - **出口段** = 这组线落过哪些 /32 / /48 / /64 / /128；
      - **指纹** = 出口段反推的落地机房分布（与声明 colo 是两件事：WG 线经
        机场节点中转，声明 NRT 的那组线 68% 落在 SIN）；
      - **池** = 指纹归到该机房的三级去重个数。

    合并是必需的，不是省事：同一 colo 的各条线共用同一个池子，段重叠度极
    高（实测 2026-11-04：合并后 /48 少 27%~85%、/64 少 11%~81%）。按渠道
    逐条印，50 多行里真正不同的只有「哪条线在哪个 colo」。

    注意 /48 归属：池子按 /64 的指纹归，`bac1:7680~76e0` 是 PHX/SJC 共用段。
    """
    if not rows:
        print("（没有渠道数据）")
        return
    print("\n出口段明细（按声明 colo 合并；池 = 指纹归到该机房的段数，"
          "/32 行括号内是它下面的 /48、/64 与 /128）")
    print("-" * 100)
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["colo"] or "?"].append(r)
    pools = _fp_pools(segments)
    head = (f"{_pad('声明', 6)} {_pad('渠道', 5, False)} "
            f"{_pad('出口段 32/48/64/128', _SEG_W, False)} "
            f"{_pad('池 48/64/128', 13, False)}  指纹落点（/64 去重，前 5）")
    print(head)
    width = max((_w(c) for c in groups), default=0) + 2
    for colo in sorted(groups):
        rs = groups[colo]
        m = _merge_segs(rs)
        n32, n48, n64, n128 = _seg_counts(m)
        v4 = m["seg_v4"]
        segs = (f"{n32}/{n48}/{n64}/{n128}" if n64 else "—")
        if v4:
            segs += f" +{len(v4)}v"
        pool = pools.get(colo) or {}
        cell = (f"{len(pool['48'])}/{len(pool['64'])}/{len(pool['128'])}"
                if pool else "—")
        fp = _fp_mix(m["seg_v6"])
        total = sum(n for _, n in fp)
        if not total:
            dist = "—"
        else:
            # 只在**主落点**与声明不同时标 ≠。混着几个机房是常态，
            # 按「是否 100% 一致」标会让几乎每行都带 ≠，标记本身就没信息了。
            mark = "" if fp[0][0] == colo else "≠"
            dist = mark + " · ".join(f"{c} {n * 100 // total}%" for c, n in fp[:5])
        print(f"{_pad(colo, width)} {_pad(str(len(rs)), 5, False)} "
              f"{_pad(segs, 20, False)} {_pad(cell, 13, False)}  {dist}")
        if n64:
            detail = "  ".join(f"{p}({a} 个 /48, {b} 个 /64, {c} 个 /128)"
                               for p, a, b, c in _seg32_groups(m))
            print(" " * (width + 1) + detail)
        tags, line = [], " " * (width + 1)
        for r in rs:
            if _w(line) + _w(r["tag"]) + 1 > 100:
                tags.append(line.rstrip())
                line = " " * (width + 1)
            line += r["tag"] + " "
        tags.append(line.rstrip())
        print("\n".join(tags))
    print("\n  池 = 指纹属于该机房的 /48 / /64 / /128 个数（全部渠道合并去重），越大越不容易撞段。")
    print("  ≠  = 主落点不是声明的机房（WG 线经机场节点中转，节点机房≠出口机房是常态）。")


# 表格「出口段」列的宽度。实测最长 `2/51/188/1624 +3v` 是 17 列，
# 留到 20 能容下 5 位数的 /128 与两位数的 v4 计数 —— 宽度不够时
# `_pad` 是**截断**而不是折行，会把 `+3v` 截成 `+3`，读起来像多了一级。
_SEG_W = 20


def _ip_svc_label(r: dict) -> str:
    """单ip服务数: 最大/最小/平均/中位"""
    s = r.get("ip_svc") or {}
    if not s.get("max"):
        return "-"
    return f"{s['max']}/{s['min']}/{s['avg']}/{s['med']}"


def _seg_label(r: dict) -> str:
    """表格里的「出口段」列：`/32//48//64//128` 四级个数，v4 追加 `+Nv`。

    全为 0 时显示 `—` 而不是 `0/0/0/0` —— 会被读成「一段都没有」，
    实际是「日志里没见过它的出口」（新起的线、日志被轮转过）。

    /128 必须带上：`2/25/147/147` 是「每个 /64 只抽到一个地址」，
    `2/25/147/9` 是「147 个地址反复挤在 9 个 /64 里」—— 前三级一模一样。
    """
    n32, n48, n64, n128 = _seg_counts(r)
    n4 = len(r.get("seg_v4") or [])
    parts = []
    if n64:
        parts.append(f"{n32}/{n48}/{n64}/{n128}")
    if n4:
        parts.append(f"+{n4}v")
    return " ".join(parts) or "—"


def _fp_mix(segs_v6: list[str]) -> list[tuple[str, int]]:
    """按**段指纹**把一条渠道的 /64 归到 colo，`[(colo, n)]` 从多到少。

    这是 `ipv6_segments.colo_of()` 的口径：由第三/四段反推落地机房。
    它和渠道自己的 `colo`（代理节点所在机房）是**两件事** —— WG 线经 NRT
    节点中转，出口完全可能落在 SIN。
    """
    import collections
    c: collections.Counter = collections.Counter()
    for s in segs_v6:
        try:
            c[ipv6_segments.colo_of(ipaddress.ip_network(s + "::/64"))] += 1
        except ValueError:
            continue
    return c.most_common()


def _fp_label(r: dict) -> str:
    """表格「指纹」列：段指纹给出的落地 colo，与声明的 colo 不同时标 `≠`。

    混用多个 colo 时只显示占比最大的那个 —— 61 个 /48 摊在 6 个机房里，
    一列放不下；要完整分布看下面的明细表。
    """
    mix = r.get("colo_fp") or []
    if not mix:
        return "—"
    top, _ = mix[0]
    return top if top == r.get("colo") else f"≠{top}"


def _w(s: str) -> int:
    """终端显示宽度：全角/emoji 占 2 列。f-string 的 `:<N` 按字符数补空格，
    中文节点名下每行都会错位。"""
    import unicodedata
    return sum(2 if (unicodedata.east_asian_width(c) in ("W", "F")
                     or ord(c) >= 0x1F000) else 1 for c in str(s))


def _pad(s: str, width: int, left: bool = True) -> str:
    out, used = "", 0
    for ch in str(s):
        w = _w(ch)
        if used + w > width:
            break
        out += ch
        used += w
    return out + " " * (width - used) if left else " " * (width - used) + out


def _probe_age(ts: float, now: float | None = None) -> str:
    """上次探测距今多久（人类可读）。没有探测记录返回 `从未`.

    WHY 要传 `now`：报表的窗口是**调用方给定的**（`summarize(..., now)`），
    单元测试与离线渲染都用固定时刻。拿 `time.time()` 自己算会得到负数
    （测试里的 now 在未来），印出「-5612757秒前探过」这种垃圾。
    """
    if not ts:
        return "从未"
    now = time.time() if now is None else now
    d = now - ts
    if d < 90:
        return f"{int(d)}秒前"
    if d < 5400:
        return f"{int(d / 60)}分钟前"
    if d < 172800:
        return f"{int(d / 3600)}小时前"
    return f"{int(d / 86400)}天前"


def _no_sample_reason(r: dict) -> str:
    """「无样本」的成因标签。四种故障长得一模一样，处置完全不同。

    - `不在探测名单` —— 线不在 daemon 的 `members` 里且从未被探过。
      有 `ban_probes` 或 `last_probe_ts` 的线**不算**「不在探测名单」。
    - `从未探测` —— 在名单里，但从来没探到过（单元没装/没跑/档位停用）。
    - `窗口外样本` —— 被探过且入过账，只是样本在当前时间窗口之外。
    - `惩罚期短路` —— **探过**，拿到地址后发现该地址在台账惩罚期，在模型层
      实测之前就返回了，所以没有判据样本。这是正常的换址逻辑，不是故障。
    - `探测未入账` —— 探过、判了结论，但结论没进 `ban_probes`。这是**入账
      漏记**的 bug。
    - `取不到地址` —— 探过，隧道取不到出口地址，没有可入账的结论。
    """
    last_probe = float(r.get("last_probe_ts") or 0)
    now = r.get("now")
    _has_ban = bool((r.get("ip_svc") or {}).get("max"))
    if not last_probe:
        if not r.get("in_members", True):
            if _has_ban:
                return "窗口外样本"
            return "不在探测名单"
        return "从未探测"
    n = int(r.get("blocked_notes") or 0)
    if n > 0:
        return "quota（惩罚期短路）"
    if r.get("no_addr_since"):
        return "隧道中断"
    if _has_ban:
        return "窗口外样本"
    return "探测未入账"


def _frozen_verdict_label(r: dict) -> str:
    """单元没在跑时，把「冻住的结论」和「冻结多久」一起印出来。

    WHY 必须印：状态台账里的 `last_verdict` 是**最后一次观测**的结论。
    单元停了之后它再也不会更新，报表若不标明，读者会把几小时前的结论
    当成「现在的状态」。实测 2026-11-11：12 条 WG 线单元 disabled，
    `last_verdict=blocked` 冻了 26 小时 —— 印出来才看得出这是**冻结值**
    而不是刚刚发生的故障。
    """
    v = r.get("last_verdict") or "-"
    age = _probe_age(float(r.get("last_probe_ts") or 0), r.get("now"))
    if v == "-":
        return "（无历史结论）"
    return f"（{v}·{age}冻结）"


def _parse_node(node: str) -> tuple[str, int]:
    """解析 node 字符串为 (host, port)。
    格式: host:port / host:port~N / host:port~N-suffix / host:port-suffix。
    用字符串处理而非 regex——regex 非贪婪+可选端口对带数字后缀（-hkg2）会误匹配。
    """
    s = (node or "").strip()
    if not s or s == "-":
        return ("", 0)
    # 去掉尾部后缀（-hkg / -vl / -hkg2 等）：最后一个 - 后跟字母数字
    import re as _re
    s = _re.sub(r'-[a-z0-9]+$', '', s)
    # 去掉 ~N 段
    s = _re.sub(r'~\d+$', '', s)
    # 分离 host:port（从右侧拆，host 可能含冒号如 IPv6）
    if ':' in s:
        _h, _p = s.rsplit(':', 1)
        if _p.isdigit():
            return (_h, int(_p))
    return (s, 443)


def _measure_latency(node: str) -> str:
    """TCP 连接测速入口节点延迟。返回 "XXms" 或 "不可达"。

    DNS 轮询节点会解析出多个 IP、其中部分是死的（实测 pq.aws65.yydjc.top
    4 个 IP 中 2 个超时）——必须遍历全部解析地址，任一连通即返回，
    只测第一个会把活节点报成「不可达」（2026-11-15）。
    """
    import socket, time as _t
    host, port = _parse_node(node)
    if not host:
        return "—"
    try:
        # DNS 解析（IP 直连跳过）—— 包一层线程超时防 getaddrinfo 卡死
        try:
            import ipaddress
            ipaddress.ip_address(host)
            addrs = [host]  # 已是 IP，跳过 DNS
        except ValueError:
            from concurrent.futures import ThreadPoolExecutor as _TPE
            with _TPE(max_workers=1) as _dns_pool:
                _dns_fut = _dns_pool.submit(
                    socket.getaddrinfo, host, port,
                    socket.AF_UNSPEC, socket.SOCK_STREAM)
                try:
                    infos = _dns_fut.result(timeout=2.0)
                except Exception:
                    return "不可达"
            seen: set[str] = set()
            addrs = []
            for ai in infos:
                a = ai[4][0]
                if a not in seen:
                    seen.add(a)
                    addrs.append(a)
        # 并行竞速全部地址（happy-eyeballs）：任一连通即返回。
        # 逐个串行不行——DNS 轮询的死 IP 会吃满超时拖慢/拖垮判定，
        # 活 IP 偶尔抖动 >1.2s 时前 3 个全失败就误报「不可达」（2026-11-15）。
        from concurrent.futures import ThreadPoolExecutor as _TPE2
        from concurrent.futures import as_completed as _ac

        def _try_one(a: str):
            s = socket.socket(socket.AF_INET6 if ':' in a else socket.AF_INET,
                              socket.SOCK_STREAM)
            s.settimeout(2.0)
            t0 = _t.monotonic()
            try:
                s.connect((a, port))
                return (_t.monotonic() - t0) * 1000
            finally:
                try:
                    s.close()
                except Exception:
                    pass

        pool = _TPE2(max_workers=max(1, len(addrs)))
        try:
            futs = {pool.submit(_try_one, a) for a in addrs}
            for f in _ac(futs, timeout=3.0):
                try:
                    dt = f.result()
                except Exception:
                    continue  # 这个地址失败，等下一个
                return f"{dt:.0f}ms"
        except Exception:
            pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return "不可达"
    except Exception:
        return "不可达"


def _measure_latency_batch(nodes: list[str]) -> dict[str, str]:
    """批量测速（ThreadPool 并发，1s 超时）→ {node: latency_str}。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FTError
    import time as _t
    result = {}
    deadline = _t.monotonic() + 6.0  # 全局 6s 上限（DNS 需要余量）
    with ThreadPoolExecutor(max_workers=50) as pool:  # 50 并发：DNS 友好，不压垮 resolver
        futs = {pool.submit(_measure_latency, n): n for n in set(nodes) if n and n != "-"}
        try:
            for f in as_completed(futs, timeout=6.0):
                n = futs[f]
                try:
                    result[n] = f.result(timeout=0.1)
                except Exception:
                    result[n] = "不可达"
        except (FTError, TimeoutError):
            pass  # 超时的 future 留空
        finally:
            for f, n in futs.items():
                if n not in result:
                    result[n] = "不可达" if not f.done() else "—"
                    f.cancel()
    for n in nodes:
        if n not in result:
            result[n] = "—"
    return result


def _measure_tunnel_latency(socks_port: int) -> str:
    """通过 SOCKS5 代理测隧道出口延迟。隧道不通 -> 不可达。"""
    import socket, time as _t
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        t0 = _t.monotonic()
        s.connect(("127.0.0.1", int(socks_port)))
        s.sendall(b"\x05\x01\x00")
        resp = s.recv(2)
        if len(resp) < 2 or resp[0] != 5 or resp[1] != 0:
            s.close(); return "不可达"
        s.sendall(b"\x05\x01\x00\x01\x01\x01\x01\x01\x01\xbb")
        resp = s.recv(10)
        if len(resp) < 2 or resp[1] != 0:
            s.close(); return "不可达"
        dt = (_t.monotonic() - t0) * 1000
        s.close()
        return f"{dt:.0f}ms"
    except (socket.timeout, OSError, ConnectionRefusedError):
        return "不可达"
    except Exception:
        return "不可达"


def _measure_tunnel_latency_batch(ports: list[int]) -> dict[int, str]:
    """批量测隧道出口延迟。{socks_port: latency_str}"""
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FTError
    result = {}
    valid = list(set(p for p in ports if p and p > 0))
    if not valid:
        return {}
    with ThreadPoolExecutor(max_workers=50) as pool:
        futs = {pool.submit(_measure_tunnel_latency, p): p for p in valid}
        try:
            for f in as_completed(futs, timeout=6.0):
                p = futs[f]
                try:
                    result[p] = f.result(timeout=0.1)
                except Exception:
                    result[p] = "不可达"
        except (FTError, TimeoutError):
            pass
        finally:
            for f, p in futs.items():
                if p not in result:
                    result[p] = "不可达" if not f.done() else "—"
                    f.cancel()
    return result


def _load_sock_ports() -> dict[str, int]:
    """从 channels.json 和 warp_wg_state.json 加载 tag -> socks_port。"""
    import json
    out = {}
    try:
        for row in _read_json(PROJECT / ".warp/h2/channels.json") or []:
            tag = row.get("member_tag") or row.get("channel")
            sp = row.get("socks_port")
            if tag and sp:
                out[tag] = int(sp)
    except Exception:
        pass
    try:
        for tag, line in ((_read_json(PROJECT / ".warp/warp_wg_state.json") or {})
                          .get("lines") or {}).items():
            sp = line.get("socks_port")
            if sp:
                out[tag] = int(sp)
    except Exception:
        pass
    return out


def _load_slot_health() -> dict:
    """加载闸道槽位健康状态（与 daemon _pick_healthy_slot 同源）。"""
    import json, os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", ".slot_health.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        # tag -> {status, verdict, slot_name} 反向映射
        out = {}
        for slot_name, h in d.items():
            tag = h.get("tag")
            if tag:
                out[tag] = {
                    "slot": slot_name,
                    "status": h.get("status", "unknown"),
                    "verdict": h.get("verdict"),
                }
        return out
    except Exception:
        return {}


def _display_state(r: dict, slot_health: dict, latency_map: dict, tunnel_latency_map: dict = None) -> tuple:
    """计算显示状态 + 排序优先级。返回 (state_str, sort_pri)。
    sort_pri: OK=0, quota=1, 中间态=2, 隧道中断=3, 单元停用=4

    红线：延迟不可达的线**不允许显示 OK**——入口 TCP 都连不上，隧道
    必然不通，探针历史 OK 是旧结论（线当时还活着），不能冒充现状。
    """
    if not r.get("unit_ok", True):
        return ("单元停用", 4)
    # 实时隧道健康覆盖：隧道出口不可达 = 隧道中断（不管探针历史结论）
    _tlat = (tunnel_latency_map or {}).get(r.get("node", "-"), "")
    if _tlat == "不可达":
        return ("隧道中断", 3)
    if r["held"]:
        return ("封禁", 1)
    if r["quota_until"] > time.time():
        return ("quota", 1)
    _sh = slot_health.get(r["tag"])
    if _sh and _sh["status"] == "ok":
        _v = r['last_verdict']
        if _v == "blocked":
            _ban = float(r.get('quota_until', 0) or 0)
            _v = "待验" if _ban <= time.time() else "quota"
        if _v not in ("OK", "quota", "flagged"):
            _v = "待验" if _v in ("unknown", "") else _v
        pri = 0 if _v == "OK" else (1 if _v in ("quota", "flagged") else 2)
        return (_v, pri)
    if r["no_addr_since"] and not r["samples"]:
        return ("隧道中断", 3)
    if r.get("egress_error") and not r["samples"]:
        return ("隧道中断", 3)
    if r["samples"]:
        _v = r['last_verdict']
        if _v == "blocked":
            _ban = float(r.get('quota_until', 0) or 0)
            _v = "待验" if _ban <= time.time() else "quota"
        pri = 0 if _v == "OK" else (1 if _v in ("quota", "flagged") else 2)
        return (_v, pri)
    if r.get("in_members", True) or r.get("last_probe_ts"):
        return ("无样本", 2)
    return ("不在探测名单", 3)


def print_table(rows: list[dict], hours: float) -> None:
    if not rows:
        print("（没有渠道数据）")
        return
    window_from = rows[0]["window_from"]
    if window_from > time.time() - hours * 3600 + 60:
        print(f"注：渠道样本只保留当天，实际窗口从 {_fmt_ts(window_from)} 起"
              f"（< {hours:g}h）\n")
    # 入口延迟：测机场节点 TCP（所有线都测）
    latency_map = _measure_latency_batch([r.get("node", "-") for r in rows])
    # 隧道出口延迟：通过 SOCKS 代理测（隧道不通 -> 不可达）
    _sock_ports = _load_sock_ports()
    tunnel_map = _measure_tunnel_latency_batch(list(_sock_ports.values()))
    slot_health = _load_slot_health()
    # 排序：状态优先级最高 -> 成功率 -> colo -> 订阅 -> 节点名
    # 延迟不可达排最后（同组内保持原有顺序）
    def _sort_key(r):
        _s, _pri = _display_state(r, slot_health, latency_map, tunnel_map)
        _sp = _sock_ports.get(r["tag"])
        _lat_bad = 1 if tunnel_map.get(_sp, "不可达") == "不可达" else 0
        return (_pri, _lat_bad, -(r["success_rate"] or 0), r["colo"] or "", r["source"] or "", r["node_name"] or "")
    rows.sort(key=_sort_key)
    head = (f"{_pad('渠道', 18)} {_pad('承载', 5)} {_pad('colo', 4)} "
            f"{_pad('机房', 8)} {_pad('成功率', 6, False)} "
            f"{_pad('ban', 5, False)} {_pad('成/败', 7, False)} "
            f"{_pad('/32,/48,/64,/128', _SEG_W, False)} "
            f"{_pad('单ip服务', 12, False)} "
            f"{_pad('重启', 6, False)}  "
            f"{_pad('订阅', 8)} {_pad('节点名', 30)} {_pad('入口延迟', 7, False)} {_pad('隧道延迟', 7, False)} 状态")
    print(head)
    print("-" * (_w(head) + 40))
    tier_off = _tier_disabled()
    for r in rows:
        # 状态列只放线的当前状态（统一层级），不混流程解释
        state = "-"
        notes = []
        _sp = _sock_ports.get(r["tag"])
        _lat_entry = latency_map.get(r.get("node", "-"), "—")
        _lat_tunnel = tunnel_map.get(_sp, "—") if _sp else "—"
        if r["tag"] in tier_off:
            notes.append("档位停用")
        if not r.get("unit_ok", True):
            # 主因，且**独占**：单元没在跑时，状态表里的 quota / 无地址 /
            # 取址失败全是停用前冻住的旧值，同时印出来会让人以为是配额问题。
            #
            # 但**必须同时印出 last_verdict**：这条线判过什么、冻结了多久，
            # 是「配置裂了」和「线坏了」的唯一区别。实测 2026-11-11：12 条
            # WG 线的单元是 disabled，状态冻在 `blocked` 上 26 小时，
            # 报表只印「档位停用 单元停用（线没跑）」—— 把「没人探它」
            # 显示成「它坏了」，正是主人说的「原因标错」。
            state = f"单元停用{_frozen_verdict_label(r)}"
        elif _lat_tunnel == "不可达":
            state = "隧道中断"
        elif r["held"]:
            state = f"封禁至{_fmt_ts(r['hold_until'])}"
        elif r["quota_until"] > time.time():
            state = f"封禁至{_fmt_ts(r['quota_until'])}"
        elif slot_health.get(r["tag"], {}).get("status") == "ok":
            # 活跃槽位且闸道认可。OK 只来自探针实测（last_verdict=="OK"），
            # 槽位健康只影响是否信旧结论——slot ok + 探针判 OK = OK；
            # slot ok + 探针判 unknown/quota = 显示探针结论（不冒充 OK）。
            _v = r['last_verdict']
            if _v == "blocked":
                _ban_until = float(r.get('quota_until', 0) or 0)
                _v = "待验" if _ban_until <= time.time() else "quota"
            # 红线：非探针实测 OK 不显示 OK（slot ok 不等于探测 OK）
            if _v not in ("OK", "quota", "flagged"):
                _v = "待验" if _v in ("unknown", "") else _v
            _ts = _fmt_ts(float(r.get("last_probe_ts") or 0)) if r.get("last_probe_ts") else "?"
            _probe_age_min = (time.time() - float(r.get("last_probe_ts") or 0)) / 60
            _stale = r["verdict_stale"] or _probe_age_min > 30
            state = f"{_v}（旧地址·{_ts}判）" if _stale else f"{_v}（{_ts}判）"
        elif r["no_addr_since"] and not r["samples"]:
            # 无样本 + no_addr = 隧道拿不到地址
            state = "隧道中断"
        elif r.get("egress_error") and not r["samples"]:
            state = f"隧道中断:{r['egress_error']}"
        else:
            # 窗口内一条样本都没有 → `last_verdict` 是**旧的**，不能当成
            # 现在的结论印出来。WARP-CF-C/G/H/I 的单元 2026-10-28 缩容时
            # 就删了（`is-enabled` 返回 not-found），prober 早已用
            # `_warp_member_unit_running` 过滤掉，状态表里那句 `unknown`
            # 是删单元当天冻住的 23 万次失败留下的。照印会读成「现在探测失败」。
            #
            # 「无样本」不是一种故障，是三种，分开标：
            #   从没探过      → 查探测名单（线根本不在名单里）
            #   探过但没入账  → 入账漏记的 bug（判了 blocked 却没进 ban_probes）
            #   探过但无地址  → 隧道取不到出口
            if r["samples"]:
                # 实时隧道健康信号优先于历史 verdict：
                # 入口不可达 / no_addr_since -> 隧道当前拿不到 IP -> 隧道中断
                _lat_now = latency_map.get(r.get("node", "-"), "—")
                _sh = slot_health.get(r["tag"])
                # 活跃槽位上的线：槽位健康是闸道判据，优先于 no_addr_since
                if _sh and _sh["status"] == "ok":
                    _v = r['last_verdict']
                    if _v == "blocked":
                        _ban_until = float(r.get('quota_until', 0) or 0)
                        _v = "待验" if _ban_until <= time.time() else "quota"
                    _ts = _fmt_ts(float(r.get("last_probe_ts") or 0)) if r.get("last_probe_ts") else "?"
                    _probe_age_min = (time.time() - float(r.get("last_probe_ts") or 0)) / 60
                    _stale = r["verdict_stale"] or _probe_age_min > 30
                    state = f"{_v}（旧地址·{_ts}判）" if _stale else f"{_v}（{_ts}判）"
                elif _sh and _sh["status"] != "ok":
                    # 红线：不从槽位 verdict 取 OK——只显示槽位状态
                    _sv = _sh.get("verdict") or ""
                    state = f"槽位{_sh['status']}" + (f"({ _sv})" if _sv and _sv != "OK" else "")
                else:
                    # 备胎线（不在活跃槽位）：以探测 verdict 为准
                    _v = r['last_verdict']
                    if _v == "blocked":
                        _ban_until = float(r.get('quota_until', 0) or 0)
                        _v = "待验" if _ban_until <= time.time() else "quota"
                    _ts = _fmt_ts(float(r.get("last_probe_ts") or 0)) if r.get("last_probe_ts") else "?"
                    _probe_age_min = (time.time() - float(r.get("last_probe_ts") or 0)) / 60
                    _stale = r["verdict_stale"] or _probe_age_min > 30
                    state = f"{_v}（旧地址·{_ts}判）" if _stale else f"{_v}（{_ts}判）"
            elif r.get("in_members", True) or r.get("last_probe_ts"):
                state = _no_sample_reason(r)
            else:
                state = "不在探测名单"
        okfail = f"{r['ok']}/{r['ban'] + r['err']}"
        _lat_entry_style = "不可达" if _lat_entry == "不可达" else _lat_entry
        _lat_tunnel_style = "不可达" if _lat_tunnel == "不可达" else _lat_tunnel
        _note = (' ' + ' '.join(notes)) if notes else ''
        print(f"{_pad(r['tag'], 18)} {_pad(r['carrier'], 5)} "
              f"{_pad(r['colo'], 4)} {_pad(r['loc'], 8)} "
              f"{_pad(_pct(r['success_rate']), 6, False)} "
              f"{_pad(_pct(r['ban_rate']), 5, False)} "
              f"{_pad(okfail, 7, False)} "
              f"{_pad(_seg_label(r), _SEG_W, False)} "
              f"{_pad(_ip_svc_label(r), 12, False)} "
              f"{_pad(str(r['restarts']), 6, False)}  "
              f"{_pad(r['source'], 8)} {_pad(r['node_name'], 30)} "
              f"{_pad(_lat_entry_style, 7, False)} {_pad(_lat_tunnel_style, 7, False)} "
              f"{state}{_note}")
    print("-" * (_w(head) + 40))
    tot = sum(r["samples"] for r in rows)
    ok = sum(r["ok"] for r in rows)
    ban = sum(r["ban"] for r in rows)
    print(f"合计 {len(rows)} 条渠道 · 样本 {tot} · 成功 {ok} · ban {ban} · "
          f"成功率 {_pct(ok / tot if tot else None)} · "
          f"锁定中 {sum(1 for r in rows if r['held'])}")


def print_grouped(rows: list[dict], by: str) -> None:
    key = {"colo": lambda r: (r["colo"], r["loc"]),
           "source": lambda r: (r["source"],),
           "carrier": lambda r: (r["carrier"],)}[by]
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    print(f"\n按 {by} 汇总")
    print("-" * 92)
    print(f"{'分组':<44} {'渠道':>4} {'样本':>6} {'成功':>6} {'ban':>6} "
          f"{'成功率':>7} {'ban率':>6}")
    for k, rs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        tot = sum(r["samples"] for r in rs)
        ok = sum(r["ok"] for r in rs)
        ban = sum(r["ban"] for r in rs)
        print(f"{' / '.join(x for x in k if x)[:44]:<44} {len(rs):>4} {tot:>6} "
              f"{ok:>6} {ban:>6} {_pct(ok / tot if tot else None):>7} "
              f"{_pct(ban / tot if tot else None):>6}")


# 状态色取 dataviz 参考盘的 status 档（固定，不随主题变），且**永远带数字**：
# 颜色不是唯一编码，色盲/打印/强制配色下都能读。
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"


def _status(rate: float | None, *, invert: bool) -> tuple[str, str]:
    if rate is None:
        return "—", "var(--muted)"
    v = rate * 100
    if invert:                      # ban 率：越低越好
        c = GOOD if v <= 10 else (WARN if v <= 50 else CRIT)
    else:                           # 成功率：越高越好
        c = GOOD if v >= 90 else (WARN if v >= 50 else CRIT)
    return f"{v:.0f}%", c


_TMPL = """<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ASIP 渠道健康</title>
<style>
:root{{color-scheme:light;--bg:#fcfcfb;--card:#fff;--line:#e4e3df;--fg:#0b0b0b;
--muted:#52514e;--track:#efeeea;--hover:#f5f4f1}}
@media (prefers-color-scheme:dark){{:root{{color-scheme:dark;--bg:#1a1a19;
--card:#222221;--line:#383835;--fg:#f0efec;--muted:#c3c2b7;--track:#2e2e2c;
--hover:#2a2a28}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI","PingFang SC",sans-serif}}
h1{{font-size:20px;margin:0 0 4px}}
.meta{{color:var(--muted);font-size:12px;margin-bottom:18px}}
.cards{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:10px 16px;min-width:112px}}
.card .k{{color:var(--muted);font-size:11px;letter-spacing:.05em}}
.card .v{{font-size:22px;font-variant-numeric:tabular-nums;margin-top:2px}}
.wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:8px;
background:var(--card)}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;font-size:11px;color:var(--muted);font-weight:600;
letter-spacing:.04em;padding:10px 12px;border-bottom:1px solid var(--line);
white-space:nowrap;background:var(--card);position:sticky;top:0}}
td{{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:middle;
font-variant-numeric:tabular-nums}}
tr:last-child td{{border-bottom:none}}
tbody tr:hover td{{background:var(--hover)}}
.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}}
.dim{{color:var(--muted)}}
.url{{max-width:330px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
display:block}}
.bar{{display:flex;align-items:center;gap:7px;min-width:98px}}
.track{{flex:1;height:6px;border-radius:3px;background:var(--track);
overflow:hidden}}
.track>i{{display:block;height:100%;border-radius:3px}}
.pill{{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;
border:1px solid var(--line);color:var(--muted);white-space:nowrap}}
.seg{{max-width:420px}}
.chip{{display:inline-block;margin:1px 3px 1px 0;padding:0 5px;border-radius:4px;
background:var(--track);font-size:11px;white-space:nowrap}}
.pill.hold{{border-color:{crit};color:{crit}}}
.pill.quot{{border-color:{warn};color:{warn}}}
tr.held td{{background:rgba(208,59,59,.07)}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px;
margin:12px 2px 0}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:2px;
margin-right:5px;vertical-align:middle}}
footer{{color:var(--muted);font-size:11px;margin-top:14px;line-height:1.7}}
</style>
<h1>ASIP 渠道健康</h1>
<div class="meta">生成 {gen} · 窗口 {hours:g}h（渠道样本自 {wf} 起）· 只读本机台账</div>
<div class="cards">{cards}</div>
<div class="wrap"><table><thead><tr>
<th>渠道</th><th>承载</th><th>colo</th><th>机房</th><th>节点名（机场）</th>
<th>节点地址</th><th>订阅</th><th>订阅地址</th><th>出口 IP</th><th>成功率</th>
<th>ban 率</th><th>成功/失败</th><th>colo ban</th>
<th title="v6 出口段四级个数：/32 / /48 / /64 / /128（悬停看 /32 批次）。/128 与 /64 并肩看才分得清「地址挤在同一段」和「摊在各个段」。v4 另计">出口段 /32·/48·/64·/128</th>
<th title="段指纹反推的落地机房。≠ 表示与声明的 colo 不同（WG 线经机场节点中转时是常态）">落点</th>
<th>重启</th><th>状态</th>
</tr></thead><tbody>{rows}</tbody></table></div>
<div class="legend">
<span><i class="dot" style="background:{good}"></i>成功率 ≥90% / ban ≤10%</span>
<span><i class="dot" style="background:{warn}"></i>成功率 ≥50% / ban ≤50%</span>
<span><i class="dot" style="background:{crit}"></i>成功率 &lt;50% / ban &gt;50%</span>
</div>
<footer>成功率/ban 率按该渠道窗口内的模型层实测样本计（daemon 的 ban_probes，
只保留当天）。H2 线拿不到出口地址时不会产生样本，看 colo ban 列 —— 那是整个
机房窗口内全部出口的封禁率。订阅地址已脱敏（token 显示为 ***）。</footer>
"""


def render_html(rows: list[dict], hours: float, out: Path) -> None:
    tot = sum(r["samples"] for r in rows)
    ok = sum(r["ok"] for r in rows)
    ban = sum(r["ban"] for r in rows)
    cards = [("渠道", len(rows)), ("样本", tot), ("成功", ok), ("ban", ban),
             ("全局成功率", _pct(ok / tot if tot else None)),
             ("全局 ban 率", _pct(ban / tot if tot else None)),
             ("锁定中", sum(1 for r in rows if r["held"]))]
    card_html = "".join(
        f'<div class="card"><div class="k">{html.escape(str(k))}</div>'
        f'<div class="v">{html.escape(str(v))}</div></div>' for k, v in cards)

    def bar(rate, invert):
        txt, color = _status(rate, invert=invert)
        if rate is None:
            return f'<span class="dim">{txt}</span>'
        # 0 也要有一格可见宽度，否则「0%」和「没有数据」在视觉上一样。
        w = min(100.0, max(1.5, rate * 100))
        return (f'<span class="bar"><span class="track"><i style="width:{w:.0f}%;'
                f'background:{color}"></i></span>'
                f'<span class="mono">{txt}</span></span>')

    body = []
    for r in rows:
        pills = []
        if not r.get("unit_ok", True):
            # 与 CLI 表同一口径：单元没在跑时，`last_verdict` 是**冻结值**，
            # 必须连同冻结时长一起印出来，否则读者会把几小时前的结论当成
            # 现在的状态（实测 12 条 WG 线的 blocked 冻了 26 小时）。
            # 零样本时还要再标一次成因 —— 「单元停用」说的是线不跑了，
            # 「无样本·不在探测名单」说的是**为什么没人探它**，两回事。
            pills.append('<span class="pill hold">单元停用（线没跑）</span>')
            pills.append('<span class="pill">'
                         f'{html.escape(_frozen_verdict_label(r))}</span>')
            if not r["samples"]:
                pills.append('<span class="pill">'
                             f'{html.escape(_no_sample_reason(r))}</span>')
        elif r["held"]:
            pills.append(f'<span class="pill hold">封禁至 {_fmt_ts(r["hold_until"])}</span>')
        elif r["quota_until"] > time.time():
            pills.append(f'<span class="pill quot">配额至 {_fmt_ts(r["quota_until"])}</span>')
        elif r["no_addr_since"]:
            pills.append('<span class="pill quot">无出口地址</span>')
        else:
            # 与 CLI 表同一口径（见 `print_table`）：窗口内没有样本时
            # `last_verdict` 是**旧值**，不能当成当前结论印出来。HTML 之前
            # 漏了这层保护，同一份数据两个视图给出不同结论。
            # 「无样本」进一步拆成三种成因（见 `_no_sample_reason`）。
            txt = (r["last_verdict"] if r["samples"]
                   else _no_sample_reason(r))
            # 红线：blocked 解禁后不是 OK，是「待验」（2026-11-15）。
            if txt == "blocked":
                _ban = float(r.get('quota_until', 0) or 0)
                txt = "待验" if _ban <= time.time() else "quota"
            pills.append(f'<span class="pill">'
                         f'{html.escape(txt)}'
                         f'</span>')
        cls = ' class="held"' if r["held"] else ""
        body.append(
            f'<tr{cls}><td class="mono">{html.escape(r["tag"])}</td>'
            f'<td>{html.escape(r["carrier"] or "-")}</td>'
            f'<td class="mono">{html.escape(r["colo"])}</td>'
            f'<td>{html.escape(r["loc"])}</td>'
            f'<td class="dim" title="{html.escape(r["node"])}">'
            f'{html.escape(r["node_name"][:40])}</td>'
            f'<td class="mono dim">{html.escape(r["node"][:44])}</td>'
            f'<td>{html.escape(r["source"])}</td>'
            f'<td class="mono dim"><span class="url" title="'
            f'{html.escape(r["source_url"])}">{html.escape(r["source_url"])}</span></td>'
            f'<td class="mono dim">{html.escape(r["egress_ip"][:30])}</td>'
            f'<td>{bar(r["success_rate"], False)}</td>'
            f'<td>{bar(r["ban_rate"], True)}</td>'
            f'<td class="mono">{r["ok"]}/{r["ban"] + r["err"]}</td>'
            f'<td class="mono dim">{_pct(r["colo_ban_rate"])}</td>'
            f'<td class="mono dim seg" title="'
            f'{html.escape(" ".join(p for p, *_ in _seg32_groups(r)))}">'
            f'{html.escape(_seg_label(r))}</td>'
            f'<td class="mono dim">{html.escape(_fp_label(r))}</td>'
            f'<td class="mono dim">{r["restarts"]}</td>'
            f'<td>{" ".join(pills)}</td></tr>')

    out.write_text(_TMPL.format(
        gen=time.strftime("%Y-%m-%d %H:%M:%S"), hours=hours,
        wf=_fmt_ts(rows[0]["window_from"]) if rows else "-",
        cards=card_html, rows="\n".join(body),
        good=GOOD, warn=WARN, crit=CRIT))
    print(f"→ {out}（{len(rows)} 条渠道，浏览器打开）")


def blocked_ip_summary(now: float | None = None,
                       path: Path | None = None) -> dict:
    """出口 IP 惩罚期概览（只读）：区分「台账标记」与「当前仍判 blocked」。

    口径与 `daemon._warp_ip_is_blocked` 一致：只有 blocked=True **且**剩余
    惩罚期 > 0 才算「生效中」。直接数 blocked=True 会把已过惩罚期、此刻正作为
    次级优质复用的地址也算成被封 —— 实测 4.2 万条标记里约 1.6 万条是这种
    残留，按它判断「池子还有多少能用的 IP」会严重偏保守。

    释放时刻计算委托 `asip.ledger_policy`（D-1 单一事实源），不再本地重算。
    """
    now = time.time() if now is None else float(now)
    w = CFG["warp"]
    ttl = ledger_policy.make_ttl(w)
    raw = _read_json(path or PROJECT / ".warp_ip_used.json") or {}
    used = raw.get("used") if isinstance(raw, dict) and isinstance(
        raw.get("used"), dict) else raw
    if not isinstance(used, dict):
        used = {}
    out = {"now": now, "total": len(used), "marked": 0, "active": 0,
           "expired": 0, "ban_until_active": 0,
           "earliest_release": None, "latest_release": None,
           "by_kind": defaultdict(int)}
    releases: list[float] = []
    for entry in used.values():
        if not isinstance(entry, dict) or not entry.get("blocked"):
            continue
        out["marked"] += 1
        stamps = ledger_policy.block_stamps(entry)
        release = ledger_policy.block_release_ts(entry, ttl)
        out["by_kind"]["+".join(sorted(stamps))] += 1
        if release > now:
            out["active"] += 1
            releases.append(release)
            bu, _bk = ledger_policy.parse_ban_until(entry)
            if bu and bu > now and release == bu:
                out["ban_until_active"] += 1
        else:
            out["expired"] += 1
    out["by_kind"] = dict(sorted(out["by_kind"].items()))
    if releases:
        out["earliest_release"] = min(releases)
        out["latest_release"] = max(releases)
    return out


def print_blocked_ips(s: dict) -> None:
    """终端输出：把「标记数」和「实际还封着」并排印出来。"""
    def _ts(t):
        return time.strftime("%m-%d %H:%M:%S", time.localtime(t)) if t else "-"

    print()
    print(f"出口 IP 惩罚期概览（口径 _warp_ip_is_blocked，{_ts(s['now'])}）")
    print(f"  台账 {s['total']} 条 / 带 blocked 标记 {s['marked']} 条")
    print(f"  仍在惩罚期 {s['active']} 条"
          f"（其中 ban_until 生效 {s['ban_until_active']} 条）")
    print(f"  已到期、标记残留 {s['expired']} 条"
          " —— 这些实际可复用，别按 blocked 字段数被封 IP")
    if s["earliest_release"]:
        print(f"  解禁：最早 {_ts(s['earliest_release'])}，"
              f"最晚 {_ts(s['latest_release'])}")
    if s["by_kind"]:
        print("  按类型：" + "，".join(f"{k} {v}" for k, v in s["by_kind"].items()))


def main() -> int:
    ap = argparse.ArgumentParser(description="渠道健康总览（只读）")
    ap.add_argument("--hours", type=float, default=6.0, help="统计窗口（小时）")
    ap.add_argument("--html", help="输出自包含 HTML 到该路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--by", choices=("colo", "source", "carrier"),
                    help="按维度汇总")
    ap.add_argument("--blocked-ips", action="store_true",
                    help="只输出出口 IP 惩罚期概览（区分标记残留与实际生效）")
    args = ap.parse_args()

    if args.blocked_ips:
        # 早退：这条路径只读 13MB 台账，不必再算渠道窗口/段/订阅源。
        s = blocked_ip_summary()
        if args.json:
            print(json.dumps(s, ensure_ascii=False, indent=1))
        else:
            print_blocked_ips(s)
        return 0

    state = (_read_json(PROJECT / ".warp_line_state.json") or {}).get("lines") or {}
    segs = channel_segments()
    rows = summarize(state, load_channels(),
                     _read_json(sources_path()) or {},
                     args.hours, dormant=dormant_tags(), segments=segs)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    elif args.html:
        render_html(rows, args.hours, Path(args.html))
    else:
        print_table(rows, args.hours)
        print_segments(rows, segs)
    if args.by:
        print_grouped(rows, args.by)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
