#!/usr/bin/env python3
"""订阅解析确定性单元测试（无网络、<5s）。

覆盖 2026-10-16 订阅换 token 时发现的 5 个阻断性 bug 的回归护栏：
- parse_vless 缺失 → PARSERS 字面量在导入时 NameError，整个解析脚本崩溃
- 单个订阅源失败（403/超时）→ 中断全部源，配置永远写不出去
- refresh_subscription 不检查 returncode → 崩溃报成「✓ 刷新完毕」
- parse_trojan 写 transport.type="tcp" → sing-box check FATAL，起不来
- 生成的配置含非法 transport → sing-box 无法启动
"""
import subprocess

import pytest

import daemon
import parse_subscription as ps

# sing-box 只接受这些 transport，显式 "tcp" 是非法值
VALID_TRANSPORTS = {"ws", "grpc", "http", "quic", "httpupgrade"}


@pytest.fixture(autouse=True)
def _isolate_production_log(tmp_path, monkeypatch):
    """daemon.log 重定向到 tmp。

    刷新路径会调 daemon.log()，而 logger 上的 RotatingFileHandler 指向生产
    daemon.log（O_APPEND）。实测 test_shrunken_refresh_rejected 的夹具
    （19 节点 → 1）曾以 "刷新节点数骤降 (19 → 1)" 的形式写进生产日志，
    读日志时会把它当成真实订阅故障排查。
    """
    import logging
    lg = logging.getLogger("auto-switch-ip")
    saved = lg.handlers[:]
    lg.handlers = []
    monkeypatch.setattr(daemon, "_logger", None)
    monkeypatch.setitem(daemon.CFG, "log_file", str(tmp_path / "daemon.log"))
    yield
    for h in lg.handlers:
        h.close()
    lg.handlers = saved


def _bad_transports(config: dict) -> list[str]:
    bad = []
    for o in config["outbounds"]:
        t = o.get("transport", {}).get("type")
        if t and t not in VALID_TRANSPORTS:
            bad.append(f"{o['tag']}:{t}")
    return bad


# ============= 1. 解析器完整性（防 NameError 崩溃） =============
def test_all_parsers_are_callable():
    assert ps.PARSERS, "PARSERS 不应为空"
    for scheme, fn in ps.PARSERS.items():
        assert callable(fn), f"{scheme} 的解析器不可调用: {fn!r}"


def test_parse_vless_is_defined():
    """vless 是订阅常见协议；缺失会让整个模块导入失败。"""
    assert callable(getattr(ps, "parse_vless", None))


def test_parse_vless_tls_and_reality():
    url = ("vless://uuid-1@h.example.com:443"
           "?type=tcp&security=reality&sni=x.com&pbk=PBKEY&sid=ab12&fp=chrome#node")
    out = ps.parse_vless(url)
    assert out["type"] == "vless"
    assert out["uuid"] == "uuid-1"
    assert out["tls"]["enabled"] is True
    assert out["tls"]["reality"]["public_key"] == "PBKEY"
    assert out["tls"]["reality"]["short_id"] == "ab12"
    assert "transport" not in out, "tcp 不应生成 transport"


def test_parse_vless_ws_transport():
    url = "vless://uuid-2@h.example.com:443?type=ws&security=tls&path=/p&host=a.com#n"
    out = ps.parse_vless(url)
    assert out["transport"]["type"] == "ws"
    assert out["transport"]["path"] == "/p"
    assert out["transport"]["headers"]["Host"] == "a.com"


def test_ws_path_decoded_once():
    """parse_qs 已 URL 解码；再调 unquote 会把 %252F 二次解码成 /。"""
    url = "vless://u@h.example.com:443?type=ws&path=%2Fa%252Fb#n"
    out = ps.parse_vless(url)
    assert out["transport"]["path"] == "/a%2Fb", \
        f"二次解码: 期望 /a%2Fb (原文 %2Fa%252Fb), 实际 {out['transport']['path']!r}"


# ============= 2. parse_trojan transport（曾导致 sing-box FATAL） =============
@pytest.mark.parametrize("query", ["", "?type=tcp"])
def test_parse_trojan_no_tcp_transport(query):
    out = ps.parse_trojan(f"trojan://pw@h.example.com:443{query}#n")
    assert "transport" not in out, "tcp 是非法 transport，会导致 sing-box 启动 FATAL"


