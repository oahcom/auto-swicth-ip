#!/usr/bin/env python3
"""opencode 专属闸口（kind="opencode"）契约 C1 的确定性单元测试。

无网络、不碰生产闸口 / 生产 sing-box / 生产台账：Clash API 全打桩、
账本指 tmp_path、daemon 的重启与公共记账函数全部打点监视。
任务书验证 4 的四条硬用例：
  ① /rotate-opencode-gate 只动 opencode 闸口 selector，其它闸口不变
  ② 一批并发报障只产生一次换节点（合并窗）
  ③ 换节点走不中断路径（断言没调重启函数）
  ④ 记账落独立账本，公共 node_info 一概不写
"""
import time
from concurrent.futures import ThreadPoolExecutor

import json
import os

import pytest

import config
import daemon
import parse_subscription
from asip import opencode_gate


# ── 沙箱 ─────────────────────────────────────────────────────────────

class FakeWorld:
    """多槽位 selector 状态的沙箱：记录每一次 PUT，可断言谁被动过。"""

    def __init__(self):
        self.states = {}   # (clash_api_port, selector) -> {"now", "all"}
        self.puts = []     # (clash_api_port, selector, name)

    def put_state(self, port, selector, now, all_names):
        self.states[(port, selector)] = {"now": now, "all": list(all_names)}

    def snapshot(self):
        return {k: v["now"] for k, v in self.states.items()}


class FakeClient:
    """ClashClient 同面（selector_state / current / select）的打桩。"""

    def __init__(self, port, world):
        self.port = port
        self.world = world

    def selector_state(self, selector):
        st = self.world.states.get((self.port, selector))
        if not st:
            return None, None
        return list(st["all"]), st["now"]

    def current(self, selector):
        st = self.world.states.get((self.port, selector))
        return st["now"] if st else None

    def select(self, selector, name, timeout=None):
        st = self.world.states.get((self.port, selector))
        if st is None or name not in st["all"]:
            return False
        self.world.puts.append((self.port, selector, name))
        st["now"] = name
        return True


@pytest.fixture
def gate_env(monkeypatch, tmp_path):
    """打开 opencode 闸口（conftest 默认关）：真实 consumer_slots() 产出
    kind="opencode" 槽位；Clash API 打桩；账本指 tmp；模块状态清零。"""
    ledger = tmp_path / "og_ledger.jsonl"
    monkeypatch.setitem(config.CFG, "opencode_gate", {
        "slots": 1, "coalesce_ms": 800, "cooldown_sec": 300,
        "ledger_path": str(ledger),
    })

    world = FakeWorld()
    slots = config.consumer_slots()
    og_slot = [s for s in slots if s.get("kind") == "opencode"]
    assert len(og_slot) == 1
    og_slot = og_slot[0]
    # 全部槽位都放进沙箱：断言「其它闸口 selector 当前项不变」要有对象。
    for s in slots:
        sel = s.get("pool_selector") or s["selector"]
        world.put_state(s["clash_api_port"], sel,
                        "cur-%s" % s["name"], ["cur-%s" % s["name"], "n1",
                                               "n2", "n3"])
    # opencode 闸口的池 selector 放三个可换候选
    world.put_state(og_slot["clash_api_port"], og_slot["pool_selector"],
                    "n1", ["n1", "n2", "n3"])

    monkeypatch.setattr(opencode_gate, "_client",
                        lambda slot: FakeClient(slot["clash_api_port"], world))
    # 模块级状态清零：独立选中态/冷却/计数不跨用例串味
    opencode_gate._CLIENTS.clear()
    daemon._COOLDOWN_NODES.clear()
    st = opencode_gate._STATE
    st["current"] = None
    st["switched_at"] = 0.0
    st["last_attempt_ts"] = 0.0
    st["cooldown"].clear()
    st["egress_by_node"].clear()
    st["ledger_entries"] = 0
    for k in list(st["counts"]):
        st["counts"][k] = 0
    return type("Env", (), {"world": world, "slot": og_slot,
                            "ledger": ledger, "slots": slots})()


def _req(**kw):
    base = {"req_id": "req-1", "reason": "rate429",
            "egress_ip": "203.0.113.9", "request_started_at": time.time(),
            "retry_after_ms": 47000, "session_id": "sess-1"}
    base.update(kw)
    return base


