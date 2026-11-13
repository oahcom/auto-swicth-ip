#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/trace_req.py` 的契约测试。

这个工具的用途是「按 req= 把一次请求散在日志里的 5~10 行拼回一条时间线」，
排查 429 归因时靠它重建 A→B→C。它出错的方式都很安静 —— 拼错一行不会报错，
只会让人得出错误结论，所以测试只盯「会不会安静地拼错」：

① 出口标识取错字段 —— 两种换线共用一个 /rotate 端点，出口在 to_ip / to_node /
   to_line 三个字段里轮流转。取错就是「这条请求去了哪」整个是错的。

② 时间戳错位 —— [ASIP] rotate 是 console.warn 直出，行首没有 [HH:MM:SS]。
   不继承上一行时间，全部 hop 的时间都会变成 None，时间线就废了。

③ 把 '-' 当成有效值 —— 日志用 '-' 表示字段缺失。当成值会让 chain 里混进一个
   假的出口，去重和 /64 计数全错。

样本全部取自 2026-11-13 真实日志，不是构造的。
"""
import importlib.util
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent
TOOL = PROJECT / "tools" / "trace_req.py"


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("trace_req", TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── 真实日志样本 ────────────────────────────────────────────────────────────
# WARP 槽位轮换：出口是真实 IPv6，在 to_ip
SAMPLE_WARP_HOP = (
    "[ASIP] rotate req=wdweyqd0m port=http://127.0.0.1:7890 reason=ok switched=true "
    "restart=false line=- ip=- verdict=n/a to=slot-3 to_line=WARP-H2-IST-80 "
    "to_ip=2a09:bac5:58c3:d2d::150:78 to_node=- to_url=http://127.0.0.1:7893"
)

# 代理池节点轮换：to_ip 是 '-'，出口在 to_node / to_line
SAMPLE_POOL_HOP = (
    "[ASIP] rotate req=wdxev0999 port=http://127.0.0.1:7890 reason=ok switched=true "
    "restart=false line=- ip=- verdict=n/a to=slot-7 to_line=pq.us07.yydjc.top:443-vl "
    "to_ip=- to_node=pq.us07.yydjc.top:443-vl to_url=http://127.0.0.1:7897"
)

# 借道：borrowed=1，daemon 回 reason=borrowed_skip
SAMPLE_BORROW_HOP = (
    "[ASIP] rotate req=wdweshi0k port=:7893 reason=borrowed_skip borrowed=1 "
    "switched=true restart=false line=WARP-H2-IST-80 ip=2a09:bac5:58c3:d2d::150:78 "
    "verdict=OK to=slot-7 to_line=WARP-H2-HKG-69 to_ip=2a09:bac5:495e:e6::17:341 "
    "to_url=http://127.0.0.1:7897"
)

SAMPLE_POST = (
    "[03:13:45] 🟡 ▶ POST 9router_hermes → opencode/glm-5.3-flash-free · "
    "FMT: openai→openai · STREAM · 2 MSG · req=wdxev0999"
)

SAMPLE_DONE = (
    "[03:36:30] 🟡 📊 DONE 9865ms · IN 7150 (CACHE ↻5376) · OUT 1213 · req=wdy7wjt0g"
)


def _feed(mod, lines):
    traces, state = {}, {"ts": None}
    for ln in lines:
        if "req=" in ln:
            mod.build(traces, ln, state)
    return traces


# ── ① 出口标识的字段回退 ───────────────────────────────────────────────────
def test_egress_id_prefers_to_ip(mod):
    """WARP 槽位换线：to_ip 有真值时必须用它，不能被 to_node 的 '-' 抢走。"""
    kv = dict(mod.KV.findall(SAMPLE_WARP_HOP))
    assert mod.egress_id(kv) == "2a09:bac5:58c3:d2d::150:78"


def test_egress_id_falls_back_to_node_when_to_ip_missing(mod):
    """代理池换线：to_ip='-' 要退回 to_node，否则这条请求等于「没换过线」。"""
    kv = dict(mod.KV.findall(SAMPLE_POOL_HOP))
    assert kv["to_ip"] == "-"
    assert mod.egress_id(kv) == "pq.us07.yydjc.top:443-vl"


def test_egress_id_ignores_dash_everywhere(mod):
    """三个字段全是 '-' 时返回 None，绝不能返回字符串 '-' 混进 chain。"""
    assert mod.egress_id({"to_ip": "-", "to_node": "-", "to_line": "-"}) is None
    assert mod.egress_id({}) is None


# ── ② 时间戳继承 ───────────────────────────────────────────────────────────
def test_rotate_line_inherits_timestamp(mod):
    """rotate 行没有 [HH:MM:SS] 前缀，必须继承上一行，否则 hop 时间全是 None。"""
    traces = _feed(mod, [SAMPLE_POST, SAMPLE_POOL_HOP])
    tr = traces["wdxev0999"]
    assert tr.start_ts == "03:13:45"
    assert tr.hops[0]["_ts"] == "03:13:45", "hop 时间没有继承上一行"


def test_timestamp_advances_with_prefixed_lines(mod):
    """后续带时间戳的行要刷新继承值，否则整条时间线会冻结在第一秒。"""
    later_post = SAMPLE_POST.replace("[03:13:45]", "[03:13:52]")
    traces = _feed(mod, [SAMPLE_POST, SAMPLE_POOL_HOP, later_post, SAMPLE_WARP_HOP])
    assert traces["wdxev0999"].hops[0]["_ts"] == "03:13:45"
    # 第二个 hop 属于另一个 req，应拿到刷新后的时间
    assert traces["wdweyqd0m"].hops[0]["_ts"] == "03:13:52"


# ── ③ chain 与统计 ────────────────────────────────────────────────────────
def test_chain_dedups_consecutive_same_egress(mod):
    """连续两次换到同一出口只算一个（daemon 并发抢锁时会重复下发）。"""
    a = SAMPLE_WARP_HOP.replace("req=wdweyqd0m", "req=x1")
    b = SAMPLE_WARP_HOP.replace("req=wdweyqd0m", "req=x1")
    tr = _feed(mod, [a, b])["x1"]
    assert tr.chain() == ["2a09:bac5:58c3:d2d::150:78"]


def test_chain_preserves_real_switch_sequence(mod):
    """A→B 两次不同出口要保留顺序，这是「重建链路」的全部价值。"""
    a = SAMPLE_WARP_HOP.replace("req=wdweyqd0m", "req=x2")
    b = (SAMPLE_WARP_HOP.replace("req=wdweyqd0m", "req=x2")
         .replace("2a09:bac5:58c3:d2d::150:78", "2a09:bac5:495e:e6::17:341"))
    tr = _feed(mod, [a, b])["x2"]
    assert tr.chain() == ["2a09:bac5:58c3:d2d::150:78",
                          "2a09:bac5:495e:e6::17:341"]


def test_borrowed_flag_is_recorded(mod):
    tr = _feed(mod, [SAMPLE_BORROW_HOP])["wdweshi0k"]
    assert tr.hops[0].get("borrowed") == "1"
    assert tr.hops[0]["reason"] == "borrowed_skip"


# ── ④ 成功/失败判定 ───────────────────────────────────────────────────────
def test_done_marks_success(mod):
    tr = _feed(mod, [SAMPLE_DONE])["wdy7wjt0g"]
    assert tr.is_success()
    assert "9865ms" in tr.done


def test_rotate_without_done_is_not_success(mod):
    """只有换线没有 DONE = 这次请求最终失败。不能因为「有记录」就算成功。"""
    tr = _feed(mod, [SAMPLE_POST, SAMPLE_POOL_HOP])["wdxev0999"]
    assert not tr.is_success()


# ── ⑤ seg64 ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("ip,expect", [
    ("2a09:bac5:58c3:d2d::150:78", "2a09:bac5:58c3:d2d"),
    ("2a09:bac1:72e0:18::150:fb", "2a09:bac1:72e0:18"),
    ("2606:4700::6812:a42", "2606:4700::6812"),
    ("45.95.212.167", "45.95.212.167"),   # IPv4 原样返回
    ("pq.us07.yydjc.top:443-vl", "pq.us07.yydjc.top:443-vl"),
])
def test_seg64(mod, ip, expect):
    assert mod.seg64(ip) == expect


# ── ⑤b 主机名 / IPv6 的区分 ────────────────────────────────────────────────
def test_is_ip6_rejects_hostname_with_colon(mod):
    """代理池节点也含冒号，不能用「含冒号」当 IPv6 判据。"""
    assert mod.is_ip6("2a09:bac5:58c3:d2d::150:78")
    assert not mod.is_ip6("ttll20-ooi.dankenet.help:64657-any")
    assert not mod.is_ip6("pq.us07.yydjc.top:443-vl")
    assert not mod.is_ip6("")


# ── ⑤c req id 被括号污染（真实踩过的坑） ──────────────────────────────────
def test_req_id_not_polluted_by_trailing_paren(mod):
    """`[EGRESS] retry ... (req=x)` 的右括号会被 \\S+ 吃进 req 值，
    导致同一个请求被拆成两个 trace、事件各丢一半。必须用紧凑字符集提。"""
    line = ("[03:41:34] ⚠️  [EGRESS] retry 1/2 429 -> accounted on "
            "http://127.0.0.1:7897 (ok) (req=wdyejb01m)")
    traces = _feed(mod, [line])
    assert list(traces) == ["wdyejb01m"], "req id 被污染：%s" % (list(traces),)
    assert traces["wdyejb01m"].events[0]["_kind"] == "retry"


# ── ⑤d retry 记账挂到对应的 hop 上 ────────────────────────────────────────
def test_retry_accounting_attached_to_hop(mod):
    """retry 行说「这次 429 记在哪个端口」—— 归因排查的关键证据，
    必须挂到它前一个 rotate 上。"""
    hop = SAMPLE_POOL_HOP.replace("req=wdxev0999", "req=y1")
    retry = ("[03:41:34] ⚠️  [EGRESS] retry 1/2 429 -> accounted on "
             "http://127.0.0.1:7897 (ok) (req=y1)")
    tr = _feed(mod, [hop, retry])["y1"]
    assert tr.hops[0]["acct_port"] == "http://127.0.0.1:7897"
    assert tr.hops[0]["acct_status"] == "ok"
    assert tr.hops[0]["acct_try"] == "1/2"


def test_rotated_event_recorded(mod):
    hop = SAMPLE_POOL_HOP.replace("req=wdxev0999", "req=y2")
    rotated = ("[03:41:32] ⚠️  [EGRESS] asip rotated egress (ok) -> retry 1/2 "
               "on fresh connection (req=y2)")
    tr = _feed(mod, [hop, rotated])["y2"]
    kinds = [e["_kind"] for e in tr.events]
    assert kinds == ["rotate", "rotated"]


# ── ⑤e 同 ip 兜底的告警 ───────────────────────────────────────────────────
def test_same_ip_across_hops_warns(mod):
    """3 次换线 ip= 全一样 = daemon 用 active_egress 兜底，不是真相。
    工具必须把这一点打在脸上，否则又会被拿去数「哪个 IP 承载了多少发」。"""
    hops = [SAMPLE_BORROW_HOP.replace("req=wdweshi0k", "req=z1") for _ in range(3)]
    tr = _feed(mod, hops)["z1"]
    out = tr.render()
    assert "active_egress 兜底" in out


# ── ⑥ 端到端：从文件读，能出时间线 ────────────────────────────────────────
def test_end_to_end_from_file(mod, tmp_path, monkeypatch, capsys):
    log = tmp_path / "n9.log"
    log.write_text("\n".join([
        "Nov 13 03:13:45 HOST npm[1]: " + SAMPLE_POST,
        "Nov 13 03:13:45 HOST npm[1]: " + SAMPLE_POOL_HOP,
        "Nov 13 03:13:50 HOST npm[1]: [03:13:50] 🟡 📊 DONE 5000ms · IN 10 · OUT 2 · req=wdxev0999",
    ]), encoding="utf-8")
    monkeypatch.setattr(mod.sys, "argv", ["trace_req.py", "--file", str(log), "--req", "wdxev0999"])
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "换线 1 次" in out
    assert "pq.us07.yydjc.top:443-vl" in out
    assert "成功" in out
