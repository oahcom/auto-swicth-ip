"""H2 渲染台账变更后，daemon 名单必须热跟随 —— 覆盖 2026-11-11 的真事故。

事故链（实测数据）：
  19:13:35  `_run_h2_sync` 调 render_h2_lines.py 重渲染，台账 112 条里 56 条
            的编号从 0~111 换成 112~167（NRT/HKG/ICN/SLC 等）。
  之后       daemon 内存里的 members 仍是 import 期那份旧台账。
  结果一     `_warp_member("WARP-H2-NRT-112")` 返回 None → 这条线不在换线
            候选、不会被 `_warp_line_restart` 按名重启、日志里从不出现它的名字。
  结果二     `_scan_running_tunnels` 的 declared 查不到 usque-h2-112.service →
             退回 `WARP-H2-SCAN-112` 假身份探测。状态台账里 103 条 SCAN 记录，
             其中 56 条正是这批真成员。
  外部症状   「blocked 之后再没被探测/重启」+「一直无样本、原因说不清」。

每个测试都写成「回滚修复即失败」。
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))


def _load_daemon(monkeypatch, tmp_path, ledger_rows=None):
    """加载 daemon 模块；H2 台账指向 tmp 下的假文件。"""
    warp = tmp_path / ".warp" / "h2"
    warp.mkdir(parents=True)
    ledger = warp / "channels.json"
    if ledger_rows is not None:
        ledger.write_text(json.dumps(ledger_rows), encoding="utf-8")
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
    # daemon 的 CFG 是 import 期读**生产台账**得到的（config._append_h2_members）。
    # 这里把 mtime 归零后强制刷一次，让名单与 tmp 台账对齐 —— 否则每个用例的
    # 起点都是「生产 112 条 + 测试伪造 1 条」的混合态，断言毫无意义。
    mod._H2_CHANNELS_MTIME = 0.0
    mod._refresh_h2_members()
    return mod, ledger


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


def _row(idx, tag, socks, unit=None):
    return {"index": idx, "member_tag": tag, "socks_port": socks,
            "unit": unit or f"usque-h2-{idx}.service", "colo": "NRT"}


# ------------------------------------------------------- ① 名单热跟随

def test_member_list_follows_ledger_rename(monkeypatch, tmp_path):
    """台账编号变了，members 里的 H2 段必须跟着换。

    回滚即失败：不调 `_refresh_h2_members` 时 members 仍是旧的 WARP-H2-NRT-0。
    """
    mod, ledger = _load_daemon(monkeypatch, tmp_path,
                               [_row(0, "WARP-H2-NRT-0", 1180)])
    h2 = [m for m in mod._warp()["members"] if m.get("kind") == "h2"]
    assert [m["tag"] for m in h2] == ["WARP-H2-NRT-0"]

    ledger.write_text(json.dumps([_row(112, "WARP-H2-NRT-112", 1232)]),
                      encoding="utf-8")
    os.utime(ledger, (1, 1))          # 同秒写两次 mtime 相同，强制拉开
    assert mod._refresh_h2_members() is True

    h2 = [m for m in mod._warp()["members"] if m.get("kind") == "h2"]
    assert [m["tag"] for m in h2] == ["WARP-H2-NRT-112"], \
        "台账重排后名单必须热跟随，否则新线进不了换线候选"
    assert mod._warp_member("WARP-H2-NRT-112") is not None
    assert mod._warp_member("WARP-H2-NRT-0") is None


def test_non_h2_members_survive_refresh(monkeypatch, tmp_path):
    """刷新只管 H2 段：WG/CF 成员一条都不能少。

    回滚即失败：若实现里 `members = fresh`（整体替换）而不是切片赋值，
    WARP-CF / WARP-WG-* 全部消失。
    """
    mod, ledger = _load_daemon(monkeypatch, tmp_path,
                               [_row(0, "WARP-H2-NRT-0", 1180)])
    others = {m["tag"] for m in mod._warp()["members"] if m.get("kind") != "h2"}
    assert others, "前置：应存在非 H2 成员（CF/WG）"
    ledger.write_text(json.dumps([_row(112, "WARP-H2-NRT-112", 1232)]),
                      encoding="utf-8")
    os.utime(ledger, (1, 1))
    mod._refresh_h2_members()
    still = {m["tag"] for m in mod._warp()["members"] if m.get("kind") != "h2"}
    assert still == others, "刷新 H2 段不能波及 CF/WG 成员"


def test_refresh_is_noop_when_mtime_unchanged(monkeypatch, tmp_path):
    """台账没变就不动名单（避免每 5s 刷一条日志）。"""
    mod, ledger = _load_daemon(monkeypatch, tmp_path,
                               [_row(0, "WARP-H2-NRT-0", 1180)])
    assert mod._refresh_h2_members() is False
    assert mod._refresh_h2_members() is False


def test_unreadable_ledger_keeps_members(monkeypatch, tmp_path):
    """台账被截断（读到半截 JSON）时**保持现状**，不许把 H2 段清空。

    回滚即失败：若实现里不做空列表检查，members 的 H2 段会变成空 —— 换线
    候选、探测目标、`members[0]` 兜底全部跟着塌。
    """
    mod, ledger = _load_daemon(monkeypatch, tmp_path,
                               [_row(0, "WARP-H2-NRT-0", 1180)])
    before = [m["tag"] for m in mod._warp()["members"] if m.get("kind") == "h2"]
    assert before
    ledger.write_text('{"truncated": ', encoding="utf-8")
    os.utime(ledger, (1, 1))
    assert mod._refresh_h2_members() is False
    after = [m["tag"] for m in mod._warp()["members"] if m.get("kind") == "h2"]
    assert after == before, "台账读不出来时必须保持原名单，不能清空"


# --------------------------------------------- ② 假身份结论的清理

def test_scan_state_dropped_once_unit_is_declared(monkeypatch, tmp_path):
    """单元已归到声明 tag 名下 → 同名 SCAN 结论必须丢掉。

    回滚即失败：不清时报表会把 SCAN-112 显示成 OK，而换线永远查不到这个
    tag —— 「有线、有结论、却没人能选它」。
    """
    mod, ledger = _load_daemon(monkeypatch, tmp_path,
                               [_row(0, "WARP-H2-NRT-0", 1180)])
    mod._warp_line_state_locked("WARP-H2-SCAN-112").update(
        {"last_verdict": "OK", "last_ok_ip": "2a09::1"})
    ledger.write_text(json.dumps([_row(112, "WARP-H2-NRT-112", 1232)]),
                      encoding="utf-8")
    os.utime(ledger, (1, 1))
    mod._refresh_h2_members()
    assert mod._warp_line_state("WARP-H2-SCAN-112").get("last_verdict") != "OK", \
        "单元已有声明归属，假身份下的结论必须清掉"


def test_scan_state_kept_when_unit_still_undeclared(monkeypatch, tmp_path):
    """反向：单元仍无声明归属（真·台账外单元）时，SCAN 结论必须保留。

    回滚即失败：若清理逻辑不比对 service，会把所有 SCAN 结论一律删掉 ——
    台账外的线就彻底没有观测记录了。
    """
    mod, ledger = _load_daemon(monkeypatch, tmp_path,
                               [_row(0, "WARP-H2-NRT-0", 1180)])
    mod._warp_line_state_locked("WARP-H2-SCAN-999").update(
        {"last_verdict": "OK", "last_ok_ip": "2a09::9"})
    ledger.write_text(json.dumps([_row(112, "WARP-H2-NRT-112", 1232)]),
                      encoding="utf-8")
    os.utime(ledger, (1, 1))
    mod._refresh_h2_members()
    assert mod._warp_line_state("WARP-H2-SCAN-999").get("last_verdict") == "OK", \
        "台账外单元的观测结论不能被误删"


# --------------------------------------------- ③ 端口与缓存

def test_declared_port_used_not_magic_base(monkeypatch, tmp_path):
    """刷新后成员的 port 必须来自台账 socks_port，而不是 1180+index。

    实测 usque-h2-135 的 socks 口 1315 与 1180+135 恰好相等是巧合（两端
    都按 8 递增、起点不同）。台账换设备后两者会脱钩，按编号推端口就会探到
    一个没人监听的端口 → 永远 port_closed。
    """
    mod, ledger = _load_daemon(monkeypatch, tmp_path,
                               [_row(0, "WARP-H2-NRT-0", 1180)])
    ledger.write_text(json.dumps([_row(112, "WARP-H2-NRT-112", 2001)]),
                      encoding="utf-8")
    os.utime(ledger, (1, 1))
    mod._refresh_h2_members()
    m = mod._warp_member("WARP-H2-NRT-112")
    assert m is not None and int(m["port"]) == 2001, \
        "端口必须取台账的 socks_port，不能按编号推算"


def test_scan_cache_invalidated_on_refresh(monkeypatch, tmp_path):
    """刷新后必须清 `_TUNNEL_SCAN_TS`，否则还会用旧 tag 探一轮（60s TTL）。"""
    mod, ledger = _load_daemon(monkeypatch, tmp_path,
                               [_row(0, "WARP-H2-NRT-0", 1180)])
    mod._TUNNEL_SCAN_TS = 9e9
    ledger.write_text(json.dumps([_row(112, "WARP-H2-NRT-112", 1232)]),
                      encoding="utf-8")
    os.utime(ledger, (1, 1))
    mod._refresh_h2_members()
    assert mod._TUNNEL_SCAN_TS == 0.0, "扫描缓存必须失效，否则 60s 内仍用旧 tag"
