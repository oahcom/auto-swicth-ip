# -*- coding: utf-8 -*-
import probe_warp as P, json, os, tempfile, time, urllib.request
from asip.proc import run as run_proc
bun = P._bun_bin()
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def direct_ip():
    for u in ["https://icanhazip.com", "https://ifconfig.me/ip", "https://api.ipify.org"]:
        try:
            with op.open(u, timeout=3) as r:
                return r.read().decode().strip()
        except Exception:
            pass
    return "?"
REAL = "TCP provides reliable ordered delivery with flow control and congestion avoidance. "
BODY = json.dumps(P._apply_env_marker({
    "model": P.FREEMODEL,
    "messages": [{"role": "system", "content": "You are a helpful assistant."},
                 {"role": "user", "content": "Reference:\n" + REAL * 800},
                 {"role": "user", "content": "say ok"}],
    "stream": True, "tools": P.FINGERPRINT_TOOLS}))
BODY2 = BODY.replace("TCP provides reliable", "UDP provides reliable", 1)
assert BODY2 != BODY and len(BODY2) == len(BODY)
SID0, SID1 = P._session_id(), P._session_id()
H0 = P._headers(SID0)
SKEYS = [k for k in H0 if "session" in k.lower()]
def hdr(sid):
    h = dict(H0)
    for k in SKEYS: h[k] = sid
    return h
def send(headers, body, tag):
    payload = {"url": P.URL, "method": "POST", "headers": headers,
               "body": body, "proxy": None, "timeoutMs": 10000}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f: json.dump(payload, f)
    try: r = run_proc([bun, P.BUN_SCRIPT, path], timeout=15)
    finally: os.remove(path)
    if r.error or r.timed_out or r.code != 0: return None, "trans"
    ls = [l for l in r.stdout.splitlines() if l.strip()]
    if not ls: return None, "trans"
    try: d = json.loads(ls[-1])
    except Exception: return None, "trans"
    if d.get("error"): return None, "trans"
    return d.get("status"), P.classify(d.get("status"), str(d.get("head") or "")[:80])
IP0 = direct_ip()
print("CHANNEL=DIRECT proxy=None BODY=%d IP0=%s SID0=%s SID1=%s" % (len(BODY), IP0, SID0[:18], SID1[:18]), flush=True)
hit = 0
for i in range(200):
    st, v = send(hdr(SID0), BODY, "burn#%d" % (i + 1))
    if st != 200 or (i + 1) % 25 == 0:
        print("burn#%-3d -> %-4s %s" % (i + 1, st, v), flush=True)
    if st == 429:
        hit = i + 1
        print("=== REAL 429 at burn#%d ===" % hit, flush=True)
        b, bv = send(hdr(SID1), BODY, "B_new_session")
        c, cv = send(hdr(SID0), BODY2, "C_new_body")
        d, dv = send(hdr(SID0), BODY, "D_original")
        IP1 = direct_ip()
        print("IP0=%s IP1=%s stable=%s" % (IP0, IP1, IP0 == IP1 and IP0 != "?"), flush=True)
        print("RESULT A=429 B=%s(%s) C=%s(%s) D=%s(%s)" % (b, bv, c, cv, d, dv), flush=True)
        if d != 429: print(">>> transient, INVALID", flush=True)
        elif b == 200: print(">>> SESSION_IDENTITY_BYPASS", flush=True)
        elif c == 200: print(">>> CONTENT_IDENTITY_BYPASS", flush=True)
        else: print(">>> NO_IDENTITY_BYPASS", flush=True)
        break
print("DONE hit=%s total_sent=%d" % (hit or "NONE", min(i + 1, 200)), flush=True)
