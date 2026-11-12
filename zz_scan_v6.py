# -*- coding: utf-8 -*-
"""批量扫描全部 outbound，找出真正能出 IPv6 的出口（零配额，只打 icanhazip/api6）。"""
import json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

BASE = "singbox-config.json-slot3.json"
BATCH = 40
P0 = 7200
FORCE_V6 = "--v6" in sys.argv


def curl(port, url, t=8):
    try:
        r = subprocess.run(["curl", "-s", "--max-time", str(t),
                            "--proxy", "http://127.0.0.1:%d" % port, url],
                           capture_output=True, text=True)
        v = (r.stdout or "").strip().splitlines()
        return v[0].strip() if v and v[0].strip() else None
    except Exception:
        return None


def run_batch(tags, idx0):
    d = json.load(open(BASE))
    ins, rules = [], []
    obs = [o for o in d["outbounds"] if o.get("tag") not in ("scan-mux",)]
    for i, tag in enumerate(tags):
        p = P0 + i
        ins.append({"type": "mixed", "tag": "in%d" % i,
                    "listen": "127.0.0.1", "listen_port": p})
        rules.append({"inbound": ["in%d" % i], "outbound": tag})
    d["inbounds"] = ins
    d["outbounds"] = obs
    d["route"]["final"] = tags[0]
    d["route"]["rules"] = [{"action": "sniff"}] + rules
    if FORCE_V6:
        d["route"]["rules"].append(
            {"action": "resolve", "domain_suffix": ["icanhazip.com", "api6.ipify.org"],
             "server": "dns-opencode", "strategy": "ipv6_only"})
    d["experimental"] = {"cache_file": {"enabled": False}}
    d["log"] = {"level": "error"}
    cfg = "/tmp/scan_%d.json" % idx0
    json.dump(d, open(cfg, "w"))
    lg = open("/tmp/scan_%d.log" % idx0, "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                          stdout=lg, stderr=subprocess.STDOUT)
    time.sleep(3.0)
    urls = ("https://icanhazip.com", "https://api6.ipify.org") if FORCE_V6 \
        else ("https://icanhazip.com", "https://api.ipify.org")
    res = {}

    def one(i, tag):
        p = P0 + i
        for u in urls:
            v = curl(p, u)
            if v:
                res[tag] = v
                return
        res[tag] = None

    with ThreadPoolExecutor(max_workers=len(tags)) as ex:
        list(ex.map(lambda a: one(*a), list(enumerate(tags))))
    pr.terminate()
    try:
        pr.wait(timeout=5)
    except Exception:
        pass
    return res


def main():
    d = json.load(open(BASE))
    tags = [o.get("tag") for o in d["outbounds"]
            if o.get("type") not in ("selector", "urltest", "direct", "block", "dns")]
    print("outbounds to scan:", len(tags), flush=True)
    allres = {}
    nb = (len(tags) + BATCH - 1) // BATCH
    for b in range(nb):
        chunk = tags[b * BATCH:(b + 1) * BATCH]
        r = run_batch(chunk, b)
        allres.update(r)
        live = [t for t, v in r.items() if v]
        print("[batch %d/%d] live=%d/%d  %s" % (b + 1, nb, len(live), len(chunk),
              ", ".join("%s=%s" % (t[:26], allres[t]) for t in live[:4])), flush=True)
    out = [{"tag": t, "ip": v,
            "family": "v6" if (v and ":" in v) else ("v4" if v else "none")}
           for t, v in allres.items()]
    json.dump(out, open("scan_result.json", "w"), ensure_ascii=False, indent=1)
    v6s = [o for o in out if o["family"] == "v6"]
    print("\n=== v6 出口 %d 个 ===" % len(v6s))
    pre = {}
    for o in v6s:
        k = ":".join(o["ip"].split(":")[:4])
        pre.setdefault(k, []).append(o)
    for k, v in sorted(pre.items()):
        print("%-26s x%d  %s" % (k, len(v), [x["tag"] for x in v][:3]))
    print("DONE")


if __name__ == "__main__":
    main()
