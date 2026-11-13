#!/usr/bin/env python3
"""台账生命周期改造测试（docs/design-20261113-ledger-lifecycle.md §6.1）。

覆盖 T1~T8 全部 8 类 + INV-1 偏差不变量。不碰生产台账（tmp_path 隔离）。
"""
import json
import time

import pytest

import daemon
from asip import ledger_policy


# ---- 共享夹具 ----

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WARP_IP_USED_FILE_PATH", str(tmp_path / "warp_ip_used.json"))
    monkeypatch.setenv("WARP_IP_HISTORY_FILE_PATH", str(tmp_path / "hist.jsonl"))
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_LEDGER_ALLOW_EMPTY_WRITE", False)
    monkeypatch.setattr(daemon, "_LEDGER_SHADOW",
                        {"checked": 0, "violations": 0, "exact_match": 0,
                         "deviation_sum": 0.0, "deviation_max": 0.0,
                         "deviation_min": 0.0})


def _ttl():
    return ledger_policy.make_ttl(daemon._warp())


def _retention():
    return float(daemon._warp().get("ledger_retention_sec", 86400))


# ---- T1: 访问不续命 ----

def test_t1_touch_does_not_extend_expires(monkeypatch):
    """探测记账 / 换线登记 / 被选中 事件后 expires_at 不变。"""
    now = 1_000_000.0
    monkeypatch.setattr(daemon.time, "time", lambda: now)
    # 创建条目（simulates 换线登记）
    daemon._mark_warp_ip_used("ip-a", now)
    entry = daemon._WARP_IP_USED["ip-a"]
    exp_before = entry["expires_at"]
    assert exp_before == pytest.approx(now + _retention())

    # 纯触碰（blocked=False）→ expires_at 不变
    daemon._mark_warp_ip_used("ip-a", now + 60)
    assert daemon._WARP_IP_USED["ip-a"]["expires_at"] == exp_before

    # 再次触碰
    daemon._mark_warp_ip_used("ip-a", now + 120)
    assert daemon._WARP_IP_USED["ip-a"]["expires_at"] == exp_before


# ---- T2: 成功续命 ----

def test_t2_success_extends_expires(monkeypatch):
    """成功流量打点后 expires_at = 该时刻 + 复用窗口，且不早于原值。"""
    now = 1_000_000.0
    monkeypatch.setattr(daemon.time, "time", lambda: now)
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "ip-ok")
    monkeypatch.setattr(daemon, "_last_success_probe", ("ip-ok", 0))
    monkeypatch.setattr(daemon, "recent_success_ts", lambda n: [now + 100])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)

    daemon._mark_warp_ip_used("ip-ok", now)
    exp_before = daemon._WARP_IP_USED["ip-ok"]["expires_at"]

    # 成功流量打点（ok 增量 > 0）
    daemon._record_active_ip_success(total=1, errors=0)

    exp_after = daemon._WARP_IP_USED["ip-ok"]["expires_at"]
    assert exp_after >= exp_before, "expires_at 单调不减（INV-3）"
    assert exp_after == pytest.approx(now + 100 + _retention()), \
        "expires_at = 成功时刻 + 复用窗口"


# ---- T3: 同轮重复标脏不续命 ----

def test_t3_same_round_reburn_does_not_extend(monkeypatch):
    """同一轮内第二次标脏，expires_at 不变。"""
    now = 1_000_000.0
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "rate429_guard_sec": 60})
    daemon._mark_warp_ip_used("ip-x", now, blocked=True, kind="rate429")
    entry = daemon._WARP_IP_USED["ip-x"]
    exp_before = entry["expires_at"]
    start_before = entry["blocked_ts_by_kind"].get("rate429")

    # 同轮重复标脏（60s 内再次 rate429）
    daemon._mark_warp_ip_used("ip-x", now + 30, blocked=True, kind="rate429")
    entry2 = daemon._WARP_IP_USED["ip-x"]
    assert entry2["expires_at"] == exp_before, "同轮重复标脏不续命"
    assert entry2["blocked_ts_by_kind"]["rate429"] == start_before, \
        "同轮起点不变"


# ---- T4: 新轮续命 ----

