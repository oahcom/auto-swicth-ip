# -*- coding: utf-8 -*-
"""排空后再回收（_drain_gate / _note_bad_maybe_retire / _lines_in_use）回归。

主人裁定的三条口径（2026-11-11）：
1. 判闲不按时间，按字节流事实（conns / young / 字节增量，可探测）；
2. 必须设回收上限（探测有盲区：TTFT 长尾 max 532.7s）；
3. H2 线上限翻倍。

另有台账洞加固（fn_xxx 教训）：无退役记录但有连接 = 刚退役被漏记，
按「刚退役」起表排空，绝不静默放行。

测试纪律（同根教训）：patch 过的函数必须还原，否则后面的用例测的是
mock 不是实现——静默假绿。全部 monkeypatch 纯验证，不起服务、不发 HTTP。
"""
import sys

sys.path.insert(0, "/home/administrator/dkk-projects/auto-switch-ip")

import daemon as d

# 原始实现存档：任何用例 patch 后必须能还原，防止 mock 泄漏（fn_xxx 同根）
_ORIG = {k: getattr(d, k) for k in
         ("_warp", "_lines_in_use", "_warp_line_stats", "_note_bad_maybe_retire",
          "log", "active_warp_tag", "consumer_slots", "_slot_current",
          "_slot_by_name", "_slot_begin_repair", "_repair_slot")}

PASS = []


def ok(name):
    PASS.append(name)
    print("PASS ", name)


def restore_all():
    for k, v in _ORIG.items():
        setattr(d, k, v)


W = {"drain_before_recycle_enabled": True,
     "drain_max_wait_sec": 120.0,
     "drain_quiet_rounds": 2,
     "drain_poll_sec": 5,
     "drain_force_retire_sec": 120}

CF = {"tag": "WARP-CF-A", "kind": "cf"}
H2 = {"tag": "WARP-H2-X", "kind": "h2"}


def reset_state():
    d._RETIRED_AT.clear()
    d._DRAIN_PENDING.clear()
    d._BAD_ON_SLOT.clear()
    d._NIGHT_LOG_TS.clear()


def set_warp(**over):
    w = dict(W)
    w.update(over)
    d._warp = lambda: w


def quiet_stats(tag):          # 老连接、零字节增长
    return (500, 0, 2)


# 基线 mock
set_warp()
d._lines_in_use = lambda: set()
d._warp_line_stats = lambda tag: (0, 0, 0)
d.log = lambda *a, **k: None

# ---------------------------------------------------------------- T1 开关关闭
set_warp(drain_before_recycle_enabled=False)
allow, _ = d._drain_gate(CF, 1000.0, "r")
assert allow, "开关关闭应放行"
ok("T1 开关关闭 → 放行（回滚路径）")
restore_all()
set_warp()

# ------------------------------------------------------------ T2 在槽一律拒绝
d._lines_in_use = lambda: {CF["tag"]}
kicks = []
d._note_bad_maybe_retire = lambda tag, now, reason="": kicks.append((tag, now))
reset_state()
d._RETIRED_AT[CF["tag"]] = 900.0
d._warp_line_stats = lambda tag: (0, 0, 0)
allow, note = d._drain_gate(CF, 1000.0, "出口处于惩罚期")
assert not allow and "在槽" in note, note
assert kicks == [(CF["tag"], 1000.0)], kicks
assert CF["tag"] in d._RETIRED_AT, "拒绝时不得清退役台账"
ok("T2 在槽 → 拒绝并触发判坏记账")
restore_all()
set_warp()
d._lines_in_use = lambda: set()

# ------------------------------------------------ T3 游离线零连接 → 放行
reset_state()
d._warp_line_stats = lambda tag: (0, 0, 0)
allow, note = d._drain_gate(CF, 1000.0, "r")
assert allow and "游离线" in note, note
ok("T3 游离线+零连接 → 放行，走既有判忙逻辑")

# --------------------------------- T3b 台账洞：无退役记录却有连接 → 不放行
reset_state()
d._warp_line_stats = lambda tag: (500, 2, 3)          # 有年轻连接
allow, note = d._drain_gate(CF, 1000.0, "r")
assert not allow, "台账洞不得静默放行（fn_xxx 同根）"
assert d._RETIRED_AT.get(CF["tag"]) == 1000.0, "应按刚退役起表"
d._warp_line_stats = quiet_stats                       # 变老连接静止
allow, _ = d._drain_gate(CF, 1005.0, "r")              # 第 1 轮
assert not allow
allow, _ = d._drain_gate(CF, 1010.0, "r")              # 第 2 轮
assert allow, "台账洞线排空后应正常回收"
ok("T3b 无退役记录却有连接 → 按刚退役起表排空（台账洞加固）")

