"""探测节奏与重启上限的回归用例（2026-10-27）。

背景：`next_probe_ts` 被 IP 惩罚期推到次日 08:00，20 条线全部冻结 56.6 分钟，
74 次换线全废。门禁被拆掉后翻到另一个极端：非 OK 的线每轮都探，配额开环消耗。
本文件锁的是「补上来的节奏」和「本就不存在的重启上限」。

命名约定：`<场景>_<期望行为>`，断言信息用中文说明"为什么这是错的"。
"""
import contextlib
import json
import os
import time

import pytest

import daemon


@contextlib.contextmanager
def _cadence(seconds):
    """临时改节奏，退出时还原。

    节奏是运维旋钮（0=立刻重排，60=每条线每分钟一次），用例要能在两种取值下
    验证语义，而不是被当前生产值绑住 —— 生产值改了不该让契约用例变红。
    """
    saved = daemon._warp().get("line_probe_cadence_sec", 60)
    daemon.CFG["warp"]["line_probe_cadence_sec"] = seconds
    try:
        yield
    finally:
        daemon.CFG["warp"]["line_probe_cadence_sec"] = saved


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每条用例独立的状态表 + 落盘隔离，避免跨用例污染。"""
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {})
    monkeypatch.setattr(daemon, "_WARP_PROBING", set())
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_PROBE_LAST_VERDICT", {})
    monkeypatch.setattr(daemon, "_PROBE_BLOCKED_AT", {})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_save_warp_line_state", lambda *a, **k: None)
    monkeypatch.setenv("WARP_LINE_STATE_FILE_PATH", str(tmp_path / "line_state.json"))


def _member(tag="W2", port=10002):
    return {"tag": tag, "port": port, "service": f"usque-warp-{tag.lower()}"}


def _seed(tag="W2", **kwargs):
    st = dict(ip="1.1.1.1", last_ok_ts=0.0, last_ok_ip=None)
    st.update(kwargs)
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked(tag).update(st)
    return st


# ─────────────────────────────────────────────────────────────
# B12 — 节奏必须与结论无关
# ─────────────────────────────────────────────────────────────

def test_probe_cadence_comes_from_config():
    """节奏是配置项的函数，不是结论的函数。

    0 = 探完立刻重排（当前默认：36 条线 × 60s 会把吞吐锁死在 36 条/分钟，
    而单次探测只要 1.9~5.7s，加渠道就白加了）。这里锁的是「改配置立刻可调」，
    不是某个具体数值 —— 具体数值属于运维决策，不属于契约。
    """
    now = 1000.0
    _seed(last_verdict="quota", last_probe_ts=now, next_probe_ts=0.0)

    with _cadence(60):
        assert not daemon._warp_line_needs_probe("W2", now + 1)[0], (
            "cadence=60 时刚探完 1 秒就放行 —— 配置没被读到"
        )
        assert daemon._warp_line_needs_probe("W2", now + 61)[0], (
            "cadence=60 时过了 61 秒仍不放行 —— 节奏卡死了"
        )

    with _cadence(0):
        assert daemon._warp_line_needs_probe("W2", now + 1)[0], (
            "cadence=0 时刚探完却不放行 —— 0 的语义是「立刻重排」"
        )


def test_cadence_survives_zero_next_probe_ts():
    """next_probe_ts 为 0 时节奏仍必须生效。

    `_warp_line_note_ok` / `_warp_line_invalidate` 都会把它写成 0，
    任何读它的节奏实现都会在此时完全放行。
    """
    now = 1000.0
    _seed(last_verdict="quota", last_probe_ts=now, next_probe_ts=0.0)
    with _cadence(60):
        assert not daemon._warp_line_needs_probe("W2", now + 1)[0], (
            "刚探完 1 秒就允许再探 —— next_probe_ts=0 把节奏吃掉了"
        )


def test_cadence_is_verdict_independent():
    """节奏不得因结论不同而变化 —— 观测不因观测结果而改变频率。"""
    now = 1000.0
    gaps = {}
    for verdict in ("quota", "blocked", "flagged", "err", "unreachable"):
        _seed(last_verdict=verdict, last_probe_ts=now, next_probe_ts=0.0)
        gaps[verdict] = next(i for i in range(600)
                             if daemon._warp_line_needs_probe("W2", now + i)[0])
    assert len(set(gaps.values())) == 1, (
        f"不同结论的重探间隔不一致：{gaps} —— "
        "结论只该影响「探完拿它做什么」，不该影响「下次还探不探」"
    )


def test_observation_does_not_park_the_line():
    """一次 quota 结论不得把下一次观测推到惩罚期结束。

    这是本次故障的根因形态：结论写回排程，13.3 小时后才允许再看。
    """
    now = 1000.0
    _seed(ip=None)
    daemon._warp_line_record_observation(
        "W2", "1.1.1.1", "quota", ts=now, started_at=now - 1)
    st = daemon._warp_line_state("W2")
    assert st["next_probe_ts"] - now <= daemon._line_probe_cadence() + 1, (
        f"quota 结论把下次探测推到 {st['next_probe_ts'] - now:.0f}s 后 —— "
        "结论驱动停探，这条线在惩罚期内零观测"
    )


def test_prober_never_freezes_every_line(monkeypatch):
    """全部 20 条线都被判 quota 时，探测器仍须在节奏内调度它们。"""
    now = time.time()
    members = [_member(f"W{i}", 10000 + i) for i in range(20)]
    monkeypatch.setitem(daemon.CFG["warp"], "members", members)
    for m in members:
        _seed(m["tag"], ip=f"10.0.0.{m['tag'][1:]}", last_verdict="quota",
              last_probe_ts=now - 120, next_probe_ts=now + 13.3 * 3600)

    schedulable = [m["tag"] for m in members
                   if daemon._warp_line_needs_probe(m["tag"], now)[0]]
    assert len(schedulable) == len(members), (
        f"20 条线中只有 {len(schedulable)} 条可被调度 —— 其余没有复活入口"
    )


# ─────────────────────────────────────────────────────────────
# B13 — 每线每小时重启次数必须有硬上限
# ─────────────────────────────────────────────────────────────

def _stub_restart(monkeypatch, new_ip="9.9.9.9"):
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_line_busy", lambda tag, win: False)
    monkeypatch.setattr(daemon, "_acquire_refresh_slot", lambda tag: True)
    monkeypatch.setattr(daemon, "_release_refresh_slot", lambda tag: None)
    monkeypatch.setattr(daemon, "_RESTART_SEM", None)
    monkeypatch.setattr(daemon, "_restart_service", lambda svc, tag=None: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda port, t: True)
    monkeypatch.setattr(daemon, "_probe_refresh_egress", lambda port: new_ip)
    monkeypatch.setattr(daemon, "_invalidate_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(daemon, "_line_block_remaining", lambda ip, ts: 0.0)


def test_restart_rate_is_capped(monkeypatch):
    """配置了上限时，超出必须被拒；配 0 时放行。

    实测无上限时 WARP-CF-F 1.67h 内重启 127 次 = 76 次/h。当前生产值 0
    （按主人要求去掉上限，唯一刹车是退避），但机制必须还能用 —— 配置一改
    就该立刻生效，而不是留下一段永远走不到的死代码。
    """
    _stub_restart(monkeypatch)
    m = _member()
    saved = daemon.CFG["warp"].get("restart_max_per_hour_per_line", 20)
    daemon.CFG["warp"]["restart_max_per_hour_per_line"] = 20
    try:
        performed = 0
        for _ in range(40):
            if daemon._warp_line_restart(m, "test"):
                performed += 1
                with daemon._WARP_LINE_CV:
                    daemon._warp_line_state_locked("W2")["next_restart_ts"] = 0.0

        assert performed <= 20, (
            f"上限配 20 时同一线连跑 40 次放行了 {performed} 次 —— 上限未生效"
        )
    finally:
        daemon.CFG["warp"]["restart_max_per_hour_per_line"] = saved

    daemon.CFG["warp"]["restart_max_per_hour_per_line"] = 0
    try:
        with daemon._WARP_LINE_CV:
            daemon._warp_line_state_locked("W2")["restart_hist"] = []
        assert daemon._warp_line_restart(m, "test") is True, (
            "上限配 0 时重启被拒 —— 0 的语义是「不设上限」"
        )
    finally:
        daemon.CFG["warp"]["restart_max_per_hour_per_line"] = saved


def test_restart_cap_does_not_stop_probing(monkeypatch):
    """达重启上限后，探测必须照常放行 —— 压的是重启，不是观测。"""
    now = 1000.0
    _seed(last_verdict="blocked", last_probe_ts=now - 3600, next_probe_ts=0.0,
          restart_hist=[now - 60 * i for i in range(20)])
    assert daemon._warp_line_needs_probe("W2", now)[0], (
        "重启配额用尽后连探测也被挡了 —— 上限必须只作用于重启这一个动作"
    )


def test_restart_cap_survives_state_reload():
    """restart_hist 必须随状态表落盘并被正确恢复。

    `_load_warp_line_state` 只恢复 `k in _LINE_DEFAULTS` 的键 ——
    新键不加进那张表会被静默丢弃，daemon 每重启一次上限就清零。
    """
    assert "restart_hist" in daemon._LINE_DEFAULTS, (
        "restart_hist 不在 _LINE_DEFAULTS 中 —— 落盘后重启即丢，上限形同虚设"
    )

    now = time.time()
    hist = [now - 60 * i for i in range(20)]
    path = os.environ["WARP_LINE_STATE_FILE_PATH"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1,
                   "lines": {"W2": {"ip": "1.1.1.1", "restart_hist": hist}}}, f)

    daemon._load_warp_line_state()
    restored = daemon._warp_line_state("W2").get("restart_hist") or []
    assert len(restored) == 20, (
        f"重启后只恢复了 {len(restored)} 条重启记录 —— 上限窗口被清零"
    )


def test_restart_cap_window_rolls_forward(monkeypatch):
    """一小时前的那批重启不能再占用配额。"""
    _stub_restart(monkeypatch)
    now = time.time()
    _seed(restart_hist=[now - 3601 - i for i in range(20)])
    assert daemon._warp_line_restart(_member(), "test") is True, (
        "窗口没有前滚 —— 一小时前的重启永久占着配额，这条线再也不能换地址"
    )


# ─────────────────────────────────────────────────────────────
# B6 — 换线不得空等
# ─────────────────────────────────────────────────────────────

def test_switch_wait_is_bounded():
    """无候选时的等待上限不得超过 3s。"""
    assert float(daemon._warp().get("switch_wait_sec", 3)) <= 3, \
        "switch_wait_sec 仍 >3s：74 次触发 × 20s = 24.7 分钟纯空转"


# ─────────────────────────────────────────────────────────────
# 回归保护
# ─────────────────────────────────────────────────────────────

def test_single_unknown_does_not_restart(monkeypatch):
    """单次取不到地址仍不得重启 —— 那是探测侧瞬态，不是隧道故障。"""
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: None)
    restarted = []
    monkeypatch.setattr(daemon, "_warp_line_restart",
                        lambda member, reason: restarted.append(member["tag"]) or True)
    daemon._probe_one_line(_member())
    assert restarted == []
    assert daemon._warp_line_state("W2")["last_verdict"] == "unknown"
