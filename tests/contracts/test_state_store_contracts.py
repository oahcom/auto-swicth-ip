"""状态权威（P3）契约测试：schema 不变量 / 库落在本地盘 / 白名单 / 投影回滚。

【防什么事故】阶段 2 的全部护栏（附件 A3/A4/A7 + 附件 C 全表）现在要换成
「库 + schema 不变量」的形式。换存储那次改动如果漏掉其中任何一条，事故不是
「库不好用」，而是**脏地址被当干净地址复用**：
- 空表/缩水拒写漏了 → 并发测试进程或误启动实例把库抹空（已实际发生）；
- ban_until 保护漏了 → 清理把仍在惩罚期的地址删掉，刚封的地址立刻回池再烧；
- 白名单漏了 → 未声明字段静默丢键（熔断清零事故：16 连重启 152 条线归零）；
- 库落在 9p → WAL 的锁/共享内存不可靠，回滚点本身不可信；
- schema 版本不单调 → 旧代码按新库解释，静默丢列。

【三分法】全部 A 类保真契约（护栏的等价物）。每条用例的 docstring 写明它对应
哪一条旧护栏 / 防哪一起事故。新增 kind 或改不变量必须同步改本文件。
"""
import json
import os
import sqlite3

import pytest

from asip.state_store import mgmt, schema, shadow, writers
from asip.state_store.store import (Store, StoreError, fstype_from_mounts,
                                    is_remote_fs)


@pytest.fixture
def store(tmp_path):
    st = Store(str(tmp_path / "state.sqlite3"))
    yield st
    st.close()


# ── INV-EMPTY / INV-SHRINK（附件 A3） ──────────────────────────────
def test_empty_write_refused_when_db_nonempty(store):
    """空表拒写：库内非空、内存为空、未放行 → 拒写并保持库内原样。

    防什么事故：并发测试进程/误启动实例用空内存覆盖盘上 16h 的 ban 台账
    （已实际发生）；抹掉后切回烧掉的 IP = 双实例互抹事故。
    """
    store.replace_rows(schema.KIND_IP_USED,
                       {"2001:db8::1": {"ts": 1.0, "blocked": True,
                                        "ban_until": 9e9, "ban_kind": "rate429"}})
    res = store.replace_rows(schema.KIND_IP_USED, {})
    assert res["written"] is False
    assert res["refused"] == "INV-EMPTY"
    assert store.count_rows(schema.KIND_IP_USED) == 1


def test_empty_write_allowed_once_with_flag(store):
    """显式放行一次写空（清理逻辑的逃生口）；放行后仍要显式再放行。"""
    store.replace_rows(schema.KIND_IP_USED, {"a": {"ts": 1.0}})
    assert store.replace_rows(schema.KIND_IP_USED, {},
                              allow_empty=True)["written"] is True
    assert store.count_rows(schema.KIND_IP_USED) == 0
    # 库现在为空 → 再空写不触发 INV-EMPTY（无数据可抹）
    assert store.replace_rows(schema.KIND_IP_USED, {})["written"] is True


def test_shrink_refused(store):
    """条数缩水拒写：库内 3 条、内存 1 条 → 拒绝覆盖。

    防什么事故：内存表未加载就落盘，N 条写成 1 条（2026-11-11 实测 3→1，
    且无任何日志）。这条兜底挡的是「整表覆盖」这类写法的所有未来变体。
    """
    store.replace_rows(schema.KIND_LINE_STATE,
                       {"T1": {"ip": "a"}, "T2": {"ip": "b"}, "T3": {"ip": "c"}})
    res = store.replace_rows(schema.KIND_LINE_STATE, {"T1": {"ip": "a"}})
    assert res["refused"] == "INV-SHRINK"
    assert store.count_rows(schema.KIND_LINE_STATE) == 3


def test_equal_or_grow_writes(store):
    """护栏不误伤正常写入（条数不缩水时照写）。"""
    store.replace_rows(schema.KIND_LINE_STATE, {"T1": {"ip": "a"}})
    assert store.replace_rows(schema.KIND_LINE_STATE,
                              {"T1": {"ip": "a"}, "T2": {"ip": "b"}})["written"]


