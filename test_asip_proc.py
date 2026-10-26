"""Deterministic tests for asip.proc (no network, no long-running commands)."""

import os
import sys
import time

import pytest

from asip import proc
from asip.proc import ProcResult, run, run_curl, run_systemctl


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _record_run(monkeypatch):
    captured = {}

    def fake(argv, *, timeout, cwd=None, env=None, redact=True):
        captured["argv"] = argv
        captured["timeout"] = timeout
        captured["redact"] = redact
        return ProcResult(0, stdout="ok")

    monkeypatch.setattr(proc, "run", fake)
    return captured


def test_run_success_returns_code_and_stdout():
    result = run(_py("print('hi')"), timeout=10)
    assert result.code == 0
    assert result.stdout == "hi\n"
    assert result.ok is True
    assert result.timed_out is False
    assert result.error is None


def test_run_nonzero_exit_is_reported_not_raised():
    result = run(_py("import sys; sys.stderr.write('boom'); sys.exit(3)"),
                 timeout=10)
    assert result.code == 3
    assert result.ok is False
    assert "boom" in result.stderr


def test_run_timeout_marks_timed_out_and_clears_code():
    result = run(_py("import time; time.sleep(5)"), timeout=0.3)
    assert result.timed_out is True
    assert result.code is None
    assert result.ok is False
    assert result.error == "timeout after 0.3s"


def test_run_timeout_reaps_grandchildren(tmp_path):
    """超时必须杀掉整个进程组，而不是只杀直接子进程。"""
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(30)\n"
    )
    result = run(_py(code), timeout=1.0)
    assert result.timed_out is True
    pid = int(pidfile.read_text())
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"孙进程 {pid} 在超时后仍存活，killpg 未生效")


def test_run_timeout_survives_process_exiting_before_kill(monkeypatch):
    """孙进程在超时判定瞬间退出：getpgid/kill 抛 ProcessLookupError 不得外抛。"""
    import subprocess

    class RacyChild:
        pid = 424242
        returncode = None
        stdout = None
        stderr = None

        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
            return "", ""

        def kill(self):
            raise ProcessLookupError

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(proc.subprocess, "Popen",
                        lambda *a, **k: RacyChild())
    monkeypatch.setattr(proc.os, "getpgid",
                        lambda pid: (_ for _ in ()).throw(ProcessLookupError))
    result = run(["x"], timeout=1)
    assert result.timed_out is True
    assert result.code is None


def test_run_timeout_does_not_block_on_pipe_holding_descendant(monkeypatch):
    """孙进程逃出进程组仍持有管道时，二次 communicate 必须有上限。"""
    import subprocess

    class StuckChild:
        pid = 424243
        returncode = None
        stdout = open(os.devnull, "r")
        stderr = open(os.devnull, "r")

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1 or timeout is None:
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout or 0)
            return "", ""

        def wait(self, timeout=None):
            return 0

    child = StuckChild()
    monkeypatch.setattr(proc.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(proc.os, "getpgid", lambda pid: 1)
    monkeypatch.setattr(proc.os, "killpg", lambda pgid, sig: None)
    started = time.time()
    result = run(["x"], timeout=0.1)
    assert time.time() - started < 10
    assert result.timed_out is True
    assert result.error == "timeout after 0.1s"
    child.stdout.close()
    child.stderr.close()


def test_run_accepts_mapping_env():
    result = run(_py("import os; print(os.environ.get('ASIP_X', ''))"),
                 timeout=10, env={"ASIP_X": "1", "PATH": os.environ.get("PATH", "")})
    assert result.stdout.strip() == "1"


def test_run_missing_binary_returns_error_instead_of_raising():
    result = run(["asip-no-such-binary-xyz"], timeout=5)
    assert result.code is None
    assert result.ok is False
    assert result.error is not None
    assert "binary not found" in result.error


def test_run_redacts_stderr_by_default():
    code = ("import sys; sys.stderr.write("
            "'Authorization: Bearer abc.def token=deadbeef')")
    result = run(_py(code), timeout=10)
    assert "abc.def" not in result.stderr
    assert "deadbeef" not in result.stderr
    assert "***" in result.stderr


def test_run_redact_false_keeps_stderr_verbatim():
    code = "import sys; sys.stderr.write('Authorization: Bearer abc.def')"
    result = run(_py(code), timeout=10, redact=False)
    assert "abc.def" in result.stderr


def test_run_never_redacts_stdout():
    result = run(_py("print('token=deadbeef')"), timeout=10)
    assert result.stdout == "token=deadbeef\n"


def test_run_curl_builds_socks_argv_and_hard_timeout(monkeypatch):
    captured = _record_run(monkeypatch)
    result = run_curl(["https://example.test/trace"], timeout=7,
                      socks_port=1080, extra=("-H", "X-Test: 1"))
    assert result.ok is True
    assert captured["argv"] == [
        "curl", "-sS", "--max-time", "7",
        "--proxy", "socks5h://127.0.0.1:1080",
        "https://example.test/trace", "-H", "X-Test: 1",
    ]
    assert captured["timeout"] == 12


def test_run_curl_builds_http_proxy_argv(monkeypatch):
    captured = _record_run(monkeypatch)
    run_curl(["https://example.test"], timeout=3,
             http_proxy="http://127.0.0.1:7890")
    assert captured["argv"] == [
        "curl", "-sS", "--max-time", "3",
        "--proxy", "http://127.0.0.1:7890",
        "https://example.test",
    ]


def test_run_curl_rejects_socks_and_http_together(monkeypatch):
    _record_run(monkeypatch)
    with pytest.raises(ValueError):
        run_curl(["https://example.test"], timeout=3, socks_port=1080,
                 http_proxy="http://127.0.0.1:7890")


def test_run_systemctl_uses_user_scope_and_default_timeout(monkeypatch):
    captured = _record_run(monkeypatch)
    result = run_systemctl("restart", "auto-switch-ip.service")
    assert result.ok is True
    assert captured["argv"] == [
        "systemctl", "--user", "restart", "auto-switch-ip.service",
    ]
    assert captured["timeout"] == 20


def test_run_systemctl_honours_custom_timeout(monkeypatch):
    captured = _record_run(monkeypatch)
    run_systemctl("is-active", "x.service", timeout=5)
    assert captured["timeout"] == 5


@pytest.mark.parametrize("bad", [0, -1, None])
def test_run_rejects_non_positive_timeout(bad):
    with pytest.raises(ValueError):
        run(_py("print('x')"), timeout=bad)


def test_run_replaces_invalid_utf8_instead_of_raising():
    """curl/bun 的错误输出可能不是 UTF-8；解码失败不能把超时结果变成异常。"""
    result = run(_py("import sys; sys.stdout.buffer.write(b'\\xff\\xfe')"),
                 timeout=10)
    assert result.code == 0
    assert result.stdout
