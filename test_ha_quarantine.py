#!/usr/bin/env python3
"""额度隔离 / 全局重启节流 的确定性测试（无网络、无副作用、无第三方依赖）。

2026-10-28：撞长 Retry-After 的线不再重启，改为隔离到解禁；另加一道全局
每分钟重启上限。两个闸都在 _warp_line_restart() / _restart_warp_line() 里。
这里钉住九件事：
  1. 全局窗口满了就拒绝，窗口滑过恢复；cap=0 时永不拒绝；
  2. 隔离中的线不进重启路径；
  3. 隔离中的线连探测都不做；
  4. 隔离过期后不再以「隔离」为由拒绝；
  5. 隔离绑地址：换址即解禁；
  6. 单次隔离被 max_sec 夹顶；
  7. 比例闸到顶就不再新增隔离；
  8. ratio<=0 关闭比例闸（且 max_sec<=0 时不夹顶）。

跑法： python3 test_ha_quarantine.py    （不需要 pytest）
"""
import os
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="asip-ha-test-")
os.environ["ASIP_NO_SIDE_EFFECTS"] = "1"
os.environ.setdefault("WARP_LINE_STATE_FILE_PATH", os.path.join(_TMP, "state.json"))
os.environ.setdefault("WARP_IP_USED_FILE_PATH", os.path.join(_TMP, "used.json"))

import config
import daemon


def _reset():
    daemon._GLOBAL_RESTART_HIST[:] = []
    daemon._GLOBAL_RESTART_LOG_TS[0] = 0.0
    daemon._QUARANTINE_REFUSE_TS = 0.0


def test_restart_rate_allow_caps_per_minute():
    _reset()
    config.CFG["warp"]["restart_max_per_minute"] = 3
    now = 1_000_000.0
    assert [daemon._restart_rate_allow(now) for _ in range(3)] == [True, True, True]
    assert daemon._restart_rate_allow(now) is False
    assert daemon._restart_rate_allow(now + 61.0) is True


def test_restart_rate_allow_disabled_by_zero():
    _reset()
    config.CFG["warp"]["restart_max_per_minute"] = 0
    now = 2_000_000.0
    assert all(daemon._restart_rate_allow(now) for _ in range(50))


def test_quarantined_line_skips_restart():
    _reset()
    tag = "WARP-WG-T1"
    member = {"tag": tag, "port": 11999, "service": "warp-wg-t1.service"}
    config.CFG["warp"]["restart_max_per_minute"] = 0
    config.CFG["warp"]["restart_max_per_hour_per_line"] = 0
    orig_active, orig_restart = daemon.active_warp_tag, daemon._restart_warp_line
    called = []
    daemon.active_warp_tag = lambda: "WARP-CF"
    daemon._restart_warp_line = lambda *a, **k: called.append(a) or True
    try:
        daemon._warp_line_update(tag, quota_until=time.time() + 600,
                                 next_restart_ts=0.0, warming=False)
        assert daemon._warp_line_restart(member, "出口处于惩罚期") is False
        assert called == []
    finally:
        daemon.active_warp_tag, daemon._restart_warp_line = orig_active, orig_restart


def test_quarantined_line_is_not_probed():
    _reset()
    tag = "WARP-WG-T2"
    member = {"tag": tag, "port": 11998}
    orig = daemon.warp_egress_ip
    daemon.warp_egress_ip = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("隔离期内不该探测"))
    try:
        daemon._warp_line_update(tag, quota_until=time.time() + 600, warming=False)
        daemon._probe_one_line(member)
    finally:
        daemon.warp_egress_ip = orig


def test_expired_quarantine_no_longer_blocks():
    _reset()
    tag = "WARP-WG-T3"
    member = {"tag": tag, "port": 11997}
    config.CFG["warp"]["restart_max_per_minute"] = 0
    config.CFG["warp"]["restart_max_per_hour_per_line"] = 0
    orig = (daemon.active_warp_tag, daemon.warp_line_busy, daemon._acquire_refresh_slot)
    daemon.active_warp_tag = lambda: "WARP-CF"
    daemon.warp_line_busy = lambda *a, **k: False
    daemon._acquire_refresh_slot = lambda tag: False
    try:
        daemon._warp_line_update(tag, quota_until=time.time() - 1,
                                 next_restart_ts=0.0, warming=False)
        daemon._warp_line_restart(member, "出口处于惩罚期")
        assert daemon._warp_line_state(tag).get("quota_until", 0) <= time.time()
    finally:
        daemon.active_warp_tag, daemon.warp_line_busy, daemon._acquire_refresh_slot = orig


def test_quarantine_binds_to_address():
    """换到新地址后旧隔离作废（否则一次 16h 的 Retry-After 会把线锁到明天）。"""
    _reset()
    tag = "WARP-WG-T4"
    config.CFG["warp"]["line_quota_quarantine_max_ratio"] = 0.0
    daemon._warp_line_update(tag, ip="1.1.1.1", quota_until=time.time() + 600,
                             quota_ip="1.1.1.1", warming=False)
    assert daemon._warp_line_quarantined(daemon._warp_line_state(tag), time.time()) is True
    daemon._warp_line_update(tag, ip="2.2.2.2")
    assert daemon._warp_line_quarantined(daemon._warp_line_state(tag), time.time()) is False


def test_quarantine_clamped_by_max_sec():
    _reset()
    tag = "WARP-WG-T5"
    config.CFG["warp"]["line_quota_quarantine_max_sec"] = 600
    config.CFG["warp"]["line_quota_quarantine_max_ratio"] = 0.0
    now = time.time()
    daemon._quarantine_line(tag, "3.3.3.3", 86400, now)
    until = float(daemon._warp_line_state(tag).get("quota_until") or 0)
    assert abs(until - (now + 600)) < 2, until


def test_quarantine_ratio_blocks_when_pool_full():
    """比例闸到顶就不再新增隔离，保证池子里始终有活线。"""
    _reset()
    config.CFG["warp"]["line_quota_quarantine_max_sec"] = 7200
    config.CFG["warp"]["line_quota_quarantine_max_ratio"] = 0.01
    tag = "WARP-WG-T6"
    now = time.time()
    daemon._quarantine_line(tag, "4.4.4.4", 600, now)
    assert float(daemon._warp_line_state(tag).get("quota_until") or 0) == 0.0


def test_ratio_zero_disables_gate_and_no_clamp():
    _reset()
    config.CFG["warp"]["line_quota_quarantine_max_ratio"] = 0.0
    config.CFG["warp"]["line_quota_quarantine_max_sec"] = 0
    tag = "WARP-WG-T7"
    now = time.time()
    daemon._quarantine_line(tag, "5.5.5.5", 120, now)
    until = float(daemon._warp_line_state(tag).get("quota_until") or 0)
    assert abs(until - (now + 120)) < 2, until


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception as e:
            failed += 1
            print("FAIL", name, "->", type(e).__name__, e)
    print("")
    print(str(len(tests) - failed) + "/" + str(len(tests)) + " passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
