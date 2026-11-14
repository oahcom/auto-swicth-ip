"""全局测试隔离：单元测试不得读写 daemon 的线上状态文件。

`_warp_line_state_path()` / `_warp_ip_used_path()` 在调用时读环境变量，所以
把路径指到 tmp_path 即可让所有测试（含并发跑的全量套件）与线上
`.warp_line_state.json` / `.warp_ip_used.json` 完全隔离。
"""
import logging
import os
import sys
import tempfile
import threading

import pytest

# ═══════════════════════════════════════════════════════════════════
# 状态写入审计（生产状态目录只读的机器守卫）
# ═══════════════════════════════════════════════════════════════════
# 原作用：历史上测试进程多次把假账写进生产状态（.warp_ip_used.json 被
# 桩地址污染、.egress_log.jsonl 泄漏 15 条假记录、.api_stats.json 落过
# calls:1/borrowed:5 的假账、daemon.log 注入假事件让 grep 误判「线上拦了
# 3 次」）。靠「记得加 monkeypatch」拦不住第 N 处 —— 这里用 Python 审计
# 钩子（sys.addaudithook）在**进程级**截获一切写意图：凡目标在本仓内
# （排除 __pycache__/.pytest_cache/venv 等构建产物目录）的 open 写模式、
# os.remove/rename/mkdir/... 一律记录；ASIP_TEST_WRITE_GUARD=block 时当场
# AssertionError 让用例红。属三分法 C 类守卫（坏行为=测试写穿生产），只
# 加严不放松；它不改变任何被测代码的语义，只让违规写暴露为测试失败。
# 附带产物：审计日志本身就是「生产状态目录零写入」验收的证据链。
_REPO_ROOT = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
_AUDIT_DIR = tempfile.mkdtemp(prefix="asip-test-audit-")
AUDIT_LOG_PATH = os.path.join(_AUDIT_DIR, "state_writes.log")
_AUDIT_LOCK = threading.Lock()
AUDIT_VIOLATIONS: list = []

# 仓内允许被写的目录：pytest/解释器构建产物，不算生产状态。
_AUDIT_EXCLUDE_TOPS = ("__pycache__", ".pytest_cache", ".git", "venv",
                       "node_modules")
_WRITE_MODE_CHARS = set("wax+")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _audit_is_repo_path(path) -> bool:
    try:
        p = os.path.realpath(os.fsdecode(path))
    except Exception:
        return False
    if not p.startswith(_REPO_ROOT + os.sep):
        return False
    rel = p[len(_REPO_ROOT) + 1:]
    # 任意一级命中即豁免：子目录的 __pycache__（tools/deploy/asip 下）也是
    # 解释器构建产物，不算生产状态（否则 pyc 编译的 rename 会刷假阳性）。
    return not (set(rel.split(os.sep)) & set(_AUDIT_EXCLUDE_TOPS))


def _audit_with_dir_fd(path, dir_fd):
    """os.* 家族支持 dir_fd：相对路径是相对那个目录的，不是相对 cwd。

    shutil.rmtree 等清理走 dir_fd + 相对名；不解析的话会把临时目录里的
    清理误判成仓内写入（实测一次误报 2000+ 条）。/proc/self/fd 解析失败
    （非 Linux/已关闭）时按相对 cwd 处理，宁可误报不可漏报。
    """
    if dir_fd is None:
        return path
    try:
        base = os.readlink("/proc/self/fd/%d" % dir_fd)
    except Exception:
        return path
    return os.path.join(base, os.fsdecode(path))


def _audit_record(kind: str, path) -> None:
    test_id = os.environ.get("PYTEST_CURRENT_TEST") or "<import/atexit>"
    entry = "%s\t%s\t%s" % (kind, test_id, os.fsdecode(path))
    with _AUDIT_LOCK:
        AUDIT_VIOLATIONS.append(entry)
        try:
            with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception:
            pass
    if os.environ.get("ASIP_TEST_WRITE_GUARD") == "block":
        raise AssertionError(
            "测试进程试图写生产状态路径: %s（%s @ %s）"
            % (os.fsdecode(path), kind, test_id))


def _audit_hook(event, args):
    try:
        if event == "open":
            path, mode, flags = args[0], args[1], args[2]
            write = False
            if isinstance(mode, str) and (_WRITE_MODE_CHARS & set(mode)):
                write = True
            if isinstance(flags, int) and (flags & _WRITE_FLAGS):
                write = True
            if write and _audit_is_repo_path(path):
                _audit_record("open-write", path)
        elif event in ("os.remove", "os.rmdir"):
            p = _audit_with_dir_fd(args[0], args[1] if len(args) > 1 else None)
            if _audit_is_repo_path(p):
                _audit_record(event, p)
        elif event == "os.mkdir":
            p = _audit_with_dir_fd(args[0], args[2] if len(args) > 2 else None)
            if _audit_is_repo_path(p):
                _audit_record(event, p)
        elif event in ("os.chmod", "os.truncate"):
            p = _audit_with_dir_fd(args[0], args[2] if len(args) > 2 else None)
            if _audit_is_repo_path(p):
                _audit_record(event, p)
        elif event in ("os.chown", "os.utime"):
            p = _audit_with_dir_fd(args[0], args[3] if len(args) > 3 else None)
            if _audit_is_repo_path(p):
                _audit_record(event, p)
        elif event in ("os.rename", "os.link"):
            pairs = ((args[0], args[2] if len(args) > 2 else None),
                     (args[1], args[3] if len(args) > 3 else None))
            for p, fd in pairs:
                rp = _audit_with_dir_fd(p, fd)
                if _audit_is_repo_path(rp):
                    _audit_record(event, rp)
        elif event == "os.symlink":
            p = _audit_with_dir_fd(args[1], args[2] if len(args) > 2 else None)
            if _audit_is_repo_path(p):
                _audit_record(event, p)
    except AssertionError:
        raise
    except Exception:
        pass


sys.addaudithook(_audit_hook)

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

# ═══════════════════════════════════════════════════════════════════
# 进程级状态基线：把运行时状态的**默认值**整体挪进会话临时根
# ═══════════════════════════════════════════════════════════════════
# 为什么夹具级重定向不够：用例级 monkeypatch/setenv 在 teardown 还原回
# 「夹具启动前的值」——若那就是生产路径，后台线程在**用例结束之后**继续
# 写（实测审计归因 <import/atexit> 窗口的 .warp/udp_bad_nodes.json 生产
# 写入 3 次）就会写穿生产。两层结构：进程级基线（本块，永不还原回生产）
# + 用例级重定向（夹具，按用例再指到 tmp_path）。调用时读 env 的路径助手
# 同理——env 基线先立住，夹具 setenv 的还原目标就是基线而非「无」。
# 对生产目录：从此测试进程的默认落点全部是会话临时根，生产全程只读
# （写入审计钩子执法）。
from pathlib import Path  # noqa: E402

import warp_wg  # noqa: E402

_STATE_BASELINE_ENV = {
    "WARP_LINE_STATE_FILE_PATH": "line_state.json",
    "WARP_IP_USED_FILE_PATH": "ip_used.json",
    "WARP_SEGMENT_FILE_PATH": "segment.json",
    "NODE_IP_USED_FILE_PATH": "node_ip_used.json",
    "EGRESS_LOG_FILE_PATH": "egress_log.jsonl",
    "WARP_IP_HISTORY_FILE_PATH": "warp_ip_history.jsonl",
    "WARP_PROXY_FALLBACK_FILE_PATH": "proxy_fallback.json",
    "WARP_IP_CYCLES_FILE_PATH": "warp_ip_cycles.json",
}
for _k, _v in _STATE_BASELINE_ENV.items():
    os.environ[_k] = os.path.join(_LEDGER_DIR, _v)

