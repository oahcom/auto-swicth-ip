#!/usr/bin/env python3
"""巡检分类七（产物面 ⊄ 生效面）与自检异常隔离（C 项 / T3 + T8）。

背景：热重载 204 ≠ selector 成员已生效（INC-017，2026-11-13 实测磁盘
7 份配置各 147 条成员、运行时只有 108 条，新线 PUT 进 selector 只拿
400 `Selector update error: not found`）。六面对账里「生效面」此前是
唯一盲区，分类七由 `_config_consistency_check_impl` 主动报出来 ——
只报告、不触发自愈（自愈沿用 bc89daa 的 `_slot_drift_after_reload` /
`_slot_drift_maybe_heal`，巡检不得重复触发）。

T3 巡检主动发现分类七：
  ① 产物面有、生效面无 → missing 含真实线，problems 计数增加；
  ② 生效面有、产物面无 → extra 含真实线；
  ③ 运行时读不到 → verdict=unknown、进 selector_unverifiable、
     **不计 problems 也绝不当一致**（T5 假阴性防护）；
  ④ 休眠/占位 tag 不构成漂移（防误杀回归）。

T8 异常隔离：compare_members 抛异常 → selfcheck_failed=True、
不向外抛。锁死 2026-11-11 17:22 事故的故障模型（自检 NameError →
崩溃循环 23 次、服务瘫 5 分钟）：观测失败 = 少一条告警，绝不掀翻服务。
"""
from types import SimpleNamespace

import pytest

import asip.config_projection as config_projection
import daemon

REAL = {"WARP-L1", "WARP-L2", "WARP-L3"}
SLOTS = [
    {"name": "slot-1", "selector": "opencode-out", "port": 7890,
     "clash_api_port": 9090,
     "config_file": "/tmp/asip-drift-inspect/cfg-slot1.json"},
    {"name": "slot-2", "selector": "opencode-out", "port": 7892,
     "clash_api_port": 9092,
     "config_file": "/tmp/asip-drift-inspect/cfg-slot2.json"},
]


def _face(rep, name):
    return next(f for f in rep["selector_drift"] if f["face"] == name)


@pytest.fixture
def sandbox(monkeypatch):
    """分类一~六保持干净（problems 基线 0），只留分类七的三面输入。"""
    log_lines = []
    monkeypatch.setattr(daemon, "log",
                        lambda *a, **k: log_lines.append(
                            " ".join(str(x) for x in a)))
    monkeypatch.setattr(daemon, "CFG", {
        "singbox": {"config_file": "main.json"},
        "warp": {"selector": "opencode-out"},
        "log_file": "/tmp/asip-drift-inspect/daemon.log",
        "intervals": {},
    })
    monkeypatch.setattr(daemon, "_warp", lambda: {
        "selector": "opencode-out",
        "members": [{"tag": t, "kind": "wg",
                     "service": "svc-%s.service" % t, "port": 1110 + i}
                    for i, t in enumerate(sorted(REAL))],
    })
    monkeypatch.setattr(daemon, "_TIER_DISABLED", set())
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {t: {} for t in REAL})
    monkeypatch.setattr(daemon, "_scan_running_tunnels", lambda now: {})
    monkeypatch.setattr(daemon, "_singbox_outbound_tags", lambda: set(REAL))
    monkeypatch.setattr(daemon, "_scan_unit_still_on_disk", lambda tag: False)
    monkeypatch.setattr(daemon, "consumer_slots", lambda: list(SLOTS))

    # 三面输入：disk=产物面 / rt=生效面（槽位）/ main_rt=生效面（主面 API）
    disk = {"main": set(REAL),
            "slot-1": set(REAL), "slot-2": set(REAL)}
    rt = {"slot-1": set(REAL), "slot-2": set(REAL)}
    main_rt = set(REAL)

    def fake_disk(slot):
        key = ("main"
               if slot.get("config_file") == daemon.CFG["singbox"]["config_file"]
               else slot["name"])
        return set(disk.get(key, set()))

    monkeypatch.setattr(daemon, "_slot_disk_selector_members", fake_disk)
    monkeypatch.setattr(daemon, "_slot_selector_members",
                        lambda slot: (None if rt.get(slot["name"]) is None
                                      else set(rt[slot["name"]])))
    monkeypatch.setattr(daemon, "singbox_api",
                        lambda *a, **k: (200, {"all": sorted(main_rt)}))
    return SimpleNamespace(logs=log_lines, disk=disk, rt=rt, main_rt=main_rt)


