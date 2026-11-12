"""第七闸口独立日志（gate.log）分流回归测试。

钉死四件事：分流口径与 CLI 一致、池闸行只进独立账本（mirror 关闭时）、
stdout 语义不变（journal 仍全量）、以及只有 systemd 进程才允许落文件
（_side_effects_off 的老规矩，pytest 不许往生产 gate.log 写假事件）。
"""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
import config  # noqa: E402
import daemon  # noqa: E402

GATE_LINES = [
    "⇢ slot-7 报 rate429（ok）[req=a] 池节点=n1，重试改走 http://127.0.0.1:7897",
    "↻ 第七闸口池内轮换：n1 → n2（rate429）",
    "· 秒切兜底：常规闸口全部不可选，第七闸口（代理池）接手（池节点=n2）",
    "⇢ slot-2 报 rate429 线=WARP-CF4 重试改走 slot-7 目标url=http://127.0.0.1:7897",
    "⚡ 秒切第七闸口：主出口翻到轮换池（原因=rate429）",
]
PLAIN_LINES = [
    "· 切代理 WARP-CF4 → WARP-H2",
    "✓ 健康检查 OK（错误=0/10）",
    "⇄ rotate 接口就绪: run/rotate.sock",
]


@pytest.fixture
def split_logger(tmp_path, monkeypatch):
    """按 _init_logger 的真实结构建一份隔离 logger，只把路径换到 tmp_path。"""
    main = tmp_path / "daemon.log"
    gate = tmp_path / "gate.log"
    lg = logging.getLogger("auto-switch-ip")
    for h in list(lg.handlers):
        try:
            h.close()
        except Exception:
            pass
    lg.handlers.clear()                      # getLogger 是单例，别把 handler 累积起来
    monkeypatch.setitem(daemon.CFG, "log_file", str(main))
    monkeypatch.setitem(daemon.CFG, "gate_log_file", str(gate))
    monkeypatch.setattr(daemon, "_side_effects_off", lambda: False)
    daemon._logger = None
    daemon._logger_retry_ts = 0.0
    daemon._init_logger()
    try:
        yield main, gate
    finally:
        for h in list(lg.handlers):
            try:
                h.close()
            except Exception:
                pass
        lg.handlers.clear()
        # 留 None：其它测试第一次 log() 会自己 lazy init，不会拿到一个
        # handlers 被清空、静默吞日志的 logger。
        daemon._logger = None
        daemon._logger_retry_ts = 0.0


def _keywords(pattern: str) -> set:
    return set(pattern.split("|"))


def test_gate_regex_matches_cli_single_source():
    """daemon 的分流口径和 CLI 的 grep 口径必须是同一份关键词。"""
    cli = (PROJECT / "deploy" / "asip-cli.sh").read_text(encoding="utf-8")
    m = re.search(r"^_RE_GATE='([^']+)'", cli, re.M)
    assert m, "CLI 里找不到 _RE_GATE"
    assert _keywords(m.group(1)) == _keywords(daemon._GATE_LOG_RE.pattern)


def test_filters_partition_lines():
    gf = daemon._GateLineFilter()
    mf = daemon._MainLineFilter()
    for text in GATE_LINES:
        rec = logging.LogRecord("t", logging.INFO, "", 0, text, None, None)
        assert gf.filter(rec) is True, text
        assert mf.filter(rec) is False, text
    for text in PLAIN_LINES:
        rec = logging.LogRecord("t", logging.INFO, "", 0, text, None, None)
        assert gf.filter(rec) is False, text
        assert mf.filter(rec) is True, text


