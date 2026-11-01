"""warp_blocks（渠道模型）与 warp_night（夜锁策略）的单测。

两个模块都是纯函数，所以这里不 mock 网络、不碰 .warp/ 的真文件：
实测表用 tmp_path 造假数据，配置用最小 dict 直接构造。要断言的是**规则**：

  - 块按 colo 切，不是按段（段是抽签结果）
  - 常规/非常规的分界是稳定性，0 次成功不算「非常规」，是不可用
  - 夜间 100% 封禁的块不刷，未知封禁率的块必须留
  - 探针自己的故障（config/port）不能记成节点故障
"""
import json

import pytest

import warp_blocks as wb
import warp_night as wn


# ---------------------------------------------------------------- 夹具

def cfg(enabled=True, per=2, table=None):
    return {"warp": {"blocks": {
        "enabled": enabled,
        "channels_per_block": per,
        "table": table if table is not None else [
            {"colo": "NRT", "ban_rate": 1.0},
            {"colo": "SJC", "ban_rate": 0.25},
            {"colo": "TPE", "ban_rate": None},
        ],
    }}}


def nodes_map():
    return {
        "n1": {"colo": "NRT", "category": "regular", "ok": 3, "n": 3},
        "n2": {"colo": "NRT", "category": "regular", "ok": 2, "n": 2},
        "n3": {"colo": "NRT", "category": "irregular", "ok": 2, "n": 3},
        "s1": {"colo": "SJC", "category": "regular", "ok": 5, "n": 5},
        "t1": {"colo": "TPE", "category": "regular", "ok": 1, "n": 1},
    }


# ---------------------------------------------------------------- channels

def test_disabled_yields_nothing():
    """默认关闭时一条渠道都不展开 —— 现网拓扑不因引入模型而改变。"""
    assert wb.channels(cfg(enabled=False), nodes_map()) == []


def test_block_cap_and_partial_pool():
    """每块每类最多 per 条；池子不够就少给，不拿同一节点凑两条。"""
    chs = wb.channels(cfg(per=2), nodes_map())
    nrt_reg = [c for c in chs if c["colo"] == "NRT" and c["category"] == "regular"]
    assert [c["node"] for c in nrt_reg] == ["n1", "n2"]
    nrt_irr = [c for c in chs if c["colo"] == "NRT" and c["category"] == "irregular"]
    assert len(nrt_irr) == 1  # 池里只有一个非常规节点


def test_v4_is_merged_single_block():
    """v4 只开一个合并块：出口段高度收敛，逐块开只是同段重复。"""
    chs = wb.channels(cfg(), nodes_map())
    v4 = [c for c in chs if c["type"] == "v4"]
    assert len(v4) == 2 and {c["colo"] for c in v4} == {wb.V4_COLO}
    assert {c["tag"] for c in v4} == {"WARP-BLK-ALL-V4-0", "WARP-BLK-ALL-V4-1"}


def test_channel_tag_is_type_block_index():
    chs = wb.channels(cfg(), nodes_map())
    tags = {c["tag"] for c in chs}
    assert "WARP-BLK-SJC-REG-0" in tags
    assert "WARP-BLK-TPE-REG-0" in tags


# ---------------------------------------------------------------- 排序

def test_order_known_delay_first_unknown_last():
    """未知延迟排尾：没测过的线可能是最慢的，不该占候选首位。"""
    out = wb.order_nodes(["a", "b", "c"], {"c": 80, "a": 300})
    assert out == ["c", "a", "b"]


def test_order_is_reproducible():
    """没有延迟时按 tag 定序：两次 plan 给出同一拓扑，排查才分得清是配置变了还是顺序抖了。"""
    assert wb.order_nodes(["b", "a", "c"]) == ["a", "b", "c"]


# ---------------------------------------------------------------- 稳定性判定

@pytest.mark.parametrize("ok,n,expect", [
    (3, 3, "regular"),   # 无失败反例
    (2, 3, "irregular"),  # 成功过也失败过
    (0, 3, None),        # 不可用，不入块
    (0, 0, None),
])
def test_category_for(ok, n, expect):
    assert wb.category_for(ok, n) == expect


# ---------------------------------------------------------------- 线路分配

def test_plan_lines_insufficient_channels_raises():
    with pytest.raises(SystemExit):
        wb.plan_lines(wb.channels(cfg(), nodes_map()), 99)


def test_plan_lines_assigns_node_family_and_same_block_rotation():
    chs = wb.channels(cfg(), nodes_map())
    plan = wb.plan_lines(chs, 2, nodes_map=nodes_map(), rotation_size=4)
    assert [p["line"] for p in plan] == [1, 2]
    assert plan[0]["node"] == chs[0]["node"] and plan[0]["family"] == chs[0]["family"]
    # 后备顺序先同块：第一条线是 NRT 常规，rotation 里应紧跟同块另一个节点
    assert plan[0]["rotation"][0] == plan[0]["node"]
    assert plan[0]["rotation"][1] == "n2"


def test_plan_lines_rotation_size_respected():
    chs = wb.channels(cfg(), nodes_map())
    plan = wb.plan_lines(chs, 1, nodes_map=nodes_map(), rotation_size=2)
    assert len(plan[0]["rotation"]) == 2


# ---------------------------------------------------------------- 夜间白名单

def test_fully_banned_block_kept_out_of_night():
    """100% 封禁的块重启只会换到另一个同样被封的 /128，白烧一次重启。"""
    tags = wb.night_channel_tags(cfg(), nodes_map())
    assert not any("NRT" in t for t in tags)
    assert any("SJC" in t for t in tags)


