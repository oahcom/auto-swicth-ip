#!/usr/bin/env python3
"""
解析 okz 订阅，生成 sing-box 配置
"""
import base64
import os
import json
import re
import time
import urllib.parse
import sys
from pathlib import Path

from asip.atomic_io import write_json
from asip.http import request_json
from asip.redact import redact_secrets

import config
from config import CFG, get_lan_auth, get_singbox_secret

LAN_INBOUND_TAG = "mixed-lan"

# 机场自有域名体系（常量已移到 config.py：单线配置 `warp_wg.build_line_config`
# 也要用它，写在这里会让那个模块反向依赖一个订阅解析脚本）。
#
# 保留这三个名字是因为既有单测与文档按 `parse_subscription.AIRPORT_DNS_*` 引用。
# 少了这条：`cn.bing.com` 走系统 DNS → 真 Bing → 拿到的是 Microsoft 的证书，
# 与节点要求的 `*.okzdns.com` 不符 → 握手失败。2026-11-01 实测：
#   系统 DNS  cn.bing.com -> 202.89.233.101（真 Bing，证书 CN=www.bing.com）
#   机场 DNS  cn.bing.com -> 172.247.99.x / 115.89.129.x（前置，证书 CN=*.okzdns.com）
# bing.com 必须在内：机场拿它当跳板，只有解析到自己的前置 IP 才有意义。
AIRPORT_DNS_SUFFIXES = config.AIRPORT_DNS_SUFFIXES
AIRPORT_DNS_URL = config.AIRPORT_DNS_URL
AIRPORT_DNS_TAG = config.AIRPORT_DNS_TAG


# ============= 解析订阅 =============
def source_label(url: str) -> str:
    """订阅源的展示名：`主机/末段[?flag=…]`，**绝不带 token**。

    路径末段常是账户 token（`/api/v1/pq/<token>`），query 里也常有 `token=`。
    台账会被 CLI/仪表盘原样打印，带上就等于把凭据写进终端回滚缓冲区。
    """
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return "?"
    host = u.hostname or "?"
    seg = [s for s in (u.path or "").split("/") if s]
    tail = ""
    if seg:
        last = seg[-1]
        # 长串十六进制/字母数字几乎一定是账户 token（dasho 的 /api/v1/pq/<32 hex>）
        tail = "/***" if re.fullmatch(r"[0-9a-fA-F]{16,}", last) else f"/{last}"
    flag = ""
    for part in (u.query or "").split("&"):
        k, _, v = part.partition("=")
        if k.lower() == "flag" and v:
            flag = f"?flag={v}"
            break
    return f"{host}{tail}{flag}"


def sub_name(url: str) -> str:
    """订阅的短名：固定源用配置键（okz/dasho…），追加源用主机首段。

    与 `source_label` 的区别是用途：label 用来核对「是不是同一个 URL」，
    短名用来在报表里一列放下。追加源的域名常常被 CDN 包了一层
    （`etcloud-zone-3qpz…eo-edgefunctions.com`），取首个可读段就够认人。
    """
    try:
        for key in config.SUBSCRIPTION_KEYS:
            if url and url == (config.CFG.get(key) or {}).get("sub_url"):
                return key
    except (AttributeError, TypeError):
        pass
    try:
        host = urllib.parse.urlparse(url).hostname or "?"
    except ValueError:
        return "?"
    if "etcloud" in host:
        return "etcloud"
    first = host.split(".")[0]
    return first or host


# 机场塞在订阅头的「信息节点」：名字是套餐状态文案，本身也是可拨号节点
# （会真的进池子）。判据必须是**「状态词 + 冒号/剩余」**这种完整形态：
# 只匹配前缀会把 `官网线路`、`订阅节点1`、`重置专用` 这些真实节点名一起清空
# —— 实测 okz/dasho 都没有这种名字，但机场换命名习惯时就会误伤。
_INFO_NAME = re.compile(
    r"^(剩余流量|套餐到期|距离下次重置|到期时间|到期日期)\s*[：:]"
    r"|^(官网|订阅|官网地址)\s*[：:]"
    r"|^(距离下次重置剩余|重置剩余)\s*[：:]")


