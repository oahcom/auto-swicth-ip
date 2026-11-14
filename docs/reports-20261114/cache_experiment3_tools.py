#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B：工具表稳定 vs 工具表变化，对前缀缓存的影响。两轮对话同历史+1条消息。"""
import json
import time
import urllib.request

URL = "http://127.0.0.1:20127/v1/chat/completions"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PREFIX = ("You are a test assistant used for cache probing. "
          + "STABLE-PREFIX-BLOCK-" * 240)
CTX = "Context notes for the probe. " + "PROBE-CTX-SEGMENT-" * 220

TOOLS_A = [
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Get weather for a city. " + "TOOLDESC-A " * 60,
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "get_time",
        "description": "Get current time. " + "TOOLDESC-B " * 60,
        "parameters": {"type": "object", "properties": {"tz": {"type": "string"}}}}},
]
TOOLS_B = TOOLS_A + [
    {"type": "function", "function": {
        "name": "calc_tip",
        "description": "Calculate tip. " + "TOOLDESC-C " * 60,
        "parameters": {"type": "object", "properties": {"amount": {"type": "number"}}}}},
]


def send(tag, tools, extra_msg=False):
    msgs = [
        {"role": "system", "content": PREFIX},
        {"role": "user", "content": "Question: name three fruits. " + CTX},
    ]
    if extra_msg:
        msgs.append({"role": "user", "content": "Follow-up: now name three vegetables."})
    body = {"model": "9router_hermes", "messages": msgs, "tools": tools,
            "stream": False, "max_tokens": 40}
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with OPENER.open(req, timeout=120) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(tag, "ERROR", e)
        return
    u = data.get("usage") or {}
    det = u.get("prompt_tokens_details") or {}
    cached = det.get("cached_tokens", u.get("cached_tokens"))
    print(f"{tag}: {time.time()-t0:.1f}s prompt={u.get('prompt_tokens')} cached={cached}")


print("--- A组：工具表稳定，轮2同历史+1消息 ---")
send("A-turn1(tools=2)", TOOLS_A)
send("A-turn2(tools=2,grown)", TOOLS_A, extra_msg=True)
print("--- B组：轮2工具表增加1个 ---")
send("B-turn1(tools=2)", TOOLS_A)
send("B-turn2(tools=3,grown)", TOOLS_B, extra_msg=True)