def test_t4_new_round_extends_expires(monkeypatch):
    """上一轮已到期后重新标脏 → expires_at = 新轮最晚解禁 + 复用窗口。"""
    now = 1_000_000.0
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "rate429_guard_sec": 60,
                         "blocked_guard_sec": 21600, "blocked_ttl_sec": 86400})
    # 第一轮 429
    daemon._mark_warp_ip_used("ip-r", now, blocked=True, kind="rate429")
    first_exp = daemon._WARP_IP_USED["ip-r"]["expires_at"]

    # 上一轮已过期（now + 60s 后，超出 rate429_guard_sec），重新标脏
    later = now + 120
    daemon._mark_warp_ip_used("ip-r", later, blocked=True, kind="rate429")
    entry = daemon._WARP_IP_USED["ip-r"]
    release = ledger_policy.block_release_ts(entry, _ttl())
    expected_exp = release + _retention()
    assert entry["expires_at"] == pytest.approx(expected_exp)
    assert entry["expires_at"] >= first_exp, "单调不减（INV-3）"


# ---- T5: 精确封禁保护（INV-1） ----

def test_t5_ban_until_blocks_deletion(monkeypatch):
    """ban_until 未到期时，条目绝不因清理被删。"""
    now = 1_000_000.0
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "rate429_guard_sec": 60,
                         "ip_reuse_guard_sec": 300})
    # 创建条目并设置精确封禁（ban_until = now + 10000）
    daemon._mark_warp_ip_used("ip-ban", now, blocked=True, kind="rate429",
                              ban_after=10000.0)
    entry = daemon._WARP_IP_USED["ip-ban"]
    assert entry["ban_until"] == pytest.approx(now + 10000)
    exp = entry["expires_at"]
    # expires_at ≥ ban_until + retention（INV-1）
    assert exp >= entry["ban_until"] + _retention() - 1, \
        "expires_at 必须覆盖 ban_until + 保留窗口"

    # 尝试触发清理（写另一个条目）：ban_until 未到期的条目不得被删
    daemon._mark_warp_ip_used("ip-other", now + 500)
    assert "ip-ban" in daemon._WARP_IP_USED, \
        "ban_until 未到期时条目绝不因清理被删"


# ---- T6: 非法 ban_kind 归一化 ----

def test_t6_invalid_ban_kind_normalized_on_load(tmp_path, monkeypatch):
    """类型非 rate429 或数值非法的 ban_until 被作废并按默认窗口重算。"""
    p = tmp_path / "warp_ip_used.json"
    now = 1_800_000_000.0
    data = {"used": {
        "ip-good": {"ts": now, "blocked": True,
                    "blocked_ts_by_kind": {"rate429": now - 60},
                    "ban_until": now + 500, "ban_kind": "rate429"},
        "ip-wrong-kind": {"ts": now, "blocked": True,
                          "blocked_ts_by_kind": {"rate429": now - 60},
                          "ban_until": now + 99999, "ban_kind": "abuse403"},
        "ip-bad-val": {"ts": now, "blocked": True,
                       "blocked_ts_by_kind": {"rate429": now - 60},
                       "ban_until": "not-a-number", "ban_kind": "rate429"},
    }}
    p.write_text(json.dumps(data))
    monkeypatch.setenv("WARP_IP_USED_FILE_PATH", str(p))
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})

    daemon._load_warp_ip_used()

    good = daemon._WARP_IP_USED["ip-good"]
    assert good["ban_until"] == pytest.approx(now + 500), "合法 ban_until 保留"

    wrong = daemon._WARP_IP_USED["ip-wrong-kind"]
    assert wrong["ban_until"] is None, "ban_kind 非 rate429 → 作废"
    # 按默认窗口重算解禁：rate429 起点 now-60 + rate429_guard_sec(60) = now
    release = ledger_policy.block_release_ts(wrong, _ttl())
    assert release == pytest.approx(now - 60 + 60), "按默认窗口重算"

    bad = daemon._WARP_IP_USED["ip-bad-val"]
    assert bad["ban_until"] is None, "非法数值 → 作废"


# ---- T7: 迁移保守口径 ----

def test_t7_migration_conservative_backfill(tmp_path, monkeypatch):
    """老条目载入后 expires_at = max(ts, 解禁时刻) + 窗口。"""
    p = tmp_path / "warp_ip_used.json"
    now = 1_800_000_000.0
    data = {"used": {
        "ip-old-clean": {"ts": now - 1000, "blocked": False,
                         "first_seen": now - 100000},
        "ip-old-blocked": {"ts": now - 500, "blocked": True,
                           "blocked_ts_by_kind": {"abuse403": now - 3600},
                           "first_seen": now - 100000},
        "ip-new": {"ts": now, "blocked": False,
                   "expires_at": now + 86400},
    }}
    p.write_text(json.dumps(data))
    monkeypatch.setenv("WARP_IP_USED_FILE_PATH", str(p))
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})

    daemon._load_warp_ip_used()
    ret = _retention()

    # 干净老条目：expires_at = ts + retention
    clean = daemon._WARP_IP_USED["ip-old-clean"]
    assert clean["expires_at"] == pytest.approx(now - 1000 + ret)

    # 被封老条目：expires_at = max(ts, 解禁) + retention
    blocked = daemon._WARP_IP_USED["ip-old-blocked"]
    release = ledger_policy.block_release_ts(blocked, _ttl())
    assert blocked["expires_at"] == pytest.approx(max(now - 500, release) + ret)

    # 已有 expires_at 的条目不变
    new = daemon._WARP_IP_USED["ip-new"]
    assert new["expires_at"] == pytest.approx(now + 86400)


