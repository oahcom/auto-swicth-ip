"""verify_rotation.journal 的失败语义（不联网，monkeypatch proc_run）。"""

from asip.proc import ProcResult

import verify_rotation


def test_journal_marks_error_instead_of_empty_on_failure(monkeypatch):
    """journalctl 失败不能折叠成空列表，否则「无异常」检查恒过。"""
    monkeypatch.setattr(verify_rotation, "proc_run",
                        lambda *a, **k: ProcResult(code=1, stderr="boom"))
    lines = verify_rotation.journal("5 minutes ago", "Traceback")
    assert list(lines) == []
    assert lines.error and "rc=1" in lines.error


def test_journal_marks_timeout(monkeypatch):
    monkeypatch.setattr(verify_rotation, "proc_run",
                        lambda *a, **k: ProcResult(code=None, timed_out=True))
    lines = verify_rotation.journal("5 minutes ago", "Traceback")
    assert lines.error == "journalctl 超时"


def test_journal_filters_lines_on_success(monkeypatch):
    body = "ok\nTraceback: x\nok again\n"
    monkeypatch.setattr(verify_rotation, "proc_run",
                        lambda *a, **k: ProcResult(code=0, stdout=body))
    lines = verify_rotation.journal("5 minutes ago", "Traceback")
    assert lines.error is None
    assert lines == ["Traceback: x"]
