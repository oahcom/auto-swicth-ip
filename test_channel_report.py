"""渠道健康报表的纯函数（tools/channel_report.py）。

不碰生产台账：状态表、源表、换线历史全部由用例喂。
"""
import importlib.util
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


@pytest.fixture
def mod(monkeypatch, tmp_path):
    m = _mod()
    # 换线历史有 13MB，用例给个空文件；源表给个固定映射（真实映射要读 .env）。
    monkeypatch.setenv("WARP_IP_HISTORY_FILE_PATH", str(tmp_path / "hist.jsonl"))
    monkeypatch.setattr(m, "sub_url_map", lambda: {"air.example/x?flag=sing":
                                                   "https://air.example/x?token=***&flag=sing"})
    return m


def _chans(**kw):
    base = {"node": "n1", "colo": "SIN", "carrier": "WG", "family": "v6", "ip": None}
    base.update(kw)
    return {"WARP-WG-1": base}


def _blocked_ledger(mod, monkeypatch, tmp_path, entries, now=1_800_000_000.0):
    """喂一份临时 IP 台账并固定 TTL，不碰生产 13MB 台账。"""
    import json

    for key, val in (("blocked_guard_sec", 21600),
                     ("rate429_guard_sec", 60),
                     ("blocked_ttl_sec", 86400)):
        monkeypatch.setitem(mod.CFG["warp"], key, val)
    p = tmp_path / "used.json"
    p.write_text(json.dumps({"used": entries}))
    return mod.blocked_ip_summary(now=now, path=p)


def test_blocked_summary_separates_active_from_residual(mod, monkeypatch, tmp_path):
    """blocked=True 不等于「还封着」：过期标记必须单独计数。"""
    now = 1_800_000_000.0
    s = _blocked_ledger(mod, monkeypatch, tmp_path, {
        "ip-403": {"blocked": True,
                   "blocked_ts_by_kind": {"abuse403": now - 3600}},
        "ip-429-default": {"blocked": True,
                           "blocked_ts_by_kind": {"rate429": now - 120}},
        "ip-429-ban": {"blocked": True,
                       "blocked_ts_by_kind": {"rate429": now - 3600},
                       "ban_until": now + 7200, "ban_kind": "rate429"},
        "ip-legacy": {"blocked": True, "blocked_ts": now - 3600,
                      "blocked_kind": None},
        "ip-clean": {"blocked": False},
    }, now)
    assert (s["total"], s["marked"]) == (5, 4)
    assert s["active"] == 3, "403 / ban_until / legacy 三档都还在惩罚期"
    assert s["expired"] == 1, "只过了 60s 默认档的 429 应算残留"
    assert s["ban_until_active"] == 1
    assert s["by_kind"] == {"abuse403": 1, "legacy": 1, "rate429": 2}
    assert s["earliest_release"] == now + 7200
    assert s["latest_release"] == now - 3600 + 86400


def test_blocked_summary_rejects_untrusted_ban_until(mod, monkeypatch, tmp_path):
    """ban_kind 不是 rate429 的 ban_until 与 daemon._warp_ban_until 一样不采信。"""
    now = 1_800_000_000.0
    s = _blocked_ledger(mod, monkeypatch, tmp_path, {
        "no-kind": {"blocked": True,
                    "blocked_ts_by_kind": {"rate429": now - 3600},
                    "ban_until": now + 99999, "ban_kind": None},
        "wrong-kind": {"blocked": True,
                       "blocked_ts_by_kind": {"rate429": now - 3600},
                       "ban_until": now + 99999, "ban_kind": "abuse403"},
    }, now)
    assert s["active"] == 0 and s["expired"] == 2
    assert s["ban_until_active"] == 0