def test_split_logger_routes_gate_lines_only(split_logger):
    main, gate = split_logger
    for text in GATE_LINES + PLAIN_LINES:
        daemon.log(text)
    for h in daemon._logger.handlers:
        h.flush()
    gate_text = gate.read_text(encoding="utf-8")
    main_text = main.read_text(encoding="utf-8")
    for text in GATE_LINES:
        assert text in gate_text, "池闸行没进独立账本: " + text
        assert text not in main_text, "池闸行仍混在主日志: " + text
    for text in PLAIN_LINES:
        assert text in main_text, "普通行被误分流: " + text
        assert text not in gate_text, "普通行混进池闸账本: " + text
    assert gate_text.count("\n") == len(GATE_LINES)
    assert main_text.count("\n") == len(PLAIN_LINES)
    assert oct(gate.stat().st_mode & 0o777) == "0o600"


def test_mirror_mode_writes_both(split_logger, monkeypatch):
    """ASIP_GATE_LOG_MIRROR=1 时恢复双写，兼容按 daemon.log grep 的旧习惯。"""
    main, gate = split_logger
    # 重建 logger：mirror 是在挂 filter 时读的，运行中改配置不生效。
    logging.getLogger("auto-switch-ip").handlers.clear()
    monkeypatch.setitem(daemon.CFG, "gate_log_mirror", True)
    monkeypatch.setattr(daemon, "_logger", None)
    daemon._init_logger()
    daemon.log(GATE_LINES[0])
    for h in daemon._logger.handlers:
        h.flush()
    assert GATE_LINES[0] in gate.read_text(encoding="utf-8")
    assert GATE_LINES[0] in main.read_text(encoding="utf-8")


def test_stdout_handler_stays_unfiltered(split_logger):
    """journal 是统一排障账本：拆文件不许给它挂过滤。"""
    import logging.handlers
    shs = [h for h in daemon._logger.handlers
           if isinstance(h, logging.StreamHandler)
           and not isinstance(h, logging.handlers.RotatingFileHandler)]
    assert shs, "stdout handler 丢了"
    assert all(not h.filters for h in shs), "stdout 被加了 filter，journalctl 会缺行"


def test_gate_handler_is_distinguished_from_main(split_logger):
    assert daemon._logger_has_file() is True
    assert daemon._logger_has_gate_file() is True
    # 只留池闸 handler 时不能误判「主文件 handler 还在」（否则 stale 自愈失效）
    logging.getLogger("auto-switch-ip").handlers[:] = [
        h for h in daemon._logger.handlers if daemon._is_gate_handler(h)]
    assert daemon._logger_has_file() is False
    assert daemon._logger_has_gate_file() is True


def test_side_effects_off_never_creates_gate_log(tmp_path, monkeypatch):
    """pytest / 手工 import 不许往生产 gate.log 写假事件。"""
    gate = tmp_path / "gate.log"
    monkeypatch.setitem(daemon.CFG, "gate_log_file", str(gate))
    monkeypatch.setattr(daemon, "_side_effects_off", lambda: True)
    monkeypatch.setattr(daemon, "_logger", logging.getLogger("auto-switch-ip"))
    daemon._init_gate_log_handler()
    assert not gate.exists()


def test_config_defaults_and_env_override():
    assert config.CFG["gate_log_mirror"] is False
    assert config.CFG["gate_log_file"].endswith("gate.log")
    code = ("import config; print(config.CFG['gate_log_file']); "
            "print(config.CFG['gate_log_mirror'])")
    env = dict(os.environ, ASIP_GATE_LOG_FILE="/tmp/asip-gate-unit.log",
               ASIP_GATE_LOG_MIRROR="yes")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(PROJECT),
                       env=env, text=True, capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "/tmp/asip-gate-unit.log" in r.stdout
    assert "True" in r.stdout


def test_cli_conf_emits_gate_log_path():
    r = subprocess.run(["bash", str(PROJECT / "deploy" / "asip-cli-conf.sh"),
                        "--print"], cwd=str(PROJECT), text=True,
                       capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "ASIP_GATE_LOG=" in r.stdout


def test_gitignore_covers_gate_log_and_rotations():
    ignore = (PROJECT / ".gitignore").read_text(encoding="utf-8")
    assert "*.log*" in ignore, "gate.log / gate.log.1 会被误提交"
