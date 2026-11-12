# -*- coding: utf-8 -*-
"""只读：审查 429 后同一 IP 被重复使用的代码路径证据。
不写文件、不发请求、不改状态。
"""
import re, os, collections

BASE = os.path.dirname(os.path.abspath(__file__))
FILES = [os.path.join(BASE, "daemon.log")] + [
    os.path.join(BASE, "daemon.log.%d" % i) for i in range(1, 8)]
FILES = [f for f in FILES if os.path.exists(f)]

lines = []
for f in FILES:
    for l in open(f, errors="ignore"):
        lines.append(l.rstrip("\n"))
print("FILES=%s lines=%d" % ([os.path.basename(f) for f in FILES], len(lines)))

TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})")
EV = re.compile(
    r"⇢ (slot-\d+) 报 (\S+)（(\S+)）.*?线=(\S+) 出口=(\S+) 报障IP=(\S+) "
    r"实测=(\S+) 标脏后=(\S+).*?重试改走 (\S+) 目标线=(\S+) 目标出口=(\S+) 目标url=(\S+)")

events = []
for l in lines:
    if "⇢ slot-" not in l or " 报 " not in l:
        continue
    m = EV.search(l)
    t = TS.search(l)
    sec = (int(t.group(1)) * 3600 + int(t.group(2)) * 60 + int(t.group(3))) if t else None
    if not m:
        events.append({"raw": l, "sec": sec, "parse": False})
        continue
    (slot, reason, reason_out, tag, ip, reported, vb, va,
     sw, sw_tag, sw_ip, sw_url) = m.groups()
    events.append(dict(slot=slot, reason=reason, reason_out=reason_out, tag=tag,
                       ip=ip, reported=reported, vb=vb, va=va, sw=sw,
                       sw_tag=sw_tag, sw_ip=sw_ip, sw_url=sw_url,
                       sec=sec, raw=l, parse=True))

r429 = [e for e in events if e["parse"] and e["reason"] == "rate429"]
print("events=%d parsed=%d rate429=%d" % (
    len(events), sum(1 for e in events if e["parse"]), len(r429)))

print("\n=== 1. rate429 回包 reason_out 分布 ===")
c = collections.Counter(e["reason_out"] for e in r429)
for k, v in c.most_common():
    sub = [e for e in r429 if e["reason_out"] == k]
    has_url = sum(1 for e in sub if e["sw_url"] not in ("-", "None", ""))
    same_target = sum(1 for e in sub if e["sw_ip"] == e["ip"] and e["sw_ip"] != "-")
    print("  %-18s %5d  给新端口 %5d (%5.1f%%)  目标出口==本出口 %4d" % (
        k, v, has_url, 100.0 * has_url / max(1, v), same_target))

print("\n=== 2. 出口 IP vs 调用方报障IP 一致性 ===")
diff = [e for e in r429 if e["reported"] not in ("-", "", e["ip"])]
same = [e for e in r429 if e["reported"] == e["ip"]]
na = [e for e in r429 if e["reported"] in ("-", "")]
print("  一致 %d / 不一致 %d / 未提供 %d  → 不一致率 %.1f%%" % (
    len(same), len(diff), len(na), 100.0 * len(diff) / max(1, len(same) + len(diff))))
if diff[:3]:
    for e in diff[:3]:
        print("    例: 出口=%s 报障IP=%s" % (e["ip"][:28], e["reported"][:28]))

print("\n=== 3. 同一 IP 的 429 连续簇（间隔<=90s）===")
by_ip = collections.defaultdict(list)
for e in r429:
    if e["ip"] not in ("-", "未知", ""):
        by_ip[e["ip"]].append(e)
clusters = []
for ip, es in by_ip.items():
    es.sort(key=lambda x: (x["sec"] if x["sec"] is not None else 0))
    cur = []
    for e in es:
        if cur and e["sec"] is not None and cur[-1]["sec"] is not None \
                and 0 <= e["sec"] - cur[-1]["sec"] <= 90:
            cur.append(e)
        else:
            if len(cur) >= 2:
                clusters.append((ip, cur))
            cur = [e]
    if len(cur) >= 2:
        clusters.append((ip, cur))
print("  簇数=%d  总发数=%d  最大簇=%d" % (
    len(clusters), sum(len(c) for _, c in clusters),
    max([len(c) for _, c in clusters] or [0])))
cc = collections.Counter()
url_uniq = collections.Counter()
for ip, es in clusters:
    for e in es:
        cc[e["reason_out"]] += 1
    url_uniq[min(len(set(e["sw_url"] for e in es)), 3)] += 1
print("  簇内 reason_out:", dict(cc.most_common()))
print("  簇内不同目标url数量分布(0/1/2/>2 → 1/2/3):", dict(sorted(url_uniq.items())))

# 簇内：本出口 IP 是否出现在目标出口里（= 选槽把刚 429 的 IP 又选回来）
self_target = 0
for ip, es in clusters:
    if any(e["sw_ip"] == ip for e in es):
        self_target += 1
print("  簇内出现「目标出口==刚429的出口」的簇: %d / %d" % (self_target, len(clusters)))

print("\n=== 4. 全局关键字计数（冷却/门控是否触发）===")
keys = [
    "在请求发出后被换走",
    "冷却中（剩",
    "是这条请求刚撞过的地址，跳过",
    "暂不换线",
    "no_healthy_slot",
    "no_warm_standby",
    "不换线（并发合并）",
    "复测 OK",
]
for k in keys:
    print("  %-28s %d" % (k, sum(1 for l in lines if k in l)))

print("\n=== 5. 暂不换线 原因分布 ===")
why = collections.Counter()
for l in lines:
    if "暂不换线" in l:
        m = re.search(r"暂不换线（([^）]*)）", l)
        if m:
            why[m.group(1)[:60]] += 1
for k, v in why.most_common(8):
    print("  %-60s %d" % (k, v))

print("\n=== 6. 短冷却过滤日志：候选被挡的比例（样本行）===")
cool = [l for l in lines if "冷却中（剩" in l]
for l in cool[:3]:
    print("  " + l.strip()[:180])

print("\nDONE")
