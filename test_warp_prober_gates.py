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
            _s = daemon._warp_line_state_locked("W2")
            _s["restart_hist"] = []
            # 桩每次都返回同一个出口 9.9.9.9，上面 40 轮已经把「换地址熔断」
            # 点着了。那条闸管的是「换不到新地址就别再重启」，与 cap=0 的
            # 「不设上限」是两层语义，这里只验证后者。
            _s["ip_stuck_until"] = 0.0
            _s["restart_ip_hist"] = []
        assert daemon._warp_line_restart(m, "test") is True, (
            "上限配 0 时重启被拒 —— 0 的语义是「不设上限」"
        )
    finally:
        daemon.CFG["warp"]["restart_max_per_hour_per_line"] = saved


def test_restart_fuses_when_ip_never_changes(monkeypatch):
    """连续重启拿回同一出口 = 换地址无效，必须熔断。

    实测 WARP-WG-5 在 30 分钟内重启 174 次，出口只在 104.28.205.115 / .117
    之间来回跳（CF 共享 IPv4），每次重启都掐断在途连接 —— 9router 侧就是每
    8~10 秒一批 aborted/503。重启是为了换 IP，换不到就不该继续重启。
    """
    _stub_restart(monkeypatch)
    m = _member()
    for _ in range(3):
        assert daemon._warp_line_restart(m, "test") is True, (
            "前 3 次必须放行 —— 熔断不能一上来就把重启掐掉"
        )
        with daemon._WARP_LINE_CV:
            daemon._warp_line_state_locked("W2")["next_restart_ts"] = 0.0
    st = daemon._warp_line_state("W2")
    assert st.get("ip_stuck_until", 0) > time.time(), (
        "连续 3 次重启都是同一出口却没有熔断"
    )
    assert daemon._warp_line_restart(m, "test") is False, (
        "熔断期内仍然放行重启 —— 死循环没被拦住"
    )


def test_restart_fuse_lifts_when_ip_changes(monkeypatch):
    """换得到新地址就不该熔断 —— 「重启换 IP」的规则不能被这条闸误伤。"""
    ips = iter(["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"])
    _stub_restart(monkeypatch)
    monkeypatch.setattr(daemon, "_probe_refresh_egress",
                        lambda port: next(ips))
    m = _member()
    for _ in range(4):
        assert daemon._warp_line_restart(m, "test") is True, (
            "出口每次都变了却拒绝重启 —— 熔断误伤了正常换地址"
        )
        with daemon._WARP_LINE_CV:
            daemon._warp_line_state_locked("W2")["next_restart_ts"] = 0.0
    assert not daemon._warp_line_state("W2").get("ip_stuck_until"), (
        "地址一直在变，不该写熔断时间"
    )


def test_penalty_restart_fuses_after_repeats():
    """重启后仍是惩罚期，连续 N 次就熔断。

    实测 WARP-H2-NRT-0/1/63 拿到全新 /128 后 2 秒又被判 quota(Retry-After
    17.3h)，3 分钟各重启 20+ 次 —— 换不出配额的重启只会掐断在途连接。
    """
    fuse = int(daemon._warp().get("restart_ip_stuck_fuse", 3))
    now = time.time()
    for _ in range(fuse - 1):
        assert daemon._penalty_restart_fuse("W2", now) is False, (
            f"未到第 {fuse} 次就熔断 —— 正常换地址被误伤"
        )
    assert daemon._penalty_restart_fuse("W2", now) is True, (
        f"连续 {fuse} 次「重启后仍惩罚期」却没有熔断"
    )
    st = daemon._warp_line_state("W2")
    assert st.get("ip_stuck_until", 0) > now, "熔断只打日志没写状态"
    assert not st.get("penalty_restart_streak"), (
        "熔断后计数未归零 —— 到期重试时会一上来就再次熔断"
    )


