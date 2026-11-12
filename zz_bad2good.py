# -*- coding: utf-8 -*-
"""坏节点上的 session 去好节点是否也 429。

1) 坏节点 A 上，session S 打 1 发  -> 期望 429
2) 好节点 B 上，全新 session S2 打 1 发 -> 确认 B 干净（同时作对照）
3) 好节点 B 上，同一个 session S 打 1 发 -> 关键判定

S 在 B 上 OK   -> 坏节点的 429 不传染 session，限流单位是 IP
S 在 B 上 429  -> 该 session 已废，跨 IP 仍 429

用法: python3 zz_bad2good.py <A_tag> <B候选...>
"""
import json, os, subprocess, sys, tempfile, time, urllib.request
import probe_warp as P

BASE = "singbox-config.json-slot3.json"
CFG = "/tmp/bad2good.json"
PORT = 7032
CLASH = 9132
SECRET = "b2g"
BODY = json.dumps(P._body())
OUT = "bad2good_result.json"

A_TAG = sys.argv[1]
CANDS = sys.argv[2:]
R = {"A": A_TAG, "spent": 0, "log": []}


def say(s):
    print(s, flush=True)
    R["log"].append(s)


def build(tags):
    d = json.load(open(BASE))
    have = {o.get("tag") for o in d["outbounds"]}
    tags = [t for t in tags if t in have]
    d["inbounds"] = [{"type": "mixed", "tag": "in", "listen": "127.0.0.1", "listen_port": PORT}]
    d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") not in ("cap-out", "cap-tri")]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": tags, "default": tags[0]})
    d["route"]["rules"] = []
    d["route"]["final"] = "cap-out"
    d["experimental"] = {"clash_api": {"external_controller": "127.0.0.1:%d" % CLASH,
                                       "secret": SECRET},
                         "cache_file": {"enabled": False}}
    d["log"] = {"level": "error"}
    json.dump(d, open(CFG, "w"))


def select(tag):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/proxies/cap-out" % CLASH,
        data=json.dumps({"name": tag}).encode(), method="PUT",
        headers={"Authorization": "Bearer " + SECRET, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return True
    except Exception:
        return False


def ip_of(t=6):
    for u in ("https://icanhazip.com", "https://api.ipify.org"):
        try:
            r = subprocess.run(["curl", "-s", "--max-time", str(t),
                                "--proxy", "http://127.0.0.1:%d" % PORT, u],
                               capture_output=True, text=True)
            v = (r.stdout or "").strip().splitlines()
            if v and v[0].strip():
                return v[0].strip()
        except Exception:
            pass
    return None


def call(sid, timeout_ms=35000):
    from asip.proc import run as run_proc
    bun = P._bun_bin()
    payload = {"url": P.URL, "method": "POST", "headers": P._headers(sid), "body": BODY,
               "proxy": "http://127.0.0.1:%d" % PORT, "timeoutMs": timeout_ms}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    try:
        r = run_proc([bun, P.BUN_SCRIPT, path], timeout=timeout_ms / 1000.0 + 8)
    finally:
        os.remove(path)
    if r.error or r.timed_out or r.code != 0:
        return "trans", None, None
    ls = [l for l in r.stdout.splitlines() if l.strip()]
    if not ls:
        return "trans", None, None
    try:
        d = json.loads(ls[-1])
    except Exception:
        return "trans", None, None
    if d.get("error"):
        return "trans", None, None
    st = d.get("status"); head = str(d.get("head") or "")
    v = P.classify(st, head[:160])
    ra = P.parse_retry_after(d.get("retry") or {}, head) if v != "OK" else None
    return v, st, ra


def same24(a, b):
    if not a or not b or "." not in a or "." not in b:
        return a == b
    return ".".join(a.split(".")[:3]) == ".".join(b.split(".")[:3])


def main():
    build([A_TAG] + CANDS)
    log = open("/tmp/bad2good.log", "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", CFG], stdout=log, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    try:
        # ---- 1) 坏节点上，S 打 1 发 ----
        select(A_TAG); time.sleep(0.9)
        ipA = ip_of()
        say("[1] 坏节点 %s -> 出口 %s" % (A_TAG, ipA))
        S = P._session_id()
        say("    session S = %s" % S)
        v, st, ra = call(S)
        R["spent"] += 1
        say("    S 在坏节点上 -> %-6s st=%s ra=%s  (消耗 %d)" % (v, st, ra, R["spent"]))
        R["step1"] = {"ip": ipA, "sid": S, "verdict": v, "ra": ra}
        if v == "OK":
            say("\n!! 这个节点是活的，不是坏节点。换一个。")
            R["error"] = "A_alive"
            return

        # ---- 2) 找好节点 B，全新 session 打 1 发 ----
        B = None
        for tag in CANDS:
            if tag == A_TAG:
                continue
            if not select(tag):
                continue
            time.sleep(1.0)
            ipB = ip_of()
            if not ipB:
                say("    NO_IP %s" % tag)
                continue
            if ipB == ipA:
                say("    %s 出口与 A 相同，跳过" % tag)
                continue
            S2 = P._session_id()
            v2, st2, ra2 = call(S2)
            R["spent"] += 1
            say("[2] %-40s ip=%-16s 全新session -> %-6s ra=%s  (消耗 %d)"
                % (tag, ipB, v2, ra2, R["spent"]))
            if v2 == "OK":
                B = {"tag": tag, "ip": ipB, "ctrl_sid": S2,
                     "ctrl_verdict": v2, "same24": same24(ipB, ipA)}
                break
        if not B:
            say("\n!! 没找到干净节点")
            R["error"] = "no_clean_B"
            return
        R["B"] = B

        # ---- 3) 同一个 S 在好节点上打 1 发 ----
        say("\n[3] 关键：同一个 session S 在好节点 %s 上打 1 发 ..." % B["ip"])
        v3, st3, ra3 = call(S)
        R["spent"] += 1
        say("    -> %-6s st=%s ra=%s  (总消耗 %d 发)" % (v3, st3, ra3, R["spent"]))
        R["step3"] = {"verdict": v3, "ra": ra3}

        if v3 == "OK":
            R["verdict"] = "坏节点上的 429 不传染 session：同一 session 换到干净 IP 立刻恢复 -> 限流单位是 IP"
        else:
            R["verdict"] = "该 session 已被判死：同一 session 换到干净 IP 仍 429 -> 限流单位包含 session"
        say("\n>>> " + R["verdict"])

    finally:
        pr.terminate()
        try:
            pr.wait(timeout=6)
        except Exception:
            pr.kill()
        log.close()
        json.dump(R, open(OUT, "w"), indent=1, ensure_ascii=False)
        say("\n结果已存 " + OUT)


if __name__ == "__main__":
    main()
