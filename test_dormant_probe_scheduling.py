"""沉睡池体检并入 daemon 主循环 —— 覆盖 2026-11-11 的调度缺口。

事故（实测，见 DESIGN-2026-11-11.md 第 12 部分）：
  `dormant_probe.py` 是 `.dormant_state.json` 的唯一生产者，
  `parse_subscription._load_dormant_state()` 是唯一消费者，但部署里没有
  任何调用方（无 timer / 无 cron / 部署脚本没接 / 仓库内除脚本自身无引用）。
  状态文件从未生成过，每轮配置刷新都打印"沉睡池状态读不到，本轮全部节点
  进池子"，判死节点反复回流 selector。

修法：不新增 systemd unit，把 6h 节流挂到 daemon 已有的主循环上，用子进程
跑脚本（脚本会另起 sing-box、会 import config，必须进程隔离）。

每个测试都写成「回滚修复即失败」的形式 —— 包括静态挂点断言：把主循环里
那行调用删掉，本文件必须立刻变红。
"""
import ast
import importlib.util
import json
import subprocess as _real_subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))


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
    # 别把测试输出写进生产 daemon.log
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
    """只替换 daemon 模块里的 subprocess 名字，不动标准库全局对象。"""
    TimeoutExpired = _real_subprocess.TimeoutExpired

    def __init__(self, rc=0, stdout="已写 .dormant_state.json（2 条，判死 1 条）",
                 stderr="", write_state_at=None):
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []
        self.write_state_at = write_state_at

    def run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.write_state_at is not None:
            self.write_state_at.write_text(
                json.dumps({"updated": time.time(), "nodes": {}}), encoding="utf-8")
        return type("R", (), {"returncode": self.rc,
                              "stdout": self.stdout,
                              "stderr": self.stderr})()


def _install(monkeypatch, mod, fake):
    monkeypatch.setattr(mod, "subprocess", fake)
    mod._DORMANT_PROBE_RUNNING = False
    mod._DORMANT_PROBE_NEXT_TS = 0.0
    mod._DORMANT_PROBE_FAIL_STREAK = 0


