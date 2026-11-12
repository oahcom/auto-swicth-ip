# -*- coding: utf-8 -*-
"""IPv4 vs IPv6 出口额度对照。

同一节点、同一账号，唯一变量：出口地址族（route.resolve strategy 强制 ipv4_only / ipv6_only）。
用法: python3 zz_v6.py <family|both> <node_tag> <limit>
"""
import json, os, subprocess, sys, tempfile, time, urllib.request
import probe_warp as P

BASE = "singbox-config.json-slot3.json"
PORT = 7013
CLASH = 9101
SECRET = "v6test"
BODY = json.dumps(P._body())
FAMILY = sys.argv[1] if len(sys.argv) > 1 else "both"
NODE = sys.argv[2] if len(sys.argv) > 2 else "upoz.cn.greatgameupload.org:20470-any"
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 200


def build(family):
    d = json.load(open(BASE))
    d["inbounds"] = [{"type": "mixed", "tag": "cap-in",
                      "listen": "127.0.0.1", "listen_port": PORT}]
    d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") not in ("cap-out", "cap-tri")]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": [NODE], "default": NODE})
    rules = [dict(r, outbound="cap-out") if r.get("outbound") == "opencode-out" else r
             for r in d["route"]["rules"]]
    rules = [r for r in rules if r.get("action") != "resolve"]
    rules.append({"action": "resolve", "domain_suffix": ["opencode.ai", ".opencode.ai"],
                  "strategy": family})
    d["route"]["rules"] = rules
    d["route"]["final"] = "cap-out"
    d["experimental"] = {"clash_api": {"external_controller": "127.0.0.1:%d" % CLASH,
                                       "secret": SECRET},
                         "cache_file": {"enabled": False}}
    d["log"] = {"level": "error", "timestamp": True}
    json.dump(d, open("/tmp/cap-v6-run.json", "w"))


def call(headers, body, timeout_ms=30000):
    from asip.proc import run as run_proc
    bun = P._bun_bin()
    payload = {"url": P.URL, "method": "POST", "headers": headers, "body": body,
               "proxy": "http://127.0.0.1:%d" % PORT, "timeoutMs": timeout_ms}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    try:
        r = run_proc([bun, P.BUN_SCRIPT, path], timeout=timeout_ms / 1000.0 + 8)
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
    st = d.get("status"); head = str(d.get("head") or "")
    v = P.classify(st, head[:160])
    ra = P.parse_retry_after(d.get("retry") or {}, head) if v != "OK" else None
    return v, st, ra


def ip_of(url, t=8):
    try:
        r = subprocess.run(["curl", "-s", "--max-time", str(t),
                            "--proxy", "http://127.0.0.1:%d" % PORT, url],
                           capture_output=True, text=True)
        v = (r.stdout or "").strip().splitlines()
        return v[0].strip() if v and v[0].strip() else None
    except Exception:
        return None


def run_family(family, limit):
    build(family)
    log = open("/tmp/capv6-%s.log" % family, "w")
    proc = subprocess.Popen(["./sing-box", "run", "-c", "/tmp/cap-v6-run.json"],
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        time.sleep(3)
        ip6 = ip_of("https://api6.ipify.org")
        ip4 = ip_of("https://api.ipify.org")
        print("[%s] node=%s v6出口=%s v4出口=%s" % (family, NODE, ip6, ip4), flush=True)
        n_ok = 0
        t0 = time.time()
        stop = "limit"
        for i in range(1, limit + 1):
            v, st, ra = call(P._headers(P._session_id()), BODY)
            if v == "OK":
                n_ok += 1
            else:
                stop = "%s(ra=%s)" % (v, ra)
                print("  #%d -> %s" % (i, stop), flush=True)
                break
            if i % 10 == 0:
                print("  .. #%d ok=%d %.0fs" % (i, n_ok, time.time() - t0), flush=True)
        print("[%s] END ok=%d stop=%s sec=%.0f" % (family, n_ok, stop, time.time() - t0),
              flush=True)
        return {"family": family, "node": NODE, "v4": ip4, "v6": ip6,
                "ok": n_ok, "stop": stop, "sec": round(time.time() - t0, 1)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log.close()


res = []
if FAMILY in ("ipv4_only", "ipv6_only"):
    res.append(run_family(FAMILY, LIMIT))
else:
    res.append(run_family("ipv4_only", LIMIT))
    res.append(run_family("ipv6_only", LIMIT))
json.dump(res, open("cap_v6_result.json", "w"), indent=2, ensure_ascii=False)
print(json.dumps(res, ensure_ascii=False, indent=1))
