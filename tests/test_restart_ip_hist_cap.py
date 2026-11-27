"""restart_ip_hist 的存储上限：判据阈值与存储上限必须分开。

线上实测（2026-11-27）：`.warp_line_state.json` 12.88 MB，其中 12.86 MB 是
`restart_ip_hist`，412 条线平均每条约 5,500 个地址。原因不是没裁剪，而是
`hist[-fuse:]` 里的 fuse 取配置 `restart_ip_stuck_fuse=999999`（主人明确
要求的「不限制」），切片长度等于不裁剪。

存储上限一旦跟着判据阈值走，配置里的「禁用」就会变成「无界增长」。所以：
fuse <= 上限 → 原判定完整保留；fuse > 上限 → 判定禁用（配置值不动），
历史只留上限条。
"""

import daemon


def _policy(monkeypatch, fuse):
    monkeypatch.setattr(
        daemon, "_warp", lambda: {"restart_ip_stuck_fuse": fuse})
    return daemon._restart_ip_fuse_policy()


def test_fuse_within_cap_keeps_original_semantics(monkeypatch):
    assert _policy(monkeypatch, 3) == (True, 3, 3)
    assert _policy(monkeypatch, 64) == (True, 64, 64)


def test_fuse_above_cap_disables_judgement_but_bounds_history(monkeypatch):
    cap = daemon._RESTART_IP_HIST_MAX

    # 65 是「刚好超过上限」：判定禁用，历史仍留满上限。
    assert _policy(monkeypatch, 65) == (False, 0, cap)
    # 生产配置：主人要求的「不限制」。
    assert _policy(monkeypatch, 999999) == (False, 0, cap)


def test_fuse_floor_is_two(monkeypatch):
    assert _policy(monkeypatch, 0) == (True, 2, 2)
    assert _policy(monkeypatch, 1) == (True, 2, 2)


def test_clamp_keeps_tail_and_drops_malformed_entries():
    st = {"restart_ip_hist": ["a", None, "b", 7, "c", "d"]}

    daemon._clamp_restart_ip_hist(st, 2)

    assert st["restart_ip_hist"] == ["c", "d"]


def test_clamp_collapses_the_measured_backlog():
    # 线上单条线的真实量级（5,700 条 / 约 170 KB）。
    st = {"restart_ip_hist": ["1.2.3.%d" % (i % 250) for i in range(5700)]}

    daemon._clamp_restart_ip_hist(st, daemon._RESTART_IP_HIST_MAX)

    assert len(st["restart_ip_hist"]) == daemon._RESTART_IP_HIST_MAX
    assert st["restart_ip_hist"][-1] == "1.2.3.%d" % (5699 % 250)


def test_clamp_tolerates_missing_or_none():
    for raw in ({}, {"restart_ip_hist": None}, {"restart_ip_hist": []}):
        st = dict(raw)
        daemon._clamp_restart_ip_hist(st, 8)
        assert st["restart_ip_hist"] == []
