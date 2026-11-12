# -*- coding: utf-8 -*-
import os, collections, re
FILES = [f for f in ["daemon.log"] + ["daemon.log.%d" % i for i in range(1, 8)] if os.path.exists(f)]
lines = []
for f in FILES:
    lines += [l.rstrip("\n") for l in open(f, errors="ignore")]
print("files=%s lines=%d" % (FILES, len(lines)))
KEYS = [
    "↻ 槽位", "错误切换失败", "· 槽位 ", "试线 ", "no_candidate", "all_tried",
    "状态 ok → repairing（rate429）", "状态 repairing → ok（OK）",
    "⚡ 秒切备用线", "⚡ 主槽位换线", "✓ WARP 换线成功", "新鲜备线切换均失败",
    "暂不回收：仍在槽上服务流量", "不重启（失败时换线）", "主动认领切槽",
    "判坏但在槽", "模型层 quota", "模型层: OK", "离渠后第", "排空：零连接",
    "排空超上限", "暂不换线", "no_healthy_slot", "在请求发出后被换走",
    "探测分歧", "重启后未就绪", "尝试重启", "模型层 flagged",
]
for k in KEYS:
    print("%-34s %d" % (k, sum(1 for l in lines if k in l)))
print("\n=== 状态转移 top ===")
c = collections.Counter()
for l in lines:
    m = re.search(r"槽位 (slot-\d+) 状态 (\S+) → (\S+)（([^）]*)）", l)
    if m: c[(m.group(2), m.group(3), m.group(4)[:24])] += 1
for (a, b, why), n in c.most_common(12):
    print("  %-10s → %-10s %-26s %d" % (a, b, why, n))
print("\n=== 秒切 / 换线成功 样本 ===")
for k in ["⚡ 秒切备用线", "↻ 槽位", "✗ 槽位", "暂不回收：仍在槽上服务流量", "不重启（失败时换线）"]:
    s = [l for l in lines if k in l]
    print("--- %s (%d) ---" % (k, len(s)))
    for l in s[:3]: print("   " + l.strip()[:190])
