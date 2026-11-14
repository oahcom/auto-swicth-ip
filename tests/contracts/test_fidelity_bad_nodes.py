"""①组保真契约 · bad_nodes 三护栏（附件 A7）。

【防什么事故】删掉任一护栏 → 共享状态被抹或误判封死整池：
  ① total_count==0（Clash API 不通）时跳过导出但**不清已标记** —— 否则一次
     API 抖动抹掉 cron/proxy-global 写入的共享坏节点（原 bug）；
  ② 占比阀：死节点超过 (1-_POOL_MIN_RATIO) 的池子时跳过导出 —— 防全误判
     把整池封死；保底线保证永远留 _POOL_MIN_RATIO 的活口；
  ③ append-only merge + 共享 flock：不覆盖他方（cron）条目，read-modify-write
     必须互斥（无锁并发互相覆盖）。
【事故出处】附件 A7；「200 同刻」家族；误杀池 50/56。
【三分法】A 保真契约。（日志文案「60%」与实配 0.3 的漂移是③组，见
test_acceptance_config_drift.py；本组只钉护栏行为本身。）
"""
import json
import time

import pytest

import daemon as d


@pytest.fixture
def bad_nodes_file(monkeypatch, tmp_path):
    """把共享坏节点文件指到 tmp（写域只落 tmp，不碰生产 .bad_nodes.json）。"""
    p = tmp_path / "bad_nodes.json"
    monkeypatch.setattr(d, "_BAD_NODES_FILE", str(p))
    monkeypatch.setattr(d, "_shared_bad_cache", {})
    monkeypatch.setattr(d, "_shared_bad_mtime", 0.0)
    monkeypatch.setattr(d, "_shared_bad_check_ts", 0.0)
    monkeypatch.setattr(d, "_shared_bad_loaded", False)
    return p


def test_zero_total_skips_export_but_keeps_marks(bad_nodes_file):
    """Clash API 不通/节点数 0 → 跳过导出、不清已标记（附件 A7 护栏①）。"""
    d._DEAD_NODES.add("n1")
    d._DEAD_NODES_RELEASE_TS["n1"] = time.time() + 300
    with d._state_lock:
        d._export_dead_nodes_locked(total_count=0)
    assert "n1" in d._DEAD_NODES  # 已标记状态保留
    assert not bad_nodes_file.exists()  # 未导出


def test_ratio_guard_skips_export_when_pool_mostly_dead(bad_nodes_file):
    """死节点占比越过 (1-_POOL_MIN_RATIO) 阀 → 跳过导出（附件 A7 护栏②）。

    防什么事故：全池误判（探测器/上游抖动）时把整池写进共享坏节点文件。
    """
    total = 10
    threshold = total * (1 - d._POOL_MIN_RATIO)
    for i in range(int(threshold) + 1):  # 越过阀
        d._DEAD_NODES.add(f"n{i}")
        d._DEAD_NODES_RELEASE_TS[f"n{i}"] = time.time() + 300
    with d._state_lock:
        d._export_dead_nodes_locked(total_count=total)
    assert not bad_nodes_file.exists()  # 阀上跳过


def test_export_writes_when_below_ratio(bad_nodes_file):
    """占比在阀内正常导出（护栏不误伤）。"""
    d._DEAD_NODES.add("n1")
    d._DEAD_NODES_RELEASE_TS["n1"] = time.time() + 300
    with d._state_lock:
        d._export_dead_nodes_locked(total_count=10)
    data = json.loads(bad_nodes_file.read_text())
    assert "n1" in data and data["n1"].get("reason") == "daemon_dead"


def test_merge_append_only_never_overwrites_others(bad_nodes_file):
    """append-only merge：不覆盖 cron/proxy-global 已有条目（附件 A7 护栏③）。
    """
    other = {"expire": time.time() + 999, "reason": "cron_said_so",
             "extra": "keep-me"}
    bad_nodes_file.write_text(json.dumps({"n-cron": other,
                                          "n-old": {"expire": 1.0,
                                                    "reason": "daemon_dead"}}))
    d._DEAD_NODES.update({"n-cron", "n-new"})
    d._DEAD_NODES_RELEASE_TS["n-cron"] = time.time() + 500
    d._DEAD_NODES_RELEASE_TS["n-new"] = time.time() + 500
    with d._state_lock:
        d._export_dead_nodes_locked(total_count=10)
    data = json.loads(bad_nodes_file.read_text())
    assert data["n-cron"] == other  # 他方条目原样，不覆盖
    assert data["n-new"]["reason"] == "daemon_dead"


def test_load_filters_expired_and_over_max_ttl(bad_nodes_file):
    """加载过滤：过期、超 MAX_TTL、畸形条目不入缓存（附件 A7 判据侧）。"""
    now = time.time()
    bad_nodes_file.write_text(json.dumps({
        "expired": now - 1,
        "ok1": now + 30,
        "over-max": now + d._DEAD_TTL_MAX + 10_000,
        "dict-ok": {"expire": now + 60},
        "junk": "text",
    }))
    d._shared_bad_check_ts = 0.0
    got = d._load_shared_bad_nodes()
    assert set(got) == {"ok1", "dict-ok"}


def test_pool_floor_prevents_blacklisting_last_strands():
    """保底阀：活节点 ≤ _POOL_MIN_RATIO 时不再拉黑（防封死整池，附件 A7）。
    """
    nodes = [f"m{i}" for i in range(10)]
    for i in range(7):  # 已死 7/10 → 活 3 = 10*0.3 触底
        d._DEAD_NODES.add(nodes[i])
    results = {n: None for n in nodes}  # 全部测失败
    d._apply_test_results(nodes, results, time.time())
    assert len(d._DEAD_NODES) == 7  # 触底后不再新增死节点
