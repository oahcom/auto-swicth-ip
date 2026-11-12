# -*- coding: utf-8 -*-
"""审计：今晚到底对哪些出口 IP 打过配额，各自打了多少发。
扫描全部 cap*/v7_result/v8_result/v6cap 结果文件，提取 (node, ip, ok, stop)。
"""
import glob
import json
import os

FILES = sorted(glob.glob("cap*.json") + glob.glob("v7_result*.json")
               + glob.glob("v8_result*.json") + glob.glob("v6cap_result.json"))

rows = []


def add(node, ip, ok, stop, src, ts):
    rows.append({"node": node or "?", "ip": ip or "?",
                 "ok": ok, "stop": stop, "src": src, "ts": ts})


for f in FILES:
    ts = os.path.getmtime(f)
    try:
        d = json.load(open(f))
    except Exception:
        continue
    # v7 / v8 结果：dict 或 list of dict
    if isinstance(d, list):
        for r in d:
            if isinstance(r, dict) and ("tag" in r or "ip_start" in r):
                add(r.get("tag"), r.get("ip_start") or r.get("ip"),
                    r.get("ok"), (r.get("death") or [{}])[0].get("verdict")
                    or r.get("err"), f, ts)
        continue
    if not isinstance(d, dict):
        continue
    # v8 单结果
    if "ip_start" in d:
        add(d.get("tag"), d.get("ip_start"), d.get("ok"),
            (d.get("death") or [{}])[0].get("verdict") or d.get("err"), f, ts)
        continue
    # cap6_same / cap_big 等：顶层带 node+ip+ok 的深度测试（先于 rows 判断）
    if ("node" in d or "tag" in d) and "ip" in d and isinstance(d.get("ok"), int):
        add(d.get("node") or d.get("tag"), d.get("ip"), d.get("ok"),
            d.get("stop"), f, ts)
        continue
    # cap_now / cap*_scan / cap6_each 等：rows 列表
    if "rows" in d and isinstance(d["rows"], list):
        for r in d["rows"]:
            if not isinstance(r, dict):
                continue
            ip = r.get("ip")
            if ip in ("?", "", None):
                # 尝试从 tag 推不出 IP，跳过 but 记录 tag
                ip = "?"
            add(r.get("tag") or r.get("node"), ip, r.get("ok"),
                r.get("verdict") or r.get("stop"), f, ts)
        continue
    # cap6_same / cap_big 等：单节点深度测试
    if "node" in d or "ip" in d:
        add(d.get("node") or d.get("tag"), d.get("ip"), d.get("ok"),
            d.get("stop"), f, ts)

# 按 IP 聚合
agg = {}
for r in rows:
    k = r["ip"]
    a = agg.setdefault(k, {"ip": k, "max_ok": 0, "attempts": 0,
                           "stops": set(), "srcs": set(), "nodes": set()})
    if isinstance(r["ok"], int):
        a["max_ok"] = max(a["max_ok"], r["ok"])
        a["attempts"] += r["ok"]
    if r["stop"]:
        a["stops"].add(str(r["stop"]))
    a["srcs"].add(os.path.basename(r["src"]))
    if r["node"] and r["node"] != "?":
        a["nodes"].add(r["node"])

print("结果文件 %d 个，记录 %d 条，涉及 IP %d 个\n" % (len(FILES), len(rows), len(agg)))
print("%-20s %8s %8s %-26s %s" % ("IP", "最大OK", "累计OK", "结局", "出现文件"))
print("-" * 110)
for k in sorted(agg, key=lambda x: -agg[x]["max_ok"]):
    a = agg[k]
    print("%-20s %8d %8d %-26s %s"
          % (k, a["max_ok"], a["attempts"],
             ",".join(sorted(a["stops"]))[:26],
             ",".join(sorted(a["srcs"]))[:60]))

# 重点核查对象
TARGET = ["154.83.84.34", "54.199.78.171", "18.183.195.87",
          "67.159.48.148", "156.224.78.207", "64.120.94.205",
          "64.120.94.207", "45.207.156.48", "134.195.101.195",
          "154.83.87.187"]
print("\n===== 重点核查（本次并发组用到的 IP）=====")
for ip in TARGET:
    a = agg.get(ip)
    if not a:
        print("%-18s 无任何记录 -> 今晚确实没打过配额" % ip)
        continue
    print("%-18s 历史最大 %d 发 / 累计 %d 发 / 结局 %s / 文件 %s"
          % (ip, a["max_ok"], a["attempts"], ",".join(sorted(a["stops"])),
             ",".join(sorted(a["srcs"]))))
