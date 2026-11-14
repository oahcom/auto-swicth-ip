"""①组保真契约 · B4 门序：策略拒绝优先于排空闸、排空闸在 CV 锁外（附件 B4）。

【防什么事故】策略闸（渠道锁/重启额度/档位）排在排空闸之后时，「渠道锁定中」的
线会先被排空闸求值：排空闸判空即放行，放行路径**清掉 `_RETIRED_AT` /
`_DRAIN_PENDING`**；紧接着渠道锁拒绝、不重启 —— 台账被清掉了、线却没回收。
下一轮这条线再进排空闸时 `_RETIRED_AT` 已无记录，只看字节流事实；名下仍有
连接就落进「台账洞」分支重起表并刷 `⚠ 无退役记录却有 N 条连接` 假告警
（2026-11-14 18:03 WARP-H2-LAX-175 实测，与附件 D「日志假事件污染账本」同族）。
排空闸若放进 `_WARP_LINE_CV` 锁内，闸内的 `_lines_in_use` / `_warp_line_stats`
在启动窗口含 HTTP，会持锁跨网络调用，阻塞所有线状态读写（探针/换线全停）。

【事故出处】附件 A9 / B4；2026-11-14 18:03 假「台账洞」告警实证。
【三分法】A 保真契约（门序即语义：拒绝路径零排空副作用 + 排空闸不在锁内）。
【边界】本组不重复 warming 置位时机（见 test_fidelity_warming_order.py 四例）；
仅钉「策略拒绝 → 排空闸不被调用、台账不变」与「排空闸运行时未持 CV 锁」。
"""
import threading
import time

import pytest

import daemon as d


MEMBER = {"tag": "WARP-WG-TEST", "port": 17999, "kind": "wg"}


