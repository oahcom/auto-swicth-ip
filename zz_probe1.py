# -*- coding: utf-8 -*-
"""zz_probe1.py — 批量 1 发探活（成本 = 出口数 x 1 发）。

每个 tag 一个 inbound 端口，同一个 sing-box 实例并发打。
同时用 icanhazip 校验真实出口 IP（这部分不消耗 opencode 额度）。

用法:
  python3 zz_probe1.py tag1 tag2 ...            # 强制 v6
  python3 zz_probe1.py --v4 tag1 tag2 ...       # v4
  python3 zz_probe1.py --out r.json tag1 ...
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import probe_warp as P

BASE = "singbox-config.json-slot3.json"
BODY = json.dumps(P._body())
P0 = 7500
MAX_IN = 30


def curl(port, url, t=8):
    try:
        r = subprocess.run(["curl", "-s", "--max-time", str(t),
                            "--proxy", "http://127.0.0.1:%d" % port, url],
                           capture_output=True, text=True)
        v = (r.stdout or "").strip().splitlines()
        return v[0].strip() if v and v[0].strip() else None
    except Exception:
        return None


def exit_ip(port, family):
    urls = ("https://icanhazip.com", "https://api6.ipify.org") if family == "v6" \
        else ("https://icanhazip.com", "https://api.ipify.org")
    for u in urls:
        for _ in range(2):
            v = curl(port, u)
            if v:
                return v
    return None


def call_opencode(port, sid, timeout_ms=40000):
    from asip.proc import run as run_proc
    bun = P._bun_bin()
    payload = {"url": P.URL, "method": "POST", "headers": P._headers(sid),
               "body": BODY, "proxy": "http://127.0.0.1:%d" % port,
               "timeoutMs": timeout_ms}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    try:
        r = run_proc([bun, P.BUN_SCRIPT, path], timeout=timeout_ms / 1000.0 + 10)
    finally:
        os.remove(path)
    if r.error or r.timed_out or r.code != 0:
        return "trans", None, None
    ls = [l for l in r.stdout.splitlines() if l.strip()]
    if not ls:
        return "trans", None, None
    try:
        d = json.loads(ls[-1])
    except Exception:
        return "trans", None, None
    if d.get("error"):
        return "trans", None, None
    st = d.get("status")
    head = str(d.get("head") or "")
    v = P.classify(st, head[:160])
    ra = P.parse_retry_after(d.get("retry") or {}, head) if v != "OK" else None
    return v, st, ra


def run_batch(tags, family, idx0, out_map):
    d = json.load(open(BASE))
    ins = []
    rules = [{"action": "sniff"}]
    for i, tag in enumerate(tags):
        port = P0 + i
        ins.append({"type": "mixed", "tag": "in%d" % i,
                    "listen": "127.0.0.1", "listen_port": port})
        rules.append({"inbound": ["in%d" % i], "outbound": tag})
    d["inbounds"] = ins
    d["route"]["final"] = tags[0]
    # resolve 规则必须在 inbound 分流之前，否则被前置规则吃掉
    pre = []
    if family == "v6":
        pre.append({"action": "resolve",
                    "domain_suffix": ["opencode.ai", ".opencode.ai"],
                    "server": "dns-opencode", "strategy": "ipv6_only"})
        pre.append({"action": "resolve",
                    "domain_suffix": ["icanhazip.com", "api6.ipify.org"],
                    "server": "dns-opencode", "strategy": "ipv6_only"})
    d["route"]["rules"] = pre + rules
    d["experimental"] = {"cache_file": {"enabled": False}}
    d["log"] = {"level": "error"}
    cfg = "/tmp/p1_%d.json" % idx0
    json.dump(d, open(cfg, "w"))
    lg = open("/tmp/p1_%d.log" % idx0, "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                          stdout=lg, stderr=subprocess.STDOUT)
    time.sleep(3.5)

    def one(args):
        i, tag = args
        port = P0 + i
        ip = exit_ip(port, family)
        sid = P._session_id()
        v, st, ra = call_opencode(port, sid)
        tries = 0
        while v == "trans" and tries < 2:
            tries += 1
            time.sleep(1.5)
            v, st, ra = call_opencode(port, sid)
        rec = {"tag": tag, "ip": ip, "family": family, "verdict": v,
               "st": st, "ra": ra, "sid": sid, "trans_retried": tries}
        out_map[tag] = rec
        return rec

    with ThreadPoolExecutor(max_workers=len(tags)) as ex:
        list(ex.map(one, list(enumerate(tags))))

    pr.terminate()
    try:
        pr.wait(timeout=6)
    except Exception:
        pass


def main():
    a = sys.argv[1:]
    family = "v6"
    outf = None
    tags = []
    i = 0
    while i < len(a):
        if a[i] == "--v4":
            family = "v4"
            i += 1
        elif a[i] == "--v6":
            family = "v6"
            i += 1
        elif a[i] == "--out":
            outf = a[i + 1]
            i += 2
        else:
            tags.append(a[i])
            i += 1
    out_map = {}
    nb = (len(tags) + MAX_IN - 1) // MAX_IN
    for b in range(nb):
        chunk = tags[b * MAX_IN:(b + 1) * MAX_IN]
        print("== batch %d/%d  (%s)  %d 个 ==" % (b + 1, nb, family, len(chunk)), flush=True)
        try:
            run_batch(chunk, family, b, out_map)
        except Exception as e:
            print("   batch err: %s" % e, flush=True)
        for t in chunk:
            r = out_map.get(t) or {"verdict": "skip"}
            print("   %-46s %-46s %-6s %s"
                  % (t[:46], (r.get("ip") or "-")[:46], r.get("verdict"),
                     ("ra=%s" % r.get("ra")) if r.get("ra") else ""), flush=True)

    print("\n===== 汇总 =====", flush=True)
    ok = [r for r in out_map.values() if r.get("verdict") == "OK"]
    q = [r for r in out_map.values() if r.get("verdict") == "quota"]
    tr = [r for r in out_map.values() if r.get("verdict") == "trans"]
    print("OK=%d  quota=%d  trans=%d  共 %d" % (len(ok), len(q), len(tr), len(out_map)))
    print("\n--- 有额度（可用作对照组）---", flush=True)
    for r in ok:
        print("  %-46s %s" % (r["tag"], r["ip"]), flush=True)
    if q:
        print("\n--- 余额为零 ---", flush=True)
        for r in q:
            print("  %-46s %-44s ra=%s" % (r["tag"], r["ip"], r.get("ra")), flush=True)
    res = list(out_map.values())
    json.dump(res, open(outf or ("probe1_%s.json" % family), "w"),
              ensure_ascii=False, indent=1)
    print("\nDONE")


if __name__ == "__main__":
    main()
