# -*- coding: utf-8 -*-
import re, os, collections
FILES = ["daemon.log"] + ["daemon.log.%d" % i for i in range(1, 8)]
FILES = [f for f in FILES if os.path.exists(f)]
lines = []
for f in FILES:
    for l in open(f, errors="ignore"):
        lines.append(l.rstrip("\n"))
TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})")
EV = re.compile(r"⇢ (slot-\d+) 报 (\S+)（(\S+)）.*?线=(\S+) 出口=(\S+) 报障IP=(\S+) 实测=(\S+) 标脏后=(\S+).*?重试改走 (\S+) 目标线=(\S+) 目标出口=(\S+) 目标url=(\S+)")
ev = []
for l in lines:
    if "⇢ slot-" not in l: continue
    m = EV.search(l); t = TS.search(l)
    if not m: continue
    g = m.groups()
    ev.append(dict(slot=g[0], reason=g[1], rout=g[2], tag=g[3], ip=g[4], rep=g[5],
                   va=g[7], sw=g[8], sw_tag=g[9], sw_ip=g[10], url=g[11],
                   sec=(int(t.group(1))*3600+int(t.group(2))*60+int(t.group(3))) if t else None,
                   line=l))
r = [e for e in ev if e["reason"] == "rate429"]
by_ip = collections.defaultdict(list)
for e in r:
    if e["ip"] not in ("-", "未知", ""): by_ip[e["ip"]].append(e)
clusters = []
for ip, es in by_ip.items():
    es.sort(key=lambda x: x["sec"] or 0); cur = []
    for e in es:
        if cur and e["sec"] is not None and cur[-1]["sec"] is not None and 0 <= e["sec"]-cur[-1]["sec"] <= 90: cur.append(e)
        else:
            if len(cur) >= 2: clusters.append((ip, cur))
            cur = [e]
    if len(cur) >= 2: clusters.append((ip, cur))
print("clusters=%d" % len(clusters))
slot_n = collections.Counter(); tag_n = collections.Counter(); url_n = collections.Counter()
va = collections.Counter(); sw = collections.Counter(); rep_same = 0
for ip, es in clusters:
    slot_n[min(len(set(e["slot"] for e in es)), 4)] += 1
    tag_n[min(len(set(e["tag"] for e in es)), 4)] += 1
    url_n[min(len(set(e["url"] for e in es)), 4)] += 1
    for e in es:
        va[e["va"]] += 1; sw[e["sw"]] += 1
    if len(set(e["rep"] for e in es)) == 1 and es[0]["rep"] == ip: rep_same += 1
print("簇内不同 slot 数分布(1/2/3/>=4):", dict(sorted(slot_n.items())))
print("簇内不同 线名 数分布:", dict(sorted(tag_n.items())))
print("簇内不同 目标url 数分布:", dict(sorted(url_n.items())))
print("簇内 标脏后 结论:", dict(va.most_common()))
print("簇内 重试改走:", dict(sw.most_common(6)))
# already_retired 行：是否同一 IP 出现在簇里
ret = [l for l in lines if "在请求发出后被换走" in l]
ret_ip = set()
for l in ret:
    m = re.search(r"请求地址 (\S+) 在请求发出后被换走", l)
    if m: ret_ip.add(m.group(1))
cl_ip = set(ip for ip, _ in clusters)
print("already_retired 行数=%d 其中带IP=%d 与簇IP重叠=%d" % (len(ret), len(ret_ip), len(ret_ip & cl_ip)))
print("no_healthy_slot 行数=%d" % sum(1 for l in lines if "no_healthy_slot" in l))
# 抽样一个最大簇的逐发行
if clusters:
    ip, es = max(clusters, key=lambda x: len(x[1]))
    print("\n=== 最大簇 ip=%s 共%d发 ===" % (ip[:36], len(es)))
    for e in es[:12]:
        print("  %s %s %s 线=%s 标脏后=%s → %s url=%s" % (
            e["sec"], e["slot"], e["rout"], e["tag"][:20], e["va"], e["sw"][:12], e["url"][-5:]))
