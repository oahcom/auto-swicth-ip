# -*- coding: utf-8 -*-
"""抓一次完整 429 响应,看错误体与限流头。"""
import json, subprocess, sys, time, urllib.request, urllib.error
import probe_warp as P

BASE = "singbox-config.json-slot3.json"
PORT = 7022
CFG = "/tmp/cap-raw.json"

NODES = sys.argv[1:]


def build(tag):
    d = json.load(open(BASE))
    d["inbounds"] = [{"type": "mixed", "tag": "in", "listen": "127.0.0.1", "listen_port": PORT}]
    d["outbounds"] = [o for o in d["outbounds"] if o.get("tag") not in ("cap-out", "cap-tri")]
    d["outbounds"].append({"type": "selector", "tag": "cap-out",
                           "outbounds": [tag], "default": tag})
    rules = [dict(r, outbound="cap-out") if r.get("outbound") == "opencode-out" else r
             for r in d["route"]["rules"]]
    d["route"]["rules"] = rules
    d["route"]["final"] = "cap-out"
    d["experimental"] = {"cache_file": {"enabled": False}}
    d["log"] = {"level": "error"}
    json.dump(d, open(CFG, "w"))


def raw_shot(sid):
    """用 curl 抓原始响应,不解析。"""
    body = json.dumps(P._body())
    hdrs = P._headers(sid)
    args = ["curl", "-sS", "-i", "--max-time", "45",
            "--proxy", "http://127.0.0.1:%d" % PORT,
            "-X", "POST", P.URL]
    for k, v in hdrs.items():
        args += ["-H", "%s: %s" % (k, v)]
    args += ["--data-binary", "@-"]
    r = subprocess.run(args, input=body.encode(), capture_output=True, timeout=60)
    return r.stdout.decode("utf-8", "replace")


for tag in NODES:
    print("=" * 70)
    print("NODE:", tag)
    build(tag)
    pr = subprocess.Popen(["./sing-box", "run", "-c", CFG],
                          stdout=open("/tmp/cap-raw.log", "w"), stderr=subprocess.STDOUT)
    time.sleep(4)
    try:
        for i in range(3):
            sid = P._session_id()
            out = raw_shot(sid)
            head, _, body = out.partition("\r\n\r\n")
            lines = [l for l in head.split("\r\n") if l.strip()]
            print("  [shot %d] session=%s" % (i + 1, sid))
            print("    " + lines[0] if lines else "    <no status line>")
            for l in lines[1:]:
                ln = l.lower()
                if any(k in ln for k in ("retry-after", "x-ratelimit", "x-codex",
                                         "cf-", "x-request-id", "openai-", "date:")):
                    print("    " + l)
            print("    BODY: " + body.strip()[:400].replace("\n", " "))
            time.sleep(1)
    finally:
        pr.terminate()
        try:
            pr.wait(timeout=10)
        except Exception:
            pr.kill()
        time.sleep(1)
