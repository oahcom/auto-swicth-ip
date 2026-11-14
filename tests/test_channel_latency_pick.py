import json
import sys
import time

sys.path.insert(0, "/home/administrator/dkk-projects/auto-switch-ip")

import daemon as d

# 渠道原生延迟软偏好（2026-11-14）：
#  1. channel_latency.json 新鲜时按渠道延迟参与排序（同档内低者优先）；
#  2. 文件缺失/过期（>2h）一律当没有，排序退化为旧键（探针延迟兜底）——
#     软偏好不是硬过滤，没有这套测量时选择行为必须与旧版完全一致。


def _write_latency_file(tmp_path, ts, channels):
    p = tmp_path / "channel_latency.json"
    p.write_text(json.dumps({"ts": ts, "channels": channels}), encoding="utf-8")
    return str(p)


def _reset_cache(monkeypatch, path):
    monkeypatch.setattr(d, "_CHANNEL_LATENCY_PATH", path)
    with d._CHANNEL_LATENCY_LOCK:
        d._CHANNEL_LATENCY_CACHE["loaded_at"] = 0.0
        d._CHANNEL_LATENCY_CACHE["by_tag"] = {}


def test_channel_latency_ms_reads_fresh_file(monkeypatch, tmp_path):
    path = _write_latency_file(tmp_path, time.time(), [
        {"channel": "WARP-A", "result": {"tls_ms": 100.0}},
        {"channel": "WARP-B", "result": {"tls_ms": 900.0}},
        {"channel": "WARP-C", "result": None},
    ])
    _reset_cache(monkeypatch, path)
    assert d._channel_latency_ms("WARP-A") == 100.0
    assert d._channel_latency_ms("WARP-B") == 900.0
    assert d._channel_latency_ms("WARP-C") is None      # 探测失败的渠道
    assert d._channel_latency_ms("WARP-NOPE") is None   # 没测过的渠道
    assert d._channel_latency_ms("") is None


def test_stale_file_is_ignored(monkeypatch, tmp_path):
    path = _write_latency_file(tmp_path, time.time() - 8000, [
        {"channel": "WARP-A", "result": {"tls_ms": 100.0}},
    ])
    _reset_cache(monkeypatch, path)
    assert d._channel_latency_ms("WARP-A") is None


def test_missing_file_degrades_to_none(monkeypatch, tmp_path):
    _reset_cache(monkeypatch, str(tmp_path / "nope.json"))
    assert d._channel_latency_ms("WARP-A") is None


def _patch_rank_probe(monkeypatch, probe_delay=None):
    monkeypatch.setattr(d, "_warp_ip_rank", lambda ip, now, guard: (0, 0, 0))
    monkeypatch.setattr(d, "_warp_guard_for_reason", lambda r: None)
    monkeypatch.setattr(d, "_slot_probe_sec", lambda name: probe_delay)


def test_pick_key_prefers_lower_channel_latency(monkeypatch):
    # 其它键位全同的两个候选：渠道延迟低者优先
    _patch_rank_probe(monkeypatch, probe_delay=None)
    monkeypatch.setattr(
        d, "_channel_latency_ms",
        lambda tag, now=None: {"WARP-A": 100.0, "WARP-B": 900.0}.get(tag))
    k_a = d._slot_pick_key({"name": "slot-1"}, "WARP-A", "2a09::a", 0, None)
    k_b = d._slot_pick_key({"name": "slot-1"}, "WARP-B", "2a09::b", 0, None)
    assert k_a < k_b


def test_channel_latency_outranks_probe_delay_within_tier(monkeypatch):
    # 同档内原生延迟是更干净的速度信号，先于探针延迟比较
    _patch_rank_probe(monkeypatch, probe_delay=None)
    monkeypatch.setattr(d, "_slot_probe_sec",
                        lambda name: {"slot-1": 5.0, "slot-2": 1.0}.get(name))
    monkeypatch.setattr(
        d, "_channel_latency_ms",
        lambda tag, now=None: {"WARP-A": 100.0, "WARP-B": 900.0}.get(tag))
    k_a = d._slot_pick_key({"name": "slot-1"}, "WARP-A", "2a09::a", 0, None)
    k_b = d._slot_pick_key({"name": "slot-2"}, "WARP-B", "2a09::b", 0, None)
    assert k_a < k_b   # A 探针延迟差（5.0 vs 1.0）不敌 B 渠道延迟差


def test_no_channel_data_falls_back_to_probe_delay(monkeypatch):
    # 原生延迟全 None：排序与旧键一致（探针延迟决定）
    _patch_rank_probe(monkeypatch)
    monkeypatch.setattr(d, "_slot_probe_sec",
                        lambda name: {"slot-1": 1.0, "slot-2": 5.0}.get(name))
    monkeypatch.setattr(d, "_channel_latency_ms", lambda tag, now=None: None)
    k1 = d._slot_pick_key({"name": "slot-1"}, "WARP-A", "2a09::a", 0, None)
    k2 = d._slot_pick_key({"name": "slot-2"}, "WARP-B", "2a09::b", 0, None)
    assert k1 < k2


def test_serving_slot_still_sorts_last(monkeypatch):
    # 分层语义不变：serving 档仍压过任何延迟优势
    _patch_rank_probe(monkeypatch, probe_delay=None)
    monkeypatch.setattr(d, "_channel_latency_ms", lambda tag, now=None: 50.0)
    k_serving = d._slot_pick_key({"name": "slot-1"}, "WARP-A", "2a09::a", 0, "slot-1")
    k_other = d._slot_pick_key({"name": "slot-2"}, "WARP-B", "2a09::b", 0, "slot-1")
    assert k_serving > k_other
