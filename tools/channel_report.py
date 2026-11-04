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
"""
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
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
from asip.redact import redact_secrets  # noqa: E402
from config import CFG, extra_subscription_urls, subscription_urls  # noqa: E402
from parse_subscription import source_label  # noqa: E402

# verdict 语义：OK 可用；quota/flagged 是出口级的两种拦截；其余是探测自身故障。
BAN_VERDICTS = {"quota", "flagged"}

# 块表没收录的 colo 兜底（BOM 是 2026-11-04 新订阅实测出来的）。
COLO_FALLBACK = {
    "NRT": "东京", "PHX": "凤凰城", "LAX": "洛杉矶", "SJC": "圣何塞",
    "SIN": "新加坡", "TPE": "台北", "HKG": "香港", "ORD": "芝加哥",
    "ICN": "首尔", "SLC": "盐湖城", "PIT": "匹兹堡", "BOM": "孟买",
    "AMS": "阿姆斯特丹", "FRA": "法兰克福", "LHR": "伦敦",
}
# ipv6_segments.colo_of() 里几个值不是落地机房，是地址段批次。
PSEUDO_COLO = {"CF": "CF 直连", "CFB": "CF 直连", "OLD": "旧批次", "UNKNOWN": "未知"}


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


def channel_segments(log_file: Path | None = None) -> dict[str, dict]:
    """每条渠道**落过的每一个出口段**：`{tag: {"v6": [段名...], "v4": [地址...]}}`。

    返回的是**段名列表**而不是计数：只给个数等于什么都没说，要看的是
    「落过哪几段」，且段名是完整的四段 `2a09:bac5:55f8:18be`。

    **不按时间窗口截断**：段列表是「这条线去过哪些地方」，不是随时间衰减的
    健康指标。限制窗口会让 H2 线显示 0 段（它们的出口日志停在 11-03，
    之后一直没拿到地址），看起来像「从没出去过」。

    v6 按 /64 计 —— 一次抽签得到的是 /64，同 /64 内的不同 /128 不算换段；
    v4 按地址计（v4 没有段的概念，同一个 colo 池里翻牌）。

    WHY 读日志而不是台账：`.warp_ip_history.jsonl` 只有 `ip/t/b/k`，**没有
    渠道归属**（唯一写入点 `daemon.py:_mark_warp_ip_used` 拿到的就是地址）。
    带渠道归属的只有 daemon 打的两类出口行：`出口 <ip> 模型层` / `重启后出口=<ip>`。
    """
    path = log_file or Path(CFG["log_file"])
    seen: dict[str, dict[str, set]] = defaultdict(lambda: {"v6": set(), "v4": set()})
    for p in _rotated_logs(path):
        try:
            fh = p.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                o = _EGRESS_LINE.search(line)
                if not o:
                    continue
                ip = o.group(2)
                try:
                    addr = ipaddress.ip_address(ip)
                except ValueError:
                    continue          # `探测失败` 之类的占位
                if addr.version == 6:
                    seen[o.group(1)]["v6"].add(
                        _seg_name(ipaddress.ip_network(ip + "/64", strict=False)))
                else:
                    seen[o.group(1)]["v4"].add(ip)
    return {tag: {"v6": sorted(v["v6"], key=_seg_sort), "v4": sorted(v["v4"])}
            for tag, v in seen.items()}


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
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


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


def summarize(state: dict, chans: dict, sources: dict, hours: float,
              now: float | None = None, dormant: set[str] | None = None,
              segments: dict[str, dict] | None = None) -> list[dict]:
    """每条渠道一行。`ban_probes` 只留当天，故窗口按当地零点夹一次 ——
    不夹的话「最近 24h」会把今天凌晨那几条样本当成整天的结论。

    `segments` 是 `channel_segments()` 的结果；不传就留空（单测与离线调用
    不必依赖 daemon.log）。"""
    now = time.time() if now is None else now
    cutoff = max(now - hours * 3600, _day_start(now))
    urls = sub_url_map()
    idx = source_index(sources)
    colos = colo_ban_rates(hours * 3600, now)
    segs = segments or {}
    rows = []
    for tag, meta in chans.items():
        st = state.get(tag) or {}
        samples = [s for s in (st.get("ban_probes") or [])
                   if isinstance(s, (list, tuple)) and len(s) == 2
                   and isinstance(s[0], (int, float)) and s[0] >= cutoff]
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
            # 窗口内这条线**落过的全部**出口段（v6 段名，v4 地址）。
            # 不是「池子里有多少段」—— 池子是机房共享的，同一时刻所有线在
            # 同一个池里抽签；这里说的是**这条线自己的覆盖面**。
            "seg_v6": (segs.get(tag) or {}).get("v6") or [],
            "seg_v4": (segs.get(tag) or {}).get("v4") or [],
            "held": bool(st.get("ban_hold_until") and st["ban_hold_until"] > now),
            "hold_until": st.get("ban_hold_until") or 0,
            "quota_until": st.get("quota_until") or 0,
            # 窗口内重启次数，不是 `restart_count`。后者是**进程终身累计**
            # （实测 WARP-WG-1 报 38881 次），拿它当健康指标等于把历史总和
            # 印在今天的行上。`restart_hist` 才是带时间戳的那份。
            "restarts": sum(1 for t in (st.get("restart_hist") or [])
                            if isinstance(t, (int, float)) and t >= cutoff),
            "last_verdict": st.get("last_verdict") or "-",
            "no_addr_since": st.get("ban_noaddr_since") or 0,
            "window_from": cutoff,
        })
    rows.sort(key=lambda r: (r["carrier"] or "", r["colo"], r["tag"]))
    return rows


def _fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _pct(v: float | None) -> str:
    return f"{v * 100:.0f}%" if v is not None else "—"


def _seg48(name: str) -> str:
    """`2a09:bac5:55f8:18be`（/64）→ `2a09:bac5:55f8`（/48）。

    /48 才是「批次」：同一个 /48 下的 /64 是同一个机房池里的一次抽签，
    而不同 /48 之间是**不同的地址池**。看覆盖面先看 /48 有几个。
    """
    return name.rsplit(":", 1)[0]


def _seg48_groups(r: dict) -> list[tuple[str, int]]:
    """`[(/48 名, 它下面落过几个 /64)]`，按数值排序。"""
    from collections import Counter
    c = Counter(_seg48(s) for s in (r.get("seg_v6") or []))
    return sorted(c.items(), key=lambda kv: _seg_sort(kv[0]))


def _seg_counts(r: dict) -> tuple[int, int]:
    """`(v6 /48 个数, v6 /64 个数)`。"""
    return len(_seg48_groups(r)), len(r.get("seg_v6") or [])


def print_segments(rows: list[dict]) -> None:
    """每条渠道：先列 **/48**，再给 /64 个数，最后列 v4 地址。

    列全部 /64 会淹掉屏幕（一条线 60~74 个），而且 /64 是**抽签结果**、不可选；
    /48 才是不同地址池的边界，一个机房通常只有 8~12 个。所以 /48 全列，
    /64 只给个数（按 /48 分组时括号里是该 /48 下的个数）。
    """
    if not rows:
        print("（没有渠道数据）")
        return
    print("\n出口段明细（v6 先列 /48、括号内是该 /48 下的 /64 个数；v4 列地址）")
    print("-" * 92)
    width = max((_w(r["tag"]) for r in rows), default=0) + 2
    for r in rows:
        groups = _seg48_groups(r)
        n48, n64 = _seg_counts(r)
        v4 = list(r.get("seg_v4") or [])
        head = f"{_pad(r['tag'], width)} "
        if n64:
            head += f"{n48} 个 /48 · {n64} 个 /64"
        if v4:
            head += (" · " if n64 else "") + f"{len(v4)} 个 v4 地址"
        if not n64 and not v4:
            print(head + "—")
            continue
        print(head)
        indent = " " * (width + 2)
        line = indent
        for name, n in groups:
            token = f"{name}({n})" if n > 1 else name
            if _w(line) + _w(token) + 2 > 140:
                print(line)
                line = indent
            line += token + "  "
        for a in v4:
            if _w(line) + _w(a) + 2 > 140:
                print(line)
                line = indent
            line += a + "  "
        print(line)


def _seg_label(r: dict) -> str:
    """表格里的「出口段」列：`/48 个数 / /64 个数`，v4 追加 `+Nv`。

    两者都为 0 时显示 `—` 而不是 `0/0` —— 会被读成「一段都没有」，
    实际是「日志里没见过它的出口」（新起的线、日志被轮转过）。
    """
    n48, n64 = _seg_counts(r)
    n4 = len(r.get("seg_v4") or [])
    parts = []
    if n64:
        parts.append(f"{n48}/{n64}")
    if n4:
        parts.append(f"+{n4}v")
    return " ".join(parts) or "—"


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


def print_table(rows: list[dict], hours: float) -> None:
    if not rows:
        print("（没有渠道数据）")
        return
    window_from = rows[0]["window_from"]
    if window_from > time.time() - hours * 3600 + 60:
        print(f"注：渠道样本只保留当天，实际窗口从 {_fmt_ts(window_from)} 起"
              f"（< {hours:g}h）\n")
    head = (f"{_pad('渠道', 18)} {_pad('承载', 5)} {_pad('colo', 4)} "
            f"{_pad('机房', 8)} {_pad('成功率', 6, False)} "
            f"{_pad('ban', 5, False)} {_pad('成/败', 7, False)} "
            f"{_pad('colo ban', 9, False)} {_pad('/48,/64', 10, False)} "
            f"{_pad('重启', 6, False)}  "
            f"{_pad('订阅', 8)} {_pad('节点名', 30)} 节点 / 状态")
    print(head)
    print("-" * (_w(head) + 40))
    for r in rows:
        flags = []
        if r["held"]:
            flags.append(f"锁定至{_fmt_ts(r['hold_until'])}")
        if r["quota_until"] > time.time():
            flags.append(f"配额至{_fmt_ts(r['quota_until'])}")
        if r["no_addr_since"]:
            flags.append("无地址")
        if not flags:
            flags.append(r["last_verdict"])
        okfail = f"{r['ok']}/{r['ban'] + r['err']}"
        print(f"{_pad(r['tag'], 18)} {_pad(r['carrier'], 5)} "
              f"{_pad(r['colo'], 4)} {_pad(r['loc'], 8)} "
              f"{_pad(_pct(r['success_rate']), 6, False)} "
              f"{_pad(_pct(r['ban_rate']), 5, False)} "
              f"{_pad(okfail, 7, False)} "
              f"{_pad(_pct(r['colo_ban_rate']), 9, False)} "
              f"{_pad(_seg_label(r), 10, False)} "
              f"{_pad(str(r['restarts']), 6, False)}  "
              f"{_pad(r['source'], 8)} {_pad(r['node_name'], 30)} "
              f"{r['node']}  {' '.join(flags)}")
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
<th title="v6 出口段：/48 个数 / /64 个数（悬停看每个 /48）。v4 另计">出口段 /48·/64</th>
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
        if r["held"]:
            pills.append(f'<span class="pill hold">锁定至 {_fmt_ts(r["hold_until"])}</span>')
        if r["quota_until"] > time.time():
            pills.append(f'<span class="pill quot">配额至 {_fmt_ts(r["quota_until"])}</span>')
        if r["no_addr_since"]:
            pills.append('<span class="pill quot">无出口地址</span>')
        if not pills:
            pills.append(f'<span class="pill">{html.escape(r["last_verdict"])}</span>')
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
            f'{html.escape(" ".join(s for s, _ in _seg48_groups(r)))}">'
            f'{html.escape(_seg_label(r))}</td>'
            f'<td class="mono dim">{r["restarts"]}</td>'
            f'<td>{" ".join(pills)}</td></tr>')

    out.write_text(_TMPL.format(
        gen=time.strftime("%Y-%m-%d %H:%M:%S"), hours=hours,
        wf=_fmt_ts(rows[0]["window_from"]) if rows else "-",
        cards=card_html, rows="\n".join(body),
        good=GOOD, warn=WARN, crit=CRIT))
    print(f"→ {out}（{len(rows)} 条渠道，浏览器打开）")


def main() -> int:
    ap = argparse.ArgumentParser(description="渠道健康总览（只读）")
    ap.add_argument("--hours", type=float, default=6.0, help="统计窗口（小时）")
    ap.add_argument("--html", help="输出自包含 HTML 到该路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--by", choices=("colo", "source", "carrier"),
                    help="按维度汇总")
    args = ap.parse_args()

    state = (_read_json(PROJECT / ".warp_line_state.json") or {}).get("lines") or {}
    rows = summarize(state, load_channels(),
                     _read_json(sources_path()) or {},
                     args.hours, dormant=dormant_tags(),
                     segments=channel_segments())
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    elif args.html:
        render_html(rows, args.hours, Path(args.html))
    else:
        print_table(rows, args.hours)
        print_segments(rows)
    if args.by:
        print_grouped(rows, args.by)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
