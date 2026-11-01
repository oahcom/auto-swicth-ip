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
def _disable_night_window(monkeypatch):
    """夜间锁默认关掉：既有套件全部用真实时钟 + 非白名单 tag。

    生产默认 20:00→08:00 生效（`config.py` 的 night_window）。不关掉的话，
    **晚上跑测试**这些用例会因为重启被拦而变红或假绿 —— 同一份代码在
    白天和夜里给出不同结论是最难查的一类不稳定。需要测夜间行为的
    用例（`test_night_window.py`）自己用 fixture 把它打开。
    """
    nw = dict(daemon.CFG["warp"].get("night_window") or {})
    nw["enabled"] = False
    monkeypatch.setitem(daemon.CFG["warp"], "night_window", nw)


@pytest.fixture(autouse=True)
def _disable_warp_blocks(monkeypatch):
    """块模型同理：默认关，用例各自按需打开。

    `ASIP_WARP_BLOCKS=1` 只是某个 shell 里的一条环境变量，但它会让
    `_night_allow_tags()` 改走块白名单、让 `warp_wg.apply` 改走 `plan_lines`
    —— 同一套用例在「导出过这个变量」的终端里给出另一种拓扑。这类
    「环境决定结论」的不稳定和夜里跑测试变红是同一种问题。
    """
    blk = dict((daemon.CFG["warp"].get("blocks") or {}))
    blk["enabled"] = False
    monkeypatch.setitem(daemon.CFG["warp"], "blocks", blk)


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
