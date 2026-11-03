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


def test_v4_is_merged_single_block_and_comes_first():
    """v4 只开一个合并块，且排在清单最前面。

    v4 出口段高度收敛（实测四条线只有 243/237/205 三个 /24），逐块开只是
    同段重复。但合并块**排在末尾**时会被线路数截断 —— 实测 29 条线时 v4
    渠道一条都分不到，等于死代码。所以它必须排最前。
    """
    chs = wb.channels(cfg(), nodes_map())
    v4 = [c for c in chs if c["type"] == "v4"]
    assert len(v4) == 2 and {c["colo"] for c in v4} == {wb.V4_COLO}
    assert {c["tag"] for c in v4} == {"WARP-BLK-ALL-V4-0", "WARP-BLK-ALL-V4-1"}
    # 排在最前：线路数不足时优先被分到
    assert [c["type"] for c in chs[:2]] == ["v4", "v4"]


def test_pinned_block_borrows_a_node_without_reassigning_it():
    """`pin` 让「众数不是这个 colo」的节点也能给某个块供样（当前只有 PIT）。

    PIT 唯一落到过的节点是 ORD 那条（ORD 7 / PIT 3 / SIN 1），按众数归 ORD，
    于是 PIT 块永远空着。pin 是「同一台节点再挂一台设备」—— 块名只是预期落点，
    实测落点以 trace 为准，所以这里只断言渠道出得来、节点是它，且 ORD 块里
    这个节点仍然在。
    """
    c = cfg(table=[{"colo": "ORD", "ban_rate": None},
                   {"colo": "PIT", "ban_rate": None,
                    "pin": ["n1"]}])
    chs = wb.channels(c, nodes_map())
    pit = [ch for ch in chs if ch["colo"] == "PIT"]
    assert [ch["node"] for ch in pit] == ["n1"]
    assert pit[0]["pinned"] is True
    # 归属不变：n1 仍然是 NRT 的节点（nodes_map 里它的 colo 是 NRT），
    # 所以 ORD 块反而没有渠道 —— pin 不会把节点「改籍」。
    assert not [ch for ch in chs if ch["colo"] == "ORD"]


def test_v4_block_prefers_nodes_that_actually_ran_v4():
    """v4 合并块按**实测 v4 握手**排序，不按延迟。

    反例来自生产：`cn.bing.com:443` 的 v6 是 regular，却被排在 v4 候选首位，
    而它两轮 v4 全是 handshake 失败。延迟序对 v4 没有意义 —— 承载不了就是
    承载不了，快也没用。
    """
    m = nodes_map()
    m["dead-v4"] = {"colo": "TPE", "category": "regular",
                    "v4_ok": 0, "v4_n": 2}
    m["good-v4"] = {"colo": "SJC", "category": "regular",
                    "v4_ok": 2, "v4_n": 2}
    chs = wb.channels(cfg(), m, delays={"dead-v4": 10, "good-v4": 900})
    v4 = [c["node"] for c in chs if c["type"] == "v4"]
    # 实测能跑 v4 的排第一，尽管它的延迟差了 90 倍
    assert v4[0] == "good-v4"
    assert "dead-v4" not in v4[:2]


def test_v4_block_never_drops_a_node_because_it_failed_once():
    """握手失败的节点只**排后**，不剔除。

    节点会恢复：机场线路的可用性以分钟计，实测 2026-11-02 上午判死的
    `unamecf…~4` 下午就通了。把「当前不通」写成「永久不可用」，淘汰掉的
    节点就再也回不来，池子只会越用越薄。淘汰是运行期的事（同块轮回守护
    换节点），不是布线期的。

    未知节点排在「实测能用」之后、「实测失败」之前：新节点还没测过，不等于
    不能用，但也不该顶掉已验证的出口。
    """
    m = {
        "ok-v4": {"colo": "SJC", "category": "regular", "v4_ok": 1, "v4_n": 1},
        "untested": {"colo": "SJC", "category": "regular"},
        "bad-v4": {"colo": "SJC", "category": "regular", "v4_ok": 0, "v4_n": 2},
    }
    chs = wb.channels(cfg(per=3, table=[{"colo": "SJC", "ban_rate": None}]), m,
                      delays={"bad-v4": 1, "untested": 2, "ok-v4": 3})
    v4 = [c["node"] for c in chs if c["type"] == "v4"]
    assert v4 == ["ok-v4", "untested", "bad-v4"]


def test_h2_carrier_nodes_are_not_wg_channels():
    """h2 承载的节点不归 WG 线管 —— 它在**排序之前**就该出池。

    `apply()` 事后还会按 `carrier != 'h2'` 过滤一遍。若这里先把 h2 节点排进
    前 `per` 名，过滤后渠道就凭空少几条（实测 NRT 块的前三名全是
    `bilibili-50037/50041`，过滤后 REG 渠道整个消失）。
    """
    m = {
        "h1": {"colo": "SJC", "category": "regular", "carrier": "h2"},
        "h2": {"colo": "SJC", "category": "regular", "carrier": "h2"},
        "w1": {"colo": "SJC", "category": "regular"},
        "w2": {"colo": "SJC", "category": "regular"},
    }
    chs = wb.channels(cfg(per=2, table=[{"colo": "SJC", "ban_rate": None}]), m,
                      delays={"h1": 1, "h2": 2})
    assert [c["node"] for c in chs if c["type"] == "v6"] == ["w1", "w2"]


def test_channel_tag_is_type_block_index():
    chs = wb.channels(cfg(), nodes_map())
    tags = {c["tag"] for c in chs}
    assert "WARP-BLK-SJC-REG-0" in tags
    assert "WARP-BLK-TPE-REG-0" in tags


def test_excluded_colos_do_not_take_devices():
    """封禁率 100% 的块不占设备：段全封，新设备拿到的 /128 必然也在限额里。

    实测（2026-11-01 夜间）：20 条线里 4 条挂在 NRT，一条也没起来过；
    把设备从 NRT 挪到 PHX/SJC/SIN/TPE 之后可用数 5/20 → 13/20。
    """
    c = cfg()
    c["warp"]["blocks"]["exclude_colos"] = ["NRT"]
    chs = wb.channels(c, nodes_map())
    assert chs, "排除一个块不该把清单清空"
    assert not any(ch["colo"] == "NRT" for ch in chs)
    # v4 是独立合并块，不受 colo 排除影响
    assert {ch["colo"] for ch in chs} == {"SJC", "TPE", wb.V4_COLO}


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

def test_sibling_lines_stagger_their_rotation():
    """同一块的兄弟线必须错开后置顺序。

    否则三条线绑同一块时 rotation 一模一样：其中一条的节点挂了，三条会在
    同一轮全跳到同一个后备节点 —— 那是「集中风险」而不是分散。旧路径
    `apply()` 的 `off = (i - 1) % per` 就是干这个的，块模型迁移时漏掉了。
    """
    rich = cfg(per=3, table=[{"colo": "BIG", "ban_rate": None}])
    m = {f"b{i}": {"colo": "BIG", "category": "regular", "ok": 1, "n": 1}
         for i in range(8)}
    plan = wb.plan_lines(wb.channels(rich, m), 3, rotation_size=8)
    heads = [p["rotation"][1] for p in plan]
    assert len(set(heads)) == len(heads), f"兄弟线后备首位重复: {heads}"


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
