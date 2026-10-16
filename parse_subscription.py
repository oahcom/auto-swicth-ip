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

from config import CFG, get_singbox_secret

_OKZ_SUB_URL = os.environ.get("OKZ_SUB_URL")

# ============= 解析订阅 =============
def fetch_and_decode_sub(url: str) -> list[str]:
    """拉订阅，base64 解码，返回原始 URL 列表
    支持：标准 base64 订阅、明文 URL 列表、Clash YAML 配置（提取 proxy 节点）
    """
    import requests
    import yaml
    # 仅对 dasho 等需要 UA 的源添加 headers
    headers = {"User-Agent": "clash-verge/1.0"} if "dasho" in url else {}
    r = requests.get(url, timeout=30, headers=headers)
    r.raise_for_status()
    raw = r.text

    all_lines = []

    # 1. 尝试 base64 解码（标准订阅格式）
    try:
        stripped = raw.strip().replace(" ", "").rstrip("=")
        decoded = base64.b64decode(stripped + "=" * (-len(stripped) % 4)).decode('utf-8', errors='replace')
        lines = [l.strip() for l in decoded.splitlines() if l.strip() and "://" in l]
        all_lines.extend(lines)
    except Exception:
        pass

    # 2. 尝试解析为 Clash YAML（部分订阅直接返回 Clash 配置）
    try:
        cfg = yaml.safe_load(raw)
        if isinstance(cfg, dict) and "proxies" in cfg:
            for p in cfg["proxies"]:
                u = _clash_proxy_to_url(p)
                if u:
                    all_lines.append(u)
    except Exception:
        pass

    # 3. 明文 URL 列表（每行一个 URL）
    if not all_lines:
        all_lines = [l.strip() for l in raw.splitlines() if l.strip() and "://" in l]

    return all_lines

