#!/usr/bin/env python3
"""备线可服务性探针 — 经目标隧道直连 opencode 免费层，判这条线此刻能不能用。

用法:
    python3 probe_warp.py                    # 探 config 里全部成员
    python3 probe_warp.py --tag WARP-CF-C    # 只探一条
    python3 probe_warp.py --repeat 3         # 连探三次（判 429 是真限流还是抖动）
    python3 probe_warp.py --check-fingerprint  # 校验指纹定义仍与 9router 源码一致

只读探测：每条隧道一次 POST，不切线、不碰生产状态文件。退出码非 0 表示有隧道不可用。

背景：主动轮换此前是盲切（config.warp 注释），因为直连上游会被 403
FreeTierError 拒掉。实测定位到唯一门控项是请求体里 `read`/`shell` 两个工具
的逐字定义（消融：含工具 200 / 去工具 403，system 提示无关）。定义抄自
9router 的 open-sse/utils/opencodeFingerprint.js，此处冻结为常量。
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time

from config import CFG

# 9router open-sse/executors/opencode.js — 逐字复刻，改一个字就 403
UA = "opencode/latest/2.0.26/cli"
FREEMODEL = "deepseek-v4.1-flash-free"
URL = "https://opencode.ai/inference/v1/chat/completions"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_MASK48 = (1 << 48) - 1

# open-sse/utils/opencodeFingerprint.js — 上游逐字比对 description 与 parameters，
# 不得改写或重排。只有 CLI 升级才变，且会先打挂 9router 自身。
FINGERPRINT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read the contents of a file or directory. Supports text files, images, and PDFs. Images and PDFs are presented directly to the model. Each text line is prefixed by its 1-based line number as <line>: <content>. The prefix is for reference and is not part of the file content. Directory entries are returned one per line. Use offset and limit to read large files or directories in sections. Prefer one larger read over many small slices, and use grep to find specific content in large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory to read"},
                    "offset": {"type": "integer", "minimum": 0,
                               "description": "The line or directory entry to start reading from (1-based)"},
                    "limit": {"type": "integer", "minimum": 0,
                              "description": "The maximum number of lines or directory entries to read (defaults to and capped at 2000)"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute a shell command and return its output. Commands run on Linux using bash. Quote file paths containing spaces or special characters. Prefer dedicated tools over shell commands when possible. When output is large, the full result is saved to a file and a truncated preview is returned. Rely on automatic truncation unless filtering the output is more useful. Commands accept an optional timeout, background commands have no timeout by default. Background commands return immediately, and you will be notified when they complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command string to execute"},
                    "workdir": {"type": "string",
                                "description": "Working directory to execute the command in. Defaults to the current working directory. When possible, avoid changing directories in the command and set the working directory here instead."},
                    "timeout": {"type": "integer", "minimum": 0,
                                "description": "Timeout in milliseconds. Set to 0 to disable the timeout. Defaults to 120000 for foreground commands. Background commands have no timeout by default."},
                    "background": {"type": "boolean",
                                   "description": "Run the command in the background and return immediately (useful for dev servers and long-running builds). You do not need to use '&' at the end of the command when using this parameter. You will be notified when it completes. DO NOT poll for completion."},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
]


def _session_id() -> str:
    """上游解码前 12 位 hex 并拒绝过大的 counter（实测 0..16 通过，>=100 拒），必须是 1。"""
    prefix = ~(int(time.time() * 1000) * 0x1000 + 1) & _MASK48
    return "ses_" + format(prefix, "012x") + "".join(secrets.choice(_B62) for _ in range(14))


def _headers(sid: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer public",
        "User-Agent": UA,
        "x-opencode-client": "cli",
        "x-opencode-session": sid,
        "x-opencode-session-id": sid,
        "x-session-affinity": sid,
        "x-session-id": sid,
        "x-opencode-project": "global",
        "Accept": "text/event-stream",
    }


def _body() -> dict:
    return {
        "model": FREEMODEL,
        "messages": [{"role": "user", "content": "say ok"}],
        "stream": True,
        "tools": FINGERPRINT_TOOLS,
    }


# 走 SOCKS 必须用 curl，不能用 requests：daemon 跑在 systemd 的 env python3
# （/usr/bin/python3.12），该解释器没有 PySocks，requests 遇 socks5h:// 会立刻
# 抛 InvalidSchema（实测耗时 0.0s，根本没发包），100% 被判 unreachable。
# curl --socks5-hostname 是 daemon.warp_egress_ip 同款通道。

def _socks(port: int) -> list[str]:
    return ["--socks5-hostname", f"127.0.0.1:{port}"]


def egress_ip(port: int, timeout: float = 10.0) -> str:
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout), *_socks(port), TRACE_URL],
            capture_output=True, text=True, timeout=timeout + 5)
    except Exception:
        return "?"
    for line in r.stdout.splitlines():
        if line.startswith("ip="):
            return line[3:].strip()
    return "?"


def classify(status: int | None, head: str) -> str:
    if "abusive traffic" in head or "FreeTierError" in head:
        return "flagged"     # 出口 IP 信誉被拉黑 → 换线有效
    if "FreeUsageLimitError" in head:
        return "quota"       # 出口 IP 配额用尽 → 换线有效
    # 200 但没读到 data: ——本质是可用线，只是首个 chunk 比 read 窗口慢
    if status == 200 or head.startswith("data:"):
        return "OK"
    return "err"


def probe(port: int, timeout: float) -> tuple[str, float, str]:
    t0 = time.time()
    fd, path = tempfile.mkstemp(prefix="probe_warp_")
    os.close(fd)
    try:
        hdr: list[str] = []
        for k, v in _headers(_session_id()).items():
            hdr += ["-H", f"{k}: {v}"]
        r = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout), *_socks(port),
             "-X", "POST", URL, *hdr,
             "--data-binary", json.dumps(_body()),
             "-o", path, "-w", "%{http_code}"],
            capture_output=True, timeout=timeout + 5)
        status = int(r.stdout.decode("ascii", "replace") or 0) or None
        if r.returncode != 0 or status is None:
            # curl 代理握手/连不上（rc=97 等）→ 隧道真不可用，非 429
            return "unreachable", round(time.time() - t0, 1), f"curl rc={r.returncode}"
        with open(path, "rb") as f:
            head = f.read(256).decode("utf-8", "replace")
        return classify(status, head), round(time.time() - t0, 1), head.strip()[:100]
    except Exception as e:
        return "unreachable", round(time.time() - t0, 1), f"{type(e).__name__}: {e}"[:100]
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def check_fingerprint() -> int:
    """冻结的定义是否仍与 9router 源码逐字一致。源码升级后这里会先红。"""
    import os
    import re
    src = "/home/administrator/.9router/open-sse/utils/opencodeFingerprint.js"
    if not os.path.exists(src):
        print(f"✗ 未找到 9router 源码: {src}")
        return 1
    text = open(src, encoding="utf-8").read()
    # 上游比对的是 description 与 parameters 两者（opencodeFingerprint.js 的
    # isFingerprintDefinition）；只比 description 会漏掉 schema 变更 —— 那种
    # 情况下线上探测全 403，而这里仍报「一致」。JS 对象字面量与 json.dumps
    # 只差空白和键的引号，去掉这两样即可逐字比对。
    def _norm(s: str) -> str:
        # 连同逗号一起去掉：JS 对象字面量的尾逗号在 Python 序列化里不存在，
        # 而两边同时去逗号是等价的，不必去追哪几个是尾逗号。
        return re.sub(r"[\s,\"]+", "", s)

    flat = _norm(text)
    missing = [t["function"]["name"] for t in FINGERPRINT_TOOLS
               if _norm(t["function"]["description"]) not in flat
               or _norm(json.dumps(t["function"]["parameters"],
                                   ensure_ascii=False)) not in flat]
    if missing:
        print(f"✗ 以下工具定义与 9router 源码不一致，需重新抄录: {missing}")
        return 1
    print(f"✓ {len(FINGERPRINT_TOOLS)} 个工具定义与 9router 源码逐字一致")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", help="只探指定隧道，如 WARP-CF-C")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=25)
    ap.add_argument("--check-fingerprint", action="store_true")
    args = ap.parse_args()

    if args.check_fingerprint:
        return check_fingerprint()

    members = CFG["warp"]["members"]
    if args.tag:
        members = [m for m in members if m["tag"] == args.tag]
        if not members:
            print(f"✗ config 里没有 {args.tag}")
            return 2

    bad = 0
    print(f"{'隧道':<12}{'出口IP':<28}{'判定':<13}{'耗时':<8}")
    for m in members:
        ip = egress_ip(m["port"])
        verdicts = []
        for i in range(args.repeat):
            verdict, sec, _ = probe(m["port"], args.timeout)
            verdicts.append(verdict)
            print(f"{m['tag']:<12}{ip:<28}{verdict:<13}{sec}s")
            if verdict != "OK" and i < args.repeat - 1:
                time.sleep(1)
        if any(v != "OK" for v in verdicts):
            bad += 1
    print(f"\n{len(members) - bad}/{len(members)} 条可用")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
