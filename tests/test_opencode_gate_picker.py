import threading
import time

import asip.opencode_gate as gate


_PICKER_THREAD_NAME = "opencode-gate-picker"


def _inflight():
    with gate._PICKER_INFLIGHT_LOCK:
        return gate._PICKER_INFLIGHT


def _picker_threads():
    return [
        t for t in threading.enumerate()
        if t.name == _PICKER_THREAD_NAME and t.is_alive()
    ]


def _wait_for_picker_exit(timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _picker_threads() and _inflight() == 0:
            return True
        time.sleep(0.01)
    return not _picker_threads() and _inflight() == 0


def _assert_clean_start():
    assert _wait_for_picker_exit(), "前一个测试遗留了 picker worker"


def test_picker_returns_value(monkeypatch):
    _assert_clean_start()
    monkeypatch.setattr(gate, "_PICKER_TIMEOUT_SEC", 0.2)

    expected = object()

    def picker(cur, pool, cooled):
        return expected

    value, ok = gate._pick_with_timeout(
        picker, "cur", ["a", "b"], set()
    )

    assert ok is True
    assert value is expected
    assert _inflight() == 0
    assert not _picker_threads()


def test_picker_exception_returns_failure(monkeypatch):
    _assert_clean_start()
    monkeypatch.setattr(gate, "_PICKER_TIMEOUT_SEC", 0.2)

    def picker(cur, pool, cooled):
        raise RuntimeError("picker failed")

    value, ok = gate._pick_with_timeout(
        picker, "cur", ["a", "b"], set()
    )

    assert (value, ok) == (None, False)
    assert _inflight() == 0
    assert not _picker_threads()


def test_picker_timeout_does_not_grow_worker_threads(monkeypatch):
    _assert_clean_start()
    monkeypatch.setattr(gate, "_PICKER_TIMEOUT_SEC", 0.2)

    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def picker(cur, pool, cooled):
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        release.wait(timeout=10)
        return "late-result"

    try:
        value, ok = gate._pick_with_timeout(
            picker, "cur", ["a"], set()
        )

        assert started.is_set()
        assert (value, ok) == (None, False)
        assert _inflight() == 1
        assert len(_picker_threads()) == 1

        # worker 仍卡住；重复调用必须立即回退，不得再创建线程。
        for _ in range(5):
            before = time.monotonic()
            value, ok = gate._pick_with_timeout(
                picker, "cur", ["a"], set()
            )
            elapsed = time.monotonic() - before

            assert (value, ok) == (None, False)
            assert elapsed < 0.1
            assert len(_picker_threads()) == 1
            assert _inflight() == 1

        with calls_lock:
            assert calls == 1
    finally:
        # 释放测试 worker，避免把常驻 daemon 线程带入后续用例。
        release.set()
        assert _wait_for_picker_exit()


def test_existing_inflight_picker_returns_immediately(monkeypatch):
    _assert_clean_start()
    monkeypatch.setattr(gate, "_PICKER_TIMEOUT_SEC", 0.2)

    started = threading.Event()
    release = threading.Event()
    first_result = []

    calls = 0
    calls_lock = threading.Lock()

    def picker(cur, pool, cooled):
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        release.wait(timeout=10)
        return "late-result"

    def first_call():
        first_result.append(
            gate._pick_with_timeout(
                picker, "cur", ["a"], set()
            )
        )

    caller = threading.Thread(target=first_call)
    try:
        caller.start()
        assert started.wait(timeout=1)

        # 第一个调用仍在 join 等待期内，第二个调用不能再创建 worker。
        before = time.monotonic()
        value, ok = gate._pick_with_timeout(
            picker, "cur", ["b"], set()
        )
        elapsed = time.monotonic() - before

        assert (value, ok) == (None, False)
        assert elapsed < 0.1
        assert len(_picker_threads()) == 1
        assert _inflight() == 1

        caller.join(timeout=1)
        assert not caller.is_alive()
        assert first_result == [(None, False)]

        with calls_lock:
            assert calls == 1
    finally:
        release.set()
        caller.join(timeout=1)
        assert not caller.is_alive()
        assert _wait_for_picker_exit()