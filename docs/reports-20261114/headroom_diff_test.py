#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""headroom 二连发 diff 实验：同一 payload 打 /v1/compress 两次，逐字节比输出。
目的：定案 headroom 压缩是否非确定性（同一输入两次不同输出 = 历史逐轮被改写 = 前缀缓存杀手）。"""
import json
import hashlib
import urllib.request

URL = "http://127.0.0.1:8787/v1/compress"

msgs = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "List files in the project."},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "Bash", "arguments": "{\"command\": \"ls -la\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content":
     "Stdout: total 128\ndrwxr-xr-x 5 admin admin 4096 Jan 1 10:00 src\n"
     "-rw-r--r-- 1 admin admin 1234 Jan 1 10:00 README.md\n"
     "-rw-r--r-- 1 admin admin 98765 Jan 1 10:00 big_log.txt\n"
     + "\n".join(f"-rw-r--r-- 1 admin admin {1000+i} Jan 1 10:0{i%10} file_{i}.log" for i in range(60))},
    {"role": "user", "content": "Summarize that."},
]


def call():
    body = json.dumps({"messages": msgs, "model": "gpt-4o"}).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as r:
        return json.loads(r.read())


r1 = call()
r2 = call()
m1, m2 = r1.get("messages", []), r2.get("messages", [])
print(f"消息数: {len(m1)} vs {len(m2)}")
print(f"tokens_saved: {r1.get('tokens_saved')} vs {r2.get('tokens_saved')}")
same = True
for i, (a, b) in enumerate(zip(m1, m2)):
    ja, jb = json.dumps(a, ensure_ascii=False, sort_keys=True), json.dumps(b, ensure_ascii=False, sort_keys=True)
    ha, hb = hashlib.sha256(ja.encode()).hexdigest()[:12], hashlib.sha256(jb.encode()).hexdigest()[:12]
    tag = "SAME" if ja == jb else "DIFF"
    if ja != jb:
        same = False
    print(f"  msg[{i}] {tag} sha={ha} vs {hb}")
    if ja != jb:
        ca, cb = (a.get("content") or "")[:100], (b.get("content") or "")[:100]
        print(f"    A: {ca!r}")
        print(f"    B: {cb!r}")
        # 首个差异字节位置
        for k, (x, y) in enumerate(zip(ja, jb)):
            if x != y:
                print(f"    首个差异 @byte {k}: ...{ja[max(0,k-20):k+20]!r} vs ...{jb[max(0,k-20):k+20]!r}")
                break
        else:
            print(f"    长度差异: {len(ja)} vs {len(jb)}")
print()
print("结论:", "输出完全一致（确定性）" if same else "输出不一致 → 非确定性 → 逐轮改写历史 → 前缀缓存必死")
