# -*- coding: utf-8 -*-
"""20 个固定 session 常驻并发实验。

用法:
  python3 zz_conc20.py probe <tag1> <tag2> ...      # 每个节点 1 发，找干净出口
  python3 zz_conc20.py run <tag> <workers> <secs>   # workers 个固定 session 一直并发

协议:
  - 每个 worker 启动时领一个 session，全程**不换**（这就是被质疑的那个点）
  - worker 循环发请求直到自己拿到 429/403（或超时）
  - 全程校验出口 IP，变化即作废
  - 记录：总成功数、每个 session 的成功数、第一个 429 出现时的累计成功数
"""
import json, os, subprocess, sys, tempfile, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
import probe_warp as P

BASE = "singbox-config.json-slot3.json"
CFG = "/tmp/cap-conc20.json"
PORT = 7020
CLASH = 9120
SECRET = "conc20"
BODY = json.dumps(P._body())

MODE = sys.argv[1] if len(sys.argv) > 1 else "run"
ARGS = sys.argv[2:]

LOCK = threading.Lock()
RES = {"ok": 0, "fail": 0, "first429_at": None, "workers": [], "events": []}


def build(tags):
    d = json.load(open(BASE))
    have = {o.get("tag") for o in d["outbounds"]}
    tags = [t for t in tags if t in have] or list(have)[:1]
    d["inbounds"] = [{"type": "mixed", "tag": "cap-in",
                      "listen": "127.0.0.1", "listen_port": PORT}]
    d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") not in ("cap-out", "cap-tri")]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": tags, "default": tags[0]})
    d["route"]["rules"] = []
    d["route"]["final"] = "cap-out"
    d["experimental"] = {"clash_api": {"external_controller": "127.0.0.1:%d" % CLASH,
                                       "secret": SECRET},
                         "cache_file": {"enabled": False}}
    d["log"] = {"level": "error", "timestamp": True}
    json.dump(d, open(CFG, "w"))
    return tags


def select(tag):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/proxies/cap-out" % CLASH,
        data=json.dumps({"name": tag}).encode(), method="PUT",
        headers={"Authorization": "Bearer " + SECRET,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode()[:40]
    except Exception as e:
        return "ERR" + str(e)[:30]


def ip_of(t=6):
    for u in ("https://icanhazip.com", "https://api.ipify.org", "https://api6.ipify.org"):
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


def call(headers, body, timeout_ms=35000):
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
    st = d.get("status"); head = str(d.get("head") or "")
    v = P.classify(st, head[:160])
    ra = P.parse_retry_after(d.get("retry") or {}, head) if v != "OK" else None
    return v, st, ra


def worker(idx, sid, deadline, out):
    """固定 session，一直打到失败。"""
    ok = 0
    fails = 0
    last = None
    while time.time() < deadline and not out["stop"]:
        v, st, ra = call(P._headers(sid), BODY)
        if v == "OK":
            ok += 1
            with LOCK:
                RES["ok"] += 1
                if RES["first429_at"] is None:
                    pass
        elif v == "trans":
            fails += 1
            if fails >= 3:
                last = "trans"
                break
            time.sleep(1)
            continue
        else:
            with LOCK:
                RES["fail"] += 1
                if RES["first429_at"] is None:
                    RES["first429_at"] = RES["ok"]
                RES["events"].append({"w": idx, "ok_at_fail": ok, "verdict": v,
                                      "st": st, "ra": ra,
                                      "t": round(time.time() - out["t0"], 1)})
            last = v
            break
    with LOCK:
        RES["workers"].append({"w": idx, "sid": sid[:16], "ok": ok, "stop": last or "deadline"})


def main():
    if MODE == "probe":
        tags = ARGS
        build(tags)
        log = open("/tmp/conc20.log", "w")
        proc = subprocess.Popen(["./sing-box", "run", "-c", CFG],
                                stdout=log, stderr=subprocess.STDOUT)
        try:
            time.sleep(3)
            for t in tags:
                select(t); time.sleep(0.6)
                ip = ip_of()
                v, st, ra = call(P._headers(P._session_id()), BODY)
                print("PROBE %-42s ip=%-42s -> %-6s st=%s ra=%s" % (t, ip, v, st, ra),
                      flush=True)
        finally:
            proc.terminate()
            try: proc.wait(timeout=5)
            except Exception: proc.kill()
            log.close()
        return

    tag = ARGS[0]
    workers = int(ARGS[1]) if len(ARGS) > 1 else 20
    secs = int(ARGS[2]) if len(ARGS) > 2 else 180
    build([tag])
    log = open("/tmp/conc20.log", "w")
    proc = subprocess.Popen(["./sing-box", "run", "-c", CFG],
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        time.sleep(3)
        select(tag); time.sleep(0.8)
        IP0 = ip_of()
        print("NODE=%s IP=%s workers=%d secs=%d t=%s" % (
            tag, IP0, workers, secs, time.strftime("%H:%M:%S")), flush=True)
        if not IP0:
            print("NO_IP"); return
        out = {"stop": False, "t0": time.time()}
        deadline = out["t0"] + secs
        sids = [P._session_id() for _ in range(workers)]
        assert len(set(sids)) == workers
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(worker, i, s, deadline, out) for i, s in enumerate(sids)]
            last = 0
            while any(not f.done() for f in futs):
                time.sleep(5)
                if RES["ok"] != last:
                    print("  .. 累计成功=%d 失败=%d ip=%s" % (RES["ok"], RES["fail"], ip_of()),
                          flush=True)
                    last = RES["ok"]
                if time.time() > deadline:
                    out["stop"] = True
            for f in futs:
                try: f.result()
                except Exception: pass
        IP1 = ip_of()
        print("\n=== 结果 ===", flush=True)
        print("总成功=%d 失败=%d IP %s -> %s same=%s" % (
            RES["ok"], RES["fail"], IP0, IP1, IP0 == IP1), flush=True)
        print("第一个 429 出现时的累计成功=%s" % RES["first429_at"], flush=True)
        ws = sorted(RES["workers"], key=lambda x: -x["ok"])
        print("每个固定 session 的成功数: %s" % [w["ok"] for w in ws], flush=True)
        print("首个失败事件: %s" % (RES["events"][0] if RES["events"] else None), flush=True)
        RES["node"] = tag; RES["ip"] = IP0; RES["workers_n"] = workers; RES["secs"] = secs
        json.dump(RES, open("conc20_%s.json" % tag.replace(":", "_").replace("/", "_"), "w"),
                  indent=2, ensure_ascii=False)
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
        log.close()


main()