def test_blocked_summary_multi_kind_takes_latest(mod, monkeypatch, tmp_path):
    """多类惩罚并行取最晚解禁，不能被 429 的 60s 短窗口盖掉。"""
    now = 1_800_000_000.0
    s = _blocked_ledger(mod, monkeypatch, tmp_path, {
        "ip": {"blocked": True, "blocked_ts_by_kind": {
            "abuse403": now - 100,     # 6h 还剩 21500s
            "rate429": now - 10}},     # 60s 还剩 50s
        "expired": {"blocked": True,
                    "blocked_ts_by_kind": {"abuse403": now - 21700}},
    }, now)
    assert s["active"] == 1 and s["expired"] == 1
    assert s["latest_release"] == now - 100 + 21600
    assert s["by_kind"] == {"abuse403": 1, "abuse403+rate429": 1}


def test_blocked_ips_cli_prints_both_counts(mod, monkeypatch, capsys):
    """--blocked-ips 只走早退分支，输出必须同时给出「生效」与「残留」。"""
    monkeypatch.setattr(sys, "argv", ["channel_report.py", "--blocked-ips"])
    monkeypatch.setattr(mod, "blocked_ip_summary", lambda: {
        "now": 1_800_000_000.0, "total": 10, "marked": 8, "active": 3,
        "expired": 5, "ban_until_active": 2, "earliest_release": None,
        "latest_release": None, "by_kind": {"rate429": 8}})
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "仍在惩罚期 3 条" in out
    assert "标记残留 5 条" in out


def test_rates_count_only_ban_verdicts_and_ok(mod, tmp_path):
    now = 1_800_000_000.0
    (tmp_path / "hist.jsonl").write_text("")
    state = {"WARP-WG-1": {"ban_probes": [
        (now - 60, "OK"), (now - 50, "quota"), (now - 40, "quota"),
        (now - 30, "err"),                     # 探测自身故障，不算 ban
        (now - 30 * 86400, "quota"),           # 窗口外
    ]}}
    r = mod.summarize(state, _chans(),
                      {"n1": {"sub": "okz", "label": "air.example/x?flag=sing",
                              "name": "新加坡-01"}}, 6, now)[0]
    assert (r["ok"], r["ban"], r["err"], r["samples"]) == (1, 2, 1, 4)
    assert r["success_rate"] == 0.25 and r["ban_rate"] == 0.5
    # 三列各司其职：短订阅名 / 完整（已脱敏）地址 / 机场节点名
    assert r["source"] == "okz"
    assert r["source_url"].endswith("token=***&flag=sing")
    assert r["node_name"] == "新加坡-01"


def test_window_is_clamped_to_local_midnight(mod, tmp_path):
    """ban_probes 只留当天，24h 窗不能把凌晨的残留算成整天。"""
    (tmp_path / "hist.jsonl").write_text("")
    now = 1_800_000_000.0
    r = mod.summarize({}, _chans(), {}, 24, now)[0]
    assert r["window_from"] == pytest.approx(mod._day_start(now))


def test_only_registered_channels_are_listed(mod, monkeypatch):
    """状态表里的历史残留（已从 members 删掉的线）不该出现在报表里。"""
    monkeypatch.setitem(mod.CFG["warp"], "members",
                        [{"tag": "WARP-WG-1", "port": 1110, "kind": "wg"},
                         {"tag": "WARP-CF", "port": 1080}])
    # 两个台账文件都不存在 → 只能靠成员表兜底，且必须一条不少。
    monkeypatch.setattr(mod, "_read_json", lambda p, default=None: default)
    chans = mod.load_channels()
    assert set(chans) == {"WARP-WG-1", "WARP-CF"}
    assert chans["WARP-WG-1"]["carrier"] == "WG"     # kind 推断，不是「未知」
    assert chans["WARP-CF"]["carrier"] == "usque"


def test_tag_drift_falls_back_to_hostport(mod, tmp_path):
    """同名节点刷新后 tag 会加 `~N`，台账里还是旧 tag —— 按 host:port 也要认。"""
    (tmp_path / "hist.jsonl").write_text("")
    now = 1_800_000_000.0
    idx = mod.source_index({"h.example:443-any":
                            {"sub": "dasho", "label": "air/x?flag=sing", "name": "SG-1"}})
    r = mod.summarize({"WARP-WG-1": {"ban_probes": [(now - 1, "OK")]}},
                      _chans(node="h.example:443~2-any"), idx, 6, now)[0]
    assert r["source"] == "dasho" and r["node_name"] == "SG-1"


