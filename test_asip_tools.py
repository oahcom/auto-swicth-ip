"""asip/ 复用工具的确定性单测（无网络）。"""
import os

import pytest

from asip import clash_client
from asip.atomic_io import read_json, write_json
from asip.clash_client import ClashClient
from asip.redact import redact_secrets, redact_url


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_clash_select_treats_200_and_204_as_success(monkeypatch):
    for status in (200, 204):
        monkeypatch.setattr(clash_client.requests, "request",
                            lambda *a, _s=status, **k: _Resp(_s))
        assert ClashClient("http://x", lambda: "s").select("sel", "n") is True


def test_clash_select_rejects_http_error_and_transport_error(monkeypatch):
    monkeypatch.setattr(clash_client.requests, "request",
                        lambda *a, **k: _Resp(500))
    assert ClashClient("http://x", lambda: "s").select("sel", "n") is False

    def boom(*a, **k):
        raise clash_client.requests.RequestException("down")

    monkeypatch.setattr(clash_client.requests, "request", boom)
    assert ClashClient("http://x", lambda: "s").select("sel", "n") is False


def test_clash_client_sends_rotated_secret(monkeypatch):
    seen = []

    def fake(method, url, json=None, headers=None, timeout=None):
        seen.append(headers)
        return _Resp(200, {"now": "n", "all": ["n"]})

    monkeypatch.setattr(clash_client.requests, "request", fake)
    secret = "old"
    c = ClashClient("http://x", lambda: secret)
    assert c.current("sel") == "n"
    secret = "new"
    assert c.current("sel") == "n"
    assert seen[0]["Authorization"] == "Bearer old"
    assert seen[1]["Authorization"] == "Bearer new"


def test_redact_hides_query_path_bearer_and_userinfo():
    text = ("GET https://h/pq/0123456789abcdef?token=deadbeef failed; "
            "Authorization: Bearer abc.def; https://u:p@host/x")
    out = redact_secrets(text)
    assert "deadbeef" not in out and "0123456789abcdef" not in out
    assert "abc.def" not in out and "u:p@" not in out
    assert redact_url("https://h/pq/0123456789abcdef?token=x") == "https://h/***"


def test_write_json_is_private_and_readable(tmp_path):
    path = tmp_path / "state.json"
    write_json(str(path), {"a": 1})
    assert read_json(str(path)) == {"a": 1}
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("state.json.*.tmp"))


def test_probe_tool_registers_its_own_device(tmp_path, monkeypatch):
    """没有探测设备时必须**自己注册**，而不是报错退出。

    文档写着 `usque-probe{1..3}` 是「asip 自己注册的探测设备」，但全库此前
    **没有任何一处真的去注册它们**。远端 2026-11-05 实测 `--new-only` 直接
    报「没有可用的探测设备」—— 而 daemon 唯一的 H2 探测入口就是它，于是
    block-map 永远补不上新节点、`want_n` 永远小于设备数、自动建线永不触发。

    这里走 `main()`，不是直接调 `_register_probe_device`：要盯的是**调用点**
    存在，光测函数本身挡不住「分支被删掉」。
    """
    import importlib.util
    import sys
    from pathlib import Path

    proj = Path(__file__).resolve().parent
    sys.path.insert(0, str(proj))
    spec = importlib.util.spec_from_file_location(
        "probe_h2_nodes", proj / "tools" / "probe_h2_nodes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    (tmp_path / ".warp" / "usque").mkdir(parents=True)
    fake_bin = tmp_path / ".warp" / "usque" / "usque"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    (tmp_path / "singbox-config.json").write_text('{"outbounds": []}')

    registered = []

    class _R:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(argv, **kw):
        registered.append(argv)
        if kw.get("cwd"):
            (Path(kw["cwd"]) / "config.json").write_text('{"id":"x"}')
        return _R()

    monkeypatch.setattr(mod, "PROJECT", tmp_path)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv",
                        ["probe", "--new-only", "--devices",
                         str(tmp_path / ".warp" / "usque-probe1"),
                         "--out", str(tmp_path / "out.json")])
    mod.main()
    assert any(a[:2] == ["./usque", "register"] for a in registered), \
        f"缺探测设备时应自动注册，实际调用 {registered}"
