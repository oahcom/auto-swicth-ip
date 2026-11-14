#!/usr/bin/env python3
"""每个出口 IP 实例的死因归因 + 槽位停机时间。

死因候选（按时刻取最早的）：
  L2-429    gate.log `⇢ slot-N 报 rate429 ... 出口=<ip>`   —— 该地址被判配额耗尽
  ADDR      daemon  该线下一次换址（含重启失败）            —— 后台/探测器把地址换掉
  SLOT      daemon  `↻ 槽位 slot-N 错误切换`                —— 槽位改挂别的线
  CENSOR    窗口结束仍在使用
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
slot_state = collections.defaultdict(list)   # slot -> [(t, state)]
gate429 = collections.defaultdict(list)      # ip -> [(t, slot)]
restart_by_line = collections.Counter()

for ln in open(BASE + '/daemon.log', encoding='utf-8', errors='ignore'):
    m = TSD.match(ln)
    if not m:
        continue
    t = td(m)
    r = re.search(r'↻ 槽位 (slot-\d) 错误切换：(\S+?) → (\S+?)，', ln)
    if r:
        slot_line[r.group(1)].append((t, r.group(3)))
        continue
    r = re.search(r'· 槽位 (slot-\d) 状态 (\w+) → (\w+)', ln)
    if r:
        slot_state[r.group(1)].append((t, r.group(3)))
        continue
    r = re.search(r'↻ 尝试重启 (\S+?)（', ln)
    if r:
        restart_by_line[r.group(1)] += 1
        continue
    r = re.search(r'([✓✗]) (\S+?) 重启后出口=(\S+)', ln)
    if r and r.group(2).startswith('WARP'):
        line_ip[r.group(2)].append((t, None if r.group(1) == '✗' or r.group(3) == '探测失败' else r.group(3)))

for ln in open(BASE + '/gate.log', encoding='utf-8', errors='ignore'):
    m = TSD.match(ln)
    if not m:
        continue
    t = td(m)
    r = re.search(r'⇢ (slot-\d) 报 rate429.*?线=(\S+) 出口=(\S+)', ln)
    if r:
        ip = r.group(3)
        if ip not in ('-', '未知'):
            gate429[ip].append((t, r.group(1)))
            line_ip[r.group(2)].append((t, ip))
    else:
        r = re.search(r'⇢ (slot-\d) 报 \S+.*?线=(\S+) 出口=(\S+)', ln)
        if r and r.group(3) not in ('-', '未知'):
            line_ip[r.group(2)].append((t, r.group(3)))
            if not slot_line.get(r.group(1)):
                slot_line[r.group(1)].append((t, r.group(2)))

for d in (line_ip, slot_line, slot_state, gate429):
    for v in d.values():
        v.sort()
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

# 槽位锚点
first_slot = {}
for t, slot in reqs:
    first_slot.setdefault(slot, t)
for slot, t in first_slot.items():
    if slot not in slot_line:
        slot_line[slot] = [(t, None)]

inst = collections.defaultdict(lambda: {'n': 0, 'first': None})
for t, slot in reqs:
    seq = slot_line.get(slot, [])
    line = seq[bisect_right([x[0] for x in seq], t) - 1][1] if seq else None
    if not line:
        continue
    ips = line_ip.get(line, [])
    if not ips:
        continue
    ip = ips[bisect_right([x[0] for x in ips], t) - 1][1]
    if not ip:
        continue
    a = inst[(slot, line, ip)]
    a['n'] += 1
    a['first'] = t if a['first'] is None else min(a['first'], t)

# 每个实例的边界
def boundaries(slot, line, ip):
    ips = line_ip.get(line, [])
    b = None
    for j, (tt, v) in enumerate(ips):
        if v == ip:
            b = tt
            break
    if b is None:
        return None
    # 地址结束
    addr_end = None
    for tt, v in ips:
        if tt > b and v != ip:
            addr_end = tt
            break
    sl = slot_line.get(slot, [])
    i = bisect_right([x[0] for x in sl], b) - 1
    slot_begin = sl[i][0] if i >= 0 else b
    slot_end = sl[i + 1][0] if i + 1 < len(sl) else None
    e429 = next((tt for tt, s in gate429.get(ip, []) if s == slot and tt >= b), None)
    return max(b, slot_begin), addr_end, slot_end, e429


cause = collections.Counter()
detail = []
for (slot, line, ip), a in inst.items():
    bd = boundaries(slot, line, ip)
    if not bd:
        continue
    birth, addr_end, slot_end, e429 = bd
    cand = [(addr_end, 'ADDR'), (slot_end, 'SLOT'), (e429, 'L2-429'), (T1, 'CENSOR')]
    cand = [(t, c) for t, c in cand if t is not None]
    death, c = min(cand)
    cause[c] += 1
    detail.append({'slot': slot, 'line': line, 'ip': ip, 'n': a['n'],
                   'birth': birth, 'death': death, 'cause': c, 'life': death - birth})

print('窗口 %s → %s（%.1f 分钟）' % (fmt(T0), fmt(T1), (T1 - T0) / 60))
print('有流量的 IP 实例 %d，请求 %d' % (len(detail), sum(d['n'] for d in detail)))
print('\n=== 死因（谁结束了这个出口）')
for c, v in cause.most_common():
    xs = [d['n'] for d in detail if d['cause'] == c]
    ls = sorted(d['life'] for d in detail if d['cause'] == c)
    print('  %-8s %4d 个 (%4.1f%%)  承载 %5d 发  均值 %5.1f  中位寿命 %5.0fs'
          % (c, v, 100 * v / len(detail), sum(xs), sum(xs) / len(xs), ls[len(ls) // 2]))

print('\n=== 按死因拆分「没打满 150」的缺口')
tot = sum(d['n'] for d in detail)
print('  实际总承载 %d 发；若 200 个 IP 各打满 150 应为 %d 发' % (tot, len(detail) * 150))

# 槽位停机
print('\n=== 槽位 repairing 停机（6 槽 × %.1f 分钟 = %.0f 槽位分钟）'
      % ((T1 - T0) / 60, (T1 - T0) / 60 * 6))
total_down = 0
for slot in sorted(PORT_SLOT.values()):
    seq = slot_state.get(slot, [])
    down = 0
    spans = []
    start = None
    for t, st in seq:
        if st == 'repairing' and start is None:
            start = t
        elif st != 'repairing' and start is not None:
            spans.append(t - start)
            down += t - start
            start = None
    total_down += down
    if spans:
        spans.sort()
        print('  %-7s 进 repairing %2d 次  停机合计 %5.1f 分钟  中位 %4.1fs  p90 %5.1fs'
              % (slot, len(spans), down / 60, spans[len(spans) // 2], spans[int(len(spans) * .9)]))
print('  合计停机 %.1f 槽位分钟 = 可用时长的 %.1f%%'
      % (total_down / 60, 100 * total_down / 60 / ((T1 - T0) / 60 * 6)))

# 承载线的重启间隔
serv = sorted({d['line'] for d in detail})
print('\n=== 承载线的地址更换频率（%d 条线）' % len(serv))
rows = []
for line in serv:
    seq = [t for t, ip in line_ip.get(line, [])]
    if len(seq) < 3:
        continue
    gaps = [b - a for a, b in zip(seq, seq[1:])]
    rows.append((statistics.median(gaps), len(seq), line))
rows.sort()
for med, n, line in rows[:10]:
    print('  %-20s 换址 %3d 次  间隔中位 %5.0fs' % (line, n, med))
print('  承载线换址间隔中位（跨线）: %.0fs' % statistics.median([r[0] for r in rows]))
