#!/usr/bin/env python3
"""判定计量单位：/128 还是 /64。输出每个 /64 内的地址数与承载量。"""
import re, sys, collections, ipaddress

BASE = '/home/administrator/dkk-projects/auto-switch-ip'
MON = {'Nov': 11}
TSD = re.compile(r'^\[(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\]')
TSJ = re.compile(r'^(\w{3}) +(\d+) (\d\d):(\d\d):(\d\d)')
PS = {'7890': 'slot-1', '7892': 'slot-2', '7893': 'slot-3',
      '7894': 'slot-4', '7895': 'slot-5', '7896': 'slot-6'}
import datetime
td = lambda m: datetime.datetime(*[int(m.group(i)) for i in range(1, 7)]).timestamp()
tj = lambda m: datetime.datetime(2026, MON[m.group(1)], int(m.group(2)), int(m.group(3)),
                                 int(m.group(4)), int(m.group(5))).timestamp()
from bisect import bisect_right

line_ip = collections.defaultdict(list)
slot_line = collections.defaultdict(list)
for ln in open(BASE + '/daemon.log', encoding='utf-8', errors='ignore'):
    m = TSD.match(ln)
    if not m:
        continue
    t = td(m)
    r = re.search(r'↻ 槽位 (slot-\d) 错误切换：(\S+?) → (\S+?)，', ln)
    if r:
        slot_line[r.group(1)].append((t, r.group(3)))
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
        slot_line[r.group(1)].append((td(m), r.group(2)))
        if r.group(3) not in ('-', '未知'):
            line_ip[r.group(2)].append((td(m), r.group(3)))
for d in (line_ip, slot_line):
    for v in d.values():
        v.sort()
for tag in line_ip:
    out = []
    for t, ip in line_ip[tag]:
        if not out or out[-1][1] != ip:
            out.append((t, ip))
    line_ip[tag] = out
for s in slot_line:
    out = []
    for t, ln_ in slot_line[s]:
        if not out or out[-1][1] != ln_:
            out.append((t, ln_))
    slot_line[s] = out

per_ip = collections.Counter()
for ln in open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/j_win.txt', encoding='utf-8', errors='ignore'):
    m = TSJ.match(ln)
    if not m:
        continue
    p = re.search(r'\[PROXY\] OPENCODE \| \S+ \|.*?url=http://127\.0\.0\.1:(\d+)', ln)
    if not p:
        continue
    s = PS.get(p.group(1))
    if not s or not slot_line.get(s):
        continue
    t = tj(m)
    sl = slot_line[s]
    line = sl[bisect_right([x[0] for x in sl], t) - 1][1]
    ips = line_ip.get(line, [])
    if not ips:
        continue
    ip = ips[bisect_right([x[0] for x in ips], t) - 1][1]
    if ip:
        per_ip[ip] += 1

per64 = collections.defaultdict(lambda: {'ips': 0, 'n': 0})
for ip, n in per_ip.items():
    k = str(ipaddress.ip_network(ip + '/64', strict=False))
    per64[k]['ips'] += 1
    per64[k]['n'] += n

rows = sorted(per64.items(), key=lambda kv: -kv[1]['n'])
print('有流量的 /64 共 %d 个，覆盖 %d 个地址、%d 发请求' %
      (len(per64), sum(v['ips'] for v in per64.values()), sum(v['n'] for v in per64.values())))
print('\n%-30s %6s %6s %8s' % ('/64', '地址数', '发数', '每地址'))
for k, v in rows[:12]:
    print('%-30s %6d %6d %8.1f' % (k, v['ips'], v['n'], v['n'] / v['ips']))

byips = collections.defaultdict(list)
for k, v in per64.items():
    byips[min(v['ips'], 6)].append(v['n'])
print('\n按「同一 /64 内有几个地址接到过流量」分组：')
for b in sorted(byips):
    xs = sorted(byips[b])
    print('  %d 个地址: %2d 个 /64  每 /64 总发数 中位 %3d / 均值 %5.1f / 最大 %3d'
          % (b, len(xs), xs[len(xs) // 2], sum(xs) / len(xs), xs[-1]))
