# 有流量的地址，在其 /64 内是第几个被铸出来的；对照组：测试运行命中的地址族
import re, json, collections, datetime, ipaddress
from bisect import bisect_right

TSD = re.compile(r'^\[(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\]')
TSJ = re.compile(r'^(\w{3}) +(\d+) (\d\d):(\d\d):(\d\d)')
MON = {'Nov': 11}
td = lambda m: datetime.datetime(*[int(m.group(i)) for i in range(1, 7)]).timestamp()
tj = lambda m: datetime.datetime(2026, MON[m.group(1)], int(m.group(2)), int(m.group(3)),
                                 int(m.group(4)), int(m.group(5))).timestamp()
PS = {'7890': 'slot-1', '7892': 'slot-2', '7893': 'slot-3',
      '7894': 'slot-4', '7895': 'slot-5', '7896': 'slot-6'}

line_ip = collections.defaultdict(list)
slot_line = collections.defaultdict(list)
for ln in open('daemon.log', encoding='utf-8', errors='ignore'):
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
        line_ip[r.group(2)].append((t, None if r.group(1) == '✗' or r.group(3) == '探测失败'
                                    else r.group(3)))
for ln in open('gate.log', encoding='utf-8', errors='ignore'):
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
    o = []
    for t, ip in line_ip[tag]:
        if not o or o[-1][1] != ip:
            o.append((t, ip))
    line_ip[tag] = o

cnt = collections.Counter()
for ln in open('/tmp/j_win.txt', encoding='utf-8', errors='ignore'):
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
        cnt[ip] += 1

led = json.load(open('.warp_ip_used.json'))['used']
per64 = collections.defaultdict(list)
for ip, v in led.items():
    try:
        k = str(ipaddress.ip_network(ip + '/64', strict=False))
    except Exception:
        continue
    per64[k].append((v.get('first_seen') or 0, ip))
order = {}
for k, rows in per64.items():
    rows.sort()
    for i, (_, ip) in enumerate(rows, 1):
        order[ip] = (i, len(rows))

print('台账地址 %d，/64 数 %d' % (len(order), len(per64)))
hit = [ip for ip in cnt if ip in order]
print('有流量的地址 %d，其中能在台账里定位 %d' % (len(cnt), len(hit)))
b = collections.Counter()
for ip in hit:
    o, n = order[ip]
    b[1 if o == 1 else 2 if o <= 3 else 4 if o <= 6 else 7 if o <= 10 else 11 if o <= 15 else 99] += 1
lab = {1: '第1个', 2: '第2-3个', 4: '第4-6个', 7: '第7-10个', 11: '第11-15个', 99: '第16个以后'}
print('\n有流量地址在 /64 内的铸出次序：')
for k in [1, 2, 4, 7, 11, 99]:
    print('  %-10s %3d 个 (%4.1f%%)' % (lab[k], b[k], 100.0 * b[k] / max(len(hit), 1)))

# 每个 /64 平均铸多少、出多少有流量的
n64 = len(per64)
with_traffic = len({str(ipaddress.ip_network(ip + '/64', strict=False)) for ip in hit})
print('\n出过承载流量的 /64: %d / %d = %.1f%%' % (with_traffic, n64, 100.0 * with_traffic / n64))
print('/64 平均铸址 %.1f 个，平均出承载 %d 个' % (
    len(order) / n64, len(hit) / max(with_traffic, 1)))
