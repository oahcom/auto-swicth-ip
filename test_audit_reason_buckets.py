"""成因标签分档 —— 覆盖 2026-11-11「无样本不标原因」的最后一层。

真实现象：巡检报告里 22 条 WG 线报「从未探测」、8 条补位池报「不在探测名单」。
两者都不是故障：
  · 22 条是 `.warp/tiers.json` 的 h2-only 档位关掉的（设计如此）；
  · 8 条是 WG-23..30 补位池（设计上的闲置池，`config.py` 注释写明不该探）。
把「设计如此」混进「该探没探」，读报告的人就没法从桶里看出真问题 —— 这正是
「一直无样本、原因说不清」的最后一环。

断言的都是**标签**，不是实现。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent


def _audit():
    spec = importlib.util.spec_from_file_location(
        "asip_audit", PROJECT / "tools" / "asip_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit():
    return _audit()


def test_tier_off_line_labelled_not_never_probed(audit):
    """档位停用且从没探过 → 「档位停用」，不是「从未探测」。"""
    r = audit.probe_reason("WARP-WG-3", {"_tier_off": True}, True, 1e9)
    assert r == "档位停用", f"档位停用要单独成档，实际 {r!r}"


def test_spare_pool_labelled_not_out_of_list(audit):
    """补位池（不在名单）→ 「补位池闲置」，不是「不在探测名单」。"""
    r = audit.probe_reason("WARP-WG-25", {"_spare_pool": True}, False, 1e9)
    assert r == "补位池闲置", f"补位池要单独成档，实际 {r!r}"


def test_plain_never_probed_still_labelled(audit):
    """反向：名单内、非档位停用、非补位池、从没探过 → 仍是「从未探测」。"""
    r = audit.probe_reason("WARP-H2-NRT-0", {}, True, 1e9)
    assert r == "从未探测", "真·该探没探必须还能被认出来"


def test_out_of_list_still_labelled(audit):
    """反向：不在名单、非补位池、从没探过 → 仍是「不在探测名单」。"""
    r = audit.probe_reason("WARP-H2-LAX-27", {}, False, 1e9)
    assert r == "不在探测名单"


def test_tier_off_with_old_conclusion_marked_as_stale(audit):
    """档位停用但有旧采样 → 标签要点明「旧结论」，别让人当现行状态读。"""
    r = audit.probe_reason("WARP-WG-3",
                           {"_tier_off": True, "last_probe_ts": 1e9 - 3600},
                           True, 1e9)
    assert r.startswith("档位停用") and "探过" in r, r


def test_reason_key_splits_suffix(audit):
    """分桶键要剥掉时长后缀，否则每个时长都是独立一桶。"""
    assert audit._reason_key("惩罚期短路（3分钟前探过）") == "惩罚期短路"
    assert audit._reason_key("档位停用") == "档位停用"


def test_legend_covers_new_buckets(audit):
    """图例（LEGEND）必须能解释桶里出现的每一档，否则报告自相矛盾。"""
    text = (PROJECT / "tools" / "asip_audit.py").read_text(encoding="utf-8")
    for key in ("档位停用", "补位池闲置"):
        assert f'"{key}"' in text, f"图例缺 {key}，报告里会出现无解释的桶"
