#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从轮转日志回填 ban_probes 被午夜清掉的配额日样本（2026-11-15 一次性修复）。

背景：前任 daemon（11-14 04:33 启动、旧代码）的 _channel_ban_trim 用自然日
过滤，11-15 00:00 后第一次写样本时把 11-14 的段清了（98 条线中招）。
日志里每个「{tag} 出口 {ip} 模型层: {verdict}」都是带时间戳的实测记录，
分散在 daemon.log* 轮转文件里——按线聚合重建当前配额日（08:00→08:00）
的样本段，替换 ban_probes 里对应时间段的内容。

只收 OK/quota/flagged/blocked（与 _channel_ban_note 同口径）；探测自身
失败（unreachable/err）不入账。运行前必须停 daemon（否则内存态会覆盖）。
"""
import glob
import json
import os
import re
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, '.warp_line_state.json')
LOGS = os.path.join(ROOT, 'daemon.log*')

ts_re = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
model_re = re.compile(r'(\S+) 出口 \S+ 模型层: (\S+)')
KEEP = {'OK', 'quota', 'flagged', 'blocked'}

now = time.time()
lt = time.localtime(now)
day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 8, 0, 0, 0, 0, -1))
if now < day_start:
    day_start -= 86400
day_end = day_start + 86400
print(f'配额日窗口: {time.strftime("%m-%d %H:%M", time.localtime(day_start))}'
      f' -> {time.strftime("%m-%d %H:%M", time.localtime(day_end))}')

# 1. 收集实测记录 [(ts, tag, verdict)]
events: dict[str, list] = {}
files = sorted(glob.glob(LOGS), key=lambda f: (len(f), f))
n_lines = 0
for f in files:
    with open(f, errors='replace') as fh:
        for line in fh:
            if '模型层:' not in line:
                continue
            m_ts = ts_re.match(line)
            m_v = model_re.search(line)
            if not (m_ts and m_v):
                continue
            ts = time.mktime(time.strptime(m_ts.group(1), '%Y-%m-%d %H:%M:%S'))
            if ts < day_start or ts >= day_end:
                continue
            tag, verdict = m_v.group(1), m_v.group(2).split('（')[0].strip()
            if verdict not in KEEP or not tag.startswith('WARP'):
                continue
            events.setdefault(tag, []).append((ts, verdict))
            n_lines += 1
print(f'日志实测记录: {n_lines} 条 / {len(events)} 条线')

# 2. 按线聚合为游程 [起, 止, 条数, 结论]（同结论相邻合并）
def to_runs(pts):
    runs = []
    for ts, v in pts:
        if runs and runs[-1][3] == v and ts - runs[-1][1] <= 600:
            runs[-1][1] = ts
            runs[-1][2] += 1
        else:
            runs.append([ts, ts, 1, v])
    return runs

# 3. 写回状态：ban_probes = 窗口内日志重建段 + 保留窗口外已有段
with open(STATE, encoding='utf-8') as f:
    data = json.load(f)
lines = data.get('lines') or {}
merged = replaced = 0
for tag, pts in events.items():
    st = lines.get(tag)
    if not isinstance(st, dict):
        continue
    pts.sort()
    new_runs = to_runs(pts)
    keep_old = [seg for seg in (st.get('ban_probes') or [])
                if isinstance(seg, (list, tuple)) and len(seg) >= 4
                and not (day_start <= float(seg[0]) < day_end)]
    old_in = [seg for seg in (st.get('ban_probes') or [])
              if isinstance(seg, (list, tuple)) and len(seg) >= 4
              and day_start <= float(seg[0]) < day_end]
    all_runs = [list(map(float, seg[:2])) + [int(seg[2]), str(seg[3])]
                for seg in keep_old + new_runs]
    all_runs.sort(key=lambda r: r[0])
    out = []
    for r in all_runs:
        if out and out[-1][3] == r[3] and r[0] - out[-1][1] <= 600:
            out[-1][1] = max(out[-1][1], r[1])
            out[-1][2] += r[2]
        else:
            out.append(r)
    st['ban_probes'] = out
    replaced += 1
    merged += sum(int(r[2]) for r in new_runs)
with open(STATE, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False)
print(f'回填完成: {replaced} 条线、{merged} 个样本入账')
