#!/usr/bin/env python3
"""出口 IP 实例：出生 → 死因 → 死前承载量。

死因取值（按时间最早）：
  rate429 / transport  该槽位 `ok → repairing（原因）`
  ADDR                 该线地址被后台换掉（未经过槽位失效）
  SLOT                 槽位改挂别的线（无 repairing 记录）
  CENSOR               窗口结束仍在用
"""
import re, sys, collections, datetime, statistics
from bisect import bisect_right

BASE = '/home/administrator/dkk-projects/auto-switch-ip'
MON = {'Nov': 11}
TSD = re.compile(r'^\[(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\]')
TSJ = re.compile(r'^(\w{3}) +(\d+) (\d\d):(\d\d):(\d\d)')
PORT_SLOT = {'7890': 'slot-1', '7892': 'slot-2', '7893': 'slot-3',
             '7894': 'slot-4', '7895': 'slot-5', '7896': 'slot-6'}
JOURNAL = sys.argv[1] if len(sys.argv) > 1 else '/tmp/j_win.txt'
fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S')
td = lambda m: datetime.datetime(*[int(m.group(i)) for i in range(1, 7)]).timestamp()
tj = lambda m: datetime.datetime(2026, MON[m.group(1)], int(m.group(2)), int(m.group(3)),
                                 int(m.group(4)), int(m.group(5))).timestamp()

line_ip = collections.defaultdict(list)
slot_line = collections.defaultdict(list)
fail = collections.defaultdict(list)     # slot -> [(t, reason)]
restart_by_line = collections.Counter()
probe_by_line = collections.Counter()

for ln in open(BASE + '/daemon.log', encoding='utf-8', errors='ignore'):
    m = TSD.match(ln)
    if not m:
        continue
    t = td(m)
    r = re.search(r'↻ 槽位 (slot-\d) 错误切换：(\S+?) → (\S+?)，', ln)
    if r:
        slot_line[r.group(1)].append((t, r.group(3)))
        continue
    r = re.search(r'· 槽位 (slot-\d) 状态 ok → repairing（(\w+)）', ln)
    if r:
        fail[r.group(1)].append((t, r.group(2)))
        continue
    r = re.search(r'↻ 尝试重启 (\S+?)（', ln)
    if r:
        restart_by_line[r.group(1)] += 1
        continue
    r = re.search(r'([✓✗]) (\S+?) 出口 \S+ 模型层: (\w+)', ln)
    if r:
        probe_by_line[(r.group(2), r.group(3))] += 1
        continue
    r = re.search(r'([✓✗]) (\S+?) 重启后出口=(\S+)', ln)
    if r and r.group(2).startswith('WARP'):
        line_ip[r.group(2)].append((t, None if r.group(1) == '✗' or r.group(3) == '探测失败' else r.group(3)))

for ln in open(BASE + '/gate.log', encoding='utf-8', errors='ignore'):
    m = TSD.match(ln)
    if not m:
        continue
    r = re.search(r'⇢ (slot-\d) 报 \S+.*?线=(\S+) 出口=(\S+)', ln)
    if r:
        t = td(m)
        slot_line[r.group(1)].append((t, r.group(2)))
        if r.group(3) not in ('-', '未知'):
            line_ip[r.group(2)].append((t, r.group(3)))

for d in (line_ip, slot_line, fail):
    for v in d.values():
        v.sort()
for slot in slot_line:
    out = []
    for t, ln_ in slot_line[slot]:
        if out and out[-1][1] == ln_:
            continue
        out.append((t, ln_))
    slot_line[slot] = out
for tag in line_ip:
    out = []
    for t, ip in line_ip[tag]:
        if out and out[-1][1] == ip:
            continue
        out.append((t, ip))
    line_ip[tag] = out

reqs = []
PROX = re.compile(r'\[PROXY\] OPENCODE \| \S+ \|.*?url=http://127\.0\.0\.1:(\d+)')
for ln in open(JOURNAL, encoding='utf-8', errors='ignore'):
    m = TSJ.match(ln)
    if not m:
        continue
    p = PROX.search(ln)
    if p and PORT_SLOT.get(p.group(1)):
        reqs.append((tj(m), PORT_SLOT[p.group(1)]))
reqs.sort()
T0, T1 = reqs[0][0], reqs[-1][0]

