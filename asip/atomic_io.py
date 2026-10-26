"""Atomic JSON/text file writes and lock-guarded read-modify-write.

Every ledger in this project is a "what has already been burned" record. A
half-written file is worse than no file: the next start treats burned addresses
as clean and routes back into them. The old helper used a fixed ``<path>.tmp``
name, so two threads writing the same ledger interleaved into one file.

This implementation gives each writer its own temporary name, fsyncs before the
rename (so a power loss cannot leave a renamed-but-empty file), and removes the
temporary file on failure.

WHY the extras here:

- ``append_line`` uses ``O_APPEND`` and one assembled byte string per call.
  Append-mode writes are positioned by the kernel at the file end, so several
  processes appending to the same JSONL file cannot overwrite each other's
  lines; the ``chr(10)`` terminator is part of that single byte string, which
  keeps a line from being split across two writers. A ``write`` may still be
  short, so the remainder is written in a loop rather than silently dropped.
- ``update_json`` takes an advisory ``flock`` because a plain read + write pair
  is not atomic: two writers that both read the same old value will each write
  a new value derived from it and the last one silently erases the other. The
  lock is held across the whole read-modify-write so the merge function always
  sees the result of the previous merge.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from typing import Any, Callable


def _atomic_write(path: str, text: str, mode: int) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    prefix = os.path.basename(path) + "."
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: str, payload: Any, *, mode: int = 0o600,
               indent: int | None = None) -> None:
    """Serialize ``payload`` and replace ``path`` atomically."""
    text = json.dumps(payload, ensure_ascii=False, indent=indent,
                      separators=None if indent else (",", ":"))
    _atomic_write(path, text, mode)


def write_text(path: str, text: str, *, mode: int = 0o600) -> None:
    """Replace ``path`` with ``text`` atomically."""
    _atomic_write(path, text, mode)


def read_json(path: str, default: Any = None) -> Any:
    """Read JSON, returning ``default`` for a missing or corrupt file.

    A corrupt ledger is a signal, not a crash: callers log and keep running.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return default


def append_line(path: str, obj: Any, *, mode: int = 0o600,
                fsync: bool = False) -> None:
    """Append ``obj`` to ``path`` as one compact JSON line.

    The line is serialized in full before opening the file, so the bytes handed
    to ``os.write`` always form a complete record. Append mode means concurrent
    writers add at the end instead of racing on an offset.
    """
    data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n") \
        .encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        # ``O_CREAT`` only applies ``mode`` to a new file; an existing ledger
        # keeps its old permissions unless chmod'ed. Ledgers carry addresses
        # and tokens, so tighten the existing file too.
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)


def update_json(path: str, merge: Callable[[Any], Any], *, default: Any = None,
                mode: int = 0o600, lock_path: str | None = None) -> Any:
    """Read ``path``, apply ``merge``, atomically write and return the result.

    With ``lock_path`` set, an exclusive ``flock`` serializes concurrent
    read-modify-write cycles. If ``merge`` raises, the file is left untouched
    and the exception propagates.
    """
    if lock_path:
        lock_file = open(lock_path, "a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            old = read_json(path, default)
            new = merge(old)
            write_json(path, new, mode=mode)
            return new
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()

    old = read_json(path, default)
    new = merge(old)
    write_json(path, new, mode=mode)
    return new
