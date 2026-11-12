#!/usr/bin/env python3
"""生成 WARP 出口段基线：.warp/ipv6_segments.json

用途：把 IPv6 /64 段与 IPv4 地址的"已用 / 已烧 / 当前可用"固化成基线，
供选线、段级冷却、扩容评估比对。只读项目内的状态文件，不改任何运行状态。

用法：
    python3 ipv6_segments.py            # 生成段表
    python3 ipv6_segments.py --no-live  # 跳过 socks 探活（离线也能跑）

数据来源：
    .warp_ip_history.jsonl   永久换线历史（每次使用/封禁一行）
    .warp_ip_used.json       滑动窗口台账（ban_until 判断当前封禁）
    .warp/wg_colo_map.json   节点→colo 实测映射
    daemon.log*              线路维度的归属（时间就近 ±120s）
"""
from __future__ import annotations

import argparse
import collections
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / ".warp" / "ipv6_segments.json"
TOL = 120

PORTS = {f"WARP-WG-{i}": 1109 + i for i in range(1, 17)}
PORTS.update({"WARP-CF4": 1080, "WARP-CF4-B": 1081, "WARP-CF4-C": 1082, "WARP-CF4-D": 1083, "WARP-CF4-E": 1084, "WARP-CF6": 1085, "WARP-CF6-B": 1086, "WARP-CF6-C": 1087, "WARP-CF6-D": 1088})

LINE_COLO = {
    **{f"WARP-WG-{i}": "NRT" for i in (1, 2, 3)},
    **{f"WARP-WG-{i}": "SIN" for i in (4, 5, 6)},
    **{f"WARP-WG-{i}": "PHX" for i in (7, 8, 9)},
    **{f"WARP-WG-{i}": "SJC" for i in range(10, 17)},
}

# 段指纹：第三段(/48) + 第四段(/64) → 落地机房。由实测 colo 反推，非猜测范围。
BAC1_RULES = [(0x3B00, 0x3B60, "NRT"), (0x6500, 0x6560, "SIN"), (0x1980, 0x19E0, "CF"),
              (0x7400, 0x7460, "TPE"), (0x3180, 0x31E0, "HKG")]
BAC5_RULES = [(0x4300, 0x4307, "NRT"), (0x55F8, 0x55FF, "SIN"), (0x6190, 0x6197, "PHX"),
              (0x6210, 0x6217, "SJC"), (0x1EF0, 0x1EFF, "LAX"), (0x1F00, 0x1F0F, "CFB"),
              (0x6360, 0x636F, "OLD"), (0x58E8, 0x58EF, "TPE"), (0x5901, 0x5904, "TPE"),
              (0x3980, 0x3987, "HKG"), (0xA5F8, 0xA5FF, "ORD")]


def colo_of(net: ipaddress.IPv6Network) -> str:
    # exploded：第四段为 0 时 str() 把地址压缩成 "::"，拆出来是空串 →
    # int('', 16) 抛 ValueError（bac1:31x0::/64 与 bac1:7400::/64 都命中过）。
    parts = [int(x, 16) for x in net.network_address.exploded.split(":")[:4]]
    band, p48, p64 = parts[1], parts[2], parts[3]
    if band == 0xBAC1:
        for lo, hi, name in BAC1_RULES:
            if lo <= p48 <= hi:
                if name == "CF":
                    return "CF"
                # 76x0 由第四段区分：:28=SJC，其余=PHX；其他 bac1 规则直接按表返回
                if p48 >= 0x7680 and p48 <= 0x76E0:
                    return "SJC" if p64 == 0x28 else "PHX"
                return name
        if 0x7680 <= p48 <= 0x76E0:
            return "SJC" if p64 == 0x28 else "PHX"
    if band == 0xBAC5:
        for lo, hi, name in BAC5_RULES:
            if lo <= p48 <= hi:
                return name
    return "UNKNOWN"


def net64(ip: str) -> str:
    return str(ipaddress.ip_network(ip + "/64", strict=False))


def trace(port: int, timeout: int = 5):
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", str(timeout), "--socks5-hostname", f"127.0.0.1:{port}",
             "https://www.cloudflare.com/cdn-cgi/trace"],
            capture_output=True, text=True, timeout=timeout + 4).stdout
        return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    except Exception:
        return {}


