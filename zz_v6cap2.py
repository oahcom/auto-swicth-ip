# -*- coding: utf-8 -*-
"""v6 出口容量测试（强制解析到 IPv6）。

关键：route.rules 里 action=resolve 必须同时给 server 和 strategy，只给 strategy 不生效。
      已验证：icanhazip.com 强制 ipv6_only 后返回 2405:84c0:8011:d000::。

mode:
  single : 1 个固定 session 连打 -> 单会话上限（发数额度）
  multi  : 每发换全新 session     -> 多会话上限（看是否卡在 40 个 session）

每 10 发校验出口 IP，变化立即作废。

用法: python3 zz_v6cap2.py <idx> <tag> <mode> <maxn>
"""
import json, os, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
import probe_warp as P

BASE = "singbox-config.json-slot3.json"
BODY = json.dumps(P._body())
LOCK = threading.Lock()


def build(idx, tag):
    port = 7080 + idx
    cfg = "/tmp/v6c2_%d.json" % idx
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
        # 关键：server + strategy 同时给，才会解析成纯 AAAA
        {"action": "resolve", "domain_suffix": ["opencode.ai", ".opencode.ai"],
         "server": "dns-opencode", "strategy": "ipv6_only"},
        # 探测域名也强制 v6，否则出口校验会被 v4 干扰
        {"action": "resolve",
         "domain_suffix": ["icanhazip.com", "api6.ipify.org"],
         "server": "dns-opencode", "strategy": "ipv6_only"},
    ]
    d["experimental"] = {"cache_file": {"enabled": False}}
    d["log"] = {"level": "error"}
    json.dump(d, open(cfg, "w"))
    return port, cfg


def ip_of(port, t=6):
    for u in ("https://icanhazip.com", "https://api6.ipify.org"):
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


def run_one(idx, tag, mode, maxn):
    port, cfg = build(idx, tag)
    lg = open("/tmp/v6c2_%d.log" % idx, "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                          stdout=lg, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    R = {"idx": idx, "tag": tag, "mode": mode, "maxn": maxn}
    try:
        ip0 = ip_of(port)
        R["ip_start"] = ip0
        fam = "v6" if (ip0 and ":" in ip0) else ("v4" if ip0 else "none")
        R["family"] = fam
        print("[V%d] %-44s mode=%-6s 起始出口=%s (%s)"
              % (idx, tag, mode, ip0, fam), flush=True)
        if not ip0:
            R["err"] = "no_ip"
            return R
        if fam != "v6":
            R["err"] = "not_v6"
            print("[V%d]   出口不是 v6，不测" % idx, flush=True)
            return R

        sids = set()
        fixed = P._session_id()
        ok = 0
        stop = None
        for i in range(maxn):
            if mode == "single":
                sid = fixed
            else:
                sid = P._session_id()
                while sid in sids:
                    sid = P._session_id()
            sids.add(sid)
            v, st, ra = call(port, sid)
            tries = 0
            while v == "trans" and tries < 3:
                tries += 1
                time.sleep(1.5)
                print("[V%d]   第 %d 发 trans，第 %d 次重试" % (idx, i + 1, tries),
                      flush=True)
                v, st, ra = call(port, sid)
            if v != "OK":
                stop = {"at_shot": i + 1, "verdict": v, "st": st, "ra": ra,
                        "distinct_sessions": len(sids), "trans_retried": tries}
                print("[V%d]   第 %d 发 %s st=%s ra=%s (OK=%d, session=%d)"
                      % (idx, i + 1, v, st, ra, ok, len(sids)), flush=True)
                break
            ok += 1
            if (i + 1) % 10 == 0:
                ipn = ip_of(port)
                same = (ipn == ip0)
                print("[V%d]     %2d 发全 OK 出口=%s session=%d%s"
                      % (idx, i + 1, ipn, len(sids), "" if same else "  <<<变了"),
                      flush=True)
                if not same:
                    R["err"] = "ip_changed"
                    R["ip_changed_to"] = ipn
                    R["ok_before_change"] = ok
                    return R
        R["ok"] = ok
        R["stop"] = stop
        R["distinct_sessions"] = len(sids)
        conf = []
        for k in range(2):
            s2 = P._session_id()
            while s2 in sids:
                s2 = P._session_id()
            sids.add(s2)
            v2, st2, ra2 = call(port, s2)
            conf.append({"verdict": v2, "st": st2, "ra": ra2})
            print("[V%d]     复核全新 session -> %s ra=%s" % (idx, v2, ra2), flush=True)
        R["confirm"] = conf
        R["ip_end"] = ip_of(port)
        print("[V%d] === %s 完成: OK=%d 发, session=%d, 出口 %s -> %s ==="
              % (idx, mode, ok, len(sids), ip0, R["ip_end"]), flush=True)
        return R
    finally:
        pr.terminate()
        try:
            pr.wait(timeout=6)
        except Exception:
            pass


def main():
    # 参数: idx tag mode maxn  (可多组, 用 -- 分隔)
    groups = []
    cur = []
    for a in sys.argv[1:]:
        if a == "--":
            if cur:
                groups.append(cur)
            cur = []
        else:
            cur.append(a)
    if cur:
        groups.append(cur)
    jobs = [(int(g[0]), g[1], g[2], int(g[3])) for g in groups]

    results = {}

    def go(idx, tag, mode, maxn):
        try:
            r = run_one(idx, tag, mode, maxn)
        except Exception as e:
            r = {"idx": idx, "tag": tag, "mode": mode, "err": "exception:%s" % e}
        with LOCK:
            results["%d_%s" % (idx, mode)] = r
            json.dump(results, open("v6cap_result.json", "w"),
                      ensure_ascii=False, indent=1)
        return r

    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = [ex.submit(go, *j) for j in jobs]
        out = [f.result() for f in futs]

    print("\n===== SUMMARY =====", flush=True)
    for r in out:
        if r.get("err"):
            print("%-44s %-6s ERR=%s" % (r.get("tag"), r.get("mode"), r["err"]))
            continue
        st = r.get("stop")
        print("%-44s %-6s 出口=%s  OK=%d 发  session数=%d  死亡点=%s"
              % (r.get("tag"), r.get("mode"), r.get("ip_start"), r.get("ok", 0),
                 r.get("distinct_sessions"), st))
    json.dump(out, open("v6cap_result.json", "w"), ensure_ascii=False, indent=1)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