# ── INV-BAN（附件 A1 / C：ban_until 保护） ─────────────────────────
def test_delete_protects_active_ban(store):
    """ban_until > now 的行删不掉：清理不得把仍在惩罚期的地址放出池。

    防什么事故：清理谓词用错窗口/时钟，把 ban_until 还有 11 小时的地址删掉，
    下一发请求立刻重新烧同一个地址（「刚封地址 60s 回池再烧」家族）。
    """
    now = 1_000_000.0
    store.replace_rows(schema.KIND_IP_USED, {
        "banned": {"ts": now - 10, "blocked": True, "blocked_kind": "rate429",
                   "ban_until": now + 3600, "ban_kind": "rate429"},
        "clean": {"ts": now - 10, "blocked": False},
    })
    res = store.delete_rows(schema.KIND_IP_USED, ["banned", "clean"], now=now)
    assert res["blocked"] == ["banned"]
    assert res["removed"] == ["clean"]
    assert store.count_rows(schema.KIND_IP_USED) == 1


def test_delete_allows_expired_ban_and_bad_kind(store):
    """过期封禁可删；ban_kind 非 rate429 的 ban_until 不构成保护（与
    daemon._warp_ban_until 同口径：坏值只丢精确封禁，不挡正常清理）。"""
    now = 1_000_000.0
    store.replace_rows(schema.KIND_IP_USED, {
        "expired": {"ban_until": now - 1, "ban_kind": "rate429"},
        "wrong_kind": {"ban_until": now + 3600, "ban_kind": "abuse403"},
        "bad_value": {"ban_until": "not-a-number", "ban_kind": "rate429"},
    })
    res = store.delete_rows(schema.KIND_IP_USED,
                            ["expired", "wrong_kind", "bad_value"], now=now)
    assert res["removed"] == ["expired", "wrong_kind", "bad_value"]
    assert res["blocked"] == []


def test_protected_by_ban_rejects_nan_and_inf():
    """坏值（NaN/Inf/非数值）不构成保护，也不能让判定抛异常。

    数字字符串 **算有效**：`daemon._as_ts` / `ledger_policy._as_ts` 都接受
    float("9e9")，手工编辑/旧版本写出的字符串时间戳是真实存在的形态。这里若
    比 daemon 更严格，清理就会比 daemon 多删 —— 「刚封地址回池再烧」正是这么来的。
    """
    assert schema.protected_by_ban({"ban_until": float("nan"),
                                    "ban_kind": "rate429"}, 0.0) is False
    assert schema.protected_by_ban({"ban_until": float("inf"),
                                    "ban_kind": "rate429"}, 0.0) is False
    assert schema.protected_by_ban({"ban_until": "not-a-number",
                                    "ban_kind": "rate429"}, 0.0) is False
    assert schema.protected_by_ban({"ban_until": "9e9",
                                    "ban_kind": "rate429"}, 0.0) is True
    assert schema.protected_by_ban(None, 0.0) is False


# ── INV-WHITELIST（附件 A4） ───────────────────────────────────────
def test_whitelist_matches_daemon_line_defaults():
    """白名单字段集必须与 daemon._LINE_DEFAULTS 逐字相等。

    防什么事故：一侧新增字段、另一侧没跟上 → 「写进盘上读不回来」
    （blocked_notes 事故：磁盘 156 个 tag 带该键的是 0 个）。
    """
    import daemon as d
    assert schema.LINE_STATE_FIELDS == frozenset(d._LINE_DEFAULTS)
    # 唯一的 list 默认是 restart_hist：`_LINE_DEFAULTS` 是共享对象，多个 list
    # 默认值会被 setdefault 串到所有渠道上（ban_probes 事故的形态）。
    assert [k for k, v in d._LINE_DEFAULTS.items() if isinstance(v, list)] == [
        "restart_hist"]


def test_read_filters_undeclared_fields(store):
    """读只返回声明字段（与 daemon 载入语义逐字一致）。"""
    store.replace_rows(schema.KIND_LINE_STATE, {"T1": {
        "ip": "keep", "penalty_restart_streak": 3, "quota_until": 999.0}})
    rows = store.read_rows(schema.KIND_LINE_STATE)
    assert rows["T1"] == {"ip": "keep", "penalty_restart_streak": 3}
    assert "quota_until" not in rows["T1"]


def test_raw_keeps_undeclared_fields(store):
    """原文保管：未声明字段不删（比现状「丢了就没了」更保守）。

    INV-UNMIGRATED 的同族要求：不确定的字段宁可留着 + 计数，也不静默丢。
    """
    store.replace_rows(schema.KIND_LINE_STATE, {"T1": {
        "ip": "keep", "quota_until": 999.0, "egress_error": "x"}})
    raw = store.read_raw(schema.KIND_LINE_STATE)
    assert raw["T1"]["quota_until"] == 999.0
    assert raw["T1"]["egress_error"] == "x"


