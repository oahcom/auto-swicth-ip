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
def test_loopback_nodes_filtered():
    """订阅源套餐过期时会返回 ss://...@127.0.0.1:6666 占位节点，必须过滤。"""
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
    import logging

    class R:
        returncode = 1
        stderr = "NameError: name 'parse_vless' is not defined"
        stdout = ""

    lg = logging.getLogger("auto-switch-ip")
    saved_handlers = lg.handlers[:]
    lg.handlers = []
    monkeypatch.setattr(daemon, "_logger", None)          # 强制重新 _init_logger
    monkeypatch.setitem(daemon.CFG, "log_file", str(tmp_path / "daemon.log"))

    try:
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
        monkeypatch.setattr(daemon, "restart_singbox",
                            lambda: pytest.fail("不应重启 sing-box"))
        assert daemon.refresh_subscription() is False

        text = (tmp_path / "daemon.log").read_text()
        assert "订阅解析失败" in text, f"日志未落在 tmp: {text!r}"
    finally:
        for h in lg.handlers:
            h.close()
        lg.handlers = saved_handlers


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
