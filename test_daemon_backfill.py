"""补位设备线的挑选判据（daemon._backfill_spare_indices）。

用 AST 抽函数体，不 import daemon（会拉起线程和全局状态）。
"""
import ast
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent


NAMES = ("_wg_line_state", "_backfill_spare_indices")


def _fn():
    """连同 `_wg_line_state` 一起抽 —— 判据已经合并到那一份。"""
    sys.path.insert(0, str(PROJECT))
    tree = ast.parse((PROJECT / "daemon.py").read_text())
    picked = [n for n in tree.body if getattr(n, "name", None) in NAMES]
    assert len(picked) == len(NAMES), "daemon.py 里找不到判据函数"
    return picked


def _mod(monkeypatch, tmp_path, registered, members, units):
    """伪造设备目录与单元文件。

    `registered` = 有 profile.conf 的线号
    `members`    = 已在 members 里（正在服务）的 tag
    `units`      = {线号: "installed" | "masked" | "missing"}
    """
    import warp_wg
    wgdir = tmp_path / "wglines"
    for i in registered:
        d = wgdir / f"line{i}"
        d.mkdir(parents=True)
        (d / "profile.conf").write_text("[Interface]\n")
    monkeypatch.setattr(warp_wg, "line_dir", lambda i: wgdir / f"line{i}")

    unitdir = tmp_path / "systemd"
    unitdir.mkdir()
    for i, kind in units.items():
        p = unitdir / f"warp-wg-{i}.service"
        if kind == "installed":
            p.write_text("[Unit]\n")
        elif kind == "masked":
            p.symlink_to("/dev/null")
        # "missing" → 不创建

    box = {
        "os": os,
        "warp_wg": warp_wg,
        "_warp": lambda: {"members": [{"tag": t} for t in members]},
    }
    real = os.path.exists

    def fake_exists(p):
        # 把 /etc/systemd/system/... 重定向到 tmp（daemon 里是硬编码路径）
        name = os.path.basename(str(p))
        if str(p).startswith("/etc/systemd/system/"):
            q = unitdir / name
            return real(q)
        return real(p)

    def fake_realpath(p):
        name = os.path.basename(str(p))
        if str(p).startswith("/etc/systemd/system/"):
            q = unitdir / name
            if q.is_symlink():
                return "/dev/null" if os.readlink(q) == "/dev/null" else str(q)
            return str(q) if real(q) else str(q)
        return os.path.realpath(p)

    box["os"] = type("O", (), {
        "path": type("P", (), {"exists": staticmethod(fake_exists),
                               "realpath": staticmethod(fake_realpath)}),
        "realpath": fake_realpath,
        "path_exists": fake_exists,
    })()
    exec(compile(ast.Module(body=_fn(), type_ignores=[]), "daemon.py", "exec"),
         box)
    return box["_backfill_spare_indices"]()


def test_line_without_unit_is_not_picked(monkeypatch, tmp_path):
    """注册了但**单元没装**的线不能当补位候选。

    实测 2026-11-04：`line30` 的 profile.conf 在、单元从没生成过。补位挑中它
    → `Unit warp-wg-30.service not found` → `continue` 到下一轮又挑中同一个
    30（27 被 mask、28/29 已在 members），补位永远开不起来。
    """
    got = _mod(monkeypatch, tmp_path,
               registered=[1, 2, 30], members=[],
               units={1: "installed", 2: "installed", 30: "missing"})
    assert 30 not in got, "单元没装的线被当成可用补位"
    assert got == [1, 2]


def test_masked_line_is_not_picked(monkeypatch, tmp_path):
    """mask 的单元（软链到 /dev/null）永远起不来，必须排除。"""
    got = _mod(monkeypatch, tmp_path,
               registered=[1, 27], members=[],
               units={1: "installed", 27: "masked"})
    assert 27 not in got
    assert got == [1]


def test_lines_already_in_members_are_skipped(monkeypatch, tmp_path):
    """正在服务流量的线不是「空闲」—— 再开一条会抢同一台设备。"""
    got = _mod(monkeypatch, tmp_path,
               registered=[1, 2, 3],
               members=["WARP-WG-1", "WARP-WG-2"],
               units={1: "installed", 2: "installed", 3: "installed"})
    assert got == [3]


def test_fully_exhausted_pool_returns_empty(monkeypatch, tmp_path):
    """没有可用的线时返回空表 —— 调用方据此报「无法补位」，
    而不是挑一条注定失败的。"""
    got = _mod(monkeypatch, tmp_path,
               registered=[27, 30], members=[],
               units={27: "masked", 30: "missing"})
    assert got == []
