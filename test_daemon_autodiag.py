"""订阅刷新后「新节点自动实测」的节点挑选逻辑（daemon._pending_probe_nodes）。

真正跑探测的一步不在这里测：它要起 sing-box、占设备会话。这里只锁**挑选
规则**，因为挑错了要么永远测不到新源，要么反复重测同一批。

用例用 AST 抽取函数体，不 import 整个 daemon（import 会拉起线程和全局状态）。
"""
import ast
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
# _h2_fail_fresh 是 _pending_probe_nodes 的同模块依赖（失败结论 TTL）：
# AST 沙箱不 import 整个 daemon，helper 不列进来就会 NameError。
NAMES = ("_pending_probe_nodes", "_ensure_spare_devices", "_h2_fail_fresh")


def _func():
    sys.path.insert(0, str(PROJECT))
    tree = ast.parse((PROJECT / "daemon.py").read_text())
    picked = [n for n in tree.body if getattr(n, "name", None) in NAMES]
    assert len(picked) == len(NAMES)

    class _Cfg(dict):
        pass

    module_cfg = {"singbox": {"config_file": ""}}
    # helper 用到的模块级名字（真 daemon 里是全局），沙箱里补齐：
    # time 用真时钟（ts 由用例写成相对当前时刻），_warp 给一份最小配置。
    box: dict = {"json": json, "CFG": module_cfg, "warp_blocks": None,
                 "log": lambda *a, **k: None, "time": time,
                 "_warp": lambda: {"h2_probe_fail_ttl_sec": 21600}}
    exec(compile(ast.Module(body=picked, type_ignores=[]), "daemon.py", "exec"),
         box)
    return box, module_cfg


def _setup(tmp_path, outs, mapped, ledger):
    """写一份最小配置 + 台账，并让 CFG 指向它。"""
    import warp_blocks
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"outbounds": outs}))
    (tmp_path / "cfg.json.sources.json").write_text(json.dumps(ledger))
    (tmp_path / ".warp").mkdir(exist_ok=True)
    bm = tmp_path / ".warp" / "block-map.json"
    bm.write_text(json.dumps({"nodes": mapped}))
    warp_blocks.map_path = lambda: bm
    return str(cfg)


def _out(tag, server="h"):
    return {"tag": tag, "type": "anytls", "server": server, "server_port": 443}


def test_unmapped_nodes_are_picked_and_mapped_ones_skipped(tmp_path, monkeypatch):
    import warp_blocks

    cfg = _setup(
        tmp_path,
        outs=[_out("a:443-any"), _out("b:443-any"), _out("c:443-any")],
        mapped={"a:443-any": {"colo": "NRT", "category": "regular"}},
        ledger={"a:443-any": {"sub": "okz"}, "b:443-any": {"sub": "okz"},
                "c:443-any": {"sub": "new"}})
    box, module_cfg = _func()
    module_cfg["singbox"]["config_file"] = cfg
    box["warp_blocks"] = warp_blocks
    picked = {x["tag"] for x in box["_pending_probe_nodes"]()}
    assert picked == {"b:443-any", "c:443-any"}   # a 已有 colo，不重测


def test_new_source_gets_a_slot_even_when_old_source_is_huge(tmp_path):
    """老源几百个节点也不能把新源挤出去。

    按顺序取前 N 个时，实测新加的源永远轮不上（老源排前面且数量多）。
    """
    import warp_blocks

    old = [_out(f"old{i}:443-any") for i in range(20)]
    cfg = _setup(
        tmp_path,
        outs=old + [_out("new:443-any")],
        mapped={},
        ledger={**{o["tag"]: {"sub": "okz"} for o in old},
                "new:443-any": {"sub": "yyd2tett"}})
    box, module_cfg = _func()
    module_cfg["singbox"]["config_file"] = cfg
    box["warp_blocks"] = warp_blocks
    picked = [x["tag"] for x in box["_pending_probe_nodes"](limit=4)]
    assert "new:443-any" in picked
    assert len(picked) == 4