def test_parse_trojan_ws_transport():
    out = ps.parse_trojan("trojan://pw@h.example.com:443?type=ws&path=/p&host=a.com#n")
    assert out["transport"]["type"] == "ws"
    assert out["transport"]["headers"]["Host"] == "a.com"


# ============= 2b. shadowsocks plugin_opts（sing-box 要求字符串） =============
def test_ss_plugin_opts_is_string():
    """sing-box 的 plugin_opts 是分号拼接字符串；传 dict 会 FATAL。"""
    out = ps.parse_shadowsocks(
        "ss://YWVzLTI1Ni1nY206cHc@h.example.com:8388"
        "?plugin=obfs-local%3Bobfs%3Dhttp%3Bobfs-host%3Dx.com#n")
    assert out["plugin"] == "obfs-local"
    assert out["plugin_opts"] == "obfs=http;obfs-host=x.com"
    assert isinstance(out["plugin_opts"], str), \
        f"plugin_opts 必须是 str, 实际 {type(out['plugin_opts']).__name__}"


# ============= 2c. vmess base64-JSON（v2rayN/Clash 主流格式） =============
def test_vmess_base64_json_parsed():
    """base64-JSON 是 vmess 主流格式；不识别会把整个串当 hostname。"""
    import base64 as _b64
    import json as _json
    payload = {"v": "2", "ps": "东京", "add": "1.2.3.4", "port": "443",
               "id": "12345678-1234-1234-1234-123456789abc", "aid": "0",
               "net": "ws", "host": "a.com", "path": "/p", "tls": "tls", "sni": "x.com"}
    url = "vmess://" + _b64.b64encode(_json.dumps(payload).encode()).decode()
    out = ps.parse_vmess(url)
    assert out["server"] == "1.2.3.4", f"server 应为 1.2.3.4, 实际 {out['server']!r}"
    assert out["server_port"] == 443
    assert out["uuid"] == payload["id"]
    assert out["tls"]["enabled"] is True
    assert out["transport"]["type"] == "ws"


def test_vmess_standard_url_format_still_works():
    out = ps.parse_vmess(
        "vmess://uuid-1@h.example.com:443?alterId=0&security=auto&type=ws&path=/p&host=a.com#n")
    assert out["server"] == "h.example.com"
    assert out["uuid"] == "uuid-1"


def test_vmess_garbage_returns_empty():
    """解析失败必须返回空 dict（被 make_config 丢弃），不能产出半成品节点。"""
    out = ps.parse_vmess("vmess://bm90LWpzb24")
    assert not out.get("server"), f"垃圾输入不应产出 server, 实际 {out.get('server')!r}"


def test_make_config_drops_incomplete_nodes():
    """残缺节点（无 server/port）必须被丢弃，否则 daemon 会拿它轮换、白白浪费切换。"""
    good = ps.parse_trojan("trojan://pw@a.example.com:443#t")
    bad = {"type": "vmess", "tag": "garbage", "server": "", "server_port": None}
    cfg = ps.make_config([good, bad], listen_port=7890, clash_port=9090)
    tags = [o["tag"] for o in cfg["outbounds"]]
    assert "garbage" not in tags, "残缺节点泄漏进配置"
    assert "a.example.com:443-tr" in tags, "合法节点被误删"


# ============= 2d. reality 块以 pbk 为准 =============
def test_reality_from_clash_source_kept():
    """Clash-YAML 源的 reality 节点被转成 security=tls 但仍带 pbk，不能丢 reality 块。"""
    proxy = {"type": "vless", "name": "r", "server": "1.2.3.4", "port": 443, "uuid": "u",
             "tls": True, "network": "tcp", "servername": "x.com",
             "reality-opts": {"public-key": "REALKEY", "short-id": "ab"}}
    out = ps.parse_vless(ps._clash_proxy_to_url(proxy))
    assert "reality" in out["tls"], "Clash 源 reality 节点丢失 reality 块"
    assert out["tls"]["reality"]["public_key"] == "REALKEY"


def test_reality_without_pbk_omitted():
    """security=reality 但无 pbk 时不得下发 reality 块 —— sing-box 会 FATAL。"""
    out = ps.parse_vless("vless://u@h.example.com:443?type=tcp&security=reality&sni=x.com#n")
    assert "reality" not in out.get("tls", {}), "空 pbk 会致 sing-box invalid public_key"


