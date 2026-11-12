# -*- coding: utf-8 -*-
"""判决实验：同一 IP 上「40 个不同 session」是否必定 429（与发数解耦）。

命题（用户）：同一 IP 上累计用 40 个不同 session 就必定 429。
之前所有实验里「换 session」和「换发」完全共变，无法区分两者。

设计（同一 IP 上两阶段）：
  阶段 A：1 个固定 session 连打 PHASE_A 发       -> 确认该 IP 总额度 > PHASE_A
  阶段 B：之后每发都换全新 session，继续打        -> 看第几个新 session 死

判定：
  - B 在第 40 个新 session 附近（总 ~40+PHASE_A 发）429
        => session 数是独立计数维度，命题成立
  - B 一路冲到 MAXTOTAL 发以上才 429
        => 只按「发数」计数，与 session 数量无关，命题不成立

每 10 发校验出口 IP，变化立即作废（v4/v6 是两个独立额度）。

用法: python3 zz_sess40.py
"""
import json, os, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
import probe_warp as P

BASE = "singbox-config.json-slot3.json"
BODY = json.dumps(P._body())
PHASE_A = int(os.environ.get("PHASE_A", "50"))
MAXTOTAL = int(os.environ.get("MAXTOTAL", "130"))
OUT = "sess40_result.json"
LOCK = threading.Lock()

POOL_T = ["ttll20-ooi.dankenet.help:34765-any", "ttll20-ooi.dankenet.help:25558-any",
          "ttll20-ooi.dankenet.help:64657-any", "ttll20-ooi.dankenet.help:16996-any",
          "ttll20-ooi.dankenet.help:48483-any", "ttll20-ooi.dankenet.help:29874-any",
          "ttll20-ooi.dankenet.help:5561-any", "ttll20-ooi.dankenet.help:42068-any",
          "ttll20-ooi.dankenet.help:63153-any", "ttll20-ooi.dankenet.help:6650-any",
          "ttll20-ooi.dankenet.help:32992-any"]
POOL_D = ["ddixx-1dd1.dankenet.help:23886-any", "ddixx-1dd1.dankenet.help:21174-any",
          "ddixx-1dd1.dankenet.help:42839-any", "ddixx-1dd1.dankenet.help:11957-any",
          "ddixx-1dd1.dankenet.help:44073-any", "ddixx-1dd1.dankenet.help:5474-any",
          "ddixx-1dd1.dankenet.help:41723-any", "ddixx-1dd1.dankenet.help:3526-any",
          "ddixx-1dd1.dankenet.help:60130-any"]


