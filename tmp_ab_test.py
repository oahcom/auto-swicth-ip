# -*- coding: utf-8 -*-
"""大请求在 429 出口(7892)打 -> 429 后,一模一样的请求马上打到优质端口。"""
import probe_warp as P, json, os, tempfile, time
from asip.proc import run as run_proc

bun = P._bun_bin()
BAD_PROXY = "http://127.0.0.1:7892"   # 生产正在 429 的出口
GOOD_PORT = 1081                        # 之前验证过 200 的优质端口

REAL = """TCP/IP model organizes protocols into four layers: Link, Internet, Transport, Application.
TCP provides reliable ordered delivery with flow control. UDP provides lightweight best-effort delivery.
Three-way handshake: SYN SYN-ACK ACK. Sliding window for flow control.
Congestion control: slow start, congestion avoidance, fast retransmit, fast recovery.
IP addressing: IPv4 32-bit, IPv6 128-bit. CIDR notation for subnet prefix.
DNS resolves domain names to IP addresses. HTTP is the foundation of web communication.
"""
rounds = 400 * 1024 // len(REAL)
body = {"model": P.FREEMODEL,
        "messages": [{"role": "system", "content": "You are a helpful assistant."},
                      {"role": "user", "content": "Reference:\n" + REAL * rounds},
                      {"role": "assistant", "content": "Got it."},
                      {"role": "user", "content": "say ok"}],
        "stream": True, "tools": P.FINGERPRINT_TOOLS}
BODY_STR = json.dumps(P._apply_env_marker(body))
print("body=%d bytes" % len(BODY_STR), flush=True)

SID = P._session_id()  # 同一个 session 贯穿全程

def send_via(proxy, tag):
    payload = {"url": P.URL, "method": "POST", "headers": P._headers(SID),
               "body": BODY_STR, "proxy": proxy, "timeoutMs": 30000}
    fd, path = tempfile.mkstemp(prefix="ab_", suffix=".json")
    with os.fdopen(fd, "w") as f: json.dump(payload, f)
    r = run_proc([bun, P.BUN_SCRIPT, path], timeout=35)
    os.remove(path)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    if lines:
        d = json.loads(lines[-1])
        st, v = d.get("status"), P.classify(d.get("status"), str(d.get("head", ""))[:60])
        print("%-28s sid=%s -> status=%-4s %s" % (tag, SID[:16], st, v), flush=True)
        return st, v
    print("%-28s NO_OUTPUT" % tag, flush=True)
    return None, "no_out"

# 第 1 步: 大请求反复打 429 出口,直到拿到 429
got_429 = False
for i in range(10):
    st, v = send_via(BAD_PROXY, "A#%d 429出口(7892)" % (i + 1))
    if st == 429 or "quota" in str(v):
        got_429 = True
        print(">>> 拿到 429 (第 %d 次)" % (i + 1), flush=True)
        break
    time.sleep(0.5)

if not got_429:
    print("!! 7892 没打出 429(可能生产刚好已恢复/出口已轮换)", flush=True)

# 第 2 步: 一模一样的请求马上打优质端口
with P._LocalBridge(GOOD_PORT) as gp:
    st2, v2 = send_via(gp, "B  优质端口(%d)" % GOOD_PORT)

# 第 3 步: 回到 429 出口再打一次(对照)
st3, v3 = send_via(BAD_PROXY, "C  再回 429 出口(7892)")

print(flush=True)
print("=== 结论 ===", flush=True)
print("429出口: %s | 优质端口同请求: %s | 回429出口: %s" % (got_429, st2, st3), flush=True)
