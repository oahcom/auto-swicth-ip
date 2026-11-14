"""tests/contracts/ 专用收集钩子 + 内存状态复位（契约测试三分法 · 任务 T1）。

WHY 需要 pytest_ignore_collect：
根 conftest.py 的 `collect_ignore_glob = ["tests/*"]` 用 fnmatch 匹配，
`*` 跨目录分隔符，`tests/contracts/*.py` 同样命中 —— 显式传路径也不收集
（实证：无本钩子时 `pytest tests/contracts/` 输出 "no tests collected"）。
这里对 contracts 下的路径显式返回 False（不忽略），使契约测试可被收集；
根 conftest 的 8 个 autouse 隔离夹具（状态文件指 tmp、日志指 tmp、夜间锁关、
块模型关、环境还原、config 模块还原）对本目录照常生效 —— 契约测试依赖它们
保证「测试不写穿生产状态」（附件 D「测试/侧写隔离」右栏）。

本文件只做两件事：解除收集屏蔽；每个用例前后清 daemon 的**内存**台账/状态
表，防跨用例污染（生产落盘文件已被根夹具指到 tmp，清内存不碰任何生产文件）。
"""
import pathlib
import sys

import pytest

# 防 tests/contracts/__pycache__ 写入触发根 conftest 的生产写入审计
# （其排除表只认顶层 __pycache__，包内 pyc 会被当「写生产状态」）。
sys.dont_write_bytecode = True


def pytest_ignore_collect(collection_path, config):
    if "contracts" in pathlib.Path(str(collection_path)).parts:
        return False
    return None


@pytest.fixture(autouse=True)
def _isolate_extra_state_files(tmp_path, monkeypatch):
    """根 conftest 未覆盖的两个落盘旁路指到 tmp（历史/周期样本文件）。"""
    monkeypatch.setenv("WARP_IP_HISTORY_FILE_PATH",
                       str(tmp_path / "ip_history.jsonl"))
    monkeypatch.setenv("WARP_IP_CYCLES_FILE_PATH",
                       str(tmp_path / "ip_cycles.json"))


@pytest.fixture(autouse=True)
def _reset_contract_state():
    """清 daemon 内存态，防用例间互相污染（只清内存，不写任何生产文件）。"""
    import daemon as d

    def _clear():
        for name in ("_WARP_IP_USED", "_WARP_LINE_STATE", "_SLOT_EXIT_LEDGER",
                     "_LINE_OK_STRIKES", "_WARP_PROBE_SEEN", "_PROBE_BLOCKED_AT",
                     "_EGRESS_RECHECK", "_DEAD_NODES", "_DEAD_NODES_RELEASE_TS",
                     "_dead_fail_count", "_NODE_DELAYS", "_NODE_DELAY_TS"):
            obj = getattr(d, name, None)
            if obj is not None and hasattr(obj, "clear"):
                obj.clear()
        obj = getattr(d, "_EXIT_LEDGER", None)
        if isinstance(obj, list):
            obj.clear()
        if hasattr(d, "_LEDGER_ALLOW_EMPTY_WRITE"):
            d._LEDGER_ALLOW_EMPTY_WRITE = False

    _clear()
    yield
    _clear()
