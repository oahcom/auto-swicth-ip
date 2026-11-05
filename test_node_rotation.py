#!/usr/bin/env python3
"""同块节点轮回的离线自检（不联网、不碰 systemd）。

    python3 test_node_rotation.py
"""
import copy
import sys
from unittest.mock import patch

sys.path.insert(0, "/home/administrator/auto-swicth-ip")
import daemon  # noqa: E402
import warp_blocks  # noqa: E402

NODES = {
    "a.nrt:443-any": {"colo": "NRT", "category": "regular"},
    "b.nrt:443-any": {"colo": "NRT", "category": "regular"},
    "c.nrt:443-any": {"colo": "NRT", "category": "irregular"},
    "d.hkg:443-any": {"colo": "HKG", "category": "regular"},
}
LINE = {"lines": {"WARP-WG-7": {
    "node": "a.nrt:443-any", "home": "a.nrt:443-any", "family": "v6",
    "rotation": ["a.nrt:443-any", "b.nrt:443-any",
                 "d.hkg:443-any", "c.nrt:443-any"],
}}}


def run(tcp, active=None, enabled=True, rounds=2, restarts=0,
        probed=True, ever_ok=True, fail_streak=0, just_restarted=False):
    """跑 N 轮轮回，返回 (换到的节点, 重启次数)。

    默认两轮：首次判死只记一笔、下一轮复查仍不通才换。

    `probed=False` → 模拟刚 apply 完、探测器还没碰过这条线；
    `ever_ok=False` → 模拟节点 TCP 通但从来没取到过出口（UDP 不通）；
    `just_restarted=True` → `restart_ts` 就是刚刚（冷静期生效中）。
    """
    moved, restarted = [], []
    import time as _t
    state = {"restart_hist": [_t.time()] * restarts,
             "last_probe_ts": (_t.time() - 300) if probed else 0.0,
             "restart_ts": _t.time() if just_restarted else 0.0,
             "fail_streak": fail_streak,
             # `ip` 才是「隧道建起来了」的证据：`last_ok_ts` 在 verdict=blocked
             # 时会被清零（出口 IP 被限流 ≠ 隧道不通），拿它当节点判据会把
             # 健康线全判死。
             "ip": "2a09:bac1::1" if ever_ok else None}
    daemon._NODE_NEXT_CHECK.clear()
    daemon._NODE_FAILS.clear()
    daemon._NODE_TCP_SEEN.clear()
    with patch.object(daemon, "_warp",
                      return_value={"node_rotation_enabled": enabled,
                                    "restart_max_per_hour_per_line": 20,
                                    "members": []}), \
         patch.object(daemon, "_warp_line_state",
                      side_effect=lambda t: state), \
         patch.object(daemon, "_warp_line_state_locked",
                      return_value={}), \
         patch.object(daemon.warp_wg, "_read_state",
                      side_effect=lambda: copy.deepcopy(LINE)), \
         patch.object(warp_blocks, "load_map", return_value=NODES), \
         patch.object(daemon, "active_warp_tag", return_value=active), \
         patch.object(daemon, "_rehome_one",
                      side_effect=lambda t, n: (moved.append(n), "ok")[1]), \
         patch.object(daemon, "_warp_member",
                      return_value={"tag": "WARP-WG-7",
                                    "service": "warp-wg-7.service"}), \
         patch.object(daemon, "_night_block_restart", return_value=False), \
         patch.object(daemon, "_warp_line_restart",
                      side_effect=lambda m, r, rebound=False:
                          (restarted.append(m["tag"]), True)[1]), \
         patch.object(daemon, "_node_tcp_ok",
                      side_effect=lambda n: tcp.get(n, True)):
        for i in range(rounds):
            if i:
                # 模拟时间推进：清掉节流表，否则第二轮被 interval 挡住。
                daemon._NODE_NEXT_CHECK.clear()
            daemon._node_rotation_pass()
    return (moved[0] if moved else None), len(restarted)


# 1) 当前节点通 → 什么都不做
assert run({"a.nrt:443-any": True}) == (None, 0), "健康节点不该被动"

# 2) 当前节点不通 → 换同块第一个可达的，并重启
assert run({"a.nrt:443-any": False, "b.nrt:443-any": True}) == \
    ("b.nrt:443-any", 1), "不通时应换同块可用节点"

# 3) 同块候选全不通 → 才允许跨块
assert run({"a.nrt:443-any": False, "b.nrt:443-any": False,
            "c.nrt:443-any": False, "d.hkg:443-any": True}) == \
    ("d.hkg:443-any", 1), "同块全灭才跨块"