def build(idx, tag):
    port = 7050 + idx
    clash = 9150 + idx
    cfg = "/tmp/s40_%d.json" % idx
    d = json.load(open(BASE))
    d["inbounds"] = [{"type": "mixed", "tag": "in",
                      "listen": "127.0.0.1", "listen_port": port}]
    d["outbounds"] = [o for o in d["outbounds"]
                      if o.get("tag") not in ("cap-out", "cap-tri")]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": [tag], "default": tag})
    d["route"]["rules"] = []
    d["route"]["final"] = "cap-out"
    d["experimental"] = {"clash_api": {"external_controller": "127.0.0.1:%d" % clash,
                                       "secret": "s40%d" % idx},
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


def run_one(idx, tag):
    """在一个节点上跑完整两阶段。返回结果 dict。"""
    port, clash, cfg = build(idx, tag)
    lg = open("/tmp/s40_%d.log" % idx, "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                          stdout=lg, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    R = {"idx": idx, "tag": tag}
    try:
        ip0 = ip_of(port)
        if not ip0:
            R["err"] = "no_ip"
            print("[W%d] %-44s NO_IP" % (idx, tag), flush=True)
            return R
        R["ip"] = ip0
        print("[W%d] %-44s -> %s" % (idx, tag, ip0), flush=True)

        # ---------------- 阶段 A：单 session ----------------
        S1 = P._session_id()
        R["S1"] = S1
        a_ok = 0
        a_stop = None
        for i in range(PHASE_A):
            v, st, ra = call(port, S1)
            if v != "OK":
                a_stop = {"at": i + 1, "verdict": v, "st": st, "ra": ra}
                print("[W%d]   A 单session 第 %d 发 %s ra=%s" % (idx, i + 1, v, ra),
                      flush=True)
                break
            a_ok += 1
            if (i + 1) % 10 == 0:
                ipn = ip_of(port)
                print("[W%d]   A %2d 发全 OK  ip=%s" % (idx, i + 1, ipn), flush=True)
                if ipn and ipn != ip0:
                    R["err"] = "ip_changed_A"
                    R["ip_changed_to"] = ipn
                    return R
        R["phaseA"] = {"ok": a_ok, "stop": a_stop}
        if a_ok < PHASE_A:
            R["conclusion"] = ("该 IP 总额度 < %d，单 session 就撞顶，"
                               "无法用它区分 session 维度" % PHASE_A)
            print("[W%d]   => 额度不足，换下一个 IP" % idx, flush=True)
            return R

        # ---------------- 阶段 B：每发换新 session ----------------
        sids = {S1}
        new_ok = 0
        total = a_ok
        b_stop = None
        for i in range(MAXTOTAL - total):
            sid = P._session_id()
            while sid in sids:
                sid = P._session_id()
            sids.add(sid)
            v, st, ra = call(port, sid)
            if v != "OK":
                b_stop = {"new_session_no": i + 1, "total_shots": total,
                          "verdict": v, "st": st, "ra": ra}
                print("[W%d]   B 第 %d 个新 session（总第 %d 发）%s ra=%s"
                      % (idx, i + 1, total, v, ra), flush=True)
                break
            new_ok += 1
            total += 1
            if (i + 1) % 10 == 0:
                ipn = ip_of(port)
                print("[W%d]   B 已换 %2d 个新 session，总 %d 发全 OK  ip=%s"
                      % (idx, i + 1, total, ipn), flush=True)
                if ipn and ipn != ip0:
                    R["err"] = "ip_changed_B"
                    R["ip_changed_to"] = ipn
                    return R
        R["phaseB"] = {"new_sessions_ok": new_ok,
                       "distinct_sessions": len(sids),
                       "total_shots": total,
                       "stop": b_stop}

        # 触顶/结束时用 2 个全新 session 复核
        conf = []
        for k in range(2):
            s2 = P._session_id()
            while s2 in sids:
                s2 = P._session_id()
            sids.add(s2)
            v2, st2, ra2 = call(port, s2)
            conf.append({"verdict": v2, "st": st2, "ra": ra2})
            print("[W%d]   复核全新 session -> %s ra=%s" % (idx, v2, ra2), flush=True)
        R["confirm"] = conf
        R["distinct_sessions_final"] = len(sids)
        return R
    finally:
        pr.terminate()
        try:
            pr.wait(timeout=6)
        except Exception:
            pass


def worker(idx, pool):
    """依次尝试池里的节点，直到拿到一份有效结果。"""
    res = []
    for tag in pool:
        try:
            r = run_one(idx, tag)
        except Exception as e:
            r = {"idx": idx, "tag": tag, "err": "exception:%s" % e}
        res.append(r)
        with LOCK:
            json.dump(res, open("/tmp/s40_part%d.json" % idx, "w"),
                      ensure_ascii=False, indent=1)
        if r.get("phaseB") and r.get("phaseB", {}).get("stop"):
            print("[W%d] === 有效结果，停止尝试 ===" % idx, flush=True)
            return res
    return res


def main():
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(worker, 0, POOL_T)
        f2 = ex.submit(worker, 1, POOL_D)
        r1, r2 = f1.result(), f2.result()
    json.dump({"w0": r1, "w1": r2}, open(OUT, "w"), ensure_ascii=False, indent=1)
    print("\n===== SUMMARY =====", flush=True)
    for w in (r1, r2):
        for r in w:
            if not r.get("phaseB"):
                continue
            A = r["phaseA"]; B = r["phaseB"]
            print("IP=%s  阶段A单session=%d发  阶段B新session成功=%d个  总发=%d"
                  % (r.get("ip"), A["ok"], B["new_sessions_ok"], B["total_shots"]))
            print("   死亡点: 第%d个新session / 总第%d发  明细=%s"
                  % (B["stop"]["new_session_no"] if B["stop"] else -1,
                     B["stop"]["total_shots"] if B["stop"] else B["total_shots"],
                     B["stop"]))
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