def test_penalty_restart_fused_is_read_only():
    """熔断查询不能计数 —— 否则被冷却期拦下的轮次会把线冻住。

    2026-11-10 实测：`_penalty_restart_fuse` 摆在 `_warp_line_restart` 之前，
    而后者会因为 `next_restart_ts`（冷却）、`warp_line_busy`（判忙）、
    刷新槽位限流提前返回 False。三次「探测判惩罚期但一次重启都没发生」
    就能写死 `ip_stuck_until`，把一条本来正常的线冻 900s。
    """
    fuse = int(daemon._warp().get("restart_ip_stuck_fuse", 3))
    now = time.time()
    _seed()
    for _ in range(fuse * 3):
        assert daemon._penalty_restart_fused("W2", now) is False, (
            "只读查询把线判进熔断了 —— 一次重启都还没发生"
        )
    st = daemon._warp_line_state("W2")
    assert not st.get("penalty_restart_streak"), "只读查询写了计数"
    assert not st.get("ip_stuck_until"), "只读查询写了熔断时间"


def _drive_quota_probe(monkeypatch, restart_ok=True, ip="2a09:bac5:9999::1"):
    """把一条非 active 线推到「模型层判 quota」这一步，返回重启调用记录。"""
    calls = []
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W-OTHER")
    monkeypatch.setattr(daemon, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda *a, **k: ip)
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "_warp_probe_blind", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "_probe_rate_allow", lambda: True)
    monkeypatch.setattr(daemon, "_warp_model_usable", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "_line_node", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_record_line_colo", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_channel_ban_note", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_MODEL_PROBE_SEM", None)
    monkeypatch.setattr(daemon, "_warp_line_restart",
                        lambda m, r, **k: calls.append(r) or restart_ok)
    daemon._PROBE_LAST_VERDICT["W2"] = "quota"
    return calls


def test_quota_restart_fuses_after_repeats(monkeypatch):
    """模型层 quota 的重启也必须熔断，否则它是一条没有闸门的路。

    2026-11-10 07:00 实测：3 分钟 272 次重启、池内 152 条线一条都没熔断。
    blocked 路径有闸、quota 路径没有 —— 而 quota 才是夜里的大头（436/10min）。
    重启换回来的全新 /128 首探照样 quota，代价却是每 30s 掐断一次在途连接。
    """
    fuse = int(daemon._warp().get("restart_ip_stuck_fuse", 3))
    _seed()
    calls = _drive_quota_probe(monkeypatch)
    for _ in range(fuse):
        daemon._probe_one_line(_member())
    assert len(calls) == fuse, f"前 {fuse} 次应各重启一次，实际 {len(calls)} 次"
    st = daemon._warp_line_state("W2")
    assert st.get("ip_stuck_until", 0) > 0, (
        f"连续 {fuse} 次「重启后仍 quota」却没有熔断"
    )
    # 熔断期内一次都不许再重启
    for _ in range(5):
        daemon._probe_one_line(_member())
    assert len(calls) == fuse, (
        f"熔断期内又重启了 {len(calls) - fuse} 次 —— 闸门没生效"
    )


def test_denied_restart_does_not_count_toward_fuse(monkeypatch):
    """重启被拦下（冷却/判忙/无槽位）时不计入熔断，线不该因此被冻住。"""
    _seed()
    calls = _drive_quota_probe(monkeypatch, restart_ok=False)
    for _ in range(6):
        daemon._probe_one_line(_member())
    assert len(calls) == 6, "重启尝试被提前掐断了 —— 闸门装错了位置"
    st = daemon._warp_line_state("W2")
    assert not st.get("ip_stuck_until"), (
        "一次重启都没发生，却写了熔断时间 —— 正常线被误伤"
    )
    assert not st.get("penalty_restart_streak"), "被拦下的重启不该累计计数"


def test_fuse_state_survives_daemon_restart():
    """熔断状态必须能落盘再读回来 —— 否则 daemon 重启一次闸门就归零。

    2026-11-10 实测：06:48~06:58 连做 16 次部署重启，152 条线的
    `penalty_restart_streak` 全被打回 0，10 分钟 1225 次单元重启、0 条熔断。
    根因是 `_LINE_DEFAULTS` 漏声明这三个键，而 `_load_warp_line_state` 只恢复
    声明过的键 —— 闸门被它要压的那个动作（重启）自己复位，等于没有闸门。
    """
    now = time.time()
    payload = {"version": 1, "lines": {"W2": {
        "ip": "1.1.1.1",
        "penalty_restart_streak": 2,
        "ip_stuck_until": now + 900,
        "restart_ip_hist": ["1.1.1.1"],
    }}}
    path = daemon._warp_line_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    daemon._load_warp_line_state()
    st = daemon._warp_line_state("W2")
    assert st.get("penalty_restart_streak") == 2, (
        "重启后 streak 丢了 —— 又可以从 0 开始白烧 3 次重启"
    )
    assert st.get("ip_stuck_until", 0) > now, "重启后熔断窗口丢了"
    assert st.get("restart_ip_hist") == ["1.1.1.1"], "重启后换址历史丢了"
    assert daemon._penalty_restart_fused("W2", now) is True, (
        "状态读回来了但闸门没认 —— 熔断形同虚设"
    )


def test_futile_restart_gets_braked(monkeypatch):
    """连续重启都换不出地址 → 推迟下次重启，但不冻结线路。

    判据是「重启这个动作有没有效果」，与配额无关（配额熔断已被 2026-11-09
    裁定禁用，不能顺带把它也关掉）。实测 2026-11-10 08:30 前后 12 分钟：
    263 次重启里 194 次以「到点无响应」收场，47 条扫描线的 fail_streak 涨到
    188 —— 同一个死节点被反复重启，重启名额全被它们吃掉。
    """
    limit = max(2, int(daemon._warp().get("restart_futile_limit", 6)))
    hold = float(daemon._warp().get("restart_futile_hold_sec", 900))
    now = time.time()
    _seed()
    for i in range(limit - 1):
        braked = daemon._restart_outcome_note("W2", False, now, "到点无响应")
        assert braked is False, f"第 {i+1} 次就刹车 —— 阈值被提前触发"
        assert daemon._warp_line_state("W2")["next_restart_ts"] == 0.0, (
            "还没到阈值就推了 next_restart_ts"
        )
    assert daemon._restart_outcome_note("W2", False, now, "到点无响应") is True, (
        f"连续 {limit} 次拿不到地址却没有刹车"
    )
    st = daemon._warp_line_state("W2")
    assert st["next_restart_ts"] >= now + hold - 1, "刹车没写 next_restart_ts"
    assert st["futile_restart_streak"] == 0, "刹车后计数没归零"
    # 关键：刹车不是判决 —— 不写 ip_stuck_until，也不停探测
    assert not st.get("ip_stuck_until"), (
        "把无效重启当成了地址熔断，会冻住线路（用户明令禁止的那类冻结）"
    )
    assert not st.get("stop_probe"), "刹车顺手停了探测 —— 那就再也发现不了恢复"


def test_restart_that_yields_ip_resets_futile_counter():
    """重启换出地址就清零，别让偶发失败累积成刹车。"""
    now = time.time()
    _seed()
    for _ in range(3):
        daemon._restart_outcome_note("W2", False, now, "到点无响应")
    assert daemon._warp_line_state("W2")["futile_restart_streak"] == 3
    daemon._restart_outcome_note("W2", True, now, "出口处于惩罚期")
    st = daemon._warp_line_state("W2")
    assert st["futile_restart_streak"] == 0, "换出地址后计数没清零"
    assert st["next_restart_ts"] == 0.0, "换出地址不该被刹车"


def test_scanned_tunnel_that_never_had_ip_is_judged_dead():
    """扫描发现、但一次都没出过口的隧道，不许再重启。

    `_scan_running_tunnels` 会把「在跑但没被声明」的单元补进探测集，其 tag 是
    `WARP-H2-SCAN-<idx>`。这类 tag 不在 `members` 里，`_warp_member()` 返回
    None，`_line_node_cannot_carry()` 于是直接落到 warp_wg 台账查找 —— 那里也
    没有它（node=None）→ 恒返回 False，把「从没出过口就别重启」这条判据整个
    绕过去了。

    实测 2026-11-10 09:00 前后 15 分钟：322 次重启里 226 次（70%）打在这 47
    条线上；它们 `ever_ip` 全 False、`ip` 全空、`last_ok_ts` 全 0、
    `last_verdict` 全 unknown、`fail_streak` 204~206。
    """
    _seed(tag="WARP-H2-SCAN-17", ip=None, ever_ip=False,
          last_probe_ts=1000.0, ip_ts=0.0, fail_streak=206)
    assert daemon._line_node_cannot_carry("WARP-H2-SCAN-17") is True, (
        "一次都没出过口的扫描线仍被判可重启 —— 70% 的重启名额继续被空转吃掉"
    )


def test_scanned_tunnel_that_once_had_ip_still_gets_restarted():
    """出过口的扫描线相反：更可能是会话僵死，重启仍是唯一修法。"""
    _seed(tag="WARP-H2-SCAN-18", ip=None, ever_ip=True,
          last_probe_ts=1000.0, ip_ts=0.0, fail_streak=6)
    assert daemon._line_node_cannot_carry("WARP-H2-SCAN-18") is False, (
        "出过口的线被判死，连带把能救回来的也停掉了"
    )


def test_restart_cap_does_not_stop_probing(monkeypatch):
    """达重启上限后，探测必须照常放行 —— 压的是重启，不是观测。"""
    now = 1000.0
    _seed(last_verdict="blocked", last_probe_ts=now - 3600, next_probe_ts=0.0,
          restart_hist=[now - 60 * i for i in range(20)])
    assert daemon._warp_line_needs_probe("W2", now)[0], (
        "重启配额用尽后连探测也被挡了 —— 上限必须只作用于重启这一个动作"
    )


def test_restart_cap_is_reset_on_state_reload():
    """`restart_hist` 落盘但**重启时不恢复** —— 这是有意的，不是漏了。

    `_load_warp_line_state` 里有一行 `st["restart_hist"] = []`，注释写明理由：
    重启是人工动作，不该继承重启前的小时预算。事故中 cap=20 会让每条线在
    启动后锁死 40~55 分钟（存档里 228~245 条）。

    这条用例原本断言**相反**的行为（必须恢复 20 条），与那行代码直接冲突，
    一直红着。要改判据时连这里一起改，别只改代码。
    """
    assert "restart_hist" in daemon._LINE_DEFAULTS, (
        "restart_hist 不在 _LINE_DEFAULTS 中 —— 落盘后读都读不回来"
    )

    now = time.time()
    hist = [now - 60 * i for i in range(20)]
    path = os.environ["WARP_LINE_STATE_FILE_PATH"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1,
                   "lines": {"W2": {"ip": "1.1.1.1", "fail_streak": 3,
                                    "restart_hist": hist}}}, f)

    daemon._load_warp_line_state()
    st = daemon._warp_line_state("W2")
    assert st.get("fail_streak") == 3, "其它字段必须恢复"
    assert st.get("restart_hist") == [], (
        "重启后必须清空重启预算，否则一次事故会把每条线锁死几十分钟"
    )
    # 陈旧的 `ip` 一并清掉：它和 `last_ok_ts` 是同一份证据的两半，
    # 只在「刚验证过」时才可信（2026-11-02 WG-22 绑死节点 4 分钟，
    # `ip` 还是上一次会话的旧地址，同块轮回守护因此不动它）。
    assert not st.get("ip"), "重启后不得沿用重启前那次会话的出口地址"


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
    # 端口**开着**：本用例测的是「探测拿不到地址」，不是「进程没了」。
    # `_member()` 的 10002 没人监听，不打这个桩会先撞上 `port_closed` 前置闸。
    monkeypatch.setattr(daemon, "_port_open", lambda port: True)
    restarted = []
    monkeypatch.setattr(daemon, "_warp_line_restart",
                        lambda member, reason, **kw: restarted.append(member["tag"]) or True)
    daemon._probe_one_line(_member())
    assert restarted == []
    # 探测真的跑过、真的没拿到地址 —— 这才该记 `unknown`。`last_probe_ts`
    # 必须推进，否则「没探过」与「探了不行」分不开（前者会永远显示 unknown）。
    st = daemon._warp_line_state("W2")
    assert st["last_verdict"] == "unknown"
    assert st["last_probe_ts"] > 0
    assert st["fail_streak"] == 1

