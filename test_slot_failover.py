#!/usr/bin/env python3
"""槽位错误切换的确定性单元测试（无网络、<5s）。

验证用户指定的设计：
  - _fast_switch：只做同步翻转，不管 owner 标记/后台（那是 _kick_background_repair 的活）
  - _kick_background_repair：秒切之前启动，把旧闸扔后台做错误切换（无论秒切成不成）
  - _repair_slot：错误切换 = 先换线、探新线、按新线结果标记（不再探旧线决定留不留）
  - assign_consumer_slots：跳过 serving 与 repairing
"""
import time
import types

import pytest

import daemon


# port 与 config.consumer_slots() 一致：7890、7892-7896（7891 被 LAN 入站占）
SLOTS = [{"name": "slot-%d" % i, "selector": "opencode-out-%d" % i,
          "port": 7890 if i == 1 else 7890 + i} for i in range(1, 7)]


class _NoopThread:
    """吞掉 target，不跑 —— 让后台线程不干扰同步断言。"""
    def __init__(self, target=None, args=(), kwargs=None, **kw):
        self.target = target
        self.args = args
    def start(self):
        pass
    def join(self, timeout=None):
        pass


@pytest.fixture
def isolated(monkeypatch):
    # 固定默认档位，避免测试随 config.py 的生产实验值漂移。
    # 需要测 all / off 的用例自己在函数内 setitem 覆盖（monkeypatch 后设先生效）。
    monkeypatch.setitem(daemon.CFG["warp"], "rate429_recheck_mode", "borrowed")
    monkeypatch.setattr(daemon.threading, "Thread", _NoopThread)
    monkeypatch.setattr(daemon, "consumer_slots", lambda: SLOTS)

    current = {"slot-1": ("WARP-A", "2606:a::1"),
               "slot-2": ("WARP-B", "2606:b::2"),
               "slot-3": ("WARP-C", "2606:c::3"),
               "slot-4": ("WARP-D", "2606:d::4"),
               "slot-5": ("WARP-E", "2606:e::5"),
               "slot-6": ("WARP-F", "2606:f::6")}
    monkeypatch.setattr(daemon, "_slot_current",
                        lambda slot: current.get(slot["name"], (None, None)))

    puts = []
    real_put = daemon._slot_put

    def fake_put(slot, tag):
        puts.append((slot["name"], tag))
        current[slot["name"]] = (tag, "2606:z:%s" % tag[-1])
        return True
    monkeypatch.setattr(daemon, "_slot_put", fake_put)

    state = {"main_tag": "WARP-A", "main_ip": "2606:a::1"}
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: state["main_tag"])
    monkeypatch.setattr(daemon, "_warp_line_ip",
                        lambda tag: "2606:a::1" if tag == "WARP-A" else None)

    def fake_switch(tag, *, ip=None):
        state["main_tag"] = tag
        state["main_ip"] = ip
        return True
    monkeypatch.setattr(daemon, "_switch_warp", fake_switch)

    for n in ("_mark_warp_ip_used", "_warp_line_selected", "_set_warp_line_ip",
              "_begin_segment", "_publish_clash_snapshot"):
        monkeypatch.setattr(daemon, n, lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_warp_member", lambda t: bool(t))

    def fresh_candidates(exclude, now):
        # 与生产候选生成器同契约：exclude 是当前线名，不能无视参数恒返回同一项。
        # 这里返回一条确定的新线；调用方额外传的 avoid 仍由 _slot_pick 过滤。
        if exclude == "WARP-NEW":
            return []
        return [({"tag": "WARP-NEW"}, "2606:n::9")]
    monkeypatch.setattr(daemon, "_warp_line_fresh_candidates", fresh_candidates)
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: False)

    def probe(tag, ip, fresh=False):
        return {"verdict": "OK", "sec": 2.0, "detail": "", "retry_after": None,
                "elapsed": 2.0, "ts": 0.0}
    monkeypatch.setattr(daemon, "_probe_line_before_rotate", probe)
    # `_probe_line_for_slots` 现在默认读渠道级缓存结论（`_channel_verdict_snapshot`），
    # 只有 slot_switch_probe=True 才走上面的实测桩。测试沙箱里没有真实状态台账，
    # 缓存路径会返回 unknown → 槽位被标 bad。让快照转发到当前实测桩，
    # 这样「按探测结果标记」的断言在两种开关下都成立，且后面用例改写
    # `_probe_line_before_rotate` 时也能连带生效。
    def cached_snapshot(tag, ip):
        return daemon._probe_line_before_rotate(tag, ip)
    monkeypatch.setattr(daemon, "_channel_verdict_snapshot", cached_snapshot)

    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)

    daemon._SLOT_HEALTH.clear()
    daemon._SLOT_EXIT_LEDGER.clear()
    # 出口短冷却也是跨用例的模块级状态：_rotate_slot_once 会给事发地址打
    # 90s 冷却（_mark_egress_cooling），不清的话上一条用例冷却过的地址会把
    # 下一条用例的选槽结果整个改写（实测：repairing 用例冷却了 b::2，
    # 后面的 rotate 用例就只能选 c::3；log 用例冷却了 a::1，
    # serving 兜底用例直接挑不到任何槽）。
    daemon._EGRESS_COOLING.clear()
    daemon._SLOT_REDIRECT_RR["index"] = 0
    for s in SLOTS:
        daemon._slot_mark(s["name"], "ok")
    daemon._MAIN_SLOT_NAME["name"] = "slot-1"
    daemon._api_stats.update({"calls": 0, "switched": 0, "coalesced": 0,
                              "line_ok": 0, "divergence": 0, "failed": 0})
    daemon._PROBE_DIVERGENCE.clear()

    return types.SimpleNamespace(current=current, puts=puts, state=state,
                                 real_put=real_put)


