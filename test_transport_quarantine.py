# trap 线短禁契约（2026-11-16 晚复盘，transport 双杀上报）：
#   单发上报 = 抖动证据不记脏；10min 窗口内 ≥2 次 = trap 线实锤 → kind=transport
#   短禁（transport_ban_sec，默认 300s），到期复活。当晚 LHR-83 一条线反复处决
#   10 次仍回池（transport 在旧语义下从不标脏 + 30min 探针缓存当它是 OK 线）。
import pytest

import daemon
from asip import ledger_policy


def _mark_isolate(monkeypatch):
    """隔离 IP 台账副作用 + trap 计数 + 日志（与 test_429_cooldown 同款）。"""
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_append_warp_ip_history", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_probe_cache_forget", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_TRANSPORT_STRIKES", {})
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)


def test_single_transport_strike_does_not_ban(monkeypatch):
    _mark_isolate(monkeypatch)
    daemon._transport_strike_account("1.2.3.4", 1000.0)
    assert "1.2.3.4" not in daemon._WARP_IP_USED, \
        "单发 transport 上报只是抖动证据，不得记脏"


def test_double_strike_writes_transport_short_ban(monkeypatch):
    _mark_isolate(monkeypatch)
    daemon._transport_strike_account("1.2.3.4", 1000.0)
    daemon._transport_strike_account("1.2.3.4", 1030.0)
    entry = daemon._WARP_IP_USED["1.2.3.4"]
    assert entry["blocked"] is True
    assert entry["blocked_ts_by_kind"]["transport"] == 1030.0
    assert not entry.get("ban_until"), \
        "transport 不走精确禁（_warp_ban_until 只认 rate429 的 Retry-After）"


def test_strikes_expire_after_10min_window(monkeypatch):
    _mark_isolate(monkeypatch)
    daemon._transport_strike_account("1.2.3.4", 1000.0)
    daemon._transport_strike_account("1.2.3.4", 1000.0 + 660.0)  # 11min 后
    assert "1.2.3.4" not in daemon._WARP_IP_USED, \
        "窗口外的旧上报不得累计成双杀"


def test_transport_ban_release_schedule(monkeypatch):
    _mark_isolate(monkeypatch)
    t0 = 1000.0
    daemon._transport_strike_account("1.2.3.4", t0)
    daemon._transport_strike_account("1.2.3.4", t0 + 5)
    entry = daemon._WARP_IP_USED["1.2.3.4"]
    ttl = ledger_policy.make_ttl(daemon._warp())
    assert ttl["transport"] == 300.0, "make_ttl 必须带 transport 档（否则落 legacy 24h）"
    # 真实执法路径（不 mock）：禁从第二击（t0+5）起算 300s → +304 仍禁、+306 放
    assert ledger_policy.is_effectively_blocked(entry, t0 + 304.0, ttl) is True
    assert ledger_policy.is_effectively_blocked(entry, t0 + 306.0, ttl) is False
    assert daemon._warp_ip_is_blocked("1.2.3.4", t0 + 304.0) is True
    assert daemon._warp_ip_is_blocked("1.2.3.4", t0 + 306.0) is False


def test_warp_block_ttl_transport_configurable(monkeypatch):
    monkeypatch.setitem(daemon.CFG["warp"], "transport_ban_sec", 120)
    assert daemon._warp_block_ttl("transport") == 120.0
    assert ledger_policy._block_ttl(
        "transport", ledger_policy.make_ttl(daemon._warp())) == 120.0
    assert daemon._warp_block_ttl("rate429") == daemon.CFG["warp"]["rate429_guard_sec"], \
        "transport 档不得污染既有档位"


def test_shipped_default_transport_ban_sec():
    # 出厂默认锁（2026-11-16 架空缺陷教训：默认值 + 执法链必须走真实 CFG）
    assert float(daemon.CFG["warp"].get("transport_ban_sec", 0)) == 300.0
    assert daemon._warp_block_ttl("transport") == 300.0
