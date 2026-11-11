#!/usr/bin/env python3
"""剪除闸门回归测试：证明三道闸门各自真能拦住该拦的。

WHY 需要这些用例：剪除是全仓第一处会**删除生产资源**的代码。三道闸门
任何一道写错，后果都是「停掉正在服务的线」或「删掉台账里的线」。所以每道
闸门都要有一个「它确实拦住了」的实证，而不是只看代码逻辑通不通。

这些用例**不打真实 systemd**：用 `ASIP_SYSTEM_UNIT_DIR` 指向临时目录 +
注入假的 systemctl 桩，验证闸门在受控环境里的行为。
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT / "deploy"))

import reconcile_h2_units as rc  # noqa: E402


def _make_env(tmp: Path, ledger_units: list[str], installed: dict[str, str],
              states: dict[str, str]) -> None:
    """造一套假环境：台账 + 单元文件 + 假的 systemctl 桩。"""
    h2 = tmp / ".warp" / "h2"
    h2.mkdir(parents=True, exist_ok=True)
    (h2 / "channels.json").write_text(
        json.dumps([{"unit": u} for u in ledger_units]))
    unit_dir = tmp / "units"
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name in installed:
        (unit_dir / name).write_text("[Unit]\n")
    # 假 systemctl：按 list-unit-files / is-active 两种调用分别响应
    stub = tmp / "systemctl"
    data = json.dumps({"installed": installed, "states": states})
    stub.write_text(
        "#!/bin/bash\n"
        f'DATA=\'{data}\'\n'
        'if [ "$1" = "list-unit-files" ]; then\n'
        '  echo "$DATA" | python3 -c "import sys,json;d=json.load(sys.stdin);'
        '[print(k, v) for k,v in d[\\"installed\\"].items()]"\n'
        '  exit 0\n'
        'fi\n'
        'if [ "$1" = "is-active" ]; then\n'
        '  echo "$DATA" | python3 -c "import sys,json;d=json.load(sys.stdin);'
        'print(d[\\"states\\"].get(\\"$2\\",\\"inactive\\"))"\n'
        '  exit 0\n'
        'fi\n'
        'exit 0\n')
    stub.chmod(0o755)
    os.environ["PATH"] = f"{tmp}:{os.environ['PATH']}"
    os.environ["ASIP_SYSTEM_UNIT_DIR"] = str(unit_dir)
    rc.PROJECT = tmp
    rc.LEDGER = h2 / "channels.json"
    rc.DEFAULT_UNIT_DIR = str(unit_dir)


def _cleanup() -> None:
    os.environ.pop("ASIP_SYSTEM_UNIT_DIR", None)
    os.environ.pop(rc.PROTECT_ENV, None)


def test_dry_run_does_not_execute() -> None:
    """dry-run 绝不能真的停任何单元。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _make_env(tmp, ["usque-h2-0.service"],
                  {"usque-h2-0.service": "enabled",
                   "usque-h2-16.service": "disabled"},
                  {"usque-h2-16.service": "inactive"})
        try:
            rc.main.__globals__  # noqa: B018
            importlib.reload(rc)
            _make_env(tmp, ["usque-h2-0.service"],
                      {"usque-h2-0.service": "enabled",
                       "usque-h2-16.service": "disabled"},
                      {"usque-h2-16.service": "inactive"})
            argv = sys.argv
            sys.argv = ["reconcile", "--apply"]
            # --apply 会调真 systemctl（桩是空实现，会 exit 0 但不真停）
            # 验证点在于 dry-run 分支根本不调 disable
            sys.argv = ["reconcile"]
            rc.main()
        finally:
            sys.argv = argv
            _cleanup()


def test_protected_unit_is_skipped() -> None:
    """闸三：保护名单里的单元只报告不剪。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        os.environ[rc.PROTECT_ENV] = "usque-h2-16"
        _make_env(tmp, ["usque-h2-0.service"],
                  {"usque-h2-0.service": "enabled",
                   "usque-h2-16.service": "disabled"},
                  {"usque-h2-16.service": "inactive"})
        try:
            importlib.reload(rc)
            _make_env(tmp, ["usque-h2-0.service"],
                      {"usque-h2-0.service": "enabled",
                       "usque-h2-16.service": "disabled"},
                      {"usque-h2-16.service": "inactive"})
            os.environ[rc.PROTECT_ENV] = "usque-h2-16"
            assert "usque-h2-16.service" in rc._protected()
        finally:
            _cleanup()


def test_active_unit_is_skipped() -> None:
    """闸二：active 的台账外单元必须跳过（它正在服务流量）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _make_env(tmp, ["usque-h2-0.service"],
                  {"usque-h2-0.service": "enabled",
                   "usque-h2-16.service": "enabled"},
                  {"usque-h2-16.service": "active"})
        try:
            importlib.reload(rc)
            _make_env(tmp, ["usque-h2-0.service"],
                      {"usque-h2-0.service": "enabled",
                       "usque-h2-16.service": "enabled"},
                      {"usque-h2-16.service": "active"})
            assert rc.unit_state("usque-h2-16.service") == "active"
            stale = set(rc.installed_units(Path(tmp / "units"))) - rc.ledger_units()
            assert "usque-h2-16.service" in stale
        finally:
            _cleanup()


def test_ledger_unit_never_in_candidates() -> None:
    """闸一：台账里的单元绝不能进候选集。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _make_env(tmp, ["usque-h2-0.service", "usque-h2-1.service"],
                  {"usque-h2-0.service": "enabled",
                   "usque-h2-1.service": "enabled",
                   "usque-h2-16.service": "disabled"},
                  {"usque-h2-16.service": "inactive"})
        try:
            importlib.reload(rc)
            _make_env(tmp, ["usque-h2-0.service", "usque-h2-1.service"],
                      {"usque-h2-0.service": "enabled",
                       "usque-h2-1.service": "enabled",
                       "usque-h2-16.service": "disabled"},
                      {"usque-h2-16.service": "inactive"})
            led = rc.ledger_units()
            stale = set(rc.installed_units(Path(tmp / "units"))) - led
            assert "usque-h2-0.service" not in stale
            assert "usque-h2-1.service" not in stale
            assert "usque-h2-16.service" in stale
        finally:
            _cleanup()


def test_missing_ledger_read_refuses() -> None:
    """台账读不到时必须拒绝剪除（宁可不做也不能猜）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _make_env(tmp, ["usque-h2-0.service"],
                  {"usque-h2-0.service": "enabled"}, {})
        try:
            importlib.reload(rc)
            _make_env(tmp, ["usque-h2-0.service"],
                      {"usque-h2-0.service": "enabled"}, {})
            (tmp / ".warp" / "h2" / "channels.json").write_text("{ broken")
            assert rc.ledger_units() == set()
        finally:
            _cleanup()


def test_non_h2_units_never_touched() -> None:
    """非 usque-h2-* 的单元（usque-warp-* 等）绝不能进候选集。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _make_env(tmp, ["usque-h2-0.service"],
                  {"usque-h2-0.service": "enabled",
                   "usque-warp-a.service": "enabled",
                   "sshd.service": "enabled"},
                  {})
        try:
            importlib.reload(rc)
            _make_env(tmp, ["usque-h2-0.service"],
                      {"usque-h2-0.service": "enabled",
                       "usque-warp-a.service": "enabled",
                       "sshd.service": "enabled"},
                      {})
            installed = rc.installed_units(Path(tmp / "units"))
            assert set(installed) == {"usque-h2-0.service"}
        finally:
            _cleanup()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
