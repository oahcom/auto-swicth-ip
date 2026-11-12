# -*- coding: utf-8 -*-
"""重建计算 v2：以 IP 为单位建台账，再按"测法"分组重算，把 L1(session闸) 与
L2(IP余额) 彻底分开。

口径
----
- 一次 run = 一个结果文件/一条记录，含 node / ip / 发数 / 结局
- 发数：顶层 ok；缺失时数 rows 中 verdict=='OK'
- 结局：death.verdict > confirm[0].verdict > stop > err
    quota   = 真打到 429（额度到顶，可作额度证据）
    max     = 主动打到预设上限（额度 >= N，不是确切值）
    egress_blocked = 出口被封/耗尽
    ip_changed     = 中途漂移，作废
- 测法归一：
    single  单 session 固定、顺序/低并发   -> 反映 IP 余额
    rotate  每发换新 session               -> 会撞 L1 的 40 session 闸
    fixedN  N 个固定 session 并发           -> 只要 N<=40 就不撞 L1
"""
import glob
import json
import os
import re

FILES = sorted(set(glob.glob("cap*.json") + glob.glob("v7_result*.json")
                   + glob.glob("v8_result*.json") + glob.glob("v6cap_result.json")))


def rows_ok(d):
    return sum(1 for r in (d.get("rows") or [])
               if isinstance(r, dict) and r.get("verdict") == "OK")


def pick_stop(it, death, conf):
    if death.get("verdict"):
        return death["verdict"]
    if conf and isinstance(conf[0], dict) and conf[0].get("verdict"):
        return conf[0]["verdict"]
    if it.get("stop"):
        return it["stop"]
    return it.get("err")


def norm_mode(it):
    m = (it.get("mode") or "").lower()
    if m in ("same", "single"):
        return "single"
    if m in ("each", "rotate"):
        return "rotate"
    if m in ("multi",):
        return "rotate"
    if m in ("fixedn",):
        return "fixedN"
    if m == "every10":
        return "single"      # 每10发换一次，近似单 session
    if "conc" in m or it.get("conc"):
        return "conc"
    if "singlex" in m:
        return "single"
    return m or "?"


runs = []
for f in FILES:
    ts = os.path.getmtime(f)
    try:
        d = json.load(open(f))
    except Exception:
        continue
    for it in (d if isinstance(d, list) else [d]):
        if not isinstance(it, dict):
            continue
        death = (it.get("death") or [{}])
        death = death[0] if death and isinstance(death[0], dict) else {}
        conf = it.get("confirm") or []
        ok = it.get("ok")
        if not isinstance(ok, int):
            ok = rows_ok(it) if "rows" in it else 0
        ip = it.get("ip") or it.get("ip_start") or it.get("v4")
        runs.append({
            "file": os.path.basename(f), "ts": ts,
            "node": it.get("tag") or it.get("node"),
            "ip": ip,
            "ok": ok,
            "stop": pick_stop(it, death, conf),
            "ra": death.get("ra") or (conf[0].get("ra") if conf and isinstance(conf[0], dict) else None),
            "mode": norm_mode(it),
            "conc": it.get("conc"),
            "sessions": it.get("distinct_sessions") or it.get("sessions"),
            "ip_end": it.get("ip_end"),
            "drift": (it.get("ip_stable") is False)
                     or bool(it.get("ip_end") and ip and it["ip_end"] != ip),
        })

# 生产痕迹
prodhits = {}
if os.path.exists("/tmp/all.log"):
    txt = open("/tmp/all.log", errors="ignore").read()
    for r in runs:
        ip = r["ip"]
        if ip and ip not in prodhits:
            prodhits[ip] = len(re.findall(r"(?<![\d.])" + re.escape(ip) + r"(?![\d])", txt))

agg = {}
for r in runs:
    if not r["ip"]:
        continue
    a = agg.setdefault(r["ip"], {"ip": r["ip"], "runs": []})
    a["runs"].append(r)

for a in agg.values():
    a["runs"].sort(key=lambda x: x["ts"])
    a["prod"] = prodhits.get(a["ip"], 0)
    a["drift"] = any(x["drift"] for x in a["runs"])
    a["total_ok"] = sum(x["ok"] for x in a["runs"])
    a["family"] = "v6" if ":" in a["ip"] else "v4"
    # 取"今晚第一次有实质发数/结局"的 run 作为额度证据
    qual = [x for x in a["runs"] if x["stop"] in
            ("quota", "max", "egress_blocked", "ip_changed") or x["ok"] > 0]
    a["key_run"] = qual[0] if qual else (a["runs"][0] if a["runs"] else None)