def display_name(name: str) -> str:
    """机场节点名 → 报表里的节点名。空/信息节点返回空串。"""
    n = (name or "").strip()
    return "" if not n or _INFO_NAME.match(n) else n


def _raw_body(result) -> str:
    """JSON 响应走 data；非 JSON（base64/Clash YAML/明文列表）走全文 text。

    request_json 显式传 text_limit=None，不能截断；结构化 data 重新序列化，
    后面的 base64/YAML/splitlines 解析都针对字符串。
    """
    if result.data is not None:
        return (result.data if isinstance(result.data, str)
                else json.dumps(result.data, ensure_ascii=False))
    return result.text or ""


def _fetch_raw(url: str) -> str:
    """抓订阅正文。默认 UA 被拒时用 clash-verge UA 重试一次。

    WHY 重试而不是继续按域名特判：2026-11-04 实测 etcloud 系订阅源默认 UA
    直接 503，带 `clash-verge/1.0` 才返回内容。按域名加 UA 的旧写法只能覆盖
    已知机场，新源要么改代码要么插不进来 —— 而 503/403 恰恰是"这个源按 UA
    决定给不给"的典型症状，重试一次的成本远低于让人回来改代码。
    okz 这类默认 UA 正常的源不会走到重试，行为不变。
    """
    headers = {"User-Agent": "clash-verge/1.0"} if "dasho" in url else {}
    result = request_json("GET", url, timeout=30, headers=headers,
                          text_limit=None)
    # 只在**服务器答了**时才换 UA 重试：status=None 是连不上/DNS 超时，换个 UA
    # 不会让死源复活，重试只会把单源失败耗时从 30s 翻到 60s（实测该用例因此
    # 撞上 60s 超时）。
    if not result.ok and not headers and result.status is not None:
        retry = request_json("GET", url, timeout=30,
                             headers={"User-Agent": "clash-verge/1.0"},
                             text_limit=None)
        if retry.ok:
            return _raw_body(retry)
    if not result.ok:
        # 与 raise_for_status 等价：传输失败单独说明，非 2xx 带上状态码。
        if result.status is None:
            detail = redact_secrets(result.error or "transport error")
        else:
            detail = f"HTTP {result.status}"
        raise RuntimeError(f"订阅拉取失败: {detail}")
    return _raw_body(result)


def singbox_outbounds(raw: str) -> list[dict]:
    """sing-box 配置里的节点出站，原样返回（已经是 make_config 要的形状）。

    `flag=sing` 这类源直接下发 sing-box JSON，不是 base64/Clash/明文 URI。
    **不转成 URL 再解析回来**：往返会丢字段（tls / transport / alpn），而
    parse_* 的产物与这份 dict 本来就是同构的 —— 转一趟只是给自己造坑。
    """
    try:
        cfg = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(cfg, dict):
        return []
    obs = cfg.get("outbounds")
    if not isinstance(obs, list):
        return []
    # 逻辑出站（selector/urltest）与内建类型不是可拨号节点；wireguard 是
    # endpoint 而不是 outbound，混进池子会让 sing-box check FATAL。
    skip = {"selector", "urltest", "direct", "block", "dns", "wireguard"}
    out = [o for o in obs
           if isinstance(o, dict) and o.get("type") not in skip
           and o.get("server") and o.get("server_port") and o.get("tag")]
    for o in out:
        # 机场给的原名先留一份再改写 tag：`_display` 是**非 sing-box 字段**，
        # 只在生成 `<cfg>.sources.json` 时读，写进配置前会被剥掉。
        o["_display"] = str(o["tag"])
        # tag 一律重算成 `host:port-<协议>`，与 URI 源同一个规则。
        # WHY：机场的 sing-box tag 是给人看的中文名，而且**每次刷新都变**
        # （`剩余流量：59.98 GB` → `59.99 GB`）。tag 是 daemon 判死表、
        # block-map、selector 成员共同的身份键 —— 让它跟着流量数字漂，
        # 等于每轮刷新都把整池节点当成新节点重来一遍。
        o["tag"] = f"{o['server']}:{o['server_port']}-{_SCHEME_ABBR.get(o['type'], o['type'])}"
    return out


