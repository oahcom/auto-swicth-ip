#!/usr/bin/env python3
"""probe_warp 纯单元测试（无网络）：env 块、按 IP 会话池、错误分类。"""
import json
import re
import time

import probe_warp


def test_env_marker_prepended_when_no_system_message():
    body = probe_warp._apply_env_marker(
        {"messages": [{"role": "user", "content": "say ok"}]})

    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    content = body["messages"][0]["content"]
    assert content.startswith("Here is some useful information")
    assert content.endswith("</env>")
    # 路径每请求随机（base + 2~3 位 CSPRNG 后缀），只钉形状不钉具体值。
    assert re.search(r"  Workspace root folder: /(?:home/user|workspace|srv)/[a-z]+-[a-z0-9]{2,3}", content)


def test_env_marker_appended_to_existing_system_message():
    body = probe_warp._apply_env_marker(
        {"messages": [{"role": "system", "content": "be brief"},
                      {"role": "user", "content": "say ok"}]})

    assert body["messages"][0]["content"].startswith("be brief")
    assert body["messages"][0]["content"].endswith("</env>")
    assert len(body["messages"]) == 2


def test_env_marker_is_idempotent():
    body = probe_warp._apply_env_marker(
        {"messages": [{"role": "system", "content": probe_warp.ENV_MARKER},
                      {"role": "user", "content": "say ok"}]})

    assert body["messages"][0]["content"] == probe_warp.ENV_MARKER
    assert body["messages"][0]["content"].count("<env>") == 1


def test_body_carries_env_marker():
    body = probe_warp._body()

    assert body["messages"][0]["role"] == "system"
    content = body["messages"][0]["content"]
    assert "<env>" in content and "</env>" in content
    assert "Working directory: " in content


def test_classify_quota_and_flagged():
    assert probe_warp.classify(429, '{"type":"FreeUsageLimitError"}') == "quota"
    assert probe_warp.classify(403, "abusive traffic") == "flagged"
    assert probe_warp.classify(200, "data: {}") == "OK"
    assert probe_warp.classify(500, "boom") == "err"


def test_session_pool_15_per_ip_and_regenerate_on_ip_change():
    probe_warp._SESSION_POOLS.clear()
    seen = set()
    for _ in range(15 * probe_warp.SESSION_POOL_SWITCH_EVERY):
        seen.add(probe_warp._pick_session("ip-a"))
    assert len(seen) == probe_warp.SESSION_POOL_SIZE == 15
    assert all(re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", s) for s in seen)
    # 同一个 IP 循环复用时不再生成新 id
    again = {probe_warp._pick_session("ip-a") for _ in range(150)}
    assert again == seen
    # IP 变化立即换一组全新的、与旧组不相交的 15 个
    fresh = {probe_warp._pick_session("ip-b") for _ in range(150)}
    assert len(fresh) == 15
    assert not (seen & fresh)


def test_session_pool_wraps_to_first_after_15_sessions():
    probe_warp._SESSION_POOLS.clear()
    total = probe_warp.SESSION_POOL_SIZE * probe_warp.SESSION_POOL_SWITCH_EVERY
    seq = [probe_warp._pick_session("ip-wrap") for _ in range(total + 1)]
    assert len(set(seq)) == probe_warp.SESSION_POOL_SIZE
    assert seq[total] == seq[0]  # 第 151 次回绕到第 1 个（均分循环）


def test_session_switches_at_least_every_10_within_ip():
    probe_warp._SESSION_POOLS.clear()
    seq = [probe_warp._pick_session("ip-c") for _ in range(11)]
    assert len(set(seq[:10])) == 1          # 前 10 次同一个会话
    assert seq[10] != seq[0]                # 第 11 次切到下一个
    rotated = probe_warp._pick_session("ip-c", rotate=True)
    assert rotated != seq[10]               # quota 重试立即切


def test_unknown_egress_sentinel_does_not_share_pool():
    probe_warp._SESSION_POOLS.clear()
    seen = {probe_warp._pick_session("?") for _ in range(15 * probe_warp.SESSION_POOL_SWITCH_EVERY)}
    # egress_ip 失败返回 "?"，必须归一到未知池，不能把 "?" 当成真实出口 key。
    assert "?" not in probe_warp._SESSION_POOLS
    assert "-" in probe_warp._SESSION_POOLS
    assert len(seen) == probe_warp.SESSION_POOL_SIZE


def test_parse_retry_after_header_numeric():
    assert probe_warp.parse_retry_after({"retry-after": "16671"}) == 16671
    assert probe_warp.parse_retry_after({"Retry-After": "90"}) == 90
    assert probe_warp.parse_retry_after({"x-ratelimit-reset-after": "30"}) == 30


def test_parse_retry_after_header_wins_over_body():
    head = '{"retry_after": 10}'
    assert probe_warp.parse_retry_after({"retry-after": "120"}, head) == 120


def test_parse_retry_after_body_fields_and_text():
    assert probe_warp.parse_retry_after({}, '{"retry_after": 30}') == 30
    assert probe_warp.parse_retry_after({}, '{"retry_after_ms": 1500}') == 1
    assert probe_warp.parse_retry_after({}, "please retry in 2h") == 7200


def test_parse_retry_after_http_date():
    from email.utils import formatdate
    now = 1_700_000_000.0
    date = formatdate(now + 600, usegmt=True)
    assert probe_warp.parse_retry_after({"retry-after": date}, "", now=now) == 600


def test_parse_retry_after_missing_and_clamped():
    assert probe_warp.parse_retry_after({}, "") is None
    assert probe_warp.parse_retry_after({"retry-after": "0"}) is None
    assert probe_warp.parse_retry_after({"retry-after": "999999"}) == 86400
