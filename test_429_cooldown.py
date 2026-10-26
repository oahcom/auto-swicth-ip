#!/usr/bin/env python3
"""429 冷却时长解析链单测（离线、<10s）。

规格：429 标记出口冷却，时长按 `header（秒数/HTTP 日期）→ 响应体字段 →
报错文案 → 钳制 [1s, 24h] → 默认 60s` 确定。
- 解析内核复用 probe_warp.parse_retry_after（与 9router parseRetryAfterMs 对齐）；
- daemon 侧新增 `_latest_rate429_body_since`（生产库取最近 429 行）与
  `_parse_rate429_cooldown`（二级包装）；
- `_mark_warp_ip_used(ban_after=)→ban_until` 管道为既有机制，覆盖回归一次；
- config 的 60s 默认（rate429_guard_sec）直接断言。
"""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

import config
import daemon
import probe_warp

_NOW = 1_700_000_000.0

_E429_BODY = json.dumps({"response": {"status": 429, "error": json.dumps(
    {"error": {"type": "FreeUsageLimitError",
               "message": "Rate limit exceeded. Please try again later."}})}})
_E403_BODY = '{"response": {"status": 403, "error": "abusive traffic from this IP"}}'


def _mark_isolate(monkeypatch):
    """隔离 IP 台账的三类副作用（选窗文件 / 追加历史 / 探测缓存）。"""
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_save_warp_ip_used", lambda: None)
    monkeypatch.setattr(daemon, "_append_warp_ip_history", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_probe_cache_forget", lambda *a, **k: None)


def _mount_db(rows, monkeypatch):
    """生产同构 requestDetails（ISO 文本时间戳，julianday 再参与运算）。"""
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE requestDetails "
                "(provider TEXT, model TEXT, status TEXT, timestamp TEXT, data TEXT)")
    mem.executemany(
        "INSERT INTO requestDetails (provider,model,status,timestamp,data) "
        "VALUES (?,?,?,?,?)", rows)
    monkeypatch.setattr(daemon, "_get_db", lambda: mem)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---- 解析链·header 层（探测路径直接可见） ----
def test_header_retry_after_seconds_parsed():
    assert probe_warp.parse_retry_after({"retry-after": "120"}, "") == 120, \
        "header Retry-After 秒数应原样解析"

def test_header_retry_after_http_date_parsed():
    dt = datetime.now(timezone.utc) + timedelta(seconds=120)
    secs = probe_warp.parse_retry_after({"retry-after": format_datetime(dt)}, "")
    assert 118 <= (secs or 0) <= 121, f"HTTP 日期应换算为剩余秒数, 得到 {secs}"

def test_header_epoch_ms_parsed():
    header = {"x-ratelimit-reset": (_NOW + 30) * 1000}
    secs = probe_warp.parse_retry_after(header, "", now=_NOW)
    assert 29 <= (secs or 0) <= 31, f"epoch 毫秒应换算为剩余秒数, 得到 {secs}"

# ---- 解析链·响应体字段层（生产落库行可见） ----
def test_body_field_seconds_parsed():
    assert probe_warp.parse_retry_after(None, '{"retry_after": 30}') == 30, \
        "体键 retry_after 应解析为秒"

def test_body_field_milliseconds_parsed():
    assert probe_warp.parse_retry_after(None, '{"retry_after_ms": 5000}') == 5, \
        "体键 retry_after_ms 应按毫秒换算"

# ---- 解析链·报错文案层 ----
def test_message_text_hours_parsed():
    assert probe_warp.parse_retry_after(None, "Free usage limit reached. "
                                             "Try again in 4 hours") == 14400, \
        "文案里的时长短语应换算为秒"

# ---- 钳制与默认 ----
def test_clamp_floor_at_1s_and_cap_at_24h():
    assert probe_warp.parse_retry_after({"retry-after": "0.3"}, "") == 1, "下限 1s"
    assert probe_warp.parse_retry_after(None, '{"retry_after": 999999}') == 86400, "上限 24h"

def test_no_signal_returns_none_then_default_60s_applies():
    parsed = probe_warp.parse_retry_after(None, _E429_BODY)
    assert parsed is None, "无时长信号的 429 文案应返回 None 交给默认值"
    assert daemon._parse_rate429_cooldown(_E429_BODY) is None, "包装层不做默认注入"
    assert daemon._warp_block_ttl("rate429") == 60.0, "config 默认冷却应为 60s"


# ---- 生产库取行 ----
def test_latest_rate429_body_picks_newest_429_row(monkeypatch):
    rows = [
        ("opencode", "m", "error", _iso(_NOW + 5), _E403_BODY),   # 最新 403
        ("opencode", "m", "error", _iso(_NOW), _E429_BODY),        # 更旧 429
        ("opencode", "m", "success", _iso(_NOW - 5), None),
    ]
    _mount_db(rows, monkeypatch)
    body = daemon._latest_rate429_body_since(_NOW - 3600)
    assert body is not None and "Rate limit exceeded" in body, \
        "403 不应顶替 429 作为取数对象"
    assert daemon._latest_rate429_body_since(_NOW - 3600) == daemon \
        ._latest_rate429_body_since(_NOW - 3600), "重复调用需幂等（纯只读）"

def test_latest_rate429_body_returns_none_without_429(monkeypatch):
    rows = [
        ("opencode", "m", "error", _iso(_NOW + 5), _E403_BODY),
        ("opencode", "m", "success", _iso(_NOW), None),
    ]
    _mount_db(rows, monkeypatch)
    assert daemon._latest_rate429_body_since(_NOW - 3600) is None, \
        "无 429 行时应返回 None"

# ---- mark→ban_until 管道 ----
def test_mark_rate429_writes_ban_until_from_parsed_secs(monkeypatch):
    _mark_isolate(monkeypatch)
    daemon._mark_warp_ip_used("9.9.9.9", 1000.0, blocked=True,
                              kind="rate429", ban_after=150)
    entry = daemon._WARP_IP_USED["9.9.9.9"]
    assert entry["ban_until"] == pytest.approx(1150.0, abs=0.5), \
        "解析到的剩余秒数应写为 ban_until"
    assert entry["ban_kind"] == "rate429", "精确封禁只对 rate429 生效"

def test_mark_rate429_without_ban_after_keeps_default_window(monkeypatch):
    _mark_isolate(monkeypatch)
    daemon._mark_warp_ip_used("9.9.9.9", 1000.0, blocked=True, kind="rate429")
    entry = daemon._WARP_IP_USED["9.9.9.9"]
    assert not entry.get("ban_until"), "无解析值时不得伪造精确封禁"
    assert entry["blocked_ts_by_kind"].get("rate429") == 1000.0, \
        "默认冷却仍按 rate429 惩罚起点记账"
