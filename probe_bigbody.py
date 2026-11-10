#!/usr/bin/env python3
"""大体量请求实测 — 同一条 WARP 隧道上，把小探针请求和真实量级请求各打一遍。

背景：asip 的模型层探针（probe_warp.py）发的是 3KB 的 "say ok"，判 OK 的线
在几秒后接真实业务请求时频繁 429，或 20 秒连响应头都不回（transport）。
要证伪/证实「体量决定死活」，必须让**同一条隧道、同一份指纹、同一个上游**
只变 messages 的体量，其余全部复刻探针。

与 probe_warp.py 一样走 Bun 的 HTTP 栈 + CONNECT→SOCKS5 本地桥：curl 的 TLS
指纹会被上游直接判 429（9router bfd5ad66 实测），用它测出来的不是同一条腿。

只读：不写探针会话表、不碰 asip 台账、不切线、不重启任何服务。
用法:
    python3 probe_bigbody.py --tags WARP-H2-HKG-10,WARP-H2-GIG-78
    python3 probe_bigbody.py --tags WARP-H2-HKG-10 --levels small,big
"""
import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import probe_warp as pw
from asip.proc import run as run_proc
from config import CFG

# (档位名, 历史消息条数, 每条填充字节数)。small 就是探针原样的那一份。
LEVELS = {
    "small": (1, 0),        # ~3KB
    "mid": (60, 400),       # ~27KB
    "big": (150, 1200),     # ~180KB：真实请求的中位长度
    "huge": (150, 3000),    # ~450KB：真实请求的上沿
}
_FILLER = ("The quick brown fox jumps over the lazy dog while the agent reads "
           "the repository and edits files. ") * 200


def make_body(n_msgs: int, fill: int) -> dict:
    messages = [{"role": "system", "content": "You are a coding assistant."}]
    chunk = _FILLER[:fill]
    for i in range(max(0, n_msgs - 1)):
        messages.append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"[turn {i}] " + chunk,
        })
    messages.append({"role": "user", "content": "say ok"})
    body = {
        "model": pw.FREEMODEL,
        "messages": messages,
        "stream": True,
        "tools": pw.FINGERPRINT_TOOLS,
    }
    return pw._apply_env_marker(body)


def send(bun: str, proxy_url: str, body: dict, sid: str,
         timeout: float) -> dict:
    """Bun 里发一发，返回 (status, head, retry, error, 秒数, body 字节数)。"""
    raw = json.dumps(body, ensure_ascii=False)
    payload = {
        "url": pw.URL,
        "method": "POST",
        "headers": pw._headers(sid),
        "body": raw,
        "proxy": proxy_url,
        "timeoutMs": int(timeout * 1000),
    }
    fd, path = tempfile.mkstemp(prefix="bigbody_job_", suffix=".json")
    t0 = time.time()
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        r = run_proc([bun, pw.BUN_SCRIPT, path], timeout=timeout + 8)
        sec = round(time.time() - t0, 1)
        if r.error or r.timed_out:
            return {"secs": sec, "bytes": len(raw.encode()),
                    "error": (r.error or "harness timeout")[:120]}
        lines = [x for x in (r.stdout or "").splitlines() if x.strip()]
        if not lines:
            return {"secs": sec, "bytes": len(raw.encode()), "error": "bun no output"}
        data = json.loads(lines[-1])
        if data.get("error"):
            return {"secs": sec, "bytes": len(raw.encode()),
                    "error": str(data["error"])[:120]}
        return {"secs": sec, "bytes": len(raw.encode()),
                "status": int(data["status"]),
                "head": str(data.get("head") or "")[:200],
                "retry": data.get("retry") or {}}
    except Exception as e:
        return {"secs": round(time.time() - t0, 1),
                "bytes": len(raw.encode()),
                "error": f"{type(e).__name__}: {e}"[:120]}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def verdict(res: dict) -> str:
    if res.get("error"):
        e = res["error"].lower()
        if "timeout" in e or "abort" in e or "timed out" in e:
            return "NO_HEADER"
        return "ERR"
    status = res.get("status")
    head = res.get("head") or ""
    if status == 429:
        if "FreeTierError" in head or "abusive traffic" in head:
            return "429_FLAGGED"
        if "FreeUsageLimitError" in head:
            return "429_QUOTA"
        return "429_OTHER"
    if status == 200:
        return "PASS" if head.startswith("data:") else "200_NO_DATA"
    return f"HTTP_{status}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tags", required=True, help="逗号分隔的隧道 tag")
    ap.add_argument("--levels", default="small,mid,big")
    ap.add_argument("--timeout", type=float, default=25)
    ap.add_argument("--gap", type=float, default=1.0, help="同线两发之间的间隔秒")
    ap.add_argument("--out", default="/tmp/bigbody_result.json")
    args = ap.parse_args()

    bun = pw._bun_bin()
    if not bun:
        print("✗ bun not found (set PROBE_BUN_BIN)", file=sys.stderr)
        return 2

    by_tag = {m["tag"]: m["port"] for m in CFG["warp"]["members"]}
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    levels = [x.strip() for x in args.levels.split(",") if x.strip()]
    for lv in levels:
        if lv not in LEVELS:
            print(f"✗ 未知档位 {lv}，可选 {list(LEVELS)}", file=sys.stderr)
            return 2

    rows = []
    print(f"{'隧道':<18}{'出口':<26}{'档位':<7}{'字节':>8}  {'判定':<14}{'耗时':>7}  响应头")
    print("-" * 118)
    for tag in tags:
        port = by_tag.get(tag)
        if port is None:
            print(f"{tag:<18}{'config 里没有这条线':<26}")
            continue
        egress = pw.egress_ip(port)
        try:
            bridge = pw._LocalBridge(port)
            proxy_url = bridge.__enter__()
        except Exception as e:
            print(f"{tag:<18}{egress:<26}{'桥起不来: ' + str(e)[:40]}")
            continue
        try:
            for lv in levels:
                n, fill = LEVELS[lv]
                body = make_body(n, fill)
                # 每档一个全新会话：体量是唯一变量，也避免小请求先把这个
                # session 的配额吃掉、让后面的大请求被动 429。
                sid = pw._session_id()
                res = send(bun, proxy_url, body, sid, args.timeout)
                v = verdict(res)
                rows.append({"tag": tag, "port": port, "egress": egress,
                             "level": lv, "verdict": v, **res})
                print(f"{tag:<18}{egress:<26}{lv:<7}{res['bytes']:>8}  "
                      f"{v:<14}{res['secs']:>6.1f}s  {(res.get('head') or res.get('error') or '')[:56]}")
                time.sleep(args.gap)
        finally:
            bridge.__exit__(None, None, None)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "levels": levels, "rows": rows}, f,
                  ensure_ascii=False, indent=1)

    print()
    for tag in tags:
        got = [r for r in rows if r["tag"] == tag]
        if not got:
            continue
        compact = "  →  ".join(f"{r['level']}:{r['verdict']}" for r in got)
        small = next((r for r in got if r["level"] == "small"), None)
        big = next((r for r in got if r["level"] != "small" and r["verdict"] != "PASS"), None)
        flag = "  ★小过/大挂" if small and small["verdict"] == "PASS" and big else ""
        print(f"{tag:<18}{compact}{flag}")
    print(f"\n明细已写 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
