# -*- coding: utf-8 -*-
"""Session 独立性实验：同一 session 在 IP-A 打死，切到 IP-B 是否仍 429。

=== 判定逻辑 ===
第 3 步(S 在 B)  第 4 步(全新 session 在 B)
   OK               OK        -> 429 跟 IP 走（session 未绑定限流）
   429              OK        -> session 被限流，跨 IP 生效
   429              429       -> B 不干净，换 B 重跑

=== 附加步骤 ===
第 5 步：若 S 在 B 恢复，继续用 S 打到 429，记录总发数 n1+n2
         —— 测 session 配额是否跨 IP 累计
第 6 步：切回 A，用全新 session 打 1 发
         —— 证明 A 已死与 session 无关
第 7 步：在 A 上用全新 session 再打若干发（最多 5），确认 A 的锁死程度

用法: python3 zz_sess_indep.py scan N     # 扫描 N 个新鲜节点找干净出口
      python3 zz_sess_indep.py run        # 跑完整实验
"""
import json, os, subprocess, sys, tempfile, time, urllib.request
import probe_warp as P

BASE = "singbox-config.json-slot3.json"
CFG = "/tmp/sess-indep.json"
PORT = 7030
CLASH = 9130
SECRET = "sessindep"
BODY = json.dumps(P._body())
OUT = "sess_indep_result.json"

MODE = sys.argv[1] if len(sys.argv) > 1 else "run"
ARG = int(sys.argv[2]) if len(sys.argv) > 2 else 20

# 今天已经打过容量的节点，排除
USED = {
    "ttll20-ooi.dankenet.help:32236-any", "ttll20-ooi.dankenet.help:17121-any",
    "ttll20-ooi.dankenet.help:44324-any", "ddixx-1dd1.dankenet.help:58546-any",
    "ddixx-1dd1.dankenet.help:10309-any", "ddixx-1dd1.dankenet.help:64245-any",
    "upoz.cn.greatgameupload.org:20470-any", "upoz.cn.greatgameupload.org:13493-any",
    "upoz.cn.greatgameupload.org:32345-any", "upoz.cn.greatgameupload.org:36958-any",
    "normal.cn.greatgameupload.org:2155-any", "normal.cn.greatgameupload.org:29512-any",
    "normal.cn.greatgameupload.org:48535-any",
}


def fresh_tags():
    d = json.load(open(BASE))
    skip = ("selector", "urltest", "direct", "block", "dns")
    out = []
    for o in d["outbounds"]:
        t = o.get("type")
        tag = o.get("tag") or ""
        if t in skip or tag in USED:
            continue
        if tag.startswith("WARP"):
            continue
        out.append(tag)
    # 按 host 轮转取样，保证跨家族
    fam = {}
    for t in out:
        fam.setdefault(t.split(":")[0], []).append(t)
    picked, i = [], 0
    while True:
        added = False
        for k in fam:
            if i < len(fam[k]):
                picked.append(fam[k][i]); added = True
        if not added:
            break
        i += 1
    return picked


def build(tags):
    d = json.load(open(BASE))
    have = {o.get("tag") for o in d["outbounds"]}
    tags = [t for t in tags if t in have]
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
    d["log"] = {"level": "error"}
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
            r.read()
        return True
    except Exception:
        return False


def ip_of(t=6):
    for u in ("https://icanhazip.com", "https://api.ipify.org", "https://ifconfig.me/ip"):
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


