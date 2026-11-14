"""①组保真契约 · 协议与时延散钉（附件 A11 /rotate 时延双钉、附件 C reason
归一、附件 D 右栏「二击出局+3s 突发并一击+双表同源」「限流只换 IP 不拉黑
节点」「_side_effects_off 判定顺序/日志假事件防线」「sidecar 预冷却 reason
撤销+只延长不缩短」「switchedAt 判重试」）。

【防什么事故】
  - 把 20s 部署实配「纠正」回 2s 代码默认 → 砍掉排队预算一个数量级，
    或反向改代码默认 → 实测分布选值丢失（附件 A11，两口径共存即契约）；
  - 未知 reason 落进「不标脏」桶 → 真被拦过的地址当干净回池（宁多勿漏）；
  - 二击出局被并发突发烧穿 → 好线误杀；双表不同源 → line_ok 早退永不命中；
  - 429 拉黑节点 → 429 恰证链路通，判死节点=冤杀（误杀池 50/56）；
  - pytest/演练进程写生产 daemon.log 假事件 → 事后 grep 得出假结论
    （「线上拦了 3 次」实为 0 次）；后门开关被写进 .env 重新打开写日志。
【事故出处】附件 A11、附件 C；D4 #11；四轮审查「严重」项。
【三分法】A 保真契约。
【取值】窗口/次数取 config 实配（transport_line_ok_* 代码兜底即生效值）。
"""
import os
import sys
import time
from pathlib import Path

import pytest

import daemon as d

ASIP_ROTATE_JS = Path(os.environ.get(
    "ASIP_ROTATE_JS",
    str(Path.home() / ".9router/open-sse/utils/asipRotate.js")))
NINE_ENV = Path(os.environ.get("ASIP_NINE_ENV", str(Path.home() / ".9router/.env")))
OPENCODE_JS = Path(os.environ.get(
    "ASIP_OPENCODE_JS",
    str(Path.home() / ".9router/open-sse/executors/opencode.js")))


# ---------- A11 /rotate 时延双钉 ----------

@pytest.mark.skipif(not ASIP_ROTATE_JS.exists(), reason="sidecar 源不可达")
def test_rotate_timeout_code_default_is_2s():
    """代码默认 2s（实测分布 min774/p50 1090/p90 1314 选值）是契约的一半。

    防什么事故：按 20s 部署值「纠正」代码默认 → 丢掉实测分布依据；反之亦然。
    """
    text = ASIP_ROTATE_JS.read_text(encoding="utf-8")
    compact = text.replace(" ", "")
    assert "ASIP_TIMEOUT_MS||2000" in compact  # 代码默认 = 2000ms（2s）


@pytest.mark.skipif(not NINE_ENV.exists(), reason="9router 部署 env 不可达")
def test_rotate_timeout_deployed_is_20s():
    """线上实配 ASIP_TIMEOUT_MS=20000 是契约的另一半（严禁按 2s「纠正」）。"""
    line = ""
    for ln in NINE_ENV.read_text(encoding="utf-8").splitlines():
        if ln.strip().startswith("ASIP_TIMEOUT_MS"):
            line = ln.strip()
    assert line == "ASIP_TIMEOUT_MS=20000"


# ---------- 附件 C reason 归一（宁多勿漏） ----------

def test_reason_whitelist_unknown_maps_to_rate429():
    """未知 reason 一律归 rate429（宁可多记一次限流，不放脏地址回池）。"""
    assert d._normalize_rotate_reason("something-new") == "rate429"
    assert d._normalize_rotate_reason("") == "rate429"
    assert d._normalize_rotate_reason(None) == "rate429"
    assert d._normalize_rotate_reason("rate429") == "rate429"
    assert d._normalize_rotate_reason("quota") == "rate429"


def test_reason_whitelist_known_buckets():
    """403/abuse→abuse403；transport/timeout/stall→transport；proactive→proactive。"""
    assert d._normalize_rotate_reason("403") == "abuse403"
    assert d._normalize_rotate_reason("abuse403") == "abuse403"
    assert d._normalize_rotate_reason("transport") == "transport"
    assert d._normalize_rotate_reason("timeout") == "transport"
    assert d._normalize_rotate_reason("stall") == "transport"
    assert d._normalize_rotate_reason("proactive") == "proactive"


# ---------- 二击出局 + 3s 突发并一击 + 双表同源 ----------

def test_line_ok_strike_burst_counts_once():
    """3s 内连发视为同一次突发只算一击（防一波并发烧穿二击出局，附件 D 右栏）。"""
    now = 1000.0
    assert d._line_ok_strike_add("2001:db8::k1", now) == 1
    assert d._line_ok_strike_add("2001:db8::k1", now + 1.0) == 1  # 突发并一击
    assert d._line_ok_strike_add("2001:db8::k1", now + 2.5) == 1
    assert d._line_ok_strike_add("2001:db8::k1", now + 20.0) == 2  # 第二击


def test_line_ok_strike_window_evicts():
    """二击出局窗口（_LINE_OK_STRIKE_WINDOW）外旧击清出计数。"""
    now = 1000.0
    d._line_ok_strike_add("2001:db8::k2", now)
    assert d._line_ok_strike_add(
        "2001:db8::k2", now + d._LINE_OK_STRIKE_WINDOW + 10) == 1


