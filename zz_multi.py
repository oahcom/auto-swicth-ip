import probe_warp as P, json, os, tempfile, time
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
PORTS = [1080, 1081, 1082, 1086, 1087]
GOOD = 1081
TMO = 7000
TARGET = 8
MAX_SEND = 220
DEADLINE = time.time() + 430

print("body=%d sid=%s target=%d_events max=%d" % (len(BODY), SID[:16], TARGET, MAX_SEND), flush=True)

def send(port):
    try:
        with P._LocalBridge(port) as proxy:
            payload = {"url": P.URL, "method": "POST", "headers": P._headers(SID),
                       "body": BODY, "proxy": proxy, "timeoutMs": TMO}
            fd, path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            r = run_proc([bun, P.BUN_SCRIPT, path], timeout=TMO / 1000 + 4)
            os.remove(path)
            ls = [l for l in r.stdout.splitlines() if l.strip()]
            if not ls:
                return None
            d = json.loads(ls[-1])
            return d.get("status")
    except Exception:
        return None

events = []
cnt = {200: 0, 429: 0, None: 0}
n = 0
rnd = 0
while len(events) < TARGET and n < MAX_SEND and time.time() < DEADLINE:
    rnd += 1
    seq = []
    for p in PORTS:
        if time.time() > DEADLINE:
            break
        st = send(p); n += 1
        cnt[st] = cnt.get(st, 0) + 1
        seq.append("%d:%s" % (p, st))
        if st == 429:
            gp = GOOD if GOOD != p else 1082
            b = send(gp); n += 1
            cnt[b] = cnt.get(b, 0) + 1
            c = send(p); n += 1
            cnt[c] = cnt.get(c, 0) + 1
            events.append((rnd, p, b, c, gp))
            seq.append(">>>A=429@%d B=%s@%d C=%s" % (p, b, gp, c))
    print("R%-2d %s" % (rnd, " ".join(seq)), flush=True)

print(flush=True)
print("=== 429 事件 (轮,坏端口,B好端口,C回坏端口,好端口) ===", flush=True)
for e in events:
    print("  r%d bad=%d B=%s@%d C=%s" % (e[0], e[1], e[2], e[4], e[3]), flush=True)
print("=== 汇总 ===", flush=True)
print("轮=%d 总发=%d 200=%d 429=%d None=%d 事件=%d" % (
    rnd, sum(cnt.values()), cnt.get(200, 0), cnt.get(429, 0), cnt.get(None, 0), len(events)), flush=True)
if events:
    b200 = sum(1 for e in events if e[2] == 200)
    b429 = sum(1 for e in events if e[2] == 429)
    bnone = len(events) - b200 - b429
    print("换好端口后: 200=%d 429=%d None=%d" % (b200, b429, bnone), flush=True)
    print("VERDICT=%s" % ("MARKED_OR_MIXED" if b429 else "IP_ONLY"), flush=True)
else:
    print("VERDICT=NO_429_IN_SAMPLE", flush=True)
