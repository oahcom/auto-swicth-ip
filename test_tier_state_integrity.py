"""档位切换的状态台账正确性 —— 覆盖 2026-11-11 的三个真病灶。

三个病灶的共同点：都是「半开状态」—— 表面看是正常的（服务在跑、单元在
停、报表有输出），实际有一份数据是错的，而错误会**静默传播**。

① 停用侧不清观测（P1）
   `_reset_stale_after_tier` 只挂在解禁侧。停用侧的线，结论冻结在档位切换
   前的旧值上（实测 30 条 WG 线冻结 27~33 小时），报表把它显示成"运维
   停用"，掩盖了"这是旧值"。

② 档位对账排在状态表加载之前（P4，本轮新发现）
   `_tier_reconcile` 的解禁侧会清观测并落盘，落盘写的是**整个内存状态表**。
   状态表还没加载就落盘 = 把 N 条覆盖成 1 条。实测复现过。

③ `blocked_notes` 不在状态表白名单（P6，本轮新发现）
   写进盘上、读不回来。实测磁盘 156 个 tag 带 `blocked_notes` 的是 0 个。

另有一条观测代码的架构约束（F1）：
   自检函数曾因内部一个未定义名字抛 NameError，把整个 daemon 掀翻，
   systemd Restart=always 反复拉起 23 次。**观测代码不该有能力打垮服务。**

每个测试都写成「回滚修复即失败」的形式 —— 断言的是**行为**，不是实现。
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))


def _load_daemon(monkeypatch, tmp_path, state_lines=None):
    """加载 daemon 模块，状态表指向 tmp 下的假文件。

    WHY 要把 `daemon`/`config`/`warp_blocks` 从 sys.modules 里摘出来：
    daemon.py 顶层 import 有副作用，而这些模块在别的测试里已经被导入过
    （例如 test_warp_wg.py 会 `importlib.reload(config)`）。不摘的话，
    本测试用的可能是别的测试留下的**半初始化**模块。

    WHY 事后要还原：别的测试（如 test_warp_wg.py）用
    `importlib.reload(cfg_mod)` 来测「环境变量变化 → 配置重新生成」。
    reload 要求模块对象仍在 `sys.modules` 里 —— 我们这里把它 pop 掉了，
    如果不还原，那边的 reload 会抛
    `ImportError: module config not in sys.modules`。
    """
    state_path = tmp_path / "state.json"
    if state_lines is not None:
        state_path.write_text(json.dumps(
            {"version": 1, "lines": state_lines}), encoding="utf-8")
    monkeypatch.setenv("WARP_LINE_STATE_FILE_PATH", str(state_path))
    saved = {name: sys.modules.get(name)
             for name in ("daemon", "config", "warp_blocks")}
    for name in saved:
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("daemon", PROJECT / "daemon.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daemon"] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        # 加载失败也要还原 sys.modules —— 否则后面的测试全部被污染
        for name, val in saved.items():
            if val is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = val
        raise
    mod._tb_saved_modules = saved
    return mod, state_path


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """每个测试结束后把 sys.modules 还原到测试前的状态。

    WHY 需要：daemon.py 的顶层 import 会把 `config`/`warp_blocks` 注册进
    sys.modules。我们为了拿到干净的模块实例把它们 pop 掉了，但别的测试
    （test_warp_wg.py）依赖 `importlib.reload(config)` 能找到原模块。
    不还原就会让**与本次改动无关**的测试失败 —— 实测过：
    test_warp_wg.py 的 3 个测试因此报 ImportError。
    """
    before = {name: sys.modules.get(name)
              for name in ("daemon", "config", "warp_blocks")}
    yield
    for name, val in before.items():
        if val is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = val


# ---------------------------------------------------------------- ① 停用侧


def test_tier_stop_clears_frozen_conclusion(monkeypatch, tmp_path):
    """档位停用的线，结论类字段必须被清掉（不能冻结在旧值上）。

    回滚即失败：把 `_clear_conclusions_on_tier_stop` 的调用去掉，
    `last_verdict` 会保持 "blocked"，断言失败。
    """
    mod, state_path = _load_daemon(monkeypatch, tmp_path)
    mod._warp_line_state_locked("WARP-WG-9").update({
        "last_verdict": "blocked", "ip": "10.0.0.1", "fail_streak": 17,
        "last_probe_ts": 1000.0, "next_probe_ts": 2000.0,
    })
    mod._clear_conclusions_on_tier_stop("WARP-WG-9")
    st = mod._warp_line_state("WARP-WG-9")
    assert st["last_verdict"] == "", "档位停用后结论必须清空，不能冻结在旧值"
    assert st["ip"] is None, "旧会话的地址必须清掉"
    assert st["fail_streak"] == 0
    assert st["last_probe_ts"] == 0.0


def test_tier_stop_keeps_today_samples(monkeypatch, tmp_path):
    """停用侧清理**必须保留 `ban_probes`**。

    WHY：ban_probes 是当天的历史证据（这条线今天被判过多少次 quota），
    是渠道锁的判据原料。停用时抹掉等于毁掉当天的账。它按"当天"自动
    过期，不需要额外清理。

    回滚即失败：若实现里顺手清了 ban_probes，本测试失败。
    """
    mod, state_path = _load_daemon(monkeypatch, tmp_path)
    now = time.time()
    mod._warp_line_state_locked("WARP-WG-9").update({
        "ban_probes": [[now, now, 1, "quota"]],
        "last_verdict": "blocked",
    })
    mod._clear_conclusions_on_tier_stop("WARP-WG-9")
    st = mod._warp_line_state("WARP-WG-9")
    assert len(st["ban_probes"]) == 1, "当天的历史样本不能被档位清理抹掉"
    assert st["last_verdict"] == "", "结论类字段仍然必须清"


def test_tier_stop_keeps_restart_counters(monkeypatch, tmp_path):
    """停用侧清理**必须保留重启计数**（熔断计数）。

    WHY：penalty_restart_streak 是"这条线换过多少次地址"的事实，与档位
    无关。档位切换把它抹掉，等于重启计数器被悄悄复位。

    回滚即失败：若清理里带上了这两个字段，本测试失败。
    """
    mod, _ = _load_daemon(monkeypatch, tmp_path)
    mod._warp_line_state_locked("WARP-WG-9").update({
        "penalty_restart_streak": 1325, "futile_restart_streak": 7,
        "last_verdict": "blocked",
    })
    mod._clear_conclusions_on_tier_stop("WARP-WG-9")
    st = mod._warp_line_state("WARP-WG-9")
    assert st["penalty_restart_streak"] == 1325
    assert st["futile_restart_streak"] == 7
    assert st["last_verdict"] == ""


def test_tier_reconcile_stop_branch_invokes_cleanup(monkeypatch, tmp_path):
    """停用分支必须真的调清理（不只是清理函数存在）。

    回滚即失败：把停用分支里的 `_clear_conclusions_on_tier_stop(tag)`
    去掉，本测试失败 —— 这正是线上 30 条线冻结 27~33 小时的现场。
    """
    mod, _ = _load_daemon(monkeypatch, tmp_path)
    mod._warp_line_state_locked("WARP-WG-9").update({
        "last_verdict": "blocked", "ip": "10.0.0.1"})

    called = []
    monkeypatch.setattr(mod, "_clear_conclusions_on_tier_stop",
                        lambda tag: called.append(tag))
    monkeypatch.setattr(mod, "_unit_ctl", lambda *a, **k: type(
        "R", (), {"returncode": 0})())
    monkeypatch.setattr(mod, "_wg_line_state", lambda idx: "installed")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": "inactive", "returncode": 0})())
    # 造一个「档位外 + 在 members 里声明的 WG 线」
    monkeypatch.setattr(mod, "_TIER_DISABLED", {"WARP-WG-9"})
    monkeypatch.setattr(mod, "_warp", lambda: {"members": [
        {"tag": "WARP-WG-9", "kind": "wg", "service": "warp-wg-9.service",
         "index": 9}]})
    mod._tier_reconcile()
    assert "WARP-WG-9" in called, \
        "档位停用分支必须清观测 —— 原来只停单元，导致结论冻结几十小时"


# ------------------------------------------------- ② 整表覆盖的数据丢失


def test_save_refuses_to_overwrite_with_fewer_lines(monkeypatch, tmp_path):
    """落盘时若「内存条数 < 盘上条数」，必须拒绝覆盖。

    WHY：`_save_warp_line_state` 落的是整个内存状态表。状态表还没加载就
    落盘 = 把 N 条覆盖成 1 条，其余整条丢失，且**没有任何日志**。
    实测复现过：3 条假数据被覆盖成 1 条。

    回滚即失败：去掉条数保护，本测试失败。
    """
    mod, state_path = _load_daemon(monkeypatch, tmp_path, state_lines={
        "A": {"ip": "1.1.1.1"}, "B": {"ip": "2.2.2.2"}, "C": {"ip": "3.3.3.3"}})
    # 模拟「状态表未加载就调用清理」的现场：内存里只有 1 条
    mod._WARP_LINE_STATE.clear()
    mod._warp_line_state_locked("B")
    mod._save_warp_line_state(force=True)
    disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(disk["lines"]) == 3, \
        "内存条数变少时拒绝覆盖 —— 否则整张状态表会被冲掉"
    assert set(disk["lines"]) == {"A", "B", "C"}


def test_save_allows_equal_or_more_lines(monkeypatch, tmp_path):
    """条数保护不能误伤正常写入（条数相等或更多时必须放行）。

    WHY：档位停用侧清理会清掉 30 条线的结论 —— 如果这些线的条目被整体
    删掉，条数会变少。但我们是**清字段**不是**删条目**，所以条数不变，
    保护不会误触发。这个测试守住"保护不误伤"。

    回滚即失败：若保护写成 `>=` 或无条件拒绝，本测试失败。
    """
    mod, state_path = _load_daemon(monkeypatch, tmp_path, state_lines={
        "A": {"ip": "1.1.1.1"}, "B": {"ip": "2.2.2.2"}})
    mod._load_warp_line_state()
    mod._warp_line_state_locked("C")          # 内存 3 条 > 盘上 2 条
    mod._save_warp_line_state(force=True)
    disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(disk["lines"]) == 3, "条数增加时必须正常落盘"


def test_tier_reconcile_runs_after_state_load(monkeypatch, tmp_path):
    """main() 里的档位对账必须排在状态表加载**之后**。

    WHY：`_tier_reconcile` 的解禁侧会调 `_reset_stale_after_tier` →
    `_save_warp_line_state(force=True)`，落的是整个内存状态表。状态表
    没加载就落盘 = 整表被覆盖成 1 条。

    回滚即失败：把 `_tier_reconcile()` 挪回 `_load_warp_line_state()`
    之前，本测试失败。
    """
    src = (PROJECT / "daemon.py").read_text(encoding="utf-8")
    main_body = src[src.index("def main():"):]
    i_load = main_body.index("_load_warp_line_state()")
    i_tier = main_body.index("_tier_reconcile()")
    assert i_tier > i_load, \
        "档位对账必须在状态表加载之后 —— 否则解禁侧落盘会覆盖整张状态表"


# ------------------------------------------------- ③ blocked_notes 持久化


def test_blocked_notes_is_declared_in_state_defaults():
    """`blocked_notes` 必须声明进 `_LINE_DEFAULTS`。

    WHY：`_load_warp_line_state` **只恢复本表声明过的键**。漏声明 =
    写进盘上、读不回来。实测 2026-11-11：磁盘 156 个 tag，带
    `blocked_notes` 的是 0 个 —— 报表永远看不到这些观测。
    这与 `penalty_restart_streak` 是同一个坑（那里的注释已专门警告过）。

    回滚即失败：从 `_LINE_DEFAULTS` 删掉该键，本测试失败。
    """
    spec = importlib.util.spec_from_file_location("daemon_defaults_probe",
                                                  PROJECT / "daemon.py")
    src = PROJECT.joinpath("daemon.py").read_text(encoding="utf-8")
    assert '"blocked_notes"' in src, "blocked_notes 必须被声明"
    # 更强的断言：它得在 _LINE_DEFAULTS 这个字典字面量里，而不是别处
    start = src.index("_LINE_DEFAULTS = {")
    end = src.index("\n}", start)
    assert '"blocked_notes"' in src[start:end], \
        "blocked_notes 必须在 _LINE_DEFAULTS 字典里，否则读回时被过滤掉"


def test_blocked_notes_survives_reload(monkeypatch, tmp_path):
    """写了 `blocked_notes` 的线，重启后必须还能读到。

    回滚即失败：把 `blocked_notes` 从 `_LINE_DEFAULTS` 删掉，本测试失败。
    """
    mod, state_path = _load_daemon(monkeypatch, tmp_path)
    now = time.time()
    mod._warp_line_state_locked("WARP-H2-NRT-0").update(
        {"blocked_notes": [[now, now, 1, "blocked"]]})
    mod._save_warp_line_state(force=True)

    # 重新加载：模拟 daemon 重启
    mod2, _ = _load_daemon(monkeypatch, tmp_path)
    mod2._load_warp_line_state()
    st = mod2._warp_line_state("WARP-H2-NRT-0")
    assert len(st.get("blocked_notes") or []) == 1, \
        "blocked_notes 写进盘上就必须读得回来 —— 否则报表永远看不到它"


def test_blocked_notes_default_is_none_not_shared_list(monkeypatch, tmp_path):
    """默认值必须是 None 而不是 [] —— 共享对象坑。

    WHY：`_LINE_DEFAULTS` 是模块级共享对象，`setdefault` 会让所有渠道
    拿到**同一个** list。那么一条线的样本会记进另一条线的窗口。
    `ban_probes` 当年就踩过这个坑（它的默认也是 None）。

    回滚即失败：默认值改成 []，本测试失败。
    """
    mod, _ = _load_daemon(monkeypatch, tmp_path)
    a = mod._warp_line_state_locked("AAA")
    b = mod._warp_line_state_locked("BBB")
    assert a.get("blocked_notes") is None, "默认必须是 None，不能是共享的 list"
    assert b.get("blocked_notes") is None
    # 写入一条后，另一条不能被污染
    a["blocked_notes"] = [[1.0, 1.0, 1, "blocked"]]
    assert b.get("blocked_notes") is None, \
        "一条线的样本不能出现在另一条线的窗口里"


# ------------------------------------------- 观测代码不得有能力打垮服务


def test_consistency_check_swallows_internal_error(monkeypatch, tmp_path):
    """自检内部出错时必须被吞掉，返回空报告而**不抛异常**。

    WHY（真实事故）：2026-11-11 17:22:31，自检内部一个未定义名字
    `PROJECT` 抛 NameError，异常一路冒泡到 main() → 进程退出 → systemd
    `Restart=always` + `RestartSec=10` 反复拉起 → **restart counter 到
    23**，服务瘫了 5 分钟。

    WHY 必须隔离而不是"小心别写错"：这段代码会被反复修改（每发现一类
    不一致就要加分类），而**一个观测函数不该有能力掀翻服务**。

    回滚即失败：去掉异常隔离壳，本测试失败。
    """
    mod, _ = _load_daemon(monkeypatch, tmp_path)

    def boom(now=None):
        raise NameError("name 'PROJECT' is not defined")

    monkeypatch.setattr(mod, "_config_consistency_check_impl", boom)
    report = mod._config_consistency_check()
    assert report.get("problems") == 0
    assert report.get("selfcheck_failed") is True, \
        "自检失败必须显式标记 —— 否则「没查成」和「查了没问题」长得一样"


def test_consistency_check_impl_is_the_real_body(monkeypatch, tmp_path):
    """壳必须真的包住了实现（不是把实现删了改成一个永远返回空的东西）。

    WHY：只测"不抛异常"是不够的 —— 一个永远返回空的函数也满足那个断言，
    但自检就永远不会报告任何问题了。这个测试守住"实现还在、还在干活"。

    回滚即失败：若把实现删掉只留壳，本测试失败。
    """
    mod, _ = _load_daemon(monkeypatch, tmp_path)
    assert hasattr(mod, "_config_consistency_check_impl"), \
        "实现必须单独成函数，好让壳包住它"
    # 实现自己跑一遍（不注入异常）应该能返回带分类的报告
    rep = mod._config_consistency_check_impl(time.time())
    assert "problems" in rep
    assert "members" in rep and "outbounds" in rep and "state" in rep


def test_consistency_check_not_on_pre_probe_startup_path():
    """启动时的自检调用必须排在探测线程启动**之后**。

    WHY：即使有异常隔离壳，自检跑在启动关键路径上仍然会拖慢启动、
    占用启动窗口。把自检放到探测线程起来之后，观测失败对服务可用性
    的影响降到最低。

    回滚即失败：把 `_config_consistency_check()` 挪回 `_start_warp_prober()`
    之前，本测试失败。
    """
    src = (PROJECT / "daemon.py").read_text(encoding="utf-8")
    main_body = src[src.index("def main():"):]
    i_probe = main_body.index("_start_warp_prober()")
    i_check = main_body.index("_config_consistency_check()")
    assert i_check > i_probe, \
        "启动自检必须排在探测线程启动之后 —— 观测代码不该在关键路径上"


# ------------------------------- ④ 审查第二轮：台账外残留 / 热载 mtime 顺序


def test_tier_reconcile_clears_orphan_wg_conclusions(monkeypatch, tmp_path):
    """台账外 WG 线（补位池 23+ / 缩容遗留）的冻结结论必须被清掉。

    实测现场（2026-11-11 审计 + .warp_line_state.json）：WARP-WG-23..30
    的 last_verdict 停在 31~36 小时前，报表把旧值显示成「运维停用」。
    旧 `_tier_reconcile` 只遍历 members，这些线永远遍历不到 —— 停用侧
    清理函数写得再对也覆盖不到它们。
    """
    mod, state_path = _load_daemon(monkeypatch, tmp_path, state_lines={
        "WARP-WG-23": {"last_verdict": "OK", "last_probe_ts": 100.0,
                       "ip": "1.2.3.4", "ban_probes": [[time.time(), "quota"]]},
        "WARP-WG-26": {"last_verdict": "blocked", "last_probe_ts": 200.0,
                       "ip": None},
        "WARP-WG-1": {"last_verdict": "OK", "last_probe_ts": 300.0},
    })
    mod._load_warp_line_state()
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_warp", lambda: {"members": [
        {"tag": "WARP-WG-1", "kind": "wg",
         "service": "warp-wg-1.service", "index": 1}]})
    # 无档位文件 → 对账循环直接 return，只验证「台账外清理」这一段
    monkeypatch.setattr(mod.warp_blocks, "tier_spec", lambda: (None, None))

    mod._tier_reconcile()

    for tag in ("WARP-WG-23", "WARP-WG-26"):
        st = mod._warp_line_state_locked(tag)
        assert st["last_verdict"] == ""
        assert st["last_probe_ts"] == 0.0
        assert st["ip"] is None
    assert len(mod._warp_line_state_locked("WARP-WG-23")["ban_probes"]) == 1, \
        "当天的历史证据不许一起抹掉"
    assert mod._warp_line_state_locked("WARP-WG-1")["last_verdict"] == "OK", \
        "在册的线不该被误清"
    disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert disk["lines"]["WARP-WG-23"]["last_verdict"] == "", \
        "清理必须落盘，不能只在内存里生效"


def test_h2_reload_does_not_advance_mtime_on_read_failure(monkeypatch, tmp_path):
    """读台账失败必须下轮重试：旧版先推进 mtime，一次瞬态把变更永久吞掉。"""
    mod, _ = _load_daemon(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    mod._H2_CHANNELS_MTIME = 1.0
    monkeypatch.setattr(mod, "_h2_channels_mtime", lambda: 2.0)
    monkeypatch.setattr(mod, "_h2_members_from_ledger", lambda: [])

    assert mod._refresh_h2_members() is False
    assert mod._H2_CHANNELS_MTIME == 1.0, "读失败不许推进 mtime"

    monkeypatch.setattr(mod, "_h2_members_from_ledger", lambda: [
        {"tag": "WARP-H2-NRT-1", "kind": "h2", "port": 40001,
         "service": "usque-h2-1.service", "index": 1}])
    monkeypatch.setattr(mod, "_warp", lambda: {"members": []})
    monkeypatch.setattr(mod, "_drop_orphan_scan_state", lambda: 0)
    assert mod._refresh_h2_members() is True, "下轮必须能重试成功"
    assert mod._H2_CHANNELS_MTIME == 2.0


def test_tier_reload_keeps_state_on_bad_ledger(monkeypatch, tmp_path):
    """档位台账读不出来时保持现状：空集合 = 把 30 条停用线全部解禁。"""
    mod, _ = _load_daemon(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    mod._TIER_MTIME = 1.0
    mod._TIER_DISABLED = {"WARP-WG-9"}
    monkeypatch.setattr(mod, "_tier_mtime", lambda: 2.0)
    monkeypatch.setattr(mod.warp_blocks, "tier_spec", lambda: (None, None))

    assert mod._refresh_tier_disabled() is False
    assert mod._TIER_MTIME == 1.0
    assert mod._TIER_DISABLED == {"WARP-WG-9"}, \
        "坏台账不许把停用线批量解禁"
