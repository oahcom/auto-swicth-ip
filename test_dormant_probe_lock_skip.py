"""调度器撞锁：跳过 ≠ 失败（DESIGN 第 14 部分）。

实测 22:32:01~22:32:04，服务重启后 3 秒内 spawn 了 30 次体检子进程，
全部 rc=7 退出，还被当成连续失败打出「连续 1/10/20/30 次」4 条告警。
根因是 `KillMode=process` 残留的上一轮体检子进程仍持有 flock，而调度器
「探测不等待」的定标被误实现成「每个 tick 都起一个子进程去撞锁」。

修复：瞬时非阻塞探锁（拿到立刻释放）+ rc=7 归类为跳过。
两条底线不能破：**不设任何等待窗口**、**不把跳过记成失败**。
"""
import fcntl
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))


def _load_daemon(monkeypatch, tmp_path):
    before = {n: sys.modules.get(n) for n in ("daemon", "config", "warp_blocks")}
    for n in before:
        sys.modules.pop(n, None)
    spec = importlib.util.spec_from_file_location("daemon", PROJECT / "daemon.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daemon"] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        for n, v in before.items():
            if v is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = v
        raise
    mod._tb_saved = before
    monkeypatch.setattr(mod, "_PROJECT", str(tmp_path))
    logs = []
    monkeypatch.setattr(mod, "log", lambda m: logs.append(m))
    mod._test_logs = logs
    return mod


@pytest.fixture(autouse=True)
def _restore():
    before = {n: sys.modules.get(n) for n in ("daemon", "config", "warp_blocks")}
    yield
    for n, v in before.items():
        if v is None:
            sys.modules.pop(n, None)
        else:
            sys.modules[n] = v


class _FakeSubprocess:
    def __init__(self, rc=0):
        self.rc = rc
        self.calls = []

    def run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return type("R", (), {"returncode": self.rc, "stdout": "ok", "stderr": ""})()


def _install(monkeypatch, mod, fake):
    monkeypatch.setattr(mod, "subprocess", fake)
    mod._DORMANT_PROBE_RUNNING = False
    mod._DORMANT_PROBE_NEXT_TS = 0.0
    mod._DORMANT_PROBE_FAIL_STREAK = 0


def _wait(mod, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end and mod._DORMANT_PROBE_RUNNING:
        time.sleep(0.01)
    assert not mod._DORMANT_PROBE_RUNNING


# ---------------------------------------------------------- 探锁本身

def test_lock_busy_is_true_when_another_process_holds_it(monkeypatch, tmp_path):
    mod = _load_daemon(monkeypatch, tmp_path)
    fd = os.open(str(tmp_path / ".dormant_probe.lock"),
                 os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert mod._dormant_probe_lock_busy() is True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_lock_free_after_release(monkeypatch, tmp_path):
    mod = _load_daemon(monkeypatch, tmp_path)
    assert mod._dormant_probe_lock_busy() is False
    assert mod._dormant_probe_lock_busy() is False, "探锁不能自己把锁留下"


def test_probe_does_not_hold_lock(monkeypatch, tmp_path):
    """探锁拿到后必须立刻释放，否则会挡住真正要启动的子进程。"""
    mod = _load_daemon(monkeypatch, tmp_path)
    assert mod._dormant_probe_lock_busy() is False
    fd = os.open(str(tmp_path / ".dormant_probe.lock"),
                 os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # 应当拿得到
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------- 调度行为

def test_busy_tick_spawns_nothing_and_sets_no_wait(monkeypatch, tmp_path):
    """别人在跑：一个子进程都不起，也不设任何等待窗口。"""
    mod = _load_daemon(monkeypatch, tmp_path)
    fake = _FakeSubprocess()
    _install(monkeypatch, mod, fake)
    fd = os.open(str(tmp_path / ".dormant_probe.lock"),
                 os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        mod._maybe_run_dormant_probe()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert fake.calls == [], "撞锁时不能 spawn 空转子进程"
    assert mod._DORMANT_PROBE_NEXT_TS == 0.0, "不许设置等待时间"
    assert mod._DORMANT_PROBE_FAIL_STREAK == 0, "跳过不能记成失败"
    assert mod._test_logs == [], "跳过不该刷日志"


def test_lock_released_then_runs_immediately(monkeypatch, tmp_path):
    """锁一释放，下一 tick 立刻能跑 —— 没有 30 分钟窗口。"""
    mod = _load_daemon(monkeypatch, tmp_path)
    fake = _FakeSubprocess()
    _install(monkeypatch, mod, fake)
    mod._maybe_run_dormant_probe()
    _wait(mod)
    assert len(fake.calls) == 1


def test_rc_busy_is_not_a_failure(monkeypatch, tmp_path):
    """TOCTOU 窗口下仍可能撞锁；rc=7 必须归类为跳过。"""
    mod = _load_daemon(monkeypatch, tmp_path)
    fake = _FakeSubprocess(rc=mod._DORMANT_PROBE_RC_BUSY)
    _install(monkeypatch, mod, fake)
    mod._maybe_run_dormant_probe()
    _wait(mod)
    assert mod._DORMANT_PROBE_FAIL_STREAK == 0, "撞锁不能累计连续失败"
    assert mod._DORMANT_PROBE_NEXT_TS == 0.0, "撞锁不能引入等待"
    assert not any("失败" in x for x in mod._test_logs), "撞锁不该打失败日志"


def test_rollback_no_probe_check_fails(monkeypatch, tmp_path):
    """回滚探锁：撞锁 tick 会重新 spawn 空转子进程。"""
    import inspect
    src = inspect.getsource(_load_daemon(monkeypatch, tmp_path)._maybe_run_dormant_probe)
    assert "_dormant_probe_lock_busy()" in src
    assert "return" in src