def _reset_coalesce(monkeypatch, ms):
    monkeypatch.setitem(config.CFG["opencode_gate"], "coalesce_ms", ms)


# ── ① 只动 opencode 闸口自己的 selector ──────────────────────────────

def test_rotate_touches_only_own_selector(gate_env, monkeypatch):
    world = gate_env.world
    before = world.snapshot()
    my_key = (gate_env.slot["clash_api_port"], gate_env.slot["pool_selector"])

    def _boom(*a, **k):
        raise AssertionError("_pick_healthy_slot 不许被 opencode 闸口触碰")
    monkeypatch.setattr(daemon, "_pick_healthy_slot", _boom)

    resp = opencode_gate.handle_rotate(_req())
    assert resp["ok"] is True
    assert resp["switched"] is True
    assert resp["reason"] == "ok"
    assert resp["switch_to_node"] in ("n2", "n3")

    # 只发生了一次 PUT，且打在 opencode 闸口自己的 pool selector 上
    assert [(p, s) for p, s, _ in world.puts] == [(my_key[0], my_key[1])]
    # 其它闸口 selector 当前项一个都没变（含第七闸口 slot-7 与常规槽位）
    after = world.snapshot()
    changed = [k for k in before if after[k] != before[k]]
    assert changed == [my_key]
    # 防假绿：其它闸口的当前项基线必须真的有值可比
    for s in gate_env.slots:
        if s["name"] == gate_env.slot["name"]:
            continue
        sel = s.get("pool_selector") or s["selector"]
        assert after[(s["clash_api_port"], sel)] == "cur-%s" % s["name"]


# ── ② 合并窗：一批并发报障只换一次节点 ──────────────────────────────

def test_coalesce_window_single_switch(gate_env, monkeypatch):
    _reset_coalesce(monkeypatch, 10000)   # 10s 窗，杜绝慢机抖动
    world = gate_env.world

    def call(i):
        return opencode_gate.handle_rotate(_req(req_id="req-%d" % i))

    with ThreadPoolExecutor(max_workers=6) as pool:
        resps = list(pool.map(call, range(6)))

    switched = [r for r in resps if r["switched"]]
    coalesced = [r for r in resps if r["reason"] == "coalesced"]
    assert len(switched) == 1, resps
    assert len(coalesced) == 5, resps
    # 只换了一次节点
    assert len(world.puts) == 1
    assert switched[0]["switch_to_node"] == world.puts[0][2]
    # coalesced 不骗重放：switch_to_node 恒 None（契约：只在真换了时填）
    for r in coalesced:
        assert r["switch_to_node"] is None
        assert r["switched"] is False

    # 窗口过期后允许再换
    _reset_coalesce(monkeypatch, 0)
    r = opencode_gate.handle_rotate(_req(req_id="req-late"))
    assert r["switched"] is True
    assert len(world.puts) == 2


# ── ③ 换节点走「不中断在途」路径 ────────────────────────────────────

def test_rotate_does_not_restart_anything(gate_env, monkeypatch):
    world = gate_env.world
    calls = []

    def _spy(name):
        def f(*a, **k):
            calls.append(name)
            return None
        return f

    # 重启/断流/别人家轮换的全部入口打点：一个都不许碰
    for fn in ("_warp_line_restart", "restart_singbox", "_repair_slot",
               "_rehome_one", "_slot_put", "_proxy_gate_rotate_node",
               "maybe_reconnect_warp", "_fast_switch"):
        monkeypatch.setattr(daemon, fn, _spy(fn))

    resp = opencode_gate.handle_rotate(_req())
    assert resp["switched"] is True
    assert calls == [], "换节点不许调用任何重启/断流函数: %s" % calls
    # 正向证据：唯一动作是 selector PUT（等价 reconnect_warp 的换线语义）
    assert len(world.puts) == 1

    # 「不中断在途」的机制面：池型闸口渲染出的 selector 不掐在途连接
    warps = [{"tag": "WARP-A", "server_port": 1, "type": "socks"}]
    monkeypatch.setattr(parse_subscription, "_load_warp_outbounds",
                        lambda: list(warps))
    nodes = [{"tag": "n1", "server": "1.2.3.4", "server_port": 443,
              "type": "socks"},
             {"tag": "n2", "server": "5.6.7.8", "server_port": 443,
              "type": "socks"}]
    cfg = parse_subscription.make_config(
        nodes, listen_port=gate_env.slot["port"],
        clash_port=gate_env.slot["clash_api_port"],
        include_lan=False, default_to_pool=True)
    sels = {o["tag"]: o for o in cfg["outbounds"] if o.get("type") == "selector"}
    pool_sel = gate_env.slot["pool_selector"]
    warp_sel = gate_env.slot["selector"]
    assert sels[warp_sel]["default"] == pool_sel
    assert sels[warp_sel]["interrupt_exist_connections"] is False
    assert sels[pool_sel]["interrupt_exist_connections"] is False


# ── ④ 记账落独立账本，公共 node_info 一概不写 ───────────────────────

def test_ledger_isolated_from_node_info(gate_env, monkeypatch):
    touched = []

    def _spy(name):
        def f(*a, **k):
            touched.append(name)
            return None
        return f

    # 公共记账/公共冷却的全部写入口打点
    for fn in ("update_node_info_async", "_cool_down_node",
               "_mark_warp_ip_used", "_exit_ledger_note",
               "_warp_line_selected", "_set_warp_line_ip"):
        monkeypatch.setattr(daemon, fn, _spy(fn))

    resp = opencode_gate.handle_rotate(_req())
    assert resp["switched"] is True
    assert touched == [], "公共 node_info/公共冷却一概不许写: %s" % touched

    # 专属账本落盘（tmp），字段齐、归因对
    lines = gate_env.ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["req_id"] == "req-1"
    assert entry["outcome"] == "ok"
    assert entry["from_node"] == "n1"
    assert entry["to_node"] == resp["switch_to_node"]
    assert entry["egress_ip"] == "203.0.113.9"
    assert entry["switched"] is True

    # 冷却独立：旧节点进了模块自己的冷却表，没写 daemon._COOLDOWN_NODES
    snap = opencode_gate.snapshot()
    assert "n1" in snap["cooldown"]
    assert daemon._COOLDOWN_NODES.get("n1") is None
    assert snap["ledger_entries"] == 1
    assert snap["current"] == resp["switch_to_node"]


# ── 契约形状与降级路径 ──────────────────────────────────────────────

def test_contract_response_shape(gate_env, monkeypatch):
    """C1 响应七字段一字不差；reason 只取契约四个值。"""
    resp = opencode_gate.handle_rotate(_req())
    assert set(resp) == {"ok", "switched", "reason", "switch_to_node",
                         "switched_at", "active_egress", "ms"}
    assert resp["ok"] is True
    assert resp["ms"] == 0
    assert resp["reason"] in ("ok", "coalesced", "borrowed_skip",
                              "no_healthy_node")
    assert isinstance(resp["switched_at"], float)
    # active_egress = 事发出口（请求报来的 egress_ip）
    assert resp["active_egress"] == "203.0.113.9"


def test_borrowed_skip_and_no_healthy_node(gate_env, monkeypatch):
    # 借道：不冷节点、不换节点（D14）
    resp = opencode_gate.handle_rotate(_req(borrowed=True))
    assert resp["reason"] == "borrowed_skip"
    assert resp["switched"] is False
    assert gate_env.world.puts == []
    assert opencode_gate.snapshot()["cooldown"] == {}

    # 池里只剩当前节点：no_healthy_node，不硬切
    world = gate_env.world
    my = (gate_env.slot["clash_api_port"], gate_env.slot["pool_selector"])
    world.states[my] = {"now": "n1", "all": ["n1"]}
    _reset_coalesce(monkeypatch, 0)
    resp = opencode_gate.handle_rotate(_req(req_id="req-2"))
    assert resp["reason"] == "no_healthy_node"
    assert resp["switched"] is False
    assert world.puts == []


# ── 配置层：槽位产出委托 make_slot ──────────────────────────────────

