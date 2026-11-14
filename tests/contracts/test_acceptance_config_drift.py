"""③组验收测试 · 注释/配置/行为漂移（修前红、修后绿）。

【三分法】C 已知坏行为：漂移修「取值来源/文案口径」，**禁止钉成契约**——
本组断言的是「三方一致」而非任何一方的具体值；裁决方向（注释错/配置错/
行为错）以 P1「注释 vs 配置 vs 行为」三方核对报告为准，两边对齐即转绿。
【防什么事故】取值纪律被注释带偏（漂移实证：30s vs 1800、60% vs 0.3）——
按注释写死断言/参数 → 与真实行为脱节，事故记忆被下次事故改写。
【事故出处】任务书取值纪律；M13；附件 D「line_ok 窗口」左栏。
【live 复核（2026-11-14）】
  - line_ok 窗口：_probe_ok_recent docstring 仍写「窗口默认 30s」，而生效值
    是调用点 cfg.get("transport_line_ok_window_sec", 1800)（调用点注释已更正
    「旧默认 30s 等于永不命中」，docstring 漏改）。
  - bad_nodes 占比阀：_POOL_MIN_RATIO=0.3（允许 70% 拉黑），而导出跳过分支
    的日志文案硬编码「>60%」（10531 行），与判据 (1-0.3) 不符。
"""
import inspect
import os
import re
from pathlib import Path

import pytest

import daemon as d


def test_line_ok_window_doc_matches_effective_default():
    """_probe_ok_recent 的默认窗口陈述必须与调用点生效默认一致（30s vs 1800 漂移）。

    修前红：docstring「30s」vs 生效 1800。修后绿：两边对齐（无论改注释还是
    改配置，以三方核对裁决为准）。
    """
    fn_src = inspect.getsource(d._probe_ok_recent)
    m = re.search(r"窗口默认\s*(\d+)\s*s", fn_src)
    assert m, "docstring 应陈述默认窗口（口径需可核）"
    doc_val = float(m.group(1))
    # 生效默认：调用点 cfg.get 兜底值（config.py 未定义该键时即生效值）
    slot_src = inspect.getsource(d._rotate_slot_once)
    m2 = re.search(r'transport_line_ok_window_sec",\s*(\d+)', slot_src)
    assert m2
    effective = float(d._warp().get("transport_line_ok_window_sec",
                                    m2.group(1)))
    assert doc_val == effective, (
        f"漂移：docstring 声明 {doc_val}s，生效 {effective}s")


def test_pool_ratio_log_matches_effective_threshold():
    """bad_nodes 跳过导出日志里的百分比必须与 (1-_POOL_MIN_RATIO) 判据一致（60% vs 0.3 漂移）。

    修前红：日志硬编码「>60%」，判据是 70%。修后绿：文案与判据同源。
    """
    src = inspect.getsource(d._export_dead_nodes_locked)
    m = re.search(r"死节点\s*>(\d+)%", src)
    assert m, "跳过导出分支应给出阈值口径"
    log_pct = float(m.group(1))
    threshold_pct = (1 - d._POOL_MIN_RATIO) * 100
    assert log_pct == pytest.approx(threshold_pct), (
        f"漂移：日志写 {log_pct}%，判据是 {threshold_pct:.0f}%"
        f"（_POOL_MIN_RATIO={d._POOL_MIN_RATIO}）")


def test_probe_ok_recent_docstring_not_stale_on_ttl_alignment():
    """「窗口对齐探针结论缓存 TTL」的注释口径与生效值一致（1800 对 1800）。"""
    w = d._warp()
    effective = float(w.get("transport_line_ok_window_sec", 1800))
    ttl = float(w.get("probe_ok_ttl_sec", 1800))
    assert effective == ttl, (
        f"line_ok 窗口 {effective}s 与探针缓存 TTL {ttl}s 失配")
