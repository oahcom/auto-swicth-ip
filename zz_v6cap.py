# -*- coding: utf-8 -*-
"""v6 出口容量测试。

阶段 1 scan : 扫所有候选节点的出口地址族（只查 IP，不消耗 opencode 配额）
阶段 2 single: 在指定节点上，1 个固定 session 连打，直到 429 -> 单会话上限
阶段 3 multi : 在指定节点上，每发换全新 session，直到 429 -> 多会话上限（看是否卡在 40 个）

每发都校验出口 IP，变化即作废（v4/v6 是两个独立额度）。

用法:
  python3 zz_v6cap.py scan
  python3 zz_v6cap.py cap <idx> <tag> <mode:single|multi> <maxn>
"""
import json, os, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
import probe_warp as P

BASE = "singbox-config.json-slot3.json"
BODY = json.dumps(P._body())
LOCK = threading.Lock()

ALL_TAGS = ["ttll20-ooi.dankenet.help:12815-any", "ttll20-ooi.dankenet.help:11732-any",
            "ddixx-1dd1.dankenet.help:8656-any", "ttll20-ooi.dankenet.help:34765-any",
            "ttll20-ooi.dankenet.help:25558-any", "ttll20-ooi.dankenet.help:64657-any",
            "ddixx-1dd1.dankenet.help:35849-any", "ddixx-1dd1.dankenet.help:31966-any",
            "ddixx-1dd1.dankenet.help:23886-any", "ttll20-ooi.dankenet.help:16996-any",
            "ttll20-ooi.dankenet.help:48483-any", "ttll20-ooi.dankenet.help:26514-vl",
            "ttll20-ooi.dankenet.help:30207-vl", "ddixx-1dd1.dankenet.help:21174-any",
            "ddixx-1dd1.dankenet.help:42839-any", "ttll20-ooi.dankenet.help:29874-any",
            "ttll20-ooi.dankenet.help:5561-any", "ddixx-1dd1.dankenet.help:11957-any",
            "ddixx-1dd1.dankenet.help:44073-any", "ttll20-ooi.dankenet.help:42068-any",
            "ddixx-1dd1.dankenet.help:5474-any", "ttll20-ooi.dankenet.help:63153-any",
            "ddixx-1dd1.dankenet.help:41723-any", "ttll20-ooi.dankenet.help:6650-any",
            "ddixx-1dd1.dankenet.help:3526-any", "ttll20-ooi.dankenet.help:32992-any",
            "ttll20-ooi.dankenet.help:61827-vl", "ddixx-1dd1.dankenet.help:60130-any",
            "ddixx-1dd1.dankenet.help:16895-vl", "ttll20-ooi.dankenet.help:20472-any",
            "ddixx-1dd1.dankenet.help:37735-any", "ttll20-ooi.dankenet.help:44503-any",
            "ddixx-1dd1.dankenet.help:51747-any", "ttll20-ooi.dankenet.help:21094-any",
            "ddixx-1dd1.dankenet.help:63099-any", "ttll20-ooi.dankenet.help:33068-any",
            "ddixx-1dd1.dankenet.help:3824-any", "ttll20-ooi.dankenet.help:53175-vl",
            "ttll20-ooi.dankenet.help:40302-vl", "ttll20-ooi.dankenet.help:59753-vl"]


def build(idx, tags):
    port = 7060 + idx
    clash = 9160 + idx
    cfg = "/tmp/v6c_%d.json" % idx
    d = json.load(open(BASE))
    have = {o.get("tag") for o in d["outbounds"]}
    tags = [t for t in tags if t in have]
    d["inbounds"] = [{"type": "mixed", "tag": "in",
                      "listen": "127.0.0.1", "listen_port": port}]
    d["outbounds"] = [o for o in d["outbounds"]
                      if o.get("tag") not in ("cap-out", "cap-tri")]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": tags, "default": tags[0]})
    d["route"]["rules"] = []
    d["route"]["final"] = "cap-out"
    d["experimental"] = {"clash_api": {"external_controller": "127.0.0.1:%d" % clash,
                                       "secret": "v6c%d" % idx},
                         "cache_file": {"enabled": False}}
    d["log"] = {"level": "error"}
    json.dump(d, open(cfg, "w"))
    return port, clash, cfg