# 无 env 钩子的模块常量：默认值一并挪走（用例夹具再按用例细化到 tmp_path）
daemon._BAD_NODES_FILE = os.path.join(_LEDGER_DIR, ".bad_nodes.json")
daemon._SLOT_HEALTH_FILE = os.path.join(_LEDGER_DIR, ".slot_health.json")
daemon._API_STATS_FILE = os.path.join(_LEDGER_DIR, ".api_stats.json")
daemon._LEDGER_SHADOW_FILE = os.path.join(_LEDGER_DIR, ".ledger_shadow.json")
daemon._CHANNEL_LATENCY_PATH = os.path.join(_LEDGER_DIR, ".channel_latency.json")
daemon._DAEMON_LOCK_FILE = os.path.join(_LEDGER_DIR, ".daemon.lock")
daemon.POOL_HISTORY = os.path.join(_LEDGER_DIR, ".pool_history.json")
daemon._DORMANT_PROBE_ATTEMPT_FILE = os.path.join(_LEDGER_DIR, ".dormant_probe_attempt")
daemon._DORMANT_PROBE_LOG_FILE = os.path.join(_LEDGER_DIR, ".dormant_probe.log")
daemon._DORMANT_PROBE_LOCK_FILE = os.path.join(_LEDGER_DIR, ".dormant_probe.lock")
# warp_wg.PROJECT（模块加载期从 config.PROJECT_DIR 拷贝）挂着 .warp/ 下的
# 运行时状态（udp_bad_nodes.json / warp_wg_state.json / wg_colo_map.json）
warp_wg.PROJECT = Path(_LEDGER_DIR)

import config as _config_baseline  # noqa: E402

_config_baseline.EXTRA_SUBS_FILE = os.path.join(_LEDGER_DIR, "extra_subs.json")

# singbox-config.json 是 warp_wg.load_nodes() 的输入（refresh 的产物）：
# 测试只读、写必须落临时根。把生产文件原样拷到会话临时根当种子 —— 读到的
# 内容与生产一致（用例结论不漂移），任何写只碰临时根（生产文件只读）。
# 注意不能放 tmp_path：有用例会枚举 tmp_path 计数（test_subscription_parse
# 的快照缓存、test_daemon_sidecars 的 stats 清理），多一个文件就红。
SEEDED_SINGBOX_CONFIG = os.path.join(_LEDGER_DIR, "singbox-config.json")
_prod_cfg = os.path.join(_REPO_ROOT, "singbox-config.json")
if os.path.exists(_prod_cfg):
    import shutil
    shutil.copyfile(_prod_cfg, SEEDED_SINGBOX_CONFIG)
else:
    SEEDED_SINGBOX_CONFIG = _prod_cfg   # 仓里没有就保持原语义（读不到会报错）


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


