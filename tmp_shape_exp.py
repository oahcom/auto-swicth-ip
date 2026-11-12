#!/usr/bin/env python3
"""临时实验：同一出口，只改 body 形状，看上游判 OK 还是 quota。

变体：
  A probe      probe_warp 的原始 body（say ok + read/shell + env marker）
  B prod-small 生产形状：长 system + 多轮 messages + 客户端工具（Bash/Read/Write/Edit），总 ~3KB
  C prod-big   生产形状 + 填充到 ~500KB（模拟真实大 body）

用法: python3 tmp_shape_exp.py <socks_port>
"""
import json
import sys
import time

sys.path.insert(0, "/home/administrator/dkk-projects/auto-switch-ip")
import probe_warp as P

CLIENT_TOOLS = [
    {"type": "function", "function": {"name": "Bash", "description": "Run bash command",
      "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "Read", "description": "Read a file",
      "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}},
    {"type": "function", "function": {"name": "Write", "description": "Write a file",
      "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}}},
    {"type": "function", "function": {"name": "Edit", "description": "Edit a file",
      "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}},
]

SYSTEM = ("This conversation is powered by 9router_hermes\n\n"
          "Your main goal is to follow the USER's instructions at each message, denoted by the "
          "<user_query> tag.\n\n"
          "Here's what you're good at — and you should use all of it:\n\n"
          "- **Research & writing.** Dig into topics, verify facts, produce reports or documents.\n"
          "- **Data & analysis.** Crunch numbers, spot patterns, build visualizations.\n"
          "- **Building things.** Websites, apps, tools — if it needs to exist, you can make it.\n"
          "- **System access.** You have the local filesystem and the internet at your disposal.\n"
          "- **Everything in between.** If it's a real task a capable person could do at a computer, "
          "you can probably do it.\n\n"
          "Respond like terse caveman. All technical substance stay exact, only fluff die.")


def variant_A():
    return P._body()


def variant_B(pad_kb=0):
    msgs = [{"role": "user", "content": "list files in /etc"}]
    for i in range(3):
        msgs.append({"role": "assistant", "content": "ok %d" % i})
        msgs.append({"role": "user", "content": "continue %d" % i})
    if pad_kb:
        filler = "x" * (pad_kb * 1024)
        msgs.append({"role": "user", "content": "here is the file:\n" + filler})
    body = {
        "model": P.FREEMODEL,
        "messages": [{"role": "system", "content": SYSTEM}, *msgs],
        "stream": True,
        "tools": CLIENT_TOOLS,
    }
    return P._apply_env_marker(body)


def run(port, label, body):
    size = len(json.dumps(body))
    with P._LocalBridge(port) as proxy_url:
        sid = P._session_for(port)
        t0 = time.time()
        status, head, retry, err = P._bun_probe(P._bun_bin(), proxy_url, sid, 20)
        sec = round(time.time() - t0, 1)
        verdict = P.classify(status, head) if err is None else "unreachable"
        print("%-12s size=%-7d status=%-5s verdict=%-9s sec=%-5s %s"
              % (label, size, status, verdict, sec,
                 (err or head.strip().replace("\n", " "))[:120]))
        return verdict


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1080
    print("port=%d  egress=%s" % (port, P.egress_ip(port)))
    vA = run(port, "A_probe", variant_A())
    time.sleep(1.0)
    vB = run(port, "B_prod_small", variant_B(0))
    time.sleep(1.0)
    vC = run(port, "C_prod_big", variant_B(500))
    print("\nSUMMARY same-egress: A=%s B=%s C=%s" % (vA, vB, vC))
