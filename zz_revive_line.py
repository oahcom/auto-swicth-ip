# -*- coding: utf-8 -*-
"""按 WARP 线名(tag)追踪：一进生产就 429 的节点，后续判活是否又复活。
只读 daemon.log*，不发请求、不改状态。
"""
import re, os, datetime, collections, statistics

IPRE = r'2a09:[0-9a-fA-F:]+'
TS = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
FILES = ['daemon.log'] + ['daemon.log.%d' % i for i in range(1, 6)]

ev = []  # (ts, kind, tag, meta)
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
            # 1) 报障行
            if '⇢' in line and '报 ' in line and '线=' in line:
                reason = re.search(r'报 (\w+)（([^）]+)）', line)
                tag = re.search(r'线=(\S+)', line)
                tgt = re.search(r'目标线=(\S+)', line)
                vb = re.search(r'实测=([^ ]+)', line)
                va = re.search(r'标脏后=([^ ，]+)', line)
                ip = re.search(r'出口=(' + IPRE + r')', line)
                if not (reason and tag):
                    continue
                if reason.group(1) != 'rate429':
                    continue
                meta = {'before': vb.group(1) if vb else '?',
                        'after': va.group(1) if va else '?',
                        'reason_out': reason.group(2),
                        'ip': ip.group(1) if ip else '?',
                        'raw': line.strip()}
                ev.append((t, 'R429', tag.group(1), meta))
                if tgt:
                    ev.append((t, 'SELECT', tgt.group(1), {'src': 'retry_target'}))
            # 2) 模型层判活 OK（带线名与 IP）
            elif '✓' in line and '模型层: OK' in line:
                tag = re.search(r'✓ (\S+) 出口 ', line)
                if tag:
                    ev.append((t, 'PROBE_OK', tag.group(1), {'raw': line.strip()}))
            # 3) 重启后出口（线复活）
            elif '✓' in line and '重启后出口=' in line:
                tag = re.search(r'✓ (\S+) 重启后出口=', line)
                if tag:
                    ev.append((t, 'RESTART_OK', tag.group(1), {'raw': line.strip()}))
            # 4) 换线时实测 OK（新线被判活）
            elif '↻ 槽位' in line and '实测 OK' in line:
                tag = re.search(r'→ ([^，\s（]+)，实测 OK', line)
                if tag:
                    ev.append((t, 'SWITCH_OK', tag.group(1), {'raw': line.strip()}))
            # 5) 主槽位换线（新线进生产）
            elif '⚡ 主槽位换线' in line:
                tag = re.search(r'→ ([^，\s（]+)（出口=', line)
                if tag:
                    ev.append((t, 'ENTER', tag.group(1), {'raw': line.strip()}))

ev.sort(key=lambda x: (x[0], x[1]))
by_tag = collections.defaultdict(list)
for t, k, tag, m in ev:
    by_tag[tag].append((t, k, m))

r429 = [(t, tag, m) for t, k, tag, m in ev if k == 'R429']
OK_KINDS = ('PROBE_OK', 'RESTART_OK', 'SWITCH_OK')
ENTER_KINDS = ('SELECT', 'ENTER')

print('=== 0. 规模（按线名） ===')
print('事件=%d 独立线=%d  R429=%d' % (len(ev), len(by_tag), len(r429)))
for k in ['R429', 'SELECT', 'ENTER', 'PROBE_OK', 'RESTART_OK', 'SWITCH_OK']:
    print('  %-11s %d' % (k, sum(1 for e in ev if e[1] == k)))

print()
print('=== 1. 事发前判活结论（实测=verdict_before）与标脏后台账 ===')
print('  实测=   ', dict(collections.Counter(m['before'] for t, tag, m in r429)))
print('  标脏后= ', dict(collections.Counter(m['after'] for t, tag, m in r429)))
both = collections.Counter((m['before'], m['after']) for t, tag, m in r429)
print('  before×after top:')
for k, n in both.most_common(8):
    print('    %-6s → %-6s %d' % (k[0], k[1], n))

print()
print('=== 2. 「进生产 → 首次 429」间隔（按线名） ===')
gaps = []
for tag, tl in by_tag.items():
    rts = [t for t, k, m in tl if k == 'R429']
    if not rts:
        continue
    for t, k, m in tl:
        if k not in ENTER_KINDS:
            continue
        nxt = next((rt for rt in rts if rt >= t and rt - t <= 300), None)
        if nxt is not None:
            gaps.append((nxt - t, tag, k))
if gaps:
    gs = [g[0] for g in gaps]
    print('  配对=%d median=%.1fs p90=%.1fs  ≤5s=%.1f%%  ≤10s=%.1f%%  ≤30s=%.1f%%' % (
        len(gs), statistics.median(gs), sorted(gs)[int(len(gs) * .9)],
        100.0 * sum(1 for g in gs if g <= 5) / len(gs),
        100.0 * sum(1 for g in gs if g <= 10) / len(gs),
        100.0 * sum(1 for g in gs if g <= 30) / len(gs)))
    print('  分桶:', end=' ')
    for lo, hi in [(0, 2), (2, 5), (5, 10), (10, 30), (30, 60), (60, 300)]:
        print('%d-%ds:%d' % (lo, hi, sum(1 for g in gs if lo <= g < hi)), end='  ')
    print()