json.dump({k: {"ip": v["ip"], "family": v["family"], "prod": v["prod"],
               "drift": v["drift"], "total_ok": v["total_ok"],
               "runs": v["runs"]}
           for k, v in agg.items()},
          open("rebuild_result.json", "w"), ensure_ascii=False, indent=1)

# ================= 输出 =================
print("结果文件 %d 个 / run %d 条 / IP %d 个\n" % (len(FILES), len(runs), len(agg)))
print("%-40s %-4s %-9s %-9s %6s %5s %s"
      % ("IP", "族", "测法", "额度证据", "累计", "生产", "结局"))
print("-" * 116)
for ip in sorted(agg, key=lambda x: -(agg[x]["key_run"]["ok"] if agg[x]["key_run"] else 0)):
    a = agg[ip]
    k = a["key_run"]
    if not k:
        continue
    ev = ("%d发" % k["ok"]) if k["stop"] in ("quota", "egress_blocked") else (
        ">=%d发" % k["ok"] if k["stop"] == "max" else "%d发" % k["ok"])
    mode = k["mode"] + (("x%s" % (k["conc"] or k["sessions"]))
                        if k["mode"] in ("fixedN", "conc") and (k["conc"] or k["sessions"]) else "")
    flag = " [漂移]" if a["drift"] else ""
    if a["prod"]:
        flag += " [生产%d]" % a["prod"]
    print("%-40s %-4s %-9s %-9s %6d %5d %s%s"
          % (ip, a["family"], mode, ev, a["total_ok"], a["prod"], k["stop"], flag))

# ================= 按测法分组 =================
def group(mode_want, label):
    print("\n\n===== %s =====" % label)
    rows = []
    seen = set()
    for ip, a in agg.items():
        if ip in seen:
            continue
        for r in a["runs"]:
            if r["mode"] == mode_want and r["stop"] in ("quota", "max", "egress_blocked"):
                rows.append((ip, r, a))
                seen.add(ip)
                break
    rows.sort(key=lambda x: -x[1]["ok"])
    if not rows:
        print("(无)")
        return []
    print("%-40s %-9s %8s %6s %s" % ("IP", "族", "发数", "生产", "结局"))
    print("-" * 90)
    out = []
    for ip, r, a in rows:
        ev = ("%d" % r["ok"]) + ("" if r["stop"] == "quota" else "(>=)")
        print("%-40s %-9s %8s %6d %s%s"
              % (ip, a["family"], ev, a["prod"], r["stop"],
                 "  [漂移]" if a["drift"] else ""))
        out.append(r["ok"])
    return out


v_single = group("single", "单 session 固定（反映 IP 余额，不受 L1 制约）")
v_rotate = group("rotate", "每发换新 session（会撞 L1 的 40 session 闸）")
v_fixed = group("fixedN", "N 个固定 session 并发（N<=40 时不撞 L1）")

def stat(name, vals):
    if not vals:
        print("\n%s: 无样本" % name)
        return
    s = sorted(vals)
    n = len(s)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    print("\n%s: n=%d  升序=%s  中位=%.1f  均值=%.1f"
          % (name, n, s, med, sum(s) / n))


stat("单session额度", v_single)
stat("换session额度", v_rotate)
stat("固定N session额度", v_fixed)

# 结论自动判定
print("\n\n===== 自动判定 =====")
if v_rotate:
    tight = [v for v in v_rotate if v <= 41]
    print("1) 换 session 组 %d/%d 个样本落在 <=41 发：%s"
          % (len(tight), len(v_rotate), sorted(v_rotate)))
    if len(tight) == len(v_rotate):
        print("   -> 换 session 时**全部**在 40/41 处死，与 IP 无关 => L1 = 40 个不同 session")
big_single = [v for v in v_single if v > 41]
if big_single:
    print("2) 单 session 组有 %d 个样本超过 41 发：%s" % (len(big_single), sorted(big_single)))
    print("   -> 同一批 IP 上单 session 能超 40 => 40 不是发数额度，是 session 数闸")
