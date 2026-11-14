#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归因对照：env 会话稳定修复（05:42 部署）到底有没有让「会话历史吃到前缀缓存」。
判据：同一会话相邻两轮（时间近、IN 递增但增量小 = 一轮消息），第二轮 CACHE 是否覆盖第一轮 IN。
若覆盖 → 历史被缓存命中；若只命中静态前缀（5-8K）→ 历史零命中。
按请求时刻切 05:42 前/后两组对比。
"""
import re
import subprocess
from datetime import datetime

SINCE = "2026-11-14 03:00:00"
UNTIL = "2026-11-14 13:20:00"
FIX_TS = datetime.strptime("2026-11-14 05:42:00", "%Y-%m-%d %H:%M:%S")

out = subprocess.run(
    ["journalctl", "-u", "9router", "--since", SINCE, "--until", UNTIL,
     "--no-pager", "-o", "cat"],
    capture_output=True, text=True, errors="replace").stdout
lines = out.splitlines()

# 提取请求记录
reqs = {}   # rid -> dict
order = []
for l in lines:
    m = re.search(r"req=([a-z0-9]+)", l)
    if not m:
        continue
    rid = m.group(1)
    if rid not in reqs:
        reqs[rid] = {}
        order.append(rid)
    d = reqs[rid]
    if "\u25b6 POST" in l or "POST" in l:
        tm = re.search(r"(\d+) MSG", l)
        if tm:
            d["msgs"] = int(tm.group(1))
        tsm = re.match(r"\[(\d{2}:\d{2}:\d{2})\]", l)
        if tsm:
            d["ts"] = tsm.group(1)
        fm = re.search(r"FMT: (\S+)", l)
        if fm:
            d["fmt"] = fm.group(1)
    if "DONE" in l:
        cm = re.search(r"CACHE \u21bb(\d+)", l)
        im = re.search(r"IN (\d+)", l)
        if cm:
            d["cache"] = int(cm.group(1))
        if im:
            d["in_"] = int(im.group(1))

# 顺序化
seq = []
for rid in order:
    d = reqs[rid]
    if "ts" in d and "in_" in d and "cache" in d:
        ts = datetime.strptime("2026-11-14 " + d["ts"], "%Y-%m-%d %H:%M:%S")
        seq.append((ts, rid, d))
seq.sort(key=lambda x: x[0])

# 找同会话相邻轮：时间差 <= 180s，IN 递增，增量 < 60K（一轮消息的量）
pairs = []
for i in range(1, len(seq)):
    t1, r1, d1 = seq[i - 1]
    t2, r2, d2 = seq[i]
    gap = (t2 - t1).total_seconds()
    if gap > 180 or gap < 0:
        continue
    in1, in2 = d1.get("in_", 0), d2.get("in_", 0)
    c2 = d2.get("cache", 0)
    if in1 <= 0 or in2 <= 0:
        continue
    if in2 < in1:          # 历史没增长 -> 不是同会话续轮
        continue
    if in2 - in1 > 60000:  # 增量太大 -> 不是一轮消息
        continue
    # 第二轮是否命中第一轮的前缀（覆盖 ≥90% 第一轮输入）
    hit_prev = c2 >= 0.9 * in1
    only_static = c2 < 20000
    pairs.append({
        "t2": t2, "r2": r2, "in1": in1, "in2": in2, "c2": c2,
        "hit_prev": hit_prev, "only_static": only_static,
        "post": t2 >= FIX_TS,
        "msgs": d2.get("msgs"),
    })

def summarize(name, ps):
    if not ps:
        print(f"  {name}: 样本 0")
        return
    n = len(ps)
    hp = sum(1 for p in ps if p["hit_prev"])
    os_ = sum(1 for p in ps if p["only_static"])
    ratios = [p["c2"] / p["in2"] for p in ps]
    ratios.sort()
    p50 = ratios[len(ratios) // 2]
    print(f"  {name}: 样本 {n} | 命中前一轮前缀 {hp} ({hp/n*100:.0f}%) | "
          f"只吃静态前缀 {os_} ({os_/n*100:.0f}%) | CACHE/IN p50={p50*100:.0f}%")

print(f"== 同会话相邻轮前缀缓存归因对照（{SINCE[11:]} ~ {UNTIL[11:]}，切点 05:42 env修复部署）==")
print()
pre = [p for p in pairs if not p["post"]]
post = [p for p in pairs if p["post"]]
summarize("05:42 之前", pre)
summarize("05:42 之后", post)
print()
print("== 之后组的逐对明细（前 20）==")
for p in post[:20]:
    print(f"  [{p['t2'].strftime('%H:%M:%S')}] {p['r2']} IN {p['in1']}→{p['in2']} "
          f"CACHE {p['c2']} hit_prev={p['hit_prev']} static_only={p['only_static']} "
          f"MSG={p['msgs']}")
print()
print("== 之前组的逐对明细（前 20）==")
for p in pre[:20]:
    print(f"  [{p['t2'].strftime('%H:%M:%S')}] {p['r2']} IN {p['in1']}→{p['in2']} "
          f"CACHE {p['c2']} hit_prev={p['hit_prev']} static_only={p['only_static']} "
          f"MSG={p['msgs']}")
