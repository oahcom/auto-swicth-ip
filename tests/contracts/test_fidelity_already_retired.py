"""①组保真契约 · already_retired 只否不肯定 + client_since 校验（附件 A5）。

【防什么事故】改双向判定（「ip==当前出口」也用来肯定已换走）→ 每次都回
already_retired、从不换线，客户端拿死隧道彻底卡死（daemon._rotate_ip_is_retired
文档记载的事故）；判不出时误判「已换走」→ 真实 429 得不到处理。
【事故出处】附件 A5；D16/357 白跳家族；2026-11-14 wfgkef17a。
【三分法】A 保真契约（判据语义钉住；「已换走判据收紧」属左栏待改，不在本组）。
"""
import time

import pytest

import daemon as d


def test_retired_only_when_switched_after_request():
    """「已换走」只在换线时刻晚于请求发出时刻时成立（事实判据）。"""
    now = time.time()
    d._exit_ledger_note("2001:db8::r1", "2001:db8::new", now - 5.0, "T-B")
    assert d._rotate_ip_is_retired("2001:db8::r1", now - 10.0) is True
    # 切换发生在请求之前：请求打的是当时有效的出口，不算已换走
    assert d._rotate_ip_is_retired("2001:db8::r1", now - 1.0) is False


def test_unknown_judgment_is_false():
    """判不出一律 False：多换一次只废一格，误判「已换走」才致命（附件 A5）。"""
    assert d._rotate_ip_is_retired(None, time.time()) is False
    assert d._rotate_ip_is_retired("2001:db8::r2", None) is False
    assert d._rotate_ip_is_retired("2001:db8::r2", 0.0) is False
    assert d._rotate_ip_is_retired("2001:db8::never-seen", time.time()) is False


def test_client_since_cache_veto():
    """缓存接入时刻晚于请求发出 → 缓存是「未来」的，当判不出处理（附件 A5）。

    防什么事故：调用方拿错误缓存跳过换线，真实 429 不被处理。
    """
    now = time.time()
    d._exit_ledger_note("2001:db8::r3", "2001:db8::new", now - 5.0, "T-B")
    # client_since（active_egress_since）晚于请求发出：不可信
    assert d._rotate_ip_is_retired("2001:db8::r3", now - 10.0,
                                   client_since=now - 2.0) is False
    # 缓存早于请求：正常查表
    assert d._rotate_ip_is_retired("2001:db8::r3", now - 10.0,
                                   client_since=now - 20.0) is True


def test_slot_retired_uses_slot_ledger():
    """槽位口径同语义：只认「该请求发出之后」的换线记录。"""
    now = time.time()
    with d._SLOT_LEDGER_LOCK:
        d._SLOT_EXIT_LEDGER["slot-1"] = [
            {"at": now - 5.0, "old": "2001:db8::s1", "new": "2001:db8::s2",
             "tag": "T-B"}]
    assert d._slot_ip_is_retired("slot-1", "2001:db8::s1", now - 10.0) is True
    assert d._slot_ip_is_retired("slot-1", "2001:db8::s1", now - 1.0) is False
    assert d._slot_ip_is_retired("slot-1", None, now - 10.0) is False