def test_quota_is_proportional_so_a_big_new_source_is_not_starved(tmp_path):
    """名额按待测数成比例，不是每源一个。

    每源一个看着公平，实测后果是：yyd2tett 49 个待测只拿 1 个名额/轮，
    49 轮（30min 一轮）≈ 24 小时才测完；而 etcloud 11 个 11 轮就空了。
    「新源进不来」的真正原因是这个配额，不是它加入得晚。
    """
    import warp_blocks

    big = [_out(f"big{i}:443-any") for i in range(30)]
    small = [_out(f"small{i}:443-any") for i in range(10)]
    cfg = _setup(
        tmp_path, outs=big + small, mapped={},
        ledger={**{o["tag"]: {"sub": "big"} for o in big},
                **{o["tag"]: {"sub": "small"} for o in small}})
    box, module_cfg = _func()
    module_cfg["singbox"]["config_file"] = cfg
    box["warp_blocks"] = warp_blocks
    picked = [x["tag"] for x in box["_pending_probe_nodes"](limit=12)]
    n_big = sum(1 for t in picked if t.startswith("big"))
    assert len(picked) == 12
    # 30:10 的理想配额是 9:3；允许 ±1 的取整误差
    assert n_big >= 8, f"大源只拿到 {n_big} 个名额，仍会被饿死"


def test_limit_is_respected(tmp_path):
    import warp_blocks

    outs = [_out(f"n{i}:443-any") for i in range(30)]
    cfg = _setup(tmp_path, outs=outs, mapped={},
                 ledger={o["tag"]: {"sub": "s"} for o in outs})
    box, module_cfg = _func()
    module_cfg["singbox"]["config_file"] = cfg
    box["warp_blocks"] = warp_blocks
    assert len(box["_pending_probe_nodes"](limit=5)) == 5


def test_non_connectable_outbounds_are_ignored(tmp_path):
    """selector/direct 这些没有 server，不该进探测队列。"""
    import warp_blocks

    cfg = _setup(tmp_path, outs=[
        {"type": "selector", "tag": "proxy", "outbounds": ["x:443-any"]},
        {"type": "direct", "tag": "direct"},
        _out("x:443-any"),
    ], mapped={}, ledger={"x:443-any": {"sub": "s"}})
    box, module_cfg = _func()
    module_cfg["singbox"]["config_file"] = cfg
    box["warp_blocks"] = warp_blocks
    picked = [x["tag"] for x in box["_pending_probe_nodes"]()]
    assert picked == ["x:443-any"]


def _fake_devices(monkeypatch, registered, active, fail_at=None):
    """让 `import warp_diag / warp_wg` 拿到桩模块（真模块会去问 systemctl）。"""
    import types
    wd = types.ModuleType("warp_diag")
    wd.registered_slots = lambda: list(registered)
    wd.active_slots = lambda: set(active)
    ww = types.ModuleType("warp_wg")
    made = []

    def register_line(i, force=False):
        if fail_at is not None and i >= fail_at:
            raise RuntimeError("429")
        made.append(i)
        registered.append(i)

    ww.register_line = register_line
    monkeypatch.setitem(sys.modules, "warp_diag", wd)
    monkeypatch.setitem(sys.modules, "warp_wg", ww)
    return made


def test_spare_devices_are_not_registered_again(monkeypatch):
    """已有足够空闲设备时不该白注册 —— 每次注册都是一台真实的 CF 设备。"""
    made = _fake_devices(monkeypatch, registered=[1, 2, 3], active={1, 2})
    box, _ = _func()
    assert box["_ensure_spare_devices"](want=1) == [3]
    assert made == []


def test_shortage_registers_new_slots_after_the_highest(monkeypatch):
    """设备不够就补注册，且**接着最大槽位往后编**：覆盖已注册号会让两条线
    抢同一份 profile，而 profile 就是那台设备的身份。"""
    made = _fake_devices(monkeypatch, registered=[1, 2, 3, 7], active={1, 2})
    box, _ = _func()
    spare = box["_ensure_spare_devices"](want=4)
    assert made == [8, 9]
    assert spare == [3, 7, 8, 9]


