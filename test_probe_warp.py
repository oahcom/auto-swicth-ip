#!/usr/bin/env python3
"""probe_warp 纯单元测试（无网络）：env 块、会话复用/轮换、错误分类。"""
import json
import time

import probe_warp


def test_env_marker_prepended_when_no_system_message():
    body = probe_warp._apply_env_marker(
        {"messages": [{"role": "user", "content": "say ok"}]})

    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == probe_warp.ENV_MARKER
    assert "Workspace root folder: /workspace" in body["messages"][0]["content"]


def test_env_marker_appended_to_existing_system_message():
    body = probe_warp._apply_env_marker(
        {"messages": [{"role": "system", "content": "be brief"},
                      {"role": "user", "content": "say ok"}]})

    assert body["messages"][0]["content"].startswith("be brief")
    assert body["messages"][0]["content"].endswith(probe_warp.ENV_MARKER)
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
    assert probe_warp.ENV_MARKER in body["messages"][0]["content"]


def test_classify_quota_and_flagged():
    assert probe_warp.classify(429, '{"type":"FreeUsageLimitError"}') == "quota"
    assert probe_warp.classify(403, "abusive traffic") == "flagged"
    assert probe_warp.classify(200, "data: {}") == "OK"
    assert probe_warp.classify(500, "boom") == "err"


def test_session_is_stable_and_rotates(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_warp, "SESSION_STATE_FILE", str(tmp_path / "s.json"))

    first = probe_warp._session_for(1081)
    assert first == probe_warp._session_for(1081)
    assert first.startswith("ses_") and len(first) == 4 + 12 + 14

    rotated = probe_warp._session_for(1081, rotate=True)
    assert rotated != first
    data = json.loads((tmp_path / "s.json").read_text())
    assert data["1081"]["session"] == rotated
    assert data["1081"]["rotations"] == 1


def test_session_expires(monkeypatch, tmp_path):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({
        "1081": {"session": "ses_old", "ts": time.time() - probe_warp.SESSION_TTL_SEC - 1,
                 "rotations": 3},
    }))
    monkeypatch.setattr(probe_warp, "SESSION_STATE_FILE", str(state))

    fresh = probe_warp._session_for(1081)
    assert fresh != "ses_old"
    assert json.loads(state.read_text())["1081"]["rotations"] == 0


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