def sub_cache_dir() -> Path:
    """源快照目录。默认 `<项目>/.sub_cache`，可用 `ASIP_SUB_CACHE_DIR` 覆盖（测试用）。"""
    return Path(os.environ.get("ASIP_SUB_CACHE_DIR")
                or (Path(__file__).resolve().parent / ".sub_cache"))


def _cache_key(url: str) -> str:
    """源 → 稳定的文件名。**不能**把 URL 直接当文件名：里面就有 token。"""
    import hashlib
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _read_cache(url: str) -> tuple[str, float] | None:
    """上一轮成功拉到的正文。返回 (正文, 落盘时刻)；没有则 None。"""
    p = sub_cache_dir() / (_cache_key(url) + ".json")
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    raw, ts = d.get("raw"), d.get("ts")
    return (raw, float(ts)) if isinstance(raw, str) else None


def _write_cache(url: str, raw: str) -> None:
    """把成功拉到的正文落一份。写失败不影响本次刷新 —— 缓存是加分项。"""
    if not raw:
        return
    from asip.atomic_io import write_json as _wj
    p = sub_cache_dir() / (_cache_key(url) + ".json")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _wj(str(p), {"ts": time.time(), "raw": raw}, mode=0o600)
    except OSError as e:
        print(f"  ⚠ 源快照写不进去（{e}），下次该源失败时会丢节点",
              file=sys.stderr)


def _fetch_or_cache(url: str) -> tuple[str, str]:
    """抓订阅；失败时退回上一轮快照。返回 (正文, 来源说明)。

    WHY 必须有这层：订阅链接可能是**一次性的**（服务端发一次就失效）。
    实测 2026-11-04：`iqiyi29ji` 源 15:39 还能拉到 49 个节点，15:48 再拉
    就是 HTTP 403，那一轮刷新直接把 49 个节点从生产配置里删掉了 —— 旧逻辑
    遇错 `continue`，等于「一次拉取失败 = 该源所有节点消失」。

    缓存是**按源**分的：一个源失败不连累其它源；所有源都失败时，回退到
    各自快照，配置至少不会比上一轮更差。
    """
    try:
        raw = _fetch_raw(url)
        _write_cache(url, raw)
        return raw, "ok"
    except Exception as e:
        cached = _read_cache(url)
        detail = redact_secrets(str(e))[:100]
        if not cached:
            raise RuntimeError(f"订阅拉取失败且无快照: {detail}") from None
        raw, ts = cached
        age = (time.time() - ts) / 3600
        src = source_label(url)
        print(f"  ♻ {src} 拉取失败（{detail}），改用 {age:.1f}h 前的快照",
              file=sys.stderr)
        return raw, f"cache:{age:.1f}h"


def fetch_and_decode_sub(url: str) -> list[str]:
    """拉订阅，base64 解码，返回原始 URL 列表
    支持：标准 base64 订阅、明文 URL 列表、Clash YAML 配置（提取 proxy 节点）
    """
    return _decode_sub(_fetch_raw(url))


def _decode_sub(raw: str) -> list[str]:
    """把一份订阅正文解码成 URI 列表（base64 / Clash YAML / 明文）。"""
    import yaml

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

# 解析器用的两字母缩写。sing-box 源要生成**同一套** tag，否则同一个节点从
# 两种源进来会得到两个 tag（同一台机器被当成两条线）。
_SCHEME_ABBR = {"vless": "vl", "vmess": "vm", "trojan": "tr",
                "shadowsocks": "ss", "hysteria2": "h2", "anytls": "any"}

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

