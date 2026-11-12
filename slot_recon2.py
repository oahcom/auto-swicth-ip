#!/usr/bin/env python3
"""按槽位重建 v2：每个「线路+出口IP」接多少请求 + 驻留多久 + 为什么被换走。"""
import re
import sys
from bisect import bisect_right
from collections import Counter, defaultdict

LOG = sys.argv[1] if len(sys.argv) > 1 else "/tmp/j4.txt"

TS_RE = re.compile(r"^(\w{3}\s+\d+\s+\d\d:\d\d:\d\d)")
ROT_RE = re.compile(r"\[ASIP\] rotate")
PROX_RE = re.compile(r"\[PROXY\] OPENCODE \| (\S+) \| .*?url=(\S+?)(?:\s|\|)")


def kv(line):
    return dict(re.findall(r"(\w+)=(\S+)", line))


def secs(a, b):
    from datetime import datetime
    fmt = "%b %d %H:%M:%S"
    if "  " in a:
        a = a.replace("  ", " ")
    if "  " in b:
        b = b.replace("  ", " ")
    try:
        return (datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds()
    except Exception:
        return None


def main():
    url2slot = {}
    events = defaultdict(list)   # slot -> [(t, line, ip, reason)]
    reqs = []

    with open(LOG, encoding="utf-8", errors="ignore") as f:
        for ln in f:
            if not TS_RE.match(ln):
                continue
            if ROT_RE.search(ln):
                d = kv(ln)
                to, tou = d.get("to"), d.get("to_url")
                if to and tou:
                    url2slot[tou] = to
                if to:
                    events[to].append((ln[:15], d.get("to_line") or "?",
                                       d.get("to_ip") or "?", d.get("reason") or "?"))
            elif PROX_RE.search(ln):
                mm = PROX_RE.search(ln)
                reqs.append((ln[:15], mm.group(2), mm.group(1)))

    for s in events:
        events[s].sort()
    tlist = {s: [e[0] for e in events[s]] for s in events}

    per = defaultdict(lambda: {"n": 0, "dwell": 0.0, "reasons": Counter()})
    unattributed = 0
    for t, url, model in reqs:
        slot = url2slot.get(url)
        if not slot or slot not in events:
            unattributed += 1
            continue
        i = bisect_right(tlist[slot], t) - 1
        if i < 0:
            unattributed += 1
            continue
        ev = events[slot][i]
        per[(ev[1], ev[2])]["n"] += 1

    # 每个状态的驻留时长与换走原因（=下一事件的 reason）
    for slot, evs in events.items():
        for i, (t, line, ip, _) in enumerate(evs):
            key = (line, ip)
            if i + 1 < len(evs):
                d = secs(t, evs[i + 1][0])
                per[key]["reasons"][evs[i + 1][3]] += 1
            else:
                d = None
                per[key]["reasons"]["(窗口末)"] += 1
            if d is not None and d > per[key]["dwell"]:
                per[key]["dwell"] = d

    total = sum(v["n"] for v in per.values())
    print(f"请求 {len(reqs)}，归属 {total}（{unattributed} 条未归属），"
          f"(线,IP) 组合 {len(per)} → 人均 {total / len(per):.2f} 发\n")

    def agg(pred):
        keys = [k for k in per if pred(k[0])]
        n = sum(per[k]["n"] for k in keys)
        dw = [per[k]["dwell"] for k in keys if per[k]["dwell"]]
        dw.sort()
        q = lambda p: dw[min(len(dw) - 1, int(len(dw) * p))] if dw else 0
        rs = Counter()
        for k in keys:
            rs.update(per[k]["reasons"])
        return len(keys), n, q, rs

    for name, pred in (("CF", lambda l: l.startswith("WARP-CF")),
                       ("非CF", lambda l: not l.startswith("WARP-CF"))):
        k, n, q, rs = agg(pred)
        print(f"=== {name}: {k} 个 (线,IP)，{n} 请求，人均 {n / max(1, k):.2f} 发")
        print(f"    驻留(秒) 中位 {q(0.5):.0f} / p90 {q(0.9):.0f} / max {q(1.0):.0f}")
        print(f"    换走原因 {dict(rs.most_common(6))}\n")

    # 逐 CF 线
    print("=== CF 逐线")
    print(f"{'线':14} {'IP':>4} {'请求':>5} {'人均':>6} {'中位驻留(s)':>10}")
    for line in sorted({k[0] for k in per if k[0].startswith("WARP-CF")}):
        keys = [k for k in per if k[0] == line]
        n = sum(per[k]["n"] for k in keys)
        dw = sorted(per[k]["dwell"] for k in keys if per[k]["dwell"])
        med = dw[len(dw) // 2] if dw else 0
        print(f"{line:14} {len(keys):4d} {n:5d} {n / len(keys):6.2f} {med:10.0f}")

    vals = sorted(v["n"] for v in per.values())
    b = Counter()
    for v in vals:
        if v < 5: b["1-4"] += 1
        elif v < 20: b["5-19"] += 1
        elif v < 50: b["20-49"] += 1
        elif v < 100: b["50-99"] += 1
        elif v < 150: b["100-149"] += 1
        else: b["150+"] += 1
    print(f"\n单 (线,IP) 请求数分布: {dict(b)}")
    print(f"中位承载: {vals[len(vals) // 2]} 发   p90: {vals[int(len(vals) * 0.9)]} 发   "
          f"max: {vals[-1]} 发")


if __name__ == "__main__":
    main()