def test_dormant_nodes_are_labelled_not_dashed(mod, tmp_path):
    """H2 线绑的节点只存在于 `dormant-outbounds.json` 时，标签要写**具体订阅**，
    不能跟「来源不明」一样打成 `-`，也不能用内部的「沉睡池」叫法。

    那份文件是 okz 的旧入口快照（2026-11-04 实测 63/63 密码与 okz 当前订阅相同），
    所以来源列写 `okz`，标签注明是旧入口。"""
    (tmp_path / "hist.jsonl").write_text("")
    now = 1_800_000_000.0
    r = mod.summarize({}, _chans(node="h.example:50037-any"), {}, 6, now,
                      dormant={"h.example:50037-any"})[0]
    assert r["source"] == "okz"
    assert r["source_label"] == "旧入口快照"
    assert r["source_url"] == "-"


def test_channel_segments_counts_slash64_not_ip(mod, tmp_path):
    """出口段按 /64 计，同段内不同 /128 只算一段；v4 按地址计。

    台账 `.warp_ip_history.jsonl` 只有 ip/t/b/k，**没有渠道归属** ——
    带归属的只有 daemon 打的出口行，所以这一列只能从日志来。"""
    now = 1_800_000_000.0
    log = tmp_path / "daemon.log"
    def at(sec):  # now - sec 的时间戳，格式与 daemon.log 一致
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - sec))
    log.write_text(
        f"[{at(600)}]   ✓ WARP-WG-1 出口 2a09:bac5:55f8:1d0f::2e5:5e 模型层: OK\n"
        f"[{at(500)}]   · WARP-WG-1 出口 2a09:bac5:55f8:1d0f::9:9 TTL 内已有 OK\n"
        f"[{at(400)}]   ✓ WARP-WG-1 重启后出口=2a09:bac5:55f9:2a00::1:1\n"
        f"[{at(300)}]   ✗ WARP-WG-1 重启后出口=探测失败\n"
        f"[{at(200)}]   ✓ WARP-CF-F 重启后出口=104.28.205.117\n"
        f"[{at(100)}]   ✓ WARP-CF-F 重启后出口=104.28.205.117\n"
        f"[{at(86400)}]  ✓ WARP-WG-9 出口 2a09:bac5:1ef8::1:1 模型层: OK\n")
    got = mod.channel_segments(log)
    assert got["WARP-WG-1"]["v6"] == ["2a09:bac5:55f8:1d0f", "2a09:bac5:55f9:2a00"], \
        "同 /64 的两个 /128 只能算一段，且段名要四段齐全"
    assert got["WARP-WG-1"]["addr"] == ["2a09:bac5:55f8:1d0f::9:9",
                                       "2a09:bac5:55f8:1d0f::2e5:5e",
                                       "2a09:bac5:55f9:2a00::1:1"], \
        "/128 另存一份且按数值排：同段两个地址都要留，不能只留 /64"
    assert got["WARP-CF-F"]["v4"] == ["104.28.205.117"], "v4 按地址计且去重"
    # 不按时间窗口截断：日志里 24h 前那条也要算
    assert got["WARP-WG-9"]["v6"] == ["2a09:bac5:1ef8:0"]


def test_fp_mix_maps_segments_to_fingerprint_colo(mod):
    """段指纹 colo 与渠道声明的 colo 是两件事：WG 线经机场节点中转，
    节点在 NRT、出口可以落在 SIN。`_fp_mix` 只做映射，不改声明。"""
    mix = mod._fp_mix(["2a09:bac1:6500:8", "2a09:bac5:55f8:18be",
                       "2a09:bac1:3b00:10"])
    # 6500/55f8 → SIN（2 个），3b00 → NRT（1 个），从多到少
    assert mix[0][0] == "SIN" and mix[0][1] == 2
    assert ("NRT", 1) in mix
    assert mod._fp_mix([]) == []


