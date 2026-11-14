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
                        dict(daemon._LEDGER_SHADOW_DEFAULTS))
    # 影子落盘路径也隔离到 tmp：pytest 进程里的任何 tick 都不得写仓库根文件。
    monkeypatch.setattr(daemon, "_LEDGER_SHADOW_FILE",
                        str(tmp_path / "ledger_shadow.json"))


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


# ---- W1: 探针建账必须产 expires_at（写入口收敛） ----

def test_w1_probe_ok_creates_expires_at(monkeypatch):
    """探针 OK 建账走收敛入口：新条目必带 expires_at，且不冒充 last_ok_ts。"""
    now = 1_000_000.0
    monkeypatch.setattr(daemon.time, "time", lambda: now)

    daemon._ledger_mark_probe_ok("ip-probe-new", now)

    entry = daemon._WARP_IP_USED["ip-probe-new"]
    assert entry["expires_at"] == pytest.approx(now + _retention()), \
        "探针建账必须产生 expires_at（原直写路径缺字段 → P2 会秒删）"
    assert entry["probe_ok_ts"] == pytest.approx(now)
    assert entry["last_ok_ts"] is None, "探针 OK 不得冒充生产成功"
    assert entry["blocked"] is False
    assert "blocked_ts_by_kind" in entry, "必须带完整字段集（下游档位/判定依赖）"


def test_w1_probe_ok_backfills_legacy_entry(monkeypatch):
    """已存在的旧式条目（只有 ts）被探针记账后补齐 expires_at。"""
    now = 2_000_000.0
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {"ip-old": {"ts": 1_000_000.0}})

    daemon._ledger_mark_probe_ok("ip-old", now)

    entry = daemon._WARP_IP_USED["ip-old"]
    assert entry["expires_at"] == pytest.approx(1_000_000.0 + _retention()), \
        "EVENT_PROBE 不续命，但缺字段时按最近事件补初值"
    assert entry["probe_ok_ts"] == pytest.approx(now)


# ---- W2: 载入补齐（含旧 float 格式 / ts 缺失但有成功记录） ----

def test_w2_load_float_entry_backfilled(tmp_path, monkeypatch):
    """旧 float 格式（值为最后使用时刻）载入后同样补齐 expires_at。"""
    p = tmp_path / "warp_ip_used.json"
    ts = 1_800_000_000.0
    p.write_text(json.dumps({"used": {"ip-float": ts}}))
    monkeypatch.setenv("WARP_IP_USED_FILE_PATH", str(p))
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})

    daemon._load_warp_ip_used()

    entry = daemon._WARP_IP_USED["ip-float"]
    assert isinstance(entry, dict)
    assert entry["expires_at"] == pytest.approx(ts + _retention()), \
        "旧 float 条目必须补齐，否则 P2 把它当未迁移"


def test_w2_load_backfill_uses_last_ok_when_ts_missing(tmp_path, monkeypatch):
    """ts 坏值但有 last_ok_ts：补齐锚点用 last_ok_ts（INV-2 不跌破）。"""
    p = tmp_path / "warp_ip_used.json"
    now = 1_800_000_000.0
    p.write_text(json.dumps({"used": {
        "ip-okonly": {"ts": "bad", "blocked": False,
                      "last_ok_ts": now - 5000},
    }}))
    monkeypatch.setenv("WARP_IP_USED_FILE_PATH", str(p))
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})

    daemon._load_warp_ip_used()

    entry = daemon._WARP_IP_USED["ip-okonly"]
    assert entry["expires_at"] == pytest.approx(now - 5000 + _retention()), \
        "INV-2：expires_at ≥ 最近成功时刻 + 复用窗口"


def test_w2_load_unfixable_entry_kept_without_expires(tmp_path, monkeypatch):
    """时间戳全坏且无解禁时刻：不造值、不删条目（留给 P2 跳过并告警）。"""
    p = tmp_path / "warp_ip_used.json"
    p.write_text(json.dumps({"used": {"ip-bad": {"ts": None, "blocked": True}}}))
    monkeypatch.setenv("WARP_IP_USED_FILE_PATH", str(p))
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})

    daemon._load_warp_ip_used()

    entry = daemon._WARP_IP_USED["ip-bad"]
    assert entry.get("expires_at") is None, "不许凭空造生命周期值"
    assert entry["blocked"] is True, "标记保留，绝不因缺字段丢掉封禁信息"
    assert daemon._LEDGER_SHADOW["unmigrated"] == 1, \
        "启动即在影子计数暴露（不等第一次干跑）"


