"""asip gate（第七闸口日志命令）的 CLI 回归测试。

只读、不依赖运行中的 daemon：用临时 cli.env 把脚本与生产环境隔离。
重点钉死三件事 —— 命令清单/分发一致、events 与全量日志的过滤分层、
以及没有 daemon 时的优雅降级（不能甩 traceback 或假装成功）。
"""

import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
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


GATE_LOG_SAMPLE = """\
[2026-11-13 02:10:01] ⇢ slot-7 报 rate429（ok）[req=separate] 池节点=nX，重试改走 slot-7
[2026-11-13 02:10:02] ↻ 第七闸口池内轮换：nX → nY（独立账本专有行）
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


def test_status_merges_gate_log_with_main_log_history():
    """拆文件过渡期：近 1h 统计不能只读 gate.log，否则重启前的账凭空消失。"""
    src = CLI.read_text(encoding="utf-8")
    assert 'ASIP_LOG_PATH="$GATE_LOG" ASIP_MAIN_LOG="$LOG"' in src
    assert "if row in seen:" in src       # mirror 模式同落两份要去重
    assert "rows.sort()" in src
    assert "_follow \"$_RE_GATE\" \"$n\" \"$GATE_LOG\"" in src
    assert 'grep -E "$_RE_GATE_EVENT" "$GATE_LOG"' in src


def test_events_excludes_high_frequency_429_reports(fake_env):
    r = run_cli(fake_env, "gate", "events", "10")
    assert r.returncode == 0, r.stderr
    assert "第七闸口池内轮换" in r.stdout
    assert "秒切兜底" in r.stdout
    assert "秒切第七闸口" in r.stdout
    assert "slot-7 报" not in r.stdout          # 高频报障行被滤掉
    assert "重试改走 slot-7" not in r.stdout
    assert "普通健康行" not in r.stdout


def test_events_reads_separate_gate_log(fake_env, tmp_path):
    """设置里给了 ASIP_GATE_LOG，events 就只读独立账本，不再扫 daemon.log。"""
    gate = tmp_path / "gate.log"
    gate.write_text(GATE_LOG_SAMPLE, encoding="utf-8")
    conf = Path(fake_env["ASIP_CLI_ENV"])
    conf.write_text(conf.read_text(encoding="utf-8")
                    + "ASIP_GATE_LOG=%s\n" % gate, encoding="utf-8")
    r = run_cli(fake_env, "gate", "events", "10")
    assert r.returncode == 0, r.stderr
    assert "独立账本专有行" in r.stdout
    assert "n1 → n2" not in r.stdout          # 主日志里的池内轮换不该混进来
    assert "slot-7 报" not in r.stdout         # 高频报障仍被 events 滤掉


def test_events_falls_back_to_main_log_when_gate_log_missing(fake_env, tmp_path):
    """服务还没重启 / gate.log 未生成时，命令不能空转。"""
    conf = Path(fake_env["ASIP_CLI_ENV"])
    conf.write_text(conf.read_text(encoding="utf-8")
                    + "ASIP_GATE_LOG=%s\n" % (tmp_path / "not-created.log"),
                    encoding="utf-8")
    r = run_cli(fake_env, "gate", "events", "10")
    assert r.returncode == 0, r.stderr
    assert "第七闸口池内轮换" in r.stdout
    assert "独立账本专有行" not in r.stdout


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


# ── gate st 兜底文案：池闸健康决定「唯一兜底」是否成立 ─────────────────────
# /slots 取数路径与生产一致：run/rotate.sock（UDS HTTP）+ api-token。

def _make_slots_json(*, gate, warp_statuses=("bad",) * 6):
    """常规闸口默认全部不可选（0/6），由 gate 单独控制池闸健康。"""
    slots = [{"name": "WARP-%d" % i, "kind": "warp", "status": s}
             for i, s in enumerate(warp_statuses)]
    slots.append(gate)
    return json.dumps({"slots": slots, "active_tag": "pool"})


@pytest.fixture
def fake_slots_server(tmp_path):
    """假 daemon /slots 接口：rotate.sock + api-token，供 gate st 真实走一遍取数。"""
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    token = "gate-test-token"
    (run / "api-token").write_text(token, encoding="utf-8")
    sock_path = run / "rotate.sock"
    if sock_path.exists():
        sock_path.unlink()
    state = {"body": "{}"}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("X-Asip-Token") != token:
                self.send_response(403)
                self.end_headers()
                return
            body = state["body"].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class UDSServer(socketserver.UnixStreamServer):
        def get_request(self):
            c, _ = super().get_request()
            return c, ("localhost", 0)      # http.server 要求 (host, port) 形状

    server = UDSServer(str(sock_path), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def test_gate_st_pool_ok_when_no_warp_available(fake_env, fake_slots_server):
    """常规 0/6 + 池闸 ok → 兜底在位，池闸是唯一兜底。"""
    fake_slots_server["body"] = _make_slots_json(
        gate={"name": "pool", "kind": "proxy", "port": 17897,
              "status": "ok", "serving": True})
    r = run_cli(fake_env, "gate", "st", "2")
    assert r.returncode == 0, r.stderr
    assert "常规闸口可选: 0/6" in r.stdout
    assert "全部不可选，池闸是唯一兜底" in r.stdout


def test_gate_st_pool_dead_when_no_warp_available(fake_env, fake_slots_server):
    """常规 0/6 + 池闸也不健康 → 不能谎称兜底在位。"""
    fake_slots_server["body"] = _make_slots_json(
        gate={"name": "pool", "kind": "proxy", "port": 17897,
              "status": "bad", "verdict": "pool_dead", "serving": False})
    r = run_cli(fake_env, "gate", "st", "2")
    assert "无任何可用兜底" in r.stdout
    assert "pool_dead" in r.stdout
    assert "唯一兜底" not in r.stdout


def test_gate_st_pool_status_missing(fake_env, fake_slots_server):
    """/slots 没带池闸 status → 显示状态未知，不妄称兜底在位。"""
    fake_slots_server["body"] = _make_slots_json(
        gate={"name": "pool", "kind": "proxy", "port": 17897,
              "serving": False})
    r = run_cli(fake_env, "gate", "st", "2")
    assert "池闸状态未知" in r.stdout
    assert "唯一兜底" not in r.stdout