# ============= 3. 单源失败不中断全流程 =============
def test_dead_source_still_writes_config(tmp_path):
    """真实端到端（离线）：一个死源 + 一个本地活源 → 脚本 rc=0 且写出配置。

    回归点：此前单个源 403 会中断全部源，配置永远写不出去。
    """
    import base64
    import http.server
    import socketserver
    import threading
    import json as _json

    sub = base64.b64encode(
        b"trojan://pw@hk.example.com:443?sni=x.com#alive-node\n"
    ).decode()
    (tmp_path / "sub").write_text(sub)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(tmp_path), **k)

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Quiet) as srv:
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        out = tmp_path / "cfg.json"
        r = subprocess.run(
            ["python3", ps.__file__,
             "--out", str(out),
             "--urls",
             "http://127.0.0.1:1/dead-sub",          # 必然连接失败
             f"http://127.0.0.1:{port}/sub"],        # 活源
            capture_output=True, text=True, timeout=60)

    assert r.returncode == 0, f"单源失败不应中断全流程: {r.stderr[-400:]}"
    assert out.exists(), "活源存在时必须写出配置"
    tags = [o["tag"] for o in _json.loads(out.read_text())["outbounds"]]
    assert any("hk.example.com" in t for t in tags), f"活源节点丢失: {tags}"


# ============= 4. refresh_subscription 检查 returncode =============
def test_loopback_nodes_filtered(monkeypatch):
    """订阅源套餐过期时会返回 ss://...@127.0.0.1:6666 占位节点，必须过滤。"""
    monkeypatch.setattr(ps, "_load_warp_outbounds", lambda: [])  # 本地 WARP 出口也是 127.0.0.1，隔离掉
    assert ps._is_loopback("127.0.0.1") and ps._is_loopback("::1") and ps._is_loopback("localhost")
    assert not ps._is_loopback("1.2.3.4") and not ps._is_loopback("hk.example.com")
    good = ps.parse_trojan("trojan://pw@a.example.com:443#t")
    loop = ps.parse_shadowsocks("ss://YWVzLTEyOC1nY206cHc@127.0.0.1:6666#expired")
    cfg = ps.make_config([good, loop], listen_port=7890, clash_port=9090)
    assert not any("127.0.0.1" in o.get("server", "") for o in cfg["outbounds"])


def test_count_outbounds():
    """节点计数用于判断刷新是否骤降（订阅源故障时返回占位节点）。"""
    import json as _json
    assert daemon._count_outbounds("/nonexistent/x.json") == 0


def test_shrunken_refresh_rejected(monkeypatch, tmp_path):
    """节点数骤降时必须拒绝覆盖配置，保留现有节点池。

    实测事故：dasho 套餐过期返回占位节点，刷新把 56 节点池覆盖成 1 个无效节点，
    代理直接断掉。
    """
    import json as _json
    cfg = tmp_path / "cfg.json"
    cfg.write_text(_json.dumps({"outbounds": [
        {"type": "trojan", "tag": f"n{i}", "server": f"1.2.3.{i}", "server_port": 443}
        for i in range(1, 20)] + [{"type": "direct", "tag": "direct"}]}))
    monkeypatch.setitem(daemon.CFG["singbox"], "config_file", str(cfg))

    def fake_run(cmd, **k):
        out = cmd[cmd.index("--out") + 1]
        with open(out, "w") as f:                       # 模拟故障源产出 1 个节点
            _json.dump({"outbounds": [{"type": "ss", "tag": "x",
                                       "server": "1.2.3.4", "server_port": 1}]}, f)
        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(daemon, "restart_singbox", lambda: pytest.fail("不应重启 sing-box"))
    original = cfg.read_text()
    assert daemon.refresh_subscription() is False
    assert cfg.read_text() == original, "骤降的配置覆盖了现有节点池"
    assert not (tmp_path / "cfg.json.tmp").exists(), "临时文件未清理"


def test_parse_failure_makes_refresh_return_false(monkeypatch, tmp_path):
    """rc != 0 时必须返回 False，不得声称刷新成功。

    同时验证不污染生产 daemon.log：refresh_subscription 会写日志，而
    logging.getLogger("auto-switch-ip") 上已挂着指向生产 daemon.log 的
    RotatingFileHandler（O_APPEND）。只 addHandler 会多写一份到生产日志，
    导致下游 grep 到假故障。这里整体接管 handlers + 重定向 log_file。
    """
    class R:
        returncode = 1
        stderr = "NameError: name 'parse_vless' is not defined"
        stdout = ""

    monkeypatch.setattr(daemon, "_logger", None)          # 强制重新 _init_logger
    # config_file 必须一并重定向：refresh 失败路径会 _discard(config_file + ".tmp")，
    # 不重定向就会删掉生产 singbox-config.json.tmp（含刚拉取的节点凭据）
    monkeypatch.setitem(daemon.CFG["singbox"], "config_file", str(tmp_path / "cfg.json"))

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(daemon, "restart_singbox",
                        lambda: pytest.fail("不应重启 sing-box"))
    assert daemon.refresh_subscription() is False

    text = (tmp_path / "daemon.log").read_text()
    assert "订阅解析失败" in text, f"日志未落在 tmp: {text!r}"


