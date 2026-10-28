#!/usr/bin/env python3
"""
解析 okz 订阅，生成 sing-box 配置
"""
import base64
import os
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
    # text_limit=None：非 JSON 订阅是整段 base64/明文，截断到 2000 字符
    # 会静默丢掉后面的节点（多源合并后按截断内容生成配置）。
    result = request_json("GET", url, timeout=30, headers=headers,
                          text_limit=None)
    if not result.ok:
        # 与 raise_for_status 等价：传输失败单独说明，非 2xx 带上状态码。
        if result.status is None:
            detail = redact_secrets(result.error or "transport error")
        else:
            detail = f"HTTP {result.status}"
        raise RuntimeError(f"订阅拉取失败: {detail}")
    # JSON 响应走 data；非 JSON（base64/Clash YAML/明文列表）走全文 text
    # （request_json 显式传 text_limit=None，不能截断）。
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



def _load_dormant_outbounds() -> list[dict]:
    """休眠节点：订阅源暂时不可用、但节点随时可能复活的。

    现状（2026-10-28 实测）：okz 的订阅**是活的**（HTTP 200、64 条 URL），但里面的
    节点全死。所以它们被抽出来放在这份外挂文件里：出站保留，池子成员按实测动态剔除。

    与 WARP 出站共用「主配置外挂一份 JSON」的做法，区别是这些节点要进
    主池 selector —— daemon 的轮换与降级判断看的是主池成员。放在订阅节点
    之后；selector 的 default 由 `_pick_default` 依证据挑（**不是** nodes[0]），
    所以不会被死节点顶掉。
    """
    p = Path(__file__).resolve().parent / "dormant-outbounds.json"
    try:
        with open(p) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        print(f"  休眠节点文件读取失败，跳过: {e}", file=sys.stderr)
        return []
    items = data if isinstance(data, list) else [data]
    out = [o for o in items
           if isinstance(o, dict) and o.get("tag") and o.get("type")
           and o.get("server") and o.get("server_port")]
    if len(out) != len(items):
        print(f"  dropped {len(items) - len(out)} invalid dormant entries",
              file=sys.stderr)
    return out


def _node_fp(node: dict) -> str:
    """节点指纹。tag 是订阅到达顺序分配的（`~N` 后缀），源一换序，同一个 tag
    可能换指另一个物理节点；状态证据按指纹核对，对不上就整条忽略。"""
    tr = node.get("transport") or {}
    tls = node.get("tls") or {}
    ws = ""
    if isinstance(tr, dict):
        ws = f"{tr.get('type', '')}|{tr.get('path', '')}|{tr.get('host', '')}"
    sni = tls.get("server_name") if isinstance(tls, dict) else ""
    return f"{node.get('server')}|{node.get('server_port')}|{node.get('type')}|{ws}|{sni}"


def _load_dormant_state(current: list[dict] | None = None) -> set[str]:
    """沉睡池状态：`alive: false` 的 tag（由 dormant_probe.py 实测写入）。

    读不到或格式不对就当空集 —— 探测脚本没跑过时行为与原来一致（全进池子）。
    不能因为一个状态文件读失败，把整池子节点挡在门外。

    三条过滤（2026-10-28 三方审查补的）：
      - 字符串 "false" 也认，免得手写过一次就让整套机制静默失效；
      - 证据有保质期（7 天）：一周前判死的不能当今天的事实；
      - 记录带 `fp` 且与当前同 tag 节点的指纹不符 → 整条忽略（tag 被别的节点占了）。
    """
    p = Path(__file__).resolve().parent / ".dormant_state.json"
    try:
        with open(p) as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        # 静默返回空集 = 判死的节点整批回池，且日志上跟"今天没判死任何节点"无法区分。
        print(f"  ⚠ 沉睡池状态读不到（{e}），本轮全部节点进池子", file=sys.stderr)
        return set()
    entries = d.get("nodes") if isinstance(d, dict) else None
    if not isinstance(entries, dict):
        return set()
    import time
    now = time.time()
    fps = {n["tag"]: _node_fp(n) for n in (current or [])}
    out: set[str] = set()
    for t, v in entries.items():
        if not isinstance(v, dict):
            continue
        alive = v.get("alive")
        is_dead = alive is False or (
            isinstance(alive, str) and alive.strip().lower() in ("false", "0", "no"))
        if not is_dead:
            continue
        try:
            checked = float(v.get("checked") or 0)
        except (TypeError, ValueError):
            checked = 0.0
        if checked and (now - checked) > 7 * 24 * 3600:
            continue
        fp = v.get("fp")
        if fp and t in fps and fp != fps[t]:
            continue          # tag 被别的节点占了，旧证据作废
        out.add(t)
    return out