def test_filter_fields_returns_extras():
    """filter_fields 把未声明残留**交出来**而不是丢掉。"""
    kept, extra = schema.filter_fields(schema.KIND_LINE_STATE,
                                       {"ip": "a", "mystery": 1})
    assert kept == {"ip": "a"}
    assert extra == {"mystery": 1}


# ── INV-RATIO（附件 A7） ──────────────────────────────────────────
def test_ratio_gate_matches_daemon_predicate():
    """占比阀与 daemon 的 `dead > total*(1-_POOL_MIN_RATIO)` 同判。

    防什么事故：Clash API 抖动 → 全池判死 → 导出把整个池封死（误杀至
    dead=50/56 那族）。取值必须来自 daemon 的常量，不在本模块重新发明比例。
    """
    import daemon as d
    min_ratio = d._POOL_MIN_RATIO
    for total in (10, 56, 93, 122):
        max_dead = int(total * (1 - min_ratio))
        assert schema.ratio_gate(max_dead, total, min_ratio) is True
        assert schema.ratio_gate(max_dead + 1, total, min_ratio) is False
    assert schema.ratio_gate(0, 0, min_ratio) is False  # total=0 由调用方先跳过


# ── 库落 Linux 本地盘（验收项「mount 证据」的机器形态） ────────────
def test_remote_fs_detected_from_mounts():
    """9p / cifs / virtiofs 判为远端；ext4 判为本地。"""
    mounts = ("/dev/sdd / ext4 rw 0 0\n"
              "C:\\ /mnt/c 9p rw 0 0\n"
              "\\\\srv\\share /mnt/s cifs rw 0 0\n"
              "none /mnt/virtio virtiofs rw 0 0\n")
    assert fstype_from_mounts(mounts, "/home/u/proj/run/x.sqlite3") == "ext4"
    assert fstype_from_mounts(mounts, "/mnt/c/proj/x.sqlite3") == "9p"
    assert is_remote_fs("9p") and is_remote_fs("cifs")
    assert is_remote_fs("virtiofs")
    assert not is_remote_fs("ext4") and not is_remote_fs("tmpfs")
    assert not is_remote_fs(None)


def test_store_refuses_remote_fs(tmp_path, monkeypatch):
    """库落在远端文件系统上直接拒开（WAL 不可靠 → 回滚点不可信）。"""
    monkeypatch.setattr("asip.state_store.store.current_fstype",
                        lambda path: "9p")
    with pytest.raises(StoreError):
        Store(str(tmp_path / "x.sqlite3"))


def test_live_state_dir_is_local():
    """live 现场自查：项目目录必须不在远端挂载上（验收：库在 Linux 本地盘）。

    这条不是形式主义：如果把 PROJECT_DIR 指到 /mnt/c（WSL 的 Windows 盘），
    库会在第一次写事务时把 WAL 建在 9p 上，之后所有「回滚证据」都不可信。
    """
    from asip.state_store.store import current_fstype
    import config
    fs = current_fstype(config.CFG["state_store"]["db_path"])
    if fs is None:                     # 非 Linux（Windows 侧只读跑测试）
        pytest.skip("无 /proc/mounts")
    assert not is_remote_fs(fs), "状态库落在 %s 上" % fs


# ── INV-ORDER / 迁移器 ────────────────────────────────────────────
def test_newer_schema_refused(tmp_path):
    """库 schema 版本高于代码 → 拒开（不能按旧 schema 解释新库）。"""
    path = str(tmp_path / "s.sqlite3")
    st = Store(path)
    st._meta_set("schema_version", str(schema.SCHEMA_VERSION + 5))
    st.close()
    with pytest.raises(StoreError):
        Store(path)


def test_migrate_from_zero_reaches_current(store):
    """空库（version=0）打开后自动升到 SCHEMA_VERSION 并记账。"""
    assert store.schema_version() == schema.SCHEMA_VERSION
    assert store._meta_get("migrated_%d" % schema.SCHEMA_VERSION)


def test_wal_mode_enabled(store):
    """WAL 生效（增量写 + 崩溃可回放的前提）。"""
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


