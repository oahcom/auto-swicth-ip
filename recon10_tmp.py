import re, sys, json, datetime, collections
from bisect import bisect_right

J = sys.argv[1]
MON = {'Nov': 11}
TSJ = re.compile(r'^(\w{3}) +(\d+) (\d\d):(\d\d):(\d\d)')
TSD = re.compile(r'^\[(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\]')

def tj(m): return datetime.datetime(2026, MON[m.group(1)], int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))).timestamp()
def td(m): return datetime.datetime(*[int(m.group(i)) for i in range(1, 7)]).timestamp()

PORT2SLOT = {'7890': 'slot-1', '7892': 'slot-2', '7893': 'slot-3', '7894': 'slot-4',
             '7895': 'slot-5', '7896': 'slot-6', '7897': 'slot-7'}

# 1) 权威时间线：slot -> line（daemon 槽位切换 + gate 现场快照）
slot_line = collections.defaultdict(list)
line_ip = collections.defaultdict(list)
for ln in open('/home/administrator/dkk-projects/auto-switch-ip/daemon.log', encoding='utf-8', errors='ignore'):
    m = TSD.match(ln)
    if not m: continue
    t = td(m)
    r = re.search(r'↻ 槽位 (slot-\d) 错误切换：(\S+?) → (\S+?)，', ln)
    if r:
        slot_line[r.group(1)].append((t, r.group(3)))
        continue
    r = re.search(r'([✓✗]) (\S+?) 重启后出口=(\S+)', ln)
    if r and r.group(2).startswith('WARP'):
        ip = None if (r.group(1) == '✗' or r.group(3) == '探测失败') else r.group(3)
        line_ip[r.group(2)].append((t, ip))
for ln in open('/home/administrator/dkk-projects/auto-switch-ip/gate.log', encoding='utf-8', errors='ignore'):
    m = TSD.match(ln)
    if not m: continue
    r = re.search(r'⇢ (slot-\d) 报 \S+.*?线=(\S+) 出口=(\S+)', ln)
    if r:
        slot_line[r.group(1)].append((td(m), r.group(2)))
        if r.group(3) not in ('-', '未知'):
            line_ip[r.group(2)].append((td(m), r.group(3)))
for d in (slot_line, line_ip):
    for v in d.values(): v.sort()
for tag in line_ip:  # 相邻同 IP 合并，保留 None 跳变
    out = []
    for t, ip in line_ip[tag]:
        if not out or out[-1][1] != ip: out.append((t, ip))
    line_ip[tag] = out

def at(seq, t):
    if not seq: return None
    i = bisect_right([x[0] for x in seq], t) - 1
    return seq[i][1] if i >= 0 else None

def slot_of(port): return PORT2SLOT.get(port)

def ip_at(slot, t):
    line = at(slot_line.get(slot, []), t)
    if not line: return None, None
    return line, at(line_ip.get(line, []), t)

# 2) 请求：按端口归属 IP
req_cnt = collections.Counter()
seg = collections.defaultdict(lambda: {'n': 0, 't0': None, 't1': None})
PL = re.compile(r'\[PROXY\] OPENCODE \| (\S+) \|.*?url=http://127\.0\.0\.1:(\d+)')
for ln in open(J, encoding='utf-8', errors='ignore'):
    m = TSJ.match(ln)
    if not m: continue
    p = PL.search(ln)
    if not p: continue
    slot = slot_of(p.group(2))
    if not slot: continue
    t = tj(m)
    line, ip = ip_at(slot, t)
    if not ip: continue
    req_cnt[ip] += 1
    a = seg[(slot, ip)]
    a['n'] += 1
    a['t0'] = t if a['t0'] is None else min(a['t0'], t)
    a['t1'] = t if a['t1'] is None else max(a['t1'], t)

# 3) 429：文档口径 accounted on <port>
acc429 = collections.Counter()
A429 = re.compile(r'\[EGRESS\] retry \d+/\d+ 429 -> accounted on http://127\.0\.0\.1:(\d+)')
for ln in open(J, encoding='utf-8', errors='ignore'):
    m = TSJ.match(ln)
    if not m: continue
    a = A429.search(ln)
    if not a: continue
    slot = slot_of(a.group(1))
    if not slot: continue
    line, ip = ip_at(slot, tj(m))
    if ip: acc429[ip] += 1

ns = sorted(req_cnt.values())
tot = sum(ns)
print('=== 文档口径（accounted on port + 端口当时 IP）===')
print('窗口 %s -> %s' % (open(J, encoding='utf-8', errors='ignore').readline()[:15].strip(), ''))
print('可归属请求 %d  不同出口 IP %d  均值 %.2f  中位 %d  p90 %d  max %d'
      % (tot, len(ns), tot / max(len(ns), 1), ns[len(ns) // 2], ns[int(len(ns) * .9)], ns[-1]))
b = collections.Counter()
for n in ns:
    b['1' if n == 1 else '2-3' if n <= 3 else '4-10' if n <= 10 else '11-50' if n <= 50 else '51-150' if n <= 150 else '>150'] += 1
for k in ['1', '2-3', '4-10', '11-50', '51-150', '>150']:
    print('  %-7s %4d 个 (%.1f%%)' % (k, b[k], 100 * b[k] / len(ns)))
print('accounted 429 事件 %d  涉及 IP %d' % (sum(acc429.values()), len(acc429)))

# 4) 429 落在 IP 生命第几发（用 accounted 口径）
hist = collections.Counter()
for (slot, ip), a in seg.items():
    pass
# 逐条 429 回放，按该 IP 当时已承载请求数定位
ev = []
PL2 = re.compile(r'\[PROXY\] OPENCODE \| \S+ \|.*?url=http://127\.0\.0\.1:(\d+)')
for ln in open(J, encoding='utf-8', errors='ignore'):
    m = TSJ.match(ln)
    if not m: continue
    t = tj(m)
    p = PL2.search(ln)
    if p:
        s = slot_of(p.group(1))
        if s:
            _, ip = ip_at(s, t)
            if ip: ev.append((t, 0, ip))
    a = A429.search(ln)
    if a:
        s = slot_of(a.group(1))
        if s:
            _, ip = ip_at(s, t)
            if ip: ev.append((t, 1, ip))
ev.sort()
seen = collections.Counter()
for t, kind, ip in ev:
    if kind == 0: seen[ip] += 1
    else: hist[min(seen[ip], 200)] += 1
tot429 = sum(hist.values())
bucket = collections.Counter()
for k, v in hist.items():
    bucket['0(首发)' if k == 0 else '1-3' if k <= 3 else '4-10' if k <= 10 else '11-50' if k <= 50 else '51-150' if k <= 150 else '>150'] += v
print('\n429 落在该 IP 已承载第几发:')
for k in ['0(首发)', '1-3', '4-10', '11-50', '51-150', '>150']:
    print('  %-9s %4d (%.1f%%)' % (k, bucket[k], 100 * bucket[k] / max(tot429, 1)))

# 5) 段长与用量
dw = sorted(a['t1'] - a['t0'] for a in seg.values() if a['t0'] is not None)
print('\n段驻留: p50 %.0fs p90 %.0fs max %.0fs' % (dw[len(dw) // 2], dw[int(len(dw) * .9)], dw[-1]))
print('每 IP 用量 = 段驻留 × 槽速率；实测均值 %.2f' % (tot / max(len(ns), 1)))
