#!/usr/bin/env python3
"""
OKZProxyPool - Per-request proxy rotation for sing-box

每个 HTTP 请求都换一个出口 IP，按「IP 质量/延迟」加权概率选择节点。
用法:
    pool = OKZProxyPool()
    proxy_url = pool.rotate()  # 每次请求前调用，返回 http://127.0.0.1:7890
    httpx.post(url, json=data, proxy=proxy_url, timeout=30)
"""
import json
import os
import random
import sqlite3
import threading
import time
import requests

# 从共享配置导入
from config import SINGBOX_API, SINGBOX_SECRET, SELECTOR, NODE_INFO_DB

# 权重参数 - imported from shared weights module
from weights import (
    IP_SCORE_NONE,
    MIN_WEIGHT,
    compute_all_weights,
    compute_ip_score,
)


class OKZProxyPool:
    """sing-box 代理池：每次 rotate() 返回代理 URL，内部按概率选节点"""

    def __init__(
        self,
        singbox_api: str = SINGBOX_API,
        secret: str = SINGBOX_SECRET,
        selector: str = SELECTOR,
        node_info_db: str = NODE_INFO_DB,
        cache_ttl_sec: int = 60,
        *,  # keyword-only
        skip_init: bool = False,  # 测试用：跳过网络初始化
    ):
        self.api = singbox_api
        self.secret = secret
        self.selector = selector
        self.db_path = node_info_db
        self.cache_ttl = cache_ttl_sec

        self._nodes_cache: list[str] = []
        self._delays_cache: dict[str, int] = {}   # node -> delay ms
        self._ip_scores_cache: dict[str, float] = {}  # node -> 0.5~1.0
        self._weights_cache: list[tuple[str, float]] = []  # [(node, weight), ...]
        self._usage_count: dict[str, int] = {}  # node -> 调用次数
        self._cache_ts = 0
        self._lock = threading.Lock()

        if not skip_init:
            self._refresh_data()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.secret}"}

    def _refresh_nodes(self):
        """从 sing-box 拿所有可用节点 tag"""
        try:
            r = requests.get(
                f"{self.api}/proxies/{self.selector}",
                headers=self._headers(),
                timeout=3,
            )
            if r.status_code == 200:
                data = r.json()
                all_nodes = data.get("all", [])
                # 过滤掉不参考用节点
                self._nodes_cache = [n for n in all_nodes if n != "direct"]
        except Exception as e:
            print(f"  ⚠ proxy_pool 刷新节点失败: {e}")  # 保留旧缓存

    def _refresh_delays(self):
        """批量测延迟（并发 10，超时 10s），结果缓存到 _delays_cache"""
        if not self._nodes_cache:
            return

        current = self.get_current()
        to_test = [n for n in self._nodes_cache if n != current]

        def test_one(node: str) -> tuple[str, int | None]:
            try:
                r = requests.get(
                    f"{self.api}/proxies/{node}/delay",
                    params={"timeout": 10000, "url": "https://www.gstatic.com/generate_204"},
                    headers=self._headers(),
                    timeout=15,
                )
                if r.status_code == 200:
                    return node, r.json().get("delay")
            except Exception:
                pass
            return node, None

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(test_one, n): n for n in to_test}
            for f in concurrent.futures.as_completed(futures):
                node, delay = f.result()
                if delay is not None and delay < 10000:
                    self._delays_cache[node] = delay
                else:
                    self._delays_cache.pop(node, None)

    def _refresh_ip_scores(self):
        """从 node_info 表读 IP/地区，算 IP 质量分：有国家=1.0, 只有 IP=0.5, 无=0.1"""
        if not self._nodes_cache:
            return
        try:
            with sqlite3.connect(self.db_path) as c:
                c.row_factory = sqlite3.Row
                placeholders = ",".join("?" for _ in self._nodes_cache)
                rows = c.execute(
                    f"SELECT node, country, city FROM node_info WHERE node IN ({placeholders})",
                    self._nodes_cache,
                ).fetchall()

            for row in rows:
                node = row["node"]
                country = row["country"] or ""
                city = row["city"] or ""
                self._ip_scores_cache[node] = compute_ip_score(country or None, city or None)

            # 未查到的给默认分
            for n in self._nodes_cache:
                self._ip_scores_cache.setdefault(n, IP_SCORE_NONE)
        except Exception:
            for n in self._nodes_cache:
                self._ip_scores_cache.setdefault(n, IP_SCORE_NONE)

    def _refresh_data(self):
        """从网络刷新节点/延迟/IP分，然后计算权重"""
        self._refresh_nodes()
        self._refresh_delays()
        self._refresh_ip_scores()
        self._weights_cache = self._compute_weights()
        self._cache_ts = time.time()

    def _compute_weights(self) -> list[tuple[str, float]]:
        """权重 = IP分 × 延迟分 / (调用次数+1)。调用越多越不受待见。"""
        return compute_all_weights(
            nodes=self._nodes_cache,
            ip_scores=self._ip_scores_cache,
            delays=self._delays_cache,
            usage_counts=self._usage_count,
        )

    def _ensure_cache(self):
        """如缓存过期则刷新"""
        with self._lock:
            now = time.time()
            if now - self._cache_ts >= self.cache_ttl:
                self._refresh_data()
                self._cache_ts = now

    def get_current(self) -> str | None:
        """查 sing-box 当前 selector 用的是哪个节点"""
        try:
            r = requests.get(
                f"{self.api}/proxies/{self.selector}",
                headers=self._headers(),
                timeout=3,
            )
            if r.status_code == 200:
                return r.json().get("now")
        except Exception:
            pass
        return None

    def rotate(self) -> str:
        """
        选一个节点（按权重概率），切 sing-box selector，返回代理 URL。
        供 HTTP 客户端每次请求前调用。
        """
        self._ensure_cache()

        if not self._weights_cache:
            return "http://127.0.0.1:7890"  # 兜底

        # 加权随机选一个
        nodes, weights = zip(*self._weights_cache)
        chosen = random.choices(nodes, weights=weights, k=1)[0]
        self._usage_count[chosen] = self._usage_count.get(chosen, 0) + 1

        # 切 sing-box selector
        try:
            requests.put(
                f"{self.api}/proxies/{self.selector}",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"name": chosen},
                timeout=3,
            )
        except Exception as e:
            print(f"  ⚠ proxy_pool 切换 {chosen} 失败: {e}")

        return "http://127.0.0.1:7890"

    def get_stats(self) -> dict:
        """调试用：看当前权重分布"""
        self._ensure_cache()
        return {
            "total_nodes": len(self._nodes_cache),
            "with_delay": len(self._delays_cache),
            "weights": sorted(self._weights_cache, key=lambda x: -x[1])[:10],
        }


# 便捷函数：直接用
_default_pool: OKZProxyPool | None = None
_pool_lock = threading.Lock()

def get_proxy_pool() -> OKZProxyPool:
    global _default_pool
    with _pool_lock:
        if _default_pool is None:
            _default_pool = OKZProxyPool()
        return _default_pool

def rotate_proxy() -> str:
    """一行代码拿代理：proxy_url = rotate_proxy()"""
    return get_proxy_pool().rotate()


if __name__ == "__main__":
    # 自测
    pool = OKZProxyPool()
    print(f"Nodes: {len(pool._nodes_cache)}")
    print(f"Delays: {len(pool._delays_cache)}")
    print(f"Top 5 weights: {pool.get_stats()['weights'][:5]}")
    for i in range(5):
        url = pool.rotate()
        print(f"  rotate #{i+1}: {url}")