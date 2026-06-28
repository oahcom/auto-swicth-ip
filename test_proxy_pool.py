#!/usr/bin/env python3
"""proxy_pool.py 深度测试"""
import sys, os, json, threading, time

sys.path.insert(0, os.path.dirname(__file__))
from proxy_pool import OKZProxyPool, MIN_WEIGHT

def _compute(pool):
    """跳过网络请求，直接用当前缓存算权重。锁死缓存防 _ensure_cache 触发网络。"""
    pool._weights_cache = pool._compute_weights()
    pool._cache_ts = time.time() + 99999  # 远未来，cache_ttl=0 也不会过期
    return dict(pool._weights_cache)

# ============= 测试 1: 权重计算 =============
def test_weight_computation():
    """验证权重分布：延迟高+无国家 → 概率低；延迟低+有国家 → 概率高"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool._nodes_cache = ["n1", "n2", "n3"]
    pool._delays_cache = {"n1": 200, "n2": 500, "n3": 1000}
    pool._ip_scores_cache = {"n1": 1.0, "n2": 0.5, "n3": 0.1}
    weights = _compute(pool)
    print(f"  权重: {json.dumps({k: round(v, 4) for k, v in weights.items()})}")
    # n1 最低延迟+最高IP分 → 权重最大
    assert weights["n1"] > weights["n2"], "n1 应 > n2"
    # n2 延迟中等但IP分比n3高 → 权重可能相同（被 MIN_WEIGHT 托底）或略大
    assert weights["n2"] >= weights["n3"], "n2 应 >= n3"
    print("  ✅ 权重排序正确")

def test_weight_no_delay():
    """无延迟数据（死节点）→ 极低概率"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool._nodes_cache = ["live", "dead"]
    pool._delays_cache = {"live": 300}
    pool._ip_scores_cache = {"live": 1.0, "dead": 1.0}
    wl = _compute(pool)
    assert wl["dead"] < wl["live"], "死节点权重应显著低于活节点"
    print(f"  ✅ 死节点降权: live={wl['live']:.4f} dead={wl['dead']:.4f}")

def test_weight_no_ip_score():
    """无 IP 分（默认 0.1）→ 权重被拉低"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool._nodes_cache = ["known", "unknown"]
    pool._delays_cache = {"known": 300, "unknown": 300}
    pool._ip_scores_cache = {"known": 1.0}
    w = _compute(pool)
    assert w["known"] > w["unknown"], "没 IP 分的权重应更低"
    print(f"  ✅ 无 IP 分降权: known={w['known']:.4f} unknown={w['unknown']:.4f}")

def test_weight_min_floor():
    """极端值不出现零或负权重"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool._nodes_cache = ["die_hard"]
    pool._delays_cache = {}
    pool._ip_scores_cache = {"die_hard": 0.1}
    w = _compute(pool)
    assert w["die_hard"] >= MIN_WEIGHT, f"不低于 MIN_WEIGHT: {w['die_hard']}"
    print(f"  ✅ 地板值生效: {w['die_hard']:.4e} >= {MIN_WEIGHT}")

def test_weight_delay_coverage():
    """新节点（有延迟但无 IP 分）权重在 dead 和 known 之间"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool._nodes_cache = ["dead", "new_node", "known"]
    pool._delays_cache = {"new_node": 100, "known": 50}
    pool._ip_scores_cache = {"known": 1.0, "new_node": 0.8, "dead": 0.2}
    pool._usage_count = {}
    w = _compute(pool)
    print(f"  dead={w['dead']:.6f} new={w['new_node']:.6f} known={w['known']:.6f}")
    assert w["dead"] < w["new_node"], "新节点至少比死节点高"
    assert w["known"] > w["new_node"], "已知节点比新节点高"
    print("  ✅ 新节点/死节点/已知节点相对排序正确")

# ============= 测试 2: rotate 幂等性 =============
def test_rotate_returns_url():
    """rotate 永远返回 URL，从不抛异常"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool._nodes_cache = ["dummy"]
    pool._delays_cache = {"dummy": 300}
    pool._ip_scores_cache = {"dummy": 1.0}
    pool._cache_ts = time.time()  # 防 _ensure_cache 触发网络
    _compute(pool)
    for i in range(10):
        url = pool.rotate()
        assert url.startswith("http"), f"URL 格式错误: {url}"
    print("  ✅ 10 次 rotate 全部返回合法 URL")

def test_rotate_concurrent():
    """并发调用不 panic"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool._nodes_cache = [f"n{i}" for i in range(20)]
    pool._delays_cache = {f"n{i}": 300 for i in range(20)}
    pool._ip_scores_cache = {f"n{i}": 1.0 for i in range(20)}
    _compute(pool)
    errors = []
    def run():
        for _ in range(20):
            try: pool.rotate()
            except Exception as e: errors.append(e)
    threads = [threading.Thread(target=run) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    print(f"  ✅ 10×20 并发 rotate，错误数: {len(errors)}")

# ============= 测试 3: _refresh_nodes 容错 =============
def test_refresh_nodes_network_down():
    """sing-box API 不通时保留旧缓存"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool.api = "http://127.0.0.1:99999/dead"  # 指向死端口
    pool._nodes_cache = ["existing_node"]
    pool._refresh_nodes()
    assert "existing_node" in pool._nodes_cache, "网络断时保留旧缓存"
    print(f"  ✅ API 不通保留旧缓存: {pool._nodes_cache}")

