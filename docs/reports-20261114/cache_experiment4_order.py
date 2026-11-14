#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具数组顺序对上游前缀缓存的影响：
C组  同集合同顺序（对照，应命中）
D组  同集合顺序反转 —— 判上游是否按数组顺序渲染
E组  新工具插到最前 vs F组 新工具追加到末尾 —— 判插入位置的收益
每对用独立 system/历史文本，防止跨对互相暖缓存污染。"""
import json
import time
import urllib.request

URL = "http://127.0.0.1:20127/v1/chat/completions"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def mk_tool(name, desc_tag):
    return {"type": "function", "function": {
        "name": name,
        "description": f"Tool {name}. " + desc_tag * 60,
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}


def send(tag, sys_tag, tools, base_msg, extra_msg):
    msgs = [
        {"role": "system", "content": f"You are a probe assistant. {sys_tag}-SYS-" + "STABLE-BLOCK-" * 240},
        {"role": "user", "content": f"{base_msg} " + f"{sys_tag}-CTX-" * 220},
    ]
    if extra_msg:
        msgs.append({"role": "user", "content": f"{sys_tag} follow-up: name three vegetables."})
    body = {"model": "9router_hermes", "messages": msgs, "tools": tools,
            "stream": False, "max_tokens": 40}
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with OPENER.open(req, timeout=150) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(tag, "ERROR", e)
        return
    u = data.get("usage") or {}
    det = u.get("prompt_tokens_details") or {}
    cached = det.get("cached_tokens", u.get("cached_tokens"))
    print(f"{tag}: {time.time()-t0:.1f}s prompt={u.get('prompt_tokens')} cached={cached}")


T = [mk_tool("get_weather", "DA-"), mk_tool("get_time", "DB-"),
     mk_tool("calc_tip", "DC-"), mk_tool("find_hotel", "DD-"),
     mk_tool("translate", "DE-")]
NEW = mk_tool("new_tool", "DX-")

print("--- C组：同集合同顺序（对照） ---")
send("C1", "PROBE-C", T, "Question: name three fruits.", False)
send("C2(same order)", "PROBE-C", T, "Question: name three fruits.", True)

print("--- D组：同集合顺序反转 ---")
send("D1", "PROBE-D", T, "Question: name three fruits.", False)
send("D2(reversed)", "PROBE-D", list(reversed(T)), "Question: name three fruits.", True)

print("--- E组：新工具插到最前 ---")
two = T[:2]
send("E1", "PROBE-E", two, "Question: name three fruits.", False)
send("E2(new at FRONT)", "PROBE-E", [NEW] + two, "Question: name three fruits.", True)

print("--- F组：新工具追加到末尾 ---")
send("F1", "PROBE-F", two, "Question: name three fruits.", False)
send("F2(new at END)", "PROBE-F", two + [NEW], "Question: name three fruits.", True)
