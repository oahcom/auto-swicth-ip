"""deploy/probe_node_delay.py 的离线用例（纯人工工具，不进 daemon 链路）。

只锁两件事：标签解析 / 超时的返回语义，以及输出文件的形状。
写入目标用 tmp_path 重定向，绝不碰真实的 .warp/h2/node_delay.json。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "deploy" / "probe_node_delay.py"
_spec = importlib.util.spec_from_file_location("probe_node_delay", _SRC)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class _Sock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_bad_tag_returns_none():
    assert mod.probe("no-port-here") is None


def test_all_attempts_failing_returns_none(monkeypatch):
    def boom(*a, **kw):
        raise OSError("refused")
    monkeypatch.setattr(mod.socket, "create_connection", boom)
    assert mod.probe("1.2.3.4:443", tries=2, timeout=0.01) is None


def test_successful_handshake_returns_ms(monkeypatch):
    monkeypatch.setattr(mod.socket, "create_connection", lambda *a, **kw: _Sock())
    ms = mod.probe("1.2.3.4:443", tries=3, timeout=0.01)
    assert isinstance(ms, float)
    assert ms >= 0.0


def test_main_writes_expected_shape(monkeypatch, tmp_path):
    (tmp_path / ".warp" / "h2").mkdir(parents=True)
    (tmp_path / ".warp" / "block-map.json").write_text(json.dumps({
        "nodes": {
            "h2-a:443": {"carrier": "h2"},
            "h2-b:443": {"carrier": "h2"},
            "wg-c:2408::1": {"carrier": "warp_wg"},
        }
    }))
    monkeypatch.setattr(mod, "PROJECT", tmp_path)
    monkeypatch.setattr(mod, "OUT", tmp_path / ".warp" / "h2" / "node_delay.json")
    monkeypatch.setattr(mod.socket, "create_connection", lambda *a, **kw: _Sock())
    monkeypatch.setattr(sys, "argv", ["probe_node_delay.py", "--limit", "2"])
    assert mod.main() == 0
    got = json.loads((tmp_path / ".warp" / "h2" / "node_delay.json").read_text())
    assert set(got) == {"updated", "delays"}
    assert set(got["delays"]) == {"h2-a:443", "h2-b:443"}
