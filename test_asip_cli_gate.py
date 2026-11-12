"""asip gate（第七闸口日志命令）的 CLI 回归测试。

只读、不依赖运行中的 daemon：用临时 cli.env 把脚本与生产环境隔离。
重点钉死三件事 —— 命令清单/分发一致、events 与全量日志的过滤分层、
以及没有 daemon 时的优雅降级（不能甩 traceback 或假装成功）。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent
CLI = PROJECT / "deploy" / "asip-cli.sh"

SAMPLE_LOG = """\
[2026-11-13 02:00:01] ⇢ slot-7 报 rate429（ok）[req=a] 池节点=n1，重试改走 http://127.0.0.1:7897
[2026-11-13 02:00:02] ↻ 第七闸口池内轮换：n1 → n2（rate429）
[2026-11-13 02:00:03] · 秒切兜底：常规闸口全部不可选，第七闸口（代理池）接手（池节点=n2）
[2026-11-13 02:00:04] ⇢ slot-2 报 rate429（ok）[req=b] 线=WARP-CF4 重试改走 slot-7 目标url=http://127.0.0.1:7897
[2026-11-13 02:00:05] ⚡ 秒切第七闸口：主出口翻到轮换池（原因=rate429）
[2026-11-13 02:00:06] 普通健康行，不含池闸关键词
"""


@pytest.fixture
def fake_env(tmp_path):
    """最小合法 cli.env：脚本开头的清单白名单校验必须能过。"""
    log = tmp_path / "daemon.log"
    log.write_text(SAMPLE_LOG, encoding="utf-8")
    conf = tmp_path / "cli.env"
    conf.write_text(
        "\n".join([
            "ASIP_DIR=%s" % tmp_path,
            "ASIP_PY=%s" % sys.executable,
            "ASIP_LOG=%s" % log,
            "ASIP_SECRET_FILE=%s" % (tmp_path / "secret"),
            "ASIP_CLASH_API=http://localhost:9090",
            "ASIP_WARP_SELECTOR=opencode-out",
            "ASIP_POOL_SELECTOR=proxy",
            "ASIP_SOCKS_HOST=localhost",
            "ASIP_SERVICE=asip-cli-gate-test.service",
            "ASIP_SYSTEMD_SCOPE=system",
            "ASIP_UNIT_DIR=%s" % (tmp_path / "units"),
            "ASIP_WARP_COUNT=1",
            "ASIP_WARP_MEMBERS='WARP-TEST 1080 usque-warp-test.service'",
            "",
        ]),
        encoding="utf-8",
    )
    return dict(os.environ, ASIP_CLI_ENV=str(conf))


def run_cli(env, *args, timeout=25):
    return subprocess.run(
        ["bash", str(CLI), *args],
        env=env, text=True, capture_output=True, timeout=timeout,
    )


def test_cli_syntax_ok():
    assert subprocess.run(["bash", "-n", str(CLI)]).returncode == 0


def test_usage_and_gate_help_list_all_subcommands(fake_env):
    top = run_cli(fake_env, "-h")
    sub = run_cli(fake_env, "gate", "-h")
    assert top.returncode == 0 and sub.returncode == 0
    for out in (top.stdout, sub.stdout):
        assert "asip gate" in out
        assert "gate log" in out
        assert "gate events" in out
        assert "gate 9r" in out


def test_dispatch_aliases(fake_env):
    src = CLI.read_text(encoding="utf-8")
    assert 'gate|slot7|g7) shift; cmd_gate "$@" ;;' in src
    for alias in ("slot7", "g7"):
        r = run_cli(fake_env, alias, "-h")
        assert r.returncode == 0, r.stderr
        assert "asip gate —" in r.stdout


def test_no_hardcoded_site_values():
    """与 test_portability.py 同口径：站点值只能来自 cli.env / /slots。"""
    src = CLI.read_text(encoding="utf-8")
    assert "/home/" not in src
    assert "usque-warp.service" not in src
    assert "127.0.0.1:" not in src
    assert "7897" not in src          # 池闸端口从 /slots 的 kind=proxy 读


def test_gate_regex_is_single_source():
    """统计与日志过滤共用一份正则，避免改了一边另一边悄悄漂移。"""
    src = CLI.read_text(encoding="utf-8")
    assert 'GATE_RE="$_RE_GATE"' in src
    assert 're.compile(os.environ["GATE_RE"])' in src
    assert "_RE_GATE_EVENT='第七闸口|秒切兜底'" in src


def test_events_excludes_high_frequency_429_reports(fake_env):
    r = run_cli(fake_env, "gate", "events", "10")
    assert r.returncode == 0, r.stderr
    assert "第七闸口池内轮换" in r.stdout
    assert "秒切兜底" in r.stdout
    assert "秒切第七闸口" in r.stdout
    assert "slot-7 报" not in r.stdout          # 高频报障行被滤掉
    assert "重试改走 slot-7" not in r.stdout
    assert "普通健康行" not in r.stdout


def test_bad_line_count_is_rejected(fake_env):
    r = run_cli(fake_env, "gate", "log", "abc")
    assert r.returncode != 0
    assert "行数需为正整数" in r.stderr


def test_unknown_subcommand_is_rejected(fake_env):
    r = run_cli(fake_env, "gate", "nope")
    assert r.returncode != 0
    assert "未知子命令" in r.stderr


def test_status_degrades_without_daemon(fake_env):
    """没有 rotate.sock / api-token 时要说清原因，不能 traceback 或假装成功。"""
    r = run_cli(fake_env, "gate", "st", "2")
    assert r.returncode != 0
    assert "读不到 /slots" in r.stderr
    assert "日志仍可查" in r.stderr
    assert "Traceback" not in (r.stdout + r.stderr)
