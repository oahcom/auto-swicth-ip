"""①组保真契约 · /rotate 主路径流程（附件 A2 合并窗记账后置、A5 当前出口
veto、A12 前台免等三件套、附件 B1/B6 顺序敏感、transport/proactive 不标脏、
coalesced≠不值得重试）。

【防什么事故】
- 合并窗挪到记账之前 → 第一条换线后其余全命中窗，真请求的线标不到脏，
  坏 IP 反复回池（附件 A2）；窗值拉长/改错 → 吞证据。
- transport/proactive 当 IP 级封禁证据 → 换线还标脏，好线被冤杀（附件 D
  「transport 误切」右栏）。
- 免等三件套被「队列化」改掉 → 排队吃掉客户端 2s 预算；回旧身份 → 调用方
  用切换前的旧线覆盖账目（附件 A12，2026-11-14 wfgkef17a 事故族）。
- 后台修整在回包定型前启动 → 与秒切挑线并发改同一份状态表（附件 B6）。
【事故出处】附件 A2/A12、附件 B1/B6；D4 #2/#12。
【三分法】A 保真契约。
【取值】合并窗取 api.coalesce_ms 实配（config 800ms），禁注释值。
"""
import time
from collections import defaultdict

import pytest

import daemon as d


@pytest.fixture
def rotate_env(monkeypatch):
    """_rotate_once 离线跑桩：钉住外部依赖，记录记账/换线/后台修整次序。"""
    events = {"mark": [], "fast": 0, "bg": [], "seq": []}
    monkeypatch.setattr(d, "_api_stats", defaultdict(int))
    monkeypatch.setattr(d, "active_warp_tag", lambda: "WARP-TEST-A")
    monkeypatch.setattr(d, "_warp_line_ip",
                        lambda tag: "2001:db8::f1" if tag == "WARP-TEST-A"
                        else "2001:db8::f2")
    monkeypatch.setattr(d, "_warp_line_at", lambda ts: None)
    monkeypatch.setattr(d, "_exit_ledger_since_install", lambda ip: None)

    def _mark(ip, now, blocked=False, kind=None, **kw):
        events["mark"].append((ip, blocked, kind))

    monkeypatch.setattr(d, "_mark_warp_ip_used", _mark)

    def _fast(reason):
        events["fast"] += 1
        return {"tag": "WARP-TEST-B", "egress": "2001:db8::f2"}

    monkeypatch.setattr(d, "_fast_switch", _fast)

    def _bg(*a, **k):
        events["bg"].append(a)
        events["seq"].append("bg")

    monkeypatch.setattr(d, "_kick_background_repair", _bg)
    monkeypatch.setattr(d, "_reconnect_warp_by_table", lambda *a, **k: False)
    monkeypatch.setattr(d, "_rotate_gap", lambda: 0.0)
    d._last_rotate_ts = 0.0
    return events


def test_accounting_before_coalesce_window(rotate_env):
    """合并窗内不换线但**证据照收**——记账在合并判定之前（附件 B1/A2）。

    防什么事故：先合并后记账 → 一批并发 429 只有第一条留下证据，其余被
    窗吞掉，真正在用的坏线标不到脏、下一批原样选回来。
    """
    d._last_rotate_ts = 0.0
    d._rotate_once("rate429", egress_ip="2001:db8::bad1")
    assert rotate_env["mark"] and rotate_env["mark"][0][1] is True  # 首条记脏
    out2 = d._rotate_once("rate429", egress_ip="2001:db8::bad2")
    assert out2["reason"] == "coalesced" and out2["switched"] is False
    assert len(rotate_env["mark"]) == 2  # 合并窗内两条证据都在（记账后置）
    assert all(m[1] is True for m in rotate_env["mark"])
    assert rotate_env["fast"] == 1  # 不重复换线


def test_coalesce_window_from_config(rotate_env):
    """合并窗宽度取 api.coalesce_ms 实配，窗口外不合并（取值纪律）。"""
    w_ms = float(d._api_cfg()["coalesce_ms"])
    assert w_ms == float(d.CFG["api"]["coalesce_ms"])  # 同一实配来源
    d._last_rotate_ts = time.time() - (w_ms / 1000.0) - 1.0
    out = d._rotate_once("rate429", egress_ip="2001:db8::bad3")
    assert out.get("reason", "switched") != "coalesced"
    assert w_ms == 800.0  # 现状实配（800ms，非旧文档 20~45s/6s 的任何变体）


