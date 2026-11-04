"""H2 渲染的护栏（2026-11-04 实测踩到的）。

1. `free_devices()` 必须把**某几种目录/单元**排除在外，否则会把正在承载
   生产流量的设备当成空闲，渲染出「两条 usque 抢同一个 CF 设备」的拓扑。
2. 设备不够时**不许**缩小线上规模：渲染器按 0..n-1 编号，与线上编号重叠，
   一安装就把后面的线停掉。
3. 但「设备不够」不能把**台账里已经属于这批线的设备**算成不可用：原地重渲染
   换的是节点，一台设备都不换。只算空闲设备会得出 1 台，然后被第 2 条拒掉。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent


def _render_mod():
    sys.path.insert(0, str(PROJECT))
    spec = importlib.util.spec_from_file_location(
        "render_h2_lines", PROJECT / "deploy" / "render_h2_lines.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_devices_are_never_reported_free(tmp_path, monkeypatch):
    """usque-probe* 是探测工具的独占设备池，不能被分给生产线路。"""
    mod = _render_mod()
    warp = tmp_path / ".warp"
    for name in ("usque10", "usque-probe1", "usque-probe2"):
        d = warp / name
        d.mkdir(parents=True)
        (d / "config.json").write_text("{}")
    monkeypatch.setattr(mod, "PROJECT", tmp_path)
    # 单元目录指向空的 tmp：否则会读到本机生产单元，结论取决于这台机器的
    # 部署状态（换台机器跑同一条用例会得到不同答案）。
    free = mod.free_devices(tmp_path / "units")
    assert free == ["usque10"], f"探测设备被当成空闲: {free}"


def test_active_h2_units_occupy_their_devices(tmp_path, monkeypatch):
    """在跑 usque-h2-* 的设备必须算「已占用」。

    只扫 usque-warp* 时，19 条 H2 的 19 台设备全被报成空闲 —— 实测正是如此。
    """
    mod = _render_mod()
    warp = tmp_path / ".warp"
    for name in ("usque10", "usque11"):
        (warp / name).mkdir(parents=True)
        (warp / name / "config.json").write_text("{}")
    units = tmp_path / "units"
    units.mkdir()
    (units / "usque-h2-0.service").write_text(
        "WorkingDirectory=/x/.warp/usque10\n")
    monkeypatch.setattr(mod, "PROJECT", tmp_path)

    real_run = mod.subprocess.run
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "active"})())
    try:
        free = mod.free_devices(units)
    finally:
        monkeypatch.setattr(mod.subprocess, "run", real_run)
    assert free == ["usque11"], f"H2 占用的设备被当成空闲: {free}"


def test_new_colo_gets_a_member_and_old_ones_keep_their_numbers(tmp_path,
                                                                monkeypatch):
    """块模型里新冒出来的 colo 必须自动补成员，且既有编号一位都不许动。

    2026-11-05 实测：块模型 29 条 h2 渠道，`warp-outbound.json` 只有 19 个成员，
    BOM/FRA/GIG/LHR 四个新 colo 永远渲染不出来 —— `fixed_order` 只认既有成员。
    """
    mod = _render_mod()
    warp = tmp_path / ".warp"
    warp.mkdir()
    (warp / "warp-outbound.json").write_text(json.dumps([
        {"tag": "WARP-H2-NRT-0"}, {"tag": "WARP-H2-NRT-13"},
        {"tag": "WARP-H2-LAX-15"},
    ]))
    monkeypatch.setattr(mod, "PROJECT", tmp_path)
    chs = [{"colo": c} for c in ("NRT", "NRT", "LAX", "BOM")]
    order, added = mod._member_order(chs)
    assert [t for t, _ in order[:3]] == ["WARP-H2-NRT-0", "WARP-H2-NRT-13",
                                         "WARP-H2-LAX-15"]
    # 续号从既有最大编号 +1 起：16，不是 1，也不是成员个数
    assert added == ["WARP-H2-BOM-16"], added
    assert order[3] == ("WARP-H2-BOM-16", "BOM")


def test_missing_node_skips_one_line_not_the_whole_batch(tmp_path, monkeypatch):
    """落地节点从订阅里消失时只跳过那一条线。

    2026-11-05 远端部署实测：`normal.cn.greatgameupload.org:25526-any` 既不在
    主配置也不在沉睡池，`node_outbound` 抛 KeyError 冒到 `render()` 外面 ——
    H2 渲染整批失败，22 条线一条都没写，输出里只有一段 traceback。
    """
    mod = _render_mod()
    warp = tmp_path / ".warp"
    (warp / "h2").mkdir(parents=True)
    for name in ("usque10", "usque11"):
        (warp / name).mkdir()
        (warp / name / "config.json").write_text("{}")
    (warp / "warp-outbound.json").write_text(json.dumps([
        {"tag": "WARP-H2-NRT-0"}, {"tag": "WARP-H2-NRT-1"}]))
    monkeypatch.setattr(mod, "PROJECT", tmp_path)
    monkeypatch.setattr(mod, "free_devices", lambda *a, **k: ["usque10", "usque11"])
    monkeypatch.setattr(mod, "h2_channels", lambda: [
        {"tag": "WARP-H2-NRT-0", "member_tag": "WARP-H2-NRT-0", "colo": "NRT",
         "node": "n-gone", "nodes": ["n-gone"]},
        {"tag": "WARP-H2-NRT-1", "member_tag": "WARP-H2-NRT-1", "colo": "NRT",
         "node": "n-ok", "nodes": ["n-ok"]},
    ])

    def _ob(tag):
        if tag == "n-gone":
            raise KeyError(tag)
        return {"type": "socks", "tag": tag}

    monkeypatch.setattr(mod, "node_outbound", _ob)
    monkeypatch.setattr(mod, "_alive_many", lambda *a, **k: {})
    rc = mod.render(print_only=True)
    assert rc == 0, f"一条线缺节点不该让整批失败 (rc={rc})"


def _render_full(tmp_path, monkeypatch, mod, nodes):
    """在 tmp 项目里真的落盘渲染一次，返回生成的 singbox.json。"""
    warp = tmp_path / ".warp"
    (warp / "h2").mkdir(parents=True)
    for name in ("usque10", "usque11"):
        (warp / name).mkdir()
        (warp / name / "config.json").write_text("{}")
    (warp / "warp-outbound.json").write_text(json.dumps([
        {"tag": "WARP-H2-NRT-0"}, {"tag": "WARP-H2-NRT-1"}]))
    units = tmp_path / "units"
    units.mkdir()
    monkeypatch.setattr(mod, "PROJECT", tmp_path)
    monkeypatch.setattr(mod, "free_devices",
                        lambda *a, **k: ["usque10", "usque11"])
    monkeypatch.setattr(mod, "get_singbox_secret", lambda: "test-secret")
    monkeypatch.setattr(mod, "_alive_many", lambda *a, **k: {})
    monkeypatch.setattr(mod, "h2_channels", lambda: [
        {"tag": "WARP-H2-NRT-0", "member_tag": "WARP-H2-NRT-0", "colo": "NRT",
         "node": "n1", "nodes": ["n1", "n2"]},
        {"tag": "WARP-H2-NRT-1", "member_tag": "WARP-H2-NRT-1", "colo": "NRT",
         "node": "n2", "nodes": ["n2", "n3"]},
    ])
    monkeypatch.setattr(mod, "node_outbound",
                        lambda t: {"type": "socks", "tag": t})
    monkeypatch.setenv("ASIP_SYSTEM_UNIT_DIR", str(units))
    assert mod.render() == 0
    return json.loads((warp / "h2" / "singbox.json").read_text())


def test_each_line_routes_through_a_selector(tmp_path, monkeypatch):
    """换 H2 节点必须能只动一条线：规则落在 selector 上，而不是直接指出站。

    直接指出站时，换节点只能改共享配置并重启 asip-h2-singbox —— 31 条线一起
    断（既定事实 13d 第 2 条）。selector 让 daemon 用 `PUT /proxies/<sel>`
    切到已声明的候选出站，进程不动。
    """
    mod = _render_mod()
    cfg = _render_full(tmp_path, monkeypatch, mod, None)
    outb = {o["tag"]: o for o in cfg["outbounds"]}
    sels = [o for o in cfg["outbounds"] if o["type"] == "selector"]
    assert len(sels) == 2, f"每条线一个 selector，实际 {len(sels)}"
    for rule in cfg["route"]["rules"]:
        assert outb[rule["outbound"]]["type"] == "selector", rule
    # 候选池全量声明：不在配置里的出站 PUT 不过去（sing-box 回 400）。
    # 同块多条线共用整个块池（`pools_by_colo`），当前节点排在各自池首。
    assert outb["h2-sel-0"]["outbounds"] == ["h2-n1", "h2-n2", "h2-n3"]
    assert outb["h2-sel-0"]["default"] == "h2-n1"
    assert outb["h2-sel-1"]["outbounds"] == ["h2-n2", "h2-n1", "h2-n3"]
    assert outb["h2-sel-1"]["default"] == "h2-n2"
    # 出站按节点全局去重：n2 被两条线共用，只写一份。
    assert [o["tag"] for o in cfg["outbounds"]].count("h2-n2") == 1
    assert cfg["experimental"]["clash_api"]["secret"] == "test-secret"


def test_h2_switch_is_a_selector_put_and_never_restarts(monkeypatch):
    """H2 换节点 = PUT selector；成功即生效，不重启 usque、不重启共享 sing-box。"""
    import daemon

    calls = []

    class Fake:
        def request_json(self, method, path, body=None, **k):
            calls.append((method, path, body))
            return 204, None

    monkeypatch.setattr(daemon, "_h2_clash", lambda: Fake())
    m = {"tag": "WARP-H2-NRT-0", "kind": "h2", "index": 0}
    assert daemon._h2_select(m, "n2") == "ok"
    assert calls == [("PUT", "proxies/h2-sel-0", {"name": "h2-n2"})], calls
    # 节点不在候选池 → 400 → "gone"（该重渲染，而不是重试）
    Fake.request_json = lambda *a, **k: (400, None)
    assert daemon._h2_select(m, "nope") == "gone"


def test_h2_repair_switches_node_before_restarting(monkeypatch):
    """取不到出口时先换节点：重启 usque 换的是同一节点上的新会话，救不了。"""
    import daemon

    monkeypatch.setattr(daemon, "_warp", lambda: {"node_rotation_interval_sec": 60})
    monkeypatch.setattr(daemon, "_warp_member",
                        lambda t: {"tag": t, "kind": "h2", "index": 3})
    monkeypatch.setattr(daemon, "_h2_clash", lambda: type("F", (), {
        "selector_state": lambda self, s: (["h2-a", "h2-b", "h2-c"], "h2-a")})())
    moved, inval, restarts = [], [], []
    monkeypatch.setattr(daemon, "_rehome_one",
                        lambda t, n: (moved.append(n), "ok")[1])
    monkeypatch.setattr(daemon, "_invalidate_warp_line_ip",
                        lambda t: inval.append(t))
    monkeypatch.setattr(daemon, "_warp_line_restart",
                        lambda *a, **k: restarts.append(a))
    daemon._NODE_NEXT_CHECK.clear()
    assert daemon._h2_repair({"tag": "WARP-H2-X-3", "kind": "h2", "index": 3})
    # 按池序轮转：当前 a 的下一个是 b
    assert moved == ["b"] and inval == ["WARP-H2-X-3"]
    assert restarts == [], "换节点路径不该重启任何东西"
    # 当前是池尾时回绕到池首
    monkeypatch.setattr(daemon, "_h2_clash", lambda: type("F", (), {
        "selector_state": lambda self, s: (["h2-a", "h2-b"], "h2-b")})())
    daemon._NODE_NEXT_CHECK.clear()
    daemon._h2_repair({"tag": "WARP-H2-X-3", "kind": "h2", "index": 3})
    assert moved[-1] == "a", moved
    # 节流：一个 interval 内不再换
    daemon._NODE_NEXT_CHECK["WARP-H2-X-3"] = 9e18
    assert not daemon._h2_repair({"tag": "WARP-H2-X-3", "kind": "h2",
                                  "index": 3})


def test_ledger_devices_count_as_capacity(tmp_path, monkeypatch):
    """台账里的设备必须算进容量，否则原地重渲染被 `n < live_n` 拒掉。

    线上实测：19 条线、19 台设备全在台账里，`free_devices()` 只剩 1 台新设备，
    于是 n=1 < live_n=19，渲染器拒绝工作 —— 而其实一台设备都不用换。
    """
    mod = _render_mod()
    warp = tmp_path / ".warp"
    (warp / "h2").mkdir(parents=True)
    for name in ("usque10", "usque11"):
        (warp / name).mkdir()
        (warp / name / "config.json").write_text("{}")
    (warp / "h2" / "channels.json").write_text(json.dumps([
        {"index": 0, "device": "usque10"},
        {"index": 1, "device": "usque11"},
    ]))
    # 成员顺序来自这份出站表：没有它 fixed_order 为空，一条都排不出来。
    (warp / "warp-outbound.json").write_text(json.dumps([
        {"tag": "WARP-H2-NRT-0"}, {"tag": "WARP-H2-NRT-1"},
    ]))
    monkeypatch.setattr(mod, "PROJECT", tmp_path)
    # 空闲设备 0 台：全部被 active 单元占着。
    monkeypatch.setattr(mod, "free_devices", lambda *a, **k: [])
    monkeypatch.setattr(mod, "h2_channels", lambda: [
        {"tag": "WARP-H2-NRT-0", "member_tag": "WARP-H2-NRT-0", "colo": "NRT",
         "node": "n1", "nodes": ["n1"]},
        {"tag": "WARP-H2-NRT-1", "member_tag": "WARP-H2-NRT-1", "colo": "NRT",
         "node": "n2", "nodes": ["n2"]},
    ])
    monkeypatch.setattr(mod, "node_outbound", lambda t: {"type": "socks",
                                                         "tag": t})
    monkeypatch.setattr(mod, "_alive_many", lambda *a, **k: {})
    # 护栏本身必须仍然拦得住：这里要的是「不触发」，不是「绕过」。
    rc = mod.render(print_only=True)
    assert rc == 0, f"台账设备没被算进容量，渲染被拒 (rc={rc})"
