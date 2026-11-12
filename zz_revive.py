# -*- coding: utf-8 -*-
"""只读分析：生产上「一进生产就 429」的节点，后续判活是否又复活。
数据源 daemon.log*，不发起任何请求、不改任何状态。
"""
import re, os, datetime, collections, statistics

IPRE = r'2a09:[0-9a-fA-F:]+'
TS = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
FILES = ['daemon.log'] + ['daemon.log.%d' % i for i in range(1, 6)]

ev = []   # (ts, kind, ip, meta)
for f in FILES:
    if not os.path.exists(f):
        continue
    with open(f, errors='ignore') as fh:
        for line in fh:
            m = TS.match(line)
            if not m:
                continue
            try:
                t = datetime.datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
            except ValueError:
                continue
            if '报 rate429' in line and '⇢' in line:
                me = re.search(r'出口=(' + IPRE + r')', line)
                if not me:
                    continue
                mt = re.search(r'目标出口=(' + IPRE + r')', line)
                mv = re.search(r'实测=([^ ]+)', line)
                md = re.search(r'标脏后=([^ ，]+)', line)
                mr = re.search(r'报 rate429（([^）]+)）', line)
                ml = re.search(r'线=(\S+)', line)
                meta = {
                    'verdict': mv.group(1) if mv else '?',
                    'dirty': md.group(1) if md else '?',
                    'reason': mr.group(1) if mr else '?',
                    'line_name': ml.group(1) if ml else '?',
                    'target': mt.group(1) if mt else None,
                    'raw': line.strip(),
                }
                ev.append((t, 'R429', me.group(1), meta))
                # 该行同时给出「重试改走」的目标出口 —— 视为一次重新进生产
                if meta['target']:
                    ev.append((t, 'SELECT', meta['target'], {'src': 'retry_target'}))
            elif '⚡ 主槽位换线' in line:
                me = re.search(r'出口=(' + IPRE + r')', line)
                if me:
                    ev.append((t, 'ENTER', me.group(1), {'src': 'main_slot'}))
            elif '模型层: OK' in line and '✓' in line:
                me = re.search(r'出口 (' + IPRE + r')', line)
                if me:
                    ev.append((t, 'PROBE_OK', me.group(1), {'raw': line.strip()}))
            elif '✓' in line and '重启后出口=' in line:
                me = re.search(r'重启后出口=(' + IPRE + r')', line)
                if me:
                    ev.append((t, 'RESTART_OK', me.group(1), {'raw': line.strip()}))

ev.sort(key=lambda x: (x[0], x[1]))
by_ip = collections.defaultdict(list)
for t, kind, ip, meta in ev:
    by_ip[ip].append((t, kind, meta))

r429 = [(t, ip, m) for t, k, ip, m in ev if k == 'R429']
print('=== 0. 数据规模 ===')
print('日志文件      :', [f for f in FILES if os.path.exists(f)])
print('事件总数      :', len(ev), ' 独立出口 IP:', len(by_ip))
for k in ['R429', 'SELECT', 'ENTER', 'PROBE_OK', 'RESTART_OK']:
    print('  %-11s %d' % (k, sum(1 for e in ev if e[1] == k)))
if ev:
    print('时间跨度      : %s → %s' % (
        datetime.datetime.fromtimestamp(ev[0][0]).strftime('%m-%d %H:%M:%S'),
        datetime.datetime.fromtimestamp(ev[-1][0]).strftime('%m-%d %H:%M:%S')))

print()
print('=== 1. 429 那一刻，对报障 IP 的复测结论（当场复活） ===')
c = collections.Counter(m['verdict'] for t, ip, m in r429)
for k, n in c.most_common():
    print('  实测=%-8s %5d  %5.1f%%' % (k, n, 100.0 * n / max(1, len(r429))))
print('  标脏后=  :', dict(collections.Counter(m['dirty'] for t, ip, m in r429)))
print('  按 reason:', dict(collections.Counter(m['reason'] for t, ip, m in r429)))
print('  -- reason × 实测 --')
cc = collections.Counter((m['reason'], m['verdict']) for t, ip, m in r429)
for k, n in cc.most_common(12):
    print('     %-12s 实测=%-6s %d' % (k[0], k[1], n))

print()
print('=== 2. 「进生产 → 首次 429」间隔（SELECT/ENTER 之后同 IP 的下一次 R429） ===')
enter_gaps = []
for ip, tl in by_ip.items():
    r_times = [t for t, k, m in tl if k == 'R429']
    if not r_times:
        continue
    for t, k, m in tl:
        if k not in ('SELECT', 'ENTER'):
            continue
        nxt = None
        for rt in r_times:
            if rt >= t:
                nxt = rt
                break
        if nxt is not None and nxt - t <= 300:
            enter_gaps.append((nxt - t, ip, k))