def test_registration_cap_per_round(monkeypatch):
    """一轮最多补 max_new 台：CF 一个窗口约限流 9 台，补太多只是把限流
    窗口越拉越长。剩下的留给下一轮（30min 后）。"""
    made = _fake_devices(monkeypatch, registered=[1, 2], active={1, 2})
    box, _ = _func()
    box["_ensure_spare_devices"](want=10)
    assert len(made) == 3


def test_rate_limited_registration_stops_the_round(monkeypatch):
    """撞 429 后不再往下试：变成一台就退避 30/90/240s，3 台足够占满一轮
    探测的时间预算，继续试只会把后续轮次的额度一起烧掉。"""
    made = _fake_devices(monkeypatch, registered=[1], active={1}, fail_at=3)
    box, _ = _func()
    assert box["_ensure_spare_devices"](want=5) == [2]
    assert made == [2]


def test_every_source_is_drained_before_any_source_repeats(tmp_path):
    """小源先被取空：比例配额不能把小源饿到 0 个名额。"""
    import warp_blocks

    big = [_out(f"big{i}:443-any") for i in range(50)]
    tiny = [_out("tiny0:443-any")]
    cfg = _setup(
        tmp_path, outs=big + tiny, mapped={},
        ledger={**{o["tag"]: {"sub": "big"} for o in big},
                "tiny0:443-any": {"sub": "tiny"}})
    box, module_cfg = _func()
    module_cfg["singbox"]["config_file"] = cfg
    box["warp_blocks"] = warp_blocks
    picked = [x["tag"] for x in box["_pending_probe_nodes"](limit=5)]
    assert "tiny0:443-any" in picked


def test_h2_probe_uses_its_own_ledger_not_wg_colo(tmp_path):
    """H2 待测名单不能因为「WG 已测出 colo」就把节点排掉。

    实测 2026-11-04：296 个生产节点里只有 61 个有 h2 结论。共用 WG 的
    colo 判据会让 H2 探测永远认为「已测过」，`carrier=h2` 再也不会增加，
    H2 池子就死在老节点上 —— 这正是「节点进不了 H2」的根因。
    """
    import warp_blocks

    outs = [_out("a:443-any"), _out("b:443-any"), _out("c:443-any")]
    # a 已被 WG 测出 colo；b/c 从没测过 H2
    cfg = _setup(tmp_path, outs=outs,
                 mapped={"a:443-any": {"colo": "NRT", "category": "regular"}},
                 ledger={o["tag"]: {"sub": "s"} for o in outs})
    h2state = tmp_path / "h2_state.json"
    h2state.write_text(json.dumps({"nodes": {
        # ts 必须是「最近」：失败结论有 6h TTL，过期的要重新测（见下一条用例）。
        "b:443-any": {"tested": True, "ok": False, "ts": time.time()},
    }}))

    box, module_cfg = _func()
    module_cfg["singbox"]["config_file"] = cfg
    box["warp_blocks"] = warp_blocks

    # WG 视角：a 有 colo → 跳过
    wg = {x["tag"] for x in box["_pending_probe_nodes"]()}
    assert "a:443-any" not in wg
    assert {"b:443-any", "c:443-any"} <= wg

    # H2 视角：a 有 WG colo 但没测过 H2 → 仍要测；b 测过且失败 → 跳过
    h2 = {x["tag"] for x in box["_pending_probe_nodes"](state_path=str(h2state))}
    assert "a:443-any" in h2, "有 WG colo 不等于测过 H2"
    assert "b:443-any" not in h2, "H2 测过且失败的不该重测"
    assert "c:443-any" in h2