print()
print('=== 3. 429 之后，同一条线是否又被判活 OK / 重新进生产（复活） ===')
rev = collections.defaultdict(list)
reuse = collections.defaultdict(list)
for tag, tl in by_tag.items():
    for t, k, m in tl:
        if k != 'R429':
            continue
        nxt_ok = next(((t2 - t, k2) for t2, k2, m2 in tl
                       if t2 > t and t2 - t <= 1800 and k2 in OK_KINDS), None)
        nxt_enter = next(((t2 - t, k2) for t2, k2, m2 in tl
                          if t2 > t and t2 - t <= 1800 and k2 in ENTER_KINDS), None)
        if nxt_ok:
            rev[tag].append(nxt_ok)
        if nxt_enter:
            reuse[tag].append(nxt_enter)
flat_ok = [x[0] for v in rev.values() for x in v]
flat_ent = [x[0] for v in reuse.values() for x in v]
print('  后续判活 OK : %d/%d (%.1f%%) median=%.0fs  ≤30s=%.1f%%  ≤120s=%.1f%%  ≤600s=%.1f%%' % (
    len(flat_ok), len(r429), 100.0 * len(flat_ok) / len(r429),
    statistics.median(flat_ok) if flat_ok else -1,
    100.0 * sum(1 for g in flat_ok if g <= 30) / len(flat_ok) if flat_ok else 0,
    100.0 * sum(1 for g in flat_ok if g <= 120) / len(flat_ok) if flat_ok else 0,
    100.0 * sum(1 for g in flat_ok if g <= 600) / len(flat_ok) if flat_ok else 0))
print('    复活类型:', dict(collections.Counter(x[1] for v in rev.values() for x in v)))
print('  后续重新进生产: %d/%d (%.1f%%) median=%.0fs  ≤30s=%.1f%%' % (
    len(flat_ent), len(r429), 100.0 * len(flat_ent) / len(r429),
    statistics.median(flat_ent) if flat_ent else -1,
    100.0 * sum(1 for g in flat_ent if g <= 30) / len(flat_ent) if flat_ent else 0))

print()
print('=== 4. 复活之后是否马上又 429（循环） ===')
cyc = []
for tag, tl in by_tag.items():
    rts = [t for t, k, m in tl if k == 'R429']
    for t, k, m in tl:
        if k not in OK_KINDS + ENTER_KINDS:
            continue
        nxt = next((rt for rt in rts if rt > t and rt - t <= 300), None)
        if nxt is not None and any(rt <= t for rt in rts):
            cyc.append((nxt - t, tag, k))
if cyc:
    gs = [c[0] for c in cyc]
    print('  复活后再 429 配对=%d median=%.1fs  ≤10s=%.1f%%  ≤30s=%.1f%%' % (
        len(gs), statistics.median(gs),
        100.0 * sum(1 for g in gs if g <= 10) / len(gs),
        100.0 * sum(1 for g in gs if g <= 30) / len(gs)))

print()
print('=== 5. 单条线 429 次数 / 复活次数分布 ===')
n429 = collections.Counter(tag for t, tag, m in r429)
nok = collections.Counter(tag for tag, v in rev.items() for _ in v)
print('  429 次数: median=%s max=%s  只1次=%d  ≥5次=%d  ≥20次=%d' % (
    statistics.median(n429.values()), max(n429.values()),
    sum(1 for n in n429.values() if n == 1),
    sum(1 for n in n429.values() if n >= 5),
    sum(1 for n in n429.values() if n >= 20)))
print('  独立线=%d  既 429 又复活过的线=%d (%.1f%%)' % (
    len(n429), sum(1 for t in n429 if t in nok),
    100.0 * sum(1 for t in n429 if t in nok) / max(1, len(n429))))

print()
print('=== 6. 典型线 timeline（429 次数 top 3，截断 16 条） ===')
for tag, n in n429.most_common(3):
    print('--', tag, ' 429×%d 复活×%d' % (n, nok.get(tag, 0)))
    for t, k, m in by_tag[tag][:16]:
        ts = datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S')
        extra = ''
        if k == 'R429':
            extra = ' 判活前=%s 标脏后=%s ip=%s' % (m['before'], m['after'], m['ip'][:26])
        print('   %-8s %-11s%s' % (ts, k, extra))
print()
print('=== 7. 标脏后=OK vs =未探：后续复活速度对照 ===')
groups = collections.defaultdict(list)
for tag, tl in by_tag.items():
    for t, k, m in tl:
        if k != 'R429':
            continue
        g = m['after']
        nxt_ok = next((t2 - t for t2, k2, m2 in tl
                       if t2 > t and t2 - t <= 1800 and k2 in OK_KINDS), None)
        nxt_ent = next((t2 - t for t2, k2, m2 in tl
                        if t2 > t and t2 - t <= 1800 and k2 in ENTER_KINDS), None)
        groups[g].append((nxt_ok, nxt_ent))
for g in ['OK', '未探', 'quota']:
    vals = groups.get(g) or []
    if not vals:
        continue
    ok = [v[0] for v in vals if v[0] is not None]
    ent = [v[1] for v in vals if v[1] is not None]
    print('  标脏后=%-4s n=%-5d 判活OK %.0f%%(中位 %s s)  重新进生产 %.0f%%(中位 %s s, ≤30s %.1f%%)' % (
        g, len(vals),
        100.0 * len(ok) / len(vals), ('%.0f' % statistics.median(ok)) if ok else '-',
        100.0 * len(ent) / len(vals), ('%.0f' % statistics.median(ent)) if ent else '-',
        100.0 * sum(1 for x in ent if x <= 30) / len(ent) if ent else 0))
print('DONE')