def _tls_alpn(p: dict) -> list[str] | None:
    """从订阅参数里取 alpn。

    机场把 `alpn: [h2, http/1.1]` 写在**订阅的 Clash 版本**里，base64 版本
    不带。anytls 服务端按 alpn 选协议，客户端不声明就协商不出可用的那个，
    连接会卡在握手后静默超时（日志只有 INFO 没有 ERROR，看着像节点死了）。
    """
    raw = p.get("alpn")
    if not raw:
        return None
    vals = [v for v in (raw[0] if isinstance(raw, list) else raw).split(",") if v]
    return vals or None


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
        # 全新部署还没生成过：合法形态，主配置里就是没有 WARP 出口。
        return []
    except (OSError, ValueError) as e:
        # **不能跳过。** 文件在却读不了（典型：属主被 root 抢走，600），
        # 生成一份「没有 WARP 出口」的配置仍然合法，rc 为 0，于是它会被
        # promote 上线 —— opencode 流量静默落到机场池，selector 整条消失，
        # daemon 之后每次换线都是 status=404。2026-11-03 实测：远端因此
        # 2 小时 24 分无 WARP 出口，日志里一条记录都没有。
        #
        # 这里必须让整个解析失败（rc≠0），由调用方保留旧配置。
        raise SystemExit(f"✗ WARP 出口配置存在但读取失败，拒绝生成不含 WARP 的配置: {e}")
    items = data if isinstance(data, list) else [data]
    out = [o for o in items if isinstance(o, dict) and o.get("tag") and o.get("server_port")]
    if len(out) != len(items):
        print(f"  dropped {len(items) - len(out)} invalid WARP entries", file=sys.stderr)
    return out



