"""沉睡池体检的可观测性与幂等 —— 覆盖 2026-11-11 第二轮实测暴露的问题。

第一次自动全量体检（21:51:37）退出码 3：`探测故障 225/247 条（阈值 82）`，
但 daemon 只把最后 4 行写进日志，首条故障原因、实例何时垮掉全都查不到。
同时 21:50 与 21:51:03 服务连着启动两次，两次全量体检的临时 sing-box
同时在盘上 —— 失败后单靠进程内时间戳，重启就会立刻再跑一遍 247 条。

本文件锁住三件事：
  ① 探测故障要留下具体原因，且 flock 撞车不能伪装成"体检成功"；
  ② 完整输出落盘，不再只留摘要；
  ③ "已经发起过"这件事持久化，重启风暴不会变成反复全量探测。
"""
import importlib.util
import re
import subprocess as _real_subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "dormant_probe_diag", PROJECT / "dormant_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_daemon(monkeypatch, tmp_path):
    saved = {name: sys.modules.get(name)
             for name in ("daemon", "config", "warp_blocks")}
    for name in saved:
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("daemon", PROJECT / "daemon.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daemon"] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        for name, val in saved.items():
            if val is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = val
        raise
    mod._tb_saved_modules = saved
    monkeypatch.setattr(mod, "_PROJECT", str(tmp_path))
    logs = []
    monkeypatch.setattr(mod, "log", lambda m: logs.append(m))
    mod._test_logs = logs
    return mod


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    before = {name: sys.modules.get(name)
              for name in ("daemon", "config", "warp_blocks")}
    yield
    for name, val in before.items():
        if val is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = val


class _FakeSubprocess:
    TimeoutExpired = _real_subprocess.TimeoutExpired

    def __init__(self, rc=0, lines=None):
        self.rc = rc
        self.lines = lines or ["[1/3] ok a", "[2/3] error b", "[3/3] dead c",
                               "结果：通 1 / 3，探测故障 1 条"]
        self.calls = []

    def run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return type("R", (), {"returncode": self.rc,
                              "stdout": "\n".join(self.lines) + "\n",
                              "stderr": ""})()


def _install(monkeypatch, mod, fake):
    monkeypatch.setattr(mod, "subprocess", fake)
    mod._DORMANT_PROBE_RUNNING = False
    mod._DORMANT_PROBE_NEXT_TS = 0.0


def _wait_done(mod, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline and mod._DORMANT_PROBE_RUNNING:
        time.sleep(0.01)
    assert not mod._DORMANT_PROBE_RUNNING


# ------------------------------------------------------- ① 故障可诊断

def test_probe_one_records_api_failure_reason(monkeypatch):
    dp = _load_probe()

    def boom(*a, **k):
        raise OSError("Connection refused")

    monkeypatch.setattr(dp, "api", boom)
    assert dp.probe_one("t", "s", 1, 2, 7) == dp.ERROR
    assert "clash API" in dp._LAST_ERROR
    assert "Connection refused" in dp._LAST_ERROR


def test_probe_one_records_port_closed_reason(monkeypatch):
    dp = _load_probe()
    monkeypatch.setattr(dp, "api", lambda *a, **k: None)
    monkeypatch.setattr(dp, "port_open", lambda p: False)
    monkeypatch.setattr(dp.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "000"})())
    assert dp.probe_one("t", "s", 1, 2, 7) == dp.ERROR
    assert "端口" in dp._LAST_ERROR


def test_first_error_reason_is_printed():
    """回滚敏感：225 条 error 只给一句"探测故障"是查不动的。"""
    src = (PROJECT / "dormant_probe.py").read_text(encoding="utf-8")
    assert "首条探测故障原因" in src
    assert "_LAST_ERROR" in src


def test_flock_conflict_is_not_reported_as_success():
    """回滚敏感：return 0 会让调度器把"什么都没做"当成体检成功，等 6h。"""
    src = (PROJECT / "dormant_probe.py").read_text(encoding="utf-8")
    m = re.search(r"另一个探测实例正在跑.*?return\s+(\d+)", src, re.S)
    assert m, "找不到 flock 冲突分支"
    assert m.group(1) != "0", "并发跳过又被报成成功"


# ------------------------------------------------------- ② 完整输出落盘

def test_full_probe_output_written_to_log(monkeypatch, tmp_path):
    mod = _load_daemon(monkeypatch, tmp_path)
    fake = _FakeSubprocess(rc=3, lines=["[1/3] ok a", "[2/3] error b",
                                        "⚠ 首条探测故障原因：clash API 切换失败",
                                        "结果：通 1 / 3，探测故障 2 条"])
    _install(monkeypatch, mod, fake)

    mod._maybe_run_dormant_probe()
    _wait_done(mod)

    log_path = tmp_path / mod._DORMANT_PROBE_LOG_FILE
    assert log_path.exists(), "完整输出没落盘"
    body = log_path.read_text(encoding="utf-8")
    assert "exit=3" in body
    assert "首条探测故障原因" in body
    assert "结果：通 1 / 3" in body
    assert any("完整输出" in x for x in mod._test_logs)


# ------------------------------------------------------- ③ 重启幂等

def test_attempt_file_written_when_triggered(monkeypatch, tmp_path):
    mod = _load_daemon(monkeypatch, tmp_path)
    fake = _FakeSubprocess()
    _install(monkeypatch, mod, fake)

    mod._maybe_run_dormant_probe()
    _wait_done(mod)

    attempt = tmp_path / mod._DORMANT_PROBE_ATTEMPT_FILE
    assert attempt.exists()
    assert abs(float(attempt.read_text()) - time.time()) < 10


def test_restart_resumes_immediately_not_blocked_by_attempt_file(monkeypatch, tmp_path):
    """重启不该被时间窗口挡住；「不重复全量」靠脚本的 pending 断点，不靠等待。

    旧实现用 `_DORMANT_PROBE_RETRY_SEC=30min` 的 attempt 闸门防重启风暴，
    但那连「进度已存盘、只差剩下 120 条」都要等半小时，与主人定标的
    「探测不等待、禁止设置等待时间」冲突。现在 attempt 文件只作诊断，
    调度立即触发；脚本读 pending 只测剩下的，不会重跑 247 条。
    """
    mod = _load_daemon(monkeypatch, tmp_path)
    (tmp_path / mod._DORMANT_PROBE_ATTEMPT_FILE).write_text(str(time.time() - 60))
    fake = _FakeSubprocess()
    _install(monkeypatch, mod, fake)

    mod._maybe_run_dormant_probe()
    _wait_done(mod)
    assert len(fake.calls) == 1, "attempt 文件不是闸门：重启后立即续跑"
    assert "--max-seconds" in fake.calls[0][0], \
        "按预算分批 + pending 断点才是「不重复全量」的保证"


def test_no_wait_regardless_of_attempt_age(monkeypatch, tmp_path):
    """不管上次发起是 1 分钟前还是 7 小时前，都不设等待窗口。"""
    mod = _load_daemon(monkeypatch, tmp_path)
    (tmp_path / mod._DORMANT_PROBE_ATTEMPT_FILE).write_text(
        str(time.time() - 7 * 3600))
    fake = _FakeSubprocess()
    _install(monkeypatch, mod, fake)
    assert mod._DORMANT_PROBE_RETRY_SEC == 0

    mod._maybe_run_dormant_probe()
    _wait_done(mod)
    assert len(fake.calls) == 1