# ── T3：巡检主动发现分类七 ─────────────────────────────────────────────

def test_inspect_reports_selector_missing_real_lines(sandbox):
    """产物面有、生效面无的真实线 → missing 报出来，problems 计数增加。"""
    sandbox.rt["slot-2"].discard("WARP-L2")
    rep = daemon._config_consistency_check()
    face = _face(rep, "slot-2")
    assert face["verdict"] == "drift"
    assert face["missing"] == ["WARP-L2"]
    assert face["extra"] == []
    assert rep["problems"] == 1
    assert rep["selector_unverifiable"] == []
    assert any("产物面 ⊄ 生效面" in ln for ln in sandbox.logs), \
        "detail 必须出现「产物面 ⊄ 生效面」文案"


def test_inspect_reports_selector_extra_real_lines(sandbox):
    """生效面有、产物面无的真实线 → extra 同样算漂移（双向差集）。"""
    sandbox.disk["slot-1"].discard("WARP-L1")
    rep = daemon._config_consistency_check()
    face = _face(rep, "slot-1")
    assert face["verdict"] == "drift"
    assert face["extra"] == ["WARP-L1"]
    assert face["missing"] == []
    assert rep["problems"] == 1


def test_inspect_unknown_face_is_unverifiable_not_pass(sandbox):
    """运行时读不到 → verdict=unknown，绝不当一致、也绝不计 problems。"""
    sandbox.rt["slot-2"] = None
    rep = daemon._config_consistency_check()
    face = _face(rep, "slot-2")
    assert face["verdict"] == "unknown"
    assert face["verdict"] != "consistent", "「无法判定」绝不洗成通过"
    assert "slot-2" in rep["selector_unverifiable"]
    assert rep["problems"] == 0
    assert any("无法判定" in ln for ln in sandbox.logs), \
        "无法判定的面必须单独告警，不能静默"


def test_inspect_dormant_placeholder_tags_are_not_drift(sandbox):
    """休眠/占位/非台账 tag 不构成漂移（防误杀回归，审查修订 #1）。"""
    sandbox.disk["slot-1"] = set(REAL) | {"dormant-x", "placeholder-y"}
    sandbox.rt["slot-1"] = set(REAL) | {"proxy"}
    rep = daemon._config_consistency_check()
    face = _face(rep, "slot-1")
    assert face["verdict"] == "consistent"
    assert face["missing"] == [] and face["extra"] == []
    assert rep["problems"] == 0
    assert rep["selector_unverifiable"] == []


# ── T8：异常隔离不掀翻服务 ─────────────────────────────────────────────

def test_selfcheck_isolated_when_compare_members_raises(sandbox, monkeypatch):
    """compare_members 抛异常 → selfcheck_failed=True，绝不向外抛。

    对照事故先例（2026-11-11 17:22，restart counter 23，瘫 5 分钟）锁死
    「观测失败 = 少一条告警」的故障模型。本用例若向外抛异常会直接报错，
    即 pytest.raises 的反向断言。
    """
    def boom(*a, **k):
        raise RuntimeError("compare boom")

    monkeypatch.setattr(config_projection, "compare_members", boom)
    rep = daemon._config_consistency_check()
    assert rep.get("selfcheck_failed") is True
    assert rep.get("problems") == 0
    assert rep.get("error")
    assert any("配置自检本身出错（已忽略，不影响服务）" in ln
               for ln in sandbox.logs), "隔离必须留痕，禁止静默降级"