@pytest.fixture(autouse=True)
def _isolate_state_root(tmp_path, monkeypatch):
    """状态根目录隔离：把其余运行时状态读写也重定向到 tmp（生产只读）。

    原作用（补齐上面夹具没盖到的缺口——动前盘点过每个路径的原作用）：
    既有夹具只把 line_state/ip_used/segment/extra_subs/日志指到 tmp，
    而 daemon 还有十几处运行时状态落在仓库根（历史上泄漏过假账/桩地址）：
      - 调用时读环境变量的路径助手（node_ip_used/egress_log/warp_ip_history/
        proxy_fallback/warp_ip_cycles）：置 env 即可，读写同时被重定向；
      - 无 env 钩子的模块常量（bad_nodes/slot_health/api_stats/ledger_shadow/
        channel_latency/daemon.lock/pool_history/dormant_probe 三件）：
        monkeypatch 常量到 tmp 路径；dormant 三件是 os.path.join(_PROJECT, 相对
        名) 拼接，置成绝对路径后 join 自然回落到 tmp（不碰 _PROJECT —— 它还
        挂着 deploy 脚本与 .warp/h2 的只读路径，改它属旁路写路径风险）；
      - CFG 里的路径项（api token/sock、opencode 闸口账本、vercel 台账、
        gate.log、alert 事件、singbox 主配置）：monkeypatch.setitem 到 tmp。
    只做「路径重定向」，不改任何被测逻辑；既有 8 个夹具一个不动（只许增强
    不许削弱）。生产目录对测试进程全程只读 —— 有写入审计钩子兜底执法。
    """
    # 1) 调用时读环境变量的路径助手（读写都走这里）
    monkeypatch.setenv("NODE_IP_USED_FILE_PATH", str(tmp_path / "node_ip_used.json"))
    monkeypatch.setenv("EGRESS_LOG_FILE_PATH", str(tmp_path / "egress_log.jsonl"))
    monkeypatch.setenv("WARP_IP_HISTORY_FILE_PATH", str(tmp_path / "warp_ip_history.jsonl"))
    monkeypatch.setenv("WARP_PROXY_FALLBACK_FILE_PATH", str(tmp_path / "proxy_fallback.json"))
    monkeypatch.setenv("WARP_IP_CYCLES_FILE_PATH", str(tmp_path / "warp_ip_cycles.json"))
    # 2) 无 env 钩子的模块常量（用例再 monkeypatch 同名常量时自动叠放还原）
    monkeypatch.setattr(daemon, "_BAD_NODES_FILE", str(tmp_path / ".bad_nodes.json"))
    monkeypatch.setattr(daemon, "_SLOT_HEALTH_FILE", str(tmp_path / ".slot_health.json"))
    monkeypatch.setattr(daemon, "_API_STATS_FILE", str(tmp_path / ".api_stats.json"))
    monkeypatch.setattr(daemon, "_LEDGER_SHADOW_FILE", str(tmp_path / ".ledger_shadow.json"))
    monkeypatch.setattr(daemon, "_CHANNEL_LATENCY_PATH", str(tmp_path / ".channel_latency.json"))
    monkeypatch.setattr(daemon, "_DAEMON_LOCK_FILE", str(tmp_path / ".daemon.lock"))
    monkeypatch.setattr(daemon, "POOL_HISTORY", str(tmp_path / ".pool_history.json"))
    monkeypatch.setattr(daemon, "_DORMANT_PROBE_ATTEMPT_FILE",
                        str(tmp_path / ".dormant_probe_attempt"))
    monkeypatch.setattr(daemon, "_DORMANT_PROBE_LOG_FILE",
                        str(tmp_path / ".dormant_probe.log"))
    monkeypatch.setattr(daemon, "_DORMANT_PROBE_LOCK_FILE",
                        str(tmp_path / ".dormant_probe.lock"))
    # 3) 其他模块的生产状态文件（self_evolve_checkpoint 的 .monitor_state.json）
    try:
        import self_evolve_checkpoint as _sec
        monkeypatch.setattr(_sec, "STATE_FILE", tmp_path / ".monitor_state.json")
    except Exception:
        pass
    # 4) CFG 路径项（会话级 CFG 是共享 dict，用 setitem 按用例还原）
    monkeypatch.setitem(daemon.CFG, "gate_log_file", str(tmp_path / "gate.log"))
    monkeypatch.setitem(daemon.CFG, "alert_event_file", str(tmp_path / "events.jsonl"))
    # config_file 指到会话种子（生产同内容副本）：读不漂移、写不落生产。
    # 不放 tmp_path —— 有用例枚举 tmp_path 计数，多一个文件就红。
    monkeypatch.setitem(daemon.CFG["singbox"], "config_file", SEEDED_SINGBOX_CONFIG)
    monkeypatch.setitem(daemon.CFG["api"], "token_path", str(tmp_path / "api-token"))
    monkeypatch.setitem(daemon.CFG["api"], "socket_path", str(tmp_path / "rotate.sock"))
    monkeypatch.setitem(daemon.CFG["opencode_gate"], "ledger_path",
                        str(tmp_path / "opencode_gate.jsonl"))
    monkeypatch.setitem(daemon.CFG["opencode_gate"], "log_path",
                        str(tmp_path / "opencode-gate.log"))
    monkeypatch.setitem(daemon.CFG["vercel"], "ledger_path",
                        str(tmp_path / "vercel_relay.json"))
    # warp_wg 状态树不在这里按用例改 PROJECT：会话基线已把默认挪进临时根，
    # 而按用例挪进 tmp_path 会让后台线程的延迟写出现在 tmp_path 里、踩到
    # 「枚举 tmp_path」的用例；需要专用树的用例（test_warp_wg）自己 monkeypatch。