# ------------------------------------------- T4 退役 + 零连接 → 立即回收
reset_state()
d._RETIRED_AT[CF["tag"]] = 900.0
d._warp_line_stats = lambda tag: (500, 0, 0)
allow, note = d._drain_gate(CF, 950.0, "r")
assert allow and "零连接" in note, note
assert CF["tag"] not in d._RETIRED_AT, "回收后应清退役台账"
ok("T4 退役+零连接 → 立即回收并清台账")

# --------------------------------------------------- T5 年轻连接立即判忙
reset_state()
d._RETIRED_AT[CF["tag"]] = 900.0
d._warp_line_stats = lambda tag: (500, 2, 3)
allow, note = d._drain_gate(CF, 910.0, "r")
assert not allow and "年轻连接" in note, note
assert d._DRAIN_PENDING[CF["tag"]]["rounds"] == 0
ok("T5 年轻连接 → 判忙（首字节/流式在途）")

# ------------------------------------------------- T6 老连接仍有字节流 → 归零
reset_state()
d._RETIRED_AT[CF["tag"]] = 900.0
d._DRAIN_PENDING[CF["tag"]] = {"rounds": 1, "last_bytes": 100}
d._warp_line_stats = lambda tag: (200, 0, 2)
allow, note = d._drain_gate(CF, 910.0, "r")
assert not allow and "字节流" in note, note
assert d._DRAIN_PENDING[CF["tag"]] == {"rounds": 0, "last_bytes": 200}
ok("T6 字节有增长 → 轮数归零重来")

# --------------------------------------- T7 老连接连续静止达标 → 允许回收
reset_state()
d._RETIRED_AT[CF["tag"]] = 900.0
d._warp_line_stats = quiet_stats
allow, _ = d._drain_gate(CF, 910.0, "r")              # 第 1 轮
assert not allow
allow, note = d._drain_gate(CF, 915.0, "r")           # 第 2 轮
assert allow and "无字节增长" in note, note
assert CF["tag"] not in d._DRAIN_PENDING
ok("T7 连续 2 轮零增长 → 回收")

# ------------------------------------------- T8 非 H2 上限 120s → 强制回收
reset_state()
d._RETIRED_AT[CF["tag"]] = 900.0
d._warp_line_stats = lambda tag: (500, 2, 3)          # 即使有年轻连接也到上限
allow, note = d._drain_gate(CF, 1021.0, "r")          # elapsed=121 > 120
assert allow and "强制回收" in note and "H2" not in note, note
assert CF["tag"] not in d._RETIRED_AT
ok("T8 非 H2 超 120s → 强制回收（上限兜底）")

# ------------------------------------- T9 H2 上限翻倍 240s：121s 仍不回收
reset_state()
d._RETIRED_AT[H2["tag"]] = 900.0
d._warp_line_stats = quiet_stats
allow, _ = d._drain_gate(H2, 1021.0, "r")             # elapsed=121 < 240
assert not allow, "H2 在 121s 不得被上限回收"
allow, note = d._drain_gate(H2, 1150.0, "r")          # elapsed=250 > 240
assert allow and "H2x2" in note, note
ok("T9 H2 上限翻倍：121s 拒绝 / 250s 强制回收")

# --------------------------------------------- T10 _slot_note_switch 写台账
reset_state()
d._BAD_ON_SLOT["WARP-A"] = 1.0
d._DRAIN_PENDING["WARP-A"] = {"rounds": 1, "last_bytes": 1}
d._RETIRED_AT["WARP-B"] = 1.0
slot = {"name": "slot-3"}
d._slot_note_switch(slot, "WARP-A", "2a09:1", "WARP-B", "2a09:2", 1234.5)
assert d._RETIRED_AT.get("WARP-A") == 1234.5
assert "WARP-B" not in d._RETIRED_AT, "新入选线应撤销退役时间戳"
assert "WARP-A" not in d._BAD_ON_SLOT
assert "WARP-A" not in d._DRAIN_PENDING
ok("T10 切槽写退役台账/清判坏/新线撤销退役")