def select(clash, idx, tag):
    import urllib.request
    req = urllib.request.Request(
        "http://127.0.0.1:%d/proxies/cap-out" % clash,
        data=json.dumps({"name": tag}).encode(), method="PUT",
        headers={"Authorization": "Bearer v6c%d" % idx,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return True
    except Exception:
        return False


def ip_of(port, t=6):
    """返回 (icanhazip 结果, api6 结果)。前者可能 v4 或 v6，后者只 v6。"""
    out = []
    for u in ("https://icanhazip.com", "https://api6.ipify.org"):
        val = None
        for _ in range(2):
            try:
                r = subprocess.run(["curl", "-s", "--max-time", str(t),
                                    "--proxy", "http://127.0.0.1:%d" % port, u],
                                   capture_output=True, text=True)
                v = (r.stdout or "").strip().splitlines()
                if v and v[0].strip():
                    val = v[0].strip()
                    break
            except Exception:
                pass
        out.append(val)
    return out[0], out[1]


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


def scan_worker(idx, tags):
    port, clash, cfg = build(idx, tags)
    lg = open("/tmp/v6scan_%d.log" % idx, "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                          stdout=lg, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    res = []
    try:
        for tag in tags:
            if not select(clash, idx, tag):
                continue
            time.sleep(0.9)
            a, b = ip_of(port, t=5)
            fam = "v6" if (a and ":" in a) else ("v4" if a else "none")
            res.append({"tag": tag, "any": a, "api6": b, "fam": fam})
            print("[S%d] %-44s fam=%-4s any=%-40s api6=%s"
                  % (idx, tag, fam, a, b), flush=True)
            with LOCK:
                json.dump(res, open("/tmp/v6scan_part%d.json" % idx, "w"),
                          ensure_ascii=False, indent=1)
    finally:
        pr.terminate()
        try:
            pr.wait(timeout=6)
        except Exception:
            pass
    return res


def cap_run(idx, tag, mode, maxn):
    port, clash, cfg = build(idx, [tag])
    lg = open("/tmp/v6cap_%d.log" % idx, "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                          stdout=lg, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    R = {"idx": idx, "tag": tag, "mode": mode, "maxn": maxn}
    try:
        ip0, ip6_0 = ip_of(port)
        R["ip_start"] = ip0
        R["api6_start"] = ip6_0
        print("[C%d] %s mode=%s 起始出口=%s (api6=%s)" % (idx, tag, mode, ip0, ip6_0),
              flush=True)
        if not ip0:
            R["err"] = "no_ip"
            return R
        sids = set()
        ok = 0
        stop = None
        fixed = P._session_id()
        for i in range(maxn):
            if mode == "single":
                sid = fixed
            else:
                sid = P._session_id()
                while sid in sids:
                    sid = P._session_id()
            sids.add(sid)
            v, st, ra = call(port, sid)
            if v != "OK":
                stop = {"at_shot": i + 1, "verdict": v, "st": st, "ra": ra,
                        "distinct_sessions": len(sids)}
                print("[C%d] 第 %d 发 %s st=%s ra=%s (累计OK=%d, 已用session=%d)"
                      % (idx, i + 1, v, st, ra, ok, len(sids)), flush=True)
                break
            ok += 1
            if (i + 1) % 10 == 0:
                ipn, ip6n = ip_of(port)
                same = (ipn == ip0)
                print("[C%d]   %2d 发全 OK 出口=%s%s  已用session=%d"
                      % (idx, i + 1, ipn, "" if same else "  <<< IP变了", len(sids)),
                      flush=True)
                if not same:
                    R["err"] = "ip_changed"
                    R["ip_changed_to"] = ipn
                    R["ok_before_change"] = ok
                    return R
        R["ok"] = ok
        R["stop"] = stop
        R["distinct_sessions"] = len(sids)
        # 复核
        conf = []
        for k in range(2):
            s2 = P._session_id()
            while s2 in sids:
                s2 = P._session_id()
            sids.add(s2)
            v2, st2, ra2 = call(port, s2)
            conf.append({"verdict": v2, "st": st2, "ra": ra2})
            print("[C%d]   复核全新 session -> %s ra=%s" % (idx, v2, ra2), flush=True)
        R["confirm"] = conf
        ipn, ip6n = ip_of(port)
        R["ip_end"] = ipn
        print("[C%d] === %s 结束: OK=%d 发, 用时%d 个 session, 出口 %s ==="
              % (idx, mode, ok, len(sids), ipn), flush=True)
        return R
    finally:
        pr.terminate()
        try:
            pr.wait(timeout=6)
        except Exception:
            pass


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "scan":
        half = (len(ALL_TAGS) + 1) // 2
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(scan_worker, 0, ALL_TAGS[:half])
            f2 = ex.submit(scan_worker, 1, ALL_TAGS[half:])
            r = f1.result() + f2.result()
        json.dump(r, open("v6_scan.json", "w"), ensure_ascii=False, indent=1)
        v6 = [x for x in r if x["fam"] == "v6" or x.get("api6")]
        print("\n===== 有 v6 迹象的节点 %d 个 =====" % len(v6))
        for x in v6:
            print("  %-44s any=%-40s api6=%s" % (x["tag"], x["any"], x.get("api6")))
        print("DONE")
    elif cmd == "cap":
        idx = int(sys.argv[2])
        tag = sys.argv[3]
        mode = sys.argv[4]
        maxn = int(sys.argv[5])
        R = cap_run(idx, tag, mode, maxn)
        json.dump(R, open("/tmp/v6cap_%d_%s.json" % (idx, mode), "w"),
                  ensure_ascii=False, indent=1)
        print(json.dumps(R, ensure_ascii=False, indent=1))
    else:
        raise SystemExit("unknown cmd")


if __name__ == "__main__":
    main()
