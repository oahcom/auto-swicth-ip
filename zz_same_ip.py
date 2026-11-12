# -*- coding: utf-8 -*-
"""判定：429 之后「复活」的是同一个 IP，还是线重启换了新 IP。
只读 daemon.log*，不发请求、不改状态。
"""
import re, os, datetime, collections, statistics

IPRE = r'2a09:[0-9a-fA-F:]+'
TS = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
FILES = ['daemon.log'] + ['daemon.log.%d' % i for i in range(1, 6)]


def p64(ip):
    return ':'.join(ip.split(':')[:4])


# (ts, kind, ip, tag, meta)
ev = []
unclassified = collections.Counter()
for f in FILES:
    if not os.path.exists(f):
        continue
    with open(f, errors='ignore') as fh:
        for line in fh:
            m = TS.match(line)
            if not m:
                continue
            if not re.search(IPRE, line):
                continue
            try:
                t = datetime.datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
            except ValueError:
                continue
            g = lambda p: (re.search(p, line).group(1) if re.search(p, line) else None)
            ipx = lambda p: (re.search(p.replace('<IP>', IPRE), line).group(1)
                             if re.search(p.replace('<IP>', IPRE), line) else None)

            if '⇢' in line and '报 rate429' in line:
                cur = ipx(r'出口=(<IP>)')
                rep = ipx(r'报障IP=(<IP>)')
                tgt = ipx(r'目标出口=(<IP>)')
                tag = g(r'线=(\S+)')
                if not cur:
                    continue
                meta = {'report': rep, 'before': g(r'实测=([^ ]+)') or '?',
                        'after': g(r'标脏后=([^ ，]+)') or '?',
                        'reason_out': g(r'报 rate429（([^）]+)）') or '?',
                        'tag': tag, 'raw': line.strip()}
                ev.append((t, 'R429', cur, tag, meta))
                if tgt:
                    ev.append((t, 'TARGET', tgt, g(r'目标线=(\S+)'), {'src': 'retry'}))
            elif '⚡ 主槽位换线' in line:
                cur = ipx(r'出口=(<IP>)')
                if cur:
                    ev.append((t, 'ENTER', cur, g(r'→ ([^，\s（]+)（出口='), {}))
            elif '✓' in line and '模型层: OK' in line:
                cur = ipx(r'出口 (<IP>)')
                if cur:
                    ev.append((t, 'PROBE_OK', cur, g(r'✓ (\S+) 出口 '), {}))
            elif '✓' in line and '重启后出口=' in line:
                cur = ipx(r'重启后出口=(<IP>)')
                if cur:
                    ev.append((t, 'RESTART_OK', cur, g(r'✓ (\S+) 重启后出口='), {}))
            elif '复测 OK' in line and '不打冷却' in line:
                cur = ipx(r'出口 (<IP>)')
                if cur:
                    ev.append((t, 'RECHECK_OK', cur, None, {}))
            elif '5/5 出口级错误' in line or '4/5 出口级错误' in line:
                cur = ipx(r'\((<IP>)\)')
                if cur:
                    ev.append((t, 'ERR_LINE', cur, None, {}))
            else:
                unclassified[line.strip()[:60]] += 1

ev.sort(key=lambda x: (x[0], x[1]))
by_ip = collections.defaultdict(list)
for t, k, ip, tag, m in ev:
    by_ip[ip].append((t, k, tag, m))
by_tag = collections.defaultdict(list)
for t, k, ip, tag, m in ev:
    if tag:
        by_tag[tag].append((t, k, ip, m))

r429 = [(t, ip, tag, m) for t, k, ip, tag, m in ev if k == 'R429']
OK = ('PROBE_OK', 'RESTART_OK', 'RECHECK_OK')
USE = ('TARGET', 'ENTER')

print('=== 0. 规模与解析覆盖 ===')
print('事件=%d 独立IP=%d R429=%d' % (len(ev), len(by_ip), len(r429)))
for k in ['R429', 'TARGET', 'ENTER', 'PROBE_OK', 'RESTART_OK', 'RECHECK_OK', 'ERR_LINE']:
    print('  %-11s %d' % (k, sum(1 for e in ev if e[1] == k)))
print('  未分类含IP行(前3):', unclassified.most_common(3))
print('  出口 != 报障IP 的报障行: %d / %d' % (
    sum(1 for t, ip, tag, m in r429 if m['report'] and m['report'] != ip), len(r429)))

print()
print('=== 1. 【同 IP 复活】429 之后，同一个 IP 又被判活 OK ===')
same_ok, any_ok = [], []
for ip, tl in by_ip.items():
    for t, k, tag, m in tl:
        if k != 'R429':
            continue
        nxt = next(((t2 - t, k2) for t2, k2, tg2, m2 in tl
                    if t2 > t and t2 - t <= 1800 and k2 in OK), None)
        if nxt:
            same_ok.append((nxt[0], nxt[1], ip, m['after']))
if same_ok:
    gs = [x[0] for x in same_ok]
    print('  同IP又被判活OK: %d/%d = %.2f%%  中位=%.0fs  ≤30s=%.1f%%  ≤120s=%.1f%%' % (
        len(same_ok), len(r429), 100.0 * len(same_ok) / len(r429),
        statistics.median(gs), 100.0 * sum(1 for x in gs if x <= 30) / len(gs),
        100.0 * sum(1 for x in gs if x <= 120) / len(gs)))
    print('  复活类型:', dict(collections.Counter(x[1] for x in same_ok)))
    print('  按标脏后状态:', dict(collections.Counter(x[3] for x in same_ok)))

