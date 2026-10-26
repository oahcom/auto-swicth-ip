"""状态文件覆盖保护：空内存不得抹掉盘上已有记录。"""
import json

import daemon


def test_line_state_not_clobbered_by_empty_memory(tmp_path):
    p = tmp_path / "line_state.json"
    p.write_text(json.dumps(
        {"version": 1, "lines": {"W2": {"ip": "1.1.1.1"}}}), encoding="utf-8")
    saved = dict(daemon._WARP_LINE_STATE)
    daemon._WARP_LINE_STATE.clear()
    try:
        daemon._save_warp_line_state(force=True)
    finally:
        daemon._WARP_LINE_STATE.update(saved)
    assert json.loads(p.read_text())["lines"]["W2"]["ip"] == "1.1.1.1"


def test_ip_ledger_not_clobbered_by_empty_memory(tmp_path):
    p = tmp_path / "ip_used.json"
    p.write_text(json.dumps(
        {"used": {"1.1.1.1": {"ban_until": 9999999999}}}), encoding="utf-8")
    saved = dict(daemon._WARP_IP_USED)
    daemon._WARP_IP_USED.clear()
    try:
        daemon._save_warp_ip_used()
    finally:
        daemon._WARP_IP_USED.update(saved)
    assert "1.1.1.1" in json.loads(p.read_text())["used"]


def test_segment_start_survives_restart_for_same_ip(monkeypatch):
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", "1.1.1.1")
    saved = daemon._last_warp_switch_ok_ts
    try:
        daemon._begin_segment(1_700_000_000.0)
        assert daemon._load_segment_start(
            "1.1.1.1", now=1_700_000_100.0) == 1_700_000_000.0
        assert daemon._load_segment_start(
            "2.2.2.2", now=1_700_000_100.0) == 0.0
    finally:
        daemon._last_warp_switch_ok_ts = saved
