#!/usr/bin/env python3
"""
解析 okz 订阅，生成 sing-box 配置
"""
import base64
import json
import urllib.parse
import sys
from pathlib import Path

from asip.atomic_io import write_json
from asip.http import request_json
from asip.redact import redact_secrets

import config
from config import CFG, get_lan_auth, get_singbox_secret

LAN_INBOUND_TAG = "mixed-lan"


# ============= 解析订阅 =============
def fetch_and_decode_sub(url: str) -> list[str]:
    """拉订阅，base64 解码，返回原始 URL 列表
    支持：标准 base64 订阅、明文 URL 列表、Clash YAML 配置（提取 proxy 节点）
    """
    import yaml
    # 仅对 dasho 等需要 UA 的源添加 headers
    headers = {"User-Agent": "clash-verge/1.0"} if "dasho" in url else {}
    result = request_json("GET", url, timeout=30, headers=headers)
    if not result.ok:
        # 与 raise_for_status 等价：传输失败单独说明，非 2xx 带上状态码。
        if result.status is None:
            detail = redact_secrets(result.error or "transport error")
        else:
            detail = f"HTTP {result.status}"
        raise RuntimeError(f"订阅拉取失败: {detail}")
    # JSON 响应走 data；非 JSON（base64/Clash YAML/明文列表）走截断后的 text。
    # 结构化 data 重新序列化，后面的 base64/YAML/splitlines 解析都针对字符串。
    if result.data is not None:
        raw = (result.data if isinstance(result.data, str)
               else json.dumps(result.data, ensure_ascii=False))
    else:
        raw = result.text or ""

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

def _clash_transport_query(proxy: dict) -> str:
    """把 Clash 节点的 network/ws-opts/grpc-opts 转成 URL query 片段。

    Clash YAML 源的节点带 network 字段；不转发的话转出的 URL 缺 type，
    解析回 sing-box 时 transport 丢失 —— 节点照常入池，但握手必失败。
    """
    net = str(proxy.get("network", "tcp")).lower()
    if net in ("tcp", ""):
        return ""
    q = f"&type={net}"
    if net == "ws":
        ws = proxy.get("ws-opts", {}) or {}
        path = ws.get("path", "/")
        host = (ws.get("headers", {}) or {}).get("Host", "")
        q += f"&path={urllib.parse.quote(path)}"
        if host:
            q += f"&host={urllib.parse.quote(host)}"
    elif net == "grpc":
        grpc = proxy.get("grpc-opts", {}) or {}
        svc = grpc.get("grpc-service-name", "")
        if svc:
            q += f"&serviceName={urllib.parse.quote(svc)}"
    return q

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
        elif network == "grpc":
            q += f"&serviceName={urllib.parse.quote(proxy.get('grpc-opts', {}).get('grpc-service-name', ''))}"

        return f"vless://{uuid}@{server}:{port}?{q}#{urllib.parse.quote(name)}"

    elif ptype == "trojan":
        password = proxy.get("password", "")
        tls = proxy.get("tls", True)
        sni = proxy.get("sni", server)
        q = f"security={'tls' if tls else 'none'}"
        if sni and sni != server:
            q += f"&sni={urllib.parse.quote(sni)}"
        # 转发 transport：不转发会让 ws/grpc 节点转成 URL 后丢 transport，
        # 入池但每次连接都失败
        q += _clash_transport_query(proxy)
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
            if network == "ws":
                q += f"&host={urllib.parse.quote(host)}&path={urllib.parse.quote(path)}"
            elif network == "grpc":
                q += f"&serviceName={urllib.parse.quote(proxy.get('grpc-opts', {}).get('grpc-service-name', ''))}"
        if tls:
            q += "&tls=1"
        if sni and sni != server:
            q += f"&sni={urllib.parse.quote(sni)}"
        if fp:
            q += f"&fp={fp}"
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

# sing-box 接受这些 transport；显式 "tcp" 是非法值（会导致启动 FATAL），
# 其余未知类型也一律不发（宁可用默认 TCP 行为，也不发非法值）
VALID_TRANSPORTS = ("ws", "grpc", "http", "quic", "httpupgrade")

def _transport_from_query(p: dict) -> dict | None:
    """从 URL query 构造 sing-box transport 块（trojan/vless 共用）。

    合法类型全部转发（http/quic/httpupgrade 此前被静默丢弃，节点会进入池子
    但每次连接都失败）；ws 带 path/host，grpc 带 service_name。
    """
    network = p.get("type", [""])[0]
    if network not in VALID_TRANSPORTS:
        return None
    if network == "ws":
        return {
            "type": "ws",
            "path": p.get("path", ["/"])[0],   # parse_qs 已解码，再 unquote 会二次解码
            "headers": {"Host": p["host"][0]} if p.get("host") else {},
        }
    if network == "grpc":
        return {"type": "grpc", "service_name": p.get("serviceName", [""])[0]}
    return {"type": network}                    # http / quic / httpupgrade

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
    tr = _transport_from_query(p)
    if tr:
        out["transport"] = tr
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
    tr = _transport_from_query(p)
    if tr:
        out["transport"] = tr
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
    tr = _transport_from_query(p)
    if tr:
        out["transport"] = tr
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
def _is_loopback(host) -> bool:
    """回环/本机地址（127.0.0.0/8, ::1, localhost）不可能是有效代理出口。

    host 可能是非字符串（订阅 JSON 里 add 写成数字，v2rayN 对纯 IP 主机
    会这么干），强转 str 避免 make_config 整体崩溃导致节点池冻结。
    """
    import ipaddress
    h = str(host or "").strip().lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False
