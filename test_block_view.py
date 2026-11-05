"""块视图的纯函数（tools/block_view.py）。

不碰网络：台账文件与 Clash 客户端都由用例喂。
"""
import importlib.util
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent


def _mod(monkeypatch, tmp_path):
    sys.path.insert(0, str(PROJECT))
    spec = importlib.util.spec_from_file_location(
        "block_view", PROJECT / "tools" / "block_view.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NODES = {
    "a.example:443-vl": {"colo": "SIN", "category": "regular", "ok": 2, "n": 2,
                         "egress_ip": "1.2.3.4", "wg_ok": 2, "wg_n": 2},
    "b.example:443-any": {"colo": "SIN", "category": "irregular", "ok": 1, "n": 3,
                          "egress_ip": "5.6.7.8", "wg_ok": 0, "wg_n": 2},
    "c.example:443-any": {"colo": "SIN", "category": "regular", "ok": 1, "n": 1,
                          "egress_ip": "2a09:bac5:55fe::1",
                          "carrier": "h2"},
    "d.example:443-vl": {"colo": "NRT", "category": "regular", "ok": 1, "n": 1,
                         "egress_ip": "9.9.9.9"},
    "e.example:443-vl": {"category": "regular"},          # 没有 colo → 不入块
}


def test_blocks_groups_by_colo_and_sorts_regular_first(monkeypatch, tmp_path):
    mod = _mod(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "current_bindings", lambda: {})
    monkeypatch.setattr(mod, "planned", lambda: {})
    data = mod.build(NODES)
    assert [b["colo"] for b in data] == ["NRT", "SIN"]
    sin = next(b for b in data if b["colo"] == "SIN")
    assert [r["node"] for r in sin["nodes"]][0] == "a.example:443-vl"
    # 没有 colo 的节点不属于任何块
    assert all(r["node"] != "e.example:443-vl" for b in data for r in b["nodes"])


def test_verdict_prefers_live_binding_over_missing_diag(monkeypatch, tmp_path):
    """在跑的线哪怕没有 wg_* 记录，也算「能用」—— WG-4..9 实测就是这样。"""
    mod = _mod(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "current_bindings", lambda: {
        "d.example:443-vl": [{"channel": "WARP-WG-9", "carrier": "WG"}]})
    monkeypatch.setattr(mod, "planned", lambda: {})
    nrt = mod.build(NODES)[0]
    assert nrt["nodes"][0]["verdict"] == "✓warp-proxy"
    assert nrt["in_use"] == 1


def test_h2_binding_counts_as_in_service(monkeypatch, tmp_path):
    """H2 线把裸 TCP 交给节点出站，节点同样在数据路径上 —— 不能报「未测」。

    实测 16 个正被 H2 线绑着的节点会因此显示成 H2-only，看着像没验证过。
    """
    mod = _mod(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "current_bindings", lambda: {
        "c.example:443-any": [{"channel": "WARP-H2-SIN-3", "carrier": "H2"}]})
    monkeypatch.setattr(mod, "planned", lambda: {})
    sin = next(b for b in mod.build(NODES) if b["colo"] == "SIN")
    row = next(r for r in sin["nodes"] if r["node"] == "c.example:443-any")
    assert row["verdict"] == "✓h2-warp"


def test_planned_channel_is_shown_but_not_counted_as_bound(monkeypatch, tmp_path):
    """块模型会挑 ≠ 已经在用。两者混起来就看不出「计划选中的还没上线」。"""
    mod = _mod(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "current_bindings", lambda: {})
    monkeypatch.setattr(mod, "planned", lambda: {"a.example:443-vl": "WARP-BLK-SIN-REG-0"})
    sin = next(b for b in mod.build(NODES) if b["colo"] == "SIN")
    row = next(r for r in sin["nodes"] if r["node"] == "a.example:443-vl")
    assert row["planned"] == "WARP-BLK-SIN-REG-0"
    assert row["bound"] == []
    assert sin["planned_n"] == 1 and sin["in_use"] == 0


def test_delay_column_distinguishes_unmeasured_from_dead(monkeypatch, tmp_path):
    """`—`（没测）和「不通」（测了失败）不能画成一个样子。"""
    mod = _mod(monkeypatch, tmp_path)
    assert mod._fmt_delay({}, "n1") == "—"
    assert mod._fmt_delay({"n1": None}, "n1") == "不通"
    assert mod._fmt_delay({"n1": 120}, "n1") == "120ms"