if enter_gaps:
    gs = [g[0] for g in enter_gaps]
    print('  配对数 = %d  median=%.1fs  p90=%.1fs  min=%.1fs' % (
        len(gs), statistics.median(gs), sorted(gs)[int(len(gs) * .9)], min(gs)))
    buckets = [(0, 2), (2, 5), (5, 10), (10, 30), (30, 60), (60, 300)]
    for lo, hi in buckets:
        n = sum(1 for g in gs if lo <= g < hi)
        print('    %3d–%-3ds : %5d  %5.1f%%' % (lo, hi, n, 100.0 * n / len(gs)))
    print('  按来源:', dict(collections.Counter(g[2] for g in enter_gaps)))
    print('  ≤10s 占比: %.1f%%' % (100.0 * sum(1 for g in gs if g <= 10) / len(gs)))

print()
print('=== 3. 429 之后，同 IP 是否重新被判活 / 重新进生产（复活） ===')
revive_gaps = []          # 任意复活信号
reuse_gaps = []           # 严格：再次被选为出口/换线
revive_kind = collections.Counter()
for ip, tl in by_ip.items():
    for t, k, m in tl:
        if k != 'R429':
            continue
        for t2, k2, m2 in tl:
            if t2 <= t or t2 - t > 600:
                continue
            if k2 in ('PROBE_OK', 'RESTART_OK', 'SELECT', 'ENTER'):
                revive_gaps.append(t2 - t)
                revive_kind[k2] += 1
                break
        for t2, k2, m2 in tl:
            if t2 <= t or t2 - t > 600:
                continue
            if k2 in ('SELECT', 'ENTER'):
                reuse_gaps.append(t2 - t)
                break
if revive_gaps:
    print('  任意复活信号: %d/%d (%.1f%%)  median=%.1fs  ≤10s=%.1f%%  ≤60s=%.1f%%' % (
        len(revive_gaps), len(r429), 100.0 * len(revive_gaps) / len(r429),
        statistics.median(revive_gaps),
        100.0 * sum(1 for g in revive_gaps if g <= 10) / len(revive_gaps),
        100.0 * sum(1 for g in revive_gaps if g <= 60) / len(revive_gaps)))
    print('  复活信号类型:', dict(revive_kind))
if reuse_gaps:
    print('  严格复用(再次被选/换线): %d/%d (%.1f%%)  median=%.1fs  ≤10s=%.1f%%' % (
        len(reuse_gaps), len(r429), 100.0 * len(reuse_gaps) / len(r429),
        statistics.median(reuse_gaps),
        100.0 * sum(1 for g in reuse_gaps if g <= 10) / len(reuse_gaps)))

print()
print('=== 4. 429 → 当场 OK → 又 429 的循环（同 IP） ===')
ip429 = collections.Counter(ip for t, ip, m in r429)
print('  独立 IP 的 429 次数: median=%s max=%s  只429一次=%d  ≥3次=%d  ≥5次=%d' % (
    statistics.median(ip429.values()) if ip429 else 0,
    max(ip429.values()) if ip429 else 0,
    sum(1 for n in ip429.values() if n == 1),
    sum(1 for n in ip429.values() if n >= 3),
    sum(1 for n in ip429.values() if n >= 5)))
# 交替：R429 与 OK 类信号相邻
cycles = 0
for ip, tl in by_ip.items():
    seq = [k for t, k, m in tl]
    for i in range(len(seq) - 1):
        if seq[i] == 'R429' and seq[i + 1] in ('PROBE_OK', 'RESTART_OK', 'ENTER', 'SELECT'):
            cycles += 1
print('  R429 紧接复活信号 的相邻对: %d' % cycles)

print()
print('=== 5. 典型 IP 生命周期（挑 429 次数最多的 3 个） ===')
for ip, n in ip429.most_common(3):
    print('--', ip, ' 429×%d' % n)
    for t, k, m in by_ip[ip][:14]:
        ts = datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S')
        extra = ''
        if k == 'R429':
            extra = ' 实测=%s 标脏后=%s reason=%s' % (m['verdict'], m['dirty'], m['reason'])
        elif k in ('PROBE_OK', 'RESTART_OK'):
            extra = '  ' + m['raw'][:110]
        print('   %-8s %-11s%s' % (ts, k, extra))

print()
print('=== 6. 「进生产 ≤10s 就 429」且当场判活 OK 的实例（前 8 条） ===')
shown = 0
for dt, ip, k in sorted(enter_gaps)[:400]:
    if dt > 10:
        break
    for t, kk, m in by_ip[ip]:
        if kk == 'R429' and m['verdict'] == 'OK':
            print('   +%.0fs  ip=%-38s 实测=%s 线=%s' % (
                dt, ip, m['verdict'], m['line_name']))
            shown += 1
            break
    if shown >= 8:
        break
print('DONE')
