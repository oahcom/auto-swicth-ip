#!/usr/bin/env python3
"""验证 quota 是否按 session 计。

同一出口（同一 IP），两组对照：
  组 1 FIXED_SESSION : 同一个 x-opencode-session 连打 N 次
  组 2 FRESH_SESSION : 每次换新 x-opencode-session 打 N 次

若 429 跟 session 走，组 1 会比组 2 差很多；若跟 IP 走，两组一样。
用法: python3 tmp_session_exp.py <socks_port> [N]
"""
import secrets
import sys
import time

sys.path.insert(0, "/home/administrator/dkk-projects/auto-switch-ip")
import probe_warp as P


def new_sid():
    return "ses_" + secrets.token_hex(12)


def one(port, sid, body_template):
    body = P._apply_env_marker(dict(body_template))
    headers = P._headers(sid)
    try:
        with P._LocalBridge(port) as proxy_url:
            status, head, retry, err = P._bun_probe(
                P._bun_bin(), proxy_url, sid, 20)
    except Exception as e:
        return "unreachable", 0, str(e)[:60]
    if err:
        return "unreachable", 0, err[:60]
    v = P.classify(status, head)
    return v, status, head.strip().replace("\n", " ")[:80]


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1080
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    egress = P.egress_ip(port)
    print("port=%d egress=%s n=%d\n" % (port, egress, n))

    base = {
        "model": P.FREEMODEL,
        "messages": [{"role": "user", "content": "say ok"}],
        "stream": True,
        "tools": P.FINGERPRINT_TOOLS,
    }

    fixed = new_sid()
    print("--- group1 FIXED_SESSION sid=%s ---" % fixed[:16])
    g1 = []
    for i in range(n):
        v, st, head = one(port, fixed, base)
        g1.append(v)
        print("  #%d %-9s status=%-5s %s" % (i + 1, v, st, head))
        time.sleep(0.8)

    print("\n--- group2 FRESH_SESSION (new sid each) ---")
    g2 = []
    for i in range(n):
        sid = new_sid()
        v, st, head = one(port, sid, base)
        g2.append(v)
        print("  #%d %-9s status=%-5s %s" % (i + 1, v, st, head))
        time.sleep(0.8)

    print("\nSUMMARY egress=%s fixed=%s fresh=%s"
          % (egress, g1, g2))


if __name__ == "__main__":
    main()
