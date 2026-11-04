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
    got = mod.channel_segments(6 * 3600, now, log)
    assert got["WARP-WG-1"] == {"v6": 2, "v4": 0}, "同 /64 的两个 /128 只能算一段"
    assert got["WARP-CF-F"] == {"v6": 0, "v4": 1}, "v4 按地址计且去重"
    assert "WARP-WG-9" not in got, "窗口外的行不能算进来"


def test_seg_label_does_not_show_zero(mod):
    """两者都为 0 时显示 `—`：0 会被读成「只有一段」，实际是「日志里没它的出口」。"""
    assert mod._seg_label({"seg_v6": 0, "seg_v4": 0}) == "—"
    assert mod._seg_label({"seg_v6": 12, "seg_v4": 0}) == "12"
    assert mod._seg_label({"seg_v6": 0, "seg_v4": 3}) == "3v"
    assert mod._seg_label({"seg_v6": 12, "seg_v4": 3}) == "12+3v"


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
