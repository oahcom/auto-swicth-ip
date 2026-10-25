"""Atomic JSON/text file writes.

Every ledger in this project is a "what has already been burned" record. A
half-written file is worse than no file: the next start treats burned addresses
as clean and routes back into them. The old helper used a fixed ``<path>.tmp``
name, so two threads writing the same ledger interleaved into one file.

This implementation gives each writer its own temporary name, fsyncs before the
rename (so a power loss cannot leave a renamed-but-empty file), and removes the
temporary file on failure.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any


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
