#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""受控前缀缓存实验：字节相同请求二连发 + 会话增长第三发，看 usage.cached_tokens。"""
import json
import time
import urllib.request

URL = "http://127.0.0.1:20127/v1/chat/completions"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕过 7000 代理

PREFIX = ("You are a test assistant used for cache probing. "
          + "STABLE-PREFIX-BLOCK-" * 240)  # ~5.4KB 唯一前缀
CTX = "Context notes for the probe. " + "PROBE-CTX-SEGMENT-" * 220  # ~4.9KB


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
    dt = time.time() - t0
    print(f"{tag}: {dt:.1f}s prompt={u.get('prompt_tokens')} "
          f"cached={u.get('prompt_tokens_details', {}).get('cached_tokens', u.get('cached_tokens'))} "
          f"completion={u.get('completion_tokens')}")


send("req1(identical)")
time.sleep(25)
send("req2(identical)")
time.sleep(5)
send("req3(grown+1msg)", extra_msg=True)
