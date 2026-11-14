#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关联分析：CACHE 大命中 vs 小命中的请求，各自是否经过 headroom 压缩、TOOL 数是否变化。
目的：找出缓存命中率的真正分水岭（headroom? 工具表? 会话新鲜度?）"""
import re
import subprocess
from collections import defaultdict

SINCE = "2026-11-14 06:24"
OUT = "/tmp/9router_cache_corr.log"

subprocess.run(["journalctl", "-u", "9router", "--since", SINCE, "--no-pager", "-o", "cat"],
               stdout=open(OUT, "w"), check=False)

lines = open(OUT, encoding="utf-8", errors="replace").read().splitlines()

# req -> info
info = defaultdict(dict)
for l in lines:
    m = re.search(r"req=([a-z0-9]+)", l)
    if not m:
        continue
    rid = m.group(1)
    d = info[rid]
    if "DONE" in l:
        cm = re.search(r"CACHE ↻(\d+)", l)
        if cm:
            d["cache"] = int(cm.group(1))
        im = re.search(r"IN ([\d.]+)K|IN (\d+) ", l) or re.search(r"IN ([\d,]+)", l)
        if im:
            d["in_tok"] = im.group(1)
    if "▶ POST" in l:
        tm = re.search(r"(\d+) TOOL", l)
        if tm:
            d["tools"] = int(tm.group(1))
        mm = re.search(r"(\d+) MSG", l)
        if mm:
            d["msgs"] = int(mm.group(1))
    if "[HEADROOM]" in l and "reported token delta" in l:
        d["headroom"] = "compressed"
    if "[HEADROOM]" in l and "skipped" in l:
        d.setdefault("headroom", "skipped")

rows = [(rid, d) for rid, d in info.items() if "cache" in d]
print(f"样本（有 CACHE 值）: {len(rows)}")

def bucket(c):
    if c >= 20000:
        return "大命中(≥20K)"
    if c >= 5000:
        return "中命中(5-20K)"
    return "小命中(<5K)"

by_bucket = defaultdict(list)
for rid, d in rows:
    by_bucket[bucket(d["cache"])].append(d)

for b in ["大命中(≥20K)", "中命中(5-20K)", "小命中(<5K)"]:
    ds = by_bucket.get(b, [])
    if not ds:
        continue
    hr = sum(1 for d in ds if d.get("headroom") == "compressed")
    hs = sum(1 for d in ds if d.get("headroom") == "skipped")
    hn = sum(1 for d in ds if "headroom" not in d)
    caches = sorted(d["cache"] for d in ds)
    tools = sorted(set(d.get("tools") for d in ds if d.get("tools") is not None))
    msgs = sorted(set(d.get("msgs") for d in ds if d.get("msgs") is not None))
    print(f"\n== {b} n={len(ds)} ==")
    print(f"  CACHE: p50={caches[len(caches)//2]} min={caches[0]} max={caches[-1]}")
    print(f"  headroom: compressed={hr} skipped={hs} 无记录={hn}")
    print(f"  TOOL 数分布: {tools[:12]}")
    print(f"  MSG 数分布: {msgs[:12]}{'...' if len(msgs) > 12 else ''}")

# 同会话连续请求的命中增长：按时间排序找 CACHE 递增序列
seq = [(rid, d) for rid, d in rows]
print("\n== 大命中样例（top10）==")
top = sorted(rows, key=lambda x: -x[1]["cache"])[:10]
for rid, d in top:
    print(f"  req={rid} CACHE={d['cache']} TOOL={d.get('tools')} MSG={d.get('msgs')} headroom={d.get('headroom', '-')}")
