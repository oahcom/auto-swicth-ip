#!/usr/bin/env python3
"""transport 类报障的「先复测、再冷却」回归测试（离线、<5s）。

背景：transport 同时覆盖「线路真断」和「大 body 首字节慢」。旧逻辑一律打
90s 出口冷却，几条线轮流被冷却后 _pick_healthy_slot 抽干 →
no_healthy_slot → 503 放大。现在的判据是复测：探针 OK 就不冷却。

覆盖：
  1 超时报障 + 复测 OK   => 无冷却
  2 超时报障 + 复测失败 => 有冷却
  3 非超时失败（rate429）=> 不起复测、原行为（立即冷却）不变
  4 同一出口重复报障      => 只起一次复测
"""
import types

import pytest

import daemon

SLOTS = [{"name": "slot-%d" % i, "selector": "opencode-out-%d" % i}
         for i in range(1, 3)]


class _InlineThread:
    """同步执行 target：让复测线程在断言之前就跑完。"""
    spawned = 0

    def __init__(self, target=None, args=(), kwargs=None, **kw):
        self.target = target
        self.args = args

    def start(self):
        _InlineThread.spawned += 1
        self.target(*self.args)


class _CountingThread:
    """只计数、不执行：用于「重复报障不形成风暴」的断言。"""
    spawned = 0

    def __init__(self, target=None, args=(), kwargs=None, **kw):
        self.target = target
        self.args = args

    def start(self):
        _CountingThread.spawned += 1


@pytest.fixture
def env(monkeypatch):
    # 这个文件测的是「默认生产语义」：只有 transport 走复测，429 立即定性。
    # 不 pin 住就会被 config.py 里临时切换的实验档位带跑偏（2026-11-13 踩过）。
    monkeypatch.setitem(daemon.CFG["warp"], "rate429_recheck_mode", "borrowed")
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "consumer_slots", lambda: SLOTS)
    monkeypatch.setattr(daemon, "_slot_current",
                        lambda slot: ("WARP-A", "2606:a::1")
                        if slot["name"] == "slot-1" else (None, None))
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_probe_verdict_for_ip", lambda *a, **k: None)
    daemon._EGRESS_COOLING.clear()
    daemon._EGRESS_RECHECK.clear()
    _InlineThread.spawned = 0
    _CountingThread.spawned = 0
    return types.SimpleNamespace()


def _probe_stub(verdict):
    def probe(tag, ip, fresh=False):
        # fresh=True 必须被传下去：复用 30s 缓存等于拿旧结论给新故障盖章。
        assert fresh is True, "复测必须绕过换线前实测缓存"
        return {"tag": tag, "ip": ip, "verdict": verdict, "sec": 2.0,
                "detail": "", "retry_after": None, "elapsed": 2.0, "ts": 0.0}
    return probe


def test_transport_with_ok_recheck_does_not_cool(env, monkeypatch):
    """复测 OK = 线路没坏，只是上一次大请求慢，不能打 90s 冷却。"""
    monkeypatch.setattr(daemon.threading, "Thread", _InlineThread)
    monkeypatch.setattr(daemon, "_probe_line_before_rotate", _probe_stub("OK"))
    daemon._cool_egress_after_recheck("2606:a::1", "WARP-A")
    assert daemon._egress_cooling_left("2606:a::1") == 0.0
    assert _InlineThread.spawned == 1


def test_transport_with_failed_recheck_still_cools(env, monkeypatch):
    """复测不通 = 线路真坏，冷却照旧。"""
    monkeypatch.setattr(daemon.threading, "Thread", _InlineThread)
    monkeypatch.setattr(daemon, "_probe_line_before_rotate",
                        _probe_stub("unreachable"))
    daemon._cool_egress_after_recheck("2606:a::1", "WARP-A")
    assert daemon._egress_cooling_left("2606:a::1") > 80


