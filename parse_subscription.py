#!/usr/bin/env python3
"""
解析 okz 订阅，生成 sing-box 配置
"""
import base64
import json
import urllib.parse
import sys
import os
from pathlib import Path

SUBSCRIPTION_URL = os.environ.get("OKZ_SUB_URL")
if not SUBSCRIPTION_URL:
    raise RuntimeError("OKZ_SUB_URL env var not set — required for subscription fetch")

# ============= 解析订阅 =============
def fetch_and_decode_sub(url: str) -> list[str]:
    """拉订阅，base64 解码，返回原始 URL 列表"""
    import subprocess
    r = subprocess.run(['curl', '-sf', url], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"failed to fetch: {r.stderr}")
    raw = r.stdout
    # 尝试 base64 解码（去掉空白）
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode('utf-8', errors='replace')
        return [l.strip() for l in decoded.splitlines() if l.strip()]
    except Exception:
        # 已经是明文
        return [l.strip() for l in raw.splitlines() if l.strip()]

# ============= 节点解析 =============
def _tag_for(u, scheme: str) -> str:
    """tag 用 host:port-scheme 防止重复（订阅里多个不同名字同节点）"""
    return f"{u.hostname}:{u.port}-{scheme}"

def parse_hysteria2(url: str) -> dict:
    u = urllib.parse.urlparse(url)
    p = urllib.parse.parse_qs(u.query)
    name = urllib.parse.unquote(u.fragment) if u.fragment else u.hostname
    insecure = p.get("insecure", ["0"])[0] == "1"
    return {
        "type": "hysteria2",
        "tag": _tag_for(u, "h2"),
        "server": u.hostname,
        "server_port": u.port,
        "password": urllib.parse.unquote(u.username) if u.username else "",
        "tls": {
            "enabled": True,
            "server_name": p.get("sni", [u.hostname])[0] or u.hostname,
            "insecure": insecure,
            "utls": {"enabled": True, "fingerprint": "chrome"},
        },
        "obfs": ({"type": p["obfs"][0], "password": urllib.parse.unquote(p["obfs-password"][0])}
                 if "obfs" in p else None),
    }

def parse_anytls(url: str) -> dict:
    u = urllib.parse.urlparse(url)
    p = urllib.parse.parse_qs(u.query)
    insecure = p.get("insecure", ["0"])[0] == "1"
    return {
        "type": "anytls",
        "tag": _tag_for(u, "any"),
        "server": u.hostname,
        "server_port": u.port,
        "password": urllib.parse.unquote(u.username) if u.username else "",
        "tls": {
            "enabled": True,
            "server_name": p.get("sni", [u.hostname])[0] or u.hostname,
            "insecure": insecure,
            "utls": {"enabled": True, "fingerprint": "chrome"},
        },
    }

def parse_trojan(url: str) -> dict:
    u = urllib.parse.urlparse(url)
    p = urllib.parse.parse_qs(u.query)
    insecure = p.get("allowInsecure", ["0"])[0] == "1"
    return {
        "type": "trojan",
        "tag": _tag_for(u, "tr"),
        "server": u.hostname,
        "server_port": u.port,
        "password": urllib.parse.unquote(u.username) if u.username else "",
        "tls": {
            "enabled": True,
            "server_name": p.get("sni", [u.hostname])[0] or u.hostname,
            "insecure": insecure,
            "utls": {"enabled": True, "fingerprint": "chrome"},
        },
    }

PARSERS = {
    "hysteria2": parse_hysteria2,
    "anytls": parse_anytls,
    "trojan": parse_trojan,
}

def parse_url(url: str) -> dict | None:
    scheme = url.split("://", 1)[0].lower()
    parser = PARSERS.get(scheme)
    if not parser:
        return None
    try:
        return parser(url)
    except Exception as e:
        print(f"failed parse: {url[:80]}... err={e}", file=sys.stderr)
        return None

# ============= 生成 sing-box config =============
def make_config(nodes: list[dict], listen_port: int = 7890,
                clash_port: int = 9090, clash_secret: str = "auto-switch-2026",
                include_h2: bool = False) -> dict:
    """生成 sing-box 配置：mixed port + selector + clash API

    include_h2: 是否包含 hysteria2 节点（WSL2 下 utls 不兼容，默认排除）
    """
    if not include_h2:
        nodes = [n for n in nodes if n.get("type") != "hysteria2"]
    outbounds = nodes[:]
    outbounds.append({"type": "direct", "tag": "direct"})

    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "0.0.0.0",
                "listen_port": listen_port,
            },
        ],
        "outbounds": outbounds + [
            {
                "type": "selector",
                "tag": "proxy",
                "outbounds": [n["tag"] for n in nodes] + ["direct"],
                "default": nodes[0]["tag"] if nodes else "direct",
                "interrupt_exist_connections": False,
            },
        ],
        "route": {
            "rules": [
                {"action": "sniff"},
            ],
            "final": "proxy",
            "auto_detect_interface": True,
        },
        "experimental": {
            "clash_api": {
                "default_mode": "rule",
                "external_controller": f"127.0.0.1:{clash_port}",
                "secret": clash_secret,
            },
            "cache_file": {"enabled": False},
        },
    }

# ============= main =============
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=SUBSCRIPTION_URL)
    ap.add_argument("--out", default="/home/administrator/dkk-projects/auto-switch-ip/singbox-config.json")
    ap.add_argument("--port", type=int, default=7890)
    ap.add_argument("--clash-port", type=int, default=9090)
    ap.add_argument("--secret", default=os.environ.get("SINGBOX_SECRET", "auto-switch-2026"))
    ap.add_argument("--include-h2", action="store_true", help="include hysteria2 (default: skip, WSL2 utls incompatible)")
    ap.add_argument("--filter", default="", help="substring filter node name (case-insensitive)")
    args = ap.parse_args()

    print(f"fetching subscription from {args.url[:60]}...")
    urls = fetch_and_decode_sub(args.url)
    print(f"got {len(urls)} raw URLs")

    nodes = []
    seen_tags: set[str] = set()
    for u in urls:
        n = parse_url(u)
        if n and n["tag"] not in seen_tags:
            seen_tags.add(n["tag"])
            nodes.append(n)

    print(f"parsed {len(nodes)} usable nodes")
    if not nodes:
        print("ERROR: no nodes parsed, aborting")
        sys.exit(1)

    cfg = make_config(nodes, args.port, args.clash_port, args.secret, include_h2=args.include_h2)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    out_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    os.chmod(out_path, 0o600)  # 0600: 防本地其他用户读节点密码
    print(f"wrote config to {args.out}")
    print(f"first 3 nodes:")
    for n in nodes[:3]:
        print(f"  - {n['tag']:50s} {n['type']:10s} {n['server']}:{n['server_port']}")
    print(f"total usable nodes: {len(nodes)}")
    print(f"clash API: 127.0.0.1:{args.clash_port} secret={args.secret}")
    print(f"mixed port: 0.0.0.0:{args.port}")
