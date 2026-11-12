# -*- coding: utf-8 -*-
"""大请求体（~500KB，对齐生产 _originalSize）容量实验。

用法: python3 zz_big.py <node> <max>
"""
import probe_warp as P, json, os, subprocess, sys, tempfile, time, urllib.request

BASE = "singbox-config.json-slot3.json"
CFG = "/tmp/cap-big.json"
PORT = 7004
CLASH = 9101
SECRET = "cap-big-secret"

NODE = sys.argv[1]
MAXN = int(sys.argv[2]) if len(sys.argv) > 2 else 8

REAL = ("TCP provides reliable ordered delivery with flow control and "
        "congestion avoidance. ")
BODY = json.dumps(P._apply_env_marker({
    "model": P.FREEMODEL,
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Reference:\n" + REAL * 5400},
        {"role": "user", "content": "say ok"},
    ],
    "stream": True,
    "tools": P.FINGERPRINT_TOOLS,
}))

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


def call(headers, body, timeout_ms=90000):
    from asip.proc import run as run_proc
    bun = P._bun_bin()
    payload = {"url": P.URL, "method": "POST", "headers": headers, "body": body,
               "proxy": "http://127.0.0.1:%d" % PORT, "timeoutMs": timeout_ms}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    try:
        r = run_proc([bun, P.BUN_SCRIPT, path], timeout=timeout_ms / 1000.0 + 10)
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


log = open("/tmp/cap-big.log", "w")
proc = subprocess.Popen(["./sing-box", "run", "-c", CFG], stdout=log, stderr=subprocess.STDOUT)
rows = []
try:
    time.sleep(3)
    IP0 = ip_of()
    print("node=%s ip=%s body=%d max=%d t=%s" % (NODE[:32], IP0, len(BODY), MAXN,
                                                 time.strftime("%H:%M:%S")), flush=True)
    sid = P._session_id()
    n_ok = 0
    t0 = time.time()
    for i in range(1, MAXN + 1):
        r = call(P._headers(sid), BODY)
        v = "trans" if r["err"] else P.classify(r["status"], r["head"][:160])
        ra = P.parse_retry_after(r["retry"], r["head"]) if v == "quota" else None
        rows.append({"i": i, "verdict": v, "status": r["status"], "ra": ra,
                     "t": round(time.time() - t0, 1), "err": r["err"]})
        if v == "OK":
            n_ok += 1
        print("#%-3d %-6s st=%-4s ra=%-6s t=%.0fs%s" % (
            i, v, r["status"], ra, time.time() - t0,
            (" " + r["err"]) if r["err"] else ""), flush=True)
        if v == "quota":
            sid2 = P._session_id()
            r2 = call(P._headers(sid2), BODY)
            v2 = "trans" if r2["err"] else P.classify(r2["status"], r2["head"][:160])
            print("   CORROB new_session -> %s st=%s" % (v2, r2["status"]), flush=True)
            if v2 == "OK":
                sid = sid2
                continue
            break
        if v == "trans":
            continue
    print("END ok=%d ip=%s sec=%.0f" % (n_ok, IP0, time.time() - t0), flush=True)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    log.close()
json.dump({"node": NODE, "ip": IP0, "body": len(BODY), "rows": rows},
          open("cap_big_%d.json" % int(time.time()), "w"), indent=2)
