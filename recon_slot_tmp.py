#!/usr/bin/env python3
"""用权威时间线重建：每个槽位 / 每个出口 IP 的生命周期与承载量。

数据源（全部为生产日志，非推断）：
  journal  : [PROXY] OPENCODE | 模型 | ... url=http://127.0.0.1:PORT  —— 请求发往哪个端口
  daemon.log: ✓ <tag> 重启后出口=<ip>        —— 该线地址变更（唯一权威地址源）
              ↻ 槽位 slot-N 错误切换：A → B     —— 槽位换线
              ↻ 尝试重启 <tag>（<reason>）      —— 重启决策与原因
  gate.log  : ⇢ slot-N 报 <kind> ... 线=X 出口=IP —— (槽位,线,IP) 三元组现场快照
"""
import re, sys, collections, datetime
from bisect import bisect_right

DAEMON = '/home/administrator/dkk-projects/auto-switch-ip/daemon.log'
GATE = '/home/administrator/dkk-projects/auto-switch-ip/gate.log'
JOURNAL = sys.argv[1] if len(sys.argv) > 1 else '/tmp/j_now4.txt'

MON = {'Nov': 11}
TS_D = re.compile(r'^\[(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\]')
TS_J = re.compile(r'^(\w{3}) +(\d+) (\d\d):(\d\d):(\d\d)')
PORT_SLOT = {'7890': 'slot-1', '7892': 'slot-2', '7893': 'slot-3',
             '7894': 'slot-4', '7895': 'slot-5', '7896': 'slot-6'}


def t_daemon(m):
    return datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                             int(m.group(4)), int(m.group(5)), int(m.group(6))).timestamp()


def t_journal(m):
    return datetime.datetime(2026, MON[m.group(1)], int(m.group(2)),
                             int(m.group(3)), int(m.group(4)), int(m.group(5))).timestamp()


# ---------- 1. 线路地址时间线 ----------
line_ip = collections.defaultdict(list)      # tag -> [(t, ip)]
slot_line = collections.defaultdict(list)    # slot -> [(t, tag)]
restart_reason = collections.Counter()
restart_by_line = collections.Counter()
cur_restart_reason = None

with open(DAEMON, encoding='utf-8', errors='ignore') as f:
    for ln in f:
        m = TS_D.match(ln)
        if not m:
            continue
        t = t_daemon(m)
        if '尝试重启' in ln:
            r = re.search(r'↻ 尝试重启 (\S+)（(.+)）', ln)
            if r:
                restart_reason[r.group(2)] += 1
                restart_by_line[r.group(1)] += 1
            continue
        r = re.search(r'([✓✗]) (\S+) 重启后出口=(\S+)', ln)
        if r and (r.group(2).startswith('WARP') or r.group(2).startswith('WG')):
            ip = r.group(3) if r.group(1) == '✓' else None
            line_ip[r.group(2)].append((t, ip))
            continue
        r = re.search(r'↻ 槽位 (slot-\d) 错误切换：(\S+) → (\S+)', ln)
        if r:
            slot_line[r.group(1)].append((t, r.group(3)))

for v in line_ip.values():
    v.sort()
for v in slot_line.values():
    v.sort()

# ---------- 2. 用 gate.log 现场快照补 (slot,line,ip) ----------
# 只用来做「首段锚点」和「校验」，不覆盖重启时间线
gate_fix = collections.defaultdict(list)   # slot -> [(t, tag, ip)]
with open(GATE, encoding='utf-8', errors='ignore') as f:
    for ln in f:
        m = TS_D.match(ln)
        if not m or '⇢ slot-' not in ln:
            continue
        r = re.search(r'⇢ (slot-\d) 报 \S+.*?线=(\S+) 出口=(\S+)', ln)
        if not r:
            continue
        ip = r.group(3)
        gate_fix[r.group(1)].append((t_daemon(m), r.group(2),
                                     None if ip in ('-', '未知') else ip))
for v in gate_fix.values():
    v.sort()


def at(seq, t, idx=1):
    if not seq:
        return None
    i = bisect_right([x[0] for x in seq], t) - 1
    return seq[i][idx] if i >= 0 else None


