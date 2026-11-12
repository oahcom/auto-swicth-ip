# -*- coding: utf-8 -*-
"""复现生产 429:合法 session + 大 body 在各端口打,找到 429 的端口后,
把同一请求(body+session 头一字不改)搬到好节点再打一次。
再测:同 body 换新 session(判断标记维度)。"""
import probe_warp as P, json, os, tempfile, time, secrets
from asip.proc import run as run_proc

bun = P._bun_bin()

# 真实感内容(之前在 1087 缓存 session 上 400KB 成功过的同款)
REAL = """TCP/IP model organizes protocols into four layers: Link, Internet, Transport, Application.
TCP provides reliable ordered delivery with flow control. UDP provides lightweight best-effort delivery.
Three-way handshake: SYN SYN-ACK ACK. Sliding window for flow control.
Congestion control: slow start, congestion avoidance, fast retransmit, fast recovery.
IP addressing: IPv4 32-bit, IPv6 128-bit. CIDR notation for subnet prefix.
DNS resolves domain names to IP addresses. HTTP is the foundation of web communication.
"""

def big_body():
    rounds = 400 * 1024 // len(REAL)
    body = {"model": P.FREEMODEL,
            "messages": [{"role": "system", "content": "You are a helpful assistant."},
                          {"role": "user", "content": "Reference:\n" + REAL * rounds},
                          {"role": "assistant", "content": "Got it."},
                          {"role": "user", "content": "say ok"}],
            "stream": True, "tools": P.FINGERPRINT_TOOLS}
    return P._apply_env_marker(body)

BODY_STR = json.dumps(big_body())
print("body=%d bytes" % len(BODY_STR))

def send(port, sid, body_str=None):
    bs = body_str or BODY_STR
    with P._LocalBridge(port) as proxy:
        payload = {"url": P.URL, "method": "POST", "headers": P._headers(sid),
                   "body": bs, "proxy": proxy, "timeoutMs": 30000}
        fd, path = tempfile.mkstemp(prefix="repro_", suffix=".json")
        with os.fdopen(fd, "w") as f: json.dump(payload, f)
        r = run_proc([bun, P.BUN_SCRIPT, path], timeout=35)
        os.remove(path)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        if lines:
            d = json.loads(lines[-1])
            return d.get("status"), P.classify(d.get("status"), str(d.get("head", ""))[:60])
    return None, "no_out"

print("\n=== Phase 1: 合法新 session + 400KB body, 各端口 ===")
results = {}
for p in [1080, 1081, 1082, 1086, 1087, 1088]:
    sid = P._session_id()  # 每端口独立新 session(合法格式)
    st, v = send(p, sid)
    results[p] = (st, v, sid)
    print("port=%-4d sid=%s -> status=%-4s %s" % (p, sid[:16], st, v))
    time.sleep(1)

bad_ports = [p for p, (st, v, _) in results.items() if st == 429 or "quota" in str(v)]
good_ports = [p for p, (st, v, _) in results.items() if st == 200]

print("\n429 端口: %s  好 端口: %s" % (bad_ports, good_ports))

if not bad_ports:
    print("\n!! 本轮无端口 429:所有端口合法 session + 大 body 都过了。")
    print("   说明当前没有耗尽的出口。生产 429 需要等真实耗尽窗口,或加大请求速率复现。")
    # 加码:在同一端口快速连打 20 次大 body 试图触发真实 IP 级 429
    print("\n=== Phase 2: port 1080 快速连打 20 次大 body ===")
    sid = P._session_id()
    for i in range(20):
        st, v = send(1080, sid)
        if i < 3 or i % 5 == 4 or st == 429:
            print("#%-2d status=%s %s" % (i + 1, st, v))
        if st == 429 or "quota" in str(v):
            print(">>> 429 出现于 #%d! 立刻拿同一请求去好节点..." % (i + 1))
            bad_ports = [1080]
            break
        time.sleep(0.3)

if bad_ports and good_ports:
    bp = bad_ports[0]
    gp = good_ports[0]
    st, v, sid = results[bp] if bp in results else (None, None, P._session_id())

    print("\n=== Phase 3: 同一请求(body+session 完全一致) %s -> %s ===" % (bp, gp))
    st2, v2 = send(gp, sid)
    print("好节点 %d: status=%s %s" % (gp, st2, v2))

    print("\n=== Phase 4: 同 body 换新 session, 回到 429 端口 %s ===" % bp)
    sid_new = P._session_id()
    st3, v3 = send(bp, sid_new)
    print("新 session @ %d: status=%s %s" % (bp, st3, v3))

    print("\n=== Phase 5: 同 body 再换一个新 session, 好节点(对照组) ===")
    sid_new2 = P._session_id()
    st4, v4 = send(gp, sid_new2)
    print("新 session @ %d: status=%s %s" % (gp, st4, v4))

    print("\n=== 结论矩阵 ===")
    print("原 session+body @ 坏节点: %s" % (results.get(bp, ("?",))[0] if bp in results else "?"))
    print("原 session+body @ 好节点: %s %s" % (st2, v2))
    print("新 session+同 body @ 坏节点: %s %s" % (st3, v3))
    print("新 session+同 body @ 好节点: %s %s" % (st4, v4))
    print("""
解读:
- 好节点 429 且新 session 坏节点也 429 -> body/内容被标记
- 好节点 200 且新 session 坏节点 200 -> 纯 IP 瞬时耗尽, 换 IP 即恢复(重试循环该换 egress)
- 好节点 200 且新 session 坏节点 429 -> IP 仍耗尽, 但内容没被标记
""")
