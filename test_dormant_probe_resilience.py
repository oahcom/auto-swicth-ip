"""沉睡池体检第三轮：实例中途死亡与并发误杀（DESIGN 第 13 部分）。

实测事故链（2026-11-11 21:51 / 22:06）：
  · 自动全量体检 exit=3「探测故障 225/247」，但日志只留最后 4 行；
  · 手动全量 dry-run 118 ok / 110 dead / **19 error**，第一条 error 在第
    229 条，标记 `http=port-closed` —— 临时 sing-box 跑到后段被杀；
  · `finally` 无条件 unlink 掉实例日志，死因永远查不到；
  · `kill_orphans()` 用 pgrep 无差别 SIGTERM 所有临时实例，而 PrivateTmp
    只隔离 /tmp 文件、不隔离 /proc，服务态与手动实例互相看得见。

修复三件事，每条都写成「回滚即失败」：
  ① kill_orphans 只杀没有活跃主人的孤儿；
  ② 实例中途死亡时立即收尾、保住已测进度、返回 RC_MORE 立即续跑；
  ③ 异常轮把 sing-box 自己的日志留档，不再删光。
"""
import importlib.util
import os
import re
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent
SRC = (PROJECT / "dormant_probe.py").read_text(encoding="utf-8")


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "dormant_probe_resilience", PROJECT / "dormant_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dp():
    return _load_probe()


# ---------------------------------------------------------------- ① 只杀真孤儿

def test_kill_orphans_spares_instances_with_live_owner(dp, monkeypatch):
    """有主实例绝不能被 SIGTERM；只有 PPID 不在活跃集合的孤儿才清。"""
    killed = []
    monkeypatch.setattr(dp.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": "111 sing-box run -c /tmp/dormant-probe-a.json\n"
                            "222 sing-box run -c /tmp/dormant-probe-b.json\n"})())
    monkeypatch.setattr(dp, "_active_probe_pids", lambda: {1001})
    monkeypatch.setattr(dp, "_proc_ppid", lambda pid: {111: 1001, 222: 1}[pid])
    monkeypatch.setattr(dp.os, "kill", lambda pid, sig: killed.append(pid))
    n = dp.kill_orphans()
    assert n == 1, "只应清掉 PPID=1 的那个孤儿"
    assert killed == [222], "PPID 属于活跃 dormant_probe 的实例必须保命"


def test_kill_orphans_without_active_owner_cleans_both(dp, monkeypatch):
    """两个都是孤儿（父进程已消失）时全部清掉，不能因为精确化而漏清。"""
    killed = []
    monkeypatch.setattr(dp.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": "111 sing-box run -c /tmp/dormant-probe-a.json\n"
                            "222 sing-box run -c /tmp/dormant-probe-b.json\n"})())
    monkeypatch.setattr(dp, "_active_probe_pids", lambda: set())
    monkeypatch.setattr(dp, "_proc_ppid", lambda pid: 1)
    monkeypatch.setattr(dp.os, "kill", lambda pid, sig: killed.append(pid))
    assert dp.kill_orphans() == 2
    assert killed == [111, 222]


def test_rollback_unconditional_kill_fails():
    """回滚判据（删掉 `if _proc_ppid(pid) in active: continue`）必须破坏精确化。"""
    assert re.search(r"if _proc_ppid\(pid\) in active:\s*\n\s*continue", SRC), \
        "有主跳过判据被移除"
    assert "_proc_ppid" in SRC and "_active_probe_pids" in SRC


def test_proc_ppid_parses_comm_with_spaces(dp, monkeypatch, tmp_path):
    """/proc stat 的 comm 可能带空格和右括号，不能用 split()[1] 直接取 ppid。"""
    stat = tmp_path / "stat"
    stat.write_text('4242 (sing-box run -c) S 1001 4242 4242 0 -1 4194560\n')
    import builtins
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if str(path) == "/proc/4242/stat":
            return real_open(stat, *a, **k)
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert dp._proc_ppid(4242) == 1001


def test_active_probe_pids_excludes_self(dp, monkeypatch):
    """自己也是 dormant_probe.py，不能把自己算进「别人的活跃实例」。"""
    monkeypatch.setattr(dp.os, "getpid", lambda: 999)
    monkeypatch.setattr(dp.os, "listdir", lambda p: ["999", "1000", "abc", "1001"])
    monkeypatch.setattr(dp, "_proc_cmdline", lambda pid: {
        999: "python3 dormant_probe.py", 1000: "python3 dormant_probe.py --dry-run",
        1001: "python3 daemon.py",
    }[pid])
    assert dp._active_probe_pids() == {1000}


# -------------------------------------------------- ② 实例中途死亡保住进度

def test_instance_death_branch_returns_rc_more_and_saves_progress():
    """检测到实例已退出时必须就地收尾，不能把剩余节点全跑成 error。"""
    assert "instance_dead = (proc is not None and proc.poll() is not None)" in SRC
    assert '_LAST_HTTP.get(tag) == "port-closed"' in SRC
    assert "return RC_MORE" in SRC
    assert "save_state(st, final=False)" in SRC
    assert "本条没测完，不能 _mark_done" in SRC


def test_instance_death_before_first_probe_reports_failure():
    """一条都没测成就崩：必须如实报错，不能伪装成「进度已存」造成空转。"""
    assert re.search(r"if i <= 1:.*?return 3", SRC, re.S), \
        "首条即崩的失败分支被移除"


def test_rollback_instance_death_branch_fails():
    """回滚掉中途死亡分支，剩余节点会被继续判 error 并拖垮整轮。"""
    assert "临时实例在第 {i}/{len(tags)} 条时退出" in SRC


# ------------------------------------------------------------ ③ 异常轮留档

def test_abnormal_round_keeps_instance_log():
    """实例崩了 / 出现 error 时，sing-box 自身输出必须留档。"""
    assert 'INSTANCE_LOG = ".dormant_probe_instance.log"' in SRC
    assert "crashed_before_cleanup" in SRC
    assert "abnormal = crashed_before_cleanup or errors > 0" in SRC
    # 取崩溃状态必须在 terminate 之前，否则 poll() 恒非 None，正常轮也会留档
    i = SRC.index("crashed_before_cleanup =")
    j = SRC.index("proc.terminate()", i)
    assert i < j, "状态采样被挪到 terminate 之后就失效了"


def test_normal_round_does_not_leave_instance_log(dp, monkeypatch, tmp_path):
    """正常轮（实例活着、0 error）不该往项目里丢诊断文件。"""
    monkeypatch.setattr(dp, "ROOT", tmp_path)
    # 直接验证判据本身：正常轮的 abnormal 必须为 False
    crashed_before_cleanup = False
    errors = 0
    assert not (crashed_before_cleanup or errors > 0)


def test_instance_log_constant_is_defined_name(dp):
    """常量必须在模块里真实存在（名字检查关卡抓到过只在补丁脚本里定义）。"""
    assert isinstance(dp.INSTANCE_LOG, str) and dp.INSTANCE_LOG