# 槽位线归属：首个 gate 快照/切换事件之前，用 gate 快照里的线做锚点
for slot in PORT_SLOT.values():
    if slot not in slot_line and gate_fix.get(slot):
        slot_line[slot] = [(gate_fix[slot][0][0], gate_fix[slot][0][1])]
    elif slot_line.get(slot) and gate_fix.get(slot):
        if gate_fix[slot][0][0] < slot_line[slot][0][0]:
            slot_line[slot].insert(0, (gate_fix[slot][0][0], gate_fix[slot][0][1]))

# ---------- 3. 请求流 ----------
PROX = re.compile(r'\[PROXY\] OPENCODE \| (\S+) \|.*?url=http://127\.0\.0\.1:(\d+)')
reqs = []      # (t, slot, model)
for ln in open(JOURNAL, encoding='utf-8', errors='ignore'):
    m = TS_J.match(ln)
    if not m:
        continue
    p = PROX.search(ln)
    if not p:
        continue
    slot = PORT_SLOT.get(p.group(2))
    if slot:
        reqs.append((t_journal(m), slot, p.group(1)))
reqs.sort()
dstart = min(x[0] for v in list(line_ip.values()) for x in v) if line_ip else 0
reqs = [r for r in reqs if r[0] >= dstart - 5]

# ---------- 4. 每 (slot,line,ip) 实例 ----------
# 边界 = 槽位换线 或 该线地址变更
inst = collections.defaultdict(lambda: {'n': 0, 't0': None, 't1': None})
unattr = 0
for t, slot, model in reqs:
    line = at(slot_line.get(slot, []), t)
    if not line:
        unattr += 1
        continue
    ip = at(line_ip.get(line, []), t)
    if ip is None:
        unattr += 1
        continue
    k = (slot, line, ip)
    a = inst[k]
    a['n'] += 1
    a['t0'] = t if a['t0'] is None else min(a['t0'], t)
    a['t1'] = t if a['t1'] is None else max(a['t1'], t)

# 生命周期：从「该 IP 成为生效地址」到「它被换掉」
# 起点 = max(IP 上线时刻, 槽位挂上该线时刻)；终点 = 下一个边界
def lifetime(slot, line, ip):
    ips = line_ip.get(line, [])
    ts = [x[0] for x in ips]
    i = bisect_right(ts, 1e18)  # placeholder
    # 找到该 ip 的生效区间 [b, e)
    b = e = None
    for j, (tt, v) in enumerate(ips):
        if v == ip:
            b = tt
            for k2 in range(j + 1, len(ips)):
                if ips[k2][1] != ip:
                    e = ips[k2][0]
                    break
            if e is None:
                e = float('inf')
            break
    if b is None:
        return None
    # 与槽位挂线区间求交
    sl = slot_line.get(slot, [])
    sts = [x[0] for x in sl]
    k = bisect_right(sts, b) - 1
    slot_b = sl[k][0] if k >= 0 else b
    slot_e = sl[k + 1][0] if k + 1 < len(sl) else float('inf')
    b2, e2 = max(b, slot_b), min(e, slot_e)
    return (b2, e2)


rows = []
for (slot, line, ip), a in inst.items():
    lt = lifetime(slot, line, ip)
    lo = lt[0] if lt else a['t0']
    hi = lt[1] if lt else a['t1']
    span = (hi - lo) if hi != float('inf') else (a['t1'] - lo + 1)
    rows.append({'slot': slot, 'line': line, 'ip': ip, 'n': a['n'],
                 'life': span, 't0': a['t0'], 't1': a['t1']})

T0 = min(r[0] for r in reqs)
T1 = max(r[0] for r in reqs)
win = T1 - T0
print('=== 窗口 %.0f min（%s → %s）请求 %d，可归属 %d，未归属 %d'
      % (win / 60, datetime.datetime.fromtimestamp(T0).strftime('%H:%M'),
         datetime.datetime.fromtimestamp(T1).strftime('%H:%M'),
         len(reqs), len(reqs) - unattr, unattr))

