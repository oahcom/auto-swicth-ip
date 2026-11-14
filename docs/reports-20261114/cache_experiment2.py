#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""背靠背缓存实验：相同请求连发、增长请求连发，隔离会话轮换干扰。"""
import json
import time
import urllib.request

URL = "http://127.0.0.1:20127/v1/chat/completions"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PREFIX = ("You are a test assistant used for cache probing. "
          + "STABLE-PREFIX-BLOCK-" * 240)
CTX = "Context notes for the probe. " + "PROBE-CTX-SEGMENT-" * 220


def send(tag, extra_msg=False):
    msgs = [
        {"role": "system", "content": PREFIX},
        {"role": "user", "content": "Question: name three fruits. " + CTX},
    ]
    if extra_msg:
        msgs.append({"role": "user", "content": "Follow-up: now name three vegetables."})
    body = {"model": "9router_hermes", "messages": msgs,
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


print("--- 相同请求背靠背 ---")
send("A1(identical)")
send("A2(identical)")
print("--- 增长请求背靠背 ---")
send("B1(grown)", extra_msg=True)
send("B2(grown)", extra_msg=True)
print("--- 再回相同请求 ---")
send("C1(identical)")
