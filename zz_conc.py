# -*- coding: utf-8 -*-
"""并发突发实验：同一 IP 同时打 N 发，看承载。

用法: python3 zz_conc.py <node> <concurrency> <seq_max>
"""
import probe_warp as P, json, os, subprocess, sys, tempfile, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "singbox-config.json-slot3.json"
CFG = "/tmp/cap-conc.json"
PORT = 7003
CLASH = 9100
SECRET = "cap-conc-secret"
BODY = json.dumps(P._body())

NODE = sys.argv[1]
CONC = int(sys.argv[2]) if len(sys.argv) > 2 else 20
SEQMAX = int(sys.argv[3]) if len(sys.argv) > 3 else 60

d = json.load(open(BASE))
d["inbounds"] = [{"type": "mixed", "tag": "cap-in", "listen": "127.0.0.1", "listen_port": PORT}]
d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") != "cap-out"]
d["outbounds"].append({"type": "selector", "tag": "cap-out", "outbounds": [NODE], "default": NODE})
d["route"]["rules"] = [dict(r, outbound="cap-out") if r.get("outbound") == "opencode-out" else r
                       for r in d["route"]["rules"]]
d["route"]["final"] = "cap-out"
d["experimental"] = {"clash_api": {"external_controller": "127.0.0.1:%d" % CLASH, "secret": SECRET},
                     "cache_file": {"enabled": False}}
d["log"] = {"level": "warn", "timestamp": True}
json.dump(d, open(CFG, "w"))


def ip_of():
    try:
        r = subprocess.run(["curl", "-s", "--max-time", "8",
                            "--proxy", "http://127.0.0.1:%d" % PORT,
                            "https://icanhazip.com"], capture_output=True, text=True)
        v = (r.stdout or "").strip()
        return v if v and len(v) < 60 else "?"
    except Exception:
        return "?"


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
        return {"status": None, "head": "", "retry": {}, "err": "trans"}
    ls = [l for l in r.stdout.splitlines() if l.strip()]
    if not ls:
        return {"status": None, "head": "", "retry": {}, "err": "noout"}
    try:
        dd = json.loads(ls[-1])
    except Exception:
        return {"status": None, "head": "", "retry": {}, "err": "badjson"}
    if dd.get("error"):
        return {"status": None, "head": "", "retry": {}, "err": str(dd["error"])[:60]}
    return {"status": dd.get("status"), "head": str(dd.get("head") or ""),
            "retry": dd.get("retry") or {}, "err": None}


def fire(i):
    sid = P._session_id()
    t0 = time.time()
    r = call(P._headers(sid), BODY)
    v = "trans" if r["err"] else P.classify(r["status"], r["head"][:160])
    return {"i": i, "verdict": v, "status": r["status"],
            "ra": P.parse_retry_after(r["retry"], r["head"]) if v == "quota" else None,
            "sec": round(time.time() - t0, 1), "err": r["err"]}


log = open("/tmp/cap-conc.log", "w")
proc = subprocess.Popen(["./sing-box", "run", "-c", CFG], stdout=log, stderr=subprocess.STDOUT)
rows = []
try:
    time.sleep(3)
    IP0 = ip_of()
    print("node=%s ip=%s conc=%d seq=%d t=%s" % (NODE[:34], IP0, CONC, SEQMAX,
                                                 time.strftime("%H:%M:%S")), flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        res = list(ex.map(fire, range(1, CONC + 1)))
    ok = sum(1 for r in res if r["verdict"] == "OK")
    q = sum(1 for r in res if r["verdict"] == "quota")
    tr = sum(1 for r in res if r["verdict"] == "trans")
    print("BURST n=%d ok=%d quota=%d trans=%d wall=%.1fs" % (CONC, ok, q, tr, time.time() - t0), flush=True)
    for r in sorted(res, key=lambda x: x["i"]):
        print("   burst#%-3d %-6s st=%-4s %.1fs" % (r["i"], r["verdict"], r["status"], r["sec"]), flush=True)
    rows += [dict(r, phase="burst") for r in res]
    if q == 0:
        n_ok = 0
        for i in range(1, SEQMAX + 1):
            r = fire(i)
            rows.append(dict(r, phase="seq"))
            if r["verdict"] == "OK":
                n_ok += 1
            if r["verdict"] != "OK" or i % 10 == 0:
                print("  seq#%-3d %-6s st=%-4s ra=%s [seq_ok=%d]" % (
                    i, r["verdict"], r["status"], r["ra"], n_ok), flush=True)
            if r["verdict"] != "OK":
                break
        print("SEQ_TOTAL ok=%d after_burst(%d)" % (n_ok, CONC), flush=True)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    log.close()
json.dump({"node": NODE, "ip": IP0, "conc": CONC, "rows": rows},
          open("cap_conc_%d.json" % int(time.time()), "w"), indent=2)
