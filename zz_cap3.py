# -*- coding: utf-8 -*-
"""同 IP 容量实验：扫端口 -> 找干净出口。"""
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
    for u in ["https://icanhazip.com", "https://api.ipify.org"]:
        r = call(proxy, u, {"User-Agent": "curl/8.5.0"}, None, 12000, "GET")
        if r["err"] is None and r["status"] == 200 and r["head"].strip():
            return r["head"].strip().splitlines()[0].strip()
    return "?"


PORTS = [7890, 7892, 7893, 7894, 7895, 7896]
out = []
for p in PORTS:
    proxy = "http://127.0.0.1:%d" % p
    ip = ip_of(proxy)
    sid = P._session_id()
    t0 = time.time()
    r = call(proxy, P.URL, P._headers(sid), BODY, 25000)
    v = "trans" if r["err"] else P.classify(r["status"], r["head"][:120])
    ra = None
    if v == "quota":
        ra = P.parse_retry_after(r["retry"], r["head"])
    out.append({"port": p, "ip": ip, "verdict": v, "status": r["status"],
                "sec": round(time.time() - t0, 1), "retry_after": ra, "err": r["err"]})
    print("port=%-5d ip=%-28s -> %-6s status=%s %.1fs ra=%s %s" % (
        p, ip, v, r["status"], time.time() - t0, ra, r["err"] or ""), flush=True)

with open("node_cap3_scan.json", "w") as f:
    json.dump({"ts": time.time(), "body": len(BODY), "rows": out}, f, indent=2)
print("SCAN_DONE", flush=True)
