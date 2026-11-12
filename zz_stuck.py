# -*- coding: utf-8 -*-
"""429 之后同一 IP 是否「没被切走、继续挨打」。只读。"""
import re, os, datetime, collections, statistics

IPRE = r'2a09:[0-9a-fA-F:]+'
TS = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
FILES = ['daemon.log'] + ['daemon.log.%d' % i for i in range(1, 6)]

by_ip = collections.defaultdict(list)
cnt = collections.Counter()
for f in FILES:
    if not os.path.exists(f):
        continue
    with open(f, errors='ignore') as fh:
        for line in fh:
            m = TS.match(line)
            if not m:
                continue
            if '⇢' in line and '报 rate429' in line:
                cur = re.search(r'出口=(' + IPRE + r')', line)
                ro = re.search(r'报 rate429（([^）]+)）', line)
                if cur:
                    try:
                        t = datetime.datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
                    except ValueError:
                        continue
                    by_ip[cur.group(1)].append((t, ro.group(1) if ro else '?'))
                    cnt['r429'] += 1
                    cnt['reason_' + (ro.group(1) if ro else '?')] += 1
            elif '暂不换线' in line:
                cnt['no_switch'] += 1
            elif '报 rate429' in line and 'coalesced' in line:
                cnt['coalesced_other'] += 1

print('=== 计数 ===')
for k, v in cnt.most_common():
    print('  %-22s %d' % (k, v))

print()
print('=== 1. 429 后同一 IP 在 90s 短冷却窗口内再次 429 ===')
again, gaps = 0, []
for ip, seq in by_ip.items():
    seq.sort()
    for i, (t, r) in enumerate(seq):
        for t2, r2 in seq[i + 1:]:
            if t2 - t > 90:
                break
            again += 1
            gaps.append(t2 - t)
            break
print('  %d/%d = %.1f%% 的 429 后，同一 IP 在 90s 内又 429 一次' % (
    again, cnt['r429'], 100.0 * again / max(1, cnt['r429'])))
if gaps:
    print('  间隔中位=%.0fs  ≤5s=%.1f%%  ≤10s=%.1f%%  ≤30s=%.1f%%' % (
        statistics.median(gaps),
        100.0 * sum(1 for g in gaps if g <= 5) / len(gaps),
        100.0 * sum(1 for g in gaps if g <= 10) / len(gaps),
        100.0 * sum(1 for g in gaps if g <= 30) / len(gaps)))

print()
print('=== 2. 同一 IP 的「429 连续簇」（相邻间隔 ≤30s 视为同一簇） ===')
clusters = []
for ip, seq in by_ip.items():
    seq.sort()
    cur = []
    for t, r in seq:
        if cur and t - cur[-1] > 30:
            clusters.append(cur)
            cur = []
        cur.append(t)
    if cur:
        clusters.append(cur)
lens = [len(c) for c in clusters]
print('  簇数=%d  长度中位=%s  max=%s  ≥3发=%d  ≥5发=%d  ≥10发=%d' % (
    len(clusters), statistics.median(lens), max(lens),
    sum(1 for n in lens if n >= 3), sum(1 for n in lens if n >= 5),
    sum(1 for n in lens if n >= 10)))
big = [c for c in clusters if len(c) >= 5]
if big:
    print('  最长簇持续 %.0fs' % max(c[-1] - c[0] for c in big))
    c = max(big, key=len)
    print('  最长簇样本: %d 发, 跨 %.0fs, 从 %s' % (
        len(c), c[-1] - c[0],
        datetime.datetime.fromtimestamp(c[0]).strftime('%m-%d %H:%M:%S')))
print('DONE')
