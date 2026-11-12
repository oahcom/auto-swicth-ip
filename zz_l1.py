# -*- coding: utf-8 -*-
"""zz_l1.py — 判决：L1（40 个不同 session）满了之后，老 session 还能不能打？

流程：
  1. 用 session A 打 1 发（A 成为该 IP 的"老 session"）
  2. 用全新 session 每发换一次，一直打到 429
  3. 再用 session A 打 1 发
判决：
  - A 仍 200  -> L1 只管新 session 准入，老 session 不受影响（额度才是硬闸）
  - A 也 429  -> L1 满了即全拒（老 session 一起死）

用法: python3 zz_l1.py <idx> <tag> [--v4] [最多新 session 数]
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import probe_warp as P

BASE = "singbox-config.json-slot3.json"
BODY = json.dumps(P._body())


def build(idx, tag, port, family):
    cfg = "/tmp/l1_%d.json" % idx
    d = json.load(open(BASE))
    d["inbounds"] = [{"type": "mixed", "tag": "in",
                      "listen": "127.0.0.1", "listen_port": port}]
    d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") != "cap-out"]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": [tag], "default": tag})
    d["route"]["final"] = "cap-out"
    pre = []
    if family == "v6":
        pre.append({"action": "resolve",
                    "domain_suffix": ["opencode.ai", ".opencode.ai"],
                    "server": "dns-opencode", "strategy": "ipv6_only"})
    d["route"]["rules"] = pre + [
        {"action": "sniff"},
        {"domain_suffix": ["opencode.ai"], "outbound": "cap-out"},
    ]
    d["experimental"] = {"cache_file": {"enabled": False}}
    d["log"] = {"level": "error"}
    json.dump(d, open(cfg, "w"))
    return cfg


def curl(port, url, t=8):
    try:
        r = subprocess.run(["curl", "-s", "--max-time", str(t),
                            "--proxy", "http://127.0.0.1:%d" % port, url],
                           capture_output=True, text=True)
        v = (r.stdout or "").strip().splitlines()
        return v[0].strip() if v and v[0].strip() else None
    except Exception:
        return None


def exit_ip(port, family):
    urls = ("https://icanhazip.com", "https://api6.ipify.org") if family == "v6" \
        else ("https://icanhazip.com", "https://api.ipify.org")
    for u in urls:
        for _ in range(2):
            v = curl(port, u)
            if v:
                return v
    return None


def call(port, sid, timeout_ms=40000):
    from asip.proc import run as run_proc
    payload = {"url": P.URL, "method": "POST", "headers": P._headers(sid),
               "body": BODY, "proxy": "http://127.0.0.1:%d" % port,
               "timeoutMs": timeout_ms}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    try:
        r = run_proc([P._bun_bin(), P.BUN_SCRIPT, path], timeout=timeout_ms / 1000.0 + 10)
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


def shot(port, sid, label):
    v, st, ra = call(port, sid)
    t = 0
    while v == "trans" and t < 3:
        t += 1
        time.sleep(1.5)
        v, st, ra = call(port, sid)
    print("   %-28s sid=%-26s -> %-6s st=%-4s ra=%s"
          % (label, sid[:26], v, st, ra), flush=True)
    return {"label": label, "sid": sid, "verdict": v, "st": st, "ra": ra}


def main():
    a = sys.argv[1:]
    family = "v4" if "--v4" in a else "v6"
    a = [x for x in a if x not in ("--v4", "--v6")]
    idx = int(a[0])
    tag = a[1]
    maxnew = int(a[2]) if len(a) > 2 else 50
    port = 7800 + idx
    cfg = build(idx, tag, port, family)
    lg = open("/tmp/l1_%d.log" % idx, "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                          stdout=lg, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    R = {"tag": tag, "family": family, "steps": []}
    try:
        ip0 = exit_ip(port, family)
        R["ip"] = ip0
        print("[L1判决] %s 出口=%s" % (tag, ip0), flush=True)
        if not ip0:
            R["err"] = "no_ip"
            return
        A = P._session_id()
        R["session_A"] = A
        R["steps"].append(shot(port, A, "step1: 老session A 首打"))

        killed_at = None
        used = 0
        for n in range(1, maxnew + 1):
            s = P._session_id()
            v, st, ra = call(port, s)
            t = 0
            while v == "trans" and t < 3:
                t += 1
                time.sleep(1.5)
                v, st, ra = call(port, s)
            used += 1
            if v != "OK":
                killed_at = {"n": n, "verdict": v, "st": st, "ra": ra, "sid": s}
                print("   step2: 第 %d 个新 session -> %s st=%s ra=%s"
                      % (n, v, st, ra), flush=True)
                break
            if n % 5 == 0:
                print("   step2: 已用 %d 个新 session，全 OK" % n, flush=True)
        R["killed_at"] = killed_at
        R["new_sessions_used"] = used

        R["steps"].append(shot(port, A, "step3: 老session A 复打"))
        R["steps"].append(shot(port, P._session_id(), "step4: 全新 session 复打"))
        R["ip_end"] = exit_ip(port, family)
        R["ip_stable"] = (R["ip_end"] == ip0)

        print("\n=== 判决 ===", flush=True)
        k = killed_at or {}
        print("  新 session 撞墙于第 %s 个（ra=%s）" % (k.get("n"), k.get("ra")), flush=True)
        a3 = R["steps"][1]["verdict"] if len(R["steps"]) > 1 else "?"
        print("  老 session A 复打 -> %s" % a3, flush=True)
        if a3 == "OK":
            print("  >>> L1 只限制「新 session 准入」；老 session 仍可通", flush=True)
        else:
            print("  >>> L1 满即全拒，老 session 一起 429", flush=True)
    finally:
        pr.terminate()
        try:
            pr.wait(timeout=6)
        except Exception:
            pass
        json.dump(R, open("l1_result_%d.json" % idx, "w"),
                  ensure_ascii=False, indent=1)
        print("DONE")


if __name__ == "__main__":
    main()
