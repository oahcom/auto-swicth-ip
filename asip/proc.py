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
from dataclasses import dataclass
from typing import Mapping, Sequence

from .redact import redact_secrets

# Grace period for reaping after the process group has been signalled. The
# direct child is normally already dead; this only bounds a wedged descendant.
_REAP_GRACE_SEC = 5.0


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


def run(argv: Sequence[str], *, timeout: float, cwd: str | None = None,
        env: Mapping[str, str] | None = None, redact: bool = True) -> ProcResult:
    """Run ``argv`` and return a result instead of raising.

    Both streams are captured as text, which is what
    ``subprocess.run(capture_output=True, text=True)`` does; ``Popen`` is used
    directly only so a timeout can kill the whole process group. ``stdout`` is
    never redacted — callers need the raw payload even when it happens to
    contain a token-shaped string.
    """
    _require_positive_timeout(timeout)
    argv = list(argv)
    argv0 = argv[0] if argv else ""

    try:
        child = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
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
        # Every start failure is reported with the documented prefix; the
        # underlying OSError keeps its detail for diagnosis.
        return ProcResult(
            code=None,
            error=_mask(f"binary not found: {argv0}: {exc}", redact),
        )

    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(child)
        try:
            stdout, stderr = child.communicate(timeout=_REAP_GRACE_SEC)
        except subprocess.TimeoutExpired:
            # A descendant escaped the group and still holds the pipe. Close our
            # ends so the caller cannot block forever, then reap the direct
            # child best-effort.
            for stream in (getattr(child, "stdout", None),
                           getattr(child, "stderr", None)):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            try:
                child.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                pass
            stdout, stderr = "", ""
        return ProcResult(
            code=None,
            stdout=_as_text(stdout),
            stderr=_mask(_as_text(stderr), redact),
            timed_out=True,
            error=_mask(f"timeout after {timeout}s", redact),
        )
    except BaseException:
        # communicate() can fail for reasons other than timeout (closed pipe,
        # broken decoder). Reap the group before propagating so a failing wait
        # does not leak the child; the exception still belongs to the caller.
        _kill_process_group(child)
        try:
            child.communicate(timeout=_REAP_GRACE_SEC)
        except Exception:
            pass
        raise

    return ProcResult(
        code=child.returncode,
        stdout=_as_text(stdout),
        stderr=_mask(_as_text(stderr), redact),
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
