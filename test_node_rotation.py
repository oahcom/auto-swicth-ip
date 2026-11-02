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
        probed=True, ever_ok=True):
    """跑 N 轮轮回，返回 (换到的节点, 重启次数)。

    默认两轮：首次判死只记一笔、下一轮复查仍不通才换。

    `probed=False` → 模拟刚 apply 完、探测器还没碰过这条线；
    `ever_ok=False` → 模拟节点 TCP 通但从来没取到过出口（UDP 不通）。
    """
    moved, restarted = [], []
    import time as _t
    state = {"restart_hist": [_t.time()] * restarts,
             "last_probe_ts": (_t.time() - 300) if probed else 0.0,
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

print("✓ 11 项同块轮回自检通过")