def _load_warp_outbounds() -> list[dict]:
    """WARP 出口（主+备）：.warp/warp-outbound.json（600，gitignore）。

    单个对象或对象数组皆可；缺失/损坏/条目不完整返回 []，整条链路不生成。
    多个出口时 opencode-out 是主备 selector，可在隧道失效时秒切另一条。
    """
    p = Path(__file__).resolve().parent / ".warp" / "warp-outbound.json"
    try:
        with open(p) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        print(f"  WARP 配置读取失败，跳过: {e}", file=sys.stderr)
        return []
    items = data if isinstance(data, list) else [data]
    out = [o for o in items if isinstance(o, dict) and o.get("tag") and o.get("server_port")]
    if len(out) != len(items):
        print(f"  dropped {len(items) - len(out)} invalid WARP entries", file=sys.stderr)
    return out



def _build_inbounds(listen_port: int) -> list[dict]:
    """环回入站（必有）+ 局域网入站（配了端口且有认证时才建）。

    分成两个入站而非把一个入站改监听 0.0.0.0：环回口给 9router 与 daemon，
    它们不该需要认证；局域网口给同网段的手机/其他机器，必须有认证。
    单入站做不到「按来源决定要不要认证」。
    """
    inbounds = [{
        "type": "mixed",
        "tag": "mixed-in",
        "listen": "127.0.0.1",
        "listen_port": listen_port,
    }]
    lan_port = CFG["singbox"].get("lan_port", 0)
    if not lan_port:
        return inbounds
    auth = get_lan_auth()
    if not auth:
        print(f"  LAN 入站跳过：{config.LAN_AUTH_FILE} 缺失或格式非法（应为 username:password）；"
              f"不建无认证的局域网监听", file=sys.stderr)
        return inbounds
    username, password = auth
    inbounds.append({
        "type": "mixed",
        "tag": LAN_INBOUND_TAG,
        "listen": "0.0.0.0",
        "listen_port": lan_port,
        "users": [{"username": username, "password": password}],
    })
    return inbounds


