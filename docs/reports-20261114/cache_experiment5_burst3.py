#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三连突发判别：工具定义块在 prompt 里位于历史之前还是之后。
R1: [S][H][T2]  基线
R2: [S][H+尾][T2]  会话健康对照（应大命中）
R3: [S][H][T3]  工具+1，历史不变
判读：
- R3 命中 ≈ R1 全长 → 工具在历史【之后】（历史前缀保得住）
- R3 命中 ≈ R1 的 S+工具段 → 工具在历史【之前】（历史被工具块挤掉）
- R2/R3 均 ~0 → 三连内会话/轮换打断，重跑
三连间隔 <1s，尽量钉在同一上游会话。"""
import json
import time
import urllib.request

URL = "http://127.0.0.1:20127/v1/chat/completions"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

TAG = "BURST3-" + str(int(time.time()) % 100000)
SYS = f"You are a probe. {TAG}-SYS-" + "STABLE-PREFIX-BLOCK-" * 240
CTX = "Question: name three fruits. " + f"{TAG}-CTX-" * 220


def mk_tool(name, tag):
    return {"type": "function", "function": {
        "name": name,
        "description": f"Tool {name}. " + tag * 60,
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}


T2 = [mk_tool("get_weather", "DA-"), mk_tool("get_time", "DB-")]
T3 = T2 + [mk_tool("calc_tip", "DC-")]


def send(tag, tools, msgs):
    body = {"model": "9router_hermes", "messages": msgs, "tools": tools,
            "stream": False, "max_tokens": 30}
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with OPENER.open(req, timeout=150) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(tag, "ERROR", e)
        return 0
    u = data.get("usage") or {}
    det = u.get("prompt_tokens_details") or {}
    cached = det.get("cached_tokens", u.get("cached_tokens")) or 0
    print(f"{tag}: {time.time()-t0:.1f}s prompt={u.get('prompt_tokens')} cached={cached}")
    return cached


H1 = [{"role": "system", "content": SYS}, {"role": "user", "content": CTX}]
H2 = H1 + [{"role": "user", "content": f"{TAG} follow-up."}]

c1 = send("R1(base,T2,H)", T2, H1)
c2 = send("R2(ctrl,T2,H+tail)", T2, H2)
c3 = send("R3(T3,H)", T3, H1)
print(f"判读: R1={c1} R2={c2} R3={c3}")
if c2 and c3:
    if c3 > c1 * 0.8:
        print("=> 工具在历史【之后】（R3 接近 R1 全长）")
    elif c3 < c2 * 0.6:
        print("=> 工具在历史【之前】（R3 只保住 S+工具段，历史被挤掉）")
    else:
        print("=> 不确定，需重跑")
else:
    print("=> 会话打断（R2/R3 有 0），需重跑")