def test_fp_label_marks_only_top_mismatch(mod):
    """`落点` 列只在**主落点**与声明不同时标 ≠。

    按「是否 100% 一致」标会让几乎每行都带 ≠（多条渠道合并后必然混着
    几个机房），标记本身就没信息了。"""
    assert mod._fp_label({"colo": "SIN", "colo_fp": [("SIN", 9), ("SJC", 1)]}) == "SIN"
    assert mod._fp_label({"colo": "NRT", "colo_fp": [("SIN", 9), ("NRT", 2)]}) == "≠SIN"
    assert mod._fp_label({"colo": "NRT", "colo_fp": []}) == "—"


def test_merge_segs_dedups_across_channels(mod):
    """同 colo 的线共用同一个池子，合并要**去重**而不是相加。

    实测 SIN 的 12 条线 /32 明细完全一样（`bac1(4) + bac5(8)`）——
    按渠道相加会把同一份池子印 12 遍，正是「都是重复的」那个毛病。"""
    a = {"seg_v6": ["2a09:bac5:55f8:18be"], "seg_addr": ["2a09:bac5:55f8:18be::1"],
         "seg_v4": ["104.28.205.117"]}
    b = {"seg_v6": ["2a09:bac5:55f8:18be", "2a09:bac5:55f9:2a00"],
         "seg_addr": ["2a09:bac5:55f8:18be::2", "2a09:bac5:55f9:2a00::1"],
         "seg_v4": ["104.28.205.117"]}
    m = mod._merge_segs([a, b])
    assert m["seg_v6"] == ["2a09:bac5:55f8:18be", "2a09:bac5:55f9:2a00"]
    assert len(m["seg_addr"]) == 3 and m["seg_v4"] == ["104.28.205.117"]
    assert mod._merge_segs([]) == {"seg_v6": [], "seg_addr": [], "seg_v4": []}


def test_fp_pools_dedup_three_levels_over_full_log(mod):
    """池按**指纹 colo**归并，三级各自去重，且收全日志（含已下线 tag）。

    池子是机房的属性，不随成员表缩水；只算在册渠道会让机房看起来被削掉一半。"""
    segs = {"WARP-WG-1": {  # 在册
                "v6": ["2a09:bac5:55f8:18be", "2a09:bac5:55f8:1d0f"],
                "addr": ["2a09:bac5:55f8:18be::1", "2a09:bac5:55f8:18be::2"]},
            "WARP-GONE": {   # 已下线，日志里还在
                "v6": ["2a09:bac5:55f8:18be"], "addr": []}}
    p = mod._fp_pools(segs)
    # 两个 /64 同属 55f8 → SIN；三个 /128 只落在第一个 /64 下
    assert len(p["SIN"]["48"]) == 1 and len(p["SIN"]["64"]) == 2
    assert len(p["SIN"]["128"]) == 2
    assert mod._fp_pools({}) == {}


def test_fp_pools_half_shared_slash48_is_split_by_slash64(mod):
    """`bac1:7680` 的 /48 里同时有 PHX 与 SJC 的 /64 —— /48 必须跟着 /64 走。

    直接 `colo_of(/48)` 恒判 PHX：`:28` 那批 SJC 的 /64 会被记到 PHX 名下，
    SJC 的 /48 少算。实测旧口径 SJC 池 `8/52/…`，实际是 `12/52/…`。"""
    segs = {"WARP-X": {"v6": ["2a09:bac1:7680:8e0", "2a09:bac1:7680:28"], "addr": []}}
    p = mod._fp_pools(segs)
    assert "2a09:bac1:7680" in p["PHX"]["48"] and "2a09:bac1:7680" in p["SJC"]["48"]
    assert p["PHX"]["64"] == {"2a09:bac1:7680:8e0"}
    assert p["SJC"]["64"] == {"2a09:bac1:7680:28"}