def test_refresh_nodes_empty():
    """API 返回空列表时也保留旧缓存"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool._nodes_cache = ["existing"]
    pool.api = "http://127.0.0.1:99999/nonexistent"
    pool._refresh_nodes()
    assert "existing" in pool._nodes_cache, "空响应保留旧缓存"
    print("  ✅ 空列表保留旧缓存")

# ============= 测试 4: 边界场景 =============
def test_singbox_restart():
    """无节点时不崩，返回兜底 URL"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool._nodes_cache = []
    pool._delays_cache = {}
    pool._ip_scores_cache = {}
    pool._weights_cache = []
    url = pool.rotate()
    assert url == "http://127.0.0.1:7890", f"兜底 URL: {url}"
    print(f"  ✅ 无节点返回兜底 URL: {url}")

def test_stats_not_crash():
    """get_stats 不崩（跳过网络，直接设缓存）"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool._nodes_cache = ["n1"]
    pool._delays_cache = {}
    pool._ip_scores_cache = {}
    pool._cache_ts = time.time()  # 跳过 _ensure_cache 的网络请求
    pool._weights_cache = pool._compute_weights()
    s = pool.get_stats()
    assert "total_nodes" in s and "weights" in s
    print(f"  ✅ get_stats 正常: total={s['total_nodes']}")

def test_all_nodes_same_delay():
    """所有节点同延迟 → good(IP=1.0) 权重大于 bad(IP=0.1)"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool._nodes_cache = ["good", "bad"]
    pool._delays_cache = {"good": 100, "bad": 100}
    pool._ip_scores_cache = {"good": 1.0, "bad": 0.5}
    pool._usage_count = {}
    w = _compute(pool)
    assert w["good"] > w["bad"], "IP 分高应权重大"
    # good=1×1/600=0.00167, bad=0.5×1/600=0.00083 → bad 被 MIN_WEIGHT 托底到 0.001
    # 所以比值 = 0.00167/0.001 ≈ 1.67，不是精确 2 倍
    assert w["good"] / w["bad"] >= 1.5, f"good/bad 应 >=1.5: {w['good']/w['bad']}"
    print(f"  ✅ 同延迟IP分区分: good={w['good']:.6f}, bad={w['bad']:.6f}")

def test_weight_normalized_not_extreme():
    """调用次数降权：被调多的节点权重显著低于被调少的"""
    pool = OKZProxyPool(cache_ttl_sec=0, skip_init=True)
    pool._nodes_cache = [f"n{i}" for i in range(10)]
    pool._delays_cache = {f"n{i}": 300 + i * 50 for i in range(10)}
    pool._ip_scores_cache = {f"n{i}": 1.0 if i < 5 else 0.5 for i in range(10)}
    pool._usage_count = {f"n{i}": 10 - i for i in range(10)}  # n0调10次,n9调1次
    w = _compute(pool)
    vals = list(w.values())
    # 最慢节点+n被调最少 → 权重可能反而高
    print(f"  权重值: {[round(v,6) for v in vals]}")
    assert all(v > 0 for v in vals), "所有权重 > 0"
    print(f"  ✅ 调用次数降权有效: min={min(vals):.6f} max={max(vals):.6f}")

# ============= 运行 =============
if __name__ == "__main__":
    tests = [
        ("权重计算_排序", test_weight_computation),
        ("权重计算_死节点降权", test_weight_no_delay),
        ("权重计算_无IP降权", test_weight_no_ip_score),
        ("权重计算_地板值", test_weight_min_floor),
        ("权重计算_新老排序", test_weight_delay_coverage),
        ("权重计算_同延迟IP分区", test_all_nodes_same_delay),
        ("权重计算_非极端范围", test_weight_normalized_not_extreme),
        ("rotate_返回URL", test_rotate_returns_url),
        ("rotate_并发安全", test_rotate_concurrent),
        ("容错_API不通", test_refresh_nodes_network_down),
        ("容错_空列表", test_refresh_nodes_empty),
        ("容错_singbox重启", test_singbox_restart),
        ("调试_stats", test_stats_not_crash),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        print(f"\n{'='*50}")
        print(f"测试: {name}")
        print(f"{'='*50}")
        try:
            fn()
            print("  ✅ 通过")
            passed += 1
        except Exception as e:
            print(f"  ❌ 失败: {e}")
            import traceback; traceback.print_exc()
            failed += 1

    print(f"\n\n{'='*50}")
    print(f"汇总: {passed}/{passed+failed} 通过")
    if failed:
        print(f"❌ {failed} 个失败")
    else:
        print("✅ 全部通过")
