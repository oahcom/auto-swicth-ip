import os
import sys
import time

import pytest

from asip import proc


def _python(code):
    return [sys.executable, "-c", code]


def test_normal_exit_captures_both_streams():
    result = proc.run(
        _python(
            "import sys; "
            "sys.stdout.write('stdout-ok'); "
            "sys.stderr.write('stderr-ok')"
        ),
        timeout=2,
    )

    assert result.code == 0
    assert result.timed_out is False
    assert result.stdout == "stdout-ok"
    assert result.stderr == "stderr-ok"


def test_timeout_kills_process_and_returns_captured_output():
    result = proc.run(
        _python(
            "import sys,time; "
            "print('before-timeout', flush=True); "
            "time.sleep(10)"
        ),
        timeout=0.5,
    )

    assert result.code is None
    assert result.timed_out is True
    assert "before-timeout" in result.stdout
    assert "timeout after 0.5s" in result.error


def test_oversized_stdout_is_truncated_and_drained():
    size = proc.MAX_CAPTURE_BYTES + 128 * 1024
    result = proc.run(
        _python(
            "import sys; "
            f"sys.stdout.write('A' * {size}); "
            "sys.stdout.flush(); "
            "print('TAIL-MUST-BE-DRAINED')"
        ),
        timeout=3,
    )

    assert result.code == 0
    assert len(result.stdout.encode("utf-8")) <= (
        proc.MAX_CAPTURE_BYTES + len(proc._CAPTURE_TRUNCATION_MARKER)
    )
    assert result.stdout.startswith("A" * 1024)
    assert result.stdout.endswith("\n...[truncated]")
    assert "TAIL-MUST-BE-DRAINED" not in result.stdout


def test_oversized_stderr_is_truncated_and_drained():
    size = proc.MAX_CAPTURE_BYTES + 128 * 1024
    result = proc.run(
        _python(
            "import sys; "
            f"sys.stderr.write('B' * {size}); "
            "sys.stderr.flush(); "
            "sys.stderr.write('TAIL-MUST-BE-DRAINED')"
        ),
        timeout=3,
    )

    assert result.code == 0
    assert len(result.stderr.encode("utf-8")) <= (
        proc.MAX_CAPTURE_BYTES + len(proc._CAPTURE_TRUNCATION_MARKER)
    )
    assert result.stderr.startswith("B" * 1024)
    assert result.stderr.endswith("\n...[truncated]")
    assert "TAIL-MUST-BE-DRAINED" not in result.stderr


def test_stdout_and_stderr_large_output_do_not_deadlock():
    size = proc.MAX_CAPTURE_BYTES * 4
    result = proc.run(
        _python(
            "import sys; "
            f"data = 'X' * {size}; "
            "sys.stdout.write(data); sys.stdout.flush(); "
            "sys.stderr.write(data); sys.stderr.flush()"
        ),
        timeout=3,
    )

    assert result.code == 0
    assert result.timed_out is False
    assert result.stdout.startswith("X" * 1024)
    assert result.stderr.startswith("X" * 1024)
    assert result.stdout.endswith("\n...[truncated]")
    assert result.stderr.endswith("\n...[truncated]")


@pytest.mark.skipif(os.name == "nt", reason="process-group semantics are POSIX-specific")
def test_descendant_holding_pipe_does_not_hang_run():
    started = time.monotonic()

    result = proc.run(
        _python(
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(10)']); "
            "print('parent-exited', flush=True)"
        ),
        timeout=0.5,
    )

    elapsed = time.monotonic() - started

    assert result.code == 0
    assert result.timed_out is False
    assert "parent-exited" in result.stdout
    assert elapsed < 5