def test_seg_label_shows_four_prefix_levels(mod):
    """表格列写 `/32 / /48 / /64 / /128`：只给最细的 /64 看不出跨了几个批次，
    只给 /32 又看不出抽签密度，而没有 /128 就分不清「地址摊开」与「挤在一段」。"""
    r = {"seg_v6": ["2a09:bac5:55f8:18be", "2a09:bac5:55f8:1d0f",
                    "2a09:bac5:55f9:2a00", "2a09:bac1:76a0:8e0"],
         "seg_addr": ["2a09:bac5:55f8:18be::1", "2a09:bac5:55f8:1d0f::1",
                      "2a09:bac5:55f9:2a00::1", "2a09:bac1:76a0:8e0::1",
                      "2a09:bac1:76a0:8e0::2"],
         "seg_v4": ["104.28.205.117", "104.28.205.118"]}
    # 2 个 /32（bac1/bac5）、3 个 /48、4 个 /64、5 个 /128
    assert mod._seg_counts(r) == (2, 3, 4, 5)
    assert mod._seg_label(r) == "2/3/4/5 +2v"
    assert mod._seg_label({"seg_v6": [], "seg_addr": [], "seg_v4": []}) == "—"


def test_seg32_groups_count_under_each_batch(mod):
    """明细表按 /32 分组，给出它下面的 /48、/64 与 /128 个数，按数值排序。"""
    r = {"seg_v6": ["2a09:bac5:55f9:2a00", "2a09:bac5:55f8:18be",
                    "2a09:bac5:55f8:1d0f", "2a09:bac1:76a0:8e0"],
         "seg_addr": ["2a09:bac5:55f9:2a00::1", "2a09:bac5:55f9:2a00::2",
                      "2a09:bac1:76a0:8e0::1"]}
    assert mod._seg32_groups(r) == [
        ("2a09:bac1", 1, 1, 1),     # bac1: 1 个 /48、1 个 /64、1 个 /128
        ("2a09:bac5", 2, 3, 2),     # bac5: 2 个 /48、3 个 /64、2 个 /128
    ]


def test_seg_name_keeps_all_four_hextets(mod):
    """段名四段全给：`2a09:` 虽然目前恒定，但不是常量保证。"""
    import ipaddress
    assert mod._seg_name(ipaddress.ip_network("2a09:bac5:55f8:18be::/64")) == \
        "2a09:bac5:55f8:18be"
    assert mod._seg_name(ipaddress.ip_network("2a09:bac1:7680:3d0::/64")) == \
        "2a09:bac1:7680:3d0"
    # 第四段全 0 时 exploded 不会塌成 `::`，但要能正确输出
    assert mod._seg_name(ipaddress.ip_network("2a09:bac5:55f8::/64")) == \
        "2a09:bac5:55f8:0"


def test_segs_sorted_numerically_hex(mod):
    """段按十六进制数值排，不是字符串排 —— 否则 `19a0` 会跑到 `9` 前面。"""
    names = ["2a09:bac5:55f9:1d0f", "2a09:bac5:55f8:2a00", "2a09:bac5:55f9:15f"]
    assert sorted(names, key=mod._seg_sort) == [
        "2a09:bac5:55f8:2a00", "2a09:bac5:55f9:15f", "2a09:bac5:55f9:1d0f"]


def test_restarts_count_window_not_lifetime_total(mod, tmp_path):
    """重启列必须是**窗口内**次数：`restart_count` 是进程终身累计。

    实测 WARP-WG-1 的 `restart_count` 是 38881，印在「最近 2 小时」这一行上
    会把一条刚起 4 次的线读成永动机。只有 `restart_hist` 带时间戳。
    """
    (tmp_path / "hist.jsonl").write_text("")
    now = 1_800_000_000.0
    state = {"WARP-WG-1": {"restart_count": 38881,
                           "restart_hist": [now - 60, now - 30,
                                            now - 30 * 86400]}}
    r = mod.summarize(state, _chans(), {}, 2, now)[0]
    assert r["restarts"] == 2


def test_old_string_ledger_still_reads(mod, tmp_path):
    """升级瞬间盘上还是旧格式（纯字符串）；报表不能因此整列变 `-`。"""
    (tmp_path / "hist.jsonl").write_text("")
    now = 1_800_000_000.0
    r = mod.summarize({}, _chans(), {"n1": "air.example/x?flag=sing"}, 6, now)[0]
    assert r["source_label"] == "air.example/x?flag=sing"
    assert r["node_name"] == "-"


