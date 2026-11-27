import concurrent.futures
import io
import json
import socket
import threading
import time

import pytest

import daemon


class _StopLoop(Exception):
    """测试用：终止只运行有限轮次的后台循环。"""


class _FakeCondition:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def wait(self, timeout=None):
        raise _StopLoop()

    def notify_all(self):
        pass


class _FakePool:
    def __init__(self, result=None, on_submit=None):
        self.result = result
        self.on_submit = on_submit
        self.submitted = []

    def submit(self, fn, /, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        if self.on_submit is not None:
            return self.on_submit(fn, *args, **kwargs)
        future = concurrent.futures.Future()
        future.set_result(self.result)
        return future

    def stats(self):
        return {"capacity": 16, "depth": len(self.submitted), "available": 0}


def _make_handler(monkeypatch, req=None, *, pool=None, authenticated=True):
    """构造不绑定真实 socket 的 handler 测试替身。"""
    handler = type("HandlerStub", (), {})()
    handler.path = "/rotate"
    handler.headers = {
        "Content-Length": str(len(json.dumps(req or {}).encode("utf-8"))),
    }
    handler.rfile = io.BytesIO(json.dumps(req or {}).encode("utf-8"))
    handler.close_connection = False
    handler.sent = []

    handler._authed = lambda: authenticated
    handler._send = lambda code, payload: (
        handler.sent.append((code, payload)) or True
    )

    if pool is not None:
        monkeypatch.setattr(daemon, "_rotate_pool", pool)

    return handler


def _read_http_response(sock):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)

    if b"\r\n\r\n" not in data:
        return bytes(data), b""

    head, body = bytes(data).split(b"\r\n\r\n", 1)
    content_length = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())
            break

    while len(body) < content_length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk

    return head, body[:content_length]


def _post_request(sock, token):
    body = b"{}"
    request = (
        b"POST /rotate HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"X-Asip-Token: " + token.encode("utf-8") + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
        b"\r\n" + body
    )
    sock.sendall(request)


def test_rotate_pool_accepts_eight_running_and_eight_queued_tasks():
    pool = daemon._BoundedPool(
        max_workers=8,
        name_prefix="test-rotate",
        queue=16,
    )
    release = threading.Event()
    started = []
    started_lock = threading.Lock()

    def task():
        with started_lock:
            started.append(threading.current_thread().name)
        assert release.wait(timeout=5)
        return "done"

    try:
        futures = [pool.submit(task) for _ in range(16)]

        assert all(future is not None for future in futures)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with started_lock:
                if len(started) == 8:
                    break
            time.sleep(0.01)

        with started_lock:
            assert len(started) == 8

        assert pool.submit(task) is None
        assert pool.stats()["depth"] == 16

        release.set()
        assert [future.result(timeout=5) for future in futures] == [
            "done"
        ] * 16

        assert pool.stats()["depth"] == 0
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_seventeenth_rotate_request_returns_503_without_business_call(
    monkeypatch,
):
    calls = {"main": 0, "slot": 0}

    def fake_rotate_once(*args, **kwargs):
        calls["main"] += 1
        return {}

    def fake_rotate_slot_once(*args, **kwargs):
        calls["slot"] += 1
        return {}

    monkeypatch.setattr(daemon, "_rotate_once", fake_rotate_once)
    monkeypatch.setattr(daemon, "_rotate_slot_once", fake_rotate_slot_once)

    pool = _FakePool(on_submit=lambda *args, **kwargs: None)
    handler = _make_handler(monkeypatch, {"reason": "test"}, pool=pool)

    daemon._RotateHandler.do_POST(handler)

    assert len(handler.sent) == 1
    code, payload = handler.sent[0]
    assert code == 503
    assert payload["ok"] is False
    assert payload["reason"] == "overloaded"
    assert payload["retry_after_ms"] == 3000
    assert calls == {"main": 0, "slot": 0}


