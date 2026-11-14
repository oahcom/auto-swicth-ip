# 生产 429 落在地址生命的哪一段（同小时测试单地址可打 272 发）
import re, subprocess, collections, datetime
from bisect import bisect_right

MON = {'Nov': 11}
TSJ = re.compile(r'^(\w{3}) +(\d+) (\d\d):(\d\d):(\d\d)')
PS = {'7890': 'slot-1', '7892': 'slot-2', '7893': 'slot-3',
      '7894': 'slot-4', '7895': 'slot-5', '7896': 'slot-6'}
KV = lambda l: dict(re.findall(r'(\w+)=(\S+)', l))


def analyze(since, until, label):
    lines = subprocess.run(['journalctl', '-u', '9router', '--since', since,
                            '--until', until, '--no-pager'],
                           capture_output=True, text=True).stdout.splitlines()
    slot_ev = collections.defaultdict(list)
    reqs = []
    e429 = []
    for ln in lines:
        m = TSJ.match(ln)
        if not m:
            continue
        t = datetime.datetime(2026, MON[m.group(1)], int(m.group(2)), int(m.group(3)),
                              int(m.group(4)), int(m.group(5))).timestamp()
        if '[ASIP] rotate' in ln:
            d = KV(ln)
            slot, line, ip = d.get('to'), d.get('to_line'), d.get('to_ip')
            if slot and line:
                slot_ev[slot].append((t, line, ip if ip not in ('-', '?', None) else None))
            continue
        p = re.search(r'\[PROXY\] OPENCODE \| \S+ \|.*?url=http://127\.0\.0\.1:(\d+)', ln)
        if p:
            reqs.append((t, PS.get(p.group(1))))
            continue
        p = re.search(r'429 -> accounted on http://127\.0\.0\.1:(\d+)', ln)
        if p:
            e429.append((t, PS.get(p.group(1))))
    for v in slot_ev.values():
        v.sort(key=lambda x: (x[0], x[1], x[2] or ""))

    def ip_at(slot, t):
        ev = slot_ev.get(slot) or []
        if not ev:
            return None
        i = bisect_right([x[0] for x in ev], t) - 1
        return ev[i][2] if i >= 0 else None

    # 地址 -> 按时间排的请求时刻
    by_ip = collections.defaultdict(list)
    for t, slot in reqs:
        ip = ip_at(slot, t)
        if ip:
            by_ip[ip].append(t)
    for v in by_ip.values():
        v.sort()

    hist = collections.Counter()
    served_before = []
    for t, slot in e429:
        ip = ip_at(slot, t)
        if not ip or ip not in by_ip:
            hist['地址未知'] += 1
            continue
        n = sum(1 for x in by_ip[ip] if x < t)
        served_before.append(n)
        hist['0（首发就 429）' if n == 0 else '1' if n == 1 else '2-3' if n <= 3
              else '4-10' if n <= 10 else '11-40' if n <= 40 else '41+'] += 1
    tot = sum(hist.values())
    print('\n=== %s  请求 %d  429 记账 %d' % (label, len(reqs), len(e429)))
    for k in ['0（首发就 429）', '1', '2-3', '4-10', '11-40', '41+', '地址未知']:
        if hist[k]:
            print('  429 前该地址已服务 %-14s %4d 次 (%4.1f%%)' % (k, hist[k], 100.0 * hist[k] / tot))
    if served_before:
        s = sorted(served_before)
        print('  中位 %d 发，均值 %.1f，max %d' % (s[len(s) // 2], sum(s) / len(s), s[-1]))


analyze('2026-11-12 21:00', '2026-11-12 23:00', '11-12 21:00-23:00（测试同小时）')
analyze('2026-11-13 16:40', '2026-11-13 19:00', '11-13 16:40-19:00（今天）')
