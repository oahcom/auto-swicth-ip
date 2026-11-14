"""①组保真契约 · 台账护栏（附件 A3 空表拒写+条数缩水拒写）与
_LINE_DEFAULTS 白名单（附件 A4）。

【防什么事故】
- 空表拒写：并发测试进程/误启动实例用空内存覆盖盘上台账，把 16h 的
  ban_until 全抹掉（已实际发生）；抹掉后切回烧掉的 IP = 双实例互抹事故。
- 条数缩水拒写：内存表未加载就落盘，N 条被写成 1 条（2026-11-11 实测
  3 条假数据被覆盖成 1 条，无任何日志）。
- _LINE_DEFAULTS 白名单：漏声明字段=静默丢键（penalty_restart_streak 熔断
  清零事故：daemon 重启把全池熔断打回 0，10 分钟 1225 次重启、0 条熔断）；
  list 默认必须 None 不能 []（共享对象被 setdefault 串线）。
【事故出处】附件 A3/A4。
【三分法】A 保真契约：护栏非冗余，删=事故复活，只许钉住。
"""
import json
import time

import pytest

import daemon as d


def test_empty_memory_refuses_to_overwrite_disk_ledger():
    """进程内为空但盘上已有台账时拒绝写入（附件 A3）。"""
    path = d._warp_ip_used_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"used": {"2001:db8::b1": {"ts": 1.0, "blocked": True,
                                             "ban_until": 9e9}}}, f)
    d._WARP_IP_USED.clear()
    d._LEDGER_ALLOW_EMPTY_WRITE = False
    d._save_warp_ip_used()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert "2001:db8::b1" in data.get("used", data)  # 盘上台账原样保留


def test_explicit_empty_write_allowed_once():
    """清理逻辑显式放行一次写空，且放行只生效一次（附件 A3 逃生口）。"""
    path = d._warp_ip_used_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"used": {"x": {"ts": 1.0}}}, f)
    d._WARP_IP_USED.clear()
    d._LEDGER_ALLOW_EMPTY_WRITE = True
    d._save_warp_ip_used()
    assert d._LEDGER_ALLOW_EMPTY_WRITE is False  # 放行即复位
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert (data.get("used", data)) == {}
    # 再次空写无放行 → 拒绝（空表拒写恢复生效）
    d._save_warp_ip_used()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert (data.get("used", data)) == {}  # 仍是空但没被改动


def test_line_state_empty_memory_refuses_overwrite():
    """状态表同款空表拒写（附件 A3）。"""
    path = d._warp_line_state_path()
    payload = {"version": 1,
               "lines": {"T1": {"ip": "i1"}, "T2": {"ip": "i2"}}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    d._WARP_LINE_STATE.clear()
    d._save_warp_line_state(force=True)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert len(data.get("lines", data)) == 2


def test_line_state_shrink_refuses_overwrite():
    """盘上 N 条、内存 M<N 条时拒绝覆盖（附件 A3 条数缩水拒写）。

    防什么事故：未加载就落盘（启动早期/清理函数抢跑）把整表写没——
    兜底挡的是「整表覆盖」这类危险写法的所有未来变体。
    """
    path = d._warp_line_state_path()
    lines = {f"T{i}": {"ip": f"i{i}"} for i in range(3)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "lines": lines}, f)
    d._WARP_LINE_STATE.clear()
    d._warp_line_state_locked("ONLY")  # 内存只剩 1 条
    d._save_warp_line_state(force=True)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert len(data.get("lines", data)) == 3  # 拒绝覆盖，盘上原样


def test_line_state_equal_or_grow_writes():
    """条数不缩水时正常落盘（护栏不误伤正常写入）。"""
    path = d._warp_line_state_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1,
                   "lines": {"T1": {"ip": "i1"}, "T2": {"ip": "i2"}}}, f)
    d._WARP_LINE_STATE.clear()
    for t in ("T1", "T2", "T3"):
        d._warp_line_state_locked(t)
    d._save_warp_line_state(force=True)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert len(data.get("lines", data)) == 3


def test_load_restores_only_declared_keys():
    """白名单语义：载入只恢复 _LINE_DEFAULTS 声明过的键（附件 A4）。

    防什么事故：漏声明=写进盘上、读不回来（blocked_notes 事故：磁盘 156 个
    tag 带 blocked_notes 的 0 个）；反之未声明的残留键（quota_until 等已删
    字段）被滤掉，不需要额外清理函数。
    注：载入的证据新鲜度净化（ip 需配对 fresh 的 last_ok_ip/last_ok_ts，
    否则连 ip 一起清）是另一条保真语义，见 test_fidelity_protocol_pins 族。
    """
    now = time.time()
    path = d._warp_line_state_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "lines": {
            "T1": {"ip": "keep", "ip_ts": now,
                   "last_ok_ts": now, "last_ok_ip": "keep",
                   "penalty_restart_streak": 3,
                   "quota_until": 999.0, "stop_probe": True}}}, f)
    d._WARP_LINE_STATE.clear()
    d._load_warp_line_state()
    st = d._WARP_LINE_STATE["T1"]
    assert st["ip"] == "keep"
    assert st["penalty_restart_streak"] == 3  # 熔断状态活过重启（16 连重启事故）
    assert st["stop_probe"] is True
    assert "quota_until" not in st  # 已删字段不复活


def test_declared_fields_survive_save_load_roundtrip():
    """声明过的字段保存/加载往返不丢（白名单=迁移语义等价物）。"""
    now = time.time()
    d._WARP_LINE_STATE.clear()
    st = d._warp_line_state_locked("T-R")
    st["penalty_restart_streak"] = 7
    st["ip_stuck_until"] = now + 3600
    st["futile_restart_streak"] = 2
    st["ban_probes"] = [[now, now, 3, "quota"]]
    d._save_warp_line_state(force=True)
    d._WARP_LINE_STATE.clear()
    d._load_warp_line_state()
    st2 = d._WARP_LINE_STATE["T-R"]
    assert st2["penalty_restart_streak"] == 7
    assert st2["ip_stuck_until"] == pytest.approx(now + 3600)
    assert st2["futile_restart_streak"] == 2
    assert st2["ban_probes"] == [[now, now, 3, "quota"]]


def test_list_defaults_none_not_shared():
    """list 默认必须 None 不能 []：共享对象 setdefault 会让所有渠道串线（附件 A4）。
    """
    d._WARP_LINE_STATE.clear()
    s1 = d._warp_line_state_locked("T-A")
    s2 = d._warp_line_state_locked("T-B")
    assert s1["ban_probes"] is None and s2["ban_probes"] is None
    assert s1["restart_ip_hist"] is None
    s1["ban_probes"] = [[1.0, 1.0, 1, "quota"]]
    assert s2["ban_probes"] is None  # 不共享同一个 list


def test_stop_probe_default_bool():
    """stop_probe 默认 False 的 bool 语义（附件 A4：非 bool 恒真事故）。"""
    d._WARP_LINE_STATE.clear()
    st = d._warp_line_state_locked("T-C")
    assert st["stop_probe"] is False
    assert isinstance(st["stop_probe"], bool)
