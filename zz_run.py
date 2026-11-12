import probe_warp as P, json, os, tempfile
from asip.proc import run as run_proc

bun = P._bun_bin()
txt = "TCP provides reliable ordered delivery with flow control and congestion avoidance. " * 3000
body = {"model": P.FREEMODEL, "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Reference:\n" + txt},
    {"role": "user", "content": "say ok"}],
    "stream": True, "tools": P.FINGERPRINT_TOOLS}
BODY = json.dumps(P._apply_env_marker(body))
SID = P._session_id()
print("body=%d sid=%s" % (len(BODY), SID[:16]), flush=True)

def send(port, tag):
    try:
        with P._LocalBridge(port) as proxy:
            payload = {"url": P.URL, "method": "POST", "headers": P._headers(SID),
                       "body": BODY, "proxy": proxy, "timeoutMs": 20000}
            fd, path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            r = run_proc([bun, P.BUN_SCRIPT, path], timeout=24)
            os.remove(path)
            ls = [l for l in r.stdout.splitlines() if l.strip()]
            if not ls:
                print("%s -> NO_OUTPUT" % tag, flush=True)
                return None
            d = json.loads(ls[-1])
            st = d.get("status")
            print("%s -> %s | %s" % (tag, st, str(d.get("head", ""))[:90]), flush=True)
            return st
    except Exception as e:
        print("%s -> ERR %s" % (tag, str(e)[:70]), flush=True)
        return None

GOOD = None
for p in [1081, 1082, 1087]:
    if send(p, "GOOD_CHECK_%d" % p) == 200:
        GOOD = p
        break
print("GOOD=%s" % GOOD, flush=True)

BAD = None
A = None
for p in [1086, 1080, 1088, 1082, 1087]:
    st = send(p, "A_CAND_%d" % p)
    if st == 429:
        BAD = p
        A = st
        break
print("BAD=%s A=%s" % (BAD, A), flush=True)

if BAD is not None and GOOD is not None and GOOD != BAD:
    B = send(GOOD, "B_GOOD_SAME_REQUEST")
    C = send(BAD, "C_BAD_AGAIN")
    print("RESULT A=429 B=%s C=%s" % (B, C), flush=True)
    if B == 429:
        print("VERDICT=MARKED", flush=True)
    elif B == 200:
        print("VERDICT=IP_ONLY", flush=True)
    else:
        print("VERDICT=INCONCLUSIVE", flush=True)
else:
    print("VERDICT=NO_PAIR", flush=True)