def call(sid, timeout_ms=35000):
    from asip.proc import run as run_proc
    bun = P._bun_bin()
    payload = {"url": P.URL, "method": "POST", "headers": P._headers(sid), "body": BODY,
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


def burn(sid, limit=200, label=""):
    """用固定 session 连打，直到失败。返回 (成功数, 终止判定, ra)"""
    ok = 0
    trans = 0
    for i in range(limit):
        v, st, ra = call(sid)
        if v == "OK":
            ok += 1
            if ok % 10 == 0:
                print("     %s 已 %d 发..." % (label, ok), flush=True)
        elif v == "trans":
            trans += 1
            if trans >= 3:
                return ok, "trans", None
            time.sleep(1)
            continue
        else:
            return ok, v, ra
    return ok, "limit", None


def start():
    log = open("/tmp/sess-indep.log", "w")
    pr = subprocess.Popen(["./sing-box", "run", "-c", CFG],
                          stdout=log, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    return pr, log


def stop(pr, log):
    pr.terminate()
    try:
        pr.wait(timeout=6)
    except Exception:
        pr.kill()
    try:
        log.close()
    except Exception:
        pass
    time.sleep(1)


def cmd_scan(n):
    tags = fresh_tags()[:n]
    print("扫描 %d 个新鲜节点..." % len(tags), flush=True)
    build(tags)
    pr, log = start()
    good = []
    try:
        for t in tags:
            if not select(t):
                print("  SELECT_ERR %s" % t, flush=True)
                continue
            time.sleep(0.6)
            ip = ip_of()
            if not ip:
                print("  NO_IP   %-40s" % t, flush=True)
                continue
            v, st, ra = call(P._session_id())
            print("  %-40s ip=%-16s %-6s ra=%s" % (t, ip, v, ra), flush=True)
            if v == "OK":
                good.append({"tag": t, "ip": ip})
            time.sleep(0.4)
    finally:
        stop(pr, log)
    json.dump(good, open("sess_scan.json", "w"), indent=1)
    print("\n干净出口 %d 个 -> sess_scan.json" % len(good), flush=True)
    for g in good:
        print("   %s  %s" % (g["ip"], g["tag"]), flush=True)


def same24(ip1, ip2):
    return ".".join(ip1.split(".")[:3]) == ".".join(ip2.split(".")[:3])


def cmd_run():
    good = json.load(open("sess_scan.json"))
    if len(good) < 2:
        print("干净出口不足 2 个，先跑 scan")
        return
    A = good[0]
    B = next((g for g in good[1:] if not same24(g["ip"], A["ip"])), None)
    if not B:
        print("找不到与 A 不同段(不同 /24)的 B")
        return
    print("=" * 72)
    print("A = %-16s  %s" % (A["ip"], A["tag"]))
    print("B = %-16s  %s" % (B["ip"], B["tag"]))
    print("=" * 72, flush=True)

    build([A["tag"], B["tag"]])
    pr, log = start()
    R = {"A": A, "B": B, "steps": []}
    try:
        # --- 步骤 1: 切到 A 并确认出口 ---
        select(A["tag"]); time.sleep(0.8)
        ipA = ip_of()
        print("\n[1] 切到 A -> 出口 %s" % ipA, flush=True)
        if ipA != A["ip"]:
            print("    出口与预期不符，A 可能换 IP 了，实际用 %s" % ipA)
            A["ip"] = ipA
        R["A_ip_seen"] = ipA

        # --- 步骤 2: 固定 session S 在 A 上打到 429 ---
        S = P._session_id()
        print("\n[2] session S = %s 在 A 上连打..." % S, flush=True)
        n1, why, ra1 = burn(S, limit=200, label="A")
        ipA2 = ip_of()
        print("    A 上打出 %d 发 终止=%s ra=%s；出口仍为 %s (same=%s)"
              % (n1, why, ra1, ipA2, ipA2 == ipA), flush=True)
        R["step2_A"] = {"sid": S, "ok": n1, "stop": why, "ra": ra1, "ip": ipA2}

        # --- 步骤 3: 切到 B，同一 session S 打 1 发 ---
        select(B["tag"]); time.sleep(1.2)
        ipB = ip_of()
        print("\n[3] 切到 B -> 出口 %s" % ipB, flush=True)
        if ipB == ipA:
            print("    出口没变！B 切换失败，终止")
            R["error"] = "B_switch_failed"
            json.dump(R, open(OUT, "w"), indent=1)
            return
        R["B_ip_seen"] = ipB
        v3, st3, ra3 = call(S)
        print("    同一 session S 在 B 上 -> %-6s st=%s ra=%s" % (v3, st3, ra3), flush=True)
        R["step3_S_on_B"] = {"verdict": v3, "st": st3, "ra": ra3}

        # --- 步骤 4: 同样在 B，用全新 session 打 1 发（对照）---
        S2 = P._session_id()
        v4, st4, ra4 = call(S2)
        print("    全新 session S2=%s 在 B 上 -> %-6s st=%s ra=%s"
              % (S2[:20], v4, st4, ra4), flush=True)
        R["step4_newS_on_B"] = {"sid": S2, "verdict": v4, "st": st4, "ra": ra4}

        # --- 判定 ---
        if v4 != "OK":
            print("\n    !! B 也不干净（全新 session 都 %s），本组作废，换 B 重跑" % v4, flush=True)
            R["error"] = "B_not_clean"
        else:
            if v3 == "OK":
                verdict = "额度跟 IP 走：session 在 A 耗尽后，跨到干净 IP 依然可用"
            else:
                verdict = "session 被限流且跨 IP 生效：同一 session 换到干净 IP 仍 429"
            print("\n    >>> 判定：%s" % verdict, flush=True)
            R["verdict"] = verdict

            # --- 步骤 5: 若 S 在 B 恢复，继续用 S 打，测配额是否跨 IP 累计 ---
            if v3 == "OK":
                print("\n[5] 继续用同一 session S 在 B 上打，测跨 IP 是否累计...", flush=True)
                n2, why2, ra2 = burn(S, limit=200, label="B")
                total = n1 + n2 + 1  # +1 是步骤3那发
                print("    B 上又打 %d 发 终止=%s ra=%s" % (n2, why2, ra2), flush=True)
                print("    同一 session 跨 A+B 累计 = %d 发（A:%d + 转场1 + B:%d）"
                      % (total, n1, n2), flush=True)
                R["step5_S_on_B_burn"] = {"ok": n2, "stop": why2, "ra": ra2,
                                          "total_with_same_session": total}

        # --- 步骤 6: 切回 A，全新 session 打 1 发 ---
        select(A["tag"]); time.sleep(1.2)
        ipA3 = ip_of()
        print("\n[6] 切回 A -> 出口 %s" % ipA3, flush=True)
        S3 = P._session_id()
        v6, st6, ra6 = call(S3)
        print("    全新 session 在 A 上 -> %-6s st=%s ra=%s" % (v6, st6, ra6), flush=True)
        R["step6_newS_on_A"] = {"sid": S3, "verdict": v6, "st": st6, "ra": ra6, "ip": ipA3}

        # --- 步骤 7: A 上再打几发全新 session，看锁死程度 ---
        print("\n[7] A 上再打 3 发全新 session...", flush=True)
        extra = []
        for i in range(3):
            s = P._session_id()
            v, st, ra = call(s)
            extra.append({"sid": s[:20], "verdict": v, "ra": ra})
            print("    #%d %s -> %s ra=%s" % (i + 1, s[:16], v, ra), flush=True)
            time.sleep(0.8)
        R["step7_A_extra"] = extra

    finally:
        stop(pr, log)
        json.dump(R, open(OUT, "w"), indent=1, ensure_ascii=False)
        print("\n结果已存 %s" % OUT, flush=True)


if __name__ == "__main__":
    if MODE == "scan":
        cmd_scan(ARG)
    else:
        cmd_run()
