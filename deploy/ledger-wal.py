#!/usr/bin/env python3
"""Inspect/compact the WARP IP ledger WAL without importing daemon side effects.

Before rolling code back to a revision that predates WAL support, stop
`auto-switch-ip.service`, run `compact`, verify `status` reports wal_bytes=0,
then perform the rollback.  A legacy daemon reads only the JSON snapshot.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "asip_ledger_wal", ROOT / "asip" / "ledger_wal.py")
if _spec is None or _spec.loader is None:
    raise RuntimeError("cannot load asip/ledger_wal.py")
ledger_wal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger_wal)


def _daemon_lock_held() -> bool:
    lock = ROOT / ".daemon.lock"
    if not lock.exists():
        return False
    fd = os.open(lock, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("status", "compact"))
    ap.add_argument("--snapshot", default=str(ROOT / ".warp_ip_used.json"))
    args = ap.parse_args(argv)
    try:
        if args.command == "status":
            out = ledger_wal.inspect(args.snapshot)
        else:
            active = subprocess.run(["systemctl", "is-active", "--quiet",
                                     "auto-switch-ip.service"],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL).returncode == 0
            if active or _daemon_lock_held():
                raise RuntimeError(
                    "daemon writer is active; stop it before rollback compaction")
            out = ledger_wal.compact_from_disk(args.snapshot)
        print(json.dumps(out, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print("ledger WAL operation failed: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
