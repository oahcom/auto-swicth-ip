"""Run child processes without leaving them unbounded or shell-parsed.

Every call requires an explicit ``timeout``. This project drives a daemon and
several operator CLIs that shell out to ``curl`` and ``systemctl``; before this
helper one hung ``curl`` could stall the rotation loop indefinitely. A mandatory
timeout turns that failure into a ``ProcResult`` with ``timed_out=True`` instead
of a process the caller can never reap.

``shell=True`` is deliberately not offered. Arguments include proxy URLs and
subscription tokens loaded from config, so a shell would add an injection
surface and force every caller to quote correctly. Passing an argv list keeps
those values inert.

Returned ``stderr`` and ``error`` are redacted by default because they often
echo a failing command line. ``stdout`` is left raw: callers parse it as data
(JSON bodies, IP addresses) and masking would corrupt it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Mapping, Sequence

from .redact import redact_secrets

# Grace period for reaping after the process group has been signalled. The
# direct child is normally already dead; this only bounds a wedged descendant.
_REAP_GRACE_SEC = 5.0

# 单个子进程单条流最多保留的字节数：超过就丢弃后续内容，但**继续 drain 到 EOF**，
# 否则子进程写满 pipe 会永久阻塞，超时语义也随之失效。
MAX_CAPTURE_BYTES = 256 * 1024
_CAPTURE_READ_SIZE = 64 * 1024
_CAPTURE_TRUNCATION_MARKER = b"\n...[truncated]"


@dataclass(frozen=True)
class ProcResult:
    """Outcome of one child process.

    ``code`` is ``None`` when the process could not be started or was killed on
    timeout. ``error`` is set for those cases only; a non-zero exit is reported
    through ``code`` and never raises.
    """

    code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.code == 0


def _require_positive_timeout(timeout: float) -> float:
    if timeout is None or timeout <= 0:
        raise ValueError("timeout must be greater than 0")
    return timeout


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _mask(value: str, redact: bool) -> str:
    return redact_secrets(value) if redact else value


def _kill_process_group(child: subprocess.Popen) -> None:
    """SIGKILL the whole session, falling back to the direct child.

    The child is started with ``start_new_session=True``, so its pid is also its
    process-group id. curl/bun can spawn helpers; killing only the direct child
    leaves those helpers holding the pipe and leaking across retries.
    """
    try:
        os.killpg(os.getpgid(child.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # The child may have exited between communicate() timing out and here.
        try:
            child.kill()
        except (ProcessLookupError, OSError):
            pass


def _kill_process_group_id(pgid: int) -> None:
    """杀掉一个进程组，即使组长已经退出。

    ``start_new_session=True`` 让 ``child.pid`` 就是 pgid；直接子进程退出后
    组里可能还有继承 pipe 的后代，必须按 pgid 杀，不能用 ``child.returncode``
    判断组是否还存在。
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass



def run(argv: Sequence[str], *, timeout: float, cwd: str | None = None,
        env: Mapping[str, str] | None = None, redact: bool = True) -> ProcResult:
    """Run ``argv`` and return a result instead of raising.

    stdout/stderr 各自由一条读取线程持续 drain，只保留前 ``MAX_CAPTURE_BYTES``；
    超限后继续读并丢弃，保证子进程不会因为 pipe 写满而卡死。超时仍先杀进程组
    再回收，``ProcResult`` 的字段语义不变。
    """
    _require_positive_timeout(timeout)
    argv = list(argv)
    argv0 = argv[0] if argv else ""

    try:
        child = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            start_new_session=True,
        )
    except FileNotFoundError:
        return ProcResult(
            code=None,
            error=_mask(f"binary not found: {argv0}", redact),
        )
    except OSError as exc:
        return ProcResult(
            code=None,
            error=_mask(f"binary not found: {argv0}: {exc}", redact),
        )

    captures = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    reader_errors: list[BaseException] = []

    def drain(name, stream):
        try:
            while True:
                chunk = stream.read(_CAPTURE_READ_SIZE)
                if not chunk:
                    break
                captured = captures[name]
                remaining = MAX_CAPTURE_BYTES - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[name] = True
        except (OSError, ValueError) as exc:
            reader_errors.append(exc)

    readers = [
        threading.Thread(target=drain, args=("stdout", child.stdout),
                         name="proc-stdout-drain", daemon=True),
        threading.Thread(target=drain, args=("stderr", child.stderr),
                         name="proc-stderr-drain", daemon=True),
    ]
    for reader in readers:
        reader.start()

    def captured_text(name) -> str:
        data = bytes(captures[name])
        if truncated[name]:
            data += _CAPTURE_TRUNCATION_MARKER
        return data.decode("utf-8", errors="replace")

    def join_readers(timeout_sec: float) -> None:
        for reader in readers:
            reader.join(timeout_sec)

    def close_streams() -> None:
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(child)
        try:
            child.wait(timeout=_REAP_GRACE_SEC)
        except subprocess.TimeoutExpired:
            pass
        join_readers(_REAP_GRACE_SEC)
        if any(reader.is_alive() for reader in readers):
            # 后代逃出进程组仍握着 pipe：关掉我们这端，别让 reader 永久挂住
            close_streams()
            join_readers(1.0)
        return ProcResult(
            code=None,
            stdout=_as_text(captured_text("stdout")),
            stderr=_mask(_as_text(captured_text("stderr")), redact),
            timed_out=True,
            error=_mask(f"timeout after {timeout}s", redact),
        )
    except BaseException:
        _kill_process_group(child)
        try:
            child.wait(timeout=_REAP_GRACE_SEC)
        except Exception:
            pass
        join_readers(_REAP_GRACE_SEC)
        close_streams()
        raise

    # 直接子进程已退出；后代可能仍握着继承来的 pipe 写端，reader 看不到 EOF。
    join_readers(0.2)

    if any(reader.is_alive() for reader in readers):
        # start_new_session=True 使 child.pid 就是 pgid；组长退出不代表组没了。
        _kill_process_group_id(child.pid)
        join_readers(1.0)

    return ProcResult(
        code=child.returncode,
        stdout=_as_text(captured_text("stdout")),
        stderr=_mask(_as_text(captured_text("stderr")), redact),
    )


def run_curl(argv: Sequence[str], *, timeout: float,
             socks_port: int | None = None,
             http_proxy: str | None = None,
             extra: Sequence[str] = ()) -> ProcResult:
    """Run ``curl`` through a SOCKS or HTTP proxy, never both.

    Argument order is: base flags, proxy flags, caller ``argv``, then ``extra``.
    ``extra`` therefore lands last and can append or override caller options.

    ``--max-time`` carries ``timeout`` so curl gives up on its own. The child
    process gets five extra seconds as a hard stop for the cases curl cannot
    bound itself (stuck DNS resolver, wedged TLS handshake).
    """
    _require_positive_timeout(timeout)
    if socks_port is not None and http_proxy is not None:
        raise ValueError("socks_port and http_proxy are mutually exclusive")

    command = ["curl", "-sS", "--max-time", str(timeout)]
    if socks_port is not None:
        command += ["--proxy", f"socks5h://127.0.0.1:{int(socks_port)}"]
    elif http_proxy is not None:
        command += ["--proxy", http_proxy]
    command += [*argv, *extra]
    return run(command, timeout=timeout + 5)


def run_systemctl(action: str, unit: str, *, timeout: float = 20) -> ProcResult:
    """Run ``systemctl --user <action> <unit>``."""
    return run(["systemctl", "--user", action, unit], timeout=timeout)
