"""①组保真契约 · ban_until / Retry-After 续命契约（附件 A1、附件 B2/B7）。

【防什么事故】把已落地的 Retry-After 链当成缺失重造、或改坏替换律/兜底/顺序
——复演「全池榨干」：刚封的 /128 60s 就回池被反复复用（附件 A1 误解之祸：
「重造成功能/改叠加律/兜底改 60s→复演全池榨干」）。
【事故出处】附件 A1；探针实测上游 FreeUsageLimitError 的 Retry-After 为
11~13h，config 注释明写 12h 兜底「避免 60s 让刚封地址立刻回池」。
【三分法】A 保真契约：改坏即事故，只许钉住不许改动。
【取值】全部从 config 实配（daemon._warp() / CFG["api"] / probe_warp 常量）
读取，禁按注释/日志文案写死（漂移实证 30s vs 1800、60% vs 0.3）。
"""
import time

import pytest

import daemon as d
import probe_warp
from asip import ledger_policy


def _ttl():
    return ledger_policy.make_ttl(d._warp())


def test_precise_ban_replaces_default_window():
    """精确封禁**替换**同类默认窗（替换律，非叠加）。

    防什么事故：把替换律改成叠加/丢弃 → 429 默认 60s 提前放行仍在封禁期的
    地址，或反向把默认窗当精确值使用（附件 A1）。
    """
    w = d._warp()
    assert d._rotate_ban_after_from_req({"retry_after_ms": 3_600_000}) == 3600.0
    now = time.time()
    d._mark_warp_ip_used("2001:db8::a1", now, blocked=True, kind="rate429",
                         ban_after=3600.0)
    entry = d._WARP_IP_USED["2001:db8::a1"]
    assert entry["ban_until"] == pytest.approx(now + 3600.0, abs=1e-6)
    assert entry["ban_kind"] == "rate429"
    # 默认窗口（rate429_guard_sec，通常 60s）远早于精确封禁：替换律要求
    # 判定按 ban_until 走，不能按默认窗提前解禁。
    assert d._warp_ip_is_blocked("2001:db8::a1", now + float(w["rate429_guard_sec"]) + 5)
    assert not d._warp_ip_is_blocked("2001:db8::a1", now + 3700.0)


def test_ban_after_clamped_one_second_to_24h():
    """钳制 [1s, 24h]：防上游给荒谬值把地址永久放逐或立刻回池（附件 A1）。
    """
    now = time.time()
    d._mark_warp_ip_used("2001:db8::a2", now, blocked=True, kind="rate429",
                         ban_after=0.4)
    assert d._WARP_IP_USED["2001:db8::a2"]["ban_until"] == pytest.approx(
        now + 1.0, abs=1e-6)
    d._mark_warp_ip_used("2001:db8::a3", now, blocked=True, kind="rate429",
                         ban_after=999_999.0)
    assert d._WARP_IP_USED["2001:db8::a3"]["ban_until"] == pytest.approx(
        now + 86400.0, abs=1e-6)
    # 请求层入口（9router 透传 retry_after_ms）同一钳制。
    assert d._rotate_ban_after_from_req({"retry_after_ms": 1e12}) == 86400.0
    assert probe_warp.parse_retry_after({"retry-after": "0.2"}) == 1
    assert probe_warp.parse_retry_after({"retry-after": "999999999"}) == \
        probe_warp.MAX_RETRY_SEC


def test_only_rate429_kind_gets_ban_until():
    """只认 rate429：403 套 Retry-After 是已修 bug，不许复活（附件 A1）。

    防什么事故：403 是 6h 信誉惩罚，被响应里偶然出现的 rate-limit 头缩短
    会提前放行脏地址；坏 ban_kind 值一律作废按默认窗。
    """
    now = time.time()
    d._mark_warp_ip_used("2001:db8::a4", now, blocked=True, kind="abuse403",
                         ban_after=3600.0)
    entry = d._WARP_IP_USED["2001:db8::a4"]
    assert entry["ban_until"] is None
    assert d._warp_ip_is_blocked(
        "2001:db8::a4",
        now + float(d._warp()["rate429_guard_sec"]) + 5)  # 403 走 6h 窗
    # 解析层同语义：ban_kind 非 rate429 → 视为没有精确封禁。
    bu, bk = d._warp_ban_until({"ban_until": now + 100, "ban_kind": "abuse403"})
    assert bu is None and bk == ""


def test_same_round_ban_only_extends():
    """轮内只延长不缩短：迟到的更短 Retry-After 不得提前解禁（附件 A1）。
    """
    now = time.time()
    d._mark_warp_ip_used("2001:db8::a5", now, blocked=True, kind="rate429",
                         ban_after=3600.0)
    d._mark_warp_ip_used("2001:db8::a5", now + 5, blocked=True, kind="rate429",
                         ban_after=60.0)
    entry = d._WARP_IP_USED["2001:db8::a5"]
    assert entry["ban_until"] == pytest.approx(now + 3600.0, abs=1e-6)
    d._mark_warp_ip_used("2001:db8::a5", now + 10, blocked=True, kind="rate429",
                         ban_after=7200.0)
    assert d._WARP_IP_USED["2001:db8::a5"]["ban_until"] == pytest.approx(
        now + 10 + 7200.0, abs=1e-6)