def test_fast_switch_picks_healthy_and_updates_main_slot(isolated):
    """_fast_switch 只管同步翻转：挑健康槽位、切主出口、更新 _MAIN_SLOT。不管 owner。"""
    r = daemon._fast_switch("rate429")
    assert r is not None
    assert r["tag"] != "WARP-A", "不能切回同一条坏线"
    assert daemon._slot_serving_name() != "slot-1", "_MAIN_SLOT 必须改指新槽位"
    assert isolated.state["main_tag"] == r["tag"]
    # owner（slot-1）的状态不该被 _fast_switch 碰（那是 _kick_background_repair 的活）
    assert daemon._slot_health("slot-1")["status"] == "ok"


def test_fast_switch_no_healthy_returns_none(isolated):
    for n in ("slot-2", "slot-3", "slot-4", "slot-5", "slot-6"):
        daemon._slot_mark(n, "bad")
    r = daemon._fast_switch("rate429")
    assert r is None


def test_kick_marks_owner_repairing(isolated):
    """_kick_background_repair 找到 owner，标 repairing 并起后台线程。"""
    daemon._kick_background_repair("WARP-A", "2606:a::1", "rate429")
    assert daemon._slot_health("slot-1")["status"] == "repairing"


def test_kick_no_owner_uses_repair_line(isolated, monkeypatch):
    """_MAIN_SLOT 为 None 时（主出口的线不是槽位借来的），走游离线复核。"""
    daemon._MAIN_SLOT_NAME["name"] = None
    spawned = {}
    class _SpyThread(_NoopThread):
        def __init__(self, target=None, args=(), kwargs=None, **kw):
            super().__init__(target, args, kwargs, **kw)
            spawned[getattr(target, "__name__", "?")] = args
    monkeypatch.setattr(daemon.threading, "Thread", _SpyThread)
    daemon._kick_background_repair("WARP-A", "2606:a::1", "rate429")
    assert "_repair_line" in spawned, "无 owner 时应走 _repair_line"


def test_kick_skips_already_repairing(isolated):
    """已经在修整中的槽位不重复启动。"""
    daemon._slot_mark("slot-1", "repairing")
    daemon._kick_background_repair("WARP-A", "2606:a::1", "rate429")
    assert daemon._slot_health("slot-1")["status"] == "repairing"


def test_repair_slot_always_switches_and_probes_new(isolated):
    """错误切换：先换线（_slot_put 被调），再探新线，按新线结果标记。不探旧线、不留旧线。"""
    daemon._slot_mark("slot-2", "repairing")
    daemon._repair_slot({"name": "slot-2", "selector": "opencode-out-2"}, "rate429")
    assert ("slot-2", "WARP-NEW") in isolated.puts, "必须换线（错误切换）"
    assert isolated.current["slot-2"][0] == "WARP-NEW", "旧线已被换掉"
    h = daemon._slot_health("slot-2")
    assert h["status"] == "ok" and h["verdict"] == "OK"


def test_repair_slot_new_line_bad_marks_bad(isolated):
    """换上去的新线探不通 → 标 bad。"""
    daemon._slot_mark("slot-2", "repairing")
    daemon._probe_line_before_rotate = lambda tag, ip, fresh=False: \
        {"verdict": "quota", "sec": 3.0, "detail": "3h", "retry_after": None,
         "elapsed": 3.0, "ts": 0.0}
    daemon._repair_slot({"name": "slot-2", "selector": "opencode-out-2"}, "rate429")
    assert ("slot-2", "WARP-NEW") in isolated.puts, "仍要换线"
    h = daemon._slot_health("slot-2")
    assert h["status"] == "bad" and h["verdict"] == "all_tried"


def test_repair_slot_no_candidate_marks_bad(isolated):
    """没有新线可换 → 标 bad，不崩。"""
    daemon._slot_mark("slot-2", "repairing")
    daemon._warp_line_fresh_candidates = lambda exclude, now: []
    daemon._repair_slot({"name": "slot-2", "selector": "opencode-out-2"}, "rate429")
    assert daemon._slot_health("slot-2")["status"] == "bad"
    assert not any(n == "slot-2" for n, _ in isolated.puts), "没候选就不该 PUT"


def test_assign_skips_serving_and_repairing(isolated):
    daemon._slot_mark("slot-2", "repairing")
    daemon._slot_mark("slot-3", "bad")
    isolated.current["slot-3"] = (None, None)
    before_1 = daemon._slot_current({"name": "slot-1"})
    before_2 = daemon._slot_current({"name": "slot-2"})
    daemon.assign_consumer_slots()
    assert daemon._slot_current({"name": "slot-1"}) == before_1, "serving 不应被动"
    assert daemon._slot_current({"name": "slot-2"}) == before_2, "repairing 不应被动"
    assert any(name == "slot-3" for name, _ in isolated.puts), "slot-3 应被补线"


