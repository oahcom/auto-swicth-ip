"""跨源一致性自检与「无样本」成因标注。

覆盖 2026-11-11 的两个病灶：
  ① 配置真相源分裂 —— 状态台账里有线、探测名单里没有，线永远不被探测，
     状态冻在最后一次观测上，报表却显示成「有结论」。
  ② 「无样本」不标成因 —— 从没探过 / 探过但惩罚期短路 / 探过但没入账 /
     取不到地址，四种完全不同的故障长得一模一样。
"""
import importlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent


def _mod():
    sys.path.insert(0, str(PROJECT))
    spec = importlib.util.spec_from_file_location(
        "channel_report", PROJECT / "tools" / "channel_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_env_file() -> dict:
    """读项目根的 .env，返回 KEY=VALUE（不写 os.environ）。

    WHY 这里必须读 .env：`config.CFG["warp"]["members"]` 里的 WG 线是
    **按环境变量拼出来的**（WARP_WG_FAMILIES / WARP_WG_NODES）。
    不注入就得到 WG=0 条，于是 `summarize` 的 `in_members` 把每一条 WG
    线都判成「不在探测名单」——「从未探测」这个更准确的成因被盖住，
    测试也会因此失败。真实 daemon 由 systemd 的 EnvironmentFile 注入，
    线上不会误判；只有裸跑的测试进程会。
    """
    env = {}
    path = PROJECT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env[k.strip()] = v
    return env


@pytest.fixture
def mod(monkeypatch, tmp_path):
    """按**生产条件**加载 channel_report：先注入 .env，再 reload config。

    WHY 必须 reload config（不只是注入环境）：
      `channel_report` 在模块级 `from config import CFG`，而 config 的
      members 是在 **config 被 import 的那一刻**按环境变量拼好的。
      pytest 同进程里 config 早就被别的测试 import 过了（那时没有 .env），
      CFG 里一条 WG 线都没有 —— 于是每条 WG 线都被判成「不在探测名单」，
      「从未探测」这个更准确的成因被盖住。

    WHY reload 之后要再 reload 回来、且不摘 sys.modules：
      实测踩过两次：① 摘掉 sys.modules["config"] 会让已持有旧引用的
      test_subscription_parse / test_warp_wg 拿到两个不同的 config
      对象，13 项连锁失败；② reload 后不还原，config.CFG 停在「带 .env」
      形态，别的测试期望「不带 .env」的形态，又红一片。
      所以：进出各 reload 一次，环境由 monkeypatch 自动还原。
    """
    import config as _cfg_mod

    saved_env = {k: os.environ.get(k) for k in ("WARP_WG_FAMILIES",
                                                "WARP_WG_NODES",
                                                "WARP_WG_COUNT")}
    for k, v in _load_env_file().items():
        if not os.environ.get(k):
            monkeypatch.setenv(k, v)
    importlib.reload(_cfg_mod)          # 让 CFG 按带 .env 的环境重算

    m = _mod()
    monkeypatch.setenv("WARP_IP_HISTORY_FILE_PATH", str(tmp_path / "hist.jsonl"))
    monkeypatch.setattr(m, "sub_url_map", lambda: {})
    yield m

    # 还原环境后重新 reload，让 config.CFG 回到别的测试期望的形态。
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        importlib.reload(_cfg_mod)
    except Exception:
        pass


def _chans(*tags):
    return {t: {"node": "n1", "colo": "SIN", "carrier": "WG",
                "family": "v6", "ip": None} for t in tags}


# ---------------------------------------------------------------- 报表成因

def test_no_sample_reason_splits_by_cause(mod, tmp_path):
    """「无样本」必须按成因分开，不能一律两个字。

    四种故障的处置动作完全不同：
      从没探过    → 查探测名单/单元
      惩罚期短路  → 正常换址逻辑，不是故障
      探测未入账  → 入账漏记的 bug
      取不到地址  → 查隧道与落地节点
    """
    now = 1_800_000_000.0
    (tmp_path / "hist.jsonl").write_text("")

    # ① 从没探过
    state = {"WARP-WG-1": {"ban_probes": []}}
    row = mod.summarize(state, _chans("WARP-WG-1"), {}, 6, now)[0]
    assert "从未探测" in mod._no_sample_reason(row)

    # ② 探过，但地址在惩罚期短路（在模型层实测之前就 return 了）
    state = {"WARP-WG-1": {"ban_probes": [], "last_probe_ts": now - 120,
                           "last_verdict": "blocked",
                           "blocked_notes": [[now - 120, now - 60, 3, "blocked"]]}}
    row = mod.summarize(state, _chans("WARP-WG-1"), {}, 6, now)[0]
    assert row["blocked_notes"] == 3, "惩罚期观测必须能被报表读到"
    assert "惩罚期短路" in mod._no_sample_reason(row)

    # ③ 探过、判了、但没入账
    state = {"WARP-WG-1": {"ban_probes": [], "last_probe_ts": now - 120,
                           "last_verdict": "quota"}}
    row = mod.summarize(state, _chans("WARP-WG-1"), {}, 6, now)[0]
    assert "探测未入账" in mod._no_sample_reason(row)

    # ④ 探过，取不到出口地址
    state = {"WARP-WG-1": {"ban_probes": [], "last_probe_ts": now - 120,
                           "ban_noaddr_since": now - 300}}
    row = mod.summarize(state, _chans("WARP-WG-1"), {}, 6, now)[0]
    assert "取不到地址" in mod._no_sample_reason(row)


def test_line_outside_probe_list_is_labelled(mod, tmp_path):
    """不在 daemon 探测名单里的线，报表必须说明这一点。

    实测 2026-11-11：12 条 WG 线状态冻在 blocked 上 26 小时，它们的 tag
    不在 `config.CFG["warp"]["members"]` 里 —— daemon 永远不会去探它们。
    报表只印「无样本」，读起来像「探过但没结论」。
    """
    now = 1_800_000_000.0
    (tmp_path / "hist.jsonl").write_text("")
    monkey = pytest.MonkeyPatch()
    # 让这条线不在 members 里：`svc_of` 由 CFG 派生，这里直接改 summarize 的输入
    state = {"WARP-WG-26": {"ban_probes": [], "last_probe_ts": now - 3600,
                            "last_verdict": "blocked"}}
    row = mod.summarize(state, _chans("WARP-WG-26"), {}, 6, now)[0]
    # CFG 里 WARP-WG-26 不存在（只有 22 条声明 + 补位）→ 不在名单
    assert row["in_members"] is False
    assert "不在探测名单" in mod._no_sample_reason(row)
    monkey.undo()


def test_disabled_unit_prints_frozen_verdict(mod, tmp_path, monkeypatch, capsys):
    """单元没在跑时，冻住的结论与冻结时长必须一起印出来。

    「单元停用（线没跑）」只说明线不跑了，不说明**它最后判成什么**、
    **冻结了多久**。实测 12 条 WG 线的 `blocked` 冻了 26 小时 —— 只印
    「档位停用 单元停用」会把「没人探它」显示成「它坏了」。
    """
    now = 1_800_000_000.0
    (tmp_path / "hist.jsonl").write_text("")
    monkeypatch.setattr(mod, "_unit_states",
                        lambda svc: {v: False for v in svc.values()})
    chans = {"WARP-WG-26": {"node": None, "colo": "SIN", "carrier": "WG",
                            "family": "v6", "ip": None}}
    state = {"WARP-WG-26": {"ban_probes": [], "last_verdict": "blocked",
                            "last_probe_ts": now - 26 * 3600}}
    row = mod.summarize(state, chans, {}, 6, now)[0]
    mod.print_table([row], 6)
    out = capsys.readouterr().out
    assert "单元停用" in out
    assert "blocked" in out, "冻住的结论必须印出来"
    assert "26小时前冻结" in out, "冻结时长必须印出来"


def test_html_and_cli_agree_on_no_sample_cause(mod, tmp_path):
    """HTML 与 CLI 必须同一口径 —— 两个视图给出不同结论本身就是缺陷。"""
    now = 1_800_000_000.0
    (tmp_path / "hist.jsonl").write_text("")
    state = {"WARP-WG-1": {"ban_probes": [], "last_probe_ts": now - 120,
                           "last_verdict": "blocked",
                           "blocked_notes": [[now - 120, now - 60, 2, "blocked"]]}}
    rows = mod.summarize(state, _chans("WARP-WG-1"), {}, 6, now)
    out = tmp_path / "r.html"
    mod.render_html(rows, 6, out)
    text = out.read_text(encoding="utf-8")
    assert "惩罚期短路" in text
    assert "无样本<" not in text, "不能只印笼统的「无样本」"


# ---------------------------------------------------------------- daemon 自检

@pytest.fixture(autouse=True)
def _isolate_selector_drift(monkeypatch):
    """隔离分类七（产物面 ⊄ 生效面）的真实输入面。

    分类七会去读真实槽位的 Clash API 与磁盘渲染配置，而本文件的用例只测
    分类一~六、且对 `problems` 做**精确计数断言** —— 线上真实漂移数据混
    进来会把断言打成 flaky（本机 daemon 常驻，槽位 API 是活的）。这里把
    输入面统一置为「无法判定」（unknown 不计 problems），分类七自身的
    覆盖在 test_config_drift_inspect.py。
    """
    import daemon as d
    monkeypatch.setattr(d, "consumer_slots", lambda: [])
    monkeypatch.setattr(d, "singbox_api", lambda *a, **k: (0, None))


def _daemon():
    import daemon
    return daemon


def test_consistency_check_reports_orphan_state(monkeypatch, tmp_path, caplog):
    """状态台账里有、探测名单里没有的线 —— 必须是自检报出来的第一类问题。

    这些线 daemon 永远不会去探，状态会永远冻住。这是「配置各自为伍」
    最直接的证据，必须在日志里当场暴露，而不是等人从报表反推。

    四类「不在名单」的线性质不同，要分开：
      补位线（WG 23+）   —— 闲置池，本来就不该被探测
      缩容遗留（单元文件已删）—— 只剩状态残骸，不是故障
      台账外单元残骸（单元文件还在）—— 渲染器只增不减留下的，**真问题**
      扫描孤儿（H2-SCAN） —— 单元在跑却没人探，**真问题**

    2026-11-11 语义变更：「缩容遗留」按**单元文件是否还在磁盘**再分两档。
    单元文件还在 = 它是僵尸复活的来源（`h2-install.sh` 遍历渲染产物目录，
    会把台账外单元 `install` 回 systemd 并 `enable`）→ 算问题。
    单元文件已删 = 只剩状态表里的陈旧记录 → 不算问题。
    """
    d = _daemon()
    monkeypatch.setattr(d, "_warp", lambda: {
        "members": [{"tag": "WARP-WG-1", "kind": "wg",
                     "service": "warp-wg-1.service", "port": 1110}],
    })
    monkeypatch.setattr(d, "_TIER_DISABLED", set())
    # 「单元文件是否仍在磁盘」—— 这是区分「真残骸」与「陈旧状态记录」的
    # 唯一判据。测试里必须 monkeypatch，否则会真去查 systemd。
    # 这里让 LAX-24 的单元文件「仍在磁盘」→ 它是台账外单元残骸 → 真问题。
    monkeypatch.setattr(d, "_scan_unit_still_on_disk",
                        lambda tag: tag == "WARP-H2-LAX-24")
    monkeypatch.setattr(d, "_WARP_LINE_STATE", {
        "WARP-WG-1": {},
        "WARP-H2-SCAN-47": {},          # 单元在跑却没人探 → 真问题
        "WARP-WG-26": {},               # 补位线：闲置池
        "WARP-H2-LAX-24": {},           # 台账外单元残骸：单元文件还在
    })
    # 「在跑」只能靠查单元，不能靠 tag 前缀推断 —— 实测 47 条 SCAN 线
    # 对应的单元全是 inactive，按前缀推断会把死线报成「在跑却没人探」。
    monkeypatch.setattr(d, "_scan_running_tunnels",
                        lambda now: {1196: ("WARP-H2-SCAN-47", "usque-h2-16.service")})
    monkeypatch.setattr(d, "_singbox_outbound_tags", lambda: {"WARP-WG-1"})
    rep = d._config_consistency_check()
    assert "WARP-H2-SCAN-47" in rep["scan_unprobed"]
    assert "WARP-WG-26" in rep["spare_wg"], "补位线要单独归类，不算真孤儿"
    assert "WARP-H2-LAX-24" in rep["retired"], "台账外单元残骸要单独归类"
    # 「在跑却没人探」与「单元文件还在的残骸」都是真问题；补位池不是
    assert rep["problems"] == 2, "补位池不计入问题数，两个残骸计入"


def test_consistency_check_does_not_treat_dead_scan_lines_as_running(monkeypatch):
    """单元已停的 SCAN 线不得算成「在跑却没人探」—— 那是假警报。

    实测 2026-11-11：47 条 `WARP-H2-SCAN-*` 对应的 `usque-h2-<N>` 单元
    全是 `inactive`（缩容停掉的）。按 tag 前缀推断会把它们全报成真问题，
    47 条假警报盖住真正要修的那几条。

    **语义变更（2026-11-11）**：这两条线现在**仍会被算进 `problems`**，
    但归类是 `retired`（缩容遗留）而不是 `scan_unprobed`（在跑却没人探）。

    WHY 语义变了：旧断言「停掉的单元不算不一致」漏掉了一个事实 ——
    `h2-install.sh` 遍历的是 `.warp/h2/units/` 下的单元文件，不是 systemd
    目录。实测产物目录里有 103 个单元 vs 台账 56 条，其中 47 个台账外的
    单元文件**仍在**。安装脚本会 `cmp` 比对后 `install` 回 systemd，再从
    累加出的 `SERVICES` 派生 `USQUE_UNITS` 去 `enable` —— 于是这 47 个
    台账外单元会被**装回并 enable**，僵尸单元当场复活。

    所以「单元已停」不等于「没问题」：**单元文件还在磁盘上才是问题的
    判据**，因为它是复活的来源。本测试断言的正是这个区分：
    停掉但文件还在 → `retired`（要剪）；停掉且文件已删 → 不算任何问题。
    """
    d = _daemon()
    monkeypatch.setattr(d, "_warp", lambda: {"members": []})
    monkeypatch.setattr(d, "_TIER_DISABLED", set())
    monkeypatch.setattr(d, "_WARP_LINE_STATE", {
        "WARP-H2-SCAN-16": {}, "WARP-H2-SCAN-17": {},
    })
    monkeypatch.setattr(d, "_scan_running_tunnels", lambda now: {})
    monkeypatch.setattr(d, "_singbox_outbound_tags", lambda: set())
    # 这两条的单元文件**仍在磁盘**（缩容留下的历史残留）
    monkeypatch.setattr(d, "_scan_unit_still_on_disk",
                        lambda tag: tag.startswith("WARP-H2-SCAN-"))
    rep = d._config_consistency_check()
    assert rep["scan_unprobed"] == [], "已停的单元不算『在跑却没人探』"
    assert rep["retired"] == ["WARP-H2-SCAN-16", "WARP-H2-SCAN-17"], \
        "单元文件仍在磁盘 = 台账外单元残骸，必须报出来"
    assert rep["problems"] == 2, "台账外单元残骸是要剪除的真问题"


def test_consistency_check_reports_old_numbering_residue(monkeypatch):
    """旧编号体系的 H2 线（`WARP-H2-LAX-<N>`）—— 单元文件还在就该报。

    当前台账用 NRT 等 colo，旧编号的 LAX 线早已不在台账。但它的单元文件
    若还在磁盘上，就是「渲染器只增不减」留下的残骸 —— 与 SCAN 残骸同一
    类问题，只是 tag 形态不同。

    反之：若该编号在当前台账里**已被别的线复用**（编号是端口契约、不回收），
    单元文件在磁盘上是正常的，不该报。这正是 `_scan_unit_still_on_disk`
    额外查台账的原因。
    """
    d = _daemon()
    monkeypatch.setattr(d, "_warp", lambda: {"members": []})
    monkeypatch.setattr(d, "_TIER_DISABLED", set())
    monkeypatch.setattr(d, "_WARP_LINE_STATE", {
        "WARP-H2-LAX-24": {}, "WARP-H2-LAX-30": {},
    })
    monkeypatch.setattr(d, "_scan_running_tunnels", lambda now: {})
    monkeypatch.setattr(d, "_singbox_outbound_tags", lambda: set())
    # LAX-24 的单元文件已删（只剩陈旧状态记录）→ 不算问题
    # LAX-30 的单元文件还在 → 台账外单元残骸 → 要报
    monkeypatch.setattr(d, "_scan_unit_still_on_disk",
                        lambda tag: tag == "WARP-H2-LAX-30")
    rep = d._config_consistency_check()
    assert rep["retired"] == ["WARP-H2-LAX-30"], \
        "只有单元文件仍在磁盘的那条才算残骸"
    assert rep["problems"] == 1


def test_consistency_check_ignores_removed_scan_lines(monkeypatch):
    """单元文件已从磁盘删掉的 SCAN 线 —— 只剩状态表里的陈旧记录，不算问题。

    这是上一个测试的另一半：区分「文件还在」与「文件已删」。
    只有前者才是需要剪除的不一致；后者只是状态表的陈旧数据，告警它
    只会制造噪音。
    """
    d = _daemon()
    monkeypatch.setattr(d, "_warp", lambda: {"members": []})
    monkeypatch.setattr(d, "_TIER_DISABLED", set())
    monkeypatch.setattr(d, "_WARP_LINE_STATE", {
        "WARP-H2-SCAN-16": {}, "WARP-H2-SCAN-17": {},
    })
    monkeypatch.setattr(d, "_scan_running_tunnels", lambda now: {})
    monkeypatch.setattr(d, "_singbox_outbound_tags", lambda: set())
    # 单元文件**已从磁盘删掉**
    monkeypatch.setattr(d, "_scan_unit_still_on_disk", lambda tag: False)
    rep = d._config_consistency_check()
    assert rep["scan_unprobed"] == []
    assert rep["retired"] == [], "文件已删 = 只剩陈旧状态记录，不算问题"
    assert rep["problems"] == 0


def test_consistency_check_excludes_spare_wg_pool(monkeypatch):
    """WG 补位池（23+）是设计上的闲置池，永远不算问题。

    它本来就不该被探测 —— 探测它会烧配额。把它算进 problems 会让
    「30 条闲置池」的数字压过真正的分裂问题，告警就没人看了。
    """
    d = _daemon()
    monkeypatch.setattr(d, "_warp", lambda: {"members": []})
    monkeypatch.setattr(d, "_TIER_DISABLED", set())
    # members 里**没有**任何 WG 线 → 状态表里的 WG 全是「不在名单」的
    monkeypatch.setattr(d, "_WARP_LINE_STATE", {
        "WARP-WG-23": {}, "WARP-WG-26": {},
    })
    monkeypatch.setattr(d, "_scan_running_tunnels", lambda now: {})
    monkeypatch.setattr(d, "_singbox_outbound_tags", lambda: set())
    monkeypatch.setattr(d, "_scan_unit_still_on_disk", lambda tag: False)
    rep = d._config_consistency_check()
    assert len(rep["spare_wg"]) == 2, "WG-23 与 WG-26 属补位池"
    assert rep["problems"] == 0, "闲置池不是问题"


def test_consistency_check_reports_missing_outbound(monkeypatch):
    """探测名单里有、但 sing-box 配置里没有 outbound —— 另一类不一致。

    这种线会被探测，但换线时 selector 翻不到它（根本不在 sing-box 里）。
    """
    d = _daemon()
    monkeypatch.setattr(d, "_warp", lambda: {
        "members": [{"tag": "WARP-WG-1", "kind": "wg", "service": "warp-wg-1.service",
                     "port": 1110},
                    {"tag": "WARP-H2-NRT-0", "kind": "h2", "service": "usque-h2-0.service",
                     "port": 1180}],
    })
    monkeypatch.setattr(d, "_TIER_DISABLED", set())
    monkeypatch.setattr(d, "_WARP_LINE_STATE", {"WARP-WG-1": {}, "WARP-H2-NRT-0": {}})
    monkeypatch.setattr(d, "_scan_running_tunnels", lambda now: {})
    monkeypatch.setattr(d, "_singbox_outbound_tags", lambda: {"WARP-WG-1"})
    rep = d._config_consistency_check()
    assert rep["missing_out"] == ["WARP-H2-NRT-0"]
    assert rep["problems"] == 1


def test_consistency_check_reports_tier_off_with_state(monkeypatch):
    """档位停用但状态台账里还有它的记录 —— 档位切换没清干净。"""
    d = _daemon()
    monkeypatch.setattr(d, "_warp", lambda: {
        "members": [{"tag": "WARP-WG-1", "kind": "wg",
                     "service": "warp-wg-1.service", "port": 1110}],
    })
    monkeypatch.setattr(d, "_TIER_DISABLED", {"WARP-WG-1"})
    monkeypatch.setattr(d, "_WARP_LINE_STATE", {"WARP-WG-1": {"last_verdict": "blocked"}})
    monkeypatch.setattr(d, "_scan_running_tunnels", lambda now: {})
    monkeypatch.setattr(d, "_singbox_outbound_tags", lambda: {"WARP-WG-1"})
    rep = d._config_consistency_check()
    assert rep["tier_off_with_state"] == ["WARP-WG-1"]
    assert rep["problems"] == 1


def test_blocked_note_does_not_touch_ban_probes(monkeypatch):
    """惩罚期短路记在独立字段，**不能**写进 `ban_probes`。

    WHY：`ban_probes` 是渠道锁的判据样本，`_channel_ban_hold_hit` 要求当天
    样本**全部**判 quota 才锁线。blocked 混进去会让这条线永远锁不上 ——
    blocked 不是「渠道被烧穿」，是「这一个地址在惩罚期，换个地址就好」。
    """
    d = _daemon()
    st = {"ban_probes": [], "blocked_notes": []}
    monkeypatch.setattr(d, "_warp_line_state_locked", lambda tag: st)
    monkeypatch.setattr(d, "_save_warp_line_state", lambda *a, **k: None)
    now = time.time()
    d._channel_blocked_note("WARP-WG-1", now)
    d._channel_blocked_note("WARP-WG-1", now + 1)
    assert st["ban_probes"] == [], "ban_probes 绝不能被 blocked 污染"
    assert len(st["blocked_notes"]) == 1, "同结论相邻段应合并成一段"
    assert st["blocked_notes"][0][2] == 2, "段内计数要累加"
    assert st["blocked_notes"][0][3] == "blocked"
