# -*- coding: utf-8 -*-
"""v7: 并发测 v6 出口上限。
mode:
  single  : N 路并发共用 1 个固定 session  -> 单会话发数额度(L2)
  fixedN  : N 个固定 session, N 路并发, 各打各的 -> 会话+IP 交叉极限
  rotate  : N 路并发, 每发换新 session      -> 撞 L1
每 50 发校验出口 IP, 漂移立即作废。
用法: python3 zz_v7.py <idx> <tag> <mode> <conc> <maxtotal>
"""
import json, os, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
import probe_warp as P

BASE = "singbox-config.json-slot3.json"
FORCE_V6 = "--v4" not in sys.argv
BODY = json.dumps(P._body())
LOCK = threading.Lock()
STOP = threading.Event()
TOTAL = {"n": 0}
TRANS = {"n": 0}
DEATH = []
PER = {}


def build(idx, tag, port):
    cfg = "/tmp/v7_%d.json" % idx
    d = json.load(open(BASE))
    d["inbounds"] = [{"type": "mixed", "tag": "in",
                      "listen": "127.0.0.1", "listen_port": port}]
    d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") != "cap-out"]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": [tag], "default": tag})
    d["route"]["final"] = "cap-out"
    d["dns"]["rules"] = [
        {"domain_suffix": ["okzdns.com", "juhazf.cn", "hi2026.net", "acczzz.com",
                           "bing.com"], "server": "dns-airport", "strategy": "ipv4_only"},
        {"domain_suffix": ["opencode.ai", ".opencode.ai"], "server": "dns-opencode"},
    ]
    d["route"]["rules"] = [
        {"action": "sniff"},
        {"domain_suffix": ["opencode.ai"], "outbound": "cap-out"},
    ]
    if FORCE_V6:
        d["route"]["rules"] += [
            {"action": "resolve", "domain_suffix": ["opencode.ai", ".opencode.ai"],
             "server": "dns-opencode", "strategy": "ipv6_only"},
            {"action": "resolve", "domain_suffix": ["icanhazip.com", "api6.ipify.org"],
             "server": "dns-opencode", "strategy": "ipv6_only"},
        ]
    d["experimental"] = {"cache_file": {"enabled": False}}
    d["log"] = {"level": "error"}
    json.dump(d, open(cfg, "w"))
    return cfg


def ip_of(port, t=6):
    urls = ("https://icanhazip.com", "https://api6.ipify.org") if FORCE_V6 \
        else ("https://icanhazip.com", "https://api.ipify.org")
    for u in urls:
        for _ in range(2):
            try:
                r = subprocess.run(["curl", "-s", "--max-time", str(t),
                                    "--proxy", "http://127.0.0.1:%d" % port, u],
                                   capture_output=True, text=True)
                v = (r.stdout or "").strip().splitlines()
                if v and v[0].strip():
                    return v[0].strip()
            except Exception:
                pass
    return None


def call(port, sid, timeout_ms=35000):
    from asip.proc import run as run_proc
    bun = P._bun_bin()
    payload = {"url": P.URL, "method": "POST", "headers": P._headers(sid),
               "body": BODY, "proxy": "http://127.0.0.1:%d" % port,
               "timeoutMs": timeout_ms}
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