def test_repair_never_reuses_failed_line_even_without_ip(isolated):
    """旧线 IP 未知时也必须换走；候选生成器反复给同一条也不能空转。"""
    daemon._slot_mark("slot-2", "repairing")
    isolated.current["slot-2"] = ("WARP-B", None)
    daemon._warp_line_fresh_candidates = lambda exclude, now: [({"tag": "WARP-B"}, None)]
    daemon._repair_slot({"name": "slot-2", "selector": "opencode-out-2"}, "rate429")
    assert not any(name == "slot-2" for name, _ in isolated.puts), "不能 PUT 回刚失败的线"
    assert daemon._slot_health("slot-2")["status"] == "bad"


def test_healthy_redirect_is_round_robin_and_avoids_serving(isolated):
    """失败重试不能永远压到数组第一个（slot-1/主出口）。"""
    picked = []
    for failed in ("slot-2", "slot-3", "slot-4", "slot-5"):
        s, tag, _ip = daemon._pick_healthy_slot(failed)
        assert s is not None and tag
        picked.append(s["name"])
    assert daemon._slot_serving_name() not in picked, picked
    assert len(set(picked)) > 1, "应在健康槽之间轮询，实际 %s" % picked


def test_slot_late_429_does_not_repair_new_line(isolated):
    """请求发出后槽位已换线：迟到 429 只回 already_retired，不碰新线。"""
    daemon._slot_mark("slot-2", "ok")
    daemon._slot_health("slot-2")["switched_at"] = 200.0
    r = daemon._rotate_slot_once({"name": "slot-2", "selector": "opencode-out-2",
                                  "port": 7892},
                                 "rate429", request_started_at=100.0,
                                 egress_ip="2606:b::old")
    assert r["reason"] == "already_retired", r
    assert daemon._slot_health("slot-2")["status"] == "ok", "不得把修好的槽重新标 repairing"
    assert not any(name == "slot-2" for name, _ in isolated.puts)


def test_slot_repairing_coalesces_repeated_reports(isolated):
    daemon._slot_mark("slot-2", "repairing")
    daemon._slot_health("slot-2")["last_rotate_ts"] = 1.0
    r = daemon._rotate_slot_once({"name": "slot-2", "selector": "opencode-out-2",
                                  "port": 7892},
                                 "rate429")
    assert r["reason"] == "coalesced", r
    assert r["avoid_port_until"] > 0
    assert daemon._slot_health("slot-2")["status"] == "repairing"


def test_main_slot_put_updates_global_accounting(isolated, monkeypatch):
    """slot-1 的 selector 变更必须走主出口记账，而不只是槽位自己的状态。"""
    rec = []
    monkeypatch.setattr(daemon, "slot_singbox_api",
                        lambda *a, **k: (200, {}))
    monkeypatch.setattr(daemon, "_warp_line_ip", lambda tag: "2606:new::1")
    monkeypatch.setattr(daemon, "_slot_note_switch", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_record_main_slot_switch",
                        lambda *args: rec.append(args))
    slot = {"name": "slot-1", "selector": "opencode-out-1", "port": 7890}
    assert isolated.real_put(slot, "WARP-NEW")
    assert rec, "slot-1 换线必须进入主出口记账"
    args = rec[0]
    assert args[1:] == ("WARP-A", "2606:a::1", "WARP-NEW", "2606:new::1",
                        args[5]), args
    assert isinstance(args[5], float) and args[5] > 0, args


def test_begin_repair_claims_only_once(isolated):
    """并发收敛只能有一个认领者，否则后到的那条会把刚修好的线再换掉。"""
    assert daemon._slot_begin_repair("slot-2", "duplicate") is True
    assert daemon._slot_begin_repair("slot-2", "duplicate") is False
    assert daemon._slot_health("slot-2")["status"] == "repairing"
    # 修完之后（被判坏）可以重新认领：认领不是一次性闩锁。
    daemon._slot_mark("slot-2", "bad", "no_candidate")
    assert daemon._slot_begin_repair("slot-2", "duplicate") is True


def test_slot_health_returns_live_record(isolated):
    """必须返回表里那份，调用方写入的限速事实不能落在临时 dict 上。"""
    h = daemon._slot_health("slot-never-seen")
    h["last_rotate_ts"] = 123.0
    assert daemon._slot_health("slot-never-seen")["last_rotate_ts"] == 123.0


def test_no_healthy_slot_asks_9router_to_restart(monkeypatch):
    """六条闸都修着时主链路不兜底：明确让 9router 自重启等闸回来。"""
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: (None, None, None))
    monkeypatch.setattr(daemon, "_slot_no_usable_gate", lambda: True)
    slot = {"name": "slot-1", "selector": "opencode-out-1", "port": 7890}
    out = daemon._slot_redirect_payload(slot, "WARP-A", "2606:a::1",
                                        "rate429", 0.0)
    assert out["switch_to_url"] is None
    assert out["line_unknown"] is True
    assert out["reason"] == "no_healthy_slot"
    assert out["restart_required"] is True


