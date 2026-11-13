"""全局测试隔离：单元测试不得读写 daemon 的线上状态文件。

`_warp_line_state_path()` / `_warp_ip_used_path()` 在调用时读环境变量，所以
把路径指到 tmp_path 即可让所有测试（含并发跑的全量套件）与线上
`.warp_line_state.json` / `.warp_ip_used.json` 完全隔离。
"""
import logging
import os
import tempfile

import pytest

# **必须在 `import daemon` 之前**：`config.py` 在 import 时就把
# `.warp/backfill.json` / `.warp/h2/channels.json` 并进 `members`，之后再设
# 环境变量已经晚了 —— 唯一的补救是 reload config，而 reload 会造出新的
# `CFG` 对象，与 `parse_subscription` 这类 `from config import CFG` 的模块
# 手里那个分家（表现为「单独跑绿、全量跑红」）。
#
# 后果不隔离会长什么样：补位线从 7 条涨到 10 条那天（2026-11-04），
# `test_warp_wg.py` 里三条断言「members 只有配置声明的那几条」的用例一起转红，
# 代码一行没改 —— 用例结论由生产此刻有几条补位线决定。
_LEDGER_DIR = tempfile.mkdtemp(prefix="asip-test-ledgers-")
_EMPTY_LEDGER = os.path.join(_LEDGER_DIR, "empty.json")
with open(_EMPTY_LEDGER, "w", encoding="utf-8") as _f:
    _f.write("[]")
os.environ["ASIP_BACKFILL_FILE_PATH"] = _EMPTY_LEDGER
os.environ["ASIP_H2_CHANNELS_PATH"] = _EMPTY_LEDGER

import daemon  # noqa: E402  —— 必须在上面的环境变量之后


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
def _disable_opencode_gate(monkeypatch):
    """opencode 专属闸口测试默认关：既有用例的槽位数/布局断言基线不变。

    生产 config.py 里 `opencode_gate.slots = 1`（consumer_slots() 会多产出
    一个 kind="opencode" 槽位）。既有用例（test_proxy_gate 的
    len(slots)==7、test_slot_* 的槽位布局）按 6 WARP + 1 池闸的基线写死，
    闸口默认开会把它们全部带偏 —— 与夜间锁/块模型同一个「生产默认开、
    测试默认关，需要的用例自己打开」处理。需要它的用例
    （test_opencode_gate.py）在自己的 fixture 里把 slots 设回 1。
    """
    og = dict(daemon.CFG.get("opencode_gate") or {})
    og["slots"] = 0
    monkeypatch.setitem(daemon.CFG, "opencode_gate", og)


@pytest.fixture(autouse=True)
def _isolate_extra_subscriptions(tmp_path, monkeypatch):
    """追加订阅台账指向 tmp：否则 pytest 会读到生产的 `.extra_subscriptions`，
    于是「daemon 读到了哪些源」「试跑插一个源」全被真实台账污染 —— 更糟的是
    测试里的 add 会往生产台账里写 token。"""
    import config
    monkeypatch.setattr(config, "EXTRA_SUBS_FILE", str(tmp_path / "extra_subs"))
    monkeypatch.delenv("ASIP_EXTRA_SUB_URLS", raising=False)


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


@pytest.fixture(autouse=True)
def _restore_environ():
    """用例改动 `os.environ` 的**任何**路径都要还原，不只是 monkeypatch 那条。

    `test_channel_report.py` / `test_block_view.py` 用 `exec_module` 把
    `tools/*.py` 直接跑在测试进程里，而这些 CLI 在模块级执行
    `os.environ.setdefault(k, v)` 把生产 `.env` 整个灌进来（手工运行时那正是
    它们该做的事）。这条路径不经过 monkeypatch，用例结束不会恢复 ——
    于是**后面**任何 `importlib.reload(config)` 都会读到生产的
    `WARP_WG_FAMILIES`，`test_warp_wg.py` 里两条「只应有 3 条 WG 线」的断言
    看到 22 条而转红，代码一行没改。

    实测：`pytest test_channel_report.py test_warp_wg.py` 2 failed；
    只跑 `test_warp_wg.py` 全绿。
    """
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def _restore_config_module():
    """`importlib.reload(config)` 会**原地替换**模块里的所有名字。

    `test_warp_wg.py` 用 reload 来验证 `WARP_WG_*` 环境变量的效果，reload 之后
    `config.CFG` 是一个**新 dict**；而 `parse_subscription` 在 import 时就
    `from config import CFG` 拿走了旧的那个。后续用例 `monkeypatch.setitem(
    config.CFG["singbox"], ...)` 打在 A 上、被测代码读 B ——
    实测 `test_lan_inbound_skipped_when_port_zero` 因此「单独跑绿、全量跑红」。

    每条用例结束后把 config 模块的名字表恢复成用例开始前的样子。
    """
    import config

    snapshot = {k: v for k, v in config.__dict__.items() if not k.startswith("__")}
    yield
    for k in [k for k in config.__dict__
              if not k.startswith("__") and k not in snapshot]:
        delattr(config, k)
    for k, v in snapshot.items():
        setattr(config, k, v)


# `tests/` 下是**独立脚本**，不是 pytest 用例：三个文件里没有一个 `test_` 函数，
# 全靠模块级 `assert` + print 执行，用法见 docs/warp-wg-over-proxy.md（手工
# `python3 tests/test_rotate.py`）。但文件名匹配 `test_*.py`，pytest 收集时
# 会把它们**当模块导入**——模块级代码随即执行，`d._mark_warp_ip_used = lambda ...`
# 之类的赋值是直接打在 `daemon` 模块上的，不是 monkeypatch，用例结束也不会恢复。
#
# 实测后果：跑全量 `pytest` 时 `test_rotate.py` 先被导入，把
# `_mark_warp_ip_used` 换成只收 5 个参数的 lambda，之后 `test_429_cooldown`
# 等 43 个用例全部 TypeError / KeyError；而单独跑这些文件全绿 —— 典型的
# 「单独绿、全量红」。手工跑它们仍然完全有效，只是不进 pytest 收集。
collect_ignore_glob = ["tests/*"]