# ---- W3: P2 清理谓词不误删（INV-2 排演） ----

def test_w3_p2_cleanup_backfills_instead_of_deleting(monkeypatch):
    """expires_at 模式下缺字段 = 未迁移 → 先补齐再判，不得秒删。"""
    now = 1_000_000.0
    monkeypatch.setitem(daemon.CFG["warp"], "ledger_cleanup_mode", "expires_at")
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-missing": {"ts": now - 10, "blocked": False,
                       "probe_ok_ts": now - 10},
    })

    # 任意一次写入触发全表清理
    daemon._mark_warp_ip_used("ip-trigger", now)

    assert "ip-missing" in daemon._WARP_IP_USED, \
        "未迁移条目必须补齐后保留（原实现无字段即删）"
    assert daemon._WARP_IP_USED["ip-missing"]["expires_at"] == pytest.approx(
        now - 10 + _retention())
    assert daemon._LEDGER_SHADOW["missing_expires"] == 1
    assert daemon._LEDGER_SHADOW["backfilled_total"] >= 1


def test_w3_p2_cleanup_skips_entry_without_anchor(monkeypatch):
    """补齐也定不出值的条目：保留 + 计数，不静默删。"""
    now = 1_000_000.0
    monkeypatch.setitem(daemon.CFG["warp"], "ledger_cleanup_mode", "expires_at")
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {"ip-noanchor": {"blocked": True}})

    daemon._mark_warp_ip_used("ip-trigger", now)

    assert "ip-noanchor" in daemon._WARP_IP_USED, "无锚点条目绝不删除"
    assert daemon._LEDGER_SHADOW["unmigrated"] == 1


def test_w3_p2_cleanup_deletes_only_expired(monkeypatch):
    """有 expires_at 的条目仍按 INV-4 唯一判据删除。"""
    now = 1_000_000.0
    monkeypatch.setitem(daemon.CFG["warp"], "ledger_cleanup_mode", "expires_at")
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-expired": {"ts": now - 99999, "blocked": False,
                       "expires_at": now - 1},
        "ip-alive": {"ts": now - 10, "blocked": False,
                     "expires_at": now + 100},
    })

    daemon._mark_warp_ip_used("ip-trigger", now)

    assert "ip-expired" not in daemon._WARP_IP_USED
    assert "ip-alive" in daemon._WARP_IP_USED


def test_w3_legacy_mode_unchanged(monkeypatch):
    """legacy 模式行为不变：缺 expires_at 仍按 ts 窗口清理。"""
    now = 1_000_000.0
    monkeypatch.setitem(daemon.CFG["warp"], "ledger_cleanup_mode", "legacy")
    monkeypatch.setitem(daemon.CFG["warp"], "ip_reuse_guard_sec", 300)
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "ip-stale": {"ts": now - 1000, "blocked": False},
    })

    daemon._mark_warp_ip_used("ip-trigger", now)

    assert "ip-stale" not in daemon._WARP_IP_USED


# ---- W4: would-delete 试算（P2 误删风险的运行证据） ----

def test_w4_would_delete_dry_run(monkeypatch):
    """干跑统计与 P2 实删同谓词：计数正确、样例脱敏、不改表。"""
    now = 1_000_000.0
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        "203.0.113.9": {"ts": now - 99999, "blocked": False,
                        "expires_at": now - 1},
        "203.0.113.10": {"ts": now - 10, "blocked": False,
                         "expires_at": now + 1000},
        "203.0.113.11": {"ts": now - 99999, "blocked": False},
        "203.0.113.12": {"blocked": True},
    })

    out = daemon._ledger_shadow_sweep(now)

    assert out["would_delete"] == 2, "过期 1 条 + 补齐后过期 1 条"
    assert out["missing_expires"] == 1
    assert out["unmigrated"] == 1
    samples = daemon._LEDGER_SHADOW["would_delete_samples"]
    assert len(samples) == 2
    assert all(s["ip"] == "203.0.x.x" for s in samples), "样例必须脱敏"
    assert "expires_at" not in daemon._WARP_IP_USED["203.0.113.11"], \
        "干跑只读，不得把补齐结果写回表"