def load_edges(lo: float):
    edges: dict[str, list[tuple[float, str]]] = collections.defaultdict(list)
    ts_re = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
    ch = r"(WARP-(?:WG-\d+|CF(?:-[A-Z])?))"
    pats = [re.compile(r"✓ " + ch + r" 重启后出口=(\S+)"),
            re.compile(r"✗ " + ch + r" 出口 (\S+) 模型层"),
            re.compile(r"出口=WARP:" + ch + r"\((\S+)\)")]
    for name in ("daemon.log.3", "daemon.log.2", "daemon.log.1", "daemon.log"):
        p = BASE / name
        if not p.exists():
            continue
        with p.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                m = ts_re.match(raw)
                if not m:
                    continue
                t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                if t < lo:
                    continue
                for rx in pats:
                    o = rx.search(raw)
                    if o:
                        edges[o.group(2)].append((t, o.group(1)))
                        break
    for ip in edges:
        edges[ip].sort()
    return edges


def attribute(edges, ip: str, t: float):
    cand = edges.get(ip)
    if not cand:
        return None
    times = [x[0] for x in cand]
    import bisect
    i = bisect.bisect_left(times, t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(cand):
            d = abs(cand[j][0] - t)
            if best is None or d < best[0]:
                best = (d, cand[j][1])
    return best[1] if best and best[0] <= TOL else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-live", action="store_true", help="跳过 socks 探活")
    args = ap.parse_args()

    now = time.time()
    hist = BASE / ".warp_ip_history.jsonl"
    used_f = BASE / ".warp_ip_used.json"
    if not hist.exists() or not used_f.exists():
        print("缺少 .warp_ip_history.jsonl / .warp_ip_used.json，先确认在项目根目录运行", file=sys.stderr)
        return 1

    edges = load_edges(now - 24 * 3600)

    seg = collections.defaultdict(lambda: dict(uses=0, first=None, last=None, last_ban=None, lines=set()))
    ip4 = collections.defaultdict(lambda: dict(uses=0, first=None, last=None, lines=set()))
    for line in hist.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        ip, t = r.get("ip", ""), float(r["t"])
        if not ip:
            continue
        if ":" not in ip and not ipaddress.ip_address(ip) in ipaddress.ip_network("104.28.0.0/16"):
            continue  # 过滤 9.9.9.9 这类探测噪音，只保留 WARP 落地段
        key = net64(ip) if ":" in ip else ip
        d = seg[key] if ":" in ip else ip4[key]
        d["uses"] += 1
        d["first"] = t if d["first"] is None else min(d["first"], t)
        d["last"] = t if d["last"] is None else max(d["last"], t)
        if r.get("b"):
            if ":" in ip:
                d["last_ban"] = t if d["last_ban"] is None else max(d["last_ban"], t)
            else:
                d["last_ban"] = t if d.get("last_ban") is None else max(d["last_ban"], t)
        who = attribute(edges, ip, t)
        if who:
            d["lines"].add(who)

    used = json.loads(used_f.read_text(encoding="utf-8")).get("used", {})
    ban_until = {}
    for ip, e in used.items():
        bu = e.get("ban_until") or 0
        if bu > now:
            ban_until[net64(ip) if ":" in ip else ip] = max(ban_until.get(net64(ip) if ":" in ip else ip, 0), bu)

    live = {}
    if not args.no_live:
        for tag, port in PORTS.items():
            d = trace(port)
            if d.get("ip"):
                live[tag] = d
    held = collections.defaultdict(list)
    for tag, d in live.items():
        ip = d["ip"]
        held[net64(ip) if ":" in ip else ip].append(tag)

    segments = []
    by_colo = collections.defaultdict(lambda: dict(segments=0, banned_now=0, usable=0, prefixes48=set()))
    for s, d in sorted(seg.items()):
        net = ipaddress.ip_network(s)
        colo = colo_of(net)
        bu = ban_until.get(s, 0)
        banned = bu > now
        usable = not banned
        segments.append({
            "segment": s,
            "colo": colo,
            "parent48": str(net.supernet(new_prefix=48)),
            "parent32": str(net.supernet(new_prefix=32)),
            "band": "bac1" if ":bac1:" in s else "bac5",
            "hextet4": s.split(":")[3],
            "uses": d["uses"],
            "first_seen": d["first"],
            "last_seen": d["last"],
            "last_ban": d["last_ban"],
            "ban_remaining_sec": int(bu - now) if banned else 0,
            "banned_now": banned,
            "usable": usable,
            "held_by_current_exit": sorted(held.get(s, [])),
            "lines_seen": sorted(d["lines"]),
        })
        b = by_colo[colo]
        b["segments"] += 1
        b["banned_now"] += 1 if banned else 0
        b["usable"] += 1 if usable else 0
        b["prefixes48"].add(str(net.supernet(new_prefix=48)))

    ipv4 = []
    for ip, d in sorted(ip4.items()):
        bu = ban_until.get(ip, 0)
        colo = None
        for who in d["lines"]:
            colo = LINE_COLO.get(who) or colo
        ipv4.append({
            "ip": ip,
            "colo": colo or "UNKNOWN",
            "uses": d["uses"],
            "first_seen": d["first"],
            "last_seen": d["last"],
            "banned_now": bu > now,
            "ban_remaining_sec": int(bu - now) if bu > now else 0,
            "held_by_current_exit": sorted(held.get(ip, [])),
            "lines_seen": sorted(d["lines"]),
        })

    out = {
        "generated_at": int(now),
        "generated_at_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "note": ("WARP 出口段基线。usable=当前无活跃封禁，不代表可定向选择：WARP 每次会话在机房池内随机分配 /64。"
                 "colo 由段指纹反推，UNKNOWN/OLD 表示未匹配到已知机房或已停用配置。"),
        "source": [".warp_ip_history.jsonl", ".warp_ip_used.json", ".warp/wg_colo_map.json", "daemon.log*"],
        "summary": {
            "segments_total": len(segments),
            "segments_banned_now": sum(1 for x in segments if x["banned_now"]),
            "segments_usable": sum(1 for x in segments if x["usable"]),
            "ipv4_total": len(ipv4),
            "ipv4_usable": sum(1 for x in ipv4 if not x["banned_now"]),
            "live_tunnels": {k: v.get("ip") for k, v in live.items()},
            "by_colo": {k: {**v, "prefixes48": sorted(v["prefixes48"])} for k, v in sorted(by_colo.items())},
        },
        "ipv6_segments": segments,
        "ipv4_addresses": ipv4,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OUT)

    s = out["summary"]
    print(f"写入 {OUT}")
    print(f"/64 段 {s['segments_total']}，封禁中 {s['segments_banned_now']}，可用 {s['segments_usable']}")
    print(f"IPv4 {s['ipv4_total']}，可用 {s['ipv4_usable']}")
    for c, v in s["by_colo"].items():
        print(f"  {c:<8} 段={v['segments']:>3} 封禁={v['banned_now']:>3} 可用={v['usable']:>3} /48={len(v['prefixes48'])}")
    return 0