def test_already_retired_without_standby_does_not_restart(monkeypatch):
    """线已在请求发出后换走：原地重连即可，不该把刚接上的连接重启掉。"""
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: (None, None, None))
    slot = {"name": "slot-1", "selector": "opencode-out-1", "port": 7890}
    out = daemon._slot_redirect_payload(slot, "WARP-A", "2606:a::1",
                                        "already_retired", 123.0)
    assert out["switch_to_url"] is None
    assert not out.get("restart_required")
    assert not out.get("line_unknown")
    assert out["reason"] == "already_retired"


def test_rotate_slot_log_links_request_to_slot_egress(isolated, monkeypatch):
    """排查通道：日志必须把 req_id 与这台闸此刻的线名、出口 IP 一起留下。"""
    lines = []
    monkeypatch.setattr(daemon, "log", lambda msg: lines.append(msg))
    daemon._rotate_slot_once(SLOTS[0], "rate429", req_id="REQ-TRACE-1")
    hit = [s for s in lines if "REQ-TRACE-1" in s]
    assert hit, lines
    assert "线=WARP-A" in hit[0] and "出口=2606:a::1" in hit[0], hit


def test_slot_repair_thread_starts_only_after_payload_is_built(isolated, monkeypatch):
    """秒切（挑健康槽）期间后台线程不许动状态表。"""
    import types as _types
    order = []
    real_payload = daemon._slot_redirect_payload

    class FakeThread:
        def __init__(self, target=None, args=(), **kw):
            self.target = target

        def start(self):
            order.append("start")

    # 只替换 daemon 模块里的引用，不动全局 threading（pytest 自己要用）。
    monkeypatch.setattr(daemon, "threading",
                        _types.SimpleNamespace(Thread=FakeThread))

    def spy_payload(*a, **kw):
        order.append("payload")
        return real_payload(*a, **kw)

    monkeypatch.setattr(daemon, "_slot_redirect_payload", spy_payload)
    daemon._rotate_slot_once(SLOTS[0], "rate429")
    assert order == ["payload", "start"], order


def test_startup_unknown_slots_do_not_trigger_restart(monkeypatch):
    """首轮探测没收敛前，任何 unknown 都不算「闸全废」。"""
    # 复现 19:52:19 启动、19:52:25 / 19:52:39 连发两次自重启信号的窗口。
    monkeypatch.setattr(daemon, "_SLOT_BOOT_TS", daemon.time.time())
    monkeypatch.setattr(daemon, "consumer_slots",
                        lambda: [{"name": "slot-1"}, {"name": "slot-2"}])
    monkeypatch.setattr(daemon, "_slot_health",
                        lambda name: {"status": "unknown"})
    assert daemon._slot_no_usable_gate() is False
    # 1 bad + 1 unknown：首轮还没探完，旧判据在这里误判成全废（线上 2 次
    # 白重启就是这个组合）。宽限期内必须压住。
    monkeypatch.setattr(daemon, "_slot_health",
                        lambda name: {"status": "bad"} if name == "slot-1"
                        else {"status": "unknown"})
    assert daemon._slot_no_usable_gate() is False
    # 硬上限：超过宽限期后，同样的混态仍要发信号，不能无限抑制。
    monkeypatch.setattr(daemon, "_SLOT_BOOT_TS", 0.0)
    assert daemon._slot_no_usable_gate() is True
    # 六条都出过首轮结论、无一可用：即便在宽限期内也是真全废。
    monkeypatch.setattr(daemon, "_SLOT_BOOT_TS", daemon.time.time())
    monkeypatch.setattr(daemon, "_slot_health",
                        lambda name: {"status": "repairing"})
    assert daemon._slot_no_usable_gate() is True
    monkeypatch.setattr(daemon, "_slot_health",
                        lambda name: {"status": "repairing"})
    assert daemon._slot_no_usable_gate() is True
    # 还有一条 ok 但 _pick_healthy_slot 没挑中（tag 被 avoid 排除 / 就是被报
    # 槽自己）：这仍然是「无可用闸口」，旧判据在这里漏发重启信号，线上 18:41
    # / 19:02 / 19:03 三次都因此既没切线也没重建连接池。
    monkeypatch.setattr(daemon, "_slot_health",
                        lambda name: {"status": "ok"} if name == "slot-2"
                        else {"status": "bad"})
    assert daemon._slot_no_usable_gate() is True


def test_mixed_gate_states_still_signal_no_healthy_slot(monkeypatch):
    """稳态混态（在修 + 未探 + 仅剩一条 ok 但挑不中）必须给出重启信号。"""
    # 明确站在宽限期之外：启动窗口的抑制不能把 18:41 / 19:02 / 19:03
    # 那类稳态漏发又带回来。
    monkeypatch.setattr(daemon, "_SLOT_BOOT_TS", 0.0)
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: (None, None, None))
    monkeypatch.setattr(daemon, "consumer_slots",
                        lambda: [{"name": "slot-%d" % i} for i in range(1, 7)])
    monkeypatch.setattr(daemon, "_slot_health",
                        lambda name: {"status": "ok"} if name == "slot-2"
                        else ({"status": "unknown"} if name == "slot-6"
                              else {"status": "repairing"}))
    slot = {"name": "slot-1", "selector": "opencode-out-1", "port": 7890}
    out = daemon._slot_redirect_payload(slot, "WARP-A", "2606:a::1",
                                        "rate429", 0.0)
    assert out["reason"] == "no_healthy_slot"
    assert out["restart_required"] is True
    assert out["switch_to_url"] is None


def test_startup_window_payload_does_not_ask_for_restart(monkeypatch):
    """启动窗口内同样「无健康备槽」，但不该让 9router 重启。"""
    monkeypatch.setattr(daemon, "_SLOT_BOOT_TS", daemon.time.time())
    monkeypatch.setattr(daemon, "_pick_healthy_slot",
                        lambda name, avoid_tags=None, avoid_ips=None: (None, None, None))
    monkeypatch.setattr(daemon, "consumer_slots",
                        lambda: [{"name": "slot-%d" % i} for i in range(1, 7)])
    monkeypatch.setattr(daemon, "_slot_health",
                        lambda name: {"status": "repairing"} if name == "slot-1"
                        else {"status": "unknown"})
    slot = {"name": "slot-1", "selector": "opencode-out-1", "port": 7890}
    out = daemon._slot_redirect_payload(slot, "WARP-A", "2606:a::1",
                                        "rate429", 0.0)
    assert out["reason"] == "no_healthy_slot"
    assert out["line_unknown"] is True
    assert out["switch_to_url"] is None
    assert "restart_required" not in out


def test_probe_verdict_for_ip_is_address_scoped(isolated, monkeypatch):
    """按出口 IP 反查实测结论：只认地址级，隧道级故障不许算在这个地址头上。"""
    now = daemon.time.time()
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {
        ("WARP-A", "2606:dead::1"): ("quota", now),
        ("WARP-B", "2606:ok::2"): ("OK", now),
        ("WARP-C", "2606:err::3"): ("unreachable", now),
        ("WARP-D", "2606:old::4"): ("quota", now - 10 ** 6),
    })
    assert daemon._probe_verdict_for_ip("2606:dead::1") == "quota"
    assert daemon._probe_verdict_for_ip("2606:ok::2") == "OK"
    assert daemon._probe_verdict_for_ip("2606:err::3") is None
    assert daemon._probe_verdict_for_ip("2606:old::4") is None
    assert daemon._probe_verdict_for_ip("2606:none::9") is None
    assert daemon._probe_verdict_for_ip(None) is None


def test_rotate_trace_fields_uses_slot_egress_not_reported_ip(isolated, monkeypatch):
    """回包排查三元组：线/IP 必须是这个槽自己的一对，不能拿全局出口顶替。"""
    now = daemon.time.time()
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {
        ("WARP-OLD", "2606:dead::1"): ("quota", now),
        ("WARP-NEW", "2606:new::2"): ("OK", now),
    })
    payload = {"ok": True, "active_tag": "WARP-NEW",
               "active_egress": "2606:new::2"}
    trace = daemon._rotate_trace_fields({"egress_ip": "2606:dead::1"}, payload)
    assert trace["offending_egress"] == "2606:new::2", trace
    assert trace["offending_tag"] == "WARP-NEW", trace
    assert trace["offending_verdict"] == "OK", trace
    assert trace["reported_egress"] == "2606:dead::1", trace

    # 主路径回包没有 active_egress，才退回调用方报的地址。
    main = daemon._rotate_trace_fields(
        {"egress_ip": "2606:dead::1"},
        {"ok": True, "reason": "no_healthy_slot", "line_unknown": True})
    assert main["offending_egress"] == "2606:dead::1", main
    assert main["offending_verdict"] == "quota", main
    assert main["offending_tag"] is None, main

    # 两边都没给地址时不能凭空编一个。
    empty = daemon._rotate_trace_fields({}, {"ok": True})
    assert empty["offending_egress"] is None
    assert empty["offending_verdict"] is None


def test_rotate_slot_keeps_verdict_from_before_block_marking(isolated, monkeypatch):
    """标脏会清实测台账；回包/日志必须留住事发前的结论，而不是事后重算。

    isolated 把 _mark_warp_ip_used 换成了 no-op，这里只替身出它的
    forget_probe 效果，精确复现生产时序，不碰落盘。
    """
    import time
    lines = []
    monkeypatch.setattr(daemon, "log", lambda msg: lines.append(msg))
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    monkeypatch.setitem(daemon._WARP_PROBE_SEEN,
                        ("WARP-A", "2606:a::1"), ("OK", time.time()))

    def fake_mark(ip, now, blocked=False, kind=None, **kw):
        if blocked:
            daemon._probe_cache_forget(ip)
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", fake_mark)

    out = daemon._rotate_slot_once(SLOTS[0], "rate429", req_id="REQ-VERDICT")
    assert out["offending_verdict"] == "OK", out
    # 回包补字段也不许把快照覆盖成事后读取的 None
    fields = daemon._rotate_trace_fields({"egress_ip": "2606:a::1"}, out)
    assert fields["offending_verdict"] == "OK", fields
    hit = [s for s in lines if "REQ-VERDICT" in s]
    assert hit and "实测=OK" in hit[0], hit
    assert "标脏后=未探" in hit[0], "台账确实被清，观测字段要如实记录"
    assert "目标出口=2606:b::2" in hit[0], hit[0]


