# -*- coding: utf-8 -*-
"""zz_blockadd.py — 把本轮 v8/l1/conc 结果里"真打空"的 IP 增量并入 blocked_ips.json。

判定：stop/verdict 为 quota（429），且有 ip_start 与 ra。
恢复时刻 = 测试结束时刻（文件 mtime）+ ra。
"""
import glob
import json
import os
from datetime import datetime, timedelta

OUT = "blocked_ips.json"


def ra_of(r):
    for d in (r.get("death") or []):
        if d.get("ra"):
            return d["ra"]
    for c in (r.get("confirm") or []):
        if c.get("ra"):
            return c["ra"]
    k = r.get("killed_at") or {}
    return k.get("ra")


def main():
    cur = []
    if os.path.exists(OUT):
        cur = json.load(open(OUT))
    have = {x["ip"] for x in cur}

    added = []
    files = (glob.glob("v8_result_*.json") + glob.glob("l1_result_*.json")
             + glob.glob("conc_result_*.json") + glob.glob("v7_result_*.json"))
    for f in files:
        try:
            d = json.load(open(f))
        except Exception:
            continue
        recs = d if isinstance(d, list) else [d]
        for r in recs:
            if not isinstance(r, dict):
                continue
            ip = r.get("ip_end") or r.get("ip_start") or r.get("ip")
            if not ip or ip == "?":
                continue
            ra = ra_of(r)
            if not ra:
                continue
            # 只有确实撞到 429 的才收
            hit = bool(r.get("death")) or r.get("killed_at") or \
                any(c.get("verdict") == "quota" for c in (r.get("confirm") or []))
            if not hit:
                continue
            if ip in have:
                continue
            ts = datetime.fromtimestamp(os.path.getmtime(f))
            rec = {
                "ip": ip,
                "family": "v6" if ":" in ip else "v4",
                "consumed_by_me": r.get("ok") or 0,
                "stop": "quota",
                "ra": ra,
                "blocked_at": ts.strftime("%m-%d %H:%M:%S"),
                "recover_at": (ts + timedelta(seconds=ra)).strftime("%m-%d %H:%M:%S"),
                "warp_drift": not bool(r.get("ip_stable", True)),
                "src": f,
            }
            cur.append(rec)
            have.add(ip)
            added.append(rec)

    cur.sort(key=lambda x: x.get("recover_at") or "")
    json.dump(cur, open(OUT, "w"), ensure_ascii=False, indent=1)
    print("新增 %d 条，共 %d 条" % (len(added), len(cur)))
    for r in added:
        print("  %-42s %-3s ok=%-4s ra=%-6s 恢复 %s"
              % (r["ip"], r["family"], r["consumed_by_me"], r["ra"], r["recover_at"]))


if __name__ == "__main__":
    main()