def _build_lan_guard(inbounds: list[dict]) -> list[dict]:
    """把局域网入站的来源限制在私网段，其余一律 reject。

    sing-box 没有 inbound 级的 allowed_cidr 字段，只能在 route 层拦。放在路由
    规则最前面 —— 一旦被后面的 resolve/sniff 处理过，判据就不再是纯来源地址。

    判据取自实际建出的入站而非配置里的端口：端口开着但认证文件缺失时入站不会
    生成，此时还留一条引用该 tag 的规则就是悬空规则（check 不报错，纯垃圾）。
    """
    if not any(i["tag"] == LAN_INBOUND_TAG for i in inbounds):
        return []
    return [{
        "type": "logical",
        "mode": "and",
        "rules": [
            {"inbound": [LAN_INBOUND_TAG]},
            {"source_ip_cidr": CFG["singbox"]["lan_cidrs"], "invert": True},
        ],
        "action": "reject",
    }]


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
    nodes = [n for n in nodes if not _is_loopback(n.get("server", ""))]
    if len(nodes) < before:
        print(f"  dropped {before - len(nodes)} invalid nodes (missing server/port or loopback)", file=sys.stderr)
    if not include_h2:
        nodes = [n for n in nodes if n.get("type") != "hysteria2"]
    # 与订阅节点同 tag 时丢弃 WARP 条目（订阅里同名节点优先）
    node_tags = {n["tag"] for n in nodes}
    warps = [w for w in _load_warp_outbounds() if w["tag"] not in node_tags]
    outbounds = nodes[:] + warps
    outbounds.append({"type": "direct", "tag": "direct"})
    inbounds = _build_inbounds(listen_port)
    # selector tag 与 daemon / CLI 共用 config 里的名字：改一处即可整体改名，
    # 不必再同步 grep 本文件里的字符串字面量。
    pool_selector = CFG["singbox"]["selector"]
    warp_selector = CFG["warp"]["selector"]

    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": inbounds,
        "outbounds": outbounds + [
            {
                "type": "selector",
                "tag": pool_selector,
                "outbounds": [n["tag"] for n in nodes] + ["direct"],
                "default": nodes[0]["tag"] if nodes else "direct",
                "interrupt_exist_connections": False,
            },
            # opencode 流量固定走 WARP：403 实为 IP 信誉拦截，轮换池全在脏 IP 上。
            # 多个 WARP 出口时是主备 selector：daemon 切到备线即可让主隧道重连换 IP。
            *([{
                "type": "selector",
                "tag": warp_selector,
                "outbounds": [w["tag"] for w in warps] + [pool_selector],
                "default": warps[0]["tag"],
                "interrupt_exist_connections": False,
            }] if warps else []),
        ],
        # opencode 域名独立解析：WSL2 下 /etc/resolv.conf 的 8.8.8.8 不可达，
        # 走阿里 DoH 本地解析后把 IP 交给节点（route 的 resolve 动作），
        # 而非透传域名由节点侧解析。其余域名仍用系统 DNS。
        #
        # 域名必须写裸域 `opencode.ai` 而非 `.opencode.ai`：后者是
        # domain_suffix 的"以 .opencode.ai 结尾"形式，**匹配不到裸域**。
        # 实测流量 12086 次裸域 / 2 次子域，写错形式等于专用 DNS 与
        # 本地 resolve 两个动作全部落空，裸域只能回落到 dns-default，
        # 依赖不稳定的系统解析器。与 route 出站规则保持同一种写法。
        # 注意 dns server 不能加 detour: "direct" —— sing-box check 通过但
        # 运行时 FATAL "detour to an empty direct outbound makes no sense"。
        "dns": {
            "servers": [
                {"type": "https", "tag": "dns-opencode", "server": "223.5.5.5"},
                {"type": "local", "tag": "dns-default"},
            ],
            "rules": [
                {"domain_suffix": ["opencode.ai", ".opencode.ai"],
                 "server": "dns-opencode"},
            ],
            "final": "dns-default",
        },
        "route": {
            "rules": [
                # 必须最先：来源判据要看到原始地址，不能被 sniff/resolve 影响
                *_build_lan_guard(inbounds),
                {"action": "sniff"},
                # 必须先于 resolve：resolve 后 destination 变为 IP，domain 规则不再命中
                *([{"domain_suffix": ["opencode.ai"], "outbound": warp_selector}] if warps else []),
                {"action": "resolve", "domain_suffix": ["opencode.ai", ".opencode.ai"]},
            ],
            "final": pool_selector,
            # 配了多个 dns server 时缺此字段 → sing-box 1.12+ 拒绝启动
            "default_domain_resolver": {"server": "dns-default"},
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
    ap.add_argument("--urls-file",
                    help="file containing one subscription URL per line (avoids argv leak)")
    ap.add_argument("--url", help="single subscription URL (legacy)")
    ap.add_argument("--out", default=CFG["singbox"]["config_file"])
    ap.add_argument("--port", type=int, default=CFG["singbox"]["mixed_port"])
    ap.add_argument("--clash-port", type=int,
                    default=int(CFG["singbox"]["clash_api"].rsplit(":", 1)[-1]))
    ap.add_argument("--secret", default=None)
    ap.add_argument("--include-h2", action="store_true", help="include hysteria2 (default: skip, WSL2 utls incompatible)")
    ap.add_argument("--filter", default="", help="substring filter node name (case-insensitive)")
    args = ap.parse_args()

    # 无参数时回退到 CFG 里的全部订阅源（与 daemon 的每小时刷新同一份清单，
    # 每项都可用环境变量覆盖）。README 的首装命令依赖此路径。
    file_urls = []
    if args.urls_file:
        try:
            with open(args.urls_file, encoding="utf-8") as f:
                file_urls = [ln.strip() for ln in f
                             if ln.strip() and not ln.lstrip().startswith("#")]
        except OSError as e:
            print(f"ERROR: 读取 --urls-file 失败: {e}", file=sys.stderr)
            sys.exit(1)
    sub_urls = (args.urls or file_urls
                or ([args.url] if args.url else [])
                or config.subscription_urls())
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
            # 异常串可能带完整 URL（含 token），stderr 会进 daemon.log。
            print(f"  ✗ 源失败，跳过: {type(e).__name__}: "
                  f"{redact_secrets(str(e))[:120]}", file=sys.stderr)
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
    # 调用方（daemon）传进来的 args.out 本身已是 <config>.tmp：它要在 promote
    # 前做节点数/sing-box check 校验。write_json 先写同目录唯一临时文件再
    # os.replace 到 args.out，既不截断写（崩溃不会留下半份 JSON），也不改变
    # 调用方看到的路径契约。
    write_json(args.out, cfg, mode=0o600, indent=2)
    print(f"wrote config to {args.out}")
    print(f"first 3 nodes:")
    for n in nodes[:3]:
        print(f"  - {n['tag']:50s} {n['type']:10s} {n['server']}:{n['server_port']}")
    print(f"total usable nodes: {len(nodes)}")
    print(f"clash API: 127.0.0.1:{args.clash_port}")
    print(f"mixed port: 127.0.0.1:{args.port}（仅环回）")
    # 局域网入站是否建出由认证文件决定，不看配置端口：打印嘴边要讲实际结果，
    # 否则「日志说要开」和「实际没开」会让人去查防火墙
    lan_port = CFG["singbox"].get("lan_port", 0)
    if lan_port and get_lan_auth():
        print(f"mixed port: 0.0.0.0:{lan_port}（局域网，需认证）")
    elif lan_port:
        print(f"mixed port: 局域网入站未启用（认证文件 {config.LAN_AUTH_FILE} 缺失/非法）")