def test_w4_would_delete_clean_table(monkeypatch):
    """全表都未过期 → would_delete == 0（切 P2 的直接门槛）。"""
    now = 1_000_000.0
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {
        f"ip-{i}": {"ts": now, "blocked": False, "expires_at": now + 100}
        for i in range(20)
    })

    out = daemon._ledger_shadow_sweep(now)

    assert out == {"would_delete": 0, "missing_expires": 0,
                   "unmigrated": 0, "at": now}


# ---- W5: 影子落盘续计 + 起点戳 ----

def test_w5_shadow_persist_resume_and_started_at(tmp_path, monkeypatch):
    """重启续计：计数累加、起点戳保留（否则「连续≥24h」无法证明）。"""
    monkeypatch.setattr(daemon, "_LEDGER_SHADOW",
                        dict(daemon._LEDGER_SHADOW_DEFAULTS))
    s = daemon._LEDGER_SHADOW
    s["started_at"] = 1000.0
    s["checked"] = 10
    s["violations"] = 2
    s["would_delete"] = 3
    daemon._persist_ledger_shadow()

    # 模拟重启：内存清零后从盘上续计
    monkeypatch.setattr(daemon, "_LEDGER_SHADOW",
                        dict(daemon._LEDGER_SHADOW_DEFAULTS))
    daemon._load_ledger_shadow_boot()

    s2 = daemon._LEDGER_SHADOW
    assert s2["checked"] == 10
    assert s2["violations"] == 2
    assert s2["started_at"] == 1000.0, "起点戳不得被重启重置"
    assert s2["would_delete"] == 3


def test_w5_shadow_boot_creates_started_at(tmp_path, monkeypatch):
    """无落盘文件时，启动即建立观察起点戳。"""
    monkeypatch.setattr(daemon, "_LEDGER_SHADOW",
                        dict(daemon._LEDGER_SHADOW_DEFAULTS))
    daemon._load_ledger_shadow_boot()
    assert daemon._LEDGER_SHADOW["started_at"] > 0


# ---- W6: 偏差分人群 ----

def test_w6_shadow_buckets_do_not_pollute(monkeypatch):
    """干净条目不进惩罚期偏差统计；惩罚期条目单独计数。"""
    now = 1_000_000.0
    monkeypatch.setitem(daemon.CFG["warp"], "rate429_guard_sec", 60)
    shadow = daemon._LEDGER_SHADOW

    # 干净条目：只进 clean 档，不污染 deviation_*（原口径会把时间戳当偏差）
    daemon._mark_warp_ip_used("ip-clean", now)
    assert shadow["checked_clean"] == 1
    assert shadow["checked_blocked"] == 0
    assert shadow["deviation_sum"] == 0.0
    assert shadow["deviation_max"] == 0.0

    # 惩罚期条目：expires_at = 解禁 + 窗口 → 偏差恰为 0（精确命中）
    daemon._mark_warp_ip_used("ip-burn", now + 10, blocked=True, kind="rate429")
    assert shadow["checked_blocked"] == 1
    assert shadow["exact_match"] == 1
    assert shadow["deviation_max"] == 0.0

    # 惩罚期内又打成功流量：锚点 = 成功时刻 > 解禁时刻 → 偏差为正且非零
    entry = daemon._WARP_IP_USED["ip-burn"]
    t_ok = now + 300
    entry["last_ok_ts"] = t_ok
    daemon._ledger_apply_event_locked(entry, ledger_policy.EVENT_SUCCESS, t_ok)
    assert shadow["checked_blocked"] == 2
    assert shadow["deviation_max"] == pytest.approx(t_ok - (now + 10 + 60))
    assert shadow["violations"] == 0
