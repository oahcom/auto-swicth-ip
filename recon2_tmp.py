#!/usr/bin/env python3
"""重建：每个槽位、每个出口 IP 的生命周期与承载量（权威时间线版）。

地址真值来源：daemon.log  `✓ <tag> 重启后出口=<ip>` / `✗ ... 探测失败`
             gate.log   `⇢ slot-N 报 ... 线=<tag> 出口=<ip>`（地址现场快照）
槽位真值来源：daemon.log  `↻ 槽位 slot-N 错误切换：A → B，实测 ...`
流量真值来源：9router journal `[PROXY] OPENCODE ... url=http://127.0.0.1:PORT`
"""
import re, sys, collections, datetime, statistics
from bisect import bisect_right

BASE = '/home/administrator/dkk-projects/auto-switch-ip'
DAEMON = BASE + '/daemon.log'
GATE = BASE + '/gate.log'
JOURNAL = sys.argv[1] if len(sys.argv) > 1 else '/tmp/j_win.txt'

MON = {'Nov': 11}
TSD = re.compile(r'^\[(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\]')
TSJ = re.compile(r'^(\w{3}) +(\d+) (\d\d):(\d\d):(\d\d)')
PORT_SLOT = {'7890': 'slot-1', '7892': 'slot-2', '7893': 'slot-3',
             '7894': 'slot-4', '7895': 'slot-5', '7896': 'slot-6'}
fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S')


def td(m):
    return datetime.datetime(*[int(m.group(i)) for i in range(1, 7)]).timestamp()


def tj(m):
    return datetime.datetime(2026, MON[m.group(1)], int(m.group(2)),
                             int(m.group(3)), int(m.group(4)), int(m.group(5))).timestamp()


line_ip = collections.defaultdict(list)    # tag -> [(t, ip|None)]
slot_line = collections.defaultdict(list)  # slot -> [(t, tag)]
restart_reason = collections.Counter()
restart_by_line = collections.Counter()
probe = collections.Counter()              # (tag, verdict) -> n

for ln in open(DAEMON, encoding='utf-8', errors='ignore'):
    m = TSD.match(ln)
    if not m:
        continue
    t = td(m)
    r = re.search(r'↻ 槽位 (slot-\d) 错误切换：(\S+?) → (\S+?)，', ln)
    if r:
        slot_line[r.group(1)].append((t, r.group(3)))
        continue
    r = re.search(r'↻ 尝试重启 (\S+?)（(.+)）', ln)
    if r:
        restart_reason[r.group(2)] += 1
        restart_by_line[r.group(1)] += 1
        continue
    r = re.search(r'([✓✗]) (\S+) 出口 \S+ 模型层: (\w+)', ln)
    if r:
        probe[(r.group(2), r.group(3))] += 1
        continue
    r = re.search(r'([✓✗]) (\S+?) 重启后出口=(\S+)', ln)
    if r and r.group(2).startswith('WARP'):
        line_ip[r.group(2)].append(
            (t, r.group(3) if r.group(1) == '✓' and r.group(3) != '探测失败' else None))

gate_fix = collections.defaultdict(list)   # slot -> [(t, tag, ip)]
for ln in open(GATE, encoding='utf-8', errors='ignore'):
    m = TSD.match(ln)
    if not m:
        continue
    r = re.search(r'⇢ (slot-\d) 报 (\S+?)(?:（[^）]*）)?.*?线=(\S+) 出口=(\S+)', ln)
    if not r:
        continue
    ip = r.group(4)
    gate_fix[r.group(1)].append((td(m), r.group(3), r.group(2),
                                 None if ip in ('-', '未知') else ip))

for v in line_ip.values():
    v.sort()
for v in slot_line.values():
    v.sort()
for v in gate_fix.values():
    v.sort()

# gate 快照里的 (线, 地址) 也是真值：补进 line_ip，用来给「没重启过」的线定位地址
for slot, rows in gate_fix.items():
    for t, tag, kind, ip in rows:
        if ip:
            line_ip[tag].append((t, ip))
for v in line_ip.values():
    v.sort()
for tag in line_ip:
    ded = []
    for t, ip in line_ip[tag]:
        if ded and ded[-1][1] == ip:
            continue
        ded.append((t, ip))
    line_ip[tag] = ded

# 槽位锚点：第一个 gate 快照或切换事件
for slot in PORT_SLOT.values():
    cand = []
    if slot_line.get(slot):
        cand.append(slot_line[slot][0])
    if gate_fix.get(slot):
        cand.append((gate_fix[slot][0][0], gate_fix[slot][0][1]))
    if cand:
        slot_line[slot] = [(min(cand)[0], min(cand)[1])] + [x for x in slot_line.get(slot, []) if x[0] > min(cand)[0]]

reqs = []   # (t, slot, model, req_id)
PROX = re.compile(r'\[PROXY\] OPENCODE \| (\S+) \|.*?url=http://127\.0\.0\.1:(\d+)(?: .*?req=(\w+))?')
for ln in open(JOURNAL, encoding='utf-8', errors='ignore'):
    m = TSJ.match(ln)
    if not m:
        continue
    p = PROX.search(ln)
    if not p:
        continue
    slot = PORT_SLOT.get(p.group(2))
    if slot:
        reqs.append((tj(m), slot, p.group(1), p.group(3)))
reqs.sort()
T0, T1 = reqs[0][0], reqs[-1][0]
WIN = T1 - T0
rate_win = WIN / 60


def at(seq, t, idx=1):
    if not seq:
        return None, None
    i = bisect_right([x[0] for x in seq], t) - 1
    return (seq[i][idx], i) if i >= 0 else (None, None)


