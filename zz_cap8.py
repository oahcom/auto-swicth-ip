# -*- coding: utf-8 -*-
"""单 IP 容量终版实验：顺序/并发/v4v6/复测，四合一。

私有 sing-box 实例（7012 / 9100），outbound 钉死单个机场节点，不碰 daemon 与生产槽位。

阶段:
  P0 复测：今天早前已经打出 429 的那几个 IP，现在用全新 session 还 429 吗
  P1 识别：每个候选节点的 v4 出口 与 v6 出口（api.ipify.org / api6.ipify.org）
  P2 顺序：每发都换全新 session，跑到第一次非 200；过程中每 15 发校验出口 IP 未变
  P3 并发：全新 IP 上，20 路同时打（各自全新 session），整批全过再来一批，直到出现失败
  P4 复测：P2/P3 打死的 IP，在 +5min / +15min 各用全新 session 复测一次
"""
import probe_warp as P, json, os, subprocess, sys, tempfile, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "singbox-config.json-slot3.json"
CFG = "/tmp/cap-test8.json"
PORT = 7012
CLASH = 9100
SECRET = "cap-test-secret8"
BODY = json.dumps(P._body())
CHECK_EVERY = 15
SEQ_LIMIT = 200
CONC_K = 20
CONC_ROUNDS = 12

# 未测过的（新 IP）
FRESH = ["ddixx-1dd1.dankenet.help:10309-any",     # 45.95.212.151
         "upoz.cn.greatgameupload.org:13493-any",   # 82.27.116.33
         "upoz.cn.greatgameupload.org:32345-any",   # 82.152.160.3
         "normal.cn.greatgameupload.org:29512-any",  # 188.253.117.93
         "ddixx-1dd1.dankenet.help:64245-any"]      # 45.95.212.122
# 今天早前已经 429 过的（用于 P0 复测）
BURNED = ["ttll20-ooi.dankenet.help:32236-any",     # 45.95.212.167
          "ttll20-ooi.dankenet.help:17121-any",     # 45.95.212.211
          "upoz.cn.greatgameupload.org:20470-any"]  # 82.27.116.31

ALL = BURNED + FRESH
RES = {"p0": [], "p1": {}, "p2": [], "p3": {}, "p4": []}


def build(allowed):
    d = json.load(open(BASE))
    have = {o.get("tag") for o in d["outbounds"]}
    allowed = [t for t in allowed if t in have]
    d["inbounds"] = [{"type": "mixed", "tag": "cap-in",
                      "listen": "127.0.0.1", "listen_port": PORT}]
    d["outbounds"] = [o for o in d["outbounds"]
                      if o.get("tag") not in ("cap-out", "cap-tri")]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": allowed, "default": allowed[0]})
    d["route"]["rules"] = [dict(r, outbound="cap-out") if r.get("outbound") == "opencode-out" else r
                           for r in d["route"]["rules"]]
    d["route"]["final"] = "cap-out"
    d["experimental"] = {"clash_api": {"external_controller": "127.0.0.1:%d" % CLASH,
                                       "secret": SECRET},
                         "cache_file": {"enabled": False}}
    d["log"] = {"level": "error", "timestamp": True}
    json.dump(d, open(CFG, "w"))
    return allowed


