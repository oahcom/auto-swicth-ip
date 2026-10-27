"""self_evolve_checkpoint 日志检查的失败语义（grep rc=1 不是错误）。"""

import pytest

import self_evolve_checkpoint as sec
from asip.proc import ProcResult


def _stub(monkeypatch, result):
    monkeypatch.setattr(sec.proc, "run", lambda *a, **k: result)


def test_grep_no_match_is_not_an_error(monkeypatch):
    _stub(monkeypatch, ProcResult(code=1, stdout=""))
    lines, err = sec._grep_log("bypass")
    assert lines == [] and err is None


def test_grep_match_returns_lines(monkeypatch):
    _stub(monkeypatch, ProcResult(code=0, stdout="a\nb\n"))
    lines, err = sec._grep_log("bypass")
    assert lines == ["a", "b"] and err is None


@pytest.mark.parametrize("result,needle", [
    (ProcResult(code=2, stderr="grep: boom"), "rc=2"),
    (ProcResult(code=None, timed_out=True), "timeout"),
    (ProcResult(code=None, error="FileNotFoundError: grep"), "FileNotFoundError"),
])
def test_grep_failures_are_reported_not_silently_empty(monkeypatch, result, needle):
    _stub(monkeypatch, result)
    lines, err = sec._grep_log("bypass")
    assert lines == [] and needle in err


def test_check_reports_unavailable_instead_of_pass_on_grep_error(monkeypatch):
    """grep 挂掉时 bypass_events 不能返回「0 条 = 通过」。"""
    _stub(monkeypatch, ProcResult(code=None, timed_out=True))
    ok, metric, detail = sec.run_check("bypass_events", {})
    assert ok is False
    assert metric is None
    assert detail.startswith("unavailable:")
