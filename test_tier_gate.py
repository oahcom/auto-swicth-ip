#!/usr/bin/env python3
"""档位（tiers.json）必须贯穿「探测 / 换节点 / 重定位 / 解禁」全链路。

2026-11-09 的现场：远端 `tiers.json` 是 `h2-only`（`wg: ""`），WG 单元也确实
被停掉了，但 `asip chan` 显示「档位停用」的同时，日志里还在刷
「档位停用中，跳过重启（渠道重定位）」，状态表里 fail_streak 冻着上千次。

三个缺陷：
1. 探测只判「单元在不在跑」，不判档位 —— 单元漏停时照探；
2. 同块轮回 / 渠道重定位照旧遍历档位外的线，`_rehome_one` 会**写节点配置**
   并改台账，重启才被最后一道闸拦住，解禁后线已经跑偏；
3. 解禁只改文件：`_TIER_DISABLED` 是 import 期常量、`_tier_reconcile` 只停
   不启，且停用期冻住的 fail_streak / last_verdict / ip 原样带回池子 ——
   第一轮探测就按旧结论判死，解禁即炸。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="asip-tier-test-")
os.environ["ASIP_NO_SIDE_EFFECTS"] = "1"
os.environ.setdefault("WARP_LINE_STATE_FILE_PATH", os.path.join(_TMP, "state.json"))
os.environ.setdefault("WARP_IP_USED_FILE_PATH", os.path.join(_TMP, "used.json"))
os.environ.setdefault("WARP_SEGMENT_FILE_PATH", os.path.join(_TMP, "seg.json"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import daemon  # noqa: E402


def _tiers(tmp_path: Path, wg: str, current: str = "t") -> Path:
    p = tmp_path / ".warp" / "tiers.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"current": current,
                             "tiers": {current: {"wg": wg}}}))
    return p


def test_refresh_tier_disabled_picks_up_file_change(monkeypatch, tmp_path):
    """改 tiers.json 必须热生效，不需要重启 daemon。"""
    monkeypatch.setattr(daemon.warp_blocks, "map_path",
                        lambda: tmp_path / ".warp" / "block-map.json")
    monkeypatch.setattr(daemon, "_load_tier_disabled",
                        lambda: {"WARP-WG-1", "WARP-WG-2"})
    p = _tiers(tmp_path, "")
    monkeypatch.setattr(daemon, "_TIER_MTIME", p.stat().st_mtime)
    monkeypatch.setattr(daemon, "_TIER_DISABLED", {"WARP-WG-1"})
    assert daemon._refresh_tier_disabled() is False, "没改文件不该重算"
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert daemon._refresh_tier_disabled() is True, "文件变了必须重算"
    assert daemon._TIER_DISABLED == {"WARP-WG-1", "WARP-WG-2"}


def test_node_rotation_skips_tier_disabled(monkeypatch):
    """档位外的线不换节点 —— `_rehome_one` 会写配置和台账。"""
    monkeypatch.setattr(daemon, "_TIER_DISABLED", {"WARP-WG-3"})
    daemon._NODE_NEXT_CHECK.clear()
    written = []
    monkeypatch.setattr(daemon.warp_wg, "_read_state", lambda: {"lines": {
        "WARP-WG-3": {"node": "dead-node", "family": "v6"}}})
    monkeypatch.setattr(daemon, "_warp_member", lambda t: {
        "tag": t, "kind": "wg", "index": 3, "service": "warp-wg-3.service"})
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: None)
    monkeypatch.setattr(daemon, "_rehome_one",
                        lambda tag, node: written.append((tag, node)) or "ok")
    daemon._node_rotation_pass(now=time.time())
    assert written == [], "档位停用的线不许被换节点"


def test_rehome_held_lines_skips_tier_disabled(monkeypatch):
    """渠道重定位同样不许碰档位外的线。"""
    monkeypatch.setattr(daemon, "_TIER_DISABLED", {"WARP-WG-4"})
    monkeypatch.setattr(daemon, "_warp",
                        lambda: {"channel_ban_hold_enabled": True})
    now = time.time()
    daemon._WARP_LINE_STATE.clear()
    daemon._WARP_LINE_STATE["WARP-WG-4"] = {
        "ban_hold_until": now + 3600, "ban_hold_home": "home-node",
        "ban_hold_node": "other-node"}
    touched = []
    monkeypatch.setattr(daemon, "_warp_member", lambda t: {
        "tag": t, "kind": "wg", "index": 4, "service": "warp-wg-4.service"})
    monkeypatch.setattr(daemon, "_rehome_one",
                        lambda tag, node: touched.append(tag) or "ok")
    monkeypatch.setattr(daemon, "_warp_line_restart",
                        lambda *a, **k: touched.append("restart"))
    monkeypatch.setattr(daemon, "_backfill_reconcile", lambda now: None)
    monkeypatch.setattr(daemon, "_backfill_open", lambda *a: None)
    daemon._rehome_held_lines(now)
    assert touched == [], "档位停用的线不许重定位/重启"


def test_reset_stale_after_tier_clears_frozen_observation(monkeypatch):
    """解禁拉起时清掉停用期冻住的结论，否则第一轮探测按旧账判死。"""
    daemon._WARP_LINE_STATE.clear()
    daemon._WARP_LINE_STATE["WARP-WG-5"] = {
        "fail_streak": 4891, "last_verdict": "quota", "ip": "2a09:bac5::1",
        "last_ok_ip": "2a09:bac5::1", "quota_until": time.time() + 36000,
        "ban_noaddr_since": time.time() - 7200, "ban_probes": [[0, 0, 3, "quota"]],
        "stop_probe": True, "ban_hold_until": time.time() + 3600,
        "next_probe_ts": time.time() + 3600,
    }
    daemon._reset_stale_after_tier("WARP-WG-5")
    st = daemon._WARP_LINE_STATE["WARP-WG-5"]
    assert st["fail_streak"] == 0
    assert st["last_verdict"] == ""
    assert st["ip"] is None and st["last_ok_ip"] is None
    assert st["quota_until"] == 0.0 and st["ban_noaddr_since"] == 0.0
    assert st["ban_probes"] == [] and st["stop_probe"] is False
    assert st["ban_hold_until"] == 0.0 and st["next_probe_ts"] == 0.0


def test_tier_reconcile_starts_and_resets_when_tier_reenables(monkeypatch):
    """解禁侧：档位内的线没在跑就拉起来，并清空停用期的陈旧观测。"""
    class _R:
        returncode = 0
        stdout = "inactive"

    monkeypatch.setattr(daemon.warp_blocks, "tier_spec", lambda: ("wg-on", "1-22"))
    monkeypatch.setattr(daemon, "_TIER_DISABLED", set())
    monkeypatch.setattr(daemon, "_warp", lambda: {"members": [
        {"tag": "WARP-WG-4", "kind": "wg", "index": 4,
         "service": "warp-wg-4.service"}]})
    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: _R())
    started, reset = [], []
    monkeypatch.setattr(daemon, "_unit_ctl",
                        lambda action, svc: started.append((action, svc)) or _R())
    monkeypatch.setattr(daemon, "_wg_line_state", lambda i: "installed")
    monkeypatch.setattr(daemon, "_reset_stale_after_tier", lambda tag: reset.append(tag))
    daemon._tier_reconcile()
    assert started == [("start", "warp-wg-4.service")], "档位内的线必须被拉起"
    assert reset == ["WARP-WG-4"], "拉起时必须复位陈旧观测"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {name}: {e}")
    print("all passed" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
