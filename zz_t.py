# -*- coding: utf-8 -*-
"""zz_t.py —— 同一时刻对齐探测：排除"请求时间不同"的干扰。

原理：若额度在统一时刻 T 重置，则对任意请求时刻 t，ra = T - t。
     所以同一秒内向多个已 429 的 IP 各打一发，t 相同，
     各 ra 的差值 = 各自重置时刻 T 的差值，与请求时间无关。

用法: python3 zz_t.py "tag1:v4" "tag2:v6" ...
      端口 7500 起，每个 tag 一个独立 sing-box
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import probe_warp as P

BASE = "singbox-config.json-slot3.json"
BODY = json.dumps(P._body())
LOCK = threading.Lock()
READY = []
RESULT = {}


def build(idx, tag, fam, port):
    cfg = "/tmp/zt_%d.json" % idx
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
    if fam == "v6":
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


def ip_of(port, fam, t=6):
    urls = ("https://icanhazip.com", "https://api6.ipify.org") if fam == "v6" \
        else ("https://icanhazip.com", "https://api.ipify.org")
    for u in urls:
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


def call_raw(port, sid, timeout_ms=30000):
    """返回 (verdict, status, retry_after, t_send_epoch)"""
    from asip.proc import run as run_proc
    bun = P._bun_bin()
    payload = {"url": P.URL, "method": "POST", "headers": P._headers(sid),
               "body": BODY, "proxy": "http://127.0.0.1:%d" % port,
               "timeoutMs": timeout_ms}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    t_send = time.time()
    try:
        r = run_proc([bun, P.BUN_SCRIPT, path], timeout=timeout_ms / 1000.0 + 8)
    finally:
        os.remove(path)
    if r.error or r.timed_out or r.code != 0:
        return "trans", None, None, t_send
    ls = [l for l in r.stdout.splitlines() if l.strip()]
    if not ls:
        return "trans", None, None, t_send
    try:
        d = json.loads(ls[-1])
    except Exception:
        return "trans", None, None, t_send
    if d.get("error"):
        return "trans", None, None, t_send
    st = d.get("status")
    head = str(d.get("head") or "")
    v = P.classify(st, head[:160])
    ra = P.parse_retry_after(d.get("retry") or {}, head) if v != "OK" else None
    return v, st, ra, t_send


def fmt(epoch):
    return datetime.datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M:%S.%f")[:-3]


def main():
    specs = []
    for a in sys.argv[1:]:
        if ":" in a:
            parts = a.rsplit(":", 1)
            if parts[1] in ("v4", "v6"):
                specs.append((parts[0], parts[1]))
                continue
        specs.append((a, "v6"))

    procs = []
    ports = []
    for i, (tag, fam) in enumerate(specs):
        port = 7500 + i
        cfg = build(i, tag, fam, port)
        lg = open("/tmp/zt_%d.log" % i, "w")
        pr = subprocess.Popen(["./sing-box", "run", "-c", cfg],
                              stdout=lg, stderr=subprocess.STDOUT)
        procs.append(pr)
        ports.append(port)
    time.sleep(4.0)

    try:
        # 1. 记录各自出口 IP
        ips = []
        for i, (tag, fam) in enumerate(specs):
            ip = ip_of(ports[i], fam)
            ips.append(ip)
            print("  %-44s -> %s" % (tag[:44], ip), flush=True)

        # 2. 对齐到下一个整 10 秒发起
        now = time.time()
        target = (int(now) // 10 + 2) * 10
        print("\n对齐发射时刻: %s (还有 %.1fs)"
              % (fmt(target), target - now), flush=True)
        time.sleep(max(0, target - time.time() - 0.6))
        while time.time() < target:
            time.sleep(0.002)

        # 3. 同时发射
        def fire(i):
            sid = P._session_id()
            v, st, ra, ts = call_raw(ports[i], sid)
            with LOCK:
                RESULT[i] = {"verdict": v, "st": st, "ra": ra, "t_send": ts}

        ths = [threading.Thread(target=fire, args=(i,)) for i in range(len(specs))]
        for t in ths:
            t.start()
        for t in ths:
            t.join(timeout=70)

        # 4. 汇总
        print("\n%-44s %-4s %-26s %-8s %-26s %s"
              % ("出口 tag", "IP族", "出口IP", "ra(秒)", "反推恢复时刻", "判定"))
        rows = []
        for i, (tag, fam) in enumerate(specs):
            r = RESULT.get(i) or {}
            ts = r.get("t_send")
            ra = r.get("ra")
            T = (ts + ra) if (ts and ra) else None
            print("%-44s %-4s %-26s %-8s %-26s %s"
                  % (tag[:44], fam, str(ips[i])[:26], ra,
                     fmt(T) if T else "-", r.get("verdict")), flush=True)
            rows.append({"tag": tag, "family": fam, "ip": ips[i],
                         "t_send": ts, "t_send_h": fmt(ts) if ts else None,
                         "ra": ra, "reset_epoch": T,
                         "reset_h": fmt(T) if T else None,
                         "verdict": r.get("verdict"), "status": r.get("st")})

        # 5. 差值表（核心）
        valid = [x for x in rows if x["reset_epoch"]]
        print("\n=== 同一时刻发射：各自重置时刻的差值 ===")
        if len(valid) >= 2:
            base = min(x["reset_epoch"] for x in valid)
            for x in sorted(valid, key=lambda y: y["reset_epoch"]):
                print("  %-44s %s   +%.0f 秒 (%.1f 分)"
                      % (x["tag"][:44], x["reset_h"], x["reset_epoch"] - base,
                         (x["reset_epoch"] - base) / 60.0))
            spread = max(x["reset_epoch"] for x in valid) - base
            print("\n  跨度 = %.0f 秒 (%.1f 分钟)" % (spread, spread / 60.0))
            if spread < 120:
                print("  → 跨度 < 2 分钟：可以认定是**同一个统一重置时刻**")
            else:
                print("  → 跨度大：各自的窗口起点不同，不是统一重置")
        json.dump(rows, open("zz_t_result.json", "w"), ensure_ascii=False, indent=1)
        print("\nDONE")
    finally:
        for pr in procs:
            pr.terminate()
        for pr in procs:
            try:
                pr.wait(timeout=5)
            except Exception:
                pass


if __name__ == "__main__":
    main()
