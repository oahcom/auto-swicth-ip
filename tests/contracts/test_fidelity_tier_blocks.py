"""①组保真契约 · 档位 tier None≠"" 语义与档位对账顺序（附件 A10、附件 B5）。

【防什么事故】
  - None 与 "" 归一 → 全池停机：`h2-only` 档位合法写 `wg: ""`（22 条 WG 全
    停），而「没有档位文件」必须表示全开；两种意思混用 → 全新远端/坏文件
    把 22 条 WG 一条不剩地判停用（附件 A10）。
  - 档位对账（_tier_reconcile）排在状态加载之前 → 对账的整表落盘把未加载
    的内存表写成 1 条（2026-11-11 实测复现，附件 B5）。
【事故出处】附件 A10/A4；附件 B5。
【三分法】A 保真契约。
"""
import json

import pytest

import warp_blocks
import daemon as d


@pytest.fixture
def tiers_dir(monkeypatch, tmp_path):
    """tiers.json 指到 tmp（写域只落 tmp）。"""
    fake_map = tmp_path / "block-map.json"
    monkeypatch.setattr(warp_blocks, "map_path", lambda: fake_map)
    return tmp_path


def test_missing_tiers_file_means_all_enabled(tiers_dir):
    """缺失档位文件 = (None, None) = 全开，不是「全停」（附件 A10）。"""
    assert warp_blocks.tier_spec() == (None, None)
    declared = {"WARP-WG-1", "WARP-WG-2"}
    assert warp_blocks.tier_disabled(declared) == set()


def test_empty_wg_spec_means_all_disabled(tiers_dir):
    """合法档位 `wg: ""` = 一条不留 = 全停；与 None 严格区分（附件 A10）。"""
    (tiers_dir / "tiers.json").write_text(json.dumps({
        "current": "h2-only", "tiers": {"h2-only": {"wg": ""}}}))
    cur, spec = warp_blocks.tier_spec()
    assert cur == "h2-only" and spec == ""  # "" 不是 None
    declared = {"WARP-WG-1", "WARP-WG-2"}
    assert warp_blocks.tier_disabled(declared) == declared  # 全停


def test_partial_spec_disables_only_outside(tiers_dir):
    """部分保留档位只停档位外的线（对账口径：档位只表达谁该跑）。"""
    (tiers_dir / "tiers.json").write_text(json.dumps({
        "current": "wg-1-2", "tiers": {"wg-1-2": {"wg": "1-2"}}}))
    declared = {"WARP-WG-1", "WARP-WG-2", "WARP-WG-3"}
    assert warp_blocks.tier_disabled(declared) == {"WARP-WG-3"}


def test_corrupt_tiers_file_treated_like_missing(tiers_dir):
    """坏文件一律 (None, None)：宁可全开，不把整批线冻死（附件 A10）。"""
    (tiers_dir / "tiers.json").write_text("{not json")
    assert warp_blocks.tier_spec() == (None, None)


def test_reconcile_runs_after_state_load():
    """档位对账排在 _load_warp_line_state() 之后（附件 B5，顺序即语义）。

    防什么事故：对账解禁侧会清观测并整表落盘；状态表没加载就落盘 =
    156 条被覆盖成 1 条（2026-11-11 /tmp 3 条复现）。兜底=缩水拒写
    （见 test_fidelity_ledger_guards.py），本条钉启动顺序本身。
    """
    src = d.__file__
    with open(src, encoding="utf-8") as f:
        text = f.read()
    anchor = "auto-switch-ip daemon v2 启动"
    region = text[text.index(anchor):text.index(anchor) + 4000]
    i_load = region.index("_load_warp_line_state()")
    i_reconcile = region.index("_tier_reconcile()")
    assert i_load < i_reconcile