# ============= 5. 生成配置对 sing-box 合法 =============
def test_make_config_no_invalid_transport():
    nodes = [
        ps.parse_trojan("trojan://pw@a.example.com:443#t"),
        ps.parse_anytls("anytls://pw@b.example.com:443#a"),
        ps.parse_vless("vless://u@c.example.com:443?type=tcp&security=tls#v"),
    ]
    bad = _bad_transports(ps.make_config(nodes, listen_port=7890, clash_port=9090))
    assert not bad, f"非法 transport: {bad}"


def test_generated_config_passes_singbox_check(tmp_path):
    """端到端护栏：写出的 config 必须通过 sing-box check。"""
    binary = daemon.CFG["singbox"]["binary"]
    nodes = [ps.parse_trojan("trojan://pw@a.example.com:443#t")]
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(
        __import__("json").dumps(ps.make_config(nodes, listen_port=7890, clash_port=9090))
    )
    r = subprocess.run([binary, "check", "-c", str(cfg_file)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"sing-box check 失败: {r.stderr.strip()[:300]}"


def test_opencode_dns_resolve_rule_present():
    """opencode 域名必须本地解析（DoH）后把 IP 交给节点。

    仅保留 sniff 时 sing-box 会把域名原样透传给出口节点、本地零 DNS 查询，
    节点侧解析结果不可控。route 里必须有 resolve 动作。
    """
    nodes = [ps.parse_trojan("trojan://pw@a.example.com:443#t")]
    cfg = ps.make_config(nodes, listen_port=7890, clash_port=9090)
    actions = [r.get("action") for r in cfg["route"]["rules"]]
    assert "resolve" in actions, f"route 缺少 resolve 动作: {cfg['route']['rules']}"
    assert any(".opencode.ai" in (r.get("domain_suffix") or [])
               for r in cfg["route"]["rules"] if r.get("action") == "resolve"), \
        "resolve 动作未限定到 .opencode.ai"
    # 多 dns server 时必须显式指定默认解析器，否则 sing-box 1.12+ 拒启
    assert cfg["route"].get("default_domain_resolver"), "缺少 route.default_domain_resolver"


def test_dns_server_has_no_detour_field():
    """dns server 带 detour:\"direct\" 时 sing-box check 通过但 run 时 FATAL。"""
    nodes = [ps.parse_trojan("trojan://pw@a.example.com:443#t")]
    cfg = ps.make_config(nodes, listen_port=7890, clash_port=9090)
    dns = cfg.get("dns", {})
    assert dns.get("servers"), "缺少 dns servers"
    assert any(s.get("tag") == "dns-opencode" for s in dns["servers"]), \
        "缺少 dns-opencode server"
    for s in dns["servers"]:
        assert "detour" not in s, f"dns server {s.get('tag')} 带 detour，运行时将 FATAL"


# ============= 6. config 校验护栏（promote 前用 sing-box check） =============
def test_config_check_rejects_invalid_pbk(tmp_path):
    """非法 reality pbk 会让整个配置 FATAL；guard 必须在覆盖前拦下。"""
    import json as _json
    n = ps.parse_vless("vless://u@h.example.com:443?type=tcp&security=reality&pbk=not_base64!!#n")
    cfg = ps.make_config([n], listen_port=7890, clash_port=9090)
    f = tmp_path / "cfg.json"
    f.write_text(_json.dumps(cfg))
    assert daemon._config_check_error(str(f)), "非法 pbk 的配置应被 sing-box check 拒绝"


def test_config_check_accepts_valid(tmp_path):
    import json as _json
    n = ps.parse_trojan("trojan://pw@a.example.com:443#t")
    cfg = ps.make_config([n], listen_port=7890, clash_port=9090)
    f = tmp_path / "ok.json"
    f.write_text(_json.dumps(cfg))
    assert daemon._config_check_error(str(f)) == "", "合法配置不应被拒"


def test_transport_for_all_valid_types():
    """http/quic/httpupgrade 曾被静默丢弃 —— 节点入池但每次连接都失败。"""
    for net in ps.VALID_TRANSPORTS:
        t = ps._transport_from_query({"type": [net]})
        assert t and t["type"] == net, f"{net} transport 丢失"


def test_transport_none_for_invalid_types():
    """tcp 是非法 transport（sing-box FATAL）；未知类型也不应下发。"""
    for net in ("tcp", "weird", ""):
        assert ps._transport_from_query({"type": [net]}) is None


def test_is_loopback_handles_non_string():
    """vmess JSON 里 add 可能是数字；不做类型防护会让 make_config 整体崩溃。"""
    assert ps._is_loopback(1234567) is False
    assert ps._is_loopback(None) is False
    cfg = ps.make_config([{"type": "vmess", "tag": "t", "server": 1234567, "server_port": 443,
                           "uuid": "u", "alter_id": 0, "security": "auto"}],
                         listen_port=7890, clash_port=9090)
    assert cfg["outbounds"], "数字 server 不应导致 make_config 崩溃"


# ============= 7. 并发刷新与 Clash transport 转发 =============
def test_refresh_locked_is_nonblocking():
    """已有刷新在跑时第二次调用应立刻返回 False，而不是排队等锁。"""
    import threading
    import time as _time
    with daemon._REFRESH_LOCK:
        t0 = _time.time()
        assert daemon.refresh_subscription() is False
        assert _time.time() - t0 < 0.5, "非阻塞获取锁失败（阻塞等待了）"


def test_clash_transport_roundtrip():
    """Clash YAML 源的 ws/grpc 节点转 URL 后必须保留 transport。

    此前 _clash_proxy_to_url 不转发 network，节点入池但每次连接都失败。
    """
    for ptype, net in [("trojan", "ws"), ("trojan", "grpc"),
                       ("vmess", "ws"), ("vmess", "grpc"),
                       ("vless", "ws"), ("vless", "grpc")]:
        proxy = {"type": ptype, "name": "n", "server": "1.2.3.4", "port": 443, "network": net}
        proxy["password" if ptype == "trojan" else "uuid"] = "x"
        if net == "ws":
            proxy["ws-opts"] = {"path": "/p", "headers": {"Host": "a.com"}}
        else:
            proxy["grpc-opts"] = {"grpc-service-name": "svc"}
        url = ps._clash_proxy_to_url(proxy)
        out = ps.PARSERS[ptype](url)
        assert out.get("transport", {}).get("type") == net, \
            f"{ptype} {net} transport 丢失: URL={url}"


def test_stderr_reason_picks_exception_line():
    """traceback 的异常行可能不在最后（后续警告会淹没它）。"""
    s = "Traceback:\n  File x\nNameError: boom\ntrailing warning"
    assert daemon._stderr_reason(s) == "NameError: boom"
    assert daemon._stderr_reason("") == "no stderr"

# ============= 8. 局域网入站（7891） =============
def _lan_nodes():
    return [ps.parse_trojan("trojan://pw@a.example.com:443#t")]

def test_lan_inbound_absent_without_auth_file(monkeypatch, tmp_path):
    """缺认证文件时不建局域网入站 —— 无认证的 0.0.0.0 监听等于开放代理。

    同时不得留下悬空的来源护栏：那条 reject 规则按 inbound tag 匹配，入站都
    没生成时它永远不命中，是纯垃圾（sing-box check 也不会报）。
    """
    import config
    monkeypatch.setattr(config, "LAN_AUTH_FILE", str(tmp_path / "missing"))
    cfg = ps.make_config(_lan_nodes(), listen_port=7890, clash_port=9090)
    assert len(cfg["inbounds"]) == 1, f"不应有 LAN 入站: {cfg['inbounds']}"
    assert cfg["inbounds"][0]["listen"] == "127.0.0.1"
    assert not [r for r in cfg["route"]["rules"] if r.get("action") == "reject"], \
        f"入站未生成时不该有来源护栏: {cfg['route']['rules']}"

def test_lan_inbound_present_with_auth_file(monkeypatch, tmp_path):
    """认证文件齐备时，LAN 入站必须 0.0.0.0 + users，且与环回口分立。"""
    import config
    auth = tmp_path / "auth"
    auth.write_text("lanuser:testpass")
    monkeypatch.setattr(config, "LAN_AUTH_FILE", str(auth))
    cfg = ps.make_config(_lan_nodes(), listen_port=7890, clash_port=9090)
    lan = [i for i in cfg["inbounds"] if i.get("listen") == "0.0.0.0"]
    assert len(lan) == 1, f"缺少 LAN 入站: {cfg['inbounds']}"
    assert lan[0]["listen_port"] == config.CFG["singbox"]["lan_port"]
    assert lan[0]["users"] == [{"username": "lanuser", "password": "testpass"}]
    # 环回口不得被改动或要求认证
    loop = [i for i in cfg["inbounds"] if i["listen"] == "127.0.0.1"]
    assert len(loop) == 1 and "users" not in loop[0], f"环回入站不应变化: {loop}"

def test_lan_inbound_skipped_when_port_zero(monkeypatch, tmp_path):
    """lan_port=0 显式关闭，即使认证文件存在也不建。"""
    import config
    auth = tmp_path / "auth"
    auth.write_text("lanuser:testpass")
    monkeypatch.setattr(config, "LAN_AUTH_FILE", str(auth))
    monkeypatch.setitem(config.CFG["singbox"], "lan_port", 0)
    cfg = ps.make_config(_lan_nodes(), listen_port=7890, clash_port=9090)
    assert all(i["listen"] != "0.0.0.0" for i in cfg["inbounds"]), cfg["inbounds"]

def test_lan_guard_rejects_non_private_source(monkeypatch, tmp_path):
    """来源不在私网段时 reject，且规则必须排在 sniff 之前。"""
    import config
    auth = tmp_path / "auth"
    auth.write_text("u:p")
    monkeypatch.setattr(config, "LAN_AUTH_FILE", str(auth))
    cfg = ps.make_config(_lan_nodes(), listen_port=7890, clash_port=9090)
    rules = cfg["route"]["rules"]
    guard = [r for r in rules if r.get("action") == "reject"]
    assert len(guard) == 1, f"缺少来源护栏: {rules}"
    assert rules.index(guard[0]) < next(i for i, r in enumerate(rules)
                                        if r.get("action") == "sniff"), "护栏必须先于 sniff"
    g = guard[0]
    assert g["type"] == "logical" and g["mode"] == "and"
    inbound_rule = g["rules"][0]
    cidr_rule = g["rules"][1]
    assert ps.LAN_INBOUND_TAG in inbound_rule["inbound"]
    assert cidr_rule["invert"] is True
    assert set(cidr_rule["source_ip_cidr"]) >= {"192.168.0.0/16", "10.0.0.0/8"}

def test_lan_guard_absent_when_port_zero(monkeypatch):
    """关着端口时不该有悬空的 reject 规则引用不存在的入站。"""
    import config
    monkeypatch.setitem(config.CFG["singbox"], "lan_port", 0)
    cfg = ps.make_config(_lan_nodes(), listen_port=7890, clash_port=9090)
    assert not [r for r in cfg["route"]["rules"] if r.get("action") == "reject"], cfg["route"]["rules"]

def test_lan_inbound_passes_singbox_check(monkeypatch, tmp_path):
    """端到端：带 LAN 入站 + 护栏的配置必须通过 sing-box check。"""
    import config
    auth = tmp_path / "auth"
    auth.write_text("lanuser:testpass")
    monkeypatch.setattr(config, "LAN_AUTH_FILE", str(auth))
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(__import__("json").dumps(
        ps.make_config(_lan_nodes(), listen_port=7890, clash_port=9090)))
    binary = daemon.CFG["singbox"]["binary"]
    r = subprocess.run([binary, "check", "-c", str(cfg_file)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"sing-box check 失败: {r.stderr.strip()[:300]}"

def test_get_lan_auth_rejects_malformed(tmp_path, monkeypatch):
    """空串、无冒号、空口令都应返回 None，而不是造出半截凭据。"""
    import config
    for content in ("", "nopassword", ":onlypass", "onlyuser:"):
        f = tmp_path / "auth"
        f.write_text(content)
        monkeypatch.setattr(config, "LAN_AUTH_FILE", str(f))
        assert config.get_lan_auth() is None, f"格式非法应判 None: {content!r}"
    f2 = tmp_path / "auth2"
    f2.write_text("user:pa:ss")
    monkeypatch.setattr(config, "LAN_AUTH_FILE", str(f2))
    assert config.get_lan_auth() == ("user", "pa:ss"), "口令允许含冒号，首个冒号分隔"