# 4) 当前出口不换节点（会掐断在途连接）
assert run({"a.nrt:443-any": False, "b.nrt:443-any": True},
           active="WARP-WG-7") == (None, 0), "当前出口不该被换节点"

# 5) 开关关闭时不动
assert run({"a.nrt:443-any": False, "b.nrt:443-any": True},
           enabled=False) == (None, 0), "开关关闭应完全不动作"

# 6) 节点可达性无法判断（解析失败）→ 保守不动
assert run({"a.nrt:443-any": None}) == (None, 0), "未知状态不该触发换节点"


# 7) 节点通、但本小时重启已打满（出口长期不可用）→ 换节点换地址池
# 这一档第一轮就换：重启打满本身就是「已确认不可用」，不需要二次确认
assert run({"a.nrt:443-any": True, "b.nrt:443-any": True},
           restarts=20, rounds=1) == \
    ("b.nrt:443-any", 1), "节点通但出口长期不可用也应换节点"

# 8) 节点通、重启次数没打满 → 不动（把机会留给探测器换 IP）
assert run({"a.nrt:443-any": True, "b.nrt:443-any": True}, restarts=3) == \
    (None, 0), "重启未打满时应交给探测器，不换节点"

# 9) 节点 TCP 通、但这条线没出过口 → 第 3 轮确认后换节点（UDP 不通的节点）
# 实测 2026-11-02：WG-9/21/22 起不来，而 _node_tcp_ok 对它们全判健康。
# 3 轮是下限：last_ok_ts 在 daemon 重启时会被主动清零（旧证据一律作废），
# 所以「为 0」本身不说明问题，必须本进程内连续观察够轮次。
assert run({"a.nrt:443-any": True, "b.nrt:443-any": True},
           ever_ok=False, rounds=3) == \
    ("b.nrt:443-any", 1), "TCP 通但未出过口，第 3 轮应换节点"

# 10) 只跑两轮不动：刚重启 / 刚 apply 完的线不该被立刻换掉
assert run({"a.nrt:443-any": True, "b.nrt:443-any": True},
           ever_ok=False, rounds=2) == \
    (None, 0), "前两轮只观察，不该换"

# 10b) 冷静期不能因为「一直在重启」而永久成立。
# 冷静期为的是给刚重启的隧道几十秒起来；但一条线反复重启、每次都还是取不到
# 出口，这个前提就不成立了 —— 而每次重启都把 `restart_ts` 顶到现在，冷静期
# 便永远成立。实测 2026-11-05 WARP-WG-4 `fail_streak` 13、每 ~40s 重启一次、
# `restart_ts` 永远是新的，轮回日志里从没出现过它。
assert run({"a.nrt:443-any": True, "b.nrt:443-any": True},
           ever_ok=False, rounds=3, just_restarted=True,
           fail_streak=daemon._UNKNOWN_RESTART_STREAK) == \
    ("b.nrt:443-any", 1), \
    "反复重启仍取不到出口时，冷静期必须让路给换节点"
# 对照：同样的位形、但没在反复重启（fail_streak 未达阈值）→ 冷静期照旧生效。
assert run({"a.nrt:443-any": True, "b.nrt:443-any": True},
           ever_ok=False, rounds=3, just_restarted=True,
           fail_streak=daemon._UNKNOWN_RESTART_STREAK - 1) == \
    (None, 0), "刚重启且没在反复失败时，冷静期仍然要挡住换节点"

# 11) 换节点必须带 rebound=True 去重启
# 换完不重启 = 配置写进文件、进程还在跑旧节点：线照样死，而守护以为自己
# 已经救过它了（实测 2026-11-02 WG-22 换到 ~33 后一直没重启，被夜间渠道锁
# 拦在 `_warp_line_restart` 内部）。
seen_rebound = []
with patch.object(daemon, "_warp",
                  return_value={"node_rotation_enabled": True,
                                "restart_max_per_hour_per_line": 20,
                                "members": []}), \
     patch.object(daemon, "_warp_line_state",
                  side_effect=lambda t: {"restart_hist": [], "ip": None}), \
     patch.object(daemon, "_warp_line_state_locked", return_value={}), \
     patch.object(daemon.warp_wg, "_read_state",
                  side_effect=lambda: copy.deepcopy(LINE)), \
     patch.object(warp_blocks, "load_map", return_value=NODES), \
     patch.object(daemon, "active_warp_tag", return_value=None), \
     patch.object(daemon, "_rehome_one", return_value="ok"), \
     patch.object(daemon, "_warp_member",
                  return_value={"tag": "WARP-WG-7",
                                "service": "warp-wg-7.service"}), \
     patch.object(daemon, "_night_block_restart", return_value=True), \
     patch.object(daemon, "_warp_line_restart",
                  side_effect=lambda m, r, rebound=False:
                      (seen_rebound.append(rebound), True)[1]), \
     patch.object(daemon, "_node_tcp_ok", return_value=True):
    daemon._NODE_NEXT_CHECK.clear()
    daemon._NODE_FAILS.clear()
    daemon._NODE_TCP_SEEN.clear()
    for _ in range(3):
        daemon._NODE_NEXT_CHECK.clear()
        daemon._node_rotation_pass()