def test_probe_verdict_falls_back_to_pre_rotate_table(monkeypatch):
    """两张台账要通：换线前实测写的结果也该被诊断反查读到。"""
    import time
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    monkeypatch.setattr(daemon, "_PROBE_BEFORE_ROTATE", {})
    daemon._PROBE_BEFORE_ROTATE["WARP-A|2606:a::1"] = {
        "verdict": "OK", "ts": time.time()}
    assert daemon._probe_verdict_for_ip("2606:a::1") == "OK"


# --- 全链路修复：生产 429 长封禁 + 秒切按地址过滤 -------------------------

def test_quarantine_recognizes_production_block(isolated, monkeypatch):
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked",
                        lambda ip, now=None: ip == "2606:b::2")
    assert daemon._slot_ip_is_quarantined("2606:b::2", 1000.0) is True
    assert daemon._slot_ip_is_quarantined("2606:c::3", 1000.0) is False


def test_quarantine_recognizes_probe_quota(isolated, monkeypatch):
    monkeypatch.setattr(daemon, "_probe_verdict_for_ip",
                        lambda ip, now=None: "quota" if ip == "2606:d::4" else None)
    assert daemon._slot_ip_is_quarantined("2606:d::4", 1000.0) is True
    assert daemon._slot_ip_is_quarantined("2606:e::5", 1000.0) is False


def test_pick_healthy_slot_skips_quarantined_egress(isolated, monkeypatch):
    blocked = {"2606:b::2", "2606:c::3"}
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked",
                        lambda ip, now=None: ip in blocked)
    monkeypatch.setattr(daemon, "_slot_serving_name", lambda: "slot-1")
    # slot-1 自身排除，b/c 被隔离 → 轮询结果只能落在 d/e/f。
    for _ in range(8):
        s, tag, ip = daemon._pick_healthy_slot("slot-1")
        assert s["name"] in {"slot-4", "slot-5", "slot-6"}
        assert ip not in blocked


def test_all_candidates_quarantined_returns_no_healthy_slot(isolated, monkeypatch):
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: True)
    payload = daemon._slot_redirect_payload(
        SLOTS[0], "WARP-A", "2606:a::1", "rate429", 0.0)
    assert payload["switch_to"] is None
    assert payload["reason"] == "no_healthy_slot"
    assert payload["line_unknown"] is True


def test_ban_after_prefers_upstream_retry_after_ms():
    assert daemon._rotate_ban_after_from_req({"retry_after_ms": 45000}) == 45.0
    assert daemon._rotate_ban_after_from_req({"retry_after_ms": "3600000"}) == 3600.0


def test_ban_after_falls_back_to_config_instead_of_60s():
    secs = daemon._rotate_ban_after_from_req({})
    cfg = float(daemon._api_cfg().get("production_429_ban_sec", 43200))
    assert secs == cfg
    assert secs >= 3600.0, "兜底不能退回到 rate429_guard_sec=60s"


def test_slot_rotate_writes_ban_after_into_ledger(isolated, monkeypatch):
    marks = []
    monkeypatch.setattr(daemon, "_mark_warp_ip_used",
                        lambda *a, **k: marks.append((a, k)))
    monkeypatch.setattr(daemon, "_probe_verdict_for_ip", lambda ip, now=None: None)
    # 只验证「标脏时把 ban_after 传进台账」这一环；回包构造由既有用例覆盖。
    try:
        daemon._rotate_slot_once(SLOTS[0], "rate429", None, None, "req-x", 43200.0)
    except Exception:
        pass
    assert marks, "生产 429 必须标脏"
    assert marks[0][1].get("ban_after") == 43200.0


# ── 选槽优先级：新鲜度第一，延迟第二 ────────────────────────────────

def _set_probe(name, sec, age=0.0):
    h = daemon._slot_health(name)
    h["probe_sec"] = sec
    h["probe_ts"] = time.time() - age


def test_slot_note_probe_records_and_expires(isolated):
    daemon._slot_note_probe("slot-2", {"sec": 1.25})
    assert daemon._slot_probe_sec("slot-2") == 1.25
    # 过期（超过 slot_probe_ttl_sec）等同无数据
    daemon._slot_health("slot-2")["probe_ts"] = time.time() - 10 ** 6
    assert daemon._slot_probe_sec("slot-2") is None
    # 没有数值的探测结论不写
    daemon._slot_note_probe("slot-3", {"verdict": "OK"})
    assert "probe_sec" not in daemon._slot_health("slot-3")


def test_pick_prefers_fresh_rank_over_low_latency(isolated, monkeypatch):
    """新鲜度是主判据：刚验过 OK 的慢线，压过撑过生产流量的快线。"""
    ranks = {"2606:b::2": (2, 0, 0.0), "2606:c::3": (1, 0, 5.0)}
    monkeypatch.setattr(
        daemon, "_warp_ip_rank",
        lambda ip, now, window: ranks.get(ip, (3, 0, 0.0)))
    _set_probe("slot-2", 0.1)   # 快，但档位低（只撑过生产流量）
    _set_probe("slot-3", 3.0)   # 慢，但档位高（探针刚验过 OK）
    for n in ("slot-4", "slot-5", "slot-6"):
        daemon._slot_mark(n, "bad")

    s, _tag, _ip = daemon._pick_healthy_slot("slot-1")
    assert s["name"] == "slot-3", "新鲜度必须压过延迟"