print('\n=== 每个槽位')
print('%-8s %6s %7s %8s %10s %10s %10s' %
      ('slot', '线数', 'IP数', '请求', '发/分', '寿命中位', '寿命均值'))
for slot in sorted(PORT_SLOT.values()):
    rs = [r for r in rows if r['slot'] == slot]
    if not rs:
        continue
    n = sum(r['n'] for r in rs)
    lifes = sorted(r['life'] for r in rs)
    import statistics
    print('%-8s %6d %7d %8d %10.2f %9.0fs %9.0fs' %
          (slot, len({r['line'] for r in rs}), len(rs), n, n / (win / 60),
           lifes[len(lifes) // 2], statistics.mean(lifes)))

# 同一 IP 可能被两个槽位共用 —— 配额按 IP 计，必须合并
per_ip = collections.defaultdict(lambda: {'n': 0, 'life': 0, 'slots': set()})
for r in rows:
    p = per_ip[r['ip']]
    p['n'] += r['n']
    p['life'] = max(p['life'], r['life'])
    p['slots'].add(r['slot'])

ns = sorted(p['n'] for p in per_ip.values())
print('\n=== 出口 IP 维度（%d 个真实承载过流量的 IP，请求 %d）'
      % (len(ns), sum(ns)))
print('均值 %.2f  中位 %d  p75 %d  p90 %d  p99 %d  max %d'
      % (sum(ns) / len(ns), ns[len(ns) // 2], ns[int(len(ns) * .75)],
         ns[int(len(ns) * .9)], ns[int(len(ns) * .99)], ns[-1]))
b = collections.Counter()
for n in ns:
    b['1' if n == 1 else '2-4' if n <= 4 else '5-19' if n <= 19 else
      '20-49' if n <= 49 else '50-99' if n <= 99 else '100-149' if n < 150 else '>=150'] += 1
for k in ['1', '2-4', '5-19', '20-49', '50-99', '100-149', '>=150']:
    print('  %-8s %4d (%4.1f%%)' % (k, b[k], 100 * b[k] / len(ns)))

# 相关性
import statistics
xs = [p['life'] for p in per_ip.values()]
ys = [p['n'] for p in per_ip.values()]
mx, my = statistics.mean(xs), statistics.mean(ys)
cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)
r = cov / (statistics.pstdev(xs) * statistics.pstdev(ys) or 1)
print('\n寿命 ~ 用量相关系数 r = %.3f' % r)
sl = sorted(xs)
print('IP 寿命: 中位 %.0fs  p75 %.0fs  p90 %.0fs  max %.0fs  (均值 %.0fs)'
      % (sl[len(sl) // 2], sl[int(len(sl) * .75)], sl[int(len(sl) * .9)], sl[-1], mx))

# 用量 = 速率 × 寿命 的分解
rate = sum(ns) / (win / 60) / 6   # 每槽每分钟
print('\n每槽平均速率 %.2f 发/分；若寿命 = 中位 %.0fs，理论用量 = %.1f 发'
      % (rate, sl[len(sl) // 2], rate * sl[len(sl) // 2] / 60))
print('要打到 150 发，在 %.2f 发/分 下需要寿命 %.0f 秒 = %.1f 分钟'
      % (rate, 150 / rate * 60, 150 / rate))

print('\n=== 重启原因（全窗口 %d 次）' % sum(restart_reason.values()))
for k, v in restart_reason.most_common(8):
    print('  %-24s %6d (%4.1f%%)' % (k, v, 100 * v / sum(restart_reason.values())))

# 承载线的重启频率
serv_lines = collections.Counter()
for r in rows:
    serv_lines[r['line']] = max(serv_lines[r['line']], r['n'])
print('\n=== 有流量到达的线 (%d 条) 的重启次数' % len(serv_lines))
for line, _ in serv_lines.most_common(10):
    print('  %-20s 重启 %4d 次  用量 %3d 发' % (line, restart_by_line.get(line, 0),
                                            sum(r['n'] for r in rows if r['line'] == line)))
print('全部线路数 %d，总重启 %d 次（%.1f 次/分）'
      % (len(line_ip), sum(restart_by_line.values()),
         sum(restart_by_line.values()) / (win / 60)))
