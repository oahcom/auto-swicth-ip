"""Run child processes without leaving them unbounded or shell-parsed.

Every call requires an explicit ``timeout``. This project drives a daemon and
several operator CLIs that shell out to ``curl`` and ``systemctl``; before this
helper one hung ``curl`` could stall the rotation loop indefinitely (defect
D7). A mandatory timeout turns that failure into a ``ProcResult`` with
``timed_out=True`` instead of a process the caller can never reap.

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
from dataclasses import dataclass
from typing import Iterable, Sequence

from .redact import redact_secrets


@dataclass(frozen=True)
class ProcResult:
    """Outcome of one child process.

    ``error`` is set only when the process could not be started at all; a
    non-zero exit is reported through ``code``.
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


def run(argv: Sequence[str], *, timeout: float, cwd: str | None = None,
        env: dict[str, str] | None = None, redact: bool = True) -> ProcResult:
    """Run ``argv`` and return a result instead of raising.

    ``stdout`` is never redacted — callers need the raw payload even when it
    happens to contain a token-shaped string.
    """
    _require_positive_timeout(timeout)
    try:
        child = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        error = f"binary not found: {argv[0]}"
        return ProcResult(
            code=None,
            error=redact_secrets(error) if redact else error,
        )
    except OSError as exc:
        error = str(exc)
        return ProcResult(
            code=None,
            error=redact_secrets(error) if redact else error,
        )

    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # `subprocess.run(timeout=...)` only kills the direct child.  curl and
        # bun can spawn helpers; leaving them alive keeps the port/handshake
        # wedged and leaks processes across retries.  The child is a session
        # leader (start_new_session), so killpg reaps the whole tree.
        try:
            os.killpg(os.getpgid(child.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            child.kill()
        stdout, stderr = child.communicate()
        stderr = _as_text(stderr)
        return ProcResult(
            code=None,
            stdout=_as_text(stdout),
            stderr=redact_secrets(stderr) if redact else stderr,
            timed_out=True,
        )

    stderr = stderr or ""
    return ProcResult(
        code=child.returncode,
        stdout=stdout or "",
        stderr=redact_secrets(stderr) if redact else stderr,
    )


def run_curl(args: Sequence[str], *, timeout: float,
             socks_port: int | None = None, http_proxy: str | None = None,
             extra: Iterable[str] = (), redact: bool = True) -> ProcResult:
    """Run ``curl`` through a SOCKS or HTTP proxy, never both.

    ``--max-time`` carries ``timeout`` so curl gives up on its own. The child
    process gets five extra seconds as a hard stop for the cases curl cannot
    bound itself (stuck DNS resolver, wedged TLS handshake).
    """
    _require_positive_timeout(timeout)
    if socks_port is not None and http_proxy is not None:
        raise ValueError("socks_port and http_proxy are mutually exclusive")

    argv = ["curl", "-sS", "--max-time", str(timeout)]
    if socks_port is not None:
        argv += ["--proxy", f"socks5h://127.0.0.1:{int(socks_port)}"]
    elif http_proxy is not None:
        argv += ["--proxy", http_proxy]
    argv += [*args, *extra]
    return run(argv, timeout=timeout + 5, redact=redact)


def run_systemctl(action: str, unit: str, *, timeout: float = 20,
                  redact: bool = True) -> ProcResult:
    """Run ``systemctl --user <action> <unit>``."""
    return run(["systemctl", "--user", action, unit], timeout=timeout,
               redact=redact)
