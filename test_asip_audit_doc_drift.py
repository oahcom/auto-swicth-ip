"""`tools/asip_audit.py` 文档漂移关卡（--doc-drift）与生效面对账（--runtime-drift）
的契约测试（E 项，T6；设计文档 docs/design-20261113-config-drift.md §4.1）。

只盯四件事：

① **改错必炸**（T6）：临时副本里把 `rate429_guard_sec` 的声明值改错 →
   退出 1 并打印明细 —— FR-12/R-9 两次复发都是「改了配置没改文档散文」，
   这个关卡就是让它再也过不了门。
② **没改不炸**：声明与运行值一致 → 退出 0。
③ **无声明可比对时的行为锁住**：文档里一条声明都没有 → 退出 0 且明确
   说明「无声明可比对」，而不是含糊地报通过或报错。
④ **「无法判定」不算通过**：运行时 API 读不到时 --runtime-drift 必须
   明确输出「无法判定」并退出 1 —— 绝不假阴性（设计 T5）。

另锁一条设计选择：文件名带 8 位日期戳的**历史归档文档**（事故/评审/
设计记录）保留历史数值是其职责，不参与比对 —— 拿 `docs/dev-design-*`
里的旧值150 去报漂移是假警报。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from config import CFG

PROJECT = Path(__file__).resolve().parent
TOOL = PROJECT / "tools" / "asip_audit.py"


def _run(project: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--project", str(project), *args],
        capture_output=True, text=True, timeout=180)


def _mod():
    spec = importlib.util.spec_from_file_location("asip_audit_docdrift", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _mod()


def _write(proj: Path, name: str, text: str) -> Path:
    p = proj / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _actual(key: str):
    """运行值（与工具同源：config.CFG）。测试跟着配置走，不写死数字。"""
    if key == "production_429_ban_sec":
        return CFG["api"][key]
    return CFG["warp"][key]


# ------------------------------------------------------------ ① 改错必炸


def test_wrong_declaration_exits_1_and_prints_detail(tmp_path):
    """T6 核心：rate429_guard_sec 的声明值改错 → 退出 1，打印声明值与实际值。"""
    proj = tmp_path / "proj"
    bad = int(_actual("rate429_guard_sec")) + 12345
    _write(proj, "CLAUDE.md", f"冷却窗口 `rate429_guard_sec`={bad}，精确值经 ban_until 覆盖。\n")

    r = _run(proj, "--doc-drift")
    assert r.returncode == 1, f"声明值改错必须阻断：{r.stdout[-500:]}"
    assert "rate429_guard_sec" in r.stdout
    assert f"声明={bad}" in r.stdout
    assert f"实际={_actual('rate429_guard_sec')}" in r.stdout


def test_wrong_prose_threshold_exits_1(tmp_path):
    """「满 N 格」散文声明改错（FR-12/R-9 的复发形态）→ 退出 1。"""
    proj = tmp_path / "proj"
    bad = int(_actual("proactive_after_dsv4_ok")) + 7
    _write(proj, "CLAUDE.md", f"主动轮换：requests 满 {bad} 格 dsv4（当前）\n")

    r = _run(proj, "--doc-drift")
    assert r.returncode == 1
    assert "proactive_after_dsv4_ok" in r.stdout


def test_wrong_proactive_mode_exits_1(tmp_path):
    """proactive_mode 字符串声明（当前生效标记）写反 → 退出 1。"""
    proj = tmp_path / "proj"
    actual = str(_actual("proactive_mode"))
    wrong = "fixed" if actual == "requests" else "requests"
    _write(proj, "CLAUDE.md", f"判据二选一：`{wrong}`（当前生效）\n")

    r = _run(proj, "--doc-drift")
    assert r.returncode == 1
    assert "proactive_mode" in r.stdout


def test_doc_drift_json_shape(tmp_path):
    """--json 给机器可读的 mismatches 明细（挂 timer/门禁用）。"""
    proj = tmp_path / "proj"
    bad = int(_actual("blocked_guard_sec")) + 1
    _write(proj, "CLAUDE.md", f"`blocked_guard_sec`={bad}\n")

    r = _run(proj, "--doc-drift", "--json")
    assert r.returncode == 1
    rep = json.loads(r.stdout)
    assert rep["mismatches"] and rep["mismatches"][0]["key"] == "blocked_guard_sec"
    assert rep["mismatches"][0]["declared"] == bad
    assert rep["read_only"] is True


# ------------------------------------------------------------ ② 没改不炸


def test_matching_declarations_exit_0(tmp_path):
    """声明与运行值一致 → 退出 0。"""
    proj = tmp_path / "proj"
    _write(proj, "CLAUDE.md",
           f"冷却 `rate429_guard_sec`={_actual('rate429_guard_sec')}\n"
           f"`blocked_ttl_sec`={_actual('blocked_ttl_sec')}\n")

    r = _run(proj, "--doc-drift")
    assert r.returncode == 0, r.stdout[-500:]


def test_matching_prose_declarations_exit_0(tmp_path):
    """散文形态（满 N 格 / （当前生效））与运行值一致 → 退出 0。"""
    proj = tmp_path / "proj"
    _write(proj, "CLAUDE.md",
           f"主动轮换：{_actual('proactive_mode')} "
           f"满 {_actual('proactive_after_dsv4_ok')} 格 dsv4（当前）\n")

    r = _run(proj, "--doc-drift")
    assert r.returncode == 0, r.stdout[-500:]


def test_seconds_and_humanized_units_both_count(tmp_path):
    """86400s 与 24h 是同一个声明值 —— 单位换算后比对。"""
    proj = tmp_path / "proj"
    sec = int(_actual("ip_reuse_guard_sec"))
    assert sec % 3600 == 0, "用例前提：ip_reuse_guard_sec 是整小时"
    _write(proj, "CLAUDE.md",
           f"`ip_reuse_guard_sec`（默认 **{sec}s = {sec // 3600}h**）\n")

    r = _run(proj, "--doc-drift")
    assert r.returncode == 0, r.stdout[-500:]


# -------------------------------------- ③ 无声明可比对时的行为（锁住）


def test_no_declarations_exits_0_and_says_so(tmp_path):
    """一条声明都找不到 → 退出 0，且明确写「无声明可比对」，不装作比对通过。"""
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    _write(proj, "CLAUDE.md", "本文档不含任何配置值声明。\n")

    r = _run(proj, "--doc-drift")
    assert r.returncode == 0
    assert "无声明可比对" in r.stdout


# --------------------------- 设计选择锁住：历史归档文档不参与比对


def test_dated_historical_docs_are_skipped(tmp_path):
    """文件名带 8 位日期戳的归档文档保留历史数值，不算漂移。"""
    proj = tmp_path / "proj"
    bad = int(_actual("rate429_guard_sec")) + 999
    # 归档文档：写的是当年的旧值（有意保留的史料）
    _write(proj, "docs/dev-design-20261028-old.md",
           f"当时 `rate429_guard_sec`={bad}\n")
    # 活文档：声明正确
    _write(proj, "CLAUDE.md",
           f"`rate429_guard_sec`={_actual('rate429_guard_sec')}\n")

    r = _run(proj, "--doc-drift")
    assert r.returncode == 0, f"历史归档不该被比对：{r.stdout[-500:]}"
    assert "dev-design-20261028-old.md" in r.stdout  # 报告里列为「跳过」


# --------------------------- ④ --runtime-drift：无法判定不算通过


def _fake_face(tmp_path: Path) -> dict:
    cfg = tmp_path / "sel.json"
    cfg.write_text(json.dumps({"outbounds": [
        {"tag": "opencode-out", "outbounds": ["WARP-A", "WARP-B"]},
        {"tag": "WARP-A"}, {"tag": "WARP-B"},
    ]}), encoding="utf-8")
    return {"name": "slot-test", "selector": "opencode-out",
            "config_file": str(cfg), "base_url": "http://127.0.0.1:1"}


def _run_runtime_inprocess(mod, monkeypatch, tmp_path, capsys,
                           runtime, known):
    face = _fake_face(tmp_path)
    monkeypatch.setattr(mod, "iter_selector_faces", lambda: [face])
    monkeypatch.setattr(mod, "read_members",
                        lambda: ({t: {} for t in known}, []))
    monkeypatch.setattr(mod, "read_runtime_members",
                        lambda *a, **k: runtime)
    monkeypatch.setattr(sys, "argv",
                        ["asip_audit", "--project", str(tmp_path),
                         "--runtime-drift"])
    rc = mod.main()
    return rc, capsys.readouterr().out


def test_runtime_unknown_is_not_pass(mod, monkeypatch, tmp_path, capsys):
    """API 读不到（None）→ 输出「无法判定」且退出 1，绝不报一致。"""
    rc, out = _run_runtime_inprocess(mod, monkeypatch, tmp_path, capsys,
                                     runtime=None, known={"WARP-A", "WARP-B"})
    assert rc == 1
    assert "无法判定" in out


def test_runtime_consistent_exits_0(mod, monkeypatch, tmp_path, capsys):
    """产物面与生效面一致 → 退出 0。"""
    rc, out = _run_runtime_inprocess(mod, monkeypatch, tmp_path, capsys,
                                     runtime={"WARP-A", "WARP-B"},
                                     known={"WARP-A", "WARP-B"})
    assert rc == 0, out[-500:]
    assert "一致" in out


def test_runtime_drift_reports_both_gap_sets(mod, monkeypatch, tmp_path,
                                             capsys):
    """缺失集 + 多余集都要报出来，退出 1；占位 tag 不进差集。"""
    rc, out = _run_runtime_inprocess(
        mod, monkeypatch, tmp_path, capsys,
        runtime={"WARP-B", "WARP-C", "slot-reserve"},
        known={"WARP-A", "WARP-B", "WARP-C"})
    assert rc == 1
    assert "WARP-A" in out          # 产物面有、生效面缺
    assert "WARP-C" in out          # 生效面有、产物面缺
    assert "slot-reserve" not in out  # 占位 tag 不构成漂移（防误杀）


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
