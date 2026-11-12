# -*- coding: utf-8 -*-
"""对一组节点各发 1 发（新 session），查看当前状态与恢复情况。"""
import probe_warp as P, json, os, subprocess, sys, tempfile, time, urllib.request

BASE = "singbox-config.json-slot3.json"
CFG = "/tmp/cap-test7.json"
PORT = 7001
CLASH = 9098
SECRET = "cap-test-secret"
BODY = json.dumps(P._body())

CANDIDATES = ["cn.bing.com:443~5-any", "cn.bing.com:443~6-any",
              "cn.bing.com:443~7-any", "cn.bing.com:443~8-any",
              "cn.bing.com:443~9-any", "cn.bing.com:443~10-any"]


def build():
    d = json.load(open(BASE))
    have = {o.get("tag") for o in d["outbounds"]}
    cands = [c for c in CANDIDATES if c in have]
    d["inbounds"] = [{"type": "mixed", "tag": "cap-in",
                      "listen": "127.0.0.1", "listen_port": PORT}]
    d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") != "cap-out"]
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


def main():
    cands = build()
    log = open("/tmp/cap-sing7.log", "w")
    proc = subprocess.Popen(["./sing-box", "run", "-c", CFG],
                            stdout=log, stderr=subprocess.STDOUT)
    rows = []
    try:
        time.sleep(3)
        for tag in cands:
            select(tag)
            time.sleep(0.7)
            ip = ip_of()
            sid = P._session_id()
            r = call(P._headers(sid), BODY)
            v = "trans" if r["err"] else P.classify(r["status"], r["head"][:160])
            ra = P.parse_retry_after(r["retry"], r["head"]) if v == "quota" else None
            rows.append({"tag": tag, "ip": ip, "verdict": v, "status": r["status"], "ra": ra})
            print("%-24s ip=%-18s -> %-6s st=%-4s ra=%s" % (
                tag.replace("cn.bing.com:443", "bing"), ip, v, r["status"], ra), flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log.close()
    json.dump({"ts": time.time(), "t": time.strftime("%H:%M:%S"), "rows": rows},
              open("cap_now_%s.json" % time.strftime("%H%M%S"), "w"), indent=2)


main()
