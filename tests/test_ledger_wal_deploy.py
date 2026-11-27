from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_state_projection_refuses_pending_ledger_wal(tmp_path):
    root = Path(__file__).resolve().parents[1]
    state = tmp_path / "state"
    state.mkdir()
    (state / ".warp_ip_used.json").write_text('{"used":{}}')
    (state / ".warp_ip_used.json.wal").write_text(
        '{"v":1,"ts":1,"upserts":{"burned":{"blocked":true}},"deletes":[]}\n')
    out = tmp_path / "projection.json"
    proc = subprocess.run(
        [sys.executable, str(root / "deploy/state-projection.py"), "export",
         "--state-root", str(state), "--out", str(out)],
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert "WAL" in proc.stderr
    assert not out.exists()


def _fake_systemctl(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    path = fake / "systemctl"
    path.write_text("#!/bin/sh\nexit 3\n")
    path.chmod(0o755)
    return str(fake)


def test_state_projection_refuses_when_manual_daemon_lock_is_held(tmp_path):
    import fcntl
    import os
    import shutil

    source = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    (project / "deploy").mkdir(parents=True)
    shutil.copy2(source / "deploy/state-projection.py",
                 project / "deploy/state-projection.py")
    state = tmp_path / "state"
    state.mkdir()
    (state / ".warp_ip_used.json").write_text('{"used":{}}')
    (state / ".warp_ip_used.json.wal.lock").write_text("")
    project_lock = project / ".daemon.lock"
    fd = os.open(project_lock, os.O_RDWR | os.O_CREAT, 0o600)
    out = tmp_path / "projection.json"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env = dict(os.environ, PATH=_fake_systemctl(tmp_path))
        proc = subprocess.run(
            [sys.executable, str(project / "deploy/state-projection.py"), "export",
             "--state-root", str(state), "--out", str(out)],
            capture_output=True, text=True, env=env)
        assert proc.returncode == 1
        assert "WAL" in proc.stderr
        assert not out.exists()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_ledger_wal_cli_compact_refuses_manual_daemon_lock(tmp_path):
    import fcntl
    import os
    import shutil

    source = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    (project / "deploy").mkdir(parents=True)
    (project / "asip").mkdir()
    shutil.copy2(source / "deploy/ledger-wal.py", project / "deploy/ledger-wal.py")
    shutil.copy2(source / "asip/ledger_wal.py", project / "asip/ledger_wal.py")
    snapshot = tmp_path / ".warp_ip_used.json"
    snapshot.write_text('{"used":{"old":{}}}')
    (tmp_path / ".warp_ip_used.json.wal").write_text(
        '{"v":1,"ts":1,"upserts":{"burned":{"blocked":true}},"deletes":[]}\n')
    project_lock = project / ".daemon.lock"
    fd = os.open(project_lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env = dict(os.environ, PATH=_fake_systemctl(tmp_path))
        proc = subprocess.run(
            [sys.executable, str(project / "deploy/ledger-wal.py"), "compact",
             "--snapshot", str(snapshot)],
            capture_output=True, text=True, env=env)
        assert proc.returncode == 1
        assert "daemon writer is active" in proc.stderr
        assert (tmp_path / ".warp_ip_used.json.wal").stat().st_size > 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