# ── 投影回滚契约（接 deploy/state-projection.py 封套） ──────────────
def test_export_projection_envelope_and_shape(store, tmp_path):
    """投影封套与 state-projection.py 一致，且 data 是**旧代码读得懂的原文**。

    防什么事故：回滚点导出来的是库自己的形状（表/列），老 daemon 读不懂 ——
    那就不叫回滚点，叫「另存为」。这里钉死三件事：schema 字符串、source.type
    = sqlite、逐条 sha256/size 自证。
    """
    import hashlib
    store.replace_rows(schema.KIND_IP_USED, {"2001:db8::1": {"ts": 1.5,
                                                            "blocked": False}})
    store.replace_rows(schema.KIND_LINE_STATE,
                       {"T1": {"ip": "a", "penalty_restart_streak": 2}})
    store.write_doc(schema.KIND_API_STATS, {"calls": 7})
    out = str(tmp_path / "proj.json")
    info = store.export_projection(out)
    proj = json.load(open(out, encoding="utf-8"))
    assert proj["schema"] == "asip-state-projection/1"
    assert proj["source"]["type"] == "sqlite"
    assert proj["source"]["schema_version"] == schema.SCHEMA_VERSION
    names = [e["name"] for e in proj["entries"]]
    assert ".warp_ip_used.json" in names and ".api_stats.json" in names
    for e in proj["entries"]:
        data = e["data"].encode("utf-8")
        assert hashlib.sha256(data).hexdigest() == e["sha256"]
        assert len(data) == e["size"]
    used = json.loads(next(e["data"] for e in proj["entries"]
                           if e["name"] == ".warp_ip_used.json"))
    assert used == {"used": {"2001:db8::1": {"ts": 1.5, "blocked": False}}}
    lines = json.loads(next(e["data"] for e in proj["entries"]
                            if e["name"] == ".warp_line_state.json"))
    assert lines["version"] == 1 and lines["lines"]["T1"]["ip"] == "a"
    assert info["entries"] == len(proj["entries"])


def test_export_projection_is_readback_compatible(store, tmp_path):
    """导出的投影能被 deploy/state-projection.py 的 freeze/readback 直接吃掉。

    这是「只增数据源、不动封套」的机器证明：不 import 那个脚本的业务函数，
    只跑 CLI 三步（export 已由库给出 → freeze → readback --verify-only）。
    """
    import subprocess
    import sys

    store.replace_rows(schema.KIND_BAD_NODES, {"n1": {"expire": 9e9,
                                                     "reason": "daemon_dead"}})
    proj = str(tmp_path / "p.json")
    store.export_projection(proj)
    tool = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "deploy",
        "state-projection.py")
    r1 = subprocess.run([sys.executable, tool, "freeze", proj],
                        capture_output=True, text=True, timeout=60)
    assert r1.returncode == 0, r1.stderr
    r2 = subprocess.run([sys.executable, tool, "readback", proj,
                         "--verify-only"], capture_output=True, text=True,
                        timeout=60)
    assert r2.returncode == 0, r2.stderr


# ── 影子对账分型（T2b） ───────────────────────────────────────────
def test_writer_classification():
    """写者分型：bad_nodes 是 SHARED（未收编 cron 前不影子），其余单写者可影子。

    防什么事故：两写者状态也纳入 diff → 差异无法归因（可能是另一个写者刚写
    一半），噪声把观测价值打没（ledger_policy.shadow_bucket 分人群同一教训）。
    """
    assert shadow.writer_type(schema.KIND_BAD_NODES) == shadow.SHARED
    assert schema.KIND_BAD_NODES not in shadow.SHADOWABLE
    assert schema.KIND_IP_USED in shadow.SHADOWABLE
    assert schema.KIND_LINE_STATE in shadow.SHADOWABLE
    assert shadow.writer_type("never-registered") == shadow.SIGNAL


def test_diff_rows_reports_only_memory_db_changed():
    """diff 逐字段可比，未声明字段不参与（差异=0 精确等于「读语义一致」）。"""
    mem = {"a": {"ip": "x", "quota_until": 1.0}, "b": {"ip": "y"}}
    db = {"a": {"ip": "x"}, "c": {"ip": "z"}}
    rep = shadow.diff_rows(schema.KIND_LINE_STATE, mem, db)
    assert rep["only_memory"] == ["b"]
    assert rep["only_db"] == ["c"]
    assert rep["diffs"] == 2            # quota_until 未声明 → 不算差异
    assert rep["changed"] == []