def _wait_done(mod, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline and mod._DORMANT_PROBE_RUNNING:
        time.sleep(0.01)
    assert not mod._DORMANT_PROBE_RUNNING, "后台线程没在超时内结束"


# ------------------------------------------------------- ① 首次部署：立即跑

def test_runs_immediately_when_state_file_missing(monkeypatch, tmp_path):
    mod = _load_daemon(monkeypatch, tmp_path)
    fake = _FakeSubprocess(rc=0, write_state_at=tmp_path / ".dormant_state.json")
    _install(monkeypatch, mod, fake)

    mod._maybe_run_dormant_probe()
    _wait_done(mod)

    assert len(fake.calls) == 1, "状态文件不存在时必须立刻触发一次"
    cmd, kw = fake.calls[0]
    assert cmd[1].endswith("dormant_probe.py")
    assert kw.get("cwd") == str(tmp_path)
    assert kw.get("timeout") == mod._DORMANT_PROBE_TIMEOUT_SEC
    assert mod._DORMANT_PROBE_NEXT_TS > time.time() + 5 * 3600
    assert any("完成" in x for x in mod._test_logs)


# ------------------------------------------------------- ② 6h 内不重复跑

def test_no_rerun_within_six_hours(monkeypatch, tmp_path):
    mod = _load_daemon(monkeypatch, tmp_path)
    (tmp_path / ".dormant_state.json").write_text(
        json.dumps({"updated": time.time() - 60, "nodes": {}}), encoding="utf-8")
    fake = _FakeSubprocess()
    _install(monkeypatch, mod, fake)

    mod._maybe_run_dormant_probe()
    assert fake.calls == [], "状态还在 6h 有效期内，不该重跑全量体检"
    assert mod._DORMANT_PROBE_NEXT_TS > time.time()


def test_rerun_when_state_expired(monkeypatch, tmp_path):
    mod = _load_daemon(monkeypatch, tmp_path)
    (tmp_path / ".dormant_state.json").write_text(
        json.dumps({"updated": time.time() - 7 * 3600, "nodes": {}}),
        encoding="utf-8")
    fake = _FakeSubprocess(rc=0)
    _install(monkeypatch, mod, fake)

    mod._maybe_run_dormant_probe()
    _wait_done(mod)
    assert len(fake.calls) == 1


# ------------------------------------------- ③ 失败立即重试 / 分批续跑 / 防重入

def test_failure_retries_immediately_without_waiting(monkeypatch, tmp_path):
    """定标：探测不等待、禁止设置等待时间。失败不许套任何退避窗口。"""
    mod = _load_daemon(monkeypatch, tmp_path)
    fake = _FakeSubprocess(rc=3, stderr="探测链路故障过多")
    _install(monkeypatch, mod, fake)

    mod._maybe_run_dormant_probe()
    _wait_done(mod)

    assert mod._DORMANT_PROBE_RETRY_SEC == 0
    assert mod._DORMANT_PROBE_NEXT_TS == 0.0, "失败后不许设置等待时间"
    assert any("exit=3" in x for x in mod._test_logs)
    # 下一 tick 必须能再触发，而不是被 30 分钟窗口挡住
    mod._maybe_run_dormant_probe()
    _wait_done(mod)
    assert len(fake.calls) == 2


def test_rc_more_resumes_immediately(monkeypatch, tmp_path):
    """预算用尽、进度已存盘 = 分批，不是失败：立刻续跑剩下的，不等待。"""
    mod = _load_daemon(monkeypatch, tmp_path)
    fake = _FakeSubprocess(rc=mod.RC_MORE, stdout="⏱ 进度已存，剩 120 条立即续跑")
    _install(monkeypatch, mod, fake)

    mod._maybe_run_dormant_probe()
    _wait_done(mod)

    assert mod._DORMANT_PROBE_NEXT_TS == 0.0
    assert any("分批续跑" in x for x in mod._test_logs)
    assert mod._DORMANT_PROBE_FAIL_STREAK == 0, "分批不能算失败"
    mod._maybe_run_dormant_probe()
    _wait_done(mod)
    assert len(fake.calls) == 2


def test_period_and_budget_come_from_the_script(monkeypatch, tmp_path):
    """不许在 daemon 里硬编码第二套 6h / 预算：改脚本常量两边一起变。"""
    mod = _load_daemon(monkeypatch, tmp_path)
    import dormant_probe as dp
    assert mod._DORMANT_PROBE_PERIOD_SEC == dp.POOL_RECHECK_HOURS * 3600
    assert mod._DORMANT_PROBE_BUDGET_SEC == dp.DEFAULT_MAX_SECONDS
    assert mod._DORMANT_PROBE_TIMEOUT_SEC > dp.DEFAULT_MAX_SECONDS, \
        "子进程兜底超时必须大于脚本自己的预算，否则又会被 SIGKILL"
    fake = _FakeSubprocess()
    _install(monkeypatch, mod, fake)
    mod._maybe_run_dormant_probe()
    _wait_done(mod)
    cmd, _kw = fake.calls[0]
    assert "--max-seconds" in cmd and str(dp.DEFAULT_MAX_SECONDS) in cmd


def test_thread_start_failure_resets_running_flag(monkeypatch, tmp_path):
    """线程 start 抛异常时运行标志必须复位，否则调度永久卡死。"""
    mod = _load_daemon(monkeypatch, tmp_path)
    _install(monkeypatch, mod, _FakeSubprocess())

    class _BoomThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise RuntimeError("no threads")

    monkeypatch.setattr(mod.threading, "Thread", _BoomThread)
    mod._maybe_run_dormant_probe()
    assert mod._DORMANT_PROBE_RUNNING is False
    assert mod._DORMANT_PROBE_NEXT_TS == 0.0


def test_no_reentry_while_running(monkeypatch, tmp_path):
    mod = _load_daemon(monkeypatch, tmp_path)
    fake = _FakeSubprocess()
    _install(monkeypatch, mod, fake)
    mod._DORMANT_PROBE_RUNNING = True

    mod._maybe_run_dormant_probe()
    assert fake.calls == []


# ------------------------------------------------------- ④ 静态挂点（回滚敏感）

def test_main_loop_actually_invokes_the_scheduler():
    """把主循环里的调用删掉（= 回到"没有调度器"）就必须红。"""
    tree = ast.parse((PROJECT / "daemon.py").read_text(encoding="utf-8"))
    main_fn = next((n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main_fn is not None
    loops = [n for n in ast.walk(main_fn) if isinstance(n, ast.While)]
    assert loops, "main() 里没有主循环"
    hit = False
    for loop in loops:
        for node in ast.walk(loop):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_maybe_run_dormant_probe"):
                hit = True
    assert hit, "主循环没有调用 _maybe_run_dormant_probe（沉睡池又没人调度了）"


def test_no_new_systemd_unit_for_dormant_probe():
    """用户明确否掉"为巡检单独开服务"：部署里不许出现 dormant unit/timer。"""
    offenders = []
    for pattern in ("*.service", "*.timer", "*.sh"):
        for f in list(PROJECT.glob(pattern)) + list((PROJECT / "deploy").glob(pattern)):
            if f.is_file() and "dormant" in f.name.lower():
                offenders.append(str(f))
    assert offenders == [], f"又出现了独立的沉睡池服务：{offenders}"
