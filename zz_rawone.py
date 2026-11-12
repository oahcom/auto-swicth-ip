# -*- coding: utf-8 -*-
"""zz_rawone.py — 对指定出口发 1 发，打印完整限流头与响应正文。

用法:
  python3 zz_rawone.py <idx> <tag> [--v4] [--sid <session>]
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import probe_warp as P

BASE = "singbox-config.json-slot3.json"
BODY = json.dumps(P._body())
MJS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw_body.mjs")


def build(idx, tag, port, family):
    cfg = "/tmp/ro_%d.json" % idx
    d = json.load(open(BASE))
    d["inbounds"] = [{"type": "mixed", "tag": "in",
                      "listen": "127.0.0.1", "listen_port": port}]
    d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") != "cap-out"]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": [tag], "default": tag})
    d["route"]["final"] = "cap-out"
    pre = []
    if family == "v6":
        pre.append({"action": "resolve",
                    "domain_suffix": ["opencode.ai", ".opencode.ai"],
                    "server": "dns-opencode", "strategy": "ipv6_only"})
    d["route"]["rules"] = pre + [
        {"action": "sniff"},
        {"domain_suffix": ["opencode.ai"], "outbound": "cap-out"},
    ]
    d["experimental"] = {"cache_file": {"enabled": False}}
    d["log"] = {"level": "error"}
    json.dump(d, open(cfg, "w"))
    return cfg


def main():
    a = sys.argv[1:]
    family = "v4" if "--v4" in a else "v6"
    a = [x for x in a if x not in ("--v4", "--v6")]
    sid = None
    if "--sid" in a:
        i = a.index("--sid")
        sid = a[i + 1]
        a = a[:i] + a[i + 2:]
    idx = int(a[0])
    tag = a[1]
    port = 7700 + idx
    cfg = build(idx, tag, port, family)
    lg = open("/tmp/ro_%d.log" % idx, "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                          stdout=lg, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    try:
        sid = sid or P._session_id()
        payload = {"url": P.URL, "method": "POST", "headers": P._headers(sid),
                   "body": BODY, "proxy": "http://127.0.0.1:%d" % port,
                   "timeoutMs": 40000}
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        from asip.proc import run as run_proc
        try:
            r = run_proc([P._bun_bin(), MJS, path], timeout=55)
        finally:
            os.remove(path)
        print("=== tag=%s  session=%s  端口=%d ===" % (tag, sid, port))
        if r.error or r.timed_out or r.code != 0:
            print("!! 请求失败: err=%s timed_out=%s code=%s" % (r.error, r.timed_out, r.code))
            print((r.stdout or "")[:500])
            return
        ls = [l for l in r.stdout.splitlines() if l.strip()]
        d = json.loads(ls[-1])
        if d.get("error"):
            print("!! fetch 错误: %s" % d["error"])
            return
        print("\n--- HTTP status: %s ---" % d["status"])
        print("\n--- 响应头（只列限流相关 + 全部）---")
        for k, v in sorted(d["headers"].items()):
            print("  %-34s %s" % (k, v))
        print("\n--- 响应体（%d 字节，截前 4000）---" % d.get("bodyLen", 0))
        print(d["body"])
        json.dump(d, open("rawone_%d.json" % idx, "w"), ensure_ascii=False, indent=1)
    finally:
        pr.terminate()
        try:
            pr.wait(timeout=6)
        except Exception:
            pass


if __name__ == "__main__":
    main()