def test_diff_rows_detects_field_change():
    rep = shadow.diff_rows(schema.KIND_LINE_STATE,
                           {"a": {"ip": "x", "fail_streak": 3}},
                           {"a": {"ip": "x", "fail_streak": 4}})
    assert rep["diffs"] == 1
    assert rep["changed"][0]["fields"] == {"fail_streak": [3, 4]}


def test_shadow_runner_records_and_alerts(store):
    """影子 tick 落痕；有差异时发 observation_drift（不新造类目）。"""
    events = []

    class _A:
        def emit(self, category, message, **kw):
            events.append((category, message, kw))

    store.replace_rows(schema.KIND_IP_USED, {"a": {"ts": 1.0}})
    runner = shadow.ShadowRunner(
        store, lambda: {schema.KIND_IP_USED: {"a": {"ts": 1.0},
                                              "b": {"ts": 2.0}}},
        alert=_A(), interval_sec=0.0)
    rep = runner.tick(force=True)
    assert rep["total_diffs"] == 1
    assert events and events[0][0] == "observation_drift"
    hist = store.shadow_history(schema.KIND_IP_USED)
    assert hist and hist[0]["diffs"] == 1


def test_shadow_window_clean_requires_samples(store):
    """切读门槛：观察窗内**有样本且全 0** 才算干净；无样本不算干净。"""
    now = 1000.0
    assert store.shadow_window_clean(schema.KIND_IP_USED, 0.0)["clean"] is False
    store.record_shadow(schema.KIND_IP_USED, 10, 0, [], ts=now)
    store.record_shadow(schema.KIND_IP_USED, 10, 0, [], ts=now + 60)
    assert store.shadow_window_clean(schema.KIND_IP_USED, 0.0)["clean"] is True
    store.record_shadow(schema.KIND_IP_USED, 10, 2, [], ts=now + 120)
    assert store.shadow_window_clean(schema.KIND_IP_USED, 0.0)["clean"] is False


# ── 管理面三件套 ──────────────────────────────────────────────────
def test_restart_dry_run_grades():
    """restart --dry-run：分级结论取最坏档；未登记类型按烧地址档（fail-closed）。

    防什么事故：把「重启隧道」当运维动作随手执行 —— 重启 WARP 线 = 换出口 IP
    = 当前地址剩余额度作废。dry-run 的价值是让人在执行前看见这一条。
    """
    out = mgmt.restart_dry_run([{"target": "daemon", "kind": "daemon"},
                                {"target": "slot-1", "kind": "slot"}])
    assert out["verdict"] == "connection_drop"
    assert out["burns_address_count"] == 0
    assert out["requires_explicit_approval"] is False

    out2 = mgmt.restart_dry_run([("usque-h2-3", "line"), ("x", "???")])
    assert out2["verdict"] == "burns_address"
    assert out2["burns_address_count"] == 2
    assert out2["requires_explicit_approval"] is True
    assert all(t["grade_name"] in ("burns_address",) for t in out2["targets"])


def test_restart_dry_run_budget():
    """预算按「烧地址」目标计数，安全/断连档不占预算。"""
    out = mgmt.restart_dry_run([("a", "line")],
                               budget={"limit": 1, "used": 1})
    assert out["budget"] == {"limit": 1, "used": 1, "need": 1, "ok": False}
    out2 = mgmt.restart_dry_run([("a", "daemon")],
                                budget={"limit": 1, "used": 1})
    assert out2["budget"]["need"] == 0 and out2["budget"]["ok"] is True


def test_unban_transform_clears_all_ban_predicates():
    """解封必须清掉 _warp_ip_is_blocked 的**全部**判据，漏一个等于没解封。"""
    now = 1_000_000.0
    ttl = {"abuse403": 21600.0, "rate429": 60.0, "legacy": 86400.0}
    entry = {"ts": now - 100, "blocked": True, "blocked_kind": "rate429",
             "blocked_ts": now - 50, "blocked_ts_by_kind": {"rate429": now - 50},
             "ban_until": now + 3600, "ban_kind": "rate429",
             "expires_at": now + 7200, "served_sec": 42.0, "first_seen": 1.0}
    tf = mgmt.unban_transform(entry, now=now, ttl=ttl, retention_sec=86400.0)
    after = tf["after"]
    assert after["blocked"] is False
    for key in ("blocked_kind", "blocked_ts", "blocked_ts_by_kind",
                "ban_until", "ban_kind"):
        assert key not in after
    assert tf["was_blocked"] is True
    # 历史事实不动
    assert after["served_sec"] == 42.0 and after["first_seen"] == 1.0
    assert after["ts"] == entry["ts"]
    # expires_at 单调不减（INV-3）
    assert after["expires_at"] >= entry["expires_at"]


