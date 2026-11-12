# -*- coding: utf-8 -*-
"""扫描一批节点，看出口到底落在哪个 v6 段（零配额，只打 icanhazip）。
用法: python3 zz_v6map.py tag1 tag2 ...
"""
import json, subprocess, sys, time

BASE = "singbox-config.json-slot3.json"


def build(idx, tag):
    port = 7100 + idx
    cfg = "/tmp/v6map_%d.json" % idx
    d = json.load(open(BASE))
    d["inbounds"] = [{"type": "mixed", "tag": "in",
                      "listen": "127.0.0.1", "listen_port": port}]
    d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") != "map-out"]
    d["outbounds"].append({"type": "selector", "tag": "map-out",
                           "outbounds": [tag], "default": tag})
    d["route"]["final"] = "map-out"
    d["dns"]["rules"] = [
        {"domain_suffix": ["okzdns.com", "juhazf.cn", "hi2026.net", "acczzz.com",
                           "bing.com"], "server": "dns-airport", "strategy": "ipv4_only"},
        {"domain_suffix": ["opencode.ai", ".opencode.ai"], "server": "dns-opencode"},
    ]
    d["route"]["rules"] = [
        {"action": "sniff"},
        {"domain_suffix": ["opencode.ai"], "outbound": "map-out"},
        {"action": "resolve", "domain_suffix": ["opencode.ai", ".opencode.ai"],
         "server": "dns-opencode", "strategy": "ipv6_only"},
        {"action": "resolve", "domain_suffix": ["icanhazip.com", "api6.ipify.org"],
         "server": "dns-opencode", "strategy": "ipv6_only"},
    ]
    d["experimental"] = {"cache_file": {"enabled": False}}
    d["log"] = {"level": "error"}
    json.dump(d, open(cfg, "w"))
    return port, cfg


def probe(port, fam="v6"):
    urls = ("https://icanhazip.com", "https://api6.ipify.org") if fam == "v6" \
        else ("https://icanhazip.com", "https://api.ipify.org")
    for u in urls:
        for _ in range(2):
            try:
                r = subprocess.run(["curl", "-s", "--max-time", "7",
                                    "--proxy", "http://127.0.0.1:%d" % port, u],
                                   capture_output=True, text=True)
                v = (r.stdout or "").strip().splitlines()
                if v and v[0].strip():
                    return v[0].strip()
            except Exception:
                pass
    return None


def main():
    tags = sys.argv[1:]
    out = []
    for i, tag in enumerate(tags):
        port, cfg = build(i, tag)
        lg = open("/tmp/v6map_%d.log" % i, "w")
        pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                              stdout=lg, stderr=subprocess.STDOUT)
        time.sleep(2.5)
        ip = probe(port)
        fam = "v6" if (ip and ":" in ip) else ("v4" if ip else "none")
        out.append({"tag": tag, "ip": ip, "family": fam})
        print("%-46s -> %-42s %s" % (tag, ip, fam), flush=True)
        pr.terminate()
        try:
            pr.wait(timeout=5)
        except Exception:
            pass
    json.dump(out, open("v6map.json", "w"), ensure_ascii=False, indent=1)
    print("DONE")


if __name__ == "__main__":
    main()
