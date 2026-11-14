#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""headroom 增长实验：压缩 [m1..m4] 与 [m1..m4, m5]，对比 m1..m4 的压缩结果是否被改写。
若旧消息压缩结果随上下文增长而变化 → 每轮历史被重写 → 前缀缓存必死（需要 per-message 记忆化）。"""
import json
import hashlib
import urllib.request

URL = "http://127.0.0.1:8787/v1/compress"

tool_dump = (
    "Stdout: total 128\ndrwxr-xr-x 5 admin admin 4096 Jan 1 10:00 src\n"
    "-rw-r--r-- 1 admin admin 1234 Jan 1 10:00 README.md\n"
    + "\n".join(f"-rw-r--r-- 1 admin admin {1000+i} Jan 1 10:0{i%10} file_{i}.log" for i in range(80))
    + "\n" + "DEBUG line " + "x" * 2000
)

base = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "List files."},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "Bash", "arguments": "{\"command\": \"ls\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": tool_dump},
]
extra = {"role": "user", "content": "Now summarize the README."}


def call(msgs):
    body = json.dumps({"messages": msgs, "model": "gpt-4o"}).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=60) as r:
        return json.loads(r.read()).get("messages", [])


m_before = call(base)               # 压缩 4 条
m_after = call(base + [extra])      # 压缩 5 条（前 4 条内容一字未变）

print(f"消息数: {len(m_before)} vs {len(m_after)}")
changed = 0
for i in range(min(len(m_before), 4)):
    ja = json.dumps(m_before[i], ensure_ascii=False, sort_keys=True)
    jb = json.dumps(m_after[i], ensure_ascii=False, sort_keys=True)
    tag = "SAME" if ja == jb else "DIFF"
    if ja != jb:
        changed += 1
    la, lb = len(str(m_before[i].get("content") or "")), len(str(m_after[i].get("content") or ""))
    print(f"  msg[{i}] {tag}  长度 {la} vs {lb}")
    if ja != jb:
        ca, cb = str(m_before[i].get("content") or ""), str(m_after[i].get("content") or "")
        for k, (x, y) in enumerate(zip(ca, cb)):
            if x != y:
                print(f"    首个差异 @char {k}: {ca[max(0,k-30):k+30]!r}")
                print(f"               vs {cb[max(0,k-30):k+30]!r}")
                break
        else:
            print(f"    尾部差异: A 尾 {ca[-60:]!r} / B 尾 {cb[-60:]!r}")
print()
print("结论:", "旧消息压缩结果稳定（无逐轮改写）" if changed == 0
      else f"旧消息被改写 {changed}/4 → 上下文增长触发历史重写 → 前缀缓存必死")
