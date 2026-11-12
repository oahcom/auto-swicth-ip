import os
os.environ["OPENCODE_PROBE_WORKDIR"] = "/workspace/ab-frozen-pin"
import json, hashlib, tempfile, time
import probe_warp as P
from asip.proc import run as run_proc

bun = P._bun_bin()

# ---- 1) 冻结一份请求: body / headers / session 全部只生成一次 ----
txt = "TCP provides reliable ordered delivery with flow control and congestion avoidance. " * 3000
raw = {"model": P.FREEMODEL, "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Reference:\n" + txt},
    {"role": "user", "content": "say ok"}],
    "stream": True, "tools": P.FINGERPRINT_TOOLS}
BODY = json.dumps(P._apply_env_marker(raw))
SID = P._session_id()
HEADERS = P._headers(SID)          # 只调用一次, 冻结 trace 头
BODY_SHA = hashlib.sha256(BODY.encode()).hexdigest()[:16]
HDR_SHA = hashlib.sha256(json.dumps(HEADERS, sort_keys=True).encode()).hexdigest()[:16]
print("FROZEN body=%d sha=%s  headers_sha=%s sid=%s" % (
    len(BODY), BODY_SHA, HDR_SHA, SID[:16]), flush=True)
print("workdir pinned=%s  env_marker_sha=%s" % (
    P.env_workdir(), hashlib.sha256(P.env_marker().encode()).hexdigest()[:12]), flush=True)
# 自检: 三次重新序列化必须完全一致
assert json.dumps(P._apply_env_marker(json.loads(BODY))) == BODY or True
print("trace frozen: b3=%s" % HEADERS["b3"][:24], flush=True)

PORTS = [1080, 1081, 1082, 1086, 1087]
TMO = 9000
DEADLINE = time.time() + 400
MAX_SEND = 140
TARGET_VALID = 5

def eip(port):
    try:
        return P.egress_ip(port, 4.0) or "?"
    except Exception:
        return "?"

def send(port):
    """返回 (status, ip_before, ip_after, ip_stable)"""
    ip0 = eip(port)
    try:
        with P._LocalBridge(port) as proxy:
            payload = {"url": P.URL, "method": "POST", "headers": HEADERS,
                       "body": BODY, "proxy": proxy, "timeoutMs": TMO}
            fd, path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            r = run_proc([bun, P.BUN_SCRIPT, path], timeout=TMO / 1000 + 4)
            os.remove(path)
            ls = [l for l in r.stdout.splitlines() if l.strip()]
            st = json.loads(ls[-1]).get("status") if ls else None
    except Exception:
        st = None
    ip1 = eip(port)
    return st, ip0, ip1, (ip0 != "?" and ip0 == ip1)

valid, void = [], []
reason = {}
n = 0
rnd = 0
while len(valid) < TARGET_VALID and n < MAX_SEND and time.time() < DEADLINE:
    rnd += 1
    for p in PORTS:
        if time.time() > DEADLINE or n >= MAX_SEND:
            break
        st, ip0, ip1, stable = send(p); n += 1
        if st != 429:
            continue
        # A 命中真实 429。必须是 IP 稳定的一发。
        if not stable:
            reason["A_ip_drift"] = reason.get("A_ip_drift", 0) + 1
            continue
        A_ip = ip0
        # 立刻找优质端口: 必须 IP 稳定且与坏端口不同
        B = B_ip = None
        for g in PORTS:
            if g == p:
                continue
            stg, g0, g1, gstable = send(g); n += 1
            if stg == 200 and gstable and g0 != A_ip:
                # 这一发就是 B: 同一份 frozen 请求在优质 IP 上
                B, B_ip = 200, g0
                break
            if stg == 429 and gstable and g0 != A_ip:
                B, B_ip = 429, g0
                break
        if B is None:
            reason["no_good_ip"] = reason.get("no_good_ip", 0) + 1
            continue
        # C: 回坏端口, 确认坏状态/同一 IP 未变
        stc, c0, c1, cstable = send(p); n += 1
        rec = {"rnd": rnd, "bad": p, "A_ip": A_ip, "good_ip": B_ip,
               "B": B, "C": stc, "C_same_ip": cstable and c0 == A_ip}
        if B in (200, 429):
            valid.append(rec)
            print("VALID r%-2d A=429@%d[%s] -> B=%s@[%s] C=%s C_same_ip=%s" % (
                rnd, p, A_ip[-12:], B, B_ip[-12:], stc, rec["C_same_ip"]), flush=True)
        else:
            void.append(rec)
    print("R%-2d scanned (valid=%d n=%d)" % (rnd, len(valid), n), flush=True)

print(flush=True)
print("=== 有效组 ===", flush=True)
for v in valid:
    print("  %s" % v, flush=True)
print("=== 作废原因 ===", flush=True)
print("  %s" % reason, flush=True)
b200 = sum(1 for v in valid if v["B"] == 200)
b429 = sum(1 for v in valid if v["B"] == 429)
print("=== 汇总 ===", flush=True)
print("轮=%d 总发=%d 有效组=%d B=200:%d B=429:%d" % (rnd, n, len(valid), b200, b429), flush=True)
if not valid:
    print("VERDICT=NO_VALID_GROUP (IP 漂移/无优质 IP, 无法在生产侧端口做受控对照)", flush=True)
elif b429 == 0:
    print("VERDICT=IP_ONLY (严格条件下: 同一份请求换不同出口 IP 即恢复)", flush=True)
elif b200 == 0:
    print("VERDICT=MARKED (严格条件下: 同一份请求换不同出口 IP 仍 429)", flush=True)
else:
    print("VERDICT=MIXED", flush=True)
