# -*- coding: utf-8 -*-
"""严谨版单 IP 容量实验（私有 sing-box 实例，不碰生产槽位/daemon）。

用法:
  python3 zz_cap7.py scan  <N>              # 探测前 N 个节点的落地 IP
  python3 zz_cap7.py burn  <node_tag> <max> # 严格协议烧到 429

严格协议:
  1. 全程固定出口：起跑前、每 CHECK_EVERY 发、429 当场，各查一次出口 IP，
     任何一次变化即判 INVALID（数据作废，不参与统计）。
  2. 每发都用 _session_id() 现场生成全新 session，并断言集合内全局唯一。
  3. 只认 200 为有效成单；transport/noout 不计入额度，原地重试（上限 3 次）。
  4. 首次非 OK 立即用 3 个全新 session 连打复核，再等 10s / 30s 各用全新
     session 复核一次；全部仍非 OK 才判「出口层锁定」。
"""
import probe_warp as P, json, os, subprocess, sys, tempfile, time, urllib.request

BASE = "singbox-config.json-slot3.json"
CFG = "/tmp/cap-test7.json"
PORT = 7011
CLASH = 9099
SECRET = "cap-test-secret7"
BODY = json.dumps(P._body())
CHECK_EVERY = 15

MODE = sys.argv[1] if len(sys.argv) > 1 else "scan"
ARG1 = sys.argv[2] if len(sys.argv) > 2 else ""
ARG2 = int(sys.argv[3]) if len(sys.argv) > 3 else 200


def nodes():
    d = json.load(open(BASE))
    out = []
    for o in d["outbounds"]:
        if o.get("type") in ("selector", "urltest", "direct", "block", "dns"):
            continue
        out.append(o.get("tag"))
    return [t for t in out if t]


def build(allowed):
    d = json.load(open(BASE))
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
    d["log"] = {"level": "warn", "timestamp": True}
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
    return None


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


def shot(sid):
    """打一发。返回 (verdict, status, retry_after, err)。"""
    r = call(P._headers(sid), BODY)
    if r["err"]:
        return "trans", None, None, r["err"]
    v = P.classify(r["status"], r["head"][:160])
    ra = P.parse_retry_after(r["retry"], r["head"]) if v != "OK" else None
    return v, r["status"], ra, None


def valid_shot(sid, tries=3):
    """transport 不计额度：原地重试，最多 tries 次。"""
    last = None
    for _ in range(tries):
        v, st, ra, err = shot(sid)
        if v != "trans":
            return v, st, ra
        last = (v, st, ra)
        time.sleep(1.5)
    return last


def main():
    all_tags = nodes()
    print("节点总数:", len(all_tags), flush=True)

    if MODE == "scan":
        n = int(ARG1 or 12)
        tags = all_tags[:n]
        build(tags)
        log = open("/tmp/cap-sing7.log", "w")
        proc = subprocess.Popen(["./sing-box", "run", "-c", CFG],
                                stdout=log, stderr=subprocess.STDOUT)
        res = {}
        try:
            time.sleep(3)
            for t in tags:
                select(t)
                time.sleep(0.6)
                ip = ip_of()
                res[t] = ip
                print("SCAN %-30s -> %s" % (t, ip), flush=True)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            log.close()
        json.dump(res, open("cap7_scan.json", "w"), indent=2)
        return

    # burn：单节点严格烧机
    tag = ARG1
    limit = ARG2
    build([tag])
    log = open("/tmp/cap-sing7.log", "w")
    proc = subprocess.Popen(["./sing-box", "run", "-c", CFG],
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        time.sleep(3)
        select(tag)
        time.sleep(0.8)
        IP0 = ip_of()
        print("NODE=%s IP=%s body=%dB limit=%d t=%s" % (
            tag, IP0, len(BODY), limit, time.strftime("%H:%M:%S")), flush=True)
        if not IP0:
            print("NO_EGRESS_IP", flush=True)
            return

        sids, rows = set(), []
        n_ok = 0
        stop = "limit"
        t0 = time.time()
        for i in range(1, limit + 1):
            sid = P._session_id()
            assert sid not in sids, "session 重复！"
            sids.add(sid)
            v, st, ra = valid_shot(sid)
            rows.append({"i": i, "v": v, "st": st, "ra": ra,
                         "sid": sid[:16], "t": round(time.time() - t0, 1)})

            if v == "OK":
                n_ok += 1
            else:
                # 立刻复核：3 个全新 session
                IP_fail = ip_of()
                print("#%-3d %-6s st=%s ra=%s  (ok=%d, sid唯一=%d, ip %s->%s)" % (
                    i, v, st, ra, n_ok, len(sids), IP0, IP_fail), flush=True)
                conf = []
                for k in range(3):
                    s2 = P._session_id(); sids.add(s2)
                    v2, st2, ra2 = valid_shot(s2)
                    conf.append((v2, st2, ra2))
                    print("   复核%d 新session -> %-6s st=%s ra=%s" % (k + 1, v2, st2, ra2),
                          flush=True)
                for wait in (10, 20):
                    time.sleep(wait)
                    s2 = P._session_id(); sids.add(s2)
                    v2, st2, ra2 = valid_shot(s2)
                    conf.append((v2, st2, ra2))
                    print("   +%ds 新session -> %-6s st=%s ra=%s" % (wait, v2, st2, ra2),
                          flush=True)
                rows.append({"corroborate": conf, "ip_at_fail": IP_fail})
                if all(c[0] != "OK" for c in conf):
                    stop = "egress_locked"
                else:
                    stop = "session_level"
                moved = IP_fail and IP_fail != IP0
                if moved:
                    stop = "IP_MOVED_INVALID"
                break

            if i % CHECK_EVERY == 0:
                ipn = ip_of()
                if ipn != IP0:
                    print("!! IP 漂移 %s -> %s，判 INVALID" % (IP0, ipn), flush=True)
                    stop = "IP_MOVED_INVALID"
                    break
                print("#%-3d ok 累计=%d 用时%.0fs ip 复核=%s" % (i, n_ok, time.time() - t0, ipn),
                      flush=True)
            elif i % 5 == 0:
                print("  .. #%d ok=%d %.0fs" % (i, n_ok, time.time() - t0), flush=True)

        out = {"node": tag, "ip": IP0, "ok": n_ok, "issued": len(sids),
               "uniq_sids": len(sids), "stop": stop,
               "sec": round(time.time() - t0, 1), "body": len(BODY), "rows": rows}
        print("END node=%s ip=%s ok=%d issued=%d uniq=%d sec=%.0f stop=%s" % (
            tag, IP0, n_ok, len(sids), len(sids), time.time() - t0, stop), flush=True)
        json.dump(out, open("cap7_%s.json" % tag.replace(":", "_").replace("/", "_"), "w"),
                  indent=2)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log.close()


main()