def run(idx, tag, mode, conc, maxtotal):
    port = 7300 + idx
    cfg = build(idx, tag, port)
    lg = open("/tmp/v7_%d.log" % idx, "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                          stdout=lg, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    R = {"idx": idx, "tag": tag, "mode": mode, "conc": conc, "maxtotal": maxtotal}
    tagl = "[%s]" % mode
    try:
        ip0 = ip_of(port)
        R["ip_start"] = ip0
        fam = "v6" if (ip0 and ":" in ip0) else ("v4" if ip0 else "none")
        R["family"] = fam
        want = "v6" if FORCE_V6 else "v4"
        print("%s %-44s 起始出口=%s" % (tagl, tag, ip0), flush=True)
        if not ip0 or fam != want:
            R["err"] = "no_ip" if not ip0 else "family_mismatch:%s" % fam
            return R

        if mode == "single":
            base_sid = P._session_id()
            sids = [base_sid] * conc
        else:
            sids = [P._session_id() for _ in range(conc)]
        R["sessions"] = len(set(sids))

        def worker(k):
            sid = sids[k]
            my = 0
            while not STOP.is_set():
                with LOCK:
                    if TOTAL["n"] >= maxtotal:
                        STOP.set()
                        return
                if mode == "rotate":
                    sid = P._session_id()
                v, st, ra = call(port, sid)
                tries = 0
                while v == "trans" and tries < 4 and not STOP.is_set():
                    tries += 1
                    time.sleep(1.5)
                    v, st, ra = call(port, sid)
                if v == "trans":
                    # 链路抖动不算上限，记一笔继续
                    with LOCK:
                        TRANS["n"] += 1
                    if tries >= 4:
                        time.sleep(2.0)
                    continue
                with LOCK:
                    if v == "OK":
                        TOTAL["n"] += 1
                        my += 1
                        PER[k] = my
                        if TOTAL["n"] % 50 == 0:
                            print("%s   累计 %d 发全 OK (%d 路并发)"
                                  % (tagl, TOTAL["n"], conc), flush=True)
                    else:
                        if not STOP.is_set():
                            STOP.set()
                            DEATH.append({"worker": k, "verdict": v, "st": st,
                                          "ra": ra, "total_ok": TOTAL["n"],
                                          "my_ok": my, "sid": sid,
                                          "trans_retried": tries})
                            print("%s   !! 第 %d 发 %s st=%s ra=%s (此前共 OK %d 发)"
                                  % (tagl, TOTAL["n"] + 1, v, st, ra, TOTAL["n"]),
                                  flush=True)
                        return

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=conc) as ex:
            list(ex.map(worker, range(conc)))
        dur = time.time() - t0

        R["ok"] = TOTAL["n"]
        R["trans_cnt"] = TRANS["n"]
        R["death"] = DEATH
        R["dur_sec"] = round(dur, 1)
        R["per_worker"] = dict(PER)
        ipn = ip_of(port)
        R["ip_end"] = ipn
        R["ip_stable"] = (ipn == ip0)
        if not R["ip_stable"]:
            R["err"] = "ip_changed"
        conf = []
        for _ in range(2):
            s2 = P._session_id()
            v2, st2, ra2 = call(port, s2)
            conf.append({"verdict": v2, "st": st2, "ra": ra2})
        R["confirm"] = conf
        print("%s === 完成 OK=%d 发, 用时 %ss, 出口 %s -> %s, 复核 %s ==="
              % (tagl, R["ok"], R["dur_sec"], ip0, ipn,
                 [c["verdict"] for c in conf]), flush=True)
        return R
    finally:
        pr.terminate()
        try:
            pr.wait(timeout=6)
        except Exception:
            pass


def main():
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    jobs = []
    i = 0
    while i < len(a):
        jobs.append((int(a[i]), a[i + 1], a[i + 2], int(a[i + 3]), int(a[i + 4])))
        i += 5
    out = []
    for j in jobs:
        try:
            out.append(run(*j))
        except Exception as e:
            out.append({"idx": j[0], "tag": j[1], "mode": j[2], "err": "exc:%s" % e})
        finally:
            STOP.clear()
            with LOCK:
                TOTAL["n"] = 0
                TRANS["n"] = 0
                DEATH.clear()
                PER.clear()
        json.dump(out, open("v7_result_%d.json" % jobs[0][0], "w"),
                  ensure_ascii=False, indent=1)
        print("", flush=True)
    print("===== SUMMARY =====")
    for r in out:
        if r.get("err"):
            print("%-32s %-7s ERR=%s ok=%s" % (r["tag"][:32], r["mode"],
                                               r["err"], r.get("ok")))
        else:
            d = (r.get("death") or [{}])[0]
            print("%-32s %-7s conc=%d 出口=%s OK=%d 发 死点=%s ra=%s"
                  % (r["tag"][:32], r["mode"], r["conc"], r["ip_start"], r["ok"],
                     d.get("verdict"), d.get("ra")))
    print("DONE")


if __name__ == "__main__":
    main()