inst = collections.defaultdict(lambda: {'n': 0, 'first': None})
for t, slot in reqs:
    seq = slot_line.get(slot, [])
    if not seq:
        continue
    line = seq[bisect_right([x[0] for x in seq], t) - 1][1]
    ips = line_ip.get(line, [])
    if not ips:
        continue
    ip = ips[bisect_right([x[0] for x in ips], t) - 1][1]
    if not ip:
        continue
    a = inst[(slot, line, ip)]
    a['n'] += 1
    a['first'] = t if a['first'] is None else min(a['first'], t)


def window(slot, line, ip):
    ips = line_ip.get(line, [])
    b = next((tt for tt, v in ips if v == ip), None)
    if b is None:
        return None
    addr_end = next((tt for tt, v in ips if tt > b and v != ip), None)
    sl = slot_line.get(slot, [])
    i = bisect_right([x[0] for x in sl], b) - 1
    sb = sl[i][0] if i >= 0 else b
    se = sl[i + 1][0] if i + 1 < len(sl) else None
    f = next(((tt, why) for tt, why in fail.get(slot, []) if tt >= max(b, sb)), None)
    return max(b, sb), addr_end, se, f


rows = []
for (slot, line, ip), a in inst.items():
    w = window(slot, line, ip)
    if not w:
        continue
    birth, addr_end, slot_end, f = w
    cand = [(addr_end, 'ADDR', None), (slot_end, 'SLOT', None), (T1, 'CENSOR', None)]
    if f:
        cand.append((f[0], f[1], f[1]))
    cand = [c for c in cand if c[0] is not None]
    death, cause, _ = min(cand)
    rows.append({'slot': slot, 'line': line, 'ip': ip, 'n': a['n'], 'birth': birth,
                 'death': death, 'cause': cause, 'life': max(death - birth, 0)})

print('窗口 %s → %s（%.1f 分钟）  实例 %d  请求 %d'
      % (fmt(T0), fmt(T1), (T1 - T0) / 60, len(rows), sum(r['n'] for r in rows)))
print('\n=== 死因：谁结束了这个出口')
for c, v in collections.Counter(r['cause'] for r in rows).most_common():
    sub = [r for r in rows if r['cause'] == c]
    xs = sorted(r['n'] for r in sub)
    ls = sorted(r['life'] for r in sub)
    print('  %-9s %4d 个 (%4.1f%%)  承载 %5d 发  用量中位 %3d / 均值 %5.1f  寿命中位 %5.0fs'
          % (c, v, 100 * v / len(rows), sum(xs), xs[len(xs) // 2],
             sum(xs) / len(xs), ls[len(ls) // 2]))

print('\n=== 用量最高的 12 个出口（看容量上限到底在哪）')
for r in sorted(rows, key=lambda x: -x['n'])[:12]:
    print('  %3d 发  寿命 %5.0fs (%4.1fmin)  %3.1f 发/分  死于 %-8s %s'
          % (r['n'], r['life'], r['life'] / 60, r['n'] / max(r['life'] / 60, .01),
             r['cause'], r['ip']))

print('\n=== 用量分布（按死因）')
for c in ['rate429', 'transport', 'ADDR', 'SLOT', 'CENSOR']:
    sub = [r for r in rows if r['cause'] == c]
    if not sub:
        continue
    xs = sorted(r['n'] for r in sub)
    print('  %-9s n=%3d  p50=%2d  p90=%3d  max=%3d  ≥50 的有 %d 个'
          % (c, len(sub), xs[len(xs) // 2], xs[int(len(xs) * .9)], xs[-1],
             sum(1 for x in xs if x >= 50)))

print('\n=== 关键对照：寿命够长的那些，拿到多少')
for lo in [0, 60, 180, 300, 600, 900]:
    sub = [r for r in rows if r['life'] >= lo]
    if not sub:
        continue
    xs = sorted(r['n'] for r in sub)
    print('  寿命 ≥%4ds: %3d 个  用量中位 %3d  p90 %3d  max %3d  均值 %.1f'
          % (lo, len(sub), xs[len(xs) // 2], xs[int(len(xs) * .9)], xs[-1],
             sum(xs) / len(xs)))

print('\n=== 探测量 vs 生产量（按线）')
tot_probe = sum(probe_by_line.values())
q = sum(v for (t, vd), v in probe_by_line.items() if vd != 'OK')
print('  模型层探测 %d 发（quota %d = %.1f%%），生产请求 %d 发，比值 %.2f'
      % (tot_probe, q, 100 * q / tot_probe, sum(r['n'] for r in rows),
         tot_probe / sum(r['n'] for r in rows)))
