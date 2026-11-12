# -*- coding: utf-8 -*-
"""v8: 带出口 IP 分段的额度测试（专治 WARP 中途漂 IP）。

WARP 出口会自己重连换 IP（连 /32 都变），普通测法会把多个 IP 的额度加在一起。
这里每 3 秒采样一次出口 IP，变化就把"上一段 IP 打了多少发"结算出来，
最终输出每个 IP 各自扛了多少发。

用法: python3 zz_v8.py <idx> <tag> <mode> <conc> <maxtotal>
      python3 zz_v8.py <idx> <tag> single 4 600 --v4
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
MARKS = []   # [(累计ok, ip)]


def build(idx, tag, port):
    cfg = "/tmp/v8b_%d.json" % idx
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


def ip_of(port, t=12):
    u = "https://icanhazip.com" if FORCE_V6 else "https://icanhazip.com"
    try:
        r = subprocess.run(["curl", "-s", "--max-time", str(t),
                            "--proxy", "http://127.0.0.1:%d" % port, u],
                           capture_output=True, text=True)
        v = (r.stdout or "").strip().splitlines()
        return v[0].strip() if v and v[0].strip() else None
    except Exception:
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
    port = 7400 + idx
    cfg = build(idx, tag, port)
    lg = open("/tmp/v8b_%d.log" % idx, "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                          stdout=lg, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    R = {"idx": idx, "tag": tag, "mode": mode, "conc": conc, "maxtotal": maxtotal}
    tagl = "[%s]" % mode
    mon_stop = threading.Event()

    def monitor():
        last = None
        while not mon_stop.is_set():
            ip = ip_of(port)
            with LOCK:
                if ip and ip != last:
                    MARKS.append((TOTAL["n"], ip))
                    last = ip
                    print("%s   ~ 出口IP = %s (此点累计 %d 发)"
                          % (tagl, ip, TOTAL["n"]), flush=True)
            mon_stop.wait(3.0)

    try:
        ip0 = ip_of(port)
        R["ip_start"] = ip0
        fam = "v6" if (ip0 and ":" in ip0) else ("v4" if ip0 else "none")
        want = "v6" if FORCE_V6 else "v4"
        print("%s %-40s 起始出口=%s" % (tagl, tag, ip0), flush=True)
        if not ip0 or fam != want:
            R["err"] = "no_ip" if not ip0 else "family_mismatch:%s" % fam
            return R
        with LOCK:
            MARKS.append((0, ip0))

        if mode == "single":
            sids = [P._session_id()] * conc
        else:
            sids = [P._session_id() for _ in range(conc)]
        R["sessions"] = len(set(sids))
        mt = threading.Thread(target=monitor, daemon=True)
        mt.start()

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
                    else:
                        if not STOP.is_set():
                            STOP.set()
                            DEATH.append({"worker": k, "verdict": v, "st": st,
                                          "ra": ra, "total_ok": TOTAL["n"],
                                          "my_ok": my, "trans_retried": tries})
                            print("%s   !! 第 %d 发 %s ra=%s (此前 OK %d 发)"
                                  % (tagl, TOTAL["n"] + 1, v, ra, TOTAL["n"]),
                                  flush=True)
                        return

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=conc) as ex:
            list(ex.map(worker, range(conc)))
        dur = time.time() - t0
        mon_stop.set()
        mt.join(timeout=8)

        R["ok"] = TOTAL["n"]
        R["trans_cnt"] = TRANS["n"]
        R["death"] = DEATH
        R["dur_sec"] = round(dur, 1)
        R["per_worker"] = dict(PER)
        R["ip_end"] = ip_of(port)

        # 按 IP 分段结算
        with LOCK:
            ms = list(MARKS)
        segs = []
        for i, (n, ip) in enumerate(ms):
            end = ms[i + 1][0] if i + 1 < len(ms) else TOTAL["n"]
            segs.append({"ip": ip, "shots": end - n})
        R["segments"] = segs
        print("%s   分段: %s" % (tagl, [(s["ip"][:24], s["shots"]) for s in segs]),
              flush=True)

        conf = []
        for _ in range(2):
            s2 = P._session_id()
            v2, st2, ra2 = call(port, s2)
            conf.append({"verdict": v2, "st": st2, "ra": ra2})
        R["confirm"] = conf
        print("%s === 完成 OK=%d (trans=%d) 用时%ss, 复核=%s ==="
              % (tagl, R["ok"], R["trans_cnt"], R["dur_sec"],
                 [c["verdict"] for c in conf]), flush=True)
        return R
    finally:
        mon_stop.set()
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
                MARKS.clear()
        json.dump(out, open("v8_result_%d.json" % jobs[0][0], "w"),
                  ensure_ascii=False, indent=1)
        print("", flush=True)
    print("===== SUMMARY =====")
    for r in out:
        if r.get("err"):
            print("%-30s %-7s ERR=%s" % (r["tag"][:30], r["mode"], r["err"]))
        else:
            print("%-30s %-7s OK=%d 分段=%s"
                  % (r["tag"][:30], r["mode"], r["ok"],
                     [(s["ip"][:20], s["shots"]) for s in r.get("segments", [])]))
    print("DONE")


if __name__ == "__main__":
    main()