def test_h2_failed_node_retested_after_ttl(tmp_path):
    """失败结论不是永久属性：TTL 过期 / 没有时间戳的都要重新排队。

    实测 11-05 判失败、11-11 复活的节点曾因「永久跳过」永不复测，
    H2 供给只减不增（158 条失败节点里 157 条超过 24h）。
    """
    import warp_blocks

    outs = [_out("a:443-any"), _out("b:443-any"), _out("c:443-any")]
    cfg = _setup(tmp_path, outs=outs, mapped={},
                 ledger={o["tag"]: {"sub": "s"} for o in outs})
    h2state = tmp_path / "h2_state.json"
    h2state.write_text(json.dumps({"nodes": {
        "a:443-any": {"tested": True, "ok": False, "ts": time.time()},  # 最近的失败
        "b:443-any": {"tested": True, "ok": False, "ts": time.time() - 21601},
        "c:443-any": {"tested": True, "ok": False},                     # 旧记录无 ts
    }}))
    box, module_cfg = _func()
    module_cfg["singbox"]["config_file"] = cfg
    box["warp_blocks"] = warp_blocks

    got = {x["tag"] for x in box["_pending_probe_nodes"](state_path=str(h2state))}
    assert "a:443-any" not in got, "TTL 内的失败不该立刻重测"
    assert {"b:443-any", "c:443-any"} <= got, "过期/无时间戳的失败必须重测"


def test_h2_probe_retests_nodes_that_passed_but_never_merged(tmp_path):
    """H2 测过且**成功**的节点要重测：成功不等于已入块。

    与 WG 侧同一个坑：测成功但没跑 merge 的节点，台账里是 ok=True，
    若不重测，它的 colo 只留在 JSONL 里，block-map 永远没有它。
    """
    import warp_blocks

    cfg = _setup(tmp_path, outs=[_out("a:443-any")], mapped={},
                 ledger={"a:443-any": {"sub": "s"}})
    h2state = tmp_path / "h2_state.json"
    h2state.write_text(json.dumps({"nodes": {
        "a:443-any": {"tested": True, "ok": True}}}))
    box, module_cfg = _func()
    module_cfg["singbox"]["config_file"] = cfg
    box["warp_blocks"] = warp_blocks
    assert "a:443-any" in {
        x["tag"] for x in box["_pending_probe_nodes"](state_path=str(h2state))}


def test_h2_probe_skips_nodes_that_passed_and_landed(tmp_path):
    """H2 测过、成功、**且已落进块映射**的节点不再占名额。

    2026-11-08 实测：12 个待测名额里 6 个是刚测通过的节点 —— 判据只跳
    `tested and not ok`，成功过的一律留下，于是探测每小时空转一轮，而
    JNB/IAD/IST/SYD 这些真新节点永远排不进来。`carrier=h2` 长期不增长
    就是这么来的。

    与上一个用例是一对：`ok=True` 但块映射里没有它 → 必须重测（合并丢了）；
    `ok=True` 且块映射里有 → 跳过（已经入池）。
    """
    import warp_blocks

    cfg = _setup(tmp_path, outs=[_out("a:443-any"), _out("b:443-any")],
                 mapped={"a:443-any": {"colo": "NRT", "carrier": "h2",
                                       "h2": {"colo": "NRT"}}},
                 ledger={"a:443-any": {"sub": "s"}, "b:443-any": {"sub": "s"}})
    h2state = tmp_path / "h2_state.json"
    h2state.write_text(json.dumps({"nodes": {
        "a:443-any": {"tested": True, "ok": True},
        "b:443-any": {"tested": True, "ok": True}}}))
    box, module_cfg = _func()
    module_cfg["singbox"]["config_file"] = cfg
    box["warp_blocks"] = warp_blocks
    got = {x["tag"] for x in box["_pending_probe_nodes"](state_path=str(h2state))}
    assert "a:443-any" not in got, "已落账的测通节点不该再占名额"
    assert "b:443-any" in got, "测通但没落账的仍要重测"


def test_missing_ledger_is_not_fatal(tmp_path, monkeypatch):
    """台账还没生成时返回空表，不能让刷新流程炸掉。"""
    import warp_blocks

    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"outbounds": [_out("x:443-any")]}))
    box, module_cfg = _func()
    module_cfg["singbox"]["config_file"] = str(cfg)
    box["warp_blocks"] = warp_blocks
    assert box["_pending_probe_nodes"]() == []