# -------------------------------------------- T11 判坏在槽超时 → 主动认领切槽
restore_all()                                          # 测真实现，不是 mock
set_warp()
reset_state()
tag = "WARP-CF-BAD"
begin = []
tlog = []
d._slot_line_owners = lambda: {tag: ["slot-2", "slot-5"]}
d._slot_by_name = lambda name: {"name": name}
d._slot_current = lambda s: (tag, "2a09:x")
d._slot_begin_repair = (
    lambda name, reason=None: begin.append((name, reason)) or True)
d._repair_slot = lambda slot, reason: None
d.log = lambda *a, **k: tlog.append(a)
d._BAD_ON_SLOT[tag] = 1000.0 - 150.0                   # 已等 150s > 120s
d._note_bad_maybe_retire(tag, 1000.0, "模型层 quota")
assert ("slot-2", "drain_force_retire") in begin, begin
assert ("slot-5", "drain_force_retire") in begin, begin
assert tag not in d._BAD_ON_SLOT, "踢过之后应清账（防连续踢）"
assert tlog, "应打主动认领日志"
d._note_bad_maybe_retire(tag, 1001.0, "模型层 quota")  # 清账后重新计时
assert len(begin) == 2, "清账后 120s 内不得再踢"
ok("T11 判坏在槽超 120s → 两个所属槽各认领一次，随后重新计时")

# ------------------------------------------ T12 未超时 → 只记账不动作
reset_state()
begin.clear()
d._BAD_ON_SLOT[tag] = 1000.0 - 30.0                    # 只等了 30s
d._note_bad_maybe_retire(tag, 1000.0, "r")
assert not begin, "未超时不得踢槽"
ok("T12 判坏在槽未超时 → 只记账等真实报障")

# ------------------------------------------------------- T13 _lines_in_use
restore_all()
set_warp()
real_health = dict(d._SLOT_HEALTH)
d._SLOT_HEALTH.clear()
try:
    d.active_warp_tag = lambda: "WARP-MAIN"
    d.consumer_slots = lambda: [{"name": "slot-%d" % i} for i in range(1, 7)]
    d._slot_current = lambda s: ("WARP-L" + s["name"][-1], None)
    for i in range(1, 7):
        d._slot_health("slot-%d" % i)["tag"] = "WARP-S%d" % i
    used = d._lines_in_use()
    assert used == {"WARP-MAIN"} | {"WARP-S%d" % i for i in range(1, 7)}, used
    # 有槽位还没建台账（daemon 重启后的启动窗口）→ 回退真实 selector 查询
    d._slot_health("slot-4")["tag"] = None
    used = d._lines_in_use()
    assert used == {"WARP-MAIN"} | {"WARP-L%d" % i for i in range(1, 7)}, used
    ok("T13 在用集合：台账全 → 纯内存；有缺 → 回退真实查询")
finally:
    d._SLOT_HEALTH.clear()
    d._SLOT_HEALTH.update(real_health)

# ------------------------------------------------ T14 上限优先于快照不可用
restore_all()
set_warp()
d._lines_in_use = lambda: set()
reset_state()
d._RETIRED_AT[CF["tag"]] = 900.0
d._warp_line_stats = lambda tag: None                  # 连接快照查不到
allow, note = d._drain_gate(CF, 1021.0, "r")           # elapsed=121 > 120
assert allow and "强制回收" in note, note
d._RETIRED_AT[CF["tag"]] = 900.0                       # 上一发已清台账，重置
allow, note = d._drain_gate(CF, 950.0, "r")            # 未到上限
assert not allow and "快照" in note, note
ok("T14 上限判定先于快照查询（不可用不会卡死回收）")

# ------------------------------------- T15 无 mock 泄漏：关键函数已还原
restore_all()
assert d._note_bad_maybe_retire is _ORIG["_note_bad_maybe_retire"]
assert d._lines_in_use is _ORIG["_lines_in_use"]
assert d._warp is _ORIG["_warp"]
assert d._warp_line_stats is _ORIG["_warp_line_stats"]
ok("T15 mock 全部还原（防静默假绿，fn_xxx 同根教训）")

print("\n%d/%d 项全部通过" % (len(PASS), len(PASS)))