def _clash_proxy_to_url(proxy: dict) -> str | None:
    """将 Clash proxy 字典转为标准 URL 格式（简化版，仅支持常见字段）"""
    ptype = proxy.get("type", "").lower()
    name = proxy.get("name", "").replace(" ", "_")
    server = proxy.get("server", "")
    port = proxy.get("port", 0)
    if not server or not port:
        return None

    if ptype == "vless":
        uuid = proxy.get("uuid", "")
        tls = proxy.get("tls", False)
        network = proxy.get("network", "tcp")
        flow = proxy.get("flow", "")
        # Clash 使用 servername 而非 sni
        sni = proxy.get("servername") or proxy.get("sni")
        fp = proxy.get("client-fingerprint", "chrome")
        reality = proxy.get("reality-opts", {})
        pbk = reality.get("public-key", "")
        sid = reality.get("short-id", "")
        transport = proxy.get("ws-opts", {}) if network == "ws" else {}
        path = transport.get("path", "/")
        host = transport.get("headers", {}).get("Host", "")

        # 构建 query
        q = f"type={network}&security={'tls' if tls else 'none'}"
        if flow:
            q += f"&flow={flow}"
        if sni:  # 只有显式设置了 sni/servername 才添加
            q += f"&sni={urllib.parse.quote(sni)}"
        if fp:
            q += f"&fp={fp}"
        if pbk:
            q += f"&pbk={urllib.parse.quote(pbk)}"
        if sid:
            q += f"&sid={urllib.parse.quote(sid)}"
        if network == "ws":
            q += f"&host={urllib.parse.quote(host)}&path={urllib.parse.quote(path)}"

        return f"vless://{uuid}@{server}:{port}?{q}#{urllib.parse.quote(name)}"

    elif ptype == "trojan":
        password = proxy.get("password", "")
        tls = proxy.get("tls", True)
        sni = proxy.get("sni", server)
        q = f"security={'tls' if tls else 'none'}"
        if sni and sni != server:
            q += f"&sni={urllib.parse.quote(sni)}"
        return f"trojan://{password}@{server}:{port}?{q}#{urllib.parse.quote(name)}"

    elif ptype == "vmess":
        uuid = proxy.get("uuid", "")
        alter_id = proxy.get("alterId", 0)
        security = proxy.get("cipher", "auto")
        network = proxy.get("network", "tcp")
        tls = proxy.get("tls", False)
        sni = proxy.get("sni", server)
        fp = proxy.get("client-fingerprint", "chrome")
        ws_opts = proxy.get("ws-opts", {}) if network == "ws" else {}
        path = ws_opts.get("path", "/")
        host = ws_opts.get("headers", {}).get("Host", "")

        q = f"alterId={alter_id}&security={security}"
        if network != "tcp":
            q += f"&type={network}"
        if tls:
            q += "&tls=1"
        if sni and sni != server:
            q += f"&sni={urllib.parse.quote(sni)}"
        if fp:
            q += f"&fp={fp}"
        if network == "ws":
            q += f"&host={urllib.parse.quote(host)}&path={urllib.parse.quote(path)}"

        return f"vmess://{uuid}@{server}:{port}?{q}#{urllib.parse.quote(name)}"

    elif ptype == "shadowsocks" or ptype == "ss":
        password = proxy.get("password", "")
        method = proxy.get("cipher", "aes-256-gcm")
        plugin = proxy.get("plugin", "")
        plugin_opts = proxy.get("plugin-opts", {})

        # Shadowsocks URL format: ss://method:password@server:port?plugin=...#name
        userinfo = base64.b64encode(f"{method}:{password}".encode()).decode().rstrip("=")
        q = ""
        if plugin:
            q += f"plugin={urllib.parse.quote(plugin)}"
            for k, v in plugin_opts.items():
                q += f";{k}={urllib.parse.quote(str(v))}"
        if q:
            return f"ss://{userinfo}@{server}:{port}?{q}#{urllib.parse.quote(name)}"
        else:
            return f"ss://{userinfo}@{server}:{port}#{urllib.parse.quote(name)}"

    return None

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
        "obfs": ({"type": p["obfs"][0], "password": urllib.parse.unquote(p.get("obfs-password", [""])[0])}
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
    sni = p.get("sni", [u.hostname])[0] or u.hostname
    out = {
        "type": "trojan",
        "tag": _tag_for(u, "tr"),
        "server": u.hostname,
        "server_port": u.port,
        "password": urllib.parse.unquote(u.username) if u.username else "",
        "tls": {
            "enabled": True,
            "server_name": sni,
            "insecure": insecure,
        },
    }
    # sing-box 只接受 ws/grpc/http/quic 等 transport，显式 tcp 是非法值（会导致启动 FATAL）
    network = p.get("type", [""])[0]
    if network == "ws":
        out["transport"] = {
            "type": "ws",
            "path": p.get("path", ["/"])[0],   # parse_qs 已解码，再 unquote 会二次解码
            "headers": {"Host": p["host"][0]} if p.get("host") else {},
        }
    elif network == "grpc":
        out["transport"] = {"type": "grpc", "service_name": p.get("serviceName", [""])[0]}
    return out

def _parse_vmess_b64(raw_body: str) -> dict:
    """解析 vmess://<base64 JSON>（v2rayN/Clash 通用格式）。解析失败返回 {}。"""
    try:
        decoded = base64.b64decode(raw_body + "=" * (-len(raw_body) % 4)).decode("utf-8")
        j = json.loads(decoded)
    except Exception as e:
        print(f"vmess base64 解析失败: {type(e).__name__}: {str(e)[:80]}", file=sys.stderr)
        return {}
    host, port = j.get("add"), int(j.get("port", 0) or 0)
    if not host or not port:
        return {}
    out = {
        "type": "vmess",
        "tag": f"{host}:{port}-vm",
        "server": host,
        "server_port": port,
        "uuid": j.get("id", ""),
        "alter_id": int(j.get("aid", 0) or 0),
        "security": j.get("scy") or "auto",
    }
    tls_on = str(j.get("tls", "")).lower() in ("tls", "true", "1", "reality")
    out["tls"] = {
        "enabled": tls_on,
        "server_name": j.get("sni") or j.get("host") or host,
        "insecure": False,
        "utls": {"enabled": True, "fingerprint": "chrome"},
    }
    net = j.get("net", "tcp")
    if net == "ws":
        out["transport"] = {
            "type": "ws",
            "path": j.get("path") or "/",
            "headers": {"Host": j["host"]} if j.get("host") else {},
        }
    elif net == "grpc":
        out["transport"] = {"type": "grpc", "service_name": j.get("path") or ""}
    return out

def parse_vmess(url: str) -> dict:
    # vmess 主流格式是 base64 编码的 JSON（v2rayN/Clash 通用）：
    #   vmess://eyJ2IjoiMiIsInBzIjoi...    (base64 of {"v","ps","add","port","id",...})
    # 少数是标准 URL 格式: vmess://uuid@server:port?alterId=0&security=auto&type=ws&...
    # 必须用原始串判断：urlparse 会把 base64 当 netloc 并小写化（破坏数据），
    # 且 base64 含 "/" 会被切成 path。标准格式必有 "@"，base64 字符集不含 "@"。
    raw_body = url.split("://", 1)[1].split("#", 1)[0]
    if "@" not in raw_body:
        return _parse_vmess_b64(raw_body)

    u = urllib.parse.urlparse(url)
    p = urllib.parse.parse_qs(u.query)
    uuid = urllib.parse.unquote(u.username) if u.username else ""
    alter_id = int(p.get("alterId", ["0"])[0])
    security = p.get("security", ["auto"])[0]
    network = p.get("type", ["tcp"])[0]
    tls = p.get("tls", ["0"])[0] == "1"
    sni = p.get("sni", [u.hostname])[0] or u.hostname
    fp = p.get("fp", ["chrome"])[0]
    ws_path = p.get("path", ["/"])[0]
    ws_host = p.get("host", [""])[0]

    out = {
        "type": "vmess",
        "tag": _tag_for(u, "vm"),
        "server": u.hostname,
        "server_port": u.port,
        "uuid": uuid,
        "alter_id": alter_id,
        "security": security,
        "tls": {
            "enabled": tls,
            "server_name": sni,
            "insecure": False,
            "utls": {"enabled": True, "fingerprint": fp},
        },
    }
    if network == "ws":
        out["transport"] = {
            "type": "ws",
            "path": ws_path,
            "headers": {"Host": ws_host} if ws_host else {},
        }
    return out

def parse_shadowsocks(url: str) -> dict:
    u = urllib.parse.urlparse(url)
    # ss://method:password@server:port?plugin=obfs-local;obfs=http;obfs-host=...#name
    # 或 ss://base64(method:password)@server:port?plugin=...#name
    userinfo = u.username or ""
    if ":" in userinfo and not userinfo.endswith("="):
        # 明文 method:password
        method, password = userinfo.split(":", 1)
    else:
        # base64 编码
        try:
            decoded = base64.b64decode(userinfo + "=" * (-len(userinfo) % 4)).decode()
            method, password = decoded.split(":", 1)
        except Exception:
            method, password = "aes-256-gcm", userinfo

    p = urllib.parse.parse_qs(u.query)
    plugin = p.get("plugin", [""])[0]

    out = {
        "type": "shadowsocks",
        "tag": _tag_for(u, "ss"),
        "server": u.hostname,
        "server_port": u.port,
        "method": method,
        "password": password,
    }
    if plugin:
        # 解析 plugin options: obfs-local;obfs=http;obfs-host=example.com
        # sing-box 要求 plugin_opts 是分号拼接的字符串，传 dict 会 FATAL
        plugin_parts = plugin.split(";")
        out["plugin"] = plugin_parts[0]
        opts = ";".join(plugin_parts[1:])
        if opts:
            out["plugin_opts"] = opts
    return out

def parse_vless(url: str) -> dict:
    u = urllib.parse.urlparse(url)
    p = urllib.parse.parse_qs(u.query)
    network = p.get("type", ["tcp"])[0]
    security = p.get("security", ["none"])[0]

    out = {
        "type": "vless",
        "tag": _tag_for(u, "vl"),
        "server": u.hostname,
        "server_port": u.port,
        "uuid": urllib.parse.unquote(u.username) if u.username else "",
    }
    if p.get("flow", [""])[0]:
        out["flow"] = p["flow"][0]
    pbk = p.get("pbk", [""])[0]
    if security in ("tls", "reality") or pbk:
        out["tls"] = {
            "enabled": True,
            "server_name": p.get("sni", [u.hostname])[0] or u.hostname,
            "insecure": p.get("allowInsecure", ["0"])[0] == "1",
            "utls": {"enabled": True, "fingerprint": p.get("fp", ["chrome"])[0]},
        }
    # 以 pbk 是否存在为准（而非 security 字段）：Clash-YAML 源的 reality 节点会被
    # 转成 security=tls 但仍带 pbk；而 pbk 为空时下发 reality 块会让 sing-box
    # check 直接 FATAL（invalid public_key），使 sing-box 起不来。
    if pbk:
        out["tls"]["reality"] = {
            "enabled": True,
            "public_key": pbk,
            "short_id": p.get("sid", [""])[0],
        }
    if network == "ws":
        out["transport"] = {
            "type": "ws",
            "path": p.get("path", ["/"])[0],   # parse_qs 已解码，再 unquote 会二次解码
            "headers": {"Host": p["host"][0]} if p.get("host") else {},
        }
    elif network == "grpc":
        out["transport"] = {"type": "grpc", "service_name": p.get("serviceName", [""])[0]}
    return out

PARSERS = {
    "hysteria2": parse_hysteria2,
    "anytls": parse_anytls,
    "trojan": parse_trojan,
    "vless": parse_vless,
    "vmess": parse_vmess,
    "shadowsocks": parse_shadowsocks,
    "ss": parse_shadowsocks,
}

def parse_url(url: str) -> dict | None:
    scheme = url.split("://", 1)[0].lower()
    parser = PARSERS.get(scheme)
    if not parser:
        print(f"unsupported scheme: {scheme}", file=sys.stderr)
        return None
    try:
        return parser(url)
    except Exception as e:
        safe = url.split("@")[-1] if "@" in url else url[:80]
        print(f"failed parse: {safe[:80]}... err={e}", file=sys.stderr)
        return None

# ============= 生成 sing-box config =============
def _is_loopback(host: str) -> bool:
    """回环/本机地址（127.0.0.0/8, ::1, localhost）不可能是有效代理出口。"""
    import ipaddress
    h = (host or "").strip().lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False

def make_config(nodes: list[dict], listen_port: int = 0,
                clash_port: int = 0, clash_secret: str = "",
                include_h2: bool = False) -> dict:
    """生成 sing-box 配置：mixed port + selector + clash API

    include_h2: 是否包含 hysteria2 节点（WSL2 下 utls 不兼容，默认排除）
    """
    if not listen_port:
        listen_port = CFG["singbox"]["mixed_port"]
    if not clash_port:
        clash_port = int(CFG["singbox"]["clash_api"].rsplit(":", 1)[-1])
    # 丢弃残缺节点：解析失败的类型会产出 server="" / port=None 这种"垃圾但合法"的
    # 条目（sing-box check 不报错），daemon 会把它当正常节点轮换，白白浪费一次切换
    before = len(nodes)
    nodes = [n for n in nodes if n.get("server") and n.get("server_port")]
    # 回环/本机地址不可能是有效出口，混在池里同样会让 daemon 空轮换一次
    nodes = [n for n in nodes if not _is_loopback(n["server"])]
    if len(nodes) < before:
        print(f"  dropped {before - len(nodes)} invalid nodes (missing server/port or loopback)", file=sys.stderr)
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
                "listen": "127.0.0.1",
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
    ap.add_argument("--urls", nargs="+", help="subscription URLs (can specify multiple)")
    ap.add_argument("--url", help="single subscription URL (legacy)")
    ap.add_argument("--out", default=CFG["singbox"]["config_file"])
    ap.add_argument("--port", type=int, default=CFG["singbox"]["mixed_port"])
    ap.add_argument("--clash-port", type=int, default=9090)
    ap.add_argument("--secret", default=None)
    ap.add_argument("--include-h2", action="store_true", help="include hysteria2 (default: skip, WSL2 utls incompatible)")
    ap.add_argument("--filter", default="", help="substring filter node name (case-insensitive)")
    args = ap.parse_args()

    # 无参数时回退到 OKZ_SUB_URL 环境变量（README 的引导命令依赖此路径）
    sub_urls = args.urls or ([args.url] if args.url else []) or ([_OKZ_SUB_URL] if _OKZ_SUB_URL else [])
    if not sub_urls:
        print("ERROR: no subscription URLs provided (use --urls or --url)", file=sys.stderr)
        sys.exit(1)

    all_urls = []
    for url in sub_urls:
        safe_url = url.split("@")[-1] if "@" in url else url[:60]
        print(f"fetching subscription from {safe_url}...")
        try:
            urls = fetch_and_decode_sub(url)
        except Exception as e:
            print(f"  ✗ 源失败，跳过: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            continue
        print(f"  got {len(urls)} raw URLs")
        all_urls.extend(urls)

    print(f"total raw URLs from all sources: {len(all_urls)}")

    nodes = []
    seen_tags: set[str] = set()
    filter_sub = args.filter.lower() if args.filter else None
    for u in all_urls:
        n = parse_url(u)
        if n and n["tag"] not in seen_tags:
            if filter_sub and filter_sub not in n["tag"].lower():
                continue
            seen_tags.add(n["tag"])
            nodes.append(n)

    print(f"parsed {len(nodes)} usable nodes (deduplicated)")
    if not nodes:
        print("ERROR: no nodes parsed, aborting")
        sys.exit(1)

    cfg = make_config(nodes, args.port, args.clash_port, args.secret or get_singbox_secret(), include_h2=args.include_h2)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    print(f"wrote config to {args.out}")
    print(f"first 3 nodes:")
    for n in nodes[:3]:
        print(f"  - {n['tag']:50s} {n['type']:10s} {n['server']}:{n['server_port']}")
    print(f"total usable nodes: {len(nodes)}")
    print(f"clash API: 127.0.0.1:{args.clash_port}")
    print(f"mixed port: 0.0.0.0:{args.port}")
