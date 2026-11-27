#!/usr/bin/env python3
"""10-minute synthetic benchmark for the 47k-row WARP IP ledger WAL path.

Writes only under a temporary directory.  The baseline is the equivalent
full-snapshot bytes (rows * snapshot size per event); the WAL path measures
actual append record bytes plus periodic full compactions.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asip import ledger_wal


def synthetic_ledger(rows: int) -> dict:
    base = 1_764_000_000.0
    out = {}
    for i in range(rows):
        ip = "198.51.%d.%d" % ((i // 254) % 256, (i % 254) + 1)
        ts = base + i
        blocked = (i % 11 == 0)
        out[ip] = {
            "ts": ts,
            "blocked": blocked,
            "blocked_kind": "rate429" if blocked else None,
            "blocked_ts": ts if blocked else None,
            "blocked_ts_by_kind": {
                "rate429": ts if blocked else None,
                "model_quota": ts - 60 if i % 17 == 0 else None,
            },
            "ban_until": ts + 86_400 if blocked else None,
            "ban_kind": "rate429" if blocked else None,
            "expires_at": ts + 86_400,
            "first_seen": ts - 600,
            "last_ok_ts": ts - 10,
            "probe_ok_ts": ts - 30,
            "served_sec": float(i % 900),
        }
    return out


def _device_sectors(device: str) -> int | None:
    p = Path("/sys/block") / device / "stat"
    try:
        return int(p.read_text().split()[6])
    except (OSError, ValueError, IndexError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=600)
    ap.add_argument("--rate", type=float, default=4.0)
    ap.add_argument("--rows", type=int, default=47_665)
    ap.add_argument("--compact-records", type=int, default=1024)
    ap.add_argument("--compact-bytes", type=int, default=4 * 1024 * 1024)
    ap.add_argument("--baseline-snapshot-bytes", type=int, default=16_912_942,
                    help="observed live JSON snapshot size for full-rewrite baseline")
    ap.add_argument("--block-device", default="sdd")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="asip-ledger-wal-bench-") as td:
        snapshot = Path(td) / ".warp_ip_used.json"
        mapping = synthetic_ledger(args.rows)
        keys = list(mapping)
        initial = ledger_wal.compact(snapshot, mapping)
        snapshot_bytes = initial["snapshot_bytes"]
        device_before = _device_sectors(args.block_device)
        wal_record_bytes = 0
        compaction_bytes = 0
        compactions = 0
        pending_records = 0
        append_ms = []
        compact_ms = []
        events = max(1, int(args.seconds * args.rate))
        start = time.monotonic()

        for i in range(events):
            key = keys[i % len(keys)]
            entry = dict(mapping[key])
            entry["ts"] = 1_764_100_000.0 + i
            entry["last_ok_ts"] = entry["ts"]
            entry["served_sec"] = float((i * 13) % 3600)
            mapping[key] = entry

            t0 = time.monotonic()
            out = ledger_wal.append_delta(snapshot, upserts={key: entry})
            append_ms.append((time.monotonic() - t0) * 1000)
            wal_record_bytes += int(out["record_bytes"])
            pending_records += 1

            if (pending_records >= args.compact_records
                    or int(out["wal_bytes"]) >= args.compact_bytes):
                t0 = time.monotonic()
                compacted = ledger_wal.compact(snapshot, mapping)
                compact_ms.append((time.monotonic() - t0) * 1000)
                compaction_bytes += int(compacted["snapshot_bytes"])
                compactions += 1
                pending_records = 0

            target = start + (i + 1) / args.rate
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)

        elapsed = time.monotonic() - start
        # Verify crash/restart replay before final rollback projection.
        replayed = ledger_wal.load(snapshot)
        if replayed["used"] != mapping:
            raise RuntimeError("replayed state differs from in-memory expected state")
        pending_bytes = int(replayed["wal_bytes"])
        pending_count = int(replayed["records"])
        before_final_compact = ledger_wal.inspect(snapshot)
        final = ledger_wal.compact_from_disk(snapshot)
        final_snapshot = ledger_wal.load(snapshot)
        if final_snapshot["used"] != mapping or final_snapshot["wal_bytes"] != 0:
            raise RuntimeError("final compact projection mismatch")
        device_after = _device_sectors(args.block_device)

        baseline_bytes = args.baseline_snapshot_bytes * events
        wal_logical_bytes = wal_record_bytes + compaction_bytes
        final_compaction_bytes = int(final["snapshot_bytes"])
        total_with_final_compact = wal_logical_bytes + final_compaction_bytes
        reduction = 1.0 - (total_with_final_compact / baseline_bytes)
        report = {
            "duration_sec": round(elapsed, 2),
            "events": events,
            "event_rate_per_sec": round(events / elapsed, 3),
            "rows": args.rows,
            "initial_snapshot_bytes": snapshot_bytes,
            "baseline_snapshot_bytes_per_event": args.baseline_snapshot_bytes,
            "baseline_full_snapshot_bytes": baseline_bytes,
            "wal_record_bytes": wal_record_bytes,
            "periodic_compactions": compactions,
            "periodic_compaction_bytes": compaction_bytes,
            "pending_records_before_final_compact": pending_count,
            "pending_wal_bytes_before_final_compact": pending_bytes,
            "final_compaction_bytes": final_compaction_bytes,
            "total_wal_path_logical_bytes": total_with_final_compact,
            "logical_write_reduction_pct": round(reduction * 100, 4),
            "append_latency_ms_p50": round(statistics.median(append_ms), 3),
            "append_latency_ms_p95": round(sorted(append_ms)[int(len(append_ms)*0.95)], 3),
            "compact_latency_ms_p50": round(statistics.median(compact_ms), 3) if compact_ms else 0,
            "compact_latency_ms_p95": round(sorted(compact_ms)[int(len(compact_ms)*0.95)], 3) if compact_ms else 0,
            "snapshot_after_final_compact_bytes": final_snapshot["wal_bytes"] + os.path.getsize(snapshot),
            "block_device": args.block_device,
            "host_block_device_total_sectors_written_delta_512b_including_other_workloads": (
                device_after - device_before
                if device_before is not None and device_after is not None else None),
            "replay_verified": True,
            "rollback_projection_verified": True,
            "temporary_state_removed": True,
        }
    report["temporary_state_removed"] = not os.path.exists(td)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