def test_delay_view_prefers_live_then_cache(monkeypatch, tmp_path):
    """默认要能看到延迟：daemon 每 5 分钟测一轮，结果一直躺在 sing-box 里。"""
    mod = _mod(monkeypatch, tmp_path)
    cache = {"a": 300, "b": 800}
    # 没现测 → 全用缓存
    assert mod._delay_view(cache, None, ["a", "b", "c"]) == {"a": 300, "b": 800}
    # 现测成功的盖缓存
    assert mod._delay_view(cache, {"a": 120}, ["a", "b"]) == {"a": 120, "b": 800}
    # 现测**失败**同样盖缓存：串行复测并发失败的节点仍全失败（503/504 是真
    # 不通，不是排队超时），拿旧缓存盖掉会让 --delay 看起来像没生效。
    assert mod._delay_view(cache, {"a": None, "b": 900}, ["a", "b"]) == {
        "a": None, "b": 900}
    # 没缓存也没现测 → 键不出现，画 `—`
    assert mod._delay_view({}, None, ["c"]) == {}


def _ledger(monkeypatch, tmp_path, led):
    """把 sources 台账写到 CFG 指的位置（工具跟随 SINGBOX_CONFIG，不写死）。"""
    import json
    import config
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}")
    monkeypatch.setitem(config.CFG["singbox"], "config_file", str(cfg))
    (tmp_path / "cfg.json.sources.json").write_text(json.dumps(led))


def test_sources_ledger_provides_name_and_sub(monkeypatch, tmp_path):
    """块视图要能报出「这个节点是哪家机场的哪台机器」，不只是 host:port。"""
    mod = _mod(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "current_bindings", lambda: {})
    monkeypatch.setattr(mod, "planned", lambda: {})
    _ledger(monkeypatch, tmp_path, {
        "a.example:443-vl": {"sub": "okz", "label": "l", "name": "新加坡-01"}})
    sin = next(b for b in mod.build(NODES) if b["colo"] == "SIN")
    row = next(r for r in sin["nodes"] if r["node"] == "a.example:443-vl")
    assert (row["sub"], row["name"]) == ("okz", "新加坡-01")


def test_ambiguous_hostport_is_not_used_for_lookup(monkeypatch, tmp_path):
    """同一台机器挂多个不同名字的节点时，host:port 反查会张冠李戴。

    实测 5 组、77/152 个节点会受此影响（`cn.bing.com:443` 一个 host:port 下
    就有 37 个不同名字）。所以只有唯一的 host:port 才建索引。
    """
    mod = _mod(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "current_bindings", lambda: {})
    monkeypatch.setattr(mod, "planned", lambda: {})
    _ledger(monkeypatch, tmp_path, {
        "a.example:443-vl": {"sub": "okz", "label": "l", "name": "新加坡-01"},
        "a.example:443~2-vl": {"sub": "okz", "label": "l", "name": "香港-09"}})
    src = mod.load_sources()
    # 两个 tag 各自查得到自己；`a.example:443` 这个键不该存在
    assert src["a.example:443-vl"]["name"] == "新加坡-01"
    assert src["a.example:443~2-vl"]["name"] == "香港-09"
    assert "a.example:443" not in src
    # 漂移到一个台账里没有的 ~9：宁可查不到，也不能挂上别人的名字
    assert mod.lookup_source(src, "a.example:443~9-vl")["name"] == "-"


def test_channels_view_reports_selected_node_and_pool(monkeypatch, tmp_path):
    """渠道视角回答「这条渠道选了谁、池子里还有谁」。"""
    mod = _mod(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "current_bindings", lambda: {})
    monkeypatch.setattr(mod, "node_identity", lambda: {})
    monkeypatch.setattr(mod, "load_sources", lambda: {})
    monkeypatch.setattr(mod.warp_blocks, "channels", lambda cfg: [
        {"tag": "WARP-BLK-SIN-REG-0", "type": "v6", "colo": "SIN",
         "category": "regular", "index": 0, "node": "a.example:443-vl",
         "nodes": ["a.example:443-vl", "b.example:443-vl"]}])
    rows = mod.channels_view()
    assert rows[0]["channel"] == "WARP-BLK-SIN-REG-0"
    assert rows[0]["node"] == "a.example:443-vl"
    assert rows[0]["pool"] == ["a.example:443-vl", "b.example:443-vl"]
    assert rows[0]["pool_n"] == 2