# ── 可信子集运行通道：按前缀分组打标记（tests/run_groups.py 消费） ──────────
# 原作用：「单独绿、全量红」的一半成因是全量互踩（模块级 monkey 注入、
# 进程级全局残留、sys.path 污染）。分组标记让 tests/run_groups.py 能按组
# 以**独立进程**跑（进程级隔离），任一组可重复跑出稳定红绿。分组规则是
# 机械的文件名前缀（test_<组名>_*），不改任何用例文件；标记注册在
# pytest.ini，`python3 -m pytest -m warp` 也能单独选组。
_GROUP_PREFIXES = {
    "warp": "warp", "slot": "slot", "rotate": "rotate", "gate": "gate",
    "config": "config", "daemon": "daemon", "asip": "asip",
    "dormant": "dormant", "tier": "tier", "h2": "h2", "egress": "egress",
    "probe": "probe", "channel": "channel", "api": "api", "audit": "audit",
    "state": "state", "ledger": "ledger", "self": "self", "node": "node",
    "subscription": "subscription", "reconcile": "reconcile",
    "rebind": "rebind", "demoted": "demoted", "trace": "trace",
    "portability": "portability", "vercel": "vercel", "night": "night",
    "opencode": "opencode", "proxy": "proxy", "auto": "auto", "block": "block",
    "429": "cooldown", "verify": "verify", "tools": "tools",
    "alerting": "alerting",
}


def _group_of(modname: str, filepath: str = "") -> str:
    # tests/contracts/ 整树归 contracts 组（文件名前缀各异，按目录定组）
    if "contracts" in filepath.replace("\\", "/").split("/"):
        return "contracts"
    base = modname.split(".")[-1]
    if not base.startswith("test_"):
        return "misc"
    head = base[len("test_"):].split("_")[0]
    return _GROUP_PREFIXES.get(head, "misc")


def pytest_collection_modifyitems(items):
    for item in items:
        mod = getattr(item, "module", None)
        name = getattr(mod, "__name__", "") or ""
        path = str(getattr(item, "path", "") or getattr(item, "fspath", "") or "")
        item.add_marker(getattr(pytest.mark, _group_of(name.split(".")[-1], path)))


def pytest_collection_finish(session):
    """收集期结束后清掉 sys.path 里的漂移副本入口。

    原作用：test_channel_ban_hold / test_node_rotation 在模块级
    `sys.path.insert(0, "/home/administrator/auto-swicth-ip")`（拼写错的
    旧目录，至今存在）。收集期所有测试模块都会被执行 import，这条插入
    随后生效；任何 importlib.reload / 新 import 都可能从漂移副本拿代码，
    结论随副本内容漂移。收集完成后删掉它（模块缓存已建立，运行期不受
    影响），只清已知漂移目录，不动其他路径。
    """
    stale = os.path.realpath("/home/administrator/auto-swicth-ip")
    for p in list(sys.path):
        try:
            if p and os.path.realpath(p) == stale:
                sys.path.remove(p)
        except Exception:
            continue