def test_switch_wait_deadline_stays_under_client_timeout():
    """无候选时的等待上限必须早于调用方超时（9router ASIP_TIMEOUT_MS=2000）。

    实测 2026-10-31 19:31 起 159258 行日志：进入本分支 1608 次，1521 次能配上
    它自己的超时行，其中 1042 次（68.5%）整整走满 3s。日志里没有「等待成功」的
    完成标记（`✓ WARP 换线成功` 由别的线程写，无法与单次等待配对），所以这里只
    报能配对的那部分。走满 3s 的每一步都晚于客户端 2s 挂断，且 3s 在 _API_LOCK
    内独占，把并发调用一起串行化。
    """
    budget = daemon._warp_switch_wait_deadline(1000.0) - 1000.0
    assert budget <= 1.2 + 1e-6, (
        f"无候选时等了 {budget:.2f}s，超过调用方 2s 超时前应留的余量"
        "（ASIP_TIMEOUT_MS=2000，_rotate_once 全程持 _API_LOCK）"
    )


def test_switch_wait_cap_cannot_exceed_configured_wait():
    """上限是压低的旋钮，不能反过来把原本更短的 switch_wait_sec 拉长。"""
    saved = daemon.CFG["warp"].get("switch_wait_sec")
    try:
        daemon.CFG["warp"]["switch_wait_sec"] = 0.4
        assert daemon._warp_switch_wait_deadline(0.0) == 0.4
    finally:
        if saved is None:
            daemon.CFG["warp"].pop("switch_wait_sec", None)
        else:
            daemon.CFG["warp"]["switch_wait_sec"] = saved