@pytest.fixture
def gate_env(monkeypatch):
    """_warp_line_restart 离线跑桩：真排空闸 + 观测它是否被调用/是否持锁。

    排空闸用真函数（spy 只是记录后转发），这样「副作用」是真实台账副作用，
    不是桩造出来的结论；`_lines_in_use` 桩成空集、`_warp_line_stats` 桩成
    零连接 → 真闸若被调用一定走「零连接放行」并清两张台账表。
    """
    obs = {"drain_calls": 0, "restart_calls": 0, "cv_held_during_drain": None}
    real_drain = d._drain_gate
    monkeypatch.setitem(d.CFG["warp"], "channel_ban_hold_enabled", True)
    monkeypatch.setitem(d.CFG["warp"], "drain_before_recycle_enabled", True)
    monkeypatch.setattr(d, "active_warp_tag", lambda: "OTHER-TAG")
    monkeypatch.setattr(d, "_night_block_restart", lambda *a, **k: False)
    monkeypatch.setattr(d, "_line_enabled", lambda m: True)
    monkeypatch.setattr(d, "_warp_line_restart_allowed", lambda st, now: True)
    monkeypatch.setattr(d, "_lines_in_use", lambda: set())
    monkeypatch.setattr(d, "_warp_line_stats", lambda tag: (0, 0, 0))

    def _spy(member, now, reason=""):
        obs["drain_calls"] += 1
        held = []

        def _probe():
            # RLock 非持有线程的 blocking=False 立刻失败 → 确定性判定
            got = d._WARP_LINE_LOCK.acquire(blocking=False)
            held.append(not got)
            if got:
                d._WARP_LINE_LOCK.release()

        t = threading.Thread(target=_probe)
        t.start()
        t.join()
        obs["cv_held_during_drain"] = held[0]
        return real_drain(member, now, reason)

    monkeypatch.setattr(d, "_drain_gate", _spy)
    monkeypatch.setattr(d, "warp_line_busy", lambda *a, **k: False)
    monkeypatch.setattr(d, "_acquire_refresh_slot", lambda tag: True)
    monkeypatch.setattr(d, "_release_refresh_slot", lambda tag: None)
    monkeypatch.setattr(d, "_invalidate_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(d, "_RESTART_SEM", None)

    def _restart(member):
        obs["restart_calls"] += 1
        return False

    monkeypatch.setattr(d, "_restart_warp_line", _restart)
    monkeypatch.setattr(d, "_restart_outcome_note", lambda *a, **k: None)
    monkeypatch.setattr(d, "_wait_port_ready", lambda *a, **k: False)
    monkeypatch.setattr(d, "_probe_refresh_egress", lambda *a, **k: None)

    d._WARP_LINE_STATE.clear()
    d._RETIRED_AT.clear()
    d._DRAIN_PENDING.clear()
    d._NIGHT_LOG_TS.clear()
    yield obs
    d._RETIRED_AT.clear()
    d._DRAIN_PENDING.clear()
    d._NIGHT_LOG_TS.clear()


@pytest.mark.parametrize("gate", ["channel_lock", "restart_quota", "tier_disabled"])
def test_strategy_reject_precedes_drain_and_keeps_drain_ledger(
        gate_env, monkeypatch, gate):
    """渠道锁/额度/档位拒绝先于排空：排空闸不跑，退役台账原样保留（附件 B4）。

    防什么事故：拒绝的线先被排空闸判空放行、清掉 `_RETIRED_AT`/`_DRAIN_PENDING`，
    再被策略拒绝；下一轮同一线在「台账已无记录」下重走排空，刷假「台账洞」告警
    并重起排空时钟（2026-11-14 18:03 WARP-H2-LAX-175 实证）。
    """
    tag = MEMBER["tag"]
    now = time.time()
    d._RETIRED_AT[tag] = now - 5.0
    d._DRAIN_PENDING[tag] = {"rounds": 1, "last_bytes": 100}
    if gate == "channel_lock":
        d._warp_line_update(tag, ban_hold_until=now + 600)
    elif gate == "restart_quota":
        monkeypatch.setattr(d, "_warp_line_restart_allowed",
                            lambda st, n: False)
    else:
        monkeypatch.setattr(d, "_line_enabled", lambda m: False)

    assert d._warp_line_restart(MEMBER, "probe") is False
    assert gate_env["drain_calls"] == 0, "策略拒绝的线不得进入排空闸"
    assert gate_env["restart_calls"] == 0
    assert d._RETIRED_AT.get(tag) == now - 5.0, "拒绝路径不得清退役时间戳"
    assert d._DRAIN_PENDING.get(tag) == {"rounds": 1, "last_bytes": 100}, \
        "拒绝路径不得清排空进度"
    assert d._warp_line_state(tag).get("warming") is False


def test_strategy_reject_does_not_open_false_drainhole(gate_env, monkeypatch):
    """策略拒绝不得触发「台账洞」分支：不建 `_RETIRED_AT`、不刷假告警。

    防什么事故：无退役记录但有连接时，排空闸按「刚退役」起表并打
    `⚠ 无退役记录却有 N 条连接`。被渠道锁拒绝的线本不该走到这一步 —— 旧门序
    下先排空后策略，这条假告警每轮刷一次，污染台账判读（2026-11-14 18:03）。
    """
    tag = MEMBER["tag"]
    d._warp_line_update(tag, ban_hold_until=time.time() + 600)
    monkeypatch.setattr(d, "_warp_line_stats", lambda t: (4096, 0, 3))

    assert d._warp_line_restart(MEMBER, "probe") is False
    assert gate_env["drain_calls"] == 0
    assert tag not in d._RETIRED_AT, "拒绝路径不得凭空建立退役记录"
    assert not [k for k in d._NIGHT_LOG_TS if k.startswith("drainhole:")], \
        "拒绝路径不得产生台账洞告警"


def test_drain_gate_runs_outside_cv_lock(gate_env):
    """排空闸在 `_WARP_LINE_CV` 之外求值（附件 B4 注：闸内含 HTTP）。

    防什么事故：闸内 `_lines_in_use`/`_warp_line_stats` 在启动窗口含 HTTP，
    持 CV 锁跨网络调用 → 所有线状态读写（探测/换线/回包）一起阻塞。
    """
    assert d._warp_line_restart(MEMBER, "probe") is False  # 桩重启失败即可
    assert gate_env["drain_calls"] == 1
    assert gate_env["cv_held_during_drain"] is False, \
        "排空闸运行时不得持有 _WARP_LINE_CV"
