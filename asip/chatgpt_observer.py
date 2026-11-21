#!/usr/bin/env python3
"""chatgpt_observer — 轻量观察器（防御性节奏，90s±抖动采样）。

采样四项：
  1. chatgpt-pool 当前节点（clash API）
  2. egress 17891 过 chatgpt.com/robots.txt 的状态码/延迟（CF/transport 面）
  3. egress 出口 IP（IP 轮换观察）
  4. 闸口 _gate/status 计数（429/rotates/short_circuits 增量）

输出 JSONL: /home/ubuntu/auto-swicth-ip/run/chatgpt-observe.jsonl
无依赖，纯 stdlib。杀进程即停（pid 写 run/chatgpt-observer.pid）。
"""
import json
import os
import random
import subprocess
import sys
import time

# 直跑修复（同 chatgpt_l7/opencode_l7）：`python3 asip/xxx.py` 时 sys.path[0]
# 是 asip/ 目录，其中 http.py 会遮蔽标准库 http 包，urllib 直接炸。
_ASIP_DIR = os.path.dirname(os.path.abspath(__file__))
if sys.path and os.path.abspath(sys.path[0]) == _ASIP_DIR:
    sys.path.pop(0)

import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

RUN = "/home/ubuntu/auto-swicth-ip/run"
OUT = os.path.join(RUN, "chatgpt-observe.jsonl")
PIDF = os.path.join(RUN, "chatgpt-observer.pid")
CLASH = "http://127.0.0.1:19090"
CLASH_SECRET = "zs-local-013ac66d"
EGRESS = "http://127.0.0.1:17891"
GATE = "http://127.0.0.1:17892"
INTERVAL = 90.0  # 防御性：不连珠，90s + 抖动

# 节点 → 出口指纹（内存 + 落盘，供后续换线排除 WARP 出口节点）
_NODE_EGRESS = {}
_FINGER_PATH = os.path.join(RUN, "node-egress-fingerprint.json")


def _fingerprint(ip, node):
    """查出口 IP 归属，标记 WARP。返回 dict 或 None（查询失败）。"""
    if not ip or ip == "FAIL":
        return None
    try:
        st, body = _get(
            f"http://ip-api.com/json/{ip}"
            "?fields=status,country,isp,org,as,hosting",
            timeout=6)
        d = json.loads(body)
    except Exception:
        return None
    if d.get("status") != "success":
        return None
    org = (d.get("org") or "") + " " + (d.get("isp") or "")
    asn = d.get("as") or ""
    warp = ("warp" in org.lower()) or ("cloudflare warp" in org.lower())
    fp = {"ip": ip, "country": d.get("country"), "as": asn,
          "org": d.get("org"), "warp": warp}
    if node:
        _NODE_EGRESS[node] = fp
        try:
            with open(_FINGER_PATH, "w", encoding="utf-8") as f:
                json.dump(_NODE_EGRESS, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    return fp


def _get(url, headers=None, timeout=8):
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:
        return 0, str(e)


def sample():
    now = time.time()
    row = {"ts": now, "wall": time.strftime("%Y-%m-%d %H:%M:%S")}

    # 1. pool 当前节点
    st, body = _get(CLASH + "/proxies/chatgpt-pool",
                    {"Authorization": "Bearer " + CLASH_SECRET})
    try:
        d = json.loads(body)
        row["pool_now"] = d.get("now")
    except Exception:
        row["pool_now"] = f"ERR:{st}"

    # 2. chatgpt 连通性（走 egress）
    r = subprocess.run(
        ["curl", "-s", "--max-time", "8", "--proxy", EGRESS,
         "-o", "/dev/null", "-w", "%{http_code} %{time_total}",
         "https://chatgpt.com/robots.txt"],
        capture_output=True, text=True)
    parts = (r.stdout or "0 0").split()
    row["cg_status"] = int(parts[0]) if parts[0].isdigit() else 0
    row["cg_sec"] = float(parts[1]) if len(parts) > 1 else 0.0

    # 3. 出口 IP + WARP 指纹。
    # 探针走 chatgpt.com/cdn-cgi/trace（chatgpt 域名 → chatgpt-pool，测的是
    # pool 真实出口）。不能用 api.ipify.org：非 chatgpt 域名走 final=pick
    # （默认 warp），测出来的是 warp 出口，与 pool 无关（实测踩过）。
    r2 = subprocess.run(
        ["curl", "-s", "--max-time", "5", "--proxy", EGRESS,
         "https://chatgpt.com/cdn-cgi/trace"],
        capture_output=True, text=True)
    trace = {}
    for line in (r2.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            trace[k.strip()] = v.strip()
    row["exit_ip"] = trace.get("ip") or "FAIL"
    row["colo"] = trace.get("colo")
    row["loc"] = trace.get("loc")
    row["egress_warp"] = _fingerprint(row["exit_ip"], row.get("pool_now"))

    # 4. 闸口计数
    st, body = _get(GATE + "/_gate/status", timeout=3)
    try:
        g = json.loads(body)
        s = g.get("stats", {})
        row["gate"] = {
            "req": s.get("requests", 0), "429": s.get("rate429", 0),
            "rot": s.get("rotates", 0), "sc": s.get("short_circuits", 0),
            "tf": s.get("transport_faults", 0),
            "node": g.get("current_node"),
        }
        row["bo"] = {
            "h": round(g.get("history_bo", {}).get("remaining", 0), 1),
            "c": round(g.get("chat_bo", {}).get("remaining", 0), 1),
        }
    except Exception:
        row["gate"] = f"ERR:{st}"

    return row


def main():
    global _NODE_EGRESS
    if os.path.exists(_FINGER_PATH):
        try:
            with open(_FINGER_PATH, encoding="utf-8") as f:
                _NODE_EGRESS = json.load(f)
        except Exception:
            pass
    with open(PIDF, "w") as f:
        f.write(str(os.getpid()))
    print(f"observer pid={os.getpid()} -> {OUT}", flush=True)
    while True:
        try:
            row = sample()
            with open(OUT, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            egr = row.get("egress_warp") or {}
            flag = "WARP!" if egr.get("warp") else egr.get("country", "?")
            print(f"{row['wall']} pool={str(row.get('pool_now'))[:32]} "
                  f"cg={row.get('cg_status')}/{row.get('cg_sec')}s "
                  f"ip={row.get('exit_ip')} [{flag}] "
                  f"gate={row.get('gate')}", flush=True)
        except Exception as e:
            print(f"sample ERR: {e}", flush=True)
        # 90s ± 15s 抖动（防御性，不规律敲打）
        time.sleep(INTERVAL + random.uniform(-15, 15))


if __name__ == "__main__":
    main()
