"""tools/cf_day_mode.py 的安全与失败可见性用例。

锁四件事：外部命令非零退出必须抛、selector 设置失败必须抛、
源码里不许留 token、补位单元筛选容忍坏行。
"""
import importlib.util
import json
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "tools" / "cf_day_mode.py"
_spec = importlib.util.spec_from_file_location("cf_day_mode", _SRC)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


@pytest.fixture(autouse=True)
def _redirect_log(monkeypatch, tmp_path):
    """任何用例都不许往生产 .warp/cf_day_mode.log 写。"""
    monkeypatch.setattr(mod, "LOG", tmp_path / "cf_day_mode.log")


class _Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_sctl_raises_on_nonzero(monkeypatch):
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _Done(rc=1, err="boom"))
    with pytest.raises(RuntimeError):
        mod.sctl("disable", "--now", "u1")


def test_selector_failure_is_not_swallowed(monkeypatch, tmp_path):
    cfg = tmp_path / "singbox-config.json"
    cfg.write_text(json.dumps({"outbounds": [{"tag": mod.CF_SELECTOR, "default": "old"}]}))
    monkeypatch.setattr(mod, "CONFIG", cfg)

    def boom(*a, **kw):
        raise OSError("connection refused")
    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError):
        mod.set_default("proxy")


def test_no_token_literal_in_source():
    src = _SRC.read_text(encoding="utf-8")
    assert "CLASH_API_TOKEN" in src
    for line in src.splitlines():
        if "Bearer" in line and "{" not in line:
            pytest.fail(f"疑似硬编码凭据：{line.strip()}")


def test_spare_units_tolerates_bad_rows(monkeypatch, tmp_path):
    d = tmp_path / ".warp" / "h2"
    d.mkdir(parents=True)
    (d / "channels.json").write_text(json.dumps([
        {"index": 101, "unit": "keep-out"},
        {"index": 102, "unit": "u-spare"},
        {"index": "not-a-number", "unit": "u-bad"},
        {"unit": "u-no-index"},
    ]))
    monkeypatch.setattr(mod, "PROJECT", tmp_path)
    assert mod.spare_units() == ["u-spare"]


def test_main_returns_nonzero_when_switch_fails(monkeypatch):
    def boom():
        raise RuntimeError("systemctl failed")
    monkeypatch.setattr(mod, "cmd_on", boom)
    monkeypatch.setattr(mod.sys, "argv", ["cf_day_mode.py", "on"])
    assert mod.main() == 1


def test_cmd_on_stops_cf_and_sets_day_default(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "cf_units", lambda: ["u1", "u2"])
    monkeypatch.setattr(mod, "spare_units", lambda: [])
    monkeypatch.setattr(mod, "active_units", lambda p: set())
    monkeypatch.setattr(mod, "sctl", lambda *a: calls.append(("sctl",) + a))
    monkeypatch.setattr(mod, "set_default", lambda n: calls.append(("default", n)))
    mod.cmd_on()
    assert ("sctl", "disable", "--now", "u1", "u2") in calls
    assert ("default", mod.DAY_DEFAULT) in calls