def write_sub_stats(out_path, sub_nodes: int, dormant_source_nodes: int,
                    pool_nodes: int, dead_nodes: int, fallback: bool) -> None:
    """把"这一次生成了什么"写在**配置文件旁边**（`<out>.stats.json`）。

    WHY 不写仓库根的一个固定文件（2026-10-28 三轮审查，两条都是实测出来的）：
      - 任何手工跑、pytest、`--out /tmp/...` 试跑都会调 make_config，写固定路径会
        把线上基线污染成试跑的数值（实测：`.sub_stats.json` 的 mtime 是 18:58 的
        一次试跑，而线上配置是 18:15 生成的）。
      - 峰值不能在这里算：`ts` 每次刷新都被重写，"距上次写 >24h 才重置"永远不成立
        → 基线变成"自上次中断以来的历史最大"，只会涨不会落，一次合法的池子腰斩
        就把订阅刷新永久钉死（实测：pool 128 → 60 之后每一轮都被拒）。
    滚动窗口交给 daemon：它按真实时间戳保存样本序列，自己 prune。
    """
    import time as _t
    if not out_path:
        # 不传路径就什么都不写。以前会写出 "None.stats.json" —— pytest 里 15 处
        # make_config(...) 不带 stats_out，在仓库根跑一遍就掉一个未跟踪文件。
        return
    try:
        tmp = str(out_path) + ".stats.json.tmp"
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"sub_nodes": sub_nodes,
                       "dormant_source_nodes": dormant_source_nodes,
                       "pool_nodes": pool_nodes,
                       "dead_nodes": dead_nodes,
                       "fallback": bool(fallback),
                       "ts": _t.time()}, f)
        os.replace(tmp, str(out_path) + ".stats.json")
    except OSError as e:
        print(f"  ⚠ 订阅统计写入失败（本轮不做骤降判定）: {e}", file=sys.stderr)


