"""gate_watchdog 影子模式分支测试（只读，不发请求、不写生产状态）。"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from asip.failure_domain import FailureDomain, ProbeClass  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "gate_watchdog_shadow", PROJECT / "tools" / "gate_watchdog.py")
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)


def test_default_shadow_is_on():
    assert gw.SHADOW is True


@pytest.mark.parametrize("evidence, expected", [
    (None, FailureDomain.UNKNOWN),
    ({}, FailureDomain.UNKNOWN),
    ({"domain": "account"}, FailureDomain.ACCOUNT),
    ({"classification": "model_quota"}, FailureDomain.MODEL),
    ({"classification": "rate_limited"}, FailureDomain.REQUEST),
    ({"classification": "context_rejected"}, ProbeClass.CONTEXT_REJECTED),
    ({"reason": "403 abuse detected"}, ProbeClass.CONTEXT_REJECTED),
    ({"reason": "transport timeout"}, FailureDomain.TRANSPORT),
    ({"reason": "model quota exceeded"}, FailureDomain.MODEL),
    ({"reason": "free usage limit"}, FailureDomain.ACCOUNT),
    ({"reason": "429 rate limit"}, FailureDomain.REQUEST),
    ({"domain": "exit_ip"}, FailureDomain.EXIT_IP),
    ({"classification": "ip_blocked"}, FailureDomain.EXIT_IP),
])
def test_classify_failure(evidence, expected):
    assert gw.classify_failure(evidence) is expected


@pytest.mark.parametrize("domain", [
    FailureDomain.ACCOUNT,
    FailureDomain.MODEL,
    FailureDomain.REQUEST,
    ProbeClass.CONTEXT_REJECTED,
    "account_quota",
    "model_quota",
    "rate_limited",
    "context_rejected",
])
def test_no_rotate_domains_hold(domain):
    action, reason = gw.decide(domain, {"evidence_id": "e1"}, shadow=True)
    assert action == "hold"
    assert "不换节点" in reason


def test_unknown_keeps_observing_even_with_evidence():
    action, reason = gw.decide(FailureDomain.UNKNOWN, {"evidence_id": "e1"}, shadow=True)
    assert action == "observe"
    assert "继续观测" in reason


def test_transport_checks_link_not_rotate():
    action, reason = gw.decide(FailureDomain.TRANSPORT, {"evidence_id": "e1"}, shadow=True)
    assert action == "check_transport"
    assert "代理链路" in reason


def test_exit_ip_without_evidence_only_observes():
    action, reason = gw.decide(FailureDomain.EXIT_IP, None, shadow=True)
    assert action == "observe"
    assert "独立确认" in reason


def test_exit_ip_with_evidence_is_would_rotate_in_shadow():
    action, reason = gw.decide(
        FailureDomain.EXIT_IP, {"evidence_id": "ip-block-evidence"}, shadow=True)
    assert action == "would_rotate"
    assert "不执行 PUT" in reason


def test_exit_ip_with_evidence_rotates_only_in_live():
    action, _ = gw.decide(
        FailureDomain.EXIT_IP, {"independent_confirm": True}, shadow=False)
    assert action == "rotate"


@pytest.mark.parametrize("evidence, expected", [
    ({"evidence_id": "x"}, True),
    ({"evidence_confirmed": True}, True),
    ({"independent_confirm": "probe-2"}, True),
    ({"reason": "exit_ip"}, False),
    (None, False),
])
def test_evidence_confirmed(evidence, expected):
    assert gw.evidence_confirmed(evidence) is expected


def _wire(monkeypatch, tmp_path, evidence):
    """把 I/O 全部指向临时目录，避免碰生产状态。"""
    monkeypatch.setattr(gw, "targets", lambda: [(7900, 9900, "slot-test")])
    monkeypatch.setattr(gw, "egress_ok", lambda *a, **k: False)
    monkeypatch.setattr(gw, "alive_nodes", lambda: set())
    monkeypatch.setattr(gw, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(gw, "SHADOW_STATE", tmp_path / "shadow.json")
    monkeypatch.setattr(gw, "LOG", tmp_path / "run.log")
    ev = tmp_path / "evidence.json"
    ev.write_text(json.dumps({7900: evidence}), encoding="utf-8")
    monkeypatch.setattr(gw, "EVIDENCE", ev)


def test_two_rounds_shadow_never_calls_rotate(monkeypatch, tmp_path):
    calls = []
    _wire(monkeypatch, tmp_path, {"domain": "exit_ip", "evidence_id": "e1"})

    def forbidden(*a, **k):
        calls.append((a, k))
        raise AssertionError("SHADOW 下不得调用 rotate")

    monkeypatch.setattr(gw, "rotate", forbidden)
    # 第 1 轮只计数；第 2、3 轮各产生一次决策（影子下计数不归零，持续告警）。
    gw.one_round(shadow=True)
    gw.one_round(shadow=True)
    gw.one_round(shadow=True)

    assert calls == []
    report = gw.shadow_report(tmp_path / "shadow.json")
    assert report["counts"].get("exit_ip") == 2
    assert report["would_rotate"] == 2
    assert report["actual_rotate"] == 0


def test_two_rounds_account_never_rotates_even_if_forced_live(monkeypatch, tmp_path):
    calls = []
    _wire(monkeypatch, tmp_path, {"classification": "account_quota"})

    def forbidden(*a, **k):
        calls.append((a, k))
        raise AssertionError("账号配额不得换节点")

    monkeypatch.setattr(gw, "rotate", forbidden)
    gw.one_round(shadow=False)
    gw.one_round(shadow=False)
    gw.one_round(shadow=False)

    assert calls == []
    report = gw.shadow_report(tmp_path / "shadow.json")
    assert report["counts"].get("account") == 2
    assert report["would_rotate"] == 0


def test_live_mode_calls_rotate_only_for_confirmed_exit_ip(monkeypatch, tmp_path):
    calls = []
    _wire(monkeypatch, tmp_path, {"domain": "exit_ip", "evidence_id": "e1"})
    monkeypatch.setattr(gw, "rotate", lambda *a, **k: calls.append((a, k)) or "node-A")
    gw.one_round(shadow=False)
    gw.one_round(shadow=False)
    assert len(calls) == 1


def test_shadow_report_is_readonly(monkeypatch, tmp_path):
    p = tmp_path / "shadow.json"
    payload = {"counts": {"unknown": 3}, "would_rotate": 1, "actual_rotate": 0,
               "samples": [{"domain": "unknown"}]}
    p.write_text(json.dumps(payload), encoding="utf-8")
    before = p.read_bytes()
    r = gw.shadow_report(p)
    assert r["counts"] == {"unknown": 3}
    assert r["would_rotate"] == 1
    assert r["actual_rotate"] == 0
    assert p.read_bytes() == before


def test_normalize_unknown_string():
    assert gw.normalize_domain("nonsense") is FailureDomain.UNKNOWN
    assert gw.normalize_domain(None) is FailureDomain.UNKNOWN
