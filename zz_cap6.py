# -*- coding: utf-8 -*-
"""私有出口容量实验（固定机场节点，不碰生产 slot / WARP 隧道）。

用法: python3 zz_cap6.py <mode> <max>
  mode=each     : 每发都换全新 session
  mode=every10  : 每 10 发换一个 session
  mode=same     : 全程固定一个 session

每次 429 立即用全新 session 复核：仍 429 -> 出口层被封（停）；200 -> 会话层（继续）。
"""
import probe_warp as P, json, os, subprocess, sys, tempfile, time, urllib.request

BASE = "singbox-config.json-slot3.json"
CFG = "/tmp/cap-test6.json"
PORT = 7001
CLASH = 9098
SECRET = "cap-test-secret"
BODY = json.dumps(P._body())

CANDIDATES = ["cn.bing.com:443~6-any", "cn.bing.com:443~7-any",
              "cn.bing.com:443~8-any", "cn.bing.com:443~9-any",
              "cn.bing.com:443~10-any", "cn.bing.com:443~5-any"]

MODE = sys.argv[1] if len(sys.argv) > 1 else "every10"
MAXN = int(sys.argv[2]) if len(sys.argv) > 2 else 40
NODE = sys.argv[3] if len(sys.argv) > 3 else None


def build():
    d = json.load(open(BASE))
    have = {o.get("tag") for o in d["outbounds"]}
    cands = [c for c in CANDIDATES if c in have]
    if NODE and NODE in cands:
        cands = [NODE] + [c for c in cands if c != NODE]
    d["inbounds"] = [{"type": "mixed", "tag": "cap-in",
                      "listen": "127.0.0.1", "listen_port": PORT}]
    d["outbounds"] = [o for o in d["outbounds"]
                      if o.get("tag") not in ("cap-out", "cap-tri")]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": cands, "default": cands[0]})
    d["route"]["rules"] = [dict(r, outbound="cap-out") if r.get("outbound") == "opencode-out" else r
                           for r in d["route"]["rules"]]
    d["route"]["final"] = "cap-out"
    d["experimental"] = {"clash_api": {"external_controller": "127.0.0.1:%d" % CLASH,
                                       "secret": SECRET},
                         "cache_file": {"enabled": False}}
    d["log"] = {"level": "warn", "timestamp": True}
    json.dump(d, open(CFG, "w"))
    return cands


def select(tag):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/proxies/cap-out" % CLASH,
        data=json.dumps({"name": tag}).encode(), method="PUT",
        headers={"Authorization": "Bearer " + SECRET,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except Exception as e:
        return str(e)[:40]


def ip_of():
    for u in ["https://icanhazip.com", "https://api.ipify.org"]:
        try:
            r = subprocess.run(["curl", "-s", "--max-time", "8",
                                "--proxy", "http://127.0.0.1:%d" % PORT, u],
                               capture_output=True, text=True)
            v = (r.stdout or "").strip().splitlines()
            if v and v[0].strip():
                return v[0].strip()
        except Exception:
            pass
    return "?"


def call(headers, body, timeout_ms=25000):
    from asip.proc import run as run_proc
    bun = P._bun_bin()
    payload = {"url": P.URL, "method": "POST", "headers": headers, "body": body,
               "proxy": "http://127.0.0.1:%d" % PORT, "timeoutMs": timeout_ms}
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


def one(sid):
    r = call(P._headers(sid), BODY)
    v = "trans" if r["err"] else P.classify(r["status"], r["head"][:160])
    ra = P.parse_retry_after(r["retry"], r["head"]) if v == "quota" else None
    return r, v, ra


def main():
    cands = build()
    log = open("/tmp/cap-sing6.log", "w")
    proc = subprocess.Popen(["./sing-box", "run", "-c", CFG],
                            stdout=log, stderr=subprocess.STDOUT)
    rows = []
    try:
        time.sleep(3)
        chosen, IP0 = None, "?"
        for tag in cands:
            select(tag)
            time.sleep(0.7)
            ip = ip_of()
            print("probe %-26s ip=%s" % (tag, ip), flush=True)
            if ip != "?":
                chosen, IP0 = tag, ip
                break
        if not chosen:
            print("NO_USABLE_NODE", flush=True)
            return
        if MODE == "scan":
            for tag in cands:
                select(tag)
                time.sleep(0.7)
                print("SCAN %-26s ip=%s" % (tag, ip_of()), flush=True)
            print("SCAN_DONE", flush=True)
            return
        print("CHOSEN=%s IP=%s mode=%s max=%d body=%d t=%s" % (
            chosen, IP0, MODE, MAXN, len(BODY), time.strftime("%H:%M:%S")), flush=True)

        sid = None
        n_ok = 0
        n_sess = 0
        stop = "max"
        t0 = time.time()
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
            r, v, ra = one(sid)
            rows.append({"i": i, "sid": sid[:14], "verdict": v, "status": r["status"],
                         "ra": ra, "t": round(time.time() - t0, 1), "err": r["err"]})
            if v == "OK":
                n_ok += 1
            if v != "OK" or i % 10 == 0:
                print("#%-3d %-6s st=%-4s ra=%-6s sid=%s [ok=%d t=%.0fs]" % (
                    i, v, r["status"], ra, sid[:14], n_ok, time.time() - t0), flush=True)
            if v != "OK":
                sid2 = P._session_id()
                r2, v2, ra2 = one(sid2)
                IP1 = ip_of()
                print("   CORROB a_new_session -> %-6s st=%s ra=%s | ip %s -> %s same=%s" % (
                    v2, r2["status"], ra2, IP0, IP1, IP0 == IP1), flush=True)
                rows.append({"i": i, "corroborate": True, "verdict": v2,
                             "status": r2["status"], "ra": ra2, "sid": sid2[:14],
                             "ip": IP1})
                if v2 == "OK":
                    sid = sid2
                    n_sess += 1
                    continue
                stop = "egress_blocked"
                break
        print("END mode=%s ip=%s ok=%d sessions=%d sec=%.0f stop=%s" % (
            MODE, IP0, n_ok, n_sess, time.time() - t0, stop), flush=True)
        json.dump({"mode": MODE, "node": chosen, "ip": IP0, "body": len(BODY),
                   "ok": n_ok, "sessions": n_sess, "stop": stop,
                   "sec": round(time.time() - t0, 1), "rows": rows},
                  open("cap6_%s.json" % MODE, "w"), indent=2)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log.close()


main()