def _pick_default(nodes: list[dict]) -> str:
    """取一个「有存活证据」的节点当 selector 默认值。

    两份证据都在磁盘上、都由别人持续写：
      - `.bad_nodes.json`        daemon 的临时封禁（tag → {expire, reason}），过期不算
      - `.warp/wg_colo_map.json` 实测 colo 台账，`ok: false` = 探不通

    优先级：实测 ok:true → 证据不明的 → （全死时）nodes[0]。
    为什么不在这里现测：生成配置是热路径，128 条逐个连一遍要几分钟；而「默认值」
    只要不是死节点就够，剩下的交给 daemon 轮换 —— 别在这儿替它做决策。
    """
    import time
    if not nodes:
        return "direct"
    base = Path(__file__).resolve().parent
    now = time.time()
    dead: set[str] = set()
    alive: set[str] = set()
    try:
        with open(base / ".bad_nodes.json") as f:
            for t, v in json.load(f).items():
                if isinstance(v, dict) and (v.get("expire") or 0) > now:
                    dead.add(t)
    except (OSError, ValueError):
        pass
    try:
        with open(base / ".warp/wg_colo_map.json") as f:
            raw = json.load(f)
        raw = raw if isinstance(raw, dict) else {}
        cm = raw.get("nodes") or {}
        # 台账里有 updated。太旧的 ok:true 不能当「实测通」用：一次探测的结论
        # 最多管一天，之后节点可能早换了落地。
        try:
            upd = float(raw.get("updated") or 0)
        except (TypeError, ValueError):
            upd = 0.0
        if upd and (now - upd) > 24 * 3600:
            print(f"  colo 台账已过期 {(now - upd) / 3600:.0f} 小时，"
                  f"选默认值时不予采信", file=sys.stderr)
            cm = {}
        for t, v in cm.items():
            if not isinstance(v, dict):
                continue
            if v.get("ok") is False:
                dead.add(t)
            elif v.get("ok") is True:
                alive.add(t)
    except (OSError, ValueError):
        pass
    pick = [n["tag"] for n in nodes if n["tag"] in alive and n["tag"] not in dead]
    if not pick:
        pick = [n["tag"] for n in nodes if n["tag"] not in dead]
    return pick[0] if pick else nodes[0]["tag"]


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
                include_h2: bool = False, stats_out=None) -> dict:
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
    # 休眠节点排在订阅节点之后：default 由 _pick_default 挑，不会被死节点顶掉。
    # 订阅复活后同名 tag 由 node_tags 挡掉，不会重复。
    node_tags = {n["tag"] for n in nodes}
    dormant = [d for d in _load_dormant_outbounds()
               if d["tag"] not in node_tags]
    if not include_h2:
        # hysteria2 在 WSL2 下 utls 不兼容（默认排除）。休眠文件是外挂的，不跟着
        # 这个开关走就会从后门把 h2 放进池子。
        dormant = [d for d in dormant if d.get("type") != "hysteria2"]
    dormant_tags = {d["tag"] for d in dormant}
    # 与订阅节点/休眠节点同 tag 时丢弃 WARP 条目：命名空间撞上就是重复 outbound，
    # sing-box check 直接 FATAL，刷新会永远失败。
    warps = [w for w in _load_warp_outbounds()
             if w["tag"] not in node_tags and w["tag"] not in dormant_tags]
    if dormant:
        print(f"  + {len(dormant)} 个休眠节点（订阅源暂时不可用，等它复活）",
              file=sys.stderr)
    outbounds = nodes[:] + dormant + warps
    outbounds.append({"type": "direct", "tag": "direct"})

    # 沉睡池：出站全留着（参数、tag、订阅里的原样都在），但**池子成员按实测进出**。
    # `.dormant_state.json` 由 dormant_probe.py 写：alive:false = 刚探过、确实不通。
    # 动机：okz 那 65 个死节点留在池里时，daemon 每轮都在「撞死节点 → 标死 30 秒
    # → 再撞」之间空转（实测 dead=81），把换线机会浪费在注定失败的节点上。
    sleeping = _load_dormant_state(nodes + dormant)
    total = len(nodes) + len(dormant)
    pool_objs = [x for x in nodes + dormant if x["tag"] not in sleeping]
    dropped = total - len(pool_objs)
    fallback = False
    if len(pool_objs) < 3:
        # 全被判死时宁可空转也不能让池子空着：selector 没成员就直接不可用。
        # 但这必须喊出来 —— 退回全部等于「把判死的节点又放回来」，静默做就是骗人，
        # 而且计数会变成 0，日志上跟「完全没过滤」无法区分。
        print(f"  ⚠ 只剩 {len(pool_objs)} 条没被判死（共 {total} 条），退回全部节点；"
              f"池子里重新混入 {dropped} 条已判死的", file=sys.stderr)
        pool_objs = nodes + dormant
        fallback = True
    if sleeping and not fallback:
        print(f"  - {dropped} 个节点暂离池子（实测不通，出站仍保留）",
              file=sys.stderr)
    # dead_nodes 是"因实测判死被摘出池子"的条数 —— 这是事后回答"为什么它不在池子里"
    # 唯一可靠的数字（原来只有 stderr 那一行，成功刷新时会被 daemon 丢掉）。
    write_sub_stats(stats_out, len(nodes), len(dormant), len(pool_objs),
                    len(sleeping), fallback)
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
                "outbounds": [x["tag"] for x in pool_objs] + ["direct"],
                "default": _pick_default(pool_objs),
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
    seen_urls: set[str] = set()
    seen_tags: set[str] = set()
    filter_sub = args.filter.lower() if args.filter else None
    for u in all_urls:
        # 身份 = 去掉 #名字 的整条 URL：同一台机器上靠 ws path / sni 区分的
        # 多条节点身份不同，必须都留下；只有名字不同的重复项才该合并。
        ident = u.split("#", 1)[0]
        if ident in seen_urls:
            continue
        n = parse_url(u)
        if not n:
            continue
        if filter_sub and filter_sub not in n["tag"].lower():
            continue
        # tag 撞名不能整条丢：dasho 的 9 条日本节点全在
        # unamecf*.xn--ghqu5fm27b67w.com:443，只靠 path=/pq/jp1..jp5 区分，
        # 而 tag 只有 host:port-协议 —— 旧逻辑把 5 条压成 1 条（71 条 URL
        # 只剩 41 个 tag，24 个真节点白丢），节点数连骤降护栏都过不了。
        if n["tag"] in seen_tags:
            scheme = n["tag"].rsplit("-", 1)[-1]
            stem = f"{n['server']}:{n['server_port']}"
            i = 2
            while f"{stem}~{i}-{scheme}" in seen_tags:
                i += 1
            n["tag"] = f"{stem}~{i}-{scheme}"
        seen_urls.add(ident)
        seen_tags.add(n["tag"])
        nodes.append(n)

    print(f"parsed {len(nodes)} usable nodes (deduplicated)")
    if not nodes:
        print("ERROR: no nodes parsed, aborting")
        sys.exit(1)

    cfg = make_config(nodes, args.port, args.clash_port, args.secret or get_singbox_secret(), include_h2=args.include_h2, stats_out=args.out)
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