print()
print('=== 2. 【同 IP 重新进生产】429 之后，同一 IP 又被选为出口/重试目标/又承载 429 ===')
same_use = []
for ip, tl in by_ip.items():
    for t, k, tag, m in tl:
        if k != 'R429':
            continue
        for t2, k2, tg2, m2 in tl:
            if t2 <= t or t2 - t > 1800:
                continue
            if k2 in USE or k2 == 'R429':
                same_use.append((t2 - t, k2, ip, m['after']))
                break
if same_use:
    gs = [x[0] for x in same_use]
    print('  同IP再次被使用: %d/%d = %.2f%%  中位=%.0fs  ≤30s=%.1f%%  ≤120s=%.1f%%' % (
        len(same_use), len(r429), 100.0 * len(same_use) / len(r429),
        statistics.median(gs), 100.0 * sum(1 for x in gs if x <= 30) / len(gs),
        100.0 * sum(1 for x in gs if x <= 120) / len(gs)))
    print('  使用类型:', dict(collections.Counter(x[1] for x in same_use)))
    print('  按标脏后状态:', dict(collections.Counter(x[3] for x in same_use)))

print()
print('=== 3. 【关键】线重启前后 IP 是否相同（R429 → 之后最近 RESTART_OK） ===')
pair_same = pair_diff = 0
p64_same = p64_diff = 0
diffs = []
for tag, tl in by_tag.items():
    for t, k, ip, m in tl:
        if k != 'R429':
            continue
        nxt = next(((t2, ip2) for t2, k2, ip2, m2 in tl
                    if t2 > t and t2 - t <= 1800 and k2 == 'RESTART_OK'), None)
        if not nxt:
            continue
        t2, ip2 = nxt
        if ip2 == ip:
            pair_same += 1
        else:
            pair_diff += 1
            diffs.append(t2 - t)
            if p64(ip2) == p64(ip):
                p64_same += 1
            else:
                p64_diff += 1
tot = pair_same + pair_diff
print('  配对数=%d  重启后IP完全相同=%d (%.1f%%)  IP变了=%d (%.1f%%)' % (
    tot, pair_same, 100.0 * pair_same / max(1, tot), pair_diff,
    100.0 * pair_diff / max(1, tot)))
print('  IP变了的那批: 仍在同一 /64 = %d (%.1f%%)  换到别的 /64 = %d (%.1f%%)' % (
    p64_same, 100.0 * p64_same / max(1, pair_diff),
    p64_diff, 100.0 * p64_diff / max(1, pair_diff)))
if diffs:
    print('  重启间隔中位=%.0fs' % statistics.median(diffs))

print()
print('=== 4. 标脏后=OK vs 未探：【同 IP】复活速度对照 ===')
grp = collections.defaultdict(list)
for ip, tl in by_ip.items():
    for t, k, tag, m in tl:
        if k != 'R429':
            continue
        o = next((t2 - t for t2, k2, tg2, m2 in tl
                  if t2 > t and t2 - t <= 1800 and k2 in OK), None)
        u = next((t2 - t for t2, k2, tg2, m2 in tl
                  if t2 > t and t2 - t <= 1800 and (k2 in USE or k2 == 'R429')), None)
        grp[m['after']].append((o, u))
for g in ['OK', '未探', 'quota']:
    v = grp.get(g) or []
    if not v:
        continue
    o = [x[0] for x in v if x[0] is not None]
    u = [x[1] for x in v if x[1] is not None]
    print('  标脏后=%-4s n=%-5d 同IP判活OK %5.1f%% (中位 %s s)  同IP再被用 %5.1f%% (中位 %s s, ≤30s %.1f%%)' % (
        g, len(v), 100.0 * len(o) / len(v),
        ('%.0f' % statistics.median(o)) if o else '-',
        100.0 * len(u) / len(v), ('%.0f' % statistics.median(u)) if u else '-',
        100.0 * sum(1 for x in u if x <= 30) / len(u) if u else 0))

print()
print('=== 5. 真·同IP复活案例（429 → 同IP判活OK → 同IP再被用）前 6 条 ===')
shown = 0
for ip, tl in by_ip.items():
    seq = [(t, k, tag, m) for t, k, tag, m in tl]
    for i, (t, k, tag, m) in enumerate(seq):
        if k != 'R429':
            continue
        ok = next(((t2, k2) for t2, k2, tg2, m2 in seq[i + 1:]
                   if t2 - t <= 600 and k2 in OK), None)
        use = next(((t2, k2) for t2, k2, tg2, m2 in seq[i + 1:]
                    if t2 - t <= 600 and (k2 in USE or k2 == 'R429')), None)
        if ok and use:
            fmt = lambda x: datetime.datetime.fromtimestamp(x).strftime('%H:%M:%S')
            print('  ip=%s' % ip)
            print('    %s R429(标脏后=%s 线=%s)' % (fmt(t), m['after'], tag))
            print('    %s +%.0fs %s  <-- 同IP判活OK' % (fmt(ok[0]), ok[0] - t, ok[1]))
            print('    %s +%.0fs %s  <-- 同IP又被使用' % (fmt(use[0]), use[0] - t, use[1]))
            shown += 1
            break
    if shown >= 6:
        break
if not shown:
    print('  无')
print('DONE')