def test_unban_transform_is_effective_per_ledger_policy():
    """解封后用 ledger_policy 重算：确实不再处于惩罚期（语义闭环）。"""
    from asip import ledger_policy
    now = 1_000_000.0
    ttl = {"abuse403": 21600.0, "rate429": 60.0, "legacy": 86400.0}
    entry = {"ts": now - 100, "blocked": True, "blocked_kind": "abuse403",
             "blocked_ts": now - 50, "ban_until": now + 21600,
             "ban_kind": "rate429"}
    tf = mgmt.unban_transform(entry, now=now, ttl=ttl, retention_sec=86400.0)
    assert ledger_policy.is_effectively_blocked(tf["after"], now, ttl) is False


def test_unban_plan_refuses_unknown_and_clean(store):
    """管理面必须能拒绝：地址不在台账 / 本来就没被封 → 不动，并说明原因。"""
    store.replace_rows(schema.KIND_IP_USED, {"clean": {"ts": 1.0,
                                                       "blocked": False}})
    miss = mgmt.unban_plan(store, "nope")
    assert miss["ok"] is False and miss["found"] is False
    clean = mgmt.unban_plan(store, "clean")
    assert clean["ok"] is False and "不在惩罚期" in clean["reason"]


def test_unban_plan_produces_audit_row(store):
    """解封计划带审计行（谁/何时/为什么/清了哪些字段）——手改 JSON 没有这条。"""
    now = 1_000_000.0
    store.replace_rows(schema.KIND_IP_USED, {"b": {
        "ts": now - 10, "blocked": True, "blocked_kind": "rate429",
        "blocked_ts": now - 5, "ban_until": now + 600, "ban_kind": "rate429"}})
    plan = mgmt.unban_plan(store, "b", now=now, reason="上游已恢复",
                           actor="ops@host")
    assert plan["ok"] is True
    assert plan["audit"]["actor"] == "ops@host"
    assert plan["audit"]["reason"] == "上游已恢复"
    assert "ban_until" in plan["audit"]["cleared_fields"]
    # 只是计划：库没被改
    assert store.read_raw(schema.KIND_IP_USED)["b"]["blocked"] is True


def test_state_snapshot_keeps_health_contract(store):
    """状态快照复用 asip.health：行 7 字段（含 age_s）、顶层 8 字段不裁。"""
    from asip import health as health_mod
    now = 1_000_000.0
    probes = {
        health_mod.CHECK_HEALTH: lambda: health_mod.Check(
            "health", value="ok", ok=True, ts=now, ttl=60.0),
        health_mod.CHECK_QUEUE_DEPTH: lambda: ({"depth": 0}, now),
        health_mod.CHECK_INFLIGHT_COMMANDS: lambda: ({"count": 0}, now),
    }
    snap = mgmt.state_snapshot(store, probes, now=now, ttls={})
    assert set(snap) >= {"schema", "ts", "status", "checks", "missing",
                         "degraded_checks", "unknown_checks", "stale_checks"}
    assert set(snap["checks"][health_mod.CHECK_HEALTH]) == {
        "value", "ok", "ts", "age_s", "ttl", "stale", "detail"}
    assert health_mod.CHECK_LEDGER_HEADROOM in snap["checks"]  # 自动补台账余量
    assert snap["status"] in (health_mod.STATUS_OK, health_mod.STATUS_UNKNOWN,
                              health_mod.STATUS_DEGRADED)
    assert "store" in snap and "schema_version" in snap["store"]


# ── 事件表 = alerting 的 journal 注入点 ────────────────────────────
def test_event_journal_duck_type_and_fields(store):
    """Store 可直接作为 alerting 的 journal：事件九字段原样进表并可查回。"""
    from asip import alerting
    alerting.reset()
    disp = alerting.configure(journal=store, throttle_sec=0.0)
    try:
        disp.emit("ledger_anomaly", "缩水拒写", severity="error",
                  key="k", source="t", path="/x", on_disk=9, in_memory=1)
        rows = store.read_events(limit=5)
        assert len(rows) == 1
        ev = rows[0]
        assert ev["category"] == "ledger_anomaly"
        assert ev["severity"] == "error"
        assert ev["fields"] == {"path": "/x", "on_disk": 9, "in_memory": 1}
        assert set(schema.EVENT_FIELDS) <= set(ev)
    finally:
        alerting.reset()


