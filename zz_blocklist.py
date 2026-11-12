# -*- coding: utf-8 -*-
"""从今晚的实测记录生成"打空 IP 黑名单"，带各自精确的恢复时刻。
恢复时刻 = 该次 429 的时刻 + Retry-After（滚动 24h 窗口，各 IP 不同）。
输出 blocked_ips.json，可直接喂给 daemon 的冷却/优选逻辑。
"""
import datetime
import json

d = json.load(open("rebuild_result.json"))
rows = []
for ip, a in d.items():
    best = None
    for r in a["runs"]:
        if r["stop"] in ("quota", "egress_blocked") and r["ra"]:
            if best is None or r["ok"] > best["ok"]:
                best = r
    if not best:
        # 没 ra 的也记，标 unknown
        q = [r for r in a["runs"] if r["stop"] == "quota"]
        if q:
            best = q[0]
    if not best:
        continue
    ts = best["ts"]
    ra = best["ra"]
    if ra:
        rec = datetime.datetime.fromtimestamp(ts + ra)
        rec_s = rec.strftime("%m-%d %H:%M:%S")
        left = int(ts + ra - datetime.datetime.now().timestamp())
    else:
        rec_s = "未知(无 ra)"
        left = None
    rows.append({
        "ip": ip,
        "family": a["family"],
        "consumed_by_me": a["total_ok"],
        "stop": best["stop"],
        "ra": ra,
        "blocked_at": datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S"),
        "recover_at": rec_s,
        "seconds_left": left,
        "warp_drift": a["drift"],
    })

rows.sort(key=lambda x: (x["seconds_left"] if x["seconds_left"] is not None else 9e9))
json.dump(rows, open("blocked_ips.json", "w"), ensure_ascii=False, indent=1)

print("%-40s %-4s %6s %-18s %12s" % ("IP", "族", "我消耗", "预计恢复时刻", "剩余h"))
print("-" * 96)
for r in rows:
    if r["warp_drift"]:
        continue
    left = ("%.1f" % (r["seconds_left"] / 3600.0)) if r["seconds_left"] is not None else "-"
    print("%-40s %-4s %6d %-18s %12s"
          % (r["ip"], r["family"], r["consumed_by_me"], r["recover_at"], left))

air = [r for r in rows if r["family"] == "v4" or r["ip"].startswith("2405:")]
print("\n机场侧需冷却 %d 个；其中 24h 内不会恢复完的先别用。" % len(air))
