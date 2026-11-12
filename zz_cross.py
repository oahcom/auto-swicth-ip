# -*- coding: utf-8 -*-
import probe_warp as P, json, os, tempfile, time, urllib.request
from asip.proc import run as run_proc
bun = P._bun_bin()
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def direct_ip():
    for u in ["https://icanhazip.com", "https://ifconfig.me/ip"]:
        try:
            with op.open(u, timeout=3) as r: return r.read().decode().strip()
        except Exception: pass
    return "?"
REAL = "TCP provides reliable ordered delivery with flow control and congestion avoidance. "
BODY = json.dumps(P._apply_env_marker({
    "model": P.FREEMODEL,
    "messages": [{"role": "system", "content": "You are a helpful assistant."},
                 {"role": "user", "content": "Reference:\n" + REAL * 800},
                 {"role": "user", "content": "say ok"}],
    "stream": True, "tools": P.FINGERPRINT_TOOLS}))
SID = P._session_id()
H = P._headers(SID)          # 冻结：trace/session 头不再重新生成
def send(proxy, tag, timeout_ms=18000):
    payload = {"url": P.URL, "method": "POST", "headers": H,
               "body": BODY, "proxy": proxy, "timeoutMs": timeout_ms}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f: json.dump(payload, f)
    try: r = run_proc([bun, P.BUN_SCRIPT, path], timeout=timeout_ms / 1000 + 5)
    finally: os.remove(path)
    if r.error or r.timed_out or r.code != 0:
        print("%-22s -> TRANS" % tag, flush=True); return None, "trans"
    ls = [l for l in r.stdout.splitlines() if l.strip()]
    if not ls: print("%-22s -> TRANS" % tag, flush=True); return None, "trans"
    try: d = json.loads(ls[-1])
    except Exception: print("%-22s -> TRANS" % tag, flush=True); return None, "trans"
    if d.get("error"): print("%-22s -> TRANS(%s)" % (tag, str(d["error"])[:36]), flush=True); return None, "trans"
    st = d.get("status"); v = P.classify(st, str(d.get("head") or "")[:80])
    print("%-22s -> %-4s %s" % (tag, st, v), flush=True)
    return st, v
WARP_PORT = 1086
IP0 = direct_ip()
print("CHANNEL_A=warp1086 CHANNEL_B=DIRECT BODY=%d SID=%s IP0=%s" % (len(BODY), SID[:18], IP0), flush=True)
hit = 0
with P._LocalBridge(WARP_PORT) as WARP:
    print("bridge=%s" % WARP, flush=True)
    for i in range(25):
        a, av = send(WARP, "A_scan#%d" % (i + 1))
        if a == 429:
            hit = i + 1
            print("=== WARP 429 at scan#%d ; SAME payload -> DIRECT ===" % hit, flush=True)
            b, bv = send(None, "B_direct_same")
            c, cv = send(WARP, "C_warp_recheck")
            IP1 = direct_ip()
            print("IP0=%s IP1=%s stable=%s" % (IP0, IP1, IP0 == IP1 and IP0 != "?"), flush=True)
            print("RESULT A=429 B=%s(%s) C=%s(%s)" % (b, bv, c, cv), flush=True)
            if c != 429: print(">>> warp recovered, group INVALID", flush=True)
            elif b == 200: print(">>> VERDICT: EGRESS_ONLY (content/session not marked)", flush=True)
            else: print(">>> VERDICT: IDENTITY_MARKED or global window", flush=True)
            break
print("DONE hit=%s" % (hit or "NONE"), flush=True)
