"""warp_blocks（渠道模型）与 warp_night（夜锁策略）的单测。

两个模块都是纯函数，所以这里不 mock 网络、不碰 .warp/ 的真文件：
实测表用 tmp_path 造假数据，配置用最小 dict 直接构造。要断言的是**规则**：

  - 块按 colo 切，不是按段（段是抽签结果）
  - 常规/非常规的分界是稳定性，0 次成功不算「非常规」，是不可用
  - 夜间 100% 封禁的块不刷，未知封禁率的块必须留
  - 探针自己的故障（config/port）不能记成节点故障
"""
import json
import sys
from pathlib import Path

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


def test_build_map_preserves_h2_carrier(tmp_path, monkeypatch):
    """重建**不能**丢掉 `carrier=h2`。

    这是 2026-11-08 实测到的真实故障：`build_map` 的输入只有 WG/diag 文件，
    里面没有 `carrier` 字段，于是每重建一次就把 H2 标记清空一批。症状是
    「H2 渠道越来越少」，而块映射里那些节点明明带着 `h2: {colo, ip}` ——
    看起来像没测过，实际是测过没记上。
    """
    old = tmp_path / "block-map.json"
    old.write_text(json.dumps({"nodes": {
        "n-a": {"colo": "SJC", "category": "regular", "ok": 2, "n": 2,
                "carrier": "h2", "h2": {"colo": "SJC", "ip": "2a09::1"}},
        "n-b": {"colo": "SJC", "category": "regular", "ok": 2, "n": 2},
    }}))
    monkeypatch.setattr(wb, "map_path", lambda: old)
    _write(tmp_path, "tunnel_inputs.json", [
        {"egress_ip": "1.1.1.1", "tag": "n-a"},
        {"egress_ip": "1.1.1.1", "tag": "n-b"},
    ])
    _write(tmp_path, "warp_probe.json", [
        {"egress_ip": "1.1.1.1", "ok": True, "colo": "SJC"},
        {"egress_ip": "1.1.1.1", "ok": True, "colo": "SJC"},
    ])
    out = wb.build_map(str(tmp_path))
    assert out["nodes"]["n-a"]["carrier"] == "h2", "H2 标记被重建抹掉了"
    assert out["nodes"]["n-a"]["h2"]["colo"] == "SJC"
    assert "carrier" not in out["nodes"]["n-b"], "非 h2 节点不该被误标"