# ---- T8: 删除后回到档 0 ----

def test_t8_deleted_entry_resets_to_rank0(monkeypatch):
    """条目被删后该地址重新入账即为「从未出现过」档 0。"""
    now = 1_000_000.0
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "ip_reuse_guard_sec": 300})
    monkeypatch.setattr(daemon.time, "time", lambda: now)
    daemon._mark_warp_ip_used("ip-rank", now)
    assert daemon._warp_ip_rank("ip-rank", now + 10, 300)[0] > 0, "在台账中非档 0"

    # 到期后被清理（远超 300s 窗口）
    later = now + 1000
    daemon._mark_warp_ip_used("ip-other", later)
    assert "ip-rank" not in daemon._WARP_IP_USED, "出窗口被清理"

    # 删除后、尚未重新入账时 → 档 0（从未出现过）
    assert daemon._warp_ip_rank("ip-rank", later + 10, 300)[0] == 0, \
        "删除后未入账即「从未出现过」档 0"


# ---- INV-1: 偏差恒 ≥0 ----

def test_inv1_shadow_deviation_always_non_negative(monkeypatch):
    """expires_at 锚点 ≥ 判定侧释放时刻，偏差恒 ≥0。"""
    now = 1_000_000.0
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "rate429_guard_sec": 60,
                         "blocked_guard_sec": 21600, "blocked_ttl_sec": 86400,
                         "ip_reuse_guard_sec": 86400})
    ttl = _ttl()
    ret = _retention()

    # 各种场景下写入后验证 INV-1
    cases = [
        # (ip, now, blocked, kind, ban_after)
        ("ip-clean", now, False, None, None),
        ("ip-403", now + 10, True, "abuse403", None),
        ("ip-429", now + 20, True, "rate429", None),
        ("ip-429-ban", now + 30, True, "rate429", 3600.0),
        ("ip-legacy", now + 40, True, None, None),
    ]
    for ip, t, blocked, kind, ban_after in cases:
        daemon._mark_warp_ip_used(ip, t, blocked=blocked, kind=kind,
                                  ban_after=ban_after)
        entry = daemon._WARP_IP_USED[ip]
        dev = ledger_policy.shadow_deviation(entry, ret, ttl)
        assert dev is not None, f"{ip} 必须有 expires_at"
        assert dev >= 0, f"{ip} 偏差 {dev} < 0 → INV-1 被破坏"
        # 也验证 is_effectively_blocked 一致性
        release = ledger_policy.block_release_ts(entry, ttl)
        is_blocked = ledger_policy.is_effectively_blocked(entry, t, ttl)
        if release > 0:
            assert is_blocked == (release > t)

    # 影子统计应无违例
    shadow = daemon._LEDGER_SHADOW
    assert shadow["violations"] == 0
    assert shadow["checked"] >= len(cases)


def test_inv1_monotonic_no_decrease(monkeypatch):
    """expires_at 单调不减（INV-3）：任何后续事件都不能把它拉早。"""
    now = 1_000_000.0
    monkeypatch.setitem(daemon.CFG, "warp",
                        {**daemon.CFG["warp"], "rate429_guard_sec": 60,
                         "ip_reuse_guard_sec": 86400})
    daemon._mark_warp_ip_used("ip-mono", now, blocked=True, kind="rate429",
                              ban_after=7200.0)
    exp1 = daemon._WARP_IP_USED["ip-mono"]["expires_at"]

    # 纯触碰不续命 → 不变
    daemon._mark_warp_ip_used("ip-mono", now + 100)
    assert daemon._WARP_IP_USED["ip-mono"]["expires_at"] == exp1

    # 新一轮标脏续命 → 只增不减
    daemon._mark_warp_ip_used("ip-mono", now + 9000, blocked=True, kind="rate429")
    exp2 = daemon._WARP_IP_USED["ip-mono"]["expires_at"]
    assert exp2 >= exp1, "expires_at 单调不减"
