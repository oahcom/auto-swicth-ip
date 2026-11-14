# 同一方法比较两个时间窗的生产每地址用量（排除"池子状态"混淆）
import re, sys, subprocess, collections, datetime
from bisect import bisect_right

MON = {'Nov': 11}
TSJ = re.compile(r'^(\w{3}) +(\d+) (\d\d):(\d\d):(\d\d)')
PS = {'7890': 'slot-1', '7892': 'slot-2', '7893': 'slot-3',
      '7894': 'slot-4', '7895': 'slot-5', '7896': 'slot-6'}
ROT = re.compile(r'\[ASIP\] rotate')
PROX = re.compile(r'\[PROXY\] OPENCODE \| (\S+) \|.*?url=http://127\.0\.0\.1:(\d+)')
KV = lambda l: dict(re.findall(r'(\w+)=(\S+)', l))


def load(since, until):
    out = subprocess.run(['journalctl', '-u', '9router', '--since', since,
                          '--until', until, '--no-pager'],
                         capture_output=True, text=True).stdout.splitlines()
    return out


def analyze(lines, label):
    slot_ev = collections.defaultdict(list)   # slot -> [(t, line, ip)]
    reqs = []
    for ln in lines:
        m = TSJ.match(ln)
        if not m:
            continue
        t = datetime.datetime(2026, MON[m.group(1)], int(m.group(2)), int(m.group(3)),
                              int(m.group(4)), int(m.group(5))).timestamp()
        if ROT.search(ln):
            d = KV(ln)
            slot, line, ip = d.get('to'), d.get('to_line'), d.get('to_ip')
            if slot and line:
                slot_ev[slot].append((t, line, ip if ip not in ('-', '?', None) else None))
            continue
        p = PROX.search(ln)
        if p:
            r = re.search(r'req=(\w+)', ln)
            reqs.append((t, PS.get(p.group(2)), r.group(1) if r else None))
    for v in slot_ev.values():
        v.sort(key=lambda x: (x[0], x[1], x[2] or ""))
    # 时段
    per = collections.defaultdict(int)
    unattr = 0
    for t, slot, rid in reqs:
        if not slot or slot not in slot_ev:
            unattr += 1
            continue
        ev = slot_ev[slot]
        i = bisect_right([x[0] for x in ev], t) - 1
        if i < 0:
            unattr += 1
            continue
        _, line, ip = ev[i]
        if not ip:
            unattr += 1
            continue
        per[(line, ip)] += 1
    ns = sorted(per.values())
    tot = sum(ns)
    if not ns:
        print('%s: 无数据' % label)
        return
    print('%-22s 请求 %5d  归属 %5d  未归属 %4d  地址 %4d  均值 %6.2f  中位 %3d  p90 %3d  max %3d' % (
        label, len(reqs), tot, unattr, len(ns), tot / len(ns),
        ns[len(ns) // 2], ns[int(len(ns) * .9)], ns[-1]))
    b = collections.Counter()
    for n in ns:
        b['1' if n == 1 else '2-9' if n < 10 else '10-49' if n < 50 else
          '50-149' if n < 150 else '>=150'] += 1
    print('    ' + '  '.join('%s:%d(%.0f%%)' % (k, b[k], 100.0 * b[k] / len(ns))
                             for k in ['1', '2-9', '10-49', '50-149', '>=150']))
    return ns


for since, until, label in [
    ('2026-11-12 21:00', '2026-11-12 23:00', '11-12 21:00-23:00'),
    ('2026-11-12 13:00', '2026-11-12 15:00', '11-12 13:00-15:00'),
    ('2026-11-13 17:00', '2026-11-13 19:00', '11-13 17:00-19:00'),
]:
    analyze(load(since, until), label)