assert seen_rebound == [True], f"换节点后必须以 rebound=True 重启，实际 {seen_rebound}"

# 12) 被渠道锁住的线：换节点后要把锁的目标节点同步过去，否则每分钟被写回
# 实测 WG-21 08:50~11:50 被 `同块轮回` 与 `_rehome_held_lines` 来回改了 6 次，
# 每次白搭一条「渠道重定位」重启。`ban_hold_home` 不能动：08:00 仍要还原到
# 锁定前的原节点。
import time as _t
updates = {}
hold = _t.time() + 3600
with patch.object(daemon, "_warp",
                  return_value={"node_rotation_enabled": True,
                                "restart_max_per_hour_per_line": 20,
                                "members": []}), \
     patch.object(daemon, "_warp_line_state",
                  side_effect=lambda t: {"restart_hist": [], "ip": "2a09:bac1::1",
                                         "ban_hold_until": hold}), \
     patch.object(daemon, "_warp_line_state_locked", return_value={}), \
     patch.object(daemon, "_warp_line_update",
                  side_effect=lambda t, **kw: updates.update(kw)), \
     patch.object(daemon.warp_wg, "_read_state",
                  side_effect=lambda: copy.deepcopy(LINE)), \
     patch.object(warp_blocks, "load_map", return_value=NODES), \
     patch.object(daemon, "active_warp_tag", return_value=None), \
     patch.object(daemon, "_rehome_one", return_value="ok"), \
     patch.object(daemon, "_warp_member",
                  return_value={"tag": "WARP-WG-7",
                                "service": "warp-wg-7.service"}), \
     patch.object(daemon, "_warp_line_restart", return_value=True), \
     patch.object(daemon, "_node_tcp_ok",
                  side_effect=lambda n: n != "a.nrt:443-any"):
    daemon._NODE_NEXT_CHECK.clear()
    daemon._NODE_FAILS.clear()
    for _ in range(2):
        daemon._NODE_NEXT_CHECK.clear()
        daemon._node_rotation_pass()
assert updates.get("ban_hold_node") == "b.nrt:443-any", \
    f"锁定中的线换节点后要同步锁目标，实际 {updates}"
assert "ban_hold_home" not in updates, "还原目标不能动"
assert updates.get("ban_hold_ready"), "换节点重启成功要记 ready，免得再补一次重启"

# 13) 候选排序必须把「实测承载不了 WARP」的压到最后。
# 实测 2026-11-04：WG-1 在 HKG/NRT 两个 wg=0/4 的节点之间来回换了 12 次，
# 每次都「节点 TCP 通但拿不到出口」；39 次同块轮回里 5 分钟内拿到出口的只有
# 15 次（38%）。rotation 176 个槽位里 135 个从没测过，凭块序选等于赌。
DEAD_FIRST = {k: dict(v) for k, v in NODES.items()}
DEAD_FIRST["b.nrt:443-any"].update({"wg_ok": 0, "wg_n": 4})   # 实测不通
DEAD_FIRST["c.nrt:443-any"].update({"wg_ok": 2, "wg_n": 2})   # 实测可用
with patch.object(daemon, "_warp",
                  return_value={"node_rotation_enabled": True,
                                "restart_max_per_hour_per_line": 20,
                                "members": []}), \
     patch.object(daemon, "_warp_line_state",
                  return_value={"restart_hist": [], "ip": "2a09:bac1::1"}), \
     patch.object(daemon, "_warp_line_state_locked", return_value={}), \
     patch.object(daemon.warp_wg, "_read_state",
                  side_effect=lambda: copy.deepcopy(LINE)), \
     patch.object(warp_blocks, "load_map", return_value=DEAD_FIRST), \
     patch.object(daemon, "active_warp_tag", return_value=None), \
     patch.object(daemon, "_node_tcp_ok", return_value=True):
    got = daemon._same_block_standby("WARP-WG-7", 1_000_000.0)
assert got == "c.nrt:443-any", \
    f"应跳过块序在前但实测不通的 b，选实测可用的 c，实际 {got}"