def test_same_physical_exit_across_entries_is_flagged(monkeypatch, tmp_path):
    """沉睡池旧入口与订阅新入口是同一台机器时必须标出来。

    机场换入口只换 server/port，SNI 与密码不变。实测 55 条渠道只占 38 个
    物理出口，12 组渠道选中的其实是同一台 —— 不标出来会以为「分散了风险」。
    """
    mod = _mod(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "current_bindings", lambda: {})
    monkeypatch.setattr(mod, "load_sources", lambda: {})
    monkeypatch.setattr(mod, "node_identity", lambda: {
        "old.example:50001-any": ("sg-1.hi2026.net", "anytls", "pw1"),
        "cn.bing.com:443~21-any": ("sg-1.hi2026.net", "anytls", "pw1"),
        "other:443-any": ("jp-1.hi2026.net", "anytls", "pw2"),
    })
    monkeypatch.setattr(mod.warp_blocks, "channels", lambda cfg: [
        {"tag": "WARP-BLK-SIN-REG-0", "type": "v6", "colo": "SIN",
         "category": "regular", "index": 0,
         "node": "cn.bing.com:443~21-any", "nodes": []},
        {"tag": "WARP-H2-SIN-REG-0", "type": "h2", "colo": "SIN",
         "category": "regular", "index": 0, "carrier": "h2",
         "node": "old.example:50001-any", "nodes": []},
        {"tag": "WARP-BLK-NRT-REG-0", "type": "v6", "colo": "NRT",
         "category": "regular", "index": 0,
         "node": "other:443-any", "nodes": []}])
    rows = {r["channel"]: r for r in mod.channels_view()}
    assert rows["WARP-BLK-SIN-REG-0"]["shared_with"] == ["WARP-H2-SIN-REG-0"]
    assert rows["WARP-H2-SIN-REG-0"]["shared_with"] == ["WARP-BLK-SIN-REG-0"]
    assert rows["WARP-BLK-NRT-REG-0"]["shared_with"] == []


def test_node_identity_ignores_server_and_port(monkeypatch, tmp_path):
    """身份指纹**不能**含 server/port —— 机场换入口换的正是这两项。"""
    mod = _mod(monkeypatch, tmp_path)
    import config
    import json
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"outbounds": [
        {"tag": "old:50001-any", "type": "anytls", "server": "old.example",
         "server_port": 50001,
         "tls": {"server_name": "sg-1.hi2026.net"}, "password": "pw1"},
        {"tag": "cn.bing.com:443-any", "type": "anytls", "server": "cn.bing.com",
         "server_port": 443,
         "tls": {"server_name": "sg-1.hi2026.net"}, "password": "pw1"},
    ]}))
    monkeypatch.setitem(config.CFG["singbox"], "config_file", str(cfg))
    ident = mod.node_identity()
    assert ident["old:50001-any"] == ident["cn.bing.com:443-any"]


def test_carrier_labels_distinguish_the_three_warp_paths(monkeypatch, tmp_path):
    """承载口径必须把三种 WARP 路径分开，不能都叫 `proxy`。

    旧口径把「WG 经节点」「直连 WARP」「MASQUE/H2」全显示成 `proxy`，
    而 `proxy` 这个词在项目里另有所指（只走机场代理、不承载 WARP 的轮换池）。
    故障时三者表现完全不同：WG 要 UDP、H2 只要 TCP、直连根本不经过节点。
    """
    mod = _mod(monkeypatch, tmp_path)
    assert mod.CARRIER_LABEL["proxy"] == "warp-proxy"
    assert mod.CARRIER_LABEL["h2"] == "h2-warp"
    assert mod.CARRIER_LABEL["direct"] == "warp-direct"
    # 纯代理不在这张映射里 —— 它不该被当成一种 WARP 承载
    assert "纯代理" not in mod.CARRIER_LABEL.values()


def test_block_pool_view_counts_all_nodes_in_the_block(monkeypatch, tmp_path):
    """块 = 该 colo 下**全部**适配节点；`channels_per_block` 只决定几条线位。

    用户对渠道块的定义就是「同 colo 的节点放一块」。旧视图逐渠道列，
    21 个节点的块只显示 3 行，其余看着像没进块 —— 实际一直在池子里等位。
    """
    mod = _mod(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "current_bindings", lambda: {})
    monkeypatch.setattr(mod, "node_identity", lambda: {})
    monkeypatch.setattr(mod, "load_sources", lambda: {})
    pool = [f"n{i}:443-any" for i in range(10)]
    monkeypatch.setattr(mod.warp_blocks, "channels", lambda cfg: [
        {"tag": f"WARP-H2-SIN-REG-{i}", "type": "h2", "colo": "SIN",
         "category": "regular", "index": i, "carrier": "h2",
         "node": pool[i], "nodes": pool} for i in range(3)])
    b = mod.block_pool_view()[0]
    assert b["pool_n"] == 10, "块内节点应是全部 10 个"
    assert b["wired_n"] == 3, "只有 3 个排成线位"
    assert b["bench_n"] == 7, "其余 7 个是候补，不是「没进块」"


def test_display_width_handles_cjk_and_emoji(monkeypatch, tmp_path):
    """按显示宽度对齐：中文/国旗 emoji 占 2 列，按字符数补空格会整表错位。"""
    mod = _mod(monkeypatch, tmp_path)
    assert mod._w("abc") == 3
    assert mod._w("新加坡") == 6          # 全角汉字各 2 列
    assert mod._w("🇸🇬") == 4             # 国旗是两个 regional indicator
    assert mod._pad("新加坡-01", 12) == "新加坡-01" + " " * 3
    # 超宽要截断，不能撑破表格
    assert mod._w(mod._pad("🇸🇬新加坡03-0.1倍 | 电信联通推荐", 12)) == 12