def test_stale_verdict_is_not_printed_as_current(mod, tmp_path):
    """窗口内**零样本**的线，不能把它旧的 `last_verdict` 当当前状态印出来。

    WHY：`last_verdict` 只在探测/重启路径写。一条线停探几个小时（单元没了、
    渠道锁定）之后，那个词仍是最后一次留下的值 —— 实测 2026-11-05
    WARP-CF-C/G/H/I 的单元 10-28 就删了，报表照印 `unknown`，`fail_streak`
    冻在 14 万，读起来像「现在探测失败」。CLI 表与 HTML 必须同一口径：
    两个视图对同一份数据给出不同结论本身就是缺陷。
    """
    (tmp_path / "hist.jsonl").write_text("")
    now = 1_800_000_000.0
    state = {"WARP-WG-1": {"last_verdict": "quota", "ban_probes": [],
                           "fail_streak": 141263}}
    rows = mod.summarize(state, _chans(), {}, 6, now)
    assert rows[0]["samples"] == 0
    out = tmp_path / "r.html"
    mod.render_html(rows, 6, out)
    text = out.read_text(encoding="utf-8")
    assert "无样本" in text, "HTML 把旧结论当成了当前状态"
    assert ">quota<" not in text


def test_disabled_unit_is_primary_reason(mod, tmp_path, monkeypatch, capsys):
    """单元没在跑时只印「单元停用」，不把停用前冻住的 quota 印成当前原因。

    2026-11-09 实测：本地 usque-warp-c/e/f/g/h/i 六个单元根本没装，状态表里
    冻着删单元那天的 quota，报表照印就成了「这条线配额耗尽」——处置动作完全
    不同（配额要等重置，单元没装要去装）。
    """
    now = 1_800_000_000.0
    (tmp_path / "hist.jsonl").write_text("")
    monkeypatch.setattr(mod, "_unit_states",
                        lambda svc: {v: False for v in svc.values()})
    chans = {"WARP-CF-B": {"node": None, "colo": "NRT", "carrier": "usque",
                           "family": "v6", "ip": None}}
    state = {"WARP-CF-B": {"ban_probes": [[now - 120, now - 60, 5, "quota"]],
                           "last_verdict": "quota",
                           "quota_until": now + 3600}}
    row = mod.summarize(state, chans, {}, 6, now)[0]
    assert row["unit_ok"] is False, "单元 inactive 必须反映到行上"
    mod.print_table([row], 6)
    out = capsys.readouterr().out
    assert "单元停用" in out, "主因必须是「单元停用」"
    assert "配额至" not in out, "单元没跑时不能再印配额原因"


def test_disabled_unit_is_primary_reason(mod, tmp_path, monkeypatch, capsys):
    """单元没在跑时只印「单元停用」，不把停用前冻住的 quota 印成当前原因。

    2026-11-09 实测：本地 usque-warp-c/e/f/g/h/i 六个单元根本没装，状态表里
    冻着删单元那天的 quota，报表照印就成了「这条线配额耗尽」——处置动作完全
    不同（配额要等重置，单元没装要去装）。
    """
    now = 1_800_000_000.0
    (tmp_path / "hist.jsonl").write_text("")
    monkeypatch.setattr(mod, "_unit_states",
                        lambda svc: {v: False for v in svc.values()})
    chans = {"WARP-CF-B": {"node": None, "colo": "NRT", "carrier": "usque",
                           "family": "v6", "ip": None}}
    state = {"WARP-CF-B": {"ban_probes": [[now - 120, now - 60, 5, "quota"]],
                           "last_verdict": "quota",
                           "quota_until": now + 3600}}
    row = mod.summarize(state, chans, {}, 6, now)[0]
    assert row["unit_ok"] is False, "单元 inactive 必须反映到行上"
    mod.print_table([row], 6)
    out = capsys.readouterr().out
    assert "单元停用" in out, "主因必须是「单元停用」"
    assert "配额至" not in out, "单元没跑时不能再印配额原因"
