"""沉睡池探测器的临时实例端口分配 —— 覆盖 2026-11-11 的实测事故。

事故链（实测，见 DESIGN-2026-11-11.md 第 12 部分）：
  `ip_local_port_range = 43255..47350`（仅 4096 个），112 条 WARP 线 + 大量
  探测出站把这段占得很满。旧 `free_port()` 用 bind(0) 拿到的必然是段内的
  "孤立空隙"，而旧 `build_tmp_cfg` 把第二个入站端口硬推成 `mixed+1`，
  从不校验 —— 实测连抽 20 组，`mixed+1` 有 16 组已被占用（80%）。
  结果临时 sing-box 每次都 FATAL 在 mixed-lan，`.dormant_state.json` 永远
  写不出来，沉睡池降级机制从部署起就没生效过。

每个测试都写成「回滚修复即失败」的形式。
"""
import importlib.util
import json
import os
import socket
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent


def _load_probe():
    """独立加载，不注册 sys.modules —— 避免污染用到 config 的其它测试。"""
    spec = importlib.util.spec_from_file_location(
        "dormant_probe_under_test", PROJECT / "dormant_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dp():
    return _load_probe()


def _can_bind(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _fake_live_cfg() -> dict:
    """最小现役配置：两个入站，正好复现事故里的 mixed-in / mixed-lan。"""
    return {
        "inbounds": [
            {"tag": "mixed-in", "type": "mixed", "listen": "0.0.0.0", "listen_port": 7890},
            {"tag": "mixed-lan", "type": "mixed", "listen": "0.0.0.0", "listen_port": 7891},
        ],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "experimental": {"clash_api": {}},
    }


# ------------------------------------------------------- ① 避开内核临时段

def test_free_port_avoids_kernel_ephemeral_range(dp):
    """回滚到 bind(0) 即失败：抽到的端口全在 43255..47350 里。"""
    lo, hi = dp._ephemeral_range()
    for _ in range(20):
        p = dp.free_port()
        assert not (lo <= p <= hi), f"{p} 落在内核临时端口段 {lo}..{hi}"
        assert _can_bind(p), f"free_port 返回的 {p} 当时已被占用"


def test_free_port_respects_exclude(dp):
    """clash API 与 mixed 同号会把整批探测打进 API，必须能排除。"""
    p = dp.free_port()
    for _ in range(10):
        assert dp.free_port({p}) != p


# ------------------------------------------------------- ② 每个入站独立抽端口

def test_build_tmp_cfg_assigns_bindable_distinct_ports(dp):
    """10 轮：入站端口互异、不与 clash 撞号、且每个此刻都能 bind。"""
    for _ in range(10):
        mixed = dp.free_port()
        clash = dp.free_port({mixed})
        cfg = _fake_live_cfg()
        path = dp.build_tmp_cfg("x", mixed, clash, cfg=cfg)
        try:
            ports = [i["listen_port"] for i in cfg["inbounds"]]
            assert ports[0] == mixed
            assert len(set(ports)) == len(ports), ports
            assert clash not in ports, (mixed, clash, ports)
            for p in ports:
                assert _can_bind(p), f"入站端口 {p} 已被占用"
        finally:
            os.unlink(path)


def test_second_inbound_does_not_hardcode_mixed_plus_one(dp):
    """回滚敏感：旧代码把第二入站端口写成 `mixed+1`。

    这里主动把 `mixed+1` 占住。旧实现会原样写进去并让临时实例 FATAL；
    新实现会独立抽端口并因为 bind 校验绕开它 —— 断言直接区分两者。
    """
    guard = None
    mixed = None
    for _ in range(40):
        candidate = dp.free_port()
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", candidate + 1))
        except OSError:
            s.close()
            continue
        guard, mixed = s, candidate
        break
    if guard is None:
        pytest.skip("本机没有可占住的 mixed+1，跳过（极端环境）")
    try:
        clash = dp.free_port({mixed, mixed + 1})
        cfg = _fake_live_cfg()
        path = dp.build_tmp_cfg("x", mixed, clash, cfg=cfg)
        try:
            second = cfg["inbounds"][1]["listen_port"]
            assert second != mixed + 1, "又硬推成了 mixed+1（回滚即失败）"
            assert second != mixed and second != clash
            assert _can_bind(second), f"绕开后挑到的 {second} 仍不可用"
        finally:
            os.unlink(path)
    finally:
        guard.close()


def test_second_inbound_never_lands_in_ephemeral_range(dp):
    """第二个入站端口也必须避开临时段；旧实现 mixed+1 必在段内。"""
    lo, hi = dp._ephemeral_range()
    for _ in range(10):
        mixed = dp.free_port()
        clash = dp.free_port({mixed})
        cfg = _fake_live_cfg()
        path = dp.build_tmp_cfg("x", mixed, clash, cfg=cfg)
        try:
            second = cfg["inbounds"][1]["listen_port"]
            assert not (lo <= second <= hi), f"第二入站 {second} 落回临时段"
        finally:
            os.unlink(path)
