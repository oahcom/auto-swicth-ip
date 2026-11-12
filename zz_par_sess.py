# -*- coding: utf-8 -*-
"""并行：每个出口独立实例，固定 session 打到 429，每发校验出口 IP。

判定：
  - 打到 429 后，同 IP 换全新 session
      OK  -> 额度按 (session,IP) 记 -> 该发数是 session 上限
      429 -> 额度按 IP 记 -> 该发数是 IP 总额度
  - 出口 IP 中途变化 -> 该组数据作废（上次 114 发就是这么废的）

用法: python3 zz_par_sess.py <tag1> [tag2] [tag3]...
"""
import json, os, subprocess, sys, tempfile, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
import probe_warp as P

BASE = "singbox-config.json-slot3.json"
BODY = json.dumps(P._body())
LIMIT = int(os.environ.get("LIMIT", "220"))
OUT = "par_sess_result.json"

TAGS = sys.argv[1:]
LOCK = threading.Lock()


def build(idx, tag):
    port = 7040 + idx
    clash = 9140 + idx
    cfg = "/tmp/par%d.json" % idx
    d = json.load(open(BASE))
    d["inbounds"] = [{"type": "mixed", "tag": "in", "listen": "127.0.0.1", "listen_port": port}]
    d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") not in ("cap-out", "cap-tri")]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": [tag], "default": tag})
    d["route"]["rules"] = []
    d["route"]["final"] = "cap-out"
    d["experimental"] = {"clash_api": {"external_controller": "127.0.0.1:%d" % clash,
                                       "secret": "par%d" % idx},
                         "cache_file": {"enabled": False}}
    d["log"] = {"level": "error"}
    json.dump(d, open(cfg, "w"))
    return port, clash, cfg


def ip_of(port, t=5):
    for u in ("https://icanhazip.com", "https://api.ipify.org"):
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
    payload = {"url": P.URL, "method": "POST", "headers": P._headers(sid), "body": BODY,
               "proxy": "http://127.0.0.1:%d" % port, "timeoutMs": timeout_ms}
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


def work(idx, tag):
    R = {"idx": idx, "tag": tag, "spent": 0}
    port, clash, cfg = build(idx, tag)
    log = open("/tmp/par%d.log" % idx, "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                          stdout=log, stderr=subprocess.STDOUT)
    time.sleep(3.0 + idx * 0.5)
    try:
        ip0 = ip_of(port)
        R["ip_start"] = ip0
        if not ip0:
            R["error"] = "no_ip"
            return R

        # ---- 固定 session 打到 429，每发校验出口 ----
        S = P._session_id()
        R["sid"] = S
        n = 0
        stop = None
        ra = None
        trans = 0
        changes = []
        while n < LIMIT:
            v, st, ra = call(port, S)
            R["spent"] += 1
            if v == "trans":
                trans += 1
                if trans >= 3:
                    stop = "trans"
                    break
                continue
            trans = 0
            if v == "OK":
                n += 1
                cur = ip_of(port)
                if cur and cur != ip0:
                    changes.append({"at_n": n, "new": cur})
                    stop = "ip_changed"
                    break
            else:
                stop = v
                break
        R["ok_in_session"] = n
        R["stop"] = stop
        R["ra"] = ra
        R["ip_end"] = ip_of(port)
        R["ip_changes"] = changes

        # ---- 同 IP 换全新 session ----
        fresh = []
        if stop != "ip_changed":
            for _ in range(2):
                s2 = P._session_id()
                v2, st2, ra2 = call(port, s2)
                R["spent"] += 1
                fresh.append({"verdict": v2, "ra": ra2})
                if v2 != "OK":
                    break
                time.sleep(0.4)
        R["fresh_sessions"] = fresh

        if stop == "ip_changed":
            R["verdict"] = "作废：出口 IP 中途变化 %s" % changes
        elif fresh and all(f["verdict"] == "OK" for f in fresh):
            R["verdict"] = "(session,IP) 级：%d 发后换新 session 立刻恢复" % n
        elif fresh:
            R["verdict"] = "IP 级：%d 发后换新 session 仍 %s" % (n, fresh[0]["verdict"])
        else:
            R["verdict"] = "无数据"
        return R
    finally:
        pr.terminate()
        try:
            pr.wait(timeout=6)
        except Exception:
            pr.kill()
        log.close()


def main():
    print("并行出口数：%d，每出口上限 %d 发" % (len(TAGS), LIMIT), flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=len(TAGS)) as ex:
        futs = [ex.submit(work, i, t) for i, t in enumerate(TAGS)]
        res = []
        for f in futs:
            try:
                res.append(f.result())
            except Exception as e:
                res.append({"error": repr(e)[:120]})
    print("\n耗时 %.0fs\n" % (time.time() - t0), flush=True)
    print("%-42s %-16s %6s %-10s %-8s %s" % ("节点", "起始出口", "发数", "停止", "消耗", "判定"))
    for r in res:
        if r.get("error"):
            print("  ERR %s" % r)
            continue
        print("%-42s %-16s %6d %-10s %-8d %s" % (
            r["tag"][:42], str(r.get("ip_start"))[:16], r.get("ok_in_session") or 0,
            str(r.get("stop")), r.get("spent") or 0, r.get("verdict")))
        if r.get("ip_changes"):
            print("     出口变化: %s" % r["ip_changes"])
        if r.get("fresh_sessions"):
            print("     新session: %s" % [x["verdict"] for x in r["fresh_sessions"]])
    json.dump(res, open(OUT, "w"), indent=1, ensure_ascii=False)
    print("\n结果存 %s" % OUT)


if __name__ == "__main__":
    main()
