"""WARP-over-proxy 线路的配置生成契约。

这些断言保护的是「能起来」而不是「能换 IP」：换 IP 依赖 Cloudflare 的会话
分配，只能在真机上实测（见 docs/warp-wg-over-proxy.md 的数据）。
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import config as cfg_mod
import warp_wg


PROFILE = """\
[Interface]
PrivateKey = YXNpcC10ZXN0LWZpeHR1cmUtcHJpdmF0ZS1rZXktMzI=
Address = 172.16.0.2/32, 2606:4700:110:8f68:3de8:2497:da83:b4df/128
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = YXNpcC10ZXN0LWZpeHR1cmUtcGVlci1wdWJsaWMta2V5
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = engage.cloudflareclient.com:2408
"""


def _node(tag: str, host: str = "1.2.3.4") -> dict:
    return {
        "type": "vless", "tag": tag, "server": host, "server_port": 443,
        "uuid": "11111111-1111-1111-1111-111111111111",
        "tls": {"enabled": True, "server_name": "example.com"},
    }


def test_read_profile_parses_keys_and_addresses(tmp_path):
    p = tmp_path / "profile.conf"
    p.write_text(PROFILE)
    prof = warp_wg.read_profile(p)
    assert prof["private_key"].startswith("YXNpcC10")
    assert prof["peer_public_key"].startswith("YXNpcC10")
    # IPv6 地址必须原样保留，截断会让 endpoint 起不来
    assert "2606:4700:110:8f68:3de8:2497:da83:b4df/128" in prof["address"]


def test_read_profile_rejects_incomplete(tmp_path):
    p = tmp_path / "bad.conf"
    p.write_text("[Interface]\nAddress = 172.16.0.2/32\n")
    with pytest.raises(ValueError):
        warp_wg.read_profile(p)


@pytest.mark.parametrize("peer,expect", [
    ("162.159.192.1:2408", ("162.159.192.1", 2408)),
    ("[2606:4700:103::2]:2408", ("2606:4700:103::2", 2408)),
])
def test_split_peer(peer, expect):
    assert warp_wg._split_peer(peer) == expect


def test_build_line_config_routes_everything_through_detour():
    prof = {"private_key": "k", "address": ["172.16.0.2/32"],
            "peer_public_key": "pub"}
    node = _node("n1")
    c = warp_wg.build_line_config(prof, 1110, node, "162.159.192.1:2408")

    assert c["inbounds"][0]["listen_port"] == 1110
    ep = c["endpoints"][0]
    # 本机 UDP 被 ACL 拦，detour 缺失等于直连超时
    assert ep["detour"] == "n1"
    assert ep["type"] == "wireguard"
    assert ep["peers"][0]["allowed_ips"] == ["0.0.0.0/0", "::/0"]
    assert c["route"]["final"] == "warp"
    # 节点 server 是域名；DNS 若也 detour 到该节点会形成解析环路，
    # 所以必须用本机解析器（与主配置 dns-default 同形态）
    assert c["dns"]["servers"][0]["type"] == "local"
    assert "detour" not in c["dns"]["servers"][0]
    assert c["route"]["default_domain_resolver"] == {"server": "dns-default"}
    assert c["outbounds"][0]["tag"] == "n1"


def test_dns_strategy_selects_egress_family():
    """出口地址族由 DNS 偏好决定：v6 每次都换新，v4 由 colo 决定。

    用 ipv4_only / ipv6_only 会让只有单族记录的目标直接不可达（opencode.ai
    同时有 A 和 AAAA），所以只能是 prefer_*。
    """
    prof = {"private_key": "k", "address": ["172.16.0.2/32"],
            "peer_public_key": "pub"}
    node = _node("n1")
    v6 = warp_wg.build_line_config(prof, 1110, node, "162.159.192.1:2408", family="v6")
    v4 = warp_wg.build_line_config(prof, 1110, node, "162.159.192.1:2408", family="v4")
    assert v6["dns"]["strategy"] == "prefer_ipv6"
    assert v4["dns"]["strategy"] == "prefer_ipv4"


def test_generated_config_passes_singbox_check(tmp_path):
    sb = cfg_mod.CFG["singbox"]["binary"]
    if not os.access(sb, os.X_OK):
        pytest.skip("sing-box 不可执行")
    prof = warp_wg.read_profile(_write(tmp_path / "p.conf", PROFILE))
    cfg = warp_wg.build_line_config(prof, 1110, _node("n1"), "162.159.192.1:2408")
    f = tmp_path / "line.json"
    f.write_text(json.dumps(cfg))
    r = subprocess.run([sb, "check", "-c", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_assign_nodes_spreads_across_hosts():
    nodes = [
        _node("a", "pq.aws48.yydjc.top"), _node("b", "pq.aws49.yydjc.top"),
        _node("c", "unamecf.xn--ghqu5fm27b67w.com"),
    ]
    picked = warp_wg.assign_nodes(nodes, 2)
    assert len(picked) == 2
    # 同主机不同端口不算多样性：首选必须来自不同分组
    assert warp_wg.node_group(picked[0]) != warp_wg.node_group(picked[1])


def test_assign_nodes_rejects_short_pool():
    with pytest.raises(SystemExit):
        warp_wg.assign_nodes([_node("a", "a.example.com")], 2)


def test_rotate_switches_node_then_restarts(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"lines": {
        "WARP-WG-1": {"unit": "warp-wg-1.service", "port": 1110,
                      "family": "v4",
                      "node": "pq.aws48.yydjc.top:443-vl",
                      "rotation": ["pq.aws48.yydjc.top:443-vl",
                                   "unamecf.xn--x.com:443-vl"]}}}))
    pool = [_node("pq.aws48.yydjc.top:443-vl", "pq.aws48.yydjc.top"),
            _node("unamecf.xn--x.com:443-vl", "unamecf.xn--x.com")]
    calls = []
    monkeypatch.setattr(warp_wg, "state_path", lambda: state)
    monkeypatch.setattr(warp_wg, "load_nodes", lambda: pool)
    monkeypatch.setattr(warp_wg, "write_line",
                        lambda i, n, **kw: calls.append(("write", n["tag"])))
    monkeypatch.setattr(warp_wg, "_run", lambda argv: calls.append(("run", argv[-1])) or "")

    warp_wg.rotate(1)

    assert calls[0][0] == "write" and calls[0][1] != "pq.aws48.yydjc.top:443-vl"
    # 先写配置再重启：顺序反了会在旧配置上白重启一次
    assert calls[1] == ("run", "warp-wg-1.service")
    saved = json.loads(state.read_text())["lines"]["WARP-WG-1"]["node"]
    assert saved == calls[0][1]


def test_count_env_drives_members_and_names(monkeypatch):
    """WARP_WG_COUNT 是唯一开关：>0 时 tag/端口/单元名三处必须一致。"""
    monkeypatch.setenv("WARP_WG_COUNT", "3")
    cfg = importlib.reload(cfg_mod)
    try:
        wg = cfg.CFG["warp"]["wg"]
        assert wg["enabled"] is True and wg["count"] == 3
        added = [m for m in cfg.CFG["warp"]["members"] if m.get("kind") == "wg"]
        assert [m["tag"] for m in added] == ["WARP-WG-1", "WARP-WG-2", "WARP-WG-3"]
        assert [m["port"] for m in added] == [1110, 1111, 1112]
        assert added[0]["service"] == "warp-wg-1.service"
    finally:
        monkeypatch.delenv("WARP_WG_COUNT", raising=False)
        importlib.reload(cfg_mod)


def test_families_env_drives_channels_and_home_nodes(monkeypatch):
    """WARP_WG_FAMILIES × WARP_WG_NODES 定义拓扑：每节点 per 条渠道。"""
    monkeypatch.delenv("WARP_WG_COUNT", raising=False)
    monkeypatch.setenv("WARP_WG_FAMILIES", "v4,v6,v6,v6")
    monkeypatch.setenv("WARP_WG_NODES", "2")
    cfg = importlib.reload(cfg_mod)
    try:
        wg = cfg.CFG["warp"]["wg"]
        assert wg["count"] == 8 and wg["per_node"] == 4 and wg["home_nodes"] == 2
        added = [m for m in cfg.CFG["warp"]["members"] if m.get("kind") == "wg"]
        assert [m["family"] for m in added] == ["v4", "v6", "v6", "v6"] * 2
        assert [m["home_group"] for m in added] == [0] * 4 + [1] * 4
        assert [m["port"] for m in added][-1] == 1117
    finally:
        monkeypatch.delenv("WARP_WG_FAMILIES", raising=False)
        monkeypatch.delenv("WARP_WG_NODES", raising=False)
        importlib.reload(cfg_mod)


def test_region_key_prefers_measured_colo(monkeypatch):
    """同 colo 的主机名不同也要并成一组：aws48/aws49 都是 NRT。"""
    monkeypatch.setattr(warp_wg, "load_colo_map", lambda: {
        "a": {"colo": "NRT"}, "b": {"colo": "NRT"}, "c": {"colo": "SIN"},
    })
    assert warp_wg.region_key(_node("a")) == warp_wg.region_key(_node("b"))
    assert warp_wg.region_key(_node("a")) != warp_wg.region_key(_node("c"))
    # 没有实测数据时退回主机名分组，不能让键变成空串把池子并成一组
    assert warp_wg.region_key(_node("d", "pq.us01.yydjc.top")) == "pq.us01"


def test_count_zero_keeps_members_untouched(monkeypatch):
    monkeypatch.delenv("WARP_WG_COUNT", raising=False)
    cfg = importlib.reload(cfg_mod)
    try:
        assert cfg.CFG["warp"]["wg"]["enabled"] is False
        assert not [m for m in cfg.CFG["warp"]["members"] if m.get("kind") == "wg"]
    finally:
        importlib.reload(cfg_mod)


def test_path_helpers_return_paths():
    """config.PROJECT_DIR 是 str，路径拼接必须显式转 Path（曾因此 apply 崩溃）。"""
    assert isinstance(warp_wg.line_dir(1), Path)
    assert isinstance(warp_wg.state_path(), Path)
    assert warp_wg.line_dir(3).name == "line3"


def test_apply_writes_line_configs_and_state(tmp_path, monkeypatch):
    """端到端：注册 → 选节点 → 写每线配置 → 刷新主出站 → 写状态。"""
    sb = cfg_mod.CFG["singbox"]["binary"]
    if not os.access(sb, os.X_OK):
        pytest.skip("sing-box 不可执行")

    monkeypatch.setattr(warp_wg, "PROJECT", tmp_path)
    monkeypatch.setattr(warp_wg, "load_colo_map", lambda: {})
    pool = [_node("a", "pq.aws48.yydjc.top"), _node("b", "unamecf.xn--x.com")]
    monkeypatch.setattr(warp_wg, "load_nodes", lambda: pool)
    monkeypatch.setattr(warp_wg, "refresh_main_outbounds", lambda: None)

    def fake_register(i, force=False):
        d = warp_wg.line_dir(i)
        d.mkdir(parents=True, exist_ok=True)
        (d / "profile.conf").write_text(PROFILE)

    monkeypatch.setattr(warp_wg, "register_line", fake_register)

    st = warp_wg.apply(count=2)

    for i in (1, 2):
        d = warp_wg.line_dir(i)
        cfg = json.loads((d / "singbox.json").read_text())
        assert cfg["inbounds"][0]["listen_port"] == 1110 + i - 1
        assert cfg["endpoints"][0]["detour"] == cfg["outbounds"][0]["tag"]
        r = subprocess.run([sb, "check", "-c", str(d / "singbox.json")],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    saved = json.loads(warp_wg.state_path().read_text())["lines"]
    assert [saved[f"WARP-WG-{i}"]["port"] for i in (1, 2)] == [1110, 1111]
    assert st["lines"]["WARP-WG-1"]["node"] in ("a", "b")
    assert st["lines"]["WARP-WG-1"]["family"] == "v6"
    assert len(st["lines"]["WARP-WG-1"]["rotation"]) == 2


def test_apply_rejects_short_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(warp_wg, "PROJECT", tmp_path)
    monkeypatch.setattr(warp_wg, "load_colo_map", lambda: {})
    monkeypatch.setattr(warp_wg, "load_nodes", lambda: [_node("a", "a.example.com")])

    def fake_register(i, force=False):
        d = warp_wg.line_dir(i)
        d.mkdir(parents=True, exist_ok=True)
        (d / "profile.conf").write_text(PROFILE)

    monkeypatch.setattr(warp_wg, "register_line", fake_register)
    with pytest.raises(SystemExit):
        warp_wg.apply(count=2)