def test_probe_ok_recent_reads_both_tables():
    """line_ok 早退查两张表（_WARP_PROBE_SEEN 与换线前实测）同源（附件 D 右栏）。"""
    now = time.time()
    ip = "2001:db8::k3"
    d._WARP_PROBE_SEEN[("T1", ip)] = ("OK", now)
    assert d._probe_ok_recent(ip, now + 1, 1800) is True
    d._WARP_PROBE_SEEN.clear()
    d._PROBE_BEFORE_ROTATE["T1|" + ip] = {"verdict": "OK", "ts": now}
    assert d._probe_ok_recent(ip, now + 1, 1800) is True
    # 窗口外不命中
    assert d._probe_ok_recent(ip, now + 99999, 1800) is False


def test_line_ok_window_equals_probe_ok_ttl():
    """line_ok 窗口与探针结论 TTL 同源对齐（现行为：同为 probe_ok_ttl_sec）。"""
    w = d._warp()
    effective = float(w.get("transport_line_ok_window_sec", 1800))
    assert effective == float(w.get("probe_ok_ttl_sec", 1800))
    assert d._probe_ttl("OK") == float(w.get("probe_ok_ttl_sec", 1800))


# ---------- 限流类错误只换 IP 不拉黑节点 ----------

def test_rate429_never_blacklists_nodes():
    """429/403 记账只动 IP 台账，不进 _DEAD_NODES/bad_nodes（附件 D 右栏）。

    防什么事故：429 恰证链路通（换 IP 即恢复），判死节点=冤杀（误杀池 50/56）。
    判死只归真实探测（_apply_test_results）+ 保底阀。
    """
    d._DEAD_NODES.clear()
    d._mark_warp_ip_used("2001:db8::n1", time.time(), blocked=True,
                         kind="rate429", ban_after=60.0)
    assert len(d._DEAD_NODES) == 0  # 只标 IP，不判死节点


# ---------- _side_effects_off 判定顺序（日志假事件防线） ----------

def test_side_effects_off_wins_over_file_log_backdoor():
    """先认测试/演练、再认后门：pytest 在场时 ASIP_ALLOW_FILE_LOG=1 也无效。

    防什么事故：.env 里一行 ASIP_ALLOW_FILE_LOG=1 就让 pytest 重新往生产
    daemon.log 写假事件（四轮审查「严重」项）。
    """
    os.environ["ASIP_ALLOW_FILE_LOG"] = "1"
    try:
        assert d._side_effects_off() is True  # 测试识别优先于后门
    finally:
        os.environ.pop("ASIP_ALLOW_FILE_LOG", None)


def test_no_invocation_id_means_no_file_log():
    """非 systemd 拉起（无 INVOCATION_ID）一律只写 stdout。"""
    saved = os.environ.pop("ASIP_ALLOW_FILE_LOG", None)
    saved_inv = os.environ.pop("INVOCATION_ID", None)
    saved_mod = sys.modules.pop("pytest", None)
    try:
        assert d._side_effects_off() is True  # 无 INVOCATION_ID → 不写文件
    finally:
        if saved_mod is not None:
            sys.modules["pytest"] = saved_mod
        if saved is not None:
            os.environ["ASIP_ALLOW_FILE_LOG"] = saved
        if saved_inv is not None:
            os.environ["INVOCATION_ID"] = saved_inv


def test_state_paths_isolated_from_project_dir():
    """测试进程的状态文件路径被根夹具指到 tmp，不指向项目/生产目录（隔离防线）。"""
    for fn in (d._warp_ip_used_path, d._warp_line_state_path, d._segment_path):
        p = str(fn())
        assert "/tmp" in p or "pytest" in p or "asip-test" in p or \
            not p.startswith(str(d.PROJECT_DIR)), f"{fn.__name__} 指向生产目录: {p}"
        assert not p.endswith(".warp_ip_used.json") or "/tmp" in p


# ---------- sidecar 预冷却契约（源级：reason 撤销 + 只延长不缩短 + switchedAt 判重试） ----------

@pytest.mark.skipif(not ASIP_ROTATE_JS.exists(), reason="sidecar 源不可达")
def test_sidecar_cooldown_revoke_and_extend_only():
    """预冷却：端口无责结论撤销冷却；重打走 avoid_port_until；只延长不缩短（附件 D 右栏）。
    """
    text = ASIP_ROTATE_JS.read_text(encoding="utf-8")
    # reason 撤销白名单（already_retired/coalesced/too_frequent/line_ok）
    for r in ("already_retired", "coalesced", "too_frequent", "line_ok"):
        assert r in text
    # avoid_port_until 重打
    assert "avoid_port_until" in text
    # 只延长不缩短（max 语义）
    assert "Math.max(proxyCooldowns.get(key) || 0, wanted)" in text \
        or "只延长不缩短" in text


@pytest.mark.skipif(not ASIP_ROTATE_JS.exists(), reason="sidecar 源不可达")
def test_sidecar_retry_judged_by_switched_at_not_switched():
    """重试判断一律用 switchedAt 与请求时刻比较，不用 switched 旗标（附件 D 右栏）。

    防什么事故：coalesced 时 switched=false 但确实有人换过线——按旗标判
    「不值得重试」→ 客户端继续骑死隧道。
    """
    text = ASIP_ROTATE_JS.read_text(encoding="utf-8")
    assert "判断一律用 switchedAt" in text or "switchedAt 与请求" in text
