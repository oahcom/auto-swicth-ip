import re, json, os, tempfile
import probe_warp as P
from asip.proc import run as run_proc

log = open("daemon.log", errors="ignore").read().splitlines()[-300:]
bad_port = good_port = None
for line in reversed(log):
    if "rate429" in line and "目标url" in line:
        m = re.search(r"slot-(\d+)", line)
        u = re.search(r"目标url=http://127\.0\.0\.1:(\d+)", line)
        if m and u:
            bad_port = 7890 + int(m.group(1))
            good_port = int(u.group(1))
            print("LIVE 429 line:", line.strip()[:150], flush=True)
            break
if not bad_port:
    print("no live 429 line found", flush=True)
    raise SystemExit(1)
print("BAD_PORT=%d GOOD_PORT=%d" % (bad_port, good_port), flush=True)

txt = "TCP provides reliable ordered delivery with flow control and congestion avoidance. " * 3000
body = {"model": P.FREEMODEL, "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Reference:\n" + txt},
    {"role": "user", "content": "say ok"}],
    "stream": True, "tools": P.FINGERPRINT_TOOLS}
BODY = json.dumps(P._apply_env_marker(body))
SID = P._session_id()
bun = P._bun_bin()
print("body=%d sid=%s" % (len(BODY), SID[:16]), flush=True)

def send(port, tag):
    payload = {"url": P.URL, "method": "POST", "headers": P._headers(SID),
               "body": BODY, "proxy": "http://127.0.0.1:" + str(port),
               "timeoutMs": 18000}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f: json.dump(payload, f)
    r = run_proc([bun, P.BUN_SCRIPT, path], timeout=22)
    os.remove(path)
    ls = [l for l in r.stdout.splitlines() if l.strip()]
    if not ls:
        print(tag, "-> NO_OUTPUT", flush=True); return None
    d = json.loads(ls[-1]); st = d.get("status")
    print(tag, "->", st, P.classify(st, str(d.get("head", ""))[:50]), flush=True)
    return st

a = None
for i in range(3):
    a = send(bad_port, "A_bad#%d" % (i+1))
    if a == 429: break
b = send(good_port, "B_good_same_request")
c = send(bad_port, "C_bad_again")
print("RESULT A(bad)=%s B(good,same)=%s C(bad)=%s" % (a, b, c), flush=True)