def test_running_unit_is_probed_even_without_declaration(monkeypatch):
    """**有单元在跑就必须探测**，哪怕它的 tag 不在 members 里。

    WHY：`members` 是渲染产物（`.warp/h2/channels.json`），会落后于现场。
    实测 2026-11-05 远端 `usque-h2-25/26/28/29/30` 五个单元 active，tag 却
    不在 channels.json 里 —— 五条隧道从来没被探测过，报表上表现为「有线但
    永远没有结论」，日志里连它们的名字都不出现。声明是记账，单元是事实，
    探测要跟事实走。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "members", [
        {"tag": "DECLARED", "port": 5001, "service": "usque-h2-1.service"}])
    monkeypatch.setattr(daemon, "_scan_running_tunnels",
                        lambda now: {5002: ("WARP-H2-SCAN-2", "usque-h2-2.service"),
                                     5001: ("DECLARED", "usque-h2-1.service")})
    tags = {m["tag"] for m in daemon._probe_targets(1.0)}
    assert tags == {"DECLARED", "WARP-H2-SCAN-2"}, tags


def test_manual_members_survive_scan(monkeypatch):
    """扫描只补不删：手动写进 members 的线永远留在探测列表里。

    扫描靠 `systemctl list-units --state=active`，单元停着（人工关掉、正在
    重启）时它看不见那条线。若用扫描结果**替换** members，停着的线就会从
    探测列表里消失 —— 而它恰恰是最需要被观察的那条。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "members", [
        {"tag": "MANUAL-A", "port": 5001, "service": "usque-h2-1.service"},
        {"tag": "MANUAL-B", "port": 5002, "service": "usque-h2-2.service"}])
    monkeypatch.setattr(daemon, "_scan_running_tunnels", lambda now: {})
    tags = {m["tag"] for m in daemon._probe_targets(1.0)}
    assert tags == {"MANUAL-A", "MANUAL-B"}, tags


def test_scan_does_not_duplicate_declared_port(monkeypatch):
    """扫描到的端口已被声明时用声明的 tag，不要造出第二个目标。"""
    monkeypatch.setitem(daemon.CFG["warp"], "members", [
        {"tag": "DECLARED", "port": 1180, "service": "usque-h2-0.service"}])
    monkeypatch.setattr(daemon, "_scan_running_tunnels",
                        lambda now: {1180: ("WARP-H2-SCAN-0", "usque-h2-0.service")})
    targets = daemon._probe_targets(1.0)
    assert len(targets) == 1 and targets[0]["tag"] == "DECLARED", targets