def test_event_append_never_raises(store):
    """事件写不进去不许抛（alerting 三条硬约束之一：不递归告警、不掀翻调用链）。"""
    store.append({"ts": "not-a-ts"})          # 类型坏
    store.append({"category": "x", "fields": "not-a-dict"})
    store.append({})


# ── 共享写口（bad_nodes 收编 cron 写者） ───────────────────────────
def test_bad_nodes_append_only_keeps_foreign_entries(tmp_path):
    """daemon 侧 append-only：cron/他方条目不被旧快照覆盖（附件 A7）。"""
    target = str(tmp_path / ".bad_nodes.json")
    with open(target, "w", encoding="utf-8") as f:
        json.dump({"from-cron": {"expire": 999, "reason": "x"}}, f)
    out = writers.update_bad_nodes(target, {"from-daemon"}, expire_of=lambda n: 500,
                                   reason="daemon_dead", overwrite=False)
    assert set(out) == {"from-cron", "from-daemon"}
    assert out["from-cron"]["expire"] == 999     # 他方条目原样


def test_bad_nodes_overwrite_refreshes_expire(tmp_path):
    """cron 侧 overwrite：自己归因的节点刷新 expire（反复命中要续期）。"""
    target = str(tmp_path / ".bad_nodes.json")
    with open(target, "w", encoding="utf-8") as f:
        json.dump({"n1": {"expire": 100, "reason": "daemon_dead"}}, f)
    out = writers.update_bad_nodes(target, {"n1"}, expire_of=lambda n: 999,
                                   reason="egress_error_rate", overwrite=True)
    assert out["n1"] == {"expire": 999, "reason": "egress_error_rate"}


def test_bad_nodes_lock_path_is_single_source(tmp_path):
    """锁路径由本模块唯一计算，且与 daemon / cron 现状路径**逐字一致**。

    防什么事故：任一侧改锁文件名写法 → 锁分家 → 两写者互相覆盖
    （cron 刚写进的坏节点被 daemon 旧快照抹掉）。这里把「现状」钉成断言：
    daemon 用 `<file>.lock`，cron 用 `Path.with_suffix('.json.lock')` ——
    live 实测两者拼出同一路径，本模块必须保持不变。
    """
    from pathlib import Path
    target = str(tmp_path / ".bad_nodes.json")
    assert writers.lock_path_for(target) == target + ".lock"
    assert str(Path(target).with_suffix(".json.lock")) == target + ".lock"


def test_bad_nodes_update_shares_lock_across_writers(tmp_path):
    """两种语义共用同一把锁：并发 read-modify-write 不互相抹。"""
    target = str(tmp_path / ".bad_nodes.json")
    writers.update_bad_nodes(target, {"a"}, expire_of=lambda n: 1,
                             reason="daemon_dead", overwrite=False)
    writers.update_bad_nodes(target, {"b"}, expire_of=lambda n: 2,
                             reason="egress_error_rate", overwrite=True)
    data = json.load(open(target, encoding="utf-8"))
    assert set(data) == {"a", "b"}
    assert os.path.exists(writers.lock_path_for(target))


# ── 影子双写：库拒写不许改变调用链语义（T2b 硬约束①） ────────────────
def test_mirror_rows_refusal_is_evidence_not_exception(store):
    """库拒写（缩水）只上报，不抛 —— 影子阶段库不能反过来影响生产。"""
    from asip.state_store import bridge

    store.replace_rows(schema.KIND_IP_USED, {"a": {"ts": 1}, "b": {"ts": 2}})
    got = bridge.mirror_rows(store, schema.KIND_IP_USED, {"a": {"ts": 1}})
    assert got["refused"] == "INV-SHRINK"
    assert store.count_rows(schema.KIND_IP_USED) == 2   # 库内原样


def test_mirror_without_store_is_noop():
    """shadow 关闭 / 库未建 → 镜像整体跳过，调用方拿 skipped 而不是异常。"""
    from asip.state_store import bridge

    assert bridge.mirror_rows(None, schema.KIND_IP_USED, {})["skipped"] is True
    assert bridge.mirror_doc(None, schema.KIND_API_STATS, {})["skipped"] is True
    assert bridge.make_runner(None, lambda: {}) is None


