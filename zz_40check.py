# -*- coding: utf-8 -*-
"""40 发到底是 session 上限还是 IP 额度 —— 同 IP 换 session 对照。

在一个干净 IP 上：
1) session S 连打，直到 429，记录 n
2) 同一 IP 上全新 session S2 打 1 发
   200 -> 额度按 (session, IP) 记，40 是 session 上限（用户假设成立）
   429 -> 额度按 IP 记，40 是该 IP 总额度（session 无关）
3) 再换一个全新 session S3 复核

用法: python3 zz_40check.py <干净节点tag> [备用节点...]
"""
import json, os, subprocess, sys, tempfile, time, urllib.request
import probe_warp as P

BASE = "singbox-config.json-slot3.json"
CFG = "/tmp/c40.json"
PORT = 7033
CLASH = 9133
SECRET = "c40"
BODY = json.dumps(P._body())
OUT = "c40_result.json"
CANDS = sys.argv[1:]
R = {"spent": 0, "log": []}


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


def main():
    build(CANDS)
    log = open("/tmp/c40.log", "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", CFG], stdout=log, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    try:
        # ---- 找一个干净节点 ----
        node = None
        for tag in CANDS:
            if not select(tag):
                continue
            time.sleep(1.0)
            ip = ip_of()
            if not ip:
                say("  NO_IP %s" % tag)
                continue
            S0 = P._session_id()
            v, st, ra = call(S0)
            R["spent"] += 1
            say("[探测] %-40s ip=%-16s -> %-6s  (消耗 %d)" % (tag, ip, v, R["spent"]))
            if v == "OK":
                node = {"tag": tag, "ip": ip}
                break
        if not node:
            say("没有干净节点")
            R["error"] = "no_clean"
            return
        R["node"] = node

        # ---- 1) 固定 session S 打到 429 ----
        S = P._session_id()
        say("\n[1] 在 %s 上用 session S=%s 连打 ..." % (node["ip"], S))
        n = 1  # 探测那发算 S0，这里重新用 S 从 0 开始
        n = 0
        why, ra = None, None
        while n < 250:
            v, st, ra = call(S)
            R["spent"] += 1
            if v == "OK":
                n += 1
                if n % 10 == 0:
                    say("    已 %d 发 ..." % n)
            elif v == "trans":
                say("    transport 中断，重试")
                continue
            else:
                why = v
                break
        ip_after = ip_of()
        say("    打出 %d 发后 %s ra=%s；出口 %s (未变=%s)"
            % (n, why, ra, ip_after, ip_after == node["ip"]))
        R["step1"] = {"sid": S, "ok": n, "stop": why, "ra": ra, "ip": ip_after}

        # ---- 2) 同一 IP 全新 session 打 1 发 ----
        say("\n[2] 同一 IP 上换全新 session ...")
        res = []
        for i in range(2):
            s = P._session_id()
            v, st, ra = call(s)
            R["spent"] += 1
            res.append({"sid": s[:20], "verdict": v, "ra": ra})
            say("    全新 session #%d -> %-6s ra=%s" % (i + 1, v, ra))
            time.sleep(0.7)
        R["step2"] = res

        ok_new = [r for r in res if r["verdict"] == "OK"]
        if ok_new:
            R["verdict"] = ("额度按 (session,IP) 记：同一 IP 上换新 session 立刻能用 "
                            "-> %d 发是单个 session 的上限" % n)
        else:
            R["verdict"] = ("额度按 IP 记：同一 IP 上换新 session 仍 429 "
                            "-> %d 发是这个 IP 的总额度，与 session 无关" % n)
        say("\n>>> " + R["verdict"])

    finally:
        pr.terminate()
        try:
            pr.wait(timeout=6)
        except Exception:
            pr.kill()
        log.close()
        json.dump(R, open(OUT, "w"), indent=1, ensure_ascii=False)
        say("\n总消耗 %d 发，结果存 %s" % (R["spent"], OUT))


if __name__ == "__main__":
    main()