# 14) 没测过（rank 1）不能因为「块序在后」被实测不通的（rank 2）顶掉 ——
# 未知不等于坏，把它排到实测不可用之后但仍可被选中。
MIXED = {k: dict(v) for k, v in NODES.items()}
MIXED["b.nrt:443-any"].update({"wg_ok": 0, "wg_n": 4})
with patch.object(daemon.warp_wg, "_read_state",
                  side_effect=lambda: copy.deepcopy(LINE)), \
     patch.object(warp_blocks, "load_map", return_value=MIXED), \
     patch.object(daemon, "_node_tcp_ok", return_value=True):
    got = daemon._same_block_standby("WARP-WG-7", 1_000_000.0)
assert got == "c.nrt:443-any", f"没测过的 c 应排在实测不通的 b 之前，实际 {got}"

# 15) 全线都没测过时照常按块序选第一个 —— 过滤会让人工刚加的节点永远上不了线。
UNMEASURED = {k: dict(v) for k, v in NODES.items()}
with patch.object(daemon.warp_wg, "_read_state",
                  side_effect=lambda: copy.deepcopy(LINE)), \
     patch.object(warp_blocks, "load_map", return_value=UNMEASURED), \
     patch.object(daemon, "_node_tcp_ok", return_value=True):
    got = daemon._same_block_standby("WARP-WG-7", 1_000_000.0)
assert got == "b.nrt:443-any", f"全未知时按块序，实际 {got}"

# 16) 当前节点**不在块表里**时不能直接放弃选点。
# 实测 2026-11-05：WG-3/WG-4 的节点 `downloadcfpro…~9-vl` / `~10-vl` 从没跑过
# diag，块表里没有它们 ⇒ `home_colo is None` 分支原来直接 return None，两条线
# 永远换不了节点，只在同一个「收 TCP 不转发 UDP:2408」的节点上无限重启
# （fail_streak 197 / 88，日志每分钟一条「连续 N 次取不到出口地址」）。
# 块信息缺席不等于没有候选：退化为按承载能力排序，rank 0 照样排到前面。
NO_HOME = {"a.nrt:443-any": {"colo": "NRT", "category": "regular"},
           "b.nrt:443-any": {"wg_ok": 2, "wg_n": 2},      # 块表里没 colo
           "c.nrt:443-any": {"wg_ok": 0, "wg_n": 4},
           "d.hkg:443-any": {}}
LINE_GHOST = {"lines": {"WARP-WG-7": {
    "node": "zz.ghost:443-any",          # 块表里根本没有当前节点
    "home": "zz.ghost:443-any", "family": "v6",
    "rotation": ["zz.ghost:443-any", "c.nrt:443-any",
                 "d.hkg:443-any", "b.nrt:443-any"],
}}}
with patch.object(daemon.warp_wg, "_read_state",
                  side_effect=lambda: copy.deepcopy(LINE_GHOST)), \
     patch.object(warp_blocks, "load_map", return_value=NO_HOME), \
     patch.object(daemon, "_node_tcp_ok", return_value=True):
    got = daemon._same_block_standby("WARP-WG-7", 1_000_000.0)
assert got == "b.nrt:443-any", \
    f"当前节点不在块表时仍应按承载能力选点，实际 {got}"

# 17) 单条节点的探测超时不能把整轮轮回打成异常。
# 实测 2026-11-05 3 小时内 18 条 `✗ 同块轮回异常: timed out` —— 一条坏节点
# 在 `probe_node_tcp` 的 recv 上超时，异常一路冒到 `_node_rotation_pass` 的
# try 外面，本轮排在它后面的线全部白等一轮。
import socket as _socket

import warp_wg as _ww


class _SlowSock:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sendall(self, _b):
        pass

    def recv(self, _n):
        raise _socket.timeout("timed out")

    def close(self):
        pass


with patch.object(_ww.subprocess, "Popen"), \
        patch.object(_ww.os, "makedirs"), \
        patch.object(_ww, "write_json"), \
        patch.object(_ww.socket, "create_connection",
                     side_effect=lambda *a, **k: _SlowSock()):
    try:
        got = _ww.probe_node_tcp({"tag": "x", "type": "socks",
                                  "server": "1.2.3.4", "server_port": 443},
                                 timeout=0.1)
    except Exception as e:                                  # noqa: BLE001
        raise AssertionError(f"探测超时应返回 False，不该抛出：{e!r}") from e
assert got is False, f"端口接受但不转发时结论应是 False，实际 {got!r}"

print("✓ 17 项同块轮回自检通过")
