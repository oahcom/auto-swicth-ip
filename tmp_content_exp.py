#!/usr/bin/env python3
"""验证 quota 是否按「请求内容」计。

设计（同一出口，控制变量）：
  Phase 1 REPEAT_SAME  : 同一份 content，同 session，连打 N 次 -> 看第几次开始 429
  Phase 2 SAME_NEWSESS : 同一份 content，每次换 session，再打 N 次 -> 排除 session 维度
  Phase 3 DIFF_CONTENT : 每次换一段新 content，打 N 次 -> 排除「只是打得太多」

判读：
  1 里很快 429 且 2 里仍 429、3 里 OK  => 按内容限流（用户假设成立）
  1 里 429 但 2 里 OK                  => 按 session 限流
  1 里全 OK 且 3 里也 OK               => 都不限，是 IP
用法: python3 tmp_content_exp.py <socks_port> [N]
"""
import json
import secrets
import sys
import time

sys.path.insert(0, "/home/administrator/dkk-projects/auto-switch-ip")
import probe_warp as P

CLIENT_TOOLS = [
    {"type": "function", "function": {"name": "Bash", "description": "Run bash command",
      "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "Read", "description": "Read a file",
      "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}},
]

SYSTEM = ("This conversation is powered by 9router_hermes\n\n"
          "Your main goal is to follow the USER's instructions at each message.\n\n"
          "Respond like terse caveman. All technical substance stay exact, only fluff die.")


def body_with(question, pad_kb=40):
    """固定结构 + 指定问题文本 + 固定填充。同一 question 得到逐字相同的 body。"""
    filler = ("the quick brown fox jumps over the lazy dog. " * 60) * (pad_kb // 3)
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "Here is context:\n" + filler},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": question},
    ]
    return P._apply_env_marker({
        "model": P.FREEMODEL,
        "messages": msgs,
        "stream": True,
        "tools": CLIENT_TOOLS,
    })


def one(port, sid, body):
    try:
        with P._LocalBridge(port) as proxy_url:
            status, head, retry, err = P._bun_probe(P._bun_bin(), proxy_url, sid, 25)
    except Exception as e:
        return "unreachable", 0, str(e)[:60]
    if err:
        return "unreachable", 0, err[:60]
    return P.classify(status, head), status, head.strip().replace("\n", " ")[:90]


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1080
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    sid0 = "ses_" + secrets.token_hex(12)
    print("port=%d egress=%s n=%d sid=%s\n" % (port, egress_now(port), n, sid0[:16]))

    same_q = "what is 2+2? answer in one word"
    same_body = body_with(same_q)
    print("body size=%d bytes, question=%r\n" % (len(json.dumps(same_body)), same_q))

    print("--- Phase1 REPEAT_SAME (same content, same session) ---")
    p1 = []
    for i in range(n):
        v, st, head = one(port, sid0, same_body)
        p1.append(v)
        print("  #%d %-9s status=%-4s %s" % (i + 1, v, st, head))
        time.sleep(1.0)

    print("\n--- Phase2 SAME_CONTENT / NEW_SESSION ---")
    p2 = []
    for i in range(n):
        v, st, head = one(port, "ses_" + secrets.token_hex(12), same_body)
        p2.append(v)
        print("  #%d %-9s status=%-4s %s" % (i + 1, v, st, head))
        time.sleep(1.0)

    print("\n--- Phase3 DIFF_CONTENT / SAME_SESSION ---")
    p3 = []
    for i in range(n):
        q = "random question %s: reply ok" % secrets.token_hex(6)
        v, st, head = one(port, sid0, body_with(q))
        p3.append(v)
        print("  #%d %-9s status=%-4s %s   q=%r" % (i + 1, v, st, head, q[:40]))
        time.sleep(1.0)

    print("\nSUMMARY egress=%s" % egress_now(port))
    print("  P1 same_content+same_sess : %s" % p1)
    print("  P2 same_content+new_sess  : %s" % p2)
    print("  P3 diff_content+same_sess : %s" % p3)


def egress_now(port):
    try:
        return P.egress_ip(port, 8)
    except Exception:
        return "?"


if __name__ == "__main__":
    main()