def test_rate429_keeps_immediate_cooling_without_recheck(env, monkeypatch):
    """borrowed 模式下原地 429 不走复测：它是配额的直接证据，立即定性。"""
    calls = []
    monkeypatch.setattr(daemon, "_cool_egress_after_recheck",
                        lambda *a, **k: calls.append(a))
    daemon._rotate_slot_once(SLOTS[0], "rate429", req_id="REQ-429")
    assert not calls, "429 不该起复测：%s" % (calls,)
    assert daemon._egress_cooling_left("2606:a::1") > 80


def test_transport_report_goes_through_recheck_not_direct_cool(env, monkeypatch):
    """接线：transport 报障不再直接冷却，而是交给复测决策。"""
    calls = []
    monkeypatch.setattr(daemon, "_cool_egress_after_recheck",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setattr(daemon.threading, "Thread", _CountingThread)
    daemon._rotate_slot_once(SLOTS[0], "transport", req_id="REQ-T")
    assert calls == [("2606:a::1", "WARP-A", "transport")], \
        "复测函数必须收到原始 reason，否则日志分不清报的是 transport 还是 429：%s" % (calls,)
    assert daemon._egress_cooling_left("2606:a::1") == 0.0


def test_repeated_reports_do_not_spawn_recheck_storm(env, monkeypatch):
    """同一出口并发重复报障只起一次复测：后面的复用那次的结论。"""
    monkeypatch.setattr(daemon.threading, "Thread", _CountingThread)
    for _ in range(5):
        daemon._cool_egress_after_recheck("2606:a::1", "WARP-A")
    assert _CountingThread.spawned == 1
    # 窗口过期（等价的清表）之后才允许下一次复测。
    daemon._EGRESS_RECHECK.clear()
    daemon._cool_egress_after_recheck("2606:a::1", "WARP-A")
    assert _CountingThread.spawned == 2


def test_recheck_ok_still_throttles_within_window(env, monkeypatch):
    """复测判 OK 之后紧接着的重复报障不再复测：一次结论管一个窗口。"""
    monkeypatch.setattr(daemon.threading, "Thread", _InlineThread)
    monkeypatch.setattr(daemon, "_probe_line_before_rotate", _probe_stub("OK"))
    daemon._cool_egress_after_recheck("2606:a::1", "WARP-A")
    daemon._cool_egress_after_recheck("2606:a::1", "WARP-A")
    assert _InlineThread.spawned == 1
    assert daemon._egress_cooling_left("2606:a::1") == 0.0


def test_already_cooling_does_not_reprobe(env, monkeypatch):
    """复测已判不通并冷却的出口：重复报障直接跳过，不再打探针。"""
    monkeypatch.setattr(daemon.threading, "Thread", _CountingThread)
    daemon._mark_egress_cooling("2606:a::1")
    daemon._cool_egress_after_recheck("2606:a::1", "WARP-A")
    assert _CountingThread.spawned == 0


def test_recheck_without_line_conclusion_cools_conservatively(env, monkeypatch):
    """读不到是哪条线就没法复测：退回原逻辑冷却，不能当「线路没问题」。"""
    monkeypatch.setattr(daemon.threading, "Thread", _CountingThread)
    daemon._cool_egress_after_recheck("2606:a::1", None)
    assert _CountingThread.spawned == 0
    assert daemon._egress_cooling_left("2606:a::1") > 80


def test_recheck_exception_falls_back_to_cooling(env, monkeypatch):
    """复测本身抛异常（探针缺失等）也必须冷却，不能把未知当干净。"""
    monkeypatch.setattr(daemon.threading, "Thread", _InlineThread)

    def boom(tag, ip, fresh=False):
        raise RuntimeError("probe crashed")
    monkeypatch.setattr(daemon, "_probe_line_before_rotate", boom)
    daemon._cool_egress_after_recheck("2606:a::1", "WARP-A")
    assert daemon._egress_cooling_left("2606:a::1") > 80
    assert "2606:a::1" in daemon._EGRESS_RECHECK, "异常也要落窗口，防止连环重测"