def test_make_slot_and_consumer_slots_wiring(gate_env):
    slot = opencode_gate.make_slot(7890, 9090, 7)
    assert slot["name"] == "slot-8"
    assert slot["kind"] == "opencode"
    assert slot["port"] == 7898          # 7891 避让后的顺延编号
    assert slot["clash_api_port"] == 9098  # 9091 避让后的顺延编号
    assert slot["config_file"].endswith("singbox-config.json-slot8.json")
    assert slot["pool_selector"] == "proxy"
    assert slot["selector"] == "opencode-out"
    # consumer_slots() 的接线：6 WARP + 1 池闸 + 1 opencode 闸口
    slots = config.consumer_slots()
    assert len(slots) == 8
    og = slots[-1]
    assert og["kind"] == "opencode"
    assert og["port"] == 7898 and og["clash_api_port"] == 9098


def test_snapshot_is_readonly(gate_env):
    snap = opencode_gate.snapshot()
    assert snap["current"] is None
    assert snap["cooldown"] == {}
    assert snap["ledger_path"] == str(gate_env.ledger)
    assert snap["coalesce_win_sec"] == pytest.approx(0.8)
    # 只读：快照不产生账本写入
    assert not gate_env.ledger.exists()


# ── 专属流量日志：log_output 接线（make_slot → make_config → log.output）──

def _render_cfg(monkeypatch, **kw):
    """make_config 的确定性渲染：外挂文件全打桩，只看 log 段接线。"""
    monkeypatch.setattr(parse_subscription, "_load_warp_outbounds", lambda: [])
    monkeypatch.setattr(parse_subscription, "_load_dormant_outbounds", lambda: [])
    monkeypatch.setattr(parse_subscription, "_load_dormant_state", lambda _n: set())
    nodes = [{"tag": "n1", "server": "1.2.3.4", "server_port": 443,
              "type": "socks"}]
    return parse_subscription.make_config(
        nodes, listen_port=7899, clash_port=9099,
        include_lan=False, **kw)


def test_make_slot_log_output_points_to_dedicated_log(gate_env, monkeypatch):
    log_file = str(gate_env.ledger.parent / "opencode-gate.log")
    monkeypatch.setitem(config.CFG["opencode_gate"], "log_path", log_file)
    slot = opencode_gate.make_slot(7890, 9090, 7)
    assert slot["log_output"] == log_file
    # 缺键时回退常量路径：仍专属（指向 opencode-gate.log），不退回 None
    # 去混写公共 singbox.log
    monkeypatch.delitem(config.CFG["opencode_gate"], "log_path")
    slot2 = opencode_gate.make_slot(7890, 9090, 7)
    assert slot2["log_output"] is not None
    assert os.path.basename(slot2["log_output"]) == "opencode-gate.log"
    assert opencode_gate.log_path() == slot2["log_output"]


def test_make_config_log_output_sets_output_key(monkeypatch):
    cfg = _render_cfg(monkeypatch, log_output="/tmp/oc-test.log")
    assert cfg["log"]["output"] == "/tmp/oc-test.log"
    assert cfg["log"]["level"] == "info"
    assert cfg["log"]["timestamp"] is True


def test_make_config_default_log_matches_legacy(monkeypatch):
    """不传 log_output：log 段与改动前逐字等价（无 output 键），
    其它槽位/无槽位渲染零回归。"""
    cfg = _render_cfg(monkeypatch)
    assert cfg["log"] == {"level": "info", "timestamp": True}
    assert "output" not in cfg["log"]
    # 显式 None 与不传同义
    cfg2 = _render_cfg(monkeypatch, log_output=None)
    assert cfg2["log"] == {"level": "info", "timestamp": True}