def test_pick_prefers_lower_latency_within_same_rank(isolated, monkeypatch):
    """同档内才比延迟：0.4s 压过 3.2s。"""
    monkeypatch.setattr(daemon, "_warp_ip_rank",
                        lambda ip, now, window: (0, 0, 0.0))
    _set_probe("slot-4", 3.2)
    _set_probe("slot-5", 0.4)
    for n in ("slot-2", "slot-3", "slot-6"):
        daemon._slot_mark(n, "bad")

    s, _tag, _ip = daemon._pick_healthy_slot("slot-1")
    assert s["name"] == "slot-5"


def test_pick_prefers_known_latency_over_unknown(isolated, monkeypatch):
    """没延迟数据的不算最优：慢但已知，压过未知。"""
    monkeypatch.setattr(daemon, "_warp_ip_rank",
                        lambda ip, now, window: (0, 0, 0.0))
    _set_probe("slot-6", 4.0)
    for n in ("slot-2", "slot-3"):
        daemon._slot_mark(n, "bad")

    s, _tag, _ip = daemon._pick_healthy_slot("slot-1")
    assert s["name"] == "slot-6", "已知慢 好过 未知"


def test_pick_treats_stale_probe_as_unknown(isolated, monkeypatch):
    """过期的快读数不能当成「快」——它等同于没有数据。"""
    monkeypatch.setattr(daemon, "_warp_ip_rank",
                        lambda ip, now, window: (0, 0, 0.0))
    _set_probe("slot-4", 0.2, age=10 ** 6)   # 快，但读数过期
    _set_probe("slot-5", 5.0)                # 慢，但有效
    for n in ("slot-2", "slot-3", "slot-6"):
        daemon._slot_mark(n, "bad")

    s, _tag, _ip = daemon._pick_healthy_slot("slot-1")
    assert s["name"] == "slot-5"


def test_pick_keeps_serving_slot_as_last_resort(isolated, monkeypatch):
    """serving 槽永远排最后，但只剩它健康时仍返回它（端口本身就是备份）。"""
    monkeypatch.setattr(daemon, "_warp_ip_rank",
                        lambda ip, now, window: (0, 0, 0.0))
    for n in ("slot-2", "slot-3", "slot-4", "slot-5", "slot-6"):
        daemon._slot_mark(n, "bad")
    _set_probe("slot-1", 0.05)  # 就算它最快也不该被当成备槽

    s, _tag, _ip = daemon._pick_healthy_slot(None)
    assert s["name"] == "slot-1"


def test_pick_same_key_still_rotates(isolated, monkeypatch):
    """键完全相同的候选仍按游标轮换，不会永远压同一个槽位。"""
    monkeypatch.setattr(daemon, "_warp_ip_rank",
                        lambda ip, now, window: (0, 0, 0.0))
    for n in ("slot-2", "slot-3"):
        daemon._slot_mark(n, "bad")
    for n in ("slot-4", "slot-5", "slot-6"):
        _set_probe(n, 1.0)   # 同档同延迟 = 同键

    picked = set()
    for _ in range(6):
        s, _tag, _ip = daemon._pick_healthy_slot("slot-1")
        picked.add(s["name"])
    assert len(picked) >= 2, "同分候选应轮换：%s" % picked


# ===== 借道（borrowed）语义 =====
#
# 四个必须一起防的失效模式（来自 2026-11-13 生产取证）：
#   错误归因   —— 借道请求的失败记在被借线头上，而不是请求来源
#   状态污染   —— 一次借道失败就 _mark_warp_ip_used + _probe_cache_forget
#                 + _warp_line_invalidate，把健康线打进脏名单
#   标准不一致 —— 同一错误码，探针侧要换 session 复测，生产侧零复测
#   不可观测   —— 重试请求是克隆，B 侧无法识别；日志里 A 消失
# 下面两条用例分别锁住「不归因到 B」和「原地 429 仍然归因」。


def test_borrowed_429_does_not_dirty_lended_line(isolated, monkeypatch):
    """借道重试的 429：不标脏、不冷却被借线、不换它的线，但仍给出下一个备槽。"""
    touched = []
    monkeypatch.setattr(daemon, "_mark_warp_ip_used",
                        lambda ip, now, **kw: touched.append(("dirty", ip, kw)))
    monkeypatch.setattr(daemon, "_mark_egress_cooling",
                        lambda ip, ts=None: touched.append(("cool", ip, ts)))

    r = daemon._rotate_slot_once(
        {"name": "slot-2", "selector": "opencode-out-2", "port": 7892},
        "rate429", egress_ip="2606:b::2", borrowed=True)

    assert r["reason"] == "borrowed_skip", r
    assert touched == [], "借道失败不得给被借线标脏/冷却：%s" % (touched,)
    assert not any(name == "slot-2" for name, _ in isolated.puts), \
        "不得换掉被借线的线（它没坏）：%s" % (isolated.puts,)
    assert daemon._slot_health("slot-2")["status"] == "ok", \
        "被借线状态必须保持 ok"
    assert r["switch_to_url"], "仍要给出下一个健康备槽，让请求继续递下去"


