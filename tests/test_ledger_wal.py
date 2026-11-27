from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from asip import ledger_wal


def _snapshot(path, used):
    path.write_text(json.dumps({"used": used}, separators=(",", ":")))
    path.chmod(0o600)


def test_load_replays_upserts_and_deletes(tmp_path):
    p = tmp_path / ".warp_ip_used.json"
    _snapshot(p, {"burned": {"blocked": True}, "old": {"ts": 1}})
    ledger_wal.append_delta(p, upserts={"new": {"ts": 2}}, deletes=["old"])
    loaded = ledger_wal.load(p)
    assert loaded["used"] == {"burned": {"blocked": True}, "new": {"ts": 2}}
    assert loaded["records"] == 1


def test_partial_final_record_is_truncated_but_prior_records_survive(tmp_path):
    p = tmp_path / ".warp_ip_used.json"
    _snapshot(p, {"base": {"ts": 1}})
    ledger_wal.append_delta(p, upserts={"durable": {"blocked": True}})
    wal = tmp_path / (p.name + ".wal")
    with wal.open("ab") as f:
        f.write(b'{"v":1,"upserts":{"torn":')
        f.flush()
        os.fsync(f.fileno())
    loaded = ledger_wal.load(p)
    assert loaded["used"] == {"base": {"ts": 1}, "durable": {"blocked": True}}
    assert loaded["partial_tail_repaired"] is True
    assert wal.read_bytes().endswith(b"\n")


def test_complete_corrupt_record_fails_closed(tmp_path):
    p = tmp_path / ".warp_ip_used.json"
    _snapshot(p, {"burned": {"blocked": True}})
    wal = tmp_path / (p.name + ".wal")
    wal.write_bytes(b"not-json\n")
    with pytest.raises(ledger_wal.LedgerWalError, match="corrupt complete"):
        ledger_wal.load(p)
    assert wal.read_bytes() == b"not-json\n"


def test_compaction_crash_before_snapshot_replace_keeps_wal_recoverable(
        tmp_path, monkeypatch):
    p = tmp_path / ".warp_ip_used.json"
    _snapshot(p, {"base": {"ts": 1}})
    ledger_wal.append_delta(p, upserts={"burned": {"blocked": True}})
    original = ledger_wal._write_snapshot_unlocked

    def fail(*args, **kwargs):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(ledger_wal, "_write_snapshot_unlocked", fail)
    with pytest.raises(OSError):
        ledger_wal.compact(p, {"base": {"ts": 1}, "burned": {"blocked": True}})
    monkeypatch.setattr(ledger_wal, "_write_snapshot_unlocked", original)
    assert ledger_wal.load(p)["used"]["burned"] == {"blocked": True}


def test_compaction_crash_after_snapshot_replace_before_wal_truncate_is_idempotent(
        tmp_path, monkeypatch):
    p = tmp_path / ".warp_ip_used.json"
    _snapshot(p, {"base": {"ts": 1}})
    ledger_wal.append_delta(p, upserts={"burned": {"blocked": True}},
                            deletes=["base"])
    original = ledger_wal._truncate_wal_unlocked

    def fail(*args, **kwargs):
        raise OSError("simulated crash before WAL truncate")

    monkeypatch.setattr(ledger_wal, "_truncate_wal_unlocked", fail)
    with pytest.raises(OSError):
        ledger_wal.compact(p, {"burned": {"blocked": True}})
    monkeypatch.setattr(ledger_wal, "_truncate_wal_unlocked", original)
    loaded = ledger_wal.load(p)
    assert loaded["used"] == {"burned": {"blocked": True}}
    assert loaded["records"] == 1


def test_compact_from_disk_creates_old_code_compatible_snapshot(tmp_path):
    p = tmp_path / ".warp_ip_used.json"
    _snapshot(p, {"base": {"ts": 1}})
    ledger_wal.append_delta(p, upserts={"burned": {"blocked": True}},
                            deletes=["base"])
    result = ledger_wal.compact_from_disk(p)
    assert result["rows"] == 1
    assert json.loads(p.read_text()) == {"used": {"burned": {"blocked": True}}}
    assert (tmp_path / (p.name + ".wal")).stat().st_size == 0
    assert ledger_wal.load(p)["used"] == {"burned": {"blocked": True}}


def test_concurrent_appends_are_complete_and_replayable(tmp_path):
    p = tmp_path / ".warp_ip_used.json"
    _snapshot(p, {})

    def append(i):
        return ledger_wal.append_delta(p, upserts={"ip-%04d" % i: {"ts": i}})

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(append, range(240)))
    loaded = ledger_wal.load(p)
    assert len(loaded["used"]) == 240
    assert loaded["records"] == 240


def test_files_are_private_and_inspect_is_read_only(tmp_path):
    p = tmp_path / ".warp_ip_used.json"
    _snapshot(p, {})
    ledger_wal.append_delta(p, upserts={"secret-ip": {"blocked": True}})
    wal = tmp_path / (p.name + ".wal")
    lock = tmp_path / (p.name + ".wal.lock")
    assert wal.stat().st_mode & 0o777 == 0o600
    assert lock.stat().st_mode & 0o777 == 0o600
    before = (wal.stat().st_ino, wal.stat().st_mtime_ns, wal.stat().st_size)
    stats = ledger_wal.inspect(p)
    after = (wal.stat().st_ino, wal.stat().st_mtime_ns, wal.stat().st_size)
    assert stats["wal_records"] == 1
    assert stats["total_bytes"] == stats["snapshot_bytes"] + stats["wal_bytes"]
    assert before == after


def test_append_fsync_failure_is_reported_not_acknowledged(tmp_path, monkeypatch):
    p = tmp_path / ".warp_ip_used.json"
    _snapshot(p, {})
    monkeypatch.setattr(ledger_wal.os, "fsync",
                        lambda fd: (_ for _ in ()).throw(OSError("fsync failed")))
    with pytest.raises(OSError, match="fsync failed"):
        ledger_wal.append_delta(p, upserts={"burned": {"blocked": True}})


def test_missing_snapshot_with_nonempty_wal_fails_closed(tmp_path):
    p = tmp_path / ".warp_ip_used.json"
    wal = tmp_path / (p.name + ".wal")
    wal.write_text('{"v":1,"ts":1,"upserts":{"burned":{"blocked":true}},"deletes":[]}\n')
    with pytest.raises(ledger_wal.LedgerWalError, match="snapshot is missing"):
        ledger_wal.load(p)
    with pytest.raises(ledger_wal.LedgerWalError, match="snapshot is missing"):
        ledger_wal.compact_from_disk(p)
    with pytest.raises(ledger_wal.LedgerWalError, match="snapshot missing"):
        ledger_wal.append_delta(p, upserts={"new": {"blocked": True}})


def test_first_append_establishes_durable_empty_snapshot_base(tmp_path):
    p = tmp_path / ".warp_ip_used.json"
    assert not p.exists()
    ledger_wal.append_delta(p, upserts={"burned": {"blocked": True}})
    assert json.loads(p.read_text()) == {"used": {}}
    assert ledger_wal.load(p)["used"] == {"burned": {"blocked": True}}