def test_unknown_ban_rate_is_kept():
    """封禁率未知的块必须留：HKG/TPE/ORD/PIT 当天才出现，第一阶段正是要测它们。"""
    tags = wb.night_channel_tags(cfg(), nodes_map())
    assert any("TPE" in t for t in tags)


def test_night_line_tags_unknown_node_is_kept():
    """节点不在实测表里（表过期/新节点）按未知处理，宁可白烧也不要在夜里漏掉新段。"""
    out = wb.night_line_tags(cfg(), {"WARP-WG-1": "s1", "WARP-WG-2": "n1",
                                     "WARP-WG-3": "brand-new"}, nodes_map())
    assert out == {"WARP-WG-1", "WARP-WG-3"}


# ---------------------------------------------------------------- 实测 → 块表

def _write(dirpath, name, payload):
    (dirpath / name).write_text(json.dumps(payload, ensure_ascii=False))


def test_build_map_aggregates_by_egress_ip(tmp_path):
    """按出口 IP 聚合：一个 IP 常被两个节点共用，按节点聚合会把同一份观测算两遍。"""
    _write(tmp_path, "tunnel_inputs.json", [
        {"egress_ip": "1.1.1.1", "tag": "n-a"},
        {"egress_ip": "1.1.1.1", "tag": "n-b"},
        {"egress_ip": "2.2.2.2", "tag": "n-c"},
    ])
    _write(tmp_path, "warp_probe.json", [
        {"egress_ip": "1.1.1.1", "ok": True, "colo": "SJC"},
        {"egress_ip": "1.1.1.1", "ok": True, "colo": "SJC"},
        {"egress_ip": "2.2.2.2", "ok": False, "colo": None},
    ])
    out = wb.build_map(str(tmp_path))
    assert out["nodes"]["n-a"] == out["nodes"]["n-b"]
    assert out["nodes"]["n-a"]["category"] == "regular"
    assert out["nodes"]["n-a"]["colo"] == "SJC"
    assert "n-c" not in out["nodes"]  # 0 次成功不入块


def test_build_map_prefers_diag_over_udp_probe(tmp_path):
    """diag44 的握手轮次优先于 udp_probe：后者把「端口没绑上」也记成握手失败。"""
    _write(tmp_path, "tunnel_inputs.json", [
        {"egress_ip": "1.1.1.1", "tag": "n-a"},
        {"egress_ip": "3.3.3.3", "tag": "n-d"},
    ])
    _write(tmp_path, "warp_probe.json", [
        {"egress_ip": "1.1.1.1", "ok": True, "colo": "SJC"},
        {"egress_ip": "3.3.3.3", "ok": True, "colo": "HKG"},
    ])
    _write(tmp_path, "udp_probe_result.json", [
        {"egress_ip": "3.3.3.3", "warp_rounds": [None, None, None]},
    ])
    lines = [
        # config/port 是探针自己的故障，不计入稳定性
        {"tag": "n-a", "egress_ip": "1.1.1.1", "rounds": [
            {"stage": "config", "ok": False}, {"stage": "config", "ok": False}]},
        {"tag": "n-d", "egress_ip": "3.3.3.3", "rounds": [
            {"stage": "handshake", "ok": False}, {"stage": "handshake", "ok": False}]},
    ]
    (tmp_path / "diag44.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n")
    out = wb.build_map(str(tmp_path))
    # n-a 的 WARP 观测全是探针故障 → 只剩 warp_probe 那 1 次成功 → regular
    assert out["nodes"]["n-a"]["category"] == "regular"
    # n-d 有 1 成功 + 2 握手失败 → irregular（udp_probe 的 3 个 null 不再叠加）
    assert out["nodes"]["n-d"]["category"] == "irregular"
    assert out["nodes"]["n-d"]["n"] == 3


# ---------------------------------------------------------------- 夜锁策略

def test_night_window_crosses_midnight():
    nw = {"enabled": True, "start_hour": 20, "end_hour": 8}
    assert wn.active(nw, _ts(21)) and wn.active(nw, _ts(3))
    assert not wn.active(nw, _ts(12))


def test_night_window_disabled_never_active():
    assert not wn.active({"enabled": False}, _ts(23))


def test_restart_allowed_active_line_and_whitelist():
    nw = {"enabled": True, "start_hour": 20, "end_hour": 8}
    assert wn.restart_allowed(nw, _ts(12), "WARP-WG-3", "WARP-CF", set())  # 白天一律放行
    t = _ts(23)
    assert wn.restart_allowed(nw, t, "WARP-CF", "WARP-CF", set())   # 当前出口例外
    assert wn.restart_allowed(nw, t, "WARP-CF", "WARP-CF-B", {"WARP-CF"})
    assert not wn.restart_allowed(nw, t, "WARP-WG-3", "WARP-CF-B", {"WARP-CF"})


def test_night_label():
    nw = {"enabled": True, "start_hour": 20, "end_hour": 8}
    assert wn.label(nw, _ts(23), 3) == "20-8:3线"
    assert wn.label(nw, _ts(12), 3) == "待20点/3线"
    assert wn.label({"enabled": False}, _ts(23), 0) == "关"


def _ts(hour):
    """本地时区里今天 hour 点的 epoch ——窗口按 host 本地时间算。"""
    import time as _t
    lt = _t.localtime()
    return _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))