def test_direct_429_still_dirties_own_line(isolated, monkeypatch):
    """原地 429（非借道）仍然按原逻辑标脏并换线 —— 回归保护。"""
    touched = []
    monkeypatch.setattr(daemon, "_mark_warp_ip_used",
                        lambda ip, now, **kw: touched.append(ip))

    r = daemon._rotate_slot_once(
        {"name": "slot-2", "selector": "opencode-out-2", "port": 7892},
        "rate429", egress_ip="2606:b::2")

    assert touched == ["2606:b::2"], "原地 429 必须标脏本线：%s" % (touched,)
    assert r["reason"] in ("ok", "coalesced", "too_frequent"), r


# ===== rate429 定性开关（config.rate429_recheck_mode）=====
#
# 三档语义，下面四条用例分别锁住：
#   borrowed —— 只借道复测（默认，当前生产）
#   all      —— 所有 429 都先复测再判死（用来实测「原地 429 有多少是假 429」）
#   off      —— 全关，回到 P0-1 之前（kill switch）
# 关键约束：all 只延后「判死」，**不延后换线** —— 请求已经 429 了必须换，
# 否则它只能原地重试死端口。


def test_rate429_recheck_mode_defaults_to_borrowed(monkeypatch):
    monkeypatch.delitem(daemon.CFG["warp"], "rate429_recheck_mode", raising=False)
    assert daemon._rate429_recheck_mode() == "borrowed"


def test_rate429_recheck_mode_rejects_junk(monkeypatch):
    """写错一个字不能让借道保护静默消失 —— 它是目前唯一验证过有效的改动。"""
    monkeypatch.setitem(daemon.CFG["warp"], "rate429_recheck_mode", "al")
    assert daemon._rate429_recheck_mode() == "borrowed"

    monkeypatch.setitem(daemon.CFG["warp"], "rate429_recheck_mode", " ALL ")
    assert daemon._rate429_recheck_mode() == "all", "大小写/空格不该让配置失效"


def test_all_mode_defers_own_429_verdict(isolated, monkeypatch):
    """all 模式：原地 429 不写 24h 标脏、改走异步复测，但换线照旧。"""
    touched = []
    rechecked = []
    monkeypatch.setitem(daemon.CFG["warp"], "rate429_recheck_mode", "all")
    monkeypatch.setattr(daemon, "_mark_warp_ip_used",
                        lambda ip, now, **kw: touched.append(ip))
    monkeypatch.setattr(daemon, "_cool_egress_after_recheck",
                        lambda *a, **k: rechecked.append(a))

    r = daemon._rotate_slot_once(
        {"name": "slot-2", "selector": "opencode-out-2", "port": 7892},
        "rate429", egress_ip="2606:b::2")

    assert touched == [], "all 模式不得直接标脏这条线：%s" % (touched,)
    assert rechecked == [("2606:b::2", "WARP-B", "rate429")], \
        "all 模式必须转异步复测且带对 reason：%s" % (rechecked,)
    assert r["reason"] in ("ok", "coalesced", "too_frequent"), \
        "延后的只是判死，换线必须照常：%s" % (r,)
    assert daemon._api_stats.get("deferred_429", 0) >= 1


def test_borrowed_mode_still_dirties_own_429(isolated, monkeypatch):
    """默认（borrowed）模式下，原地 429 行为不变 —— 回归保护。"""
    touched = []
    monkeypatch.setitem(daemon.CFG["warp"], "rate429_recheck_mode", "borrowed")
    monkeypatch.setattr(daemon, "_mark_warp_ip_used",
                        lambda ip, now, **kw: touched.append(ip))

    daemon._rotate_slot_once(
        {"name": "slot-2", "selector": "opencode-out-2", "port": 7892},
        "rate429", egress_ip="2606:b::2")

    assert touched == ["2606:b::2"]


def test_off_mode_is_a_full_rollback(isolated, monkeypatch):
    """off 是 kill switch：连借道失败也重新标脏被借线（P0-1 之前的行为）。"""
    touched = []
    monkeypatch.setitem(daemon.CFG["warp"], "rate429_recheck_mode", "off")
    monkeypatch.setattr(daemon, "_mark_warp_ip_used",
                        lambda ip, now, **kw: touched.append(ip))
    monkeypatch.setattr(daemon, "_mark_egress_cooling", lambda ip, ts=None: None)

    daemon._rotate_slot_once(
        {"name": "slot-2", "selector": "opencode-out-2", "port": 7892},
        "rate429", egress_ip="2606:b::2", borrowed=True)

    assert touched == ["2606:b::2"], \
        "off 模式下借道也必须标脏 —— 否则这个开关回退不掉 P0-1"


def test_api_stats_has_observation_keys():
    """P0-4 是纯观测：这些键必须存在，否则 /debug/state 上读不到数据。"""
    for k in ("stale_ok_pick", "deferred_429"):
        assert k in daemon._api_stats, "缺少观测键 %s" % k
