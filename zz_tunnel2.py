# -*- coding: utf-8 -*-
import os, re, datetime, collections
FILES = [f for f in ["daemon.log"] + ["daemon.log.%d" % i for i in range(1, 8)] if os.path.exists(f)]
lines = []
for f in FILES:
    lines += [l.rstrip("\n") for l in open(f, errors="ignore")]
TS = re.compile(r"\[(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\]")
def tstamp(l):
    m = TS.search(l)
    if not m: return None
    return datetime.datetime(*[int(x) for x in m.groups()]).timestamp()
SW = re.compile(r"↻ 槽位 (slot-\d+) 错误切换：(\S+) → (\S+)")
EV = re.compile(r"⇢ (slot-\d+) 报 (\S+)（(\S+)）.*?线=(\S+) 出口=(\S+) 报障IP=(\S+)")
BAD = re.compile(r"✗ 槽位 (slot-\d+) 错误切换失败")
ev, sw, bad = [], [], []
for l in lines:
    t = tstamp(l)
    if t is None: continue
    m = EV.search(l)
    if m: ev.append((t, m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6), l))
    m = SW.search(l)
    if m: sw.append((t, m.group(1), m.group(2), m.group(3), l))
    m = BAD.search(l)
    if m: bad.append((t, m.group(1), l))
ev.sort(); sw.sort(); bad.sort()
print("events=%d  switch_ok=%d  switch_fail=%d" % (len(ev), len(sw), len(bad)))
ev_by_slot = collections.defaultdict(list)
for e in ev: ev_by_slot[e[1]].append(e)
import bisect
times = {s: [e[0] for e in es] for s, es in ev_by_slot.items()}
same_line = diff_line = same_ip = diff_ip = no_after = 0
for t, slot, old, new, l in sw:
    es = ev_by_slot.get(slot, [])
    ks = times.get(slot, [])
    # 换线前最近一条报障
    i0 = bisect.bisect_left(ks, t) - 1
    prev = es[i0] if i0 >= 0 and t - es[i0][0] <= 120 else None
    i1 = bisect.bisect_right(ks, t)
    nxt = es[i1] if i1 < len(es) and es[i1][0] - t <= 120 else None
    if not nxt:
        no_after += 1; continue
    if nxt[4] == new: diff_line += 1
    elif nxt[4] == old: same_line += 1
    if prev:
        if nxt[5] == prev[5]: same_ip += 1
        else: diff_ip += 1
print("翻 selector 后 120s 内同槽报障:")
print("  线名=新线 %d / 线名=旧线 %d / 无后续报障 %d" % (diff_line, same_line, no_after))
print("  出口IP 与换线前相同 %d / 不同 %d" % (same_ip, diff_ip))
print("\n=== 最大簇时间窗内的换线事件 ===")
by_ip = collections.defaultdict(list)
for e in ev:
    if e[2] == "rate429" and e[5] not in ("-", "未知"): by_ip[e[5]].append(e)
clusters = []
for ip, es in by_ip.items():
    cur = []
    for e in es:
        if cur and 0 <= e[0]-cur[-1][0] <= 90: cur.append(e)
        else:
            if len(cur) >= 2: clusters.append((ip, cur))
            cur = [e]
    if len(cur) >= 2: clusters.append((ip, cur))
ip, es = max(clusters, key=lambda x: len(x[1]))
t0, t1 = es[0][0]-30, es[-1][0]+30
slot = es[0][1]
print("ip=%s slot=%s 发数=%d  %s → %s" % (ip[:34], slot, len(es),
      datetime.datetime.fromtimestamp(es[0][0]).strftime("%m-%d %H:%M:%S"),
      datetime.datetime.fromtimestamp(es[-1][0]).strftime("%m-%d %H:%M:%S")))
for label, arr in (("切换成功", sw), ("切换失败", bad)):
    sub = [x for x in arr if x[1] == slot and t0 <= x[0] <= t1]
    print("  该窗内 %s: %d" % (label, len(sub)))
    for x in sub[:5]: print("    " + x[-1].strip()[:150])
