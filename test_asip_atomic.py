"""asip.atomic_io 的确定性单测（无网络、无外部服务）。"""
import json
import os
import threading

import pytest

from asip.atomic_io import append_line, read_json, update_json, write_json


def _read_lines(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()


def test_append_line_writes_parseable_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    append_line(str(path), {"a": 1})
    append_line(str(path), {"b": "中文"})
    lines = _read_lines(path)
    assert len(lines) == 2
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"b": "中文"}]
    assert ": " not in lines[0]  # compact separators


def test_append_line_creates_private_file(tmp_path):
    path = tmp_path / "log.jsonl"
    append_line(str(path), {"a": 1})
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_append_line_tightens_existing_permissions(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o644)
    assert os.stat(path).st_mode & 0o777 == 0o644
    append_line(str(path), {"a": 1})
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_append_line_is_not_interleaved_under_concurrency(tmp_path):
    path = tmp_path / "log.jsonl"
    threads = 8
    per_thread = 50
    barrier = threading.Barrier(threads)
    errors = []

    def writer(tid):
        try:
            barrier.wait()
            for seq in range(per_thread):
                append_line(str(path), {"tid": tid, "seq": seq, "pad": "x" * 64})
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    workers = [threading.Thread(target=writer, args=(i,)) for i in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert errors == []
    lines = _read_lines(path)
    assert len(lines) == threads * per_thread
    seen = set()
    for line in lines:
        obj = json.loads(line)
        assert isinstance(obj["tid"], int) and isinstance(obj["seq"], int)
        seen.add((obj["tid"], obj["seq"]))
    assert len(seen) == threads * per_thread


def test_update_json_returns_merged_value(tmp_path):
    path = tmp_path / "state.json"
    write_json(str(path), {"n": 1})
    result = update_json(str(path), lambda old: {"n": old["n"] + 1})
    assert result == {"n": 2}
    assert read_json(str(path)) == {"n": 2}


def test_update_json_uses_default_for_missing_file(tmp_path):
    path = tmp_path / "missing.json"
    result = update_json(str(path), lambda old: {"n": old["n"] + 1},
                         default={"n": 41})
    assert result == {"n": 42}
    assert read_json(str(path)) == {"n": 42}


def test_update_json_uses_default_for_corrupt_file(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    result = update_json(str(path), lambda old: {"ok": True}, default={})
    assert result == {"ok": True}
    assert read_json(str(path)) == {"ok": True}


def test_update_json_merge_error_leaves_file_unchanged(tmp_path):
    path = tmp_path / "state.json"
    write_json(str(path), {"n": 1})

    def boom(old):
        raise ValueError("merge failed")

    with pytest.raises(ValueError, match="merge failed"):
        update_json(str(path), boom)
    assert read_json(str(path)) == {"n": 1}


def test_update_json_with_lock_path_creates_lock_and_returns(tmp_path):
    path = tmp_path / "state.json"
    lock_path = tmp_path / "state.lock"
    result = update_json(str(path), lambda old: [1, 2], default=[],
                         lock_path=str(lock_path))
    assert result == [1, 2]
    assert lock_path.exists()
    assert read_json(str(path)) == [1, 2]


def test_write_read_json_old_behavior_unchanged(tmp_path):
    path = tmp_path / "state.json"
    write_json(str(path), {"a": 1})
    assert read_json(str(path)) == {"a": 1}
    assert read_json(str(tmp_path / "absent.json"), "fallback") == "fallback"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("state.json.*.tmp"))
