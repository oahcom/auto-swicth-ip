import json, glob, os, statistics

rows = []
files = sorted(glob.glob('v7_result_*.json')) + sorted(glob.glob('v8_result_*.json')) + \
        ['c40_result.json', 'l1_result_60.json', 'par_sess_result.json', 'v6cap_result.json']
for f in files:
    if not os.path.exists(f):
        continue
    try:
        d = json.load(open(f))
    except Exception:
        continue
    if isinstance(d, dict):
        d = [d]
    for r in d:
        if not isinstance(r, dict):
            continue
        dth = (r.get('death') or [{}])[0] or {}
        rows.append(dict(f=f, tag=r.get('tag'), mode=r.get('mode'), conc=r.get('conc'),
                         ip=r.get('ip_start'), ok=r.get('ok'), err=r.get('err'),
                         death=dth.get('verdict'), n_sess=r.get('sessions'),
                         dur=r.get('dur_sec'), ip_stable=r.get('ip_stable')))

print('测试运行数', len(rows))
print('%-22s %-7s %-4s %-44s %-6s %-9s %s' % ('file', 'mode', 'conc', 'ip_start', 'ok', 'death', 'dur'))
for r in rows:
    print('%-22s %-7s %-4s %-44s %-6s %-9s %s' % (
        str(r['f'])[:22], str(r['mode'])[:7], str(r['conc']), str(r['ip'])[:44],
        str(r['ok']), str(r['death']), str(r['dur'])))

oks = sorted([r['ok'] for r in rows if isinstance(r['ok'], int)])
print()
print('=' * 60)
print('每 IP 实测发数分布（%d 次运行）' % len(oks))
if oks:
    import collections
    b = collections.Counter()
    for n in oks:
        b['0' if n == 0 else '1-9' if n < 10 else '10-39' if n < 40 else '40-99'
          if n < 100 else '100-199' if n < 200 else '200-299' if n < 300 else '>=300'] += 1
    for k in ['0', '1-9', '10-39', '40-99', '100-199', '200-299', '>=300']:
        print('  %-9s %3d 次 (%4.1f%%)' % (k, b[k], 100.0 * b[k] / len(oks)))
    print('  中位 %d 均值 %.0f max %d' % (oks[len(oks) // 2], sum(oks) / len(oks), oks[-1]))
    print('  >=150 的运行: %d 次 (%.0f%%)  >=200: %d 次' % (
        sum(1 for n in oks if n >= 150), 100.0 * sum(1 for n in oks if n >= 150) / len(oks),
        sum(1 for n in oks if n >= 200)))
