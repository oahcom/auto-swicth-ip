"""沉睡池首跑全量的「分批存进度 / 断点续跑」—— 覆盖 21:50 审查发现的 P0。

事故链（实测）：singbox-config.json 有 247 个待探测节点，`--timeout` 默认 7s、
判死节点还要 14s 复测；旧调度器给子进程 2400s 预算，超时直接 SIGKILL，
而 `save_state` 只在整轮循环结束后调用 —— 于是状态文件永远写不出来，
下一轮又从全量开始，形成死循环。整套沉睡池降级机制等于白做。

修法：按预算主动收尾（正常 return RC_MORE），每 10 条存一次进度，
进度存盘写 updated=0 / complete=False（`final=False`），daemon 见到就
下一 tick 立刻续跑 pending 里剩下的，不引入任何等待。

每个测试都写成「回滚修复即失败」的形式。
"""
import importlib.util
import json
import os
import subprocess as _real_subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "dormant_probe_checkpoint", PROJECT / "dormant_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dp():
    return _load_probe()


class _FakeProc:
    def __init__(self, *a, **kw):
        pass

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


class _FakeSubprocess:
    TimeoutExpired = _real_subprocess.TimeoutExpired
    Popen = _FakeProc

    @staticmethod
    def run(*a, **kw):
        return type("R", (), {"stdout": "", "returncode": 0})()


class _Clock:
    """第一次调用给 started，之后一律给「预算已用尽」的时刻。"""

    def __init__(self):
        self.n = 0

    def time(self):
        self.n += 1
        return 100.0 if self.n == 1 else 200.0

    @staticmethod
    def sleep(_s):
        pass


def _install_main(monkeypatch, dp, tmp_path, tags, probed, rc_ok=True):
    """把 sing-box / curl / 端口这些外部依赖换掉，只留真正的调度逻辑。"""
    live = tmp_path / "singbox-config.json"
    live.write_text(json.dumps({"outbounds": [
        {"tag": t, "type": "vless"} for t in tags]}), encoding="utf-8")
    state = tmp_path / ".dormant_state.json"
    cfg_path = tmp_path / "tmp-cfg.json"
    cfg_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(dp, "ROOT", tmp_path)
    monkeypatch.setattr(dp, "LIVE", live)
    monkeypatch.setattr(dp, "STATE", state)
    # 生产 daemon 可能正跑着体检、持着仓库根目录下的 flock。测试必须用
    # 自己的锁，否则全部撞 rc=7「另一个探测实例正在跑」。
    monkeypatch.setattr(dp, "LOCK_PATH", str(tmp_path / ".dormant_probe.lock"))
    monkeypatch.setattr(dp, "subprocess", _FakeSubprocess)
    monkeypatch.setattr(dp, "free_port", lambda *a, **k: 12345)
    monkeypatch.setattr(dp, "build_tmp_cfg", lambda *a, **k: str(cfg_path))
    monkeypatch.setattr(dp, "kill_orphans", lambda: 0)
    monkeypatch.setattr(dp, "api", lambda *a, **k: {})
    monkeypatch.setattr(dp, "node_fp", lambda _o: "fp")
    monkeypatch.setattr(dp, "targets", lambda _all: list(tags))
    monkeypatch.setattr(dp, "selector_tag", lambda: "proxy")

    def _probe(tag, *a, **k):
        probed.append(tag)
        return dp.OK if rc_ok else dp.DEAD

    monkeypatch.setattr(dp, "probe_one", _probe)
    return state


def test_save_state_progress_is_not_a_completed_round(dp, tmp_path, monkeypatch):
    """进度存盘绝不能写 updated=now，否则 daemon 会等 6h 才测剩下的。"""
    monkeypatch.setattr(dp, "ROOT", tmp_path)
    state = tmp_path / ".dormant_state.json"
    monkeypatch.setattr(dp, "STATE", state)
    st = {"nodes": {"a": {"alive": True}}, "pending": ["b", "c"]}

    dp.save_state(st, final=False)
    disk = json.loads(state.read_text(encoding="utf-8"))
    assert disk["updated"] == 0.0
    assert disk["complete"] is False
    assert disk["pending"] == ["b", "c"]
    assert dp.load_state()["pending"] == ["b", "c"], "旧文件也要能补出 pending"

    dp.save_state(st, final=True)
    disk = json.loads(state.read_text(encoding="utf-8"))
    assert disk["updated"] > 0
    assert disk["complete"] is True


def test_budget_exhaustion_returns_rc_more_with_progress_saved(dp, tmp_path, monkeypatch):
    """被预算截断时：正常退出（不是被 kill）、进度已落盘、updated=0。"""
    tags = ["n1", "n2", "n3", "n4", "n5"]
    probed = []
    state = _install_main(monkeypatch, dp, tmp_path, tags, probed)
    monkeypatch.setattr(dp, "time", _Clock())
    monkeypatch.setattr(sys, "argv", ["dormant_probe.py", "--max-seconds", "1"])

    rc = dp.main()
    assert rc == dp.RC_MORE
    disk = json.loads(state.read_text(encoding="utf-8"))
    assert disk["updated"] == 0.0, "未完成必须写 0，daemon 才会立刻续跑"
    assert disk["complete"] is False
    assert len(probed) == 1
    assert disk["pending"] == tags[1:], "没测的要留在 pending，不能销账"
    assert "n1" in disk["nodes"]


def test_resume_only_probes_pending_nodes(dp, tmp_path, monkeypatch):
    """续跑不能重新全量：只测 pending 里的，且跑完写 complete=True。"""
    tags = ["n1", "n2", "n3", "n4", "n5"]
    state = tmp_path / ".dormant_state.json"
    state.write_text(json.dumps({
        "nodes": {"n1": {"alive": True, "checked": 100.0, "strikes": 0,
                         "result": "ok", "fp": "fp"}},
        "pending": ["n2", "n3"],
        "updated": 0.0, "complete": False,
    }), encoding="utf-8")
    probed = []
    _install_main(monkeypatch, dp, tmp_path, tags, probed)
    monkeypatch.setattr(sys, "argv", ["dormant_probe.py"])

    rc = dp.main()
    assert rc == 0
    assert probed == ["n2", "n3"], f"续跑重测了已完成的节点：{probed}"
    disk = json.loads(state.read_text(encoding="utf-8"))
    assert disk["pending"] == []
    assert disk["complete"] is True and disk["updated"] > 0
    assert set(disk["nodes"]) == {"n1", "n2", "n3"}


def test_dry_run_never_writes_progress(dp, tmp_path, monkeypatch):
    """--dry-run 是运维查链路用的，不许污染 pending / 状态文件。"""
    tags = ["n1", "n2"]
    probed = []
    state = _install_main(monkeypatch, dp, tmp_path, tags, probed)
    monkeypatch.setattr(sys, "argv", ["dormant_probe.py", "--dry-run", "--limit", "2"])
    assert dp.main() == 0
    assert not state.exists()