@pytest.fixture(autouse=True, scope="session")
def _assert_no_production_writes():
    """会话收尾硬验收：审计记录非空 = 本次跑有测试写穿生产状态，整场红。

    逐条写当场 raise（ASIP_TEST_WRITE_GUARD=block）能给单条用例定位，但
    有些写发生在被 try/except 吞掉的路径里；这条兜底保证「漏网的写」也会
    让整场不绿 —— 红绿信号可信的底线。
    """
    yield
    if AUDIT_VIOLATIONS:
        raise AssertionError(
            "本次测试运行写入了生产状态路径 %d 处（详见审计日志 %s）：\n%s"
            % (len(AUDIT_VIOLATIONS), AUDIT_LOG_PATH,
               "\n".join(AUDIT_VIOLATIONS[:20])))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    n = len(AUDIT_VIOLATIONS)
    terminalreporter.write_sep(
        "=", "生产状态写入审计: %d 处（0 = 零写入）%s"
        % (n, "" if n == 0 else " → " + AUDIT_LOG_PATH))
    # 预期红/意外红区分标注（与 tests/run_groups.py 的 EXPECTED_RED 同步）：
    # ③组「修前红」漂移验收是三方裁决落地前的有意设计红，不是故障——但绝不
    # 为全绿改测试，只做标注。意外红照样显示，红绿信号保持诚实。
    failed_ids = {rep.nodeid for rep in terminalreporter.stats.get("failed", [])}
    expected = sorted(failed_ids & EXPECTED_RED)
    unexpected = sorted(failed_ids - EXPECTED_RED)
    if expected or unexpected:
        terminalreporter.write_sep(
            "=", "红绿分类: 预期红 %d / 意外红 %d"
            % (len(expected), len(unexpected)))
        for tid in expected:
            terminalreporter.write_line("  预期红（③组修前红，漂移修复后转绿）: %s" % tid)
        for tid in unexpected:
            terminalreporter.write_line("  意外红: %s" % tid)


# ③组「修前红」漂移验收登记（tests/contracts/test_acceptance_config_drift.py）。
# 2026-11-14：三方裁决「注释/文案错」已按授权落地（daemon.py 两处文案对齐），
# 两条登记项已转绿并**清空**——登记表只收「有意设计红」；若日后漂移复发，
# 必须以意外红亮出（回归不许被「预期红」标签掩盖）。
EXPECTED_RED = set()


# ── tests/ 收集口径（2026-11-14 定案，替代 collect_ignore_glob=["tests/*"]）──
# 原作用（保持不许削弱）：tests/ 下的**旧独立脚本**不是 pytest 用例——全靠
# 模块级 `assert` + print 执行（用法见 docs/warp-wg-over-proxy.md，手工
# `python3 tests/test_rotate.py`），但文件名匹配 test_*.py，pytest 收集时会
# 把它们当模块导入——模块级 `d._mark_warp_ip_used = lambda ...` 之类赋值
# 直接打在 daemon 模块上（非 monkeypatch、用例结束不恢复）。实测后果：全量
# `pytest` 时 test_rotate.py 被导入即向 daemon 注入 monkey，其后 43 个用例
# 全部 TypeError / KeyError，单独跑却全绿——典型「单独绿、全量红」。
# **这些脚本永远排除在 pytest 收集外**（手工 python3 跑照常有效）。
#
# 为什么从 ["tests/*"] 收窄为精确清单：fnmatch 的 `*` 跨目录分隔符，
# "tests/*" 会把整棵 tests/ 树剪掉——包括 pytest 风格的 tests/contracts/
# （P2 契约测试 88 例）与 tests/test_alerting_4a.py（P6a 告警行为 27 例），
# 裸全量「绿」漏跑 115 条测试，红绿信号不可信（T0 验收硬要求：全量必须
# 含契约与行为测试）。收窄口径：**只挡已知的旧独立脚本，tests/ 新增 pytest
# 风格用例默认收集**（新增用例须 fixture 化、禁止模块级 monkey 注入）。
collect_ignore = [
    "tests/test_already_retired_veto.py",
    "tests/test_api_stats_persist.py",
    "tests/test_channel_latency_pick.py",
    "tests/test_drain_before_recycle.py",
    "tests/test_egress_cooling.py",
    "tests/test_egress_cooling_report.py",
    "tests/test_probe_cache.py",
    "tests/test_restart_scope.py",
    "tests/test_rotate.py",
    "tests/test_slot_pick_avoid_ip.py",
]