def test_rotate_responses_preserve_overload_and_normal_trace_fields(
    monkeypatch,
):
    # 过载响应字段。
    overloaded_handler = _make_handler(
        monkeypatch,
        {"reason": "test"},
        pool=_FakePool(on_submit=lambda *args, **kwargs: None),
    )
    daemon._RotateHandler.do_POST(overloaded_handler)

    code, payload = overloaded_handler.sent[0]
    assert code == 503
    assert payload == {
        "ok": False,
        "reason": "overloaded",
        "retry_after_ms": 3000,
    }

    # 正常响应字段及 trace 字段。
    monkeypatch.setattr(
        daemon,
        "_slot_by_proxy_url",
        lambda _: None,
    )
    monkeypatch.setattr(
        daemon,
        "_rotate_once",
        lambda *args, **kwargs: {
            "ok": True,
            "restart_required": True,
            "reason": "no_healthy_gate",
        },
    )
    monkeypatch.setattr(
        daemon,
        "_rotate_trace_fields",
        lambda req, result: {
            "trace_id": "trace-test",
            "req_id": "req-test",
        },
    )

    normal_handler = _make_handler(
        monkeypatch,
        {"req_id": "req-test", "reason": "test"},
        pool=_FakePool(result={
            "ok": True,
            "restart_required": True,
            "reason": "no_healthy_gate",
        }),
    )
    daemon._RotateHandler.do_POST(normal_handler)

    code, payload = normal_handler.sent[0]
    assert code == 200
    assert payload["ok"] is True
    assert payload["restart_required"] is True
    assert payload["reason"] == "no_healthy_gate"
    assert payload["trace_id"] == "trace-test"
    assert payload["req_id"] == "req-test"


def test_srv_rejects_thirty_third_connection_and_releases_slots(
    monkeypatch,
    tmp_path,
):
    socket_path = str(tmp_path / "rotate-test.sock")
    token = "test-token"
    monkeypatch.setattr(daemon, "_API_TOKEN", token)

    submitted = []
    submitted_lock = threading.Lock()
    all_submitted = threading.Event()
    release = threading.Event()

    class BlockingPool:
        def stats(self):
            with submitted_lock:
                depth = len(submitted)
            return {"capacity": 32, "depth": depth,
                    "available": 32 - depth}

        def submit(self, fn, /, *args, **kwargs):
            future = concurrent.futures.Future()
            with submitted_lock:
                submitted.append(future)
                if len(submitted) == 32:
                    all_submitted.set()

            def complete():
                if release.wait(timeout=8):
                    future.set_result({"ok": True, "restart_required": False})
                else:
                    future.set_exception(TimeoutError("test release timeout"))

            threading.Thread(target=complete, daemon=True).start()
            return future

    monkeypatch.setattr(daemon, "_rotate_pool", BlockingPool())
    # 本用例验证的是接入层 32 槽位，不是 /rotate 的预算准入：
    # 把预算阈值放到 32，让 32 个请求都能真正提交并阻塞。
    monkeypatch.setattr(
        daemon, "_ROTATE_ADMISSION_MAX_INFLIGHT", 32
    )
    monkeypatch.setattr(
        daemon,
        "_rotate_trace_fields",
        lambda req, result: {},
    )

    server = daemon._Srv(socket_path, daemon._RotateHandler)
    server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    server_thread.start()

    clients = []
    try:
        # 32 个请求分别占用 32 个接入线程槽位。
        for _ in range(32):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5)
            client.connect(socket_path)
            _post_request(client, token)
            clients.append(client)

        assert all_submitted.wait(timeout=5)

        # 第 33 个连接应由接入层直接拒绝。
        extra = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        extra.settimeout(5)
        extra.connect(socket_path)
        _post_request(extra, token)

        head, body = _read_http_response(extra)
        assert b"503 Service Unavailable" in head
        overload = json.loads(body.decode("utf-8"))
        assert overload["ok"] is False
        assert overload["reason"] == "overloaded"
        assert overload["retry_after_ms"] == 3000
        extra.close()

        # 释放业务 future，让 32 个 handler 退出。
        release.set()
        for client in clients:
            head, body = _read_http_response(client)
            assert b"200" in head
            assert json.loads(body.decode("utf-8"))["ok"] is True
            client.close()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if server._request_slots._value == 32:
                break
            time.sleep(0.01)

        assert server._request_slots._value == 32
    finally:
        release.set()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert not server_thread.is_alive()