def test_open_store_respects_shadow_flag_and_missing_db(tmp_path):
    """shadow 关 / 库文件缺失 → 不打开（daemon 自己从不建库）。"""
    from asip.state_store import bridge

    db = str(tmp_path / "state.sqlite3")
    assert bridge.open_store({"state_store": {"shadow": False,
                                              "db_path": db}}) is None
    assert not os.path.exists(db)
    assert bridge.open_store({"state_store": {"shadow": True,
                                              "db_path": db}}) is None
    assert not os.path.exists(db)
    Store(db).close()                                    # 迁移器建库
    st = bridge.open_store({"state_store": {"shadow": True, "db_path": db}})
    assert st is not None
    st.close()


# ── 影子 diff：文档式 volatile 字段不参与（否则观察窗永远不干净） ──────
def test_doc_diff_ignores_volatile_persisted_at():
    from asip.state_store import shadow

    rep = shadow.diff_doc(schema.KIND_API_STATS,
                          {"calls": 1, "persisted_at": 100.0},
                          {"calls": 1, "persisted_at": 999.0})
    assert rep["diffs"] == 0
    rep = shadow.diff_doc(schema.KIND_API_STATS,
                          {"calls": 1, "persisted_at": 100.0},
                          {"calls": 2, "persisted_at": 999.0})
    assert rep["diffs"] == 1


# ── 管理面：解封变换与副作用分级（纯函数，不落盘） ──────────────
def test_unban_transform_clears_every_block_field_and_keeps_history():
    """解封必须清掉 `_warp_ip_is_blocked` 的全部判据，且不改历史事实。"""
    now = 1000.0
    ttl = {"abuse403": 21600.0, "rate429": 60.0, "legacy": 86400.0}
    entry = {"ts": 10.0, "blocked": True, "blocked_kind": "rate429",
             "blocked_ts": 900.0, "blocked_ts_by_kind": {"rate429": 900.0},
             "ban_until": 2000.0, "ban_kind": "rate429",
             "expires_at": 5000.0, "served_sec": 42.0, "last_ok_ts": 800.0}
    tf = mgmt.unban_transform(entry, now=now, ttl=ttl, retention_sec=86400.0)
    assert tf["was_blocked"] is True
    after = tf["after"]
    for k in ("blocked_kind", "blocked_ts", "blocked_ts_by_kind",
              "ban_until", "ban_kind"):
        assert k not in after
    assert after["blocked"] is False
    assert after["ts"] == 10.0 and after["served_sec"] == 42.0
    assert after["expires_at"] >= 5000.0          # INV-3：只增不减


def test_unban_transform_never_shortens_expires_at():
    """解封不延长保留期，但也绝不缩短 —— 缩短等于提前把条目交给清理谓词。"""
    now = 1000.0
    ttl = {"abuse403": 21600.0, "rate429": 60.0, "legacy": 86400.0}
    entry = {"blocked": True, "blocked_kind": "rate429", "expires_at": 99999.0}
    tf = mgmt.unban_transform(entry, now=now, ttl=ttl, retention_sec=60.0)
    assert tf["after"]["expires_at"] == 99999.0


def test_restart_dry_run_grades_and_fail_closed():
    """未登记类型按最坏档；只要含换地址动作就必须显式批准。"""
    out = mgmt.restart_dry_run([("lineA", "line"), ("slot-1", "slot")])
    assert out["verdict"] == "burns_address"
    assert out["requires_explicit_approval"] is True
    assert out["burns_address_count"] == 1
    out = mgmt.restart_dry_run([("worker", "runtime")])
    assert out["verdict"] == "safe"
    assert out["requires_explicit_approval"] is False
    assert mgmt.restart_dry_run(
        [("x", "never-heard-of-it")])["verdict"] == "burns_address"


def test_restart_dry_run_budget_ignores_non_burning():
    out = mgmt.restart_dry_run([("a", "line")], budget={"limit": 1, "used": 1})
    assert out["budget"]["ok"] is False
    out = mgmt.restart_dry_run([("a", "slot")], budget={"limit": 1, "used": 1})
    assert out["budget"]["ok"] is True


def test_unban_plan_refuses_unknown_and_unblocked(store):
    """管理面必须能拒绝：不存在的地址、不在惩罚期的地址都不动。"""
    store.replace_rows(schema.KIND_IP_USED,
                       {"1.2.3.4": {"ts": 1.0, "blocked": False}})
    assert mgmt.unban_plan(store, "9.9.9.9")["ok"] is False
    assert mgmt.unban_plan(store, "1.2.3.4")["found"] is True
    assert mgmt.unban_plan(store, "1.2.3.4")["ok"] is False