def _load_dormant_outbounds() -> list[dict]:
    """休眠节点：`dormant-outbounds.json` 里那份手工固化的旧入口快照。

    它**不是**「订阅源挂了才用」的备份：2026-11-04 实测，okz 现在仍供这批后端，
    只是入口域名从 `bilibili-image-cdn.juhazf.cn:<5xxxx>` 换成了 `cn.bing.com:443`
    / `us.bing.com:443`。按 (SNI, 协议) 对齐后 63/63 密码相同 —— 也就是**同一批
    节点的旧入口**。留着它当 bing 前置被墙时的备胎。

    （文件 2026-10-28 15:52 手工固化后再没变过，okz 再换入口不会自动跟进。）

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


def _build_inbounds(listen_port: int, include_lan: bool = True) -> list[dict]:
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
    if not lan_port or not include_lan:
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
                include_h2: bool = False, stats_out=None,
                slot_index: int = 0, include_lan: bool = True) -> dict:
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
        print(f"  + {len(dormant)} 个休眠节点（okz 旧入口快照，非当前订阅）",
              file=sys.stderr)
    outbounds = nodes[:] + dormant + warps
    outbounds.append({"type": "direct", "tag": "direct"})
    # 机场自有机型（bing 前置 / okzdns / acczzz / hi2026）必须用机场 DoH 解析，
    # 不能在节点上省略 domain_resolver 让它退回 route.default_domain_resolver:
    # 后者是系统 DNS，`cn.bing.com` 会被解到真 Bing（202.89.233.x），握手时
    # 证书是 Microsoft 的而不是 `*.okzdns.com`，报 `not tw-mik-55.okzdns.com`。
    # 2026-11-01 实测：补上后 119 个节点从全不通变成可连（对照实验见 TEST_PLAN v1.57）。
    for o in outbounds:
        srv = str(o.get("server") or "")
        if any(srv.endswith(s) for s in AIRPORT_DNS_SUFFIXES):
            o.setdefault("domain_resolver", AIRPORT_DNS_TAG)

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
    inbounds = _build_inbounds(listen_port, include_lan=include_lan)
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
                "default": warps[slot_index % len(warps)]["tag"] if warps else "",
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
                # 机场 DoH：只用来解析节点自己的域名（见 AIRPORT_DNS_SUFFIXES）。
                # domain_resolver 必须指向 dns-default —— 它自己的 host 名
                # 只能靠系统 DNS 解，指回自己会 circular（实测 FATAL）。
                {"type": "https", "tag": AIRPORT_DNS_TAG, "server": "custom.juhazf.cn",
                 "path": "/inst", "domain_resolver": "dns-default"},
                {"type": "local", "tag": "dns-default"},
            ],
            "rules": [
                # 机场域名必须先于 opencode 规则：这些域名解析到前置 IP 才连得通，
                # 落到系统 DNS 就是「真 bing + 假证书」。
                {"domain_suffix": list(AIRPORT_DNS_SUFFIXES),
                 "server": AIRPORT_DNS_TAG, "strategy": "ipv4_only"},
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
                or config.subscription_urls() + config.extra_subscription_urls())
    if not sub_urls:
        print("ERROR: no subscription URLs provided (use --urls or --url)", file=sys.stderr)
        sys.exit(1)

    all_urls: list[tuple[str, str]] = []      # (uri, 来源 URL)
    all_nodes: list[tuple[dict, str]] = []    # (出站, 来源 URL)
    for url in sub_urls:
        safe_url = url.split("@")[-1] if "@" in url else url[:60]
        print(f"fetching subscription from {safe_url}...")
        try:
            raw, origin = _fetch_or_cache(url)
        except Exception as e:
            # 异常串可能带完整 URL（含 token），stderr 会进 daemon.log。
            print(f"  ✗ 源失败，跳过: {type(e).__name__}: "
                  f"{redact_secrets(str(e))[:120]}", file=sys.stderr)
            continue
        if origin != "ok":
            # 快照救回了这个源：节点不会消失，但这一轮的数据是陈的，
            # 日志里必须能看出来（否则「为什么机场换了节点这边没变」无从追起）。
            print(f"  · {source_label(url)} 本轮用快照（{origin.split(':', 1)[1]}）",
                  file=sys.stderr)
        # sing-box JSON 源（flag=sing）：出站已经可直接入池，不经过 URL 往返。
        direct = singbox_outbounds(raw)
        if direct:
            print(f"  got {len(direct)} sing-box outbounds")
            all_nodes.extend((n, url) for n in direct)
            continue
        try:
            urls = _decode_sub(raw)
        except Exception as e:
            print(f"  ✗ 源解析失败，跳过: {type(e).__name__}: "
                  f"{redact_secrets(str(e))[:120]}", file=sys.stderr)
            continue
        print(f"  got {len(urls)} raw URLs")
        all_urls.extend((u, url) for u in urls)

    print(f"total raw URLs from all sources: {len(all_urls)}")

    nodes = []
    seen_urls: set[str] = set()
    seen_tags: set[str] = set()
    # 跨格式身份：同一台机器可能从 URI 源和 sing-box 源各来一次（同一个 token
    # 换 flag 就是两份订阅）。两者 tag 已统一成 `host:port-协议`，但只靠 tag
    # 去重会在 sni/transport 不同的情况下误合并，靠 URL 去重又认不出 JSON 源。
    # `_node_fp`（server/port/type/transport/sni）正好是同一台机器的稳定身份。
    seen_fps: set[str] = set()
    # 节点 → 来源。value 是 dict 而不是字符串：报表要同时显示「哪个订阅」
    # 和「机场给这个节点起的名字」（`🇸🇬新加坡01-0.1倍`），只存 URL 标签
    # 不够 —— 名字是人工核对「这条线到底是哪台机器」的唯一依据。
    sources: dict[str, dict] = {}
    filter_sub = args.filter.lower() if args.filter else None
    for u, src in all_urls:
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
        seen_fps.add(_node_fp(n))
        name = urllib.parse.unquote(u.split("#", 1)[1]) if "#" in u else ""
        sources[n["tag"]] = {"sub": sub_name(src), "label": source_label(src),
                             "name": display_name(name)}
        nodes.append(n)

    # sing-box 源直接给的出站：身份去重与 tag 撞名规则与上面一致（tag 撞名
    # 不能整条丢 —— 那台机器上可能只有 path/入口不同）。指纹用 _node_fp：
    # 它由 server/port/type/transport/sni 组成，与订阅到达顺序无关，正好当身份。
    for n, src in all_nodes:
        if filter_sub and filter_sub not in str(n["tag"]).lower():
            continue
        ident = _node_fp(n)
        if ident in seen_fps:
            continue
        if n["tag"] in seen_tags:
            scheme = str(n["tag"]).rsplit("-", 1)[-1]
            stem = f"{n['server']}:{n['server_port']}"
            i = 2
            while f"{stem}~{i}-{scheme}" in seen_tags:
                i += 1
            n["tag"] = f"{stem}~{i}-{scheme}"
        seen_fps.add(ident)
        seen_tags.add(n["tag"])
        sources[n["tag"]] = {"sub": sub_name(src), "label": source_label(src),
                             "name": display_name(str(n.get("_display") or ""))}
        nodes.append(n)

    print(f"parsed {len(nodes)} usable nodes (deduplicated)")
    if not nodes:
        # **先判空再写 sources**：写在判断之前的话，一次失败的解析会留下一份
        # 「本轮有归属」的台账而配置根本没换新 —— 下游会把旧配置的节点配上
        # 新台账的名字，比没有台账更误导。
        print("ERROR: no nodes parsed, aborting")
        sys.exit(1)

    # `_display` 是给 sources 台账用的旁路字段，sing-box 不认未知键 ——
    # 带进配置会让 `sing-box check` 直接 FATAL，整轮刷新被护栏拒掉。
    for n in nodes:
        n.pop("_display", None)
    slots = config.consumer_slots()
    # daemon 以 `<final>.tmp` 调用：六份配置都写到各自最终路径的 `.tmp`，由
    # daemon 统一 check、统一 promote。不能像旧实现那样让 slot-2..6 直接写最终
    # 路径 —— 主配置被护栏拒收时，槽位文件已经上线，主/槽版本当场劈叉。
    drafting = str(args.out).endswith(".tmp")
    cfg = None
    if slots:
        for i, slot in enumerate(slots):
            final_path = config.consumer_slot_config_path(args.out, i)
            out_path = final_path + ".tmp" if drafting else final_path
            cfg = make_config(nodes, slot["port"], slot["clash_api_port"],
                              args.secret or get_singbox_secret(),
                              include_h2=args.include_h2,
                              stats_out=(args.out if i == 0 else None),
                              slot_index=i, include_lan=(i == 0))
            write_json(out_path, cfg, mode=0o600, indent=2)
    else:
        out_path = args.out + ".tmp" if drafting else args.out
        cfg = make_config(nodes, args.port, args.clash_port,
                          args.secret or get_singbox_secret(),
                          include_h2=args.include_h2, stats_out=args.out)
        write_json(out_path, cfg, mode=0o600, indent=2)

    # 来源台账与配置同目录、同一生命周期：sing-box 不认这个字段，单独落盘
    # 才不会污染配置，也不会在 promote 校验时被当成未知键。
    # **写在配置之后**：反过来一旦配置写失败，盘上会留下一份「本轮归属」的
    # 台账而配置还是旧的，下游会把旧节点配上新名字 —— 比没有台账更误导。
    # 只留**真正写进配置**的节点：`make_config` 默认排除 hysteria2，全记下来
    # 会让报表以为那些节点在池里（实测 14 条 h2 只在台账、不在配置）。
    in_cfg = {o.get("tag") for o in cfg.get("outbounds") or []
              if o.get("server") and o.get("server_port")}
    write_json(str(args.out) + ".sources.json",
               {t: v for t, v in sources.items() if t in in_cfg},
               mode=0o600, indent=None)
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