def test_recent_rotations_reads_tail_of_ledger(gate_env):
    now = time.time()
    entries = [
        {"ts": now - 10, "reason": "rate429", "from_node": "n0",
         "to_node": "n1", "session_id": "s0", "outcome": "ok",
         "switched": True},
        {"ts": now - 5, "reason": "rate429", "from_node": "n1",
         "to_node": None, "session_id": "s1", "outcome": "coalesced",
         "switched": False},
        {"ts": now - 1, "reason": "transport", "from_node": "n1",
         "to_node": "n2", "session_id": "s2", "outcome": "ok",
         "switched": True},
    ]
    with open(gate_env.ledger, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    # 喂 3 条取最近 2 条（时间序，旧→新）
    rows = opencode_gate.recent_rotations(2)
    assert [r["from_node"] for r in rows] == ["n1", "n1"]
    assert [r["to_node"] for r in rows] == [None, "n2"]
    assert len(opencode_gate.recent_rotations(None)) == 3
    assert opencode_gate.recent_rotations(0) == []
    summ = opencode_gate.rotation_summary()
    assert summ["last"]["to_node"] == "n2"
    assert summ["calls_in_window"] == 3
    assert summ["rotations_in_window"] == 2   # coalesced 不算真换节点


# ── opencode 应用日志（asip oc app）：能读就读，缺位优雅降级 ──────────

def test_opencode_log_readable(tmp_path, monkeypatch):
    f = tmp_path / "opencode.log"
    f.write_text("line1\nERROR something bad\nFree usage limit hit\nline4\n",
                 encoding="utf-8")
    monkeypatch.setenv("OPENCODE_LOG", str(f))
    # env 覆盖优先（不许硬编码路径）
    assert opencode_gate.opencode_log_path() == str(f)
    info = opencode_gate.opencode_log_info()
    assert info["path"] == str(f)
    assert info["exists"] is True
    assert info["readable"] is True
    assert info["lines"] == 4
    assert info["size"] == f.stat().st_size
    # last_alert = 最近一条命中行（Free usage limit 晚于 ERROR）
    assert "Free usage limit" in info["last_alert"]


def test_opencode_log_unreadable(tmp_path, monkeypatch):
    missing = tmp_path / "no-such-opencode.log"
    monkeypatch.setenv("OPENCODE_LOG", str(missing))
    info = opencode_gate.opencode_log_info()
    # 不可读也给结构化结果，绝不抛异常（能读就读）
    assert info["path"] == str(missing)
    assert info["exists"] is False
    assert info["readable"] is False
    assert info["lines"] is None
    assert info["last_alert"] is None


# ── B1 选点注入：picker 对齐 pick_next_node（P2C），缺省均匀随机 ──────

def test_picker_injected_chooses_picker_node(gate_env, monkeypatch):
    """picker 说了算：返回谁就换谁（daemon 委托点注入 pick_next_node 同接线）。"""
    _reset_coalesce(monkeypatch, 0)
    seen = []

    def picker(cur, all_nodes, fail_set):
        seen.append((cur, set(all_nodes), set(fail_set)))
        return "n2"

    resp = opencode_gate.handle_rotate(_req(), picker=picker)
    assert resp["switched"] is True
    assert resp["switch_to_node"] == "n2"
    assert gate_env.world.puts[0][2] == "n2"
    # 注入面：current=事发节点、all_nodes=池全体、fail_set=独立冷却中
    cur, all_nodes, fail_set = seen[0]
    assert cur == "n1"
    assert all_nodes == {"n1", "n2", "n3"}
    assert "n1" in fail_set   # 报障后旧节点先入独立冷却表，经 fail_set 交出去


def test_picker_none_falls_back_to_uniform_random(gate_env, monkeypatch):
    """picker 缺省回退均匀随机：仍换节点，落在池内且非当前。"""
    _reset_coalesce(monkeypatch, 0)
    resp = opencode_gate.handle_rotate(_req())
    assert resp["switched"] is True
    assert resp["switch_to_node"] in ("n2", "n3")
    assert resp["switch_to_node"] != "n1"
    assert len(gate_env.world.puts) == 1


def test_fault_cools_old_node_but_proactive_does_not(gate_env, monkeypatch):
    """冷却语义照第七闸口：报障冷却旧节点、proactive 不冷；独立冷却表。"""
    _reset_coalesce(monkeypatch, 0)
    resp = opencode_gate.handle_rotate(_req(reason="rate429"))
    assert resp["switched"] is True
    snap = opencode_gate.snapshot()
    assert "n1" in snap["cooldown"]                    # 报障：旧节点入独立冷却
    assert daemon._COOLDOWN_NODES.get("n1") is None    # 没写公共冷却表

    cur = resp["switch_to_node"]
    _reset_coalesce(monkeypatch, 0)
    resp2 = opencode_gate.handle_rotate(
        _req(reason="proactive", req_id="req-2"))
    assert resp2["switched"] is True
    assert cur not in opencode_gate.snapshot()["cooldown"]   # proactive 不冷
    assert daemon._COOLDOWN_NODES == {}                      # 公共表始终没人写
