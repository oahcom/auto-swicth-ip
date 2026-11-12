# -*- coding: utf-8 -*-
# 直连通道：proxy=None，出口 = 服务器真实 IP，daemon 换线/重启 usque 都碰不到。
import probe_warp as P, json, os, tempfile, time, urllib.request
from asip.proc import run as run_proc

bun = P._bun_bin()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def direct_ip():
    try:
        with _opener.open("https://api.ipify.org", timeout=4) as r:
            return r.read().decode().strip()
    except Exception:
        return "?"


REAL = "TCP provides reliable ordered delivery with flow control and congestion avoidance. "
body_obj = {
    "model": P.FREEMODEL,
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Reference:\n" + REAL * 480},
        {"role": "user", "content": "say ok"},
    ],
    "stream": True,
    "tools": P.FINGERPRINT_TOOLS,
}
BODY = json.dumps(P._apply_env_marker(body_obj))
BODY2 = BODY.replace("TCP provides reliable", "UDP provides reliable", 1)
assert BODY2 != BODY and len(BODY2) == len(BODY), "body variant failed"

SID0 = P._session_id()
SID1 = P._session_id()
H0 = P._headers(SID0)
SKEYS = [k for k in H0 if "session" in k.lower()]


def hdr(sid):
    h = dict(H0)
    for k in SKEYS:
        h[k] = sid
    return h


def send(headers, body, tag, timeout_ms=15000):
    payload = {"url": P.URL, "method": "POST", "headers": headers,
               "body": body, "proxy": None,
               "timeoutMs": timeout_ms}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    try:
        r = run_proc([bun, P.BUN_SCRIPT, path], timeout=timeout_ms / 1000 + 5)
    finally:
        os.remove(path)
    if r.error or r.timed_out or r.code != 0:
        why = str(r.error or ("timeout" if r.timed_out else "rc=%s" % r.code))[:50]
        print("%-24s -> TRANS(%s)" % (tag, why), flush=True)
        return None, "trans"
    ls = [l for l in r.stdout.splitlines() if l.strip()]
    if not ls:
        print("%-24s -> TRANS(no output)" % tag, flush=True)
        return None, "trans"
    try:
        d = json.loads(ls[-1])
    except Exception:
        print("%-24s -> TRANS(bad json)" % tag, flush=True)
        return None, "trans"
    if d.get("error"):
        print("%-24s -> TRANS(%s)" % (tag, str(d["error"])[:42]), flush=True)
        return None, "trans"
    st = d.get("status")
    head = str(d.get("head") or "")
    v = P.classify(st, head[:80])
    print("%-24s -> %-4s %s" % (tag, st, v), flush=True)
    return st, v


print("CHANNEL=DIRECT proxy=None BODY=%d SID0=%s SID1=%s SKEYS=%s" %
      (len(BODY), SID0[:18], SID1[:18], SKEYS), flush=True)
IP_START = direct_ip()
print("DIRECT_IP_START=%s" % IP_START, flush=True)

groups = 0
for i in range(60):
    a, av = send(hdr(SID0), BODY, "scan#%d" % (i + 1))
    if a != 429:
        continue
    print("--- 429 CAUGHT at scan#%d ---" % (i + 1), flush=True)
    b, bv = send(hdr(SID1), BODY, "B_only_new_session")
    c, cv = send(hdr(SID0), BODY2, "C_only_new_body")
    d, dv = send(hdr(SID0), BODY, "D_original_recheck")
    IP_END = direct_ip()
    print("DIRECT_IP_END=%s stable=%s" % (IP_END, IP_START == IP_END and IP_START != "?"), flush=True)
    print("RESULT A=429 B=%s(%s) C=%s(%s) D=%s(%s)" %
          (b, bv, c, cv, d, dv), flush=True)
    if IP_START == "?" or IP_START != IP_END:
        print(">>> IP UNVERIFIED/DRIFT, group INVALID", flush=True)
    elif d != 429:
        print(">>> recovery / transport, group INVALID", flush=True)
    elif b == 200:
        print(">>> VERDICT: SESSION_IDENTITY_BYPASS", flush=True)
    elif c == 200:
        print(">>> VERDICT: CONTENT_IDENTITY_BYPASS", flush=True)
    else:
        print(">>> VERDICT: NO_IDENTITY_BYPASS (window / IP quota)", flush=True)
    groups += 1
    if groups >= 3:
        break
    time.sleep(1)

print("DONE groups=%d" % groups, flush=True)
