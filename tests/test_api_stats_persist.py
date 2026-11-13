"""M10（遗留修复 R2）：_api_stats 落盘与启动合并的单元测试。

全部走 tmp_path，绝不触碰仓库根的真实 .api_stats.json —— `_persist_api_stats`
与 `_load_api_stats_boot` 读写的路径都来自模块级 `_API_STATS_FILE`，用例里
monkeypatch 掉即可。`_api_stats` / `_API_STATS_BASELINE` 是进程级全局，
每个用例前先替换成干净的 dict，teardown 由 monkeypatch 自动还原。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, '/home/administrator/dkk-projects/auto-switch-ip')

import daemon as d


@pytest.fixture(autouse=True)
def _isolate_stats(monkeypatch):
    """统计全局与落盘路径按用例隔离，避免污染真实文件与后续用例。"""
    monkeypatch.setattr(d, "_api_stats",
                        {k: 0 for k in ("calls", "switched", "coalesced",
                                        "retired", "too_frequent",
                                        "no_standby")})
    monkeypatch.setattr(d, "_API_STATS_BASELINE", {})
    tmp_file = Path(__file__).parent / ".api_stats_test.tmp.json"
    monkeypatch.setattr(d, "_API_STATS_FILE", str(tmp_file))
    yield
    if tmp_file.exists():
        tmp_file.unlink()


def test_merge_accumulates_missing_and_new_keys():
    """merge 累加正确：已有键相加、盘上多出的新键按 0 起步并入。"""
    d._api_stats["calls"] = 10
    d._api_stats["switched"] = 3
    out = d._merge_api_stats({"calls": 100, "new_key": 7,
                              "persisted_at": 12345.6,
                              "client_gone": 99, "junk": "x"})
    assert out["calls"] == 110          # 10 + 100
    assert out["switched"] == 3         # 盘上缺此键 → 原值保留
    assert out["new_key"] == 7          # 内存缺此键 → 从零并入
    assert "persisted_at" not in out    # 时间戳不是计数器
    assert "client_gone" not in out     # 本次运行绝对值，不合并
    assert "junk" not in out            # 非数值键忽略


def test_load_corrupted_file_starts_from_zero():
    """文件损坏 / 非 dict / 不存在 → 静默从零，基线为空。"""
    p = Path(d._API_STATS_FILE)
    # 1) 不存在
    d._load_api_stats_boot()
    assert d._api_stats["calls"] == 0
    assert d._API_STATS_BASELINE == {}
    # 2) 损坏 JSON
    p.write_text("{not json!!!", encoding="utf-8")
    d._load_api_stats_boot()
    assert d._api_stats["calls"] == 0
    assert d._API_STATS_BASELINE == {}
    # 3) JSON 是合法的但不是 dict
    p.write_text("[1, 2, 3]", encoding="utf-8")
    d._load_api_stats_boot()
    assert d._api_stats["calls"] == 0
    assert d._API_STATS_BASELINE == {}


def test_persist_atomic_roundtrip(tmp_path, monkeypatch):
    """原子写后立即读回一致：内容含 persisted_at，无 .tmp 残留。"""
    target = tmp_path / ".api_stats.json"
    monkeypatch.setattr(d, "_API_STATS_FILE", str(target))
    d._api_stats["calls"] = 5
    d._api_stats["switched"] = 2
    d._persist_api_stats()
    assert target.exists()
    assert not Path(str(target) + ".tmp").exists()   # os.replace 后无残留
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["calls"] == 5
    assert data["switched"] == 2
    assert isinstance(data.pop("persisted_at"), float)
    assert data["calls"] == 5 and data["switched"] == 2
    assert all(v == 0 for k, v in data.items()
               if k not in ("calls", "switched"))