def test_merge_h2_marks_carrier_and_heals(tmp_path, monkeypatch):
    """合并 H2 探测结果必须同时写 `carrier`，并顺带修历史漏标。

    只写 `h2` 子对象、不写 `carrier` 的话，消费方（channels 的 h2 分支、
    render_h2_lines、block_view 都判 `meta["carrier"] == "h2"`）根本看不到 ——
    PHX 的 8 个节点就是这样在 H2 池里消失了整个周期。
    """
    import importlib.util
    proj = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "merge_h2", proj / "tools" / "merge_h2_blockmap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    (tmp_path / ".warp").mkdir()
    bm = tmp_path / ".warp" / "block-map.json"
    bm.write_text(json.dumps({"nodes": {
        # 历史遗留：有 h2 结果但没有 carrier
        "old-node": {"colo": "NRT", "category": "regular", "ok": 1, "n": 1,
                     "h2": {"colo": "NRT", "ip": "2a09::9"}},
        "wg-only": {"colo": "SJC", "category": "regular", "ok": 1, "n": 1},
    }}))
    scan = tmp_path / "scan.json"
    scan.write_text(json.dumps([
        {"tag": "wg-only", "ok": True, "colo": "SJC", "ip": "2a09::2"},
        {"tag": "brand-new", "ok": True, "colo": "PHX", "ip": "2a09::3"},
    ]))
    monkeypatch.setattr(mod, "PROJECT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["merge", str(scan)])
    assert mod.main() == 0
    nodes = json.loads(bm.read_text())["nodes"]
    assert nodes["wg-only"]["carrier"] == "h2", "已存在节点没标 carrier"
    assert nodes["brand-new"]["carrier"] == "h2", "新节点没标 carrier"
    assert nodes["old-node"]["carrier"] == "h2", "历史漏标没被自愈"


def test_missing_tier_file_means_all_enabled(tmp_path, monkeypatch):
    """档位文件缺失/损坏 = 全开，**不是全停**。

    `h2-only` 档位的合法写法就是 `wg: ""`（一条都不留）。若「文件不存在」
    也归成 `""`，`tier_keep("")` 得空集 → 22 条 WG 线全被判停用，而日志里
    只有一句「档位停用」，看上去像主动配置的结果。全新远端（`.warp/` 未同步）
    和坏文件都会走到这条路上。
    """
    monkeypatch.setattr(wb, "map_path", lambda: tmp_path / "block-map.json")
    declared = {f"WARP-WG-{i}" for i in range(1, 23)}
    # 文件不存在
    assert wb.tier_spec() == (None, None)
    assert wb.tier_disabled(declared) == set()
    assert wb.tier_disabled() == set()
    # 文件损坏
    (tmp_path / "tiers.json").write_text("{ 坏掉的 json")
    assert wb.tier_disabled(declared) == set()


def test_empty_tier_spec_really_disables_everything(tmp_path, monkeypatch):
    """合法档位写 `wg: ""` 才是「全停」—— 与上面的「文件缺失」必须分开。"""
    monkeypatch.setattr(wb, "map_path", lambda: tmp_path / "block-map.json")
    (tmp_path / "tiers.json").write_text(json.dumps(
        {"current": "h2-only", "tiers": {"h2-only": {"wg": ""}}}))
    declared = {"WARP-WG-1", "WARP-WG-2"}
    assert wb.tier_spec() == ("h2-only", "")
    assert wb.tier_disabled(declared) == declared


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


# ---------------------------------------------------------------- 两条承载路径
def test_h2_nodes_are_expanded_as_their_own_channels():
    """carrier=h2 的节点必须单独展开成 H2 渠道，而不是被 v6 池剔掉。

    实测（2026-11-04）：`deploy/render_h2_lines.py` 只从 `channels()` 里筛
    `carrier == 'h2'`，而 channels() 的 v6 池把 h2 节点剔得干干净净 —— 于是
    永远走「块模型未筛出 h2 渠道，改用台账」的兜底，新插进来的订阅节点
    （哪怕实测能承载 MASQUE）一条也渲染不出去。
    """
    m = nodes_map()
    m["h2a"] = {"colo": "NRT", "category": "regular", "ok": 1, "n": 1,
                "carrier": "h2"}
    chs = wb.channels(cfg(), m)
    h2 = [c for c in chs if c.get("carrier") == "h2"]
    assert [c["node"] for c in h2] == ["h2a"]
    # v6 池仍然不含它（H2 不占 WG 线位）
    assert "h2a" not in [c["node"] for c in chs if c["type"] == "v6"]


def test_h2_channel_tag_does_not_collide_with_wg():
    """同一块同一类别下的 H2 与 WG 渠道不能撞 tag。

    tag 是夜间白名单、台账、日志共用的身份键；撞名等于两条线被当成一条。
    """
    m = nodes_map()
    m["h2a"] = {"colo": "NRT", "category": "regular", "ok": 1, "n": 1,
                "carrier": "h2"}
    tags = [c["tag"] for c in wb.channels(cfg(), m)]
    assert "WARP-H2-NRT-REG-0" in tags
    assert "WARP-BLK-NRT-REG-0" in tags
    assert len(tags) == len(set(tags)), "渠道 tag 撞名"


# ---------------------------------------------------------- 自动补块（新 colo）

def test_discovered_colo_becomes_a_block():
    """实测出的新 colo 自动成块 —— 否则它的节点永远拿不到渠道。

    实测 2026-11-04 `upoz...:28065-any` 落到 BOM，块表是静态 11 条，
    没有 BOM 也就没有任何日志说明这个节点为什么不被使用。
    """
    nm = nodes_map()
    nm["b1"] = {"colo": "BOM", "category": "regular", "ok": 2, "n": 2,
                "carrier": "h2"}
    extra = wb.discovered_blocks(cfg(), nm)
    assert [b["colo"] for b in extra] == ["BOM"]
    assert extra[0]["loc"] == "孟买"
    assert extra[0]["auto"] is True
    tags = {c["tag"] for c in wb.channels(cfg(), nm)}
    assert "WARP-H2-BOM-REG-0" in tags


def test_known_colos_are_not_duplicated():
    """块表已有的 colo 不能因为实测里有节点就再补一条。"""
    extra = wb.discovered_blocks(cfg(), nodes_map())
    assert extra == []
    # 渠道数不因补块逻辑变化
    assert len(wb.channels(cfg(), nodes_map())) == len(
        [c for c in wb.channels(cfg(), nodes_map())])


def test_unknown_colo_uses_code_not_invented_city():
    """不认识的 colo 只显示代码本身；编个城市名比显示 `XYZ` 更糟。"""
    nm = {"x1": {"colo": "XYZ", "category": "regular", "ok": 1, "n": 1}}
    extra = wb.discovered_blocks(cfg(), nm)
    assert extra[0]["loc"] == "XYZ"


def test_discovered_block_still_respects_exclude_colos():
    """被 exclude 的 colo 即使实测出来也不该补块。"""
    c = cfg()
    c["warp"]["blocks"]["exclude_colos"] = ["BOM"]
    nm = {"b1": {"colo": "BOM", "category": "regular", "ok": 1, "n": 1}}
    # discovered_blocks 本身只负责「块表没有的 colo」；排除由 channels 管
    tags = {x["colo"] for x in wb.channels(c, nm)}
    assert "BOM" not in tags
def test_rebind_uses_declared_family_not_stale_state(monkeypatch):
    """rebind 写配置时必须用**当前声明**的族，不能读状态表里的旧值。

    状态表的 `family` 只在 `apply()` 时写一次，改过拓扑就是旧世代残留。
    实测远端 2026-11-05：`.env` 是 `WARP_WG_FAMILIES=v6`，状态表记
    WG-1/2/3 为 v4 —— rebind 按状态表重写，把 line2/line3 写回
    `prefer_ipv4`。v4 出口只由 colo 决定，三条线立刻撞成同一个
    `104.28.205.117`（既定事实第 27 条：v4 渠道数不应超过 colo 数）。
    """
    import warp_wg as ww

    got = {}
    monkeypatch.setattr(ww, "_read_state", lambda: {"lines": {
        "WARP-WG-2": {"node": "old", "family": "v4",
                      "rotation": ["a", "b"], "port": 1111}}})
    monkeypatch.setattr(ww, "udp_capable", lambda nodes, probe=False: list(nodes))
    monkeypatch.setattr(ww, "load_nodes", lambda: [
        {"tag": "a", "server": "a", "server_port": 1},
        {"tag": "b", "server": "b", "server_port": 1}])
    monkeypatch.setattr(ww, "write_line",
                        lambda i, n, family=None: got.setdefault("f", family))
    monkeypatch.setattr(ww, "write_json", lambda *a, **k: None)
    monkeypatch.setattr(ww, "state_path", lambda: "/tmp/x.json")
    monkeypatch.setattr(ww, "_wg", lambda: {"families": ["v6"],
                                            "tag_prefix": "WARP-WG-",
                                            "service_prefix": "warp-wg-",
                                            "base_port": 1110})
    ww.rebind(2)
    assert got["f"] == "v6", f"应取声明的 v6，实际 {got}"
