#!/usr/bin/env python3
"""probe_warp 纯单元测试（无网络）：env 块、每日会话集、错误分类。"""
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


def test_daily_sessions_fixed_15_and_cross_day_unique():
    today = probe_warp._daily_sessions("20261112")
    assert len(today) == 15
    assert len(set(today)) == 15
    assert all(re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", s) for s in today)
    # 同一天重复调用恒定
    assert probe_warp._daily_sessions("20261112") == today
    # 跨天必然不重样
    assert not set(today) & set(probe_warp._daily_sessions("20261113"))
    # counter 落在 1..15（上游只接受 0..16，>=100 直接 403）
    counters = sorted(
        int((~int(s[4:16], 16)) & probe_warp._MASK48) % 4096 for s in today)
    assert counters == list(range(1, 16))


def test_pick_session_always_from_daily_set():
    today = set(probe_warp._daily_sessions())
    picked = {probe_warp._pick_session() for _ in range(200)}
    assert picked <= today
    # 200 次抽样覆盖全部 15 个（单个漏掉的概率 15 * (14/15)^200 ≈ 1e-5）
    assert len(picked) == 15


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
