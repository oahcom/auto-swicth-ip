"""Crash-safe append-only WAL for the WARP IP-use ledger.

The JSON snapshot remains the compatibility projection.  Every acknowledged
mutation is first appended as one fsynced JSONL delta; startup replays deltas
in order.  Compaction atomically replaces the snapshot *before* truncating
WAL, so a crash at either boundary is recoverable and replay is idempotent.

The WAL and compactor share an advisory flock.  The daemon's in-process
_WARP_IP_LOCK still protects its memory map; this file lock also serializes
compaction with any maintenance/rollback process.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time

WAL_VERSION = 1
DEFAULT_COMPACT_RECORDS = 1024
DEFAULT_COMPACT_BYTES = 4 * 1024 * 1024


class LedgerWalError(RuntimeError):
    """The ledger snapshot/WAL cannot be replayed without risking data loss."""


def wal_path(snapshot_path: str | os.PathLike) -> str:
    return os.fspath(snapshot_path) + ".wal"


def lock_path(snapshot_path: str | os.PathLike) -> str:
    return wal_path(snapshot_path) + ".lock"


@contextlib.contextmanager
def _locked(snapshot_path: str | os.PathLike):
    path = lock_path(snapshot_path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(fd).st_mode & 0o777 != 0o600:
            os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_snapshot(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerWalError("ledger snapshot is unreadable: %s" % exc) from exc
    if not isinstance(data, dict):
        raise LedgerWalError("ledger snapshot root is not an object")
    used = data.get("used", data)
    if not isinstance(used, dict):
        raise LedgerWalError("ledger snapshot 'used' is not an object")
    return {str(k): v for k, v in used.items()}


def _apply_record(mapping: dict, record: dict, line_no: int) -> None:
    if not isinstance(record, dict) or record.get("v") != WAL_VERSION:
        raise LedgerWalError("unsupported WAL record at line %d" % line_no)
    upserts = record.get("upserts", {})
    deletes = record.get("deletes", [])
    if not isinstance(upserts, dict) or not isinstance(deletes, list):
        raise LedgerWalError("malformed WAL delta at line %d" % line_no)
    delete_keys = []
    for key in deletes:
        if not isinstance(key, str) or not key:
            raise LedgerWalError("invalid delete key at WAL line %d" % line_no)
        delete_keys.append(key)
    if set(delete_keys) & set(upserts):
        raise LedgerWalError("same key upserted and deleted at WAL line %d" % line_no)
    for key, value in upserts.items():
        if not isinstance(key, str) or not key or not isinstance(value, (dict, int, float)):
            raise LedgerWalError("invalid upsert at WAL line %d" % line_no)
    for key in delete_keys:
        mapping.pop(key, None)
    for key, value in upserts.items():
        mapping[key] = value


def _replay_unlocked(snapshot_path: str, *, repair_partial: bool) -> tuple[dict, int, int, bool]:
    path = wal_path(snapshot_path)
    snapshot_exists = os.path.exists(snapshot_path)
    try:
        wal_size = os.path.getsize(path)
    except FileNotFoundError:
        wal_size = 0
    if not snapshot_exists and wal_size:
        raise LedgerWalError(
            "ledger snapshot is missing while delta WAL is non-empty; refusing partial replay")
    mapping = _read_snapshot(snapshot_path)
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return mapping, 0, 0, False
    records = 0
    valid_bytes = 0
    partial = False
    with f:
        line_no = 0
        while True:
            start = f.tell()
            line = f.readline()
            if not line:
                break
            line_no += 1
            if not line.endswith(b"\n"):
                # Only a torn final append is recoverable.  A malformed complete
                # line is corruption and must stop startup rather than drop bans.
                partial = True
                valid_bytes = start
                break
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LedgerWalError("corrupt complete WAL line %d: %s" %
                                      (line_no, exc)) from exc
            _apply_record(mapping, record, line_no)
            records += 1
            valid_bytes = f.tell()
    if partial and repair_partial:
        _truncate_wal_unlocked(path, valid_bytes)
    return mapping, records, valid_bytes, partial


def _fsync_dir(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _truncate_wal_unlocked(path: str, size: int = 0) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_mode & 0o777 != 0o600:
            os.fchmod(fd, 0o600)
        os.ftruncate(fd, size)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_snapshot_unlocked(snapshot_path: str, mapping: dict) -> int:
    directory = os.path.dirname(os.path.abspath(snapshot_path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(snapshot_path) + ".",
                               suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump({"used": mapping}, f, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, snapshot_path)
        # WAL is not truncated until the rename's directory entry is durable.
        _fsync_dir(snapshot_path)
        return os.path.getsize(snapshot_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(snapshot_path: str | os.PathLike) -> dict:
    """Load snapshot + replay WAL, repairing only a torn final record.

    Complete malformed records raise LedgerWalError.  Callers must fail closed;
    silently starting from the older snapshot could make a burned IP eligible.
    """
    snapshot_path = os.fspath(snapshot_path)
    with _locked(snapshot_path):
        mapping, records, valid_bytes, partial = _replay_unlocked(
            snapshot_path, repair_partial=True)
        wal = wal_path(snapshot_path)
        try:
            wal_bytes = os.path.getsize(wal)
        except OSError:
            wal_bytes = 0
    return {"used": mapping, "records": records,
            "wal_bytes": wal_bytes, "partial_tail_repaired": partial,
            "valid_wal_bytes": valid_bytes}


def append_delta(snapshot_path: str | os.PathLike, *, upserts: dict | None = None,
                 deletes=(), ts: float | None = None) -> dict:
    """Append one durable transaction (upserts + deletes) and fsync before return."""
    snapshot_path = os.fspath(snapshot_path)
    upserts = {str(k): v for k, v in (upserts or {}).items()}
    deletes = sorted({str(k) for k in (deletes or ())})
    if set(upserts) & set(deletes):
        raise LedgerWalError("delta cannot upsert and delete the same key")
    if not upserts and not deletes:
        return {"written": False, "wal_bytes": 0, "record_bytes": 0}
    record = {"v": WAL_VERSION, "ts": time.time() if ts is None else float(ts),
              "upserts": upserts, "deletes": deletes}
    data = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    path = wal_path(snapshot_path)
    created = False
    with _locked(snapshot_path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if not os.path.exists(snapshot_path):
            try:
                existing_wal_bytes = os.path.getsize(path)
            except OSError:
                existing_wal_bytes = 0
            if existing_wal_bytes:
                raise LedgerWalError(
                    "cannot append: ledger snapshot missing while WAL has pending deltas")
            # Fresh install: establish an empty durable base snapshot before the
            # first delta, so a later missing snapshot can never look like a valid base.
            _write_snapshot_unlocked(snapshot_path, {})
        created = not os.path.exists(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o777 != 0o600:
                os.fchmod(fd, 0o600)
            size = os.fstat(fd).st_size
            if size:
                tail = os.pread(fd, 1, size - 1)
                if tail != b"\n":
                    raise LedgerWalError("WAL has a torn tail; reload/replay before append")
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("short WAL append")
                view = view[n:]
            os.fsync(fd)
            wal_bytes = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if created:
            _fsync_dir(path)
    return {"written": True, "wal_bytes": wal_bytes,
            "record_bytes": len(data)}


def compact(snapshot_path: str | os.PathLike, mapping: dict) -> dict:
    """Compact an authoritative in-memory map to snapshot, then truncate WAL.

    Caller must hold the daemon's memory lock while taking/passing the map.
    The file lock prevents an append from crossing the snapshot/truncate boundary.
    """
    snapshot_path = os.fspath(snapshot_path)
    if not isinstance(mapping, dict):
        raise LedgerWalError("compaction mapping must be a dict")
    with _locked(snapshot_path):
        size = _write_snapshot_unlocked(snapshot_path, mapping)
        path = wal_path(snapshot_path)
        _truncate_wal_unlocked(path, 0)
        _fsync_dir(path)
        return {"compacted": True, "rows": len(mapping),
                "snapshot_bytes": size, "wal_bytes": 0}


def compact_from_disk(snapshot_path: str | os.PathLike) -> dict:
    """Replay snapshot + WAL under lock and compact; used by rollback/deploy tools."""
    snapshot_path = os.fspath(snapshot_path)
    with _locked(snapshot_path):
        mapping, records, _valid_bytes, partial = _replay_unlocked(
            snapshot_path, repair_partial=True)
        size = _write_snapshot_unlocked(snapshot_path, mapping)
        path = wal_path(snapshot_path)
        _truncate_wal_unlocked(path, 0)
        _fsync_dir(path)
        return {"compacted": True, "rows": len(mapping),
                "records_replayed": records, "snapshot_bytes": size,
                "wal_bytes": 0, "partial_tail_repaired": partial}


def inspect(snapshot_path: str | os.PathLike) -> dict:
    """Read-only size/record counters; does not repair or compact."""
    snapshot_path = os.fspath(snapshot_path)
    path = wal_path(snapshot_path)
    try:
        snapshot_bytes = os.path.getsize(snapshot_path)
    except FileNotFoundError:
        snapshot_bytes = 0
    try:
        wal_bytes = os.path.getsize(path)
    except FileNotFoundError:
        wal_bytes = 0
    records = 0
    if wal_bytes:
        with open(path, "rb") as f:
            for line in f:
                if line.endswith(b"\n"):
                    records += 1
    return {"snapshot_bytes": snapshot_bytes, "wal_bytes": wal_bytes,
            "total_bytes": snapshot_bytes + wal_bytes,
            "wal_records": records}