def test_expired_ban_cleared_before_new_round():
    """过期 ban_until 先清后开新轮（附件 B2/#7，顺序即语义）。

    防什么事故：过期 ban_until 留在原地会「盖掉」新 429 的默认窗口，
    Retry-After 到期后再烧的地址 _warp_ip_is_blocked 立刻返回 False ——
    被限地址当干净地址回池（daemon._warp_ip_used_locked 注释实测记载）。
    清理必须发生在 setdefault 新轮起点**之前**。
    """
    t0 = time.time()
    d._mark_warp_ip_used("2001:db8::a6", t0, blocked=True, kind="rate429",
                         ban_after=10.0)
    t1 = t0 + 20.0  # 精确封禁已过期，同一地址再烧（无新 Retry-After）
    d._mark_warp_ip_used("2001:db8::a6", t1, blocked=True, kind="rate429")
    entry = d._WARP_IP_USED["2001:db8::a6"]
    assert entry["ban_until"] is None  # 过期精确封禁已清，不再压住新轮
    assert d._warp_ip_is_blocked("2001:db8::a6", t1 + 5)  # 新轮默认窗生效


def test_fallback_12h_from_config_not_60s():
    """生产缺 Retry-After 时 12h 兜底来自 config 实配（附件 A1）。

    防什么事故：兜底退回 rate429_guard_sec=60 → 刚封地址 60s 回池再烧，
    复演全池榨干。取值纪律：断言值从 config 读，同时钉「兜底远大于默认冷却」
    这条语义关系（防有人把 production_429_ban_sec 改小到默认冷却量级）。
    """
    cfg_val = float(d._api_cfg().get("production_429_ban_sec"))
    assert d._rotate_ban_after_from_req({}) == pytest.approx(
        min(max(cfg_val, 1.0), 86400.0))
    assert cfg_val >= 3600.0  # 兜底以小时计，不是秒级默认窗
    assert cfg_val > float(d._warp()["rate429_guard_sec"])


def test_ttl_three_tiers_legacy_not_60s():
    """TTL 三档并行时钟：legacy 不吃 60s 默认（附件 C 迁移语义律）。

    防什么事故：kind 缺失的旧账若套上 rate429 的 60s 默认，无法判别的旧记录
    会被提前放回候选池。三档取值以 config 实配为准。
    """
    w = d._warp()
    assert d._warp_block_ttl("abuse403") == float(w["blocked_guard_sec"])
    assert d._warp_block_ttl("rate429") == float(w["rate429_guard_sec"])
    legacy = d._warp_block_ttl(None)
    assert legacy == float(w.get("blocked_ttl_sec"))
    assert legacy != float(w["rate429_guard_sec"])  # legacy ≠ 60s 默认


def test_multi_kind_takes_latest_release():
    """多类并存取最晚解禁：先 403 后 429 两条时钟并行，不被后者覆盖（附件 C）。

    防什么事故：单 (kind, start) 存储会让后一次标记覆盖前一次 —— 403 的 6h
    信誉惩罚被 429 的 60s 窗提前抹掉（_warp_block_stamps 注释）。
    """
    now = time.time()
    ip = "2001:db8::a7"
    d._mark_warp_ip_used(ip, now - 100.0, blocked=True, kind="abuse403")
    d._mark_warp_ip_used(ip, now - 10.0, blocked=True, kind="rate429")
    entry = d._WARP_IP_USED[ip]
    stamps = d._warp_block_stamps(entry)
    assert set(stamps) == {"abuse403", "rate429"}
    rel = ledger_policy.block_release_ts(entry, _ttl())
    assert rel == pytest.approx(max(
        stamps["abuse403"] + float(d._warp()["blocked_guard_sec"]),
        stamps["rate429"] + float(d._warp()["rate429_guard_sec"])), abs=1e-6)
    assert d._warp_ip_is_blocked(ip, now)


def test_load_normalization_voids_bad_ban_until():
    """载入归一化：ban_kind 非 rate429 / 数值非法的 ban_until 置空（附件 A1）。

    防什么事故：手工编辑/半截写入的坏 ban_until 若被采信，选线热路径要么
    抛异常要么把地址按错误时刻放行；坏值只丢精确封禁、退回默认窗保守处理。
    """
    e1 = {"ts": time.time(), "ban_until": 123.0, "ban_kind": "abuse403"}
    assert d._normalize_ledger_entry(e1, 86400.0, _ttl())
    assert e1["ban_until"] is None and e1["ban_kind"] is None
    e2 = {"ts": time.time(), "ban_until": "not-a-ts", "ban_kind": "rate429"}
    assert d._normalize_ledger_entry(e2, 86400.0, _ttl())
    assert e2["ban_until"] is None


def test_production_fallback_chained_only_for_rate429():
    """12h 兜底只灌给 rate429；transport/proactive 传了也不产生写入（附件 A1/C）。
    """
    now = time.time()
    d._mark_warp_ip_used("2001:db8::a8", now, blocked=False, kind="transport")
    assert "2001:db8::a8" in d._WARP_IP_USED
    assert not d._WARP_IP_USED["2001:db8::a8"].get("blocked")
    # transport/proactive 不标脏是调用侧语义（_rotate_once 记账分支），此处
    # 钉写入口同语义：kind=transport 且 blocked=False 不建立惩罚期。
    assert d._warp_block_stamps(d._WARP_IP_USED["2001:db8::a8"]) == {}
