# -*- coding: utf-8 -*-
"""独立出口扫描：起一个私有 sing-box 实例，逐个切换机场节点，测首发。

不碰 daemon 的 slot（789x 的 opencode-out 会被 daemon 切到 WARP 隧道），
也不查生产出口：本实例 final=cap-out，查 IP 与模型请求走同一个节点。
"""
import probe_warp as P, json, os, subprocess, sys, tempfile, time, urllib.request

BASE = "singbox-config.json-slot3.json"
CFG = "/tmp/cap-test.json"
PORT = 7001
CLASH = 9098
SECRET = "cap-test-secret"
BODY = json.dumps(P._body())

CANDIDATES = [
    "hk-aws-122.acczzz.com:443-any",
    "hk-aws-54.acczzz.com:443-any",
    "hk-aws-53.acczzz.com:443-any",
    "hk-aws-111.acczzz.com:443-any",
    "hk-aws-123.acczzz.com:443-any",
    "hk-aws-124.acczzz.com:443-any",
    "hk-aws-244.acczzz.com:443-any",
    "sg-aws-109.acczzz.com:443-any",
    "jp-aws-110.acczzz.com:443-any",
    "jp-aws-118.acczzz.com:443-any",
]


def build():
    d = json.load(open(BASE))
    have = {o.get("tag") for o in d["outbounds"]}
    cands = [c for c in CANDIDATES if c in have]
    d["inbounds"] = [{"type": "mixed", "tag": "cap-in",
                      "listen": "127.0.0.1", "listen_port": PORT}]
    d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") not in ("cap-out",)]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": cands, "default": cands[0]})
    rules = []
    for r in d["route"]["rules"]:
        if r.get("outbound") == "opencode-out":
            r = dict(r, outbound="cap-out")
        rules.append(r)
    d["route"]["rules"] = rules
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
    for u in ["https://icanhazip.com", "https://ip.sb"]:
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


def call(url, headers, body, timeout_ms=25000, method="POST"):
    bun = P._bun_bin()
    payload = {"url": url, "method": method, "headers": headers, "body": body,
               "proxy": "http://127.0.0.1:%d" % PORT, "timeoutMs": timeout_ms}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    try:
        from asip.proc import run as run_proc
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
    print("candidates=%d cfg=%s" % (len(cands), CFG), flush=True)
    log = open("/tmp/cap-sing.log", "w")
    proc = subprocess.Popen(["./sing-box", "run", "-c", CFG],
                            stdout=log, stderr=subprocess.STDOUT)
    rows = []
    try:
        time.sleep(3)
        if select(cands[0]) != 204:
            print("clash api not ready", select(cands[0]), flush=True)
        for tag in cands:
            select(tag)
            time.sleep(0.8)
            ip = ip_of()
            sid = P._session_id()
            t0 = time.time()
            r = call(P.URL, P._headers(sid), BODY)
            v = "trans" if r["err"] else P.classify(r["status"], r["head"][:160])
            ra = P.parse_retry_after(r["retry"], r["head"]) if v == "quota" else None
            rows.append({"tag": tag, "ip": ip, "verdict": v, "status": r["status"],
                         "ra": ra, "sec": round(time.time() - t0, 1), "err": r["err"]})
            print("%-34s ip=%-30s -> %-6s st=%-4s ra=%-6s %.1fs %s" % (
                tag[:34], ip, v, r["status"], ra, time.time() - t0, r["err"] or ""),
                flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log.close()
    json.dump({"ts": time.time(), "rows": rows}, open("cap4_scan.json", "w"), indent=2)
    good = [r for r in rows if r["verdict"] == "OK"]
    print("GOOD=%d/%d" % (len(good), len(rows)), flush=True)
    for g in good:
        print("  OK", g["tag"], g["ip"], flush=True)


main()
