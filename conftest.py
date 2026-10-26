"""全局测试隔离：单元测试不得读写 daemon 的线上状态文件。

`_warp_line_state_path()` / `_warp_ip_used_path()` 在调用时读环境变量，所以
把路径指到 tmp_path 即可让所有测试（含并发跑的全量套件）与线上
`.warp_line_state.json` / `.warp_ip_used.json` 完全隔离。
"""
import logging

import pytest

import daemon


@pytest.fixture(autouse=True)
def _isolate_live_state_files(tmp_path, monkeypatch):
    monkeypatch.setenv("WARP_LINE_STATE_FILE_PATH",
                       str(tmp_path / "line_state.json"))
    monkeypatch.setenv("WARP_IP_USED_FILE_PATH",
                       str(tmp_path / "ip_used.json"))
    monkeypatch.setenv("WARP_SEGMENT_FILE_PATH",
                       str(tmp_path / "segment.json"))


@pytest.fixture(autouse=True)
def _isolate_segment_globals(monkeypatch):
    """段起点是模块级全局：上一个用例（如 sing-box 重启）会把「现在」写进去，
    下一个用例再用合成时钟 1000.0 就会被判成「刚换过线」而拒绝换线。"""
    monkeypatch.setattr(daemon, "_last_warp_switch_ok_ts", 0.0)
    monkeypatch.setattr(daemon, "_SEGMENT_PENDING", False)


@pytest.fixture(autouse=True)
def _isolate_live_log_file(tmp_path, monkeypatch):
    """日志同样指到 tmp：`CFG["log_file"]` 是绝对路径，测试跑在哪个 worktree
    都会往线上 daemon.log 里写用例产生的假事件。"""
    monkeypatch.setitem(daemon.CFG, "log_file", str(tmp_path / "daemon.log"))
    logger = logging.getLogger("auto-switch-ip")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    monkeypatch.setattr(daemon, "_logger", None)