@pytest.mark.parametrize(
    ("configured", "expected_max"),
    [(26, 4), (2, 2)],
)
def test_warp_probe_loop_respects_hard_concurrency_limit(
    monkeypatch,
    configured,
    expected_max,
):
    old_probing = set(daemon._WARP_PROBING)
    daemon._WARP_PROBING.clear()

    members = [{"tag": f"WARP-{i}"} for i in range(12)]
    submitted_counts = []

    class CapturePool:
        def submit(self, fn, /, *args, **kwargs):
            submitted_counts.append(len(daemon._WARP_PROBING))
            return object()

    monkeypatch.setattr(daemon, "_warp_probe_pool", CapturePool())
    monkeypatch.setattr(daemon, "_warp", lambda: {
        "probe_concurrency": configured,
        "prober_idle_wait_sec": 30,
    })
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)
    monkeypatch.setattr(daemon, "_probe_targets", lambda now: members)
    monkeypatch.setattr(daemon, "_line_enabled", lambda member: True)
    monkeypatch.setattr(
        daemon,
        "_warp_member_unit_running",
        lambda member, now: True,
    )
    monkeypatch.setattr(
        daemon,
        "_warp_line_needs_probe",
        lambda tag, now: (True, now),
    )
    monkeypatch.setattr(
        daemon.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(_StopLoop()),
    )

    try:
        with pytest.raises(_StopLoop):
            daemon._warp_prober_loop()

        assert submitted_counts
        assert max(submitted_counts) <= expected_max
        assert len(daemon._WARP_PROBING) <= expected_max
        assert len(submitted_counts) == expected_max
    finally:
        daemon._WARP_PROBING.clear()
        daemon._WARP_PROBING.update(old_probing)


def test_warp_probe_exception_and_pool_full_clear_probing_tags(monkeypatch):
    old_probing = set(daemon._WARP_PROBING)
    daemon._WARP_PROBING.clear()

    member = {"tag": "WARP-test"}
    monkeypatch.setattr(
        daemon,
        "_warp",
        lambda: {"probe_concurrency": 4, "prober_idle_wait_sec": 30},
    )
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)
    monkeypatch.setattr(daemon, "_probe_targets", lambda now: [member])
    monkeypatch.setattr(daemon, "_line_enabled", lambda item: True)
    monkeypatch.setattr(
        daemon,
        "_warp_member_unit_running",
        lambda item, now: True,
    )
    monkeypatch.setattr(
        daemon,
        "_warp_line_needs_probe",
        lambda tag, now: (True, now),
    )
    monkeypatch.setattr(daemon, "_WARP_LINE_CV", _FakeCondition())
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {})

    captured = []

    class CapturePool:
        def submit(self, fn, /, *args, **kwargs):
            captured.append(fn)
            return object()

    monkeypatch.setattr(daemon, "_warp_probe_pool", CapturePool())

    try:
        with pytest.raises(_StopLoop):
            daemon._warp_prober_loop()

        assert "WARP-test" in daemon._WARP_PROBING
        assert len(captured) == 1

        monkeypatch.setattr(
            daemon,
            "_probe_one_line",
            lambda item: (_ for _ in ()).throw(RuntimeError("probe failed")),
        )
        monkeypatch.setattr(daemon, "log", lambda *args, **kwargs: None)

        captured[0]()
        assert "WARP-test" not in daemon._WARP_PROBING

        # 池满时 submit 返回 None，循环必须立即清除 probing 标记。
        daemon._WARP_PROBING.clear()

        class FullPool:
            def submit(self, fn, /, *args, **kwargs):
                return None

        monkeypatch.setattr(daemon, "_warp_probe_pool", FullPool())

        with pytest.raises(_StopLoop):
            daemon._warp_prober_loop()

        assert "WARP-test" not in daemon._WARP_PROBING
    finally:
        daemon._WARP_PROBING.clear()
        daemon._WARP_PROBING.update(old_probing)


