# -*- coding: utf-8 -*-
"""同一出口 IP 的容量实验。

用法: python3 zz_cap3b.py <mode> <port> <max>
  mode=same     : 全程固定一个 session
  mode=each     : 每发都换全新 session
  mode=every10  : 每 10 发换一个 session

每次遇到 429 立刻用一个全新 session 复核：
  复核仍 429  -> 该出口整体被封（层 A / IP 层），实验停止
  复核 200    -> 429 属于会话层（层 B），继续
"""
import probe_warp as P, json, os, tempfile, time, sys
from asip.proc import run as run_proc

bun = P._bun_bin()
BODY = json.dumps(P._body())


def call(proxy, url, headers, body, timeout_ms=25000, method="POST"):
    payload = {"url": url, "method": method, "headers": headers, "body": body,
               "proxy": proxy, "timeoutMs": timeout_ms}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    try:
        r = run_proc([bun, P.BUN_SCRIPT, path], timeout=timeout_ms / 1000.0 + 6)
    finally:
        os.remove(path)
    if r.error or r.timed_out or r.code != 0:
        return {"status": None, "head": "", "retry": {}, "err": "trans"}
    ls = [l for l in r.stdout.splitlines() if l.strip()]
    if not ls:
        return {"status": None, "head": "", "retry": {}, "err": "noout"}
    try:
        d = json.loads(ls[-1])
    except Exception:
        return {"status": None, "head": "", "retry": {}, "err": "badjson"}
    if d.get("error"):
        return {"status": None, "head": "", "retry": {}, "err": str(d["error"])[:60]}
    return {"status": d.get("status"), "head": str(d.get("head") or ""),
            "retry": d.get("retry") or {}, "err": None}


def ip_of(proxy):
    for u in ["https://icanhazip.com", "https://api.ipify.org", "https://ifconfig.me/ip"]:
        r = call(proxy, u, {"User-Agent": "curl/8.5.0"}, None, 12000, "GET")
        if r["err"] is None and r["status"] == 200 and r["head"].strip():
            v = r["head"].strip().splitlines()[0].strip()
            if v:
                return v
    return "?"


def one(proxy, sid):
    r = call(proxy, P.URL, P._headers(sid), BODY, 25000)
    v = "trans" if r["err"] else P.classify(r["status"], r["head"][:160])
    ra = P.parse_retry_after(r["retry"], r["head"]) if v == "quota" else None
    return r, v, ra


MODE = sys.argv[1]
PORT = int(sys.argv[2])
MAXN = int(sys.argv[3])
proxy = None if PORT == 0 else "http://127.0.0.1:%d" % PORT
IP0 = ip_of(proxy)
print("START mode=%s port=%d ip=%s body=%d max=%d t=%s" % (
    MODE, PORT, IP0, len(BODY), MAXN, time.strftime("%H:%M:%S")), flush=True)

sid = None
rows = []
n_ok = 0
n_sess = 0
t0 = time.time()
stop = "max"

for i in range(1, MAXN + 1):
    if MODE == "each":
        sid = P._session_id()
        n_sess += 1
    elif MODE == "every10":
        if sid is None or (i - 1) % 10 == 0:
            sid = P._session_id()
            n_sess += 1
    elif sid is None:
        sid = P._session_id()
        n_sess += 1

    r, v, ra = one(proxy, sid)
    row = {"i": i, "sid": sid[:14], "verdict": v, "status": r["status"],
           "ra": ra, "t": round(time.time() - t0, 1), "err": r["err"]}
    if v == "OK":
        n_ok += 1
    rows.append(row)
    if v != "OK" or i % 10 == 0:
        print("#%-3d %-6s st=%-4s ra=%-6s sid=%s  [ok=%d t=%.0fs]" % (
            i, v, r["status"], ra, sid[:14], n_ok, time.time() - t0), flush=True)

    if v != "OK":
        # 全新 session 复核：区分「会话层」和「出口层」
        sid2 = P._session_id()
        r2, v2, ra2 = one(proxy, sid2)
        IP1 = ip_of(proxy)
        print("   CORROBORATE new_session -> %-6s st=%s ra=%s | ip %s -> %s same=%s" % (
            v2, r2["status"], ra2, IP0, IP1, IP0 == IP1), flush=True)
        rows.append({"i": i, "corroborate": True, "verdict": v2,
                     "status": r2["status"], "ra": ra2, "sid": sid2[:14],
                     "ip_before": IP0, "ip_after": IP1})
        if v2 == "OK":
            sid = sid2
            n_sess += 1
            continue
        stop = "egress_blocked"
        break

print("END mode=%s ip=%s ok=%d sent=%d sessions=%d sec=%.0f stop=%s" % (
    MODE, IP0, n_ok, len([x for x in rows if not x.get("corroborate")]), n_sess,
    time.time() - t0, stop), flush=True)

with open("cap3_%s_%d.json" % (MODE, PORT), "w") as f:
    json.dump({"mode": MODE, "port": PORT, "ip": IP0, "body": len(BODY),
               "ok": n_ok, "sessions": n_sess, "stop": stop,
               "sec": round(time.time() - t0, 1), "rows": rows}, f, indent=2)