# 每个 (slot, line, ip) 实例
inst = collections.defaultdict(lambda: {'n': 0, 'first': None, 'last': None})
un = collections.Counter()
for t, slot, model, rid in reqs:
    line, _ = at(slot_line.get(slot, []), t)
    if not line:
        un['槽位无锚点'] += 1
        continue
    ip, _ = at(line_ip.get(line, []), t)
    if ip is None:
        un['地址未知'] += 1
        continue
    k = (slot, line, ip)
    a = inst[k]
    a['n'] += 1
    a['first'] = t if a['first'] is None else min(a['first'], t)
    a['last'] = t if a['last'] is None else max(a['last'], t)

print('=== 窗口 %s → %s（%.1f 分钟）' % (fmt(T0), fmt(T1), rate_win))
print('请求 %d 条，可归属 %d，未归属 %d %s'
      % (len(reqs), sum(a['n'] for a in inst.values()),
         sum(un.values()), dict(un) if un else ''))
print('归属率 %.1f%%' % (100 * sum(a['n'] for a in inst.values()) / len(reqs)))


def lifespan(slot, line, ip):
    """该 IP 在该槽位上的存活区间。"""
    seq = line_ip.get(line, [])
    b = e = None
    for j, (tt, v) in enumerate(seq):
        if v == ip:
            b = tt
            for k in range(j + 1, len(seq)):
                if seq[k][1] != ip:
                    e = seq[k][0]
                    break
            break
    if b is None:
        return None
    sl = slot_line.get(slot, [])
    i = bisect_right([x[0] for x in sl], b) - 1
    sb = sl[i][0] if i >= 0 else b
    se = sl[i + 1][0] if i + 1 < len(sl) else float('inf')
    return max(b, sb), min(e if e else float('inf'), se)


rows = []
for (slot, line, ip), a in inst.items():
    ls = lifespan(slot, line, ip)
    lo, hi = ls if ls else (a['first'], a['last'])
    life = (hi - lo) if hi != float('inf') else (a['last'] - lo)
    rows.append({'slot': slot, 'line': line, 'ip': ip, 'n': a['n'],
                 'life': max(life, 0), 'first': a['first'], 'last': a['last']})

print('\n=== 按槽位（窗口 %.1f 分钟）' % rate_win)
print('%-7s %5s %6s %6s %8s %9s %9s %9s' %
      ('slot', '线数', 'IP数', '请求', '发/分', '寿命中位', '寿命p90', '首发429'))
for slot in sorted(PORT_SLOT.values()):
    rs = [r for r in rows if r['slot'] == slot]
    if not rs:
        continue
    lifes = sorted(r['life'] for r in rs)
    n = sum(r['n'] for r in rs)
    print('%-7s %5d %6d %6d %8.2f %8.0fs %8.0fs' %
          (slot, len({r['line'] for r in rs}), len(rs), n, n / rate_win,
           lifes[len(lifes) // 2], lifes[int(len(lifes) * .9)]))

per_ip = collections.defaultdict(lambda: {'n': 0, 'life': 0})
for r in rows:
    p = per_ip[r['ip']]
    p['n'] += r['n']
    p['life'] = max(p['life'], r['life'])

ns = sorted(p['n'] for p in per_ip.values())
print('\n=== 出口 IP 维度：%d 个 IP 承载了 %d 发' % (len(ns), sum(ns)))
print('均值 %.2f / 中位 %d / p75 %d / p90 %d / p99 %d / max %d'
      % (sum(ns) / len(ns), ns[len(ns) // 2], ns[int(len(ns) * .75)],
         ns[int(len(ns) * .9)], ns[int(len(ns) * .99)], ns[-1]))
b = collections.Counter()
for n in ns:
    b['1' if n == 1 else '2-4' if n <= 4 else '5-19' if n <= 19 else
      '20-49' if n <= 49 else '50-99' if n <= 99 else '100-149' if n < 150 else '>=150'] += 1
for k in ['1', '2-4', '5-19', '20-49', '50-99', '100-149', '>=150']:
    print('  %-8s %4d 个 (%4.1f%%)' % (k, b[k], 100 * b[k] / len(ns)))

xs = [p['life'] for p in per_ip.values()]
ys = [p['n'] for p in per_ip.values()]
mx, my = statistics.mean(xs), statistics.mean(ys)
cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)
print('\n寿命 ~ 用量 r = %.3f' % (cov / (statistics.pstdev(xs) * statistics.pstdev(ys) or 1)))
sx = sorted(xs)
print('IP 寿命：中位 %.0fs / p90 %.0fs / max %.0fs / 均值 %.0fs'
      % (sx[len(sx) // 2], sx[int(len(sx) * .9)], sx[-1], mx))
r_per_slot = sum(ns) / rate_win / 6
print('每槽速率 %.2f 发/分 → 150 发需要 %.0f 分钟寿命；实测中位寿命 %.1f 分钟'
      % (r_per_slot, 150 / r_per_slot, sx[len(sx) // 2] / 60))

print('\n=== 重启决策原因（%d 次）' % sum(restart_reason.values()))
for k, v in restart_reason.most_common(6):
    print('  %-22s %5d (%4.1f%%)' % (k, v, 100 * v / sum(restart_reason.values())))

serv = sorted({r['line'] for r in rows})
print('\n=== 承载过流量的 %d 条线' % len(serv))
print('%-20s %8s %8s %10s' % ('线', '重启', '用量', '探测quota'))
for line in sorted(serv, key=lambda x: -restart_by_line.get(x, 0))[:12]:
    q = sum(v for (tag, vd), v in probe.items() if tag == line and vd != 'OK')
    print('%-20s %8d %8d %10d'
          % (line, restart_by_line.get(line, 0), sum(r['n'] for r in rows if r['line'] == line), q))