def select(tag):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/proxies/cap-out" % CLASH,
        data=json.dumps({"name": tag}).encode(), method="PUT",
        headers={"Authorization": "Bearer " + SECRET,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode()[:60]
    except Exception as e:
        return "ERR " + str(e)[:40]


def ip_of(url="https://api.ipify.org", t=8):
    try:
        r = subprocess.run(["curl", "-s", "--max-time", str(t),
                            "--proxy", "http://127.0.0.1:%d" % PORT, url],
                           capture_output=True, text=True)
        v = (r.stdout or "").strip().splitlines()
        return v[0].strip() if v and v[0].strip() else None
    except Exception:
        return None


def call(headers, body, timeout_ms=30000):
    from asip.proc import run as run_proc
    bun = P._bun_bin()
    payload = {"url": P.URL, "method": "POST", "headers": headers, "body": body,
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
    st = d.get("status")
    head = str(d.get("head") or "")
    v = P.classify(st, head[:160])
    ra = P.parse_retry_after(d.get("retry") or {}, head) if v != "OK" else None
    return v, st, ra


def shot(sid, tries=2):
    for _ in range(tries):
        v, st, ra = call(P._headers(sid), BODY)
        if v != "trans":
            return v, st, ra
        time.sleep(1)
    return "trans", None, None


def burn_seq(tag, limit=SEQ_LIMIT, label="P2"):
    """顺序：每发全新 session，跑到第一次非 OK。"""
    select(tag)
    time.sleep(0.8)
    IP0 = ip_of()
    print("\n[%s] node=%s ip=%s t=%s" % (label, tag, IP0, time.strftime("%H:%M:%S")), flush=True)
    if not IP0:
        print("  NO_IP", flush=True)
        return None
    sids = set()
    n_ok = 0
    t0 = time.time()
    stop = "limit"
    fail_ra = None
    for i in range(1, limit + 1):
        sid = P._session_id()
        assert sid not in sids
        sids.add(sid)
        v, st, ra = shot(sid)
        if v == "OK":
            n_ok += 1
        else:
            stop = v
            fail_ra = ra
            print("  #%-3d %s st=%s ra=%s  ==> 顺序上限 ok=%d 用时%.0fs" % (
                i, v, st, ra, n_ok, time.time() - t0), flush=True)
            break
        if i % CHECK_EVERY == 0:
            ipn = ip_of()
            if ipn != IP0:
                print("  !! IP漂移 %s->%s 作废" % (IP0, ipn), flush=True)
                stop = "IP_MOVED"
                break
            print("  .. #%d ok=%d %.0fs ip=%s" % (i, n_ok, time.time() - t0, ipn), flush=True)

    rec = {"stage": label, "node": tag, "ip": IP0, "ok": n_ok, "issued": len(sids),
           "stop": stop, "retry_after": fail_ra, "sec": round(time.time() - t0, 1),
           "at": time.time(), "v6": RES["p1"].get(tag, {}).get("v6")}
    if stop not in ("limit", "IP_MOVED"):
        # 复核：3 个全新 session + 10s/20s
        conf = []
        for k in range(3):
            s2 = P._session_id()
            v2, st2, ra2 = shot(s2)
            conf.append([v2, st2, ra2])
        time.sleep(10)
        s2 = P._session_id(); v2, st2, ra2 = shot(s2); conf.append(["t+10s", v2, ra2])
        time.sleep(15)
        s2 = P._session_id(); v2, st2, ra2 = shot(s2); conf.append(["t+25s", v2, ra2])
        rec["corroborate"] = conf
        print("  复核(全新session x3 + t10 + t25): %s" % [c[0] for c in conf], flush=True)
        print("  429 当场出口 ip=%s (开跑时 %s)" % (ip_of(), IP0), flush=True)
    print("  --> %s ok=%d stop=%s" % (label, n_ok, stop), flush=True)
    return rec


def burn_conc(tag, k=CONC_K, rounds=CONC_ROUNDS):
    """并发：k 路同时打（各自全新 session），整批全过再来一批。"""
    select(tag)
    time.sleep(0.8)
    IP0 = ip_of()
    print("\n[P3] node=%s ip=%s K=%d t=%s" % (tag, IP0, k, time.strftime("%H:%M:%S")), flush=True)
    if not IP0:
        print("  NO_IP", flush=True)
        return None
    total_ok = 0
    sids = set()
    t0 = time.time()
    detail = []
    stop = "rounds"
    for r in range(1, rounds + 1):
        batch = []
        for _ in range(k):
            s = P._session_id()
            sids.add(s)
            batch.append(s)
        bt = time.time()
        with ThreadPoolExecutor(max_workers=k) as ex:
            outs = list(ex.map(lambda s: shot(s, 1), batch))
        ok = sum(1 for o in outs if o[0] == "OK")
        kinds = {}
        for o in outs:
            kinds[o[0]] = kinds.get(o[0], 0) + 1
        total_ok += ok
        el = time.time() - bt
        detail.append({"round": r, "ok": ok, "kinds": kinds, "sec": round(el, 1)})
        print("  批%d: %d/%d 成功 %s 用时%.1fs 累计ok=%d" % (r, ok, k, kinds, el, total_ok),
              flush=True)
        if ok < k:
            stop = "batch_partial"
            break
        ipn = ip_of()
        if ipn != IP0:
            print("  !! IP漂移 作废", flush=True)
            stop = "IP_MOVED"
            break
    rec = {"node": tag, "ip": IP0, "k": k, "rounds_done": len(detail),
           "total_ok": total_ok, "issued": len(sids), "stop": stop,
           "sec": round(time.time() - t0, 1), "at": time.time(), "detail": detail,
           "v6": RES["p1"].get(tag, {}).get("v6")}
    print("  --> P3 ok=%d 轮=%d stop=%s 用时%.0fs" % (total_ok, len(detail), stop, time.time() - t0),
          flush=True)
    return rec


def main():
    allowed = build(ALL)
    log = open("/tmp/cap-sing8.log", "w")
    proc = subprocess.Popen(["./sing-box", "run", "-c", CFG],
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        time.sleep(3)
        # ---------- P0 复测已 429 的 IP ----------
        print("=== P0 复测今天早前 429 过的 IP（全新 session，各 1 发）===", flush=True)
        for tag in BURNED:
            select(tag)
            time.sleep(0.7)
            ip = ip_of()
            s = P._session_id()
            v, st, ra = shot(s)
            RES["p0"].append({"node": tag, "ip": ip, "verdict": v, "status": st,
                              "retry_after": ra, "at": time.time()})
            print("  %-42s ip=%-16s -> %-6s st=%s ra=%s" % (tag, ip, v, st, ra), flush=True)

        # ---------- P1 v4/v6 识别 ----------
        print("\n=== P1 每个节点的 v4 / v6 出口 ===", flush=True)
        for tag in ALL:
            select(tag)
            time.sleep(0.6)
            v4 = ip_of("https://api.ipify.org", 8)
            v6 = ip_of("https://api6.ipify.org", 8)
            RES["p1"][tag] = {"v4": v4, "v6": v6}
            print("  %-42s v4=%-16s v6=%s" % (tag, v4, v6), flush=True)

        # ---------- P2 顺序上限（两个全新 IP）----------
        for tag in FRESH[:2]:
            r = burn_seq(tag, label="P2")
            if r:
                RES["p2"].append(r)

        # ---------- P3 并发上限（另一个全新 IP）----------
        r = burn_conc(FRESH[2])
        if r:
            RES["p3"] = r

        # ---------- P2 第二发补测（若时间允许）----------
        if len(FRESH) > 3:
            r = burn_seq(FRESH[3], label="P2b")
            if r:
                RES["p2"].append(r)

        # ---------- P4 +5min / +15min 复测 ----------
        targets = []
        for r in RES["p2"]:
            if r["stop"] not in ("limit", "IP_MOVED"):
                targets.append((r["node"], r["ip"], r["at"]))
        if RES["p3"] and RES["p3"]["stop"] == "batch_partial":
            targets.append((RES["p3"]["node"], RES["p3"]["ip"], RES["p3"]["at"]))
        for node, ip, at in targets:
            for delay in (300, 900):
                wait = at + delay - time.time()
                if wait > 0:
                    print("  等待 %ds 到 t+%dmin 复测 %s" % (wait, delay // 60, ip), flush=True)
                    time.sleep(min(wait, 900))
                select(node)
                time.sleep(0.6)
                s = P._session_id()
                v, st, ra = shot(s)
                RES["p4"].append({"node": node, "ip": ip, "after": "t+%dmin" % (delay // 60),
                                  "verdict": v, "status": st, "retry_after": ra})
                print("  P4 %-16s t+%dmin -> %-6s st=%s ra=%s" % (ip, delay // 60, v, st, ra),
                      flush=True)

        json.dump(RES, open("cap8_result.json", "w"), indent=2, ensure_ascii=False)
        print("\n=== 汇总 ===", flush=True)
        for r in RES["p2"]:
            print("P2 %-16s ok=%-4d stop=%s" % (r["ip"], r["ok"], r["stop"]), flush=True)
        if RES["p3"]:
            print("P3 %-16s 并发%d 累计ok=%d 轮=%d stop=%s" % (
                RES["p3"]["ip"], RES["p3"]["k"], RES["p3"]["total_ok"],
                RES["p3"]["rounds_done"], RES["p3"]["stop"]), flush=True)
        for r in RES["p4"]:
            print("P4 %-16s %-6s -> %s" % (r["ip"], r["after"], r["verdict"]), flush=True)
    except Exception as e:
        import traceback; traceback.print_exc()
        json.dump(RES, open("cap8_result.json", "w"), indent=2, ensure_ascii=False)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log.close()


main()