def test_late_evidence_attribution_three_states(rotate_env, monkeypatch):
    """迟到回溯三态（附件 C）：不知→当前线；轮换池→不标脏；另一线→归那条线。

    防什么事故：迟到 429 归错线 —— 新地址刚换出来、干净，却替旧地址背
    24h 惩罚期（_rotate_once 回溯注释记载的实测）。
    """
    # ① 不知道（无 request_started_at / 查不到）→ 保持当前线记账
    d._last_rotate_ts = 0.0
    d._rotate_once("rate429", egress_ip="2001:db8::x1")
    assert rotate_env["mark"][-1][0] == "2001:db8::f1"
    # ② 发出时刻在轮换池（hist_tag=None）→ 不标脏（没有可归的线）
    monkeypatch.setattr(d, "_warp_line_at", lambda ts: (None, None))
    d._last_rotate_ts = 0.0
    n = len(rotate_env["mark"])
    d._rotate_once("rate429", request_started_at=1.0, egress_ip="2001:db8::x2")
    assert len(rotate_env["mark"]) == n  # 不新增记脏
    # ③ 发出时刻在另一条线 → 归那条线当时的地址
    monkeypatch.setattr(d, "_warp_line_at",
                        lambda ts: ("WARP-OLD", "2001:db8::old-ip"))
    d._last_rotate_ts = 0.0
    d._rotate_once("rate429", request_started_at=1.0, egress_ip="2001:db8::x3")
    assert rotate_env["mark"][-1][0] == "2001:db8::old-ip"


def test_transport_and_proactive_not_marked_dirty(rotate_env):
    """transport/proactive 不是 IP 级封禁证据：换线但不标脏（附件 D 右栏）。
    """
    d._last_rotate_ts = 0.0
    d._rotate_once("transport", egress_ip="2001:db8::t1")
    assert rotate_env["mark"] == []  # 无任何标脏记账
    d._last_rotate_ts = 0.0
    d._rotate_once("proactive", egress_ip="2001:db8::t2")
    assert rotate_env["mark"] == []


def test_guard_busy_returns_coalesced_line_unknown_no_identity(rotate_env):
    """拿不到换闸锁 → coalesced + line_unknown + **不回旧身份**（附件 A12）。

    防什么事故：这一瞬间出口正被另一次切换改写，快照是旧线；回旧身份 →
    调用方拿它覆盖自己的账，下一条 429 带着错误出口来。排队等锁则吃掉
    客户端预算（9router 2s 挂断）。
    """
    assert d._SWITCH_GUARD.acquire(blocking=False)
    try:
        out = d._rotate_once("rate429", egress_ip="2001:db8::bad4")
    finally:
        d._SWITCH_GUARD.release()
    assert out["reason"] == "coalesced"
    assert out["ok"] is True  # coalesced ≠ 不值得重试
    assert out["line_unknown"] is True
    assert "active_tag" not in out and "active_egress" not in out
    assert rotate_env["fast"] == 0  # 不排队、不重复换


def test_coalesced_is_ok_not_failure(rotate_env):
    """窗口合并回 ok=True：coalesced 语义=「别人刚换过，重连即可」（附件 A12）。
    """
    d._last_rotate_ts = time.time()
    out = d._rotate_once("rate429", egress_ip="2001:db8::bad5")
    assert out["ok"] is True and out["reason"] == "coalesced"


def test_background_repair_after_response_finalized(rotate_env, monkeypatch):
    """后台修整在回包定型**之后**启动（附件 B6，顺序即语义）。"""
    d._last_rotate_ts = 0.0
    orig_payload = d._with_active_line

    def _payload(*a, **k):
        rotate_env["seq"].append("payload")
        return orig_payload(*a, **k)

    monkeypatch.setattr(d, "_with_active_line", _payload)
    d._rotate_once("rate429", egress_ip="2001:db8::bad6")
    assert rotate_env["seq"] == ["payload", "bg"]  # 先定型回包，再动后台


def test_current_egress_veto_never_answers_already_retired(rotate_env,
                                                           monkeypatch):
    """报障地址==当前出口时否定「已换走」，走真换线（附件 A5 当前出口 veto）。

    防什么事故：exit ledger 只看「曾经被换走」，IP 回收后同一 /128 再回来
    旧记录还在，判「已换走」→ 原地重连还是死地址（2026-11-14 04:58
    wfgkef17a 三连撞同死 IP 白烧 34s）。「ip==当前出口」只用于否定。
    """
    ip = "2001:db8::cur"
    monkeypatch.setattr(d, "_warp_line_ip", lambda tag: ip)
    now = time.time()
    d._exit_ledger_note(ip, "2001:db8::other", now - 5.0, "WARP-TEST-B")
    out = d._rotate_once("rate429", request_started_at=now - 10.0, egress_ip=ip)
    assert out.get("reason") != "already_retired"


def test_retired_short_circuits_without_burning_line(rotate_env, monkeypatch):
    """迟到报障（已换走）只回 already_retired，不烧新线（附件 A5 只否不肯定）。
    """
    monkeypatch.setattr(d, "_warp_line_ip", lambda tag: "2001:db8::cur2")
    now = time.time()
    d._exit_ledger_note("2001:db8::gone", "2001:db8::cur2", now - 5.0,
                        "WARP-TEST-B")
    out = d._rotate_once("rate429", request_started_at=now - 10.0,
                         egress_ip="2001:db8::gone")
    assert out["reason"] == "already_retired"
    assert out["switched"] is False
    assert rotate_env["fast"] == 0  # 不烧新线
    assert rotate_env["mark"] == []  # 回溯与记账都不执行
