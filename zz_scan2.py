# -*- coding: utf-8 -*-
"""扫描更多机场节点，找出可用（能查 IP）的落地。"""
import json, subprocess, time, urllib.request, sys

BASE = "singbox-config.json-slot3.json"
CFG = "/tmp/cap-scan2.json"
PORT = 7002
CLASH = 9099
SECRET = "cap-test-secret2"

d = json.load(open(BASE))
nodes = [o["tag"] for o in d["outbounds"]
         if o.get("type") == "anytls" and not o["tag"].startswith("cn.bing.com")]
# 按地区前缀取多样本
picks = []
for pref in ["hk-aws", "sg-aws", "jp-aws", "bilibili", "ttll20", "ddixx", "upoz", "us-", "pq", "norm"]:
    group = [t for t in nodes if t.lower().startswith(pref)]
    picks += group[:4]
picks = list(dict.fromkeys(picks))[:40]
print("candidates=%d" % len(picks), flush=True)

d["inbounds"] = [{"type": "mixed", "tag": "cap-in", "listen": "127.0.0.1", "listen_port": PORT}]
d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") != "cap-out"]
d["outbounds"].append({"type": "selector", "tag": "cap-out", "outbounds": picks, "default": picks[0]})
d["route"]["rules"] = [dict(r, outbound="cap-out") if r.get("outbound") == "opencode-out" else r
                       for r in d["route"]["rules"]]
d["route"]["final"] = "cap-out"
d["experimental"] = {"clash_api": {"external_controller": "127.0.0.1:%d" % CLASH, "secret": SECRET},
                     "cache_file": {"enabled": False}}
d["log"] = {"level": "warn", "timestamp": True}
json.dump(d, open(CFG, "w"))


def select(tag):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/proxies/cap-out" % CLASH,
        data=json.dumps({"name": tag}).encode(), method="PUT",
        headers={"Authorization": "Bearer " + SECRET, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except Exception:
        return None


def ip_of():
    try:
        r = subprocess.run(["curl", "-s", "--max-time", "6",
                            "--proxy", "http://127.0.0.1:%d" % PORT,
                            "https://icanhazip.com"], capture_output=True, text=True)
        v = (r.stdout or "").strip()
        return v if v and len(v) < 60 else "?"
    except Exception:
        return "?"


log = open("/tmp/cap-scan2.log", "w")
proc = subprocess.Popen(["./sing-box", "run", "-c", CFG], stdout=log, stderr=subprocess.STDOUT)
found = {}
try:
    time.sleep(3)
    for tag in picks:
        select(tag)
        time.sleep(0.5)
        ip = ip_of()
        if ip != "?":
            print("UP  %-34s %s" % (tag[:34], ip), flush=True)
            found[tag] = ip
        else:
            print("--  %-34s" % tag[:34], flush=True)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    log.close()
json.dump({"ts": time.time(), "found": found}, open("cap_scan2.json", "w"), indent=2)
print("FOUND=%d" % len(found), flush=True)