def colo_ban_rates(window_sec: float = 3600, now: float | None = None) -> dict[str, tuple[int, int]]:
    """最近窗口内每个 colo 的 (样本数, 封禁数)，来源 .warp_ip_history.jsonl。

    给 daemon 的「渠道被锁 → 重定位到低 ban 率 colo」用。只按地址段判 colo，
    不看渠道归属 —— 换线目标是 colo，不是某条线。
    """
    now = time.time() if now is None else now
    lo = now - float(window_sec)
    out: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    # 与 daemon 同源：它认 WARP_IP_HISTORY_FILE_PATH，这里不认的话测试会读到
    # 生产历史（`.warp_ip_history.jsonl` 是 11.7MB / 16 万行）。
    path = os.environ.get("WARP_IP_HISTORY_FILE_PATH") or str(BASE / ".warp_ip_history.jsonl")
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return {}          # 历史还没生成 / 被轮换掉：没有样本，不是错误
    for line in fh:
        try:
            r = json.loads(line)
            ip, t = r["ip"], float(r["t"])
        except (ValueError, KeyError, TypeError):
            continue
        if t < lo or t > now or not isinstance(ip, str):
            continue
        try:
            net = ipaddress.ip_network(net64(ip)) if ":" in ip else None
        except ValueError:
            continue
        if net is None:
            continue
        try:
            c = colo_of(net)
        except ValueError:
            continue
        d = out[c]
        d[0] += 1
        if r.get("b"):
            d[1] += 1
    return {c: (n, b) for c, (n, b) in out.items()}


if __name__ == "__main__":
    sys.exit(main())