def test_health_pool_has_sixteen_workers_and_256_capacity():
    assert daemon._health_pool._executor._max_workers == 16
    assert daemon._health_pool._capacity == 256


def test_memory_observation_logs_at_most_once_per_minute(monkeypatch):
    logs = []
    clock = iter([100.0, 110.0, 159.9, 160.1])

    monkeypatch.setattr(daemon, "_MEMORY_OBS_LAST", 0.0)
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(daemon, "log", lambda message: logs.append(message))

    daemon._log_memory_observation()
    daemon._log_memory_observation()
    daemon._log_memory_observation()
    daemon._log_memory_observation()

    assert len(logs) == 2
    assert all("memory rss=" in message for message in logs)
    assert all("threads=" in message for message in logs)
    assert all("rotate_inflight=" in message for message in logs)


def test_memory_observation_proc_read_failures_do_not_raise(monkeypatch):
    import builtins

    logs = []

    def failing_open(*args, **kwargs):
        raise OSError("procfs unavailable")

    monkeypatch.setattr(builtins, "open", failing_open)
    monkeypatch.setattr(daemon, "_MEMORY_OBS_LAST", 0.0)
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(daemon, "log", lambda message: logs.append(message))

    # /proc 和 cgroup 文件均不可读时，观测函数不得使调用循环退出。
    daemon._log_memory_observation()

    assert len(logs) == 1
    assert "memory rss=0kB" in logs[0]
    assert "anon=0kB" in logs[0]
    assert "cgroup=-1kB" in logs[0]
    assert "threads=0" in logs[0]


def test_rotate_budget_rejects_when_all_workers_are_inflight(
    monkeypatch,
):
    calls = {"main": 0, "slot": 0}

    def fake_rotate_once(*args, **kwargs):
        calls["main"] += 1
        return {}

    def fake_rotate_slot_once(*args, **kwargs):
        calls["slot"] += 1
        return {}

    monkeypatch.setattr(daemon, "_rotate_once", fake_rotate_once)
    monkeypatch.setattr(daemon, "_rotate_slot_once", fake_rotate_slot_once)

    pool = _FakePool()
    # depth = 排队 + 在跑；8 表示 8 个 worker 都已有任务，再提交就要排队。
    pool.submitted = [(None, (), {}) for _ in range(8)]

    handler = _make_handler(monkeypatch, {"reason": "test"}, pool=pool)

    daemon._RotateHandler.do_POST(handler)

    assert len(handler.sent) == 1
    code, payload = handler.sent[0]
    assert code == 503
    assert payload == {
        "ok": False,
        "reason": "overloaded",
        "retry_after_ms": 3000,
    }

    # 预算拒绝必须发生在 submit 之前。
    assert len(pool.submitted) == 8
    assert calls == {"main": 0, "slot": 0}


def test_rotate_budget_serializes_depth_check_and_submit(monkeypatch):
    pool = _FakePool()
    pool.submitted = [(None, (), {}) for _ in range(7)]
    monkeypatch.setattr(daemon, "_rotate_pool", pool)

    results = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(3)

    def submit_one():
        barrier.wait(timeout=2)
        result = daemon._submit_rotate_with_budget({})
        with results_lock:
            results.append(result)

    threads = [
        threading.Thread(target=submit_one),
        threading.Thread(target=submit_one),
    ]

    for thread in threads:
        thread.start()

    barrier.wait(timeout=2)

    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    # depth=7 只剩一个名额：两个并发调用只能有一个提交成功。
    assert len(pool.submitted) == 8
    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 1
