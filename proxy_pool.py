#!/usr/bin/env python3
"""
OKZProxyPool - Per-request proxy rotation for sing-box

每个 HTTP 请求都换一个出口 IP，按自适应策略选择节点：
- normal:   延迟优先，快节点优先用完
- pressure: 调用次数优先，均匀分散
用法:
    pool = OKZProxyPool()
    proxy_url = pool.rotate()  # 每次请求前调用，返回 http://127.0.0.1:{MIXED_PORT}
    httpx.post(url, json=data, proxy=proxy_url, timeout=30)
"""
import json
import os
import random
import sqlite3
import threading
import time
import requests
import concurrent.futures
from functools import lru_cache

# 从共享配置导入
from config import SINGBOX_API, get_singbox_secret, SELECTOR, NODE_INFO_DB, MIXED_PORT, CFG, BAD_NODES_MAX_TTL

# 与 daemon._load_shared_bad_nodes 同源：proxy_pool 是 test-only/dormant 模块，
# 不 import 整个 daemon（会拉入 DB/atexit 等重状态），这里直接读共享 .bad_nodes.json。
_BAD_NODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bad_nodes.json")


def _load_shared_bad_nodes() -> set[str]:
    """读 .bad_nodes.json，返回未过期死节点名集合（与 daemon 导出格式一致）。

    ponytail: daemon 独占写入、proxy_pool 只读，避免争用。要跨进程拿到 daemon
    内存态 _DEAD_NODES 需 import daemon（重），本模块只读文件，足够保持权重一致。
    升级路径: proxy_pool 若升为常驻进程，改为 import daemon._DEAD_NODES 走同一进程内存。
    """
    now = int(time.time())
    max_allowed = now + BAD_NODES_MAX_TTL
    try:
        if not os.path.exists(_BAD_NODES_FILE):
            return set()
        with open(_BAD_NODES_FILE) as f:
            nodes = json.load(f)
        out = set()
        for n, v in nodes.items():
            if isinstance(v, dict) and "expire" in v:
                if v["expire"] > now and v["expire"] <= max_allowed:
                    out.add(n)
            elif isinstance(v, (int, float)) and v > now and v <= max_allowed:
                out.add(n)
        return out
    except Exception:
        return set()

# 权重参数 - imported from shared weights module
from weights import (
    IP_SCORE_NONE,
    MIN_WEIGHT,
    compute_all_weights,
    compute_ip_score,
)


class OKZProxyPool:
    """sing-box 代理池：每次 rotate() 返回代理 URL，内部按自适应概率选节点"""

    # 共享线程池：延迟测试复用，避免频繁创建/销毁
    _delay_pool: concurrent.futures.ThreadPoolExecutor | None = None
    _delay_pool_lock = threading.Lock()

    def __init__(
        self,
        singbox_api: str = SINGBOX_API,
        secret: str | None = None,
        selector: str = SELECTOR,
        node_info_db: str = NODE_INFO_DB,
        cache_ttl_sec: int = 60,
        *,  # keyword-only
        skip_init: bool = False,  # 测试用：跳过网络初始化
    ):
        self.api = singbox_api
        self.secret = secret if secret is not None else get_singbox_secret()
        self.selector = selector
        self.db_path = node_info_db
        self.cache_ttl = cache_ttl_sec

        self._nodes_cache: list[str] = []
        self._delays_cache: dict[str, int] = {}   # node -> delay ms
        self._ip_scores_cache: dict[str, float] = {}  # node -> 0.5~1.0
        self._weights_cache: list[tuple[str, float]] = []  # [(node, weight), ...]
        self._usage_count: dict[str, int] = {}  # node -> 调用次数（仅成功调用）
        self._cache_ts = 0
        self._lock = threading.Lock()
        self._strategy_mode: str = "normal"  # "normal" or "pressure"

        # 线程池走 _get_delay_pool() 惰性单例，避免与 __init__ 重复创建
        if not skip_init:
            self._refresh_data()

    @classmethod
    def _get_delay_pool(cls) -> concurrent.futures.ThreadPoolExecutor:
        """获取共享延迟测试线程池"""
        with cls._delay_pool_lock:
            if cls._delay_pool is None:
                cls._delay_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=30, thread_name_prefix="proxy_pool_delay"
                )
            return cls._delay_pool

    def _headers(self) -> dict:
        # ponytail: secret file mtime 自动刷新 — get_singbox_secret() 内部有缓存+检测
        return {"Authorization": f"Bearer {get_singbox_secret()}"}

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

    def _refresh_ip_scores(self):
        """从 node_info 表读 IP/地区，算 IP 质量分：有国家=1.0, 只有 IP=0.5, 无=0.1"""
        if not self._nodes_cache:
            return
        try:
            # 设置 busy_timeout=5000 与 daemon.py 保持一致，避免 "database is locked"
            with sqlite3.connect(self.db_path, timeout=5) as c:
                c.execute("PRAGMA busy_timeout=5000")
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
        """从网络刷新节点/延迟/IP分，然后计算权重
        优化：并行获取节点列表、延迟、IP分（节点列表是前置依赖）
        """
        self._refresh_nodes()
        if not self._nodes_cache:
            self._weights_cache = []
            self._cache_ts = time.time()
            return

        # 并行执行延迟测试和 IP 分数查询（互不依赖）
        pool = self._get_delay_pool()
        f1 = pool.submit(self._refresh_delays)
        f2 = pool.submit(self._refresh_ip_scores)
        f1.result()
        f2.result()

        self._weights_cache = self._compute_weights()
        self._cache_ts = time.time()

    def _refresh_delays(self):
        """批量测延迟（并发 30，超时 10s），结果缓存到 _delays_cache"""
        if not self._nodes_cache:
            return

        current = self.get_current()
        to_test = [n for n in self._nodes_cache if n != current]

        def test_one(node: str) -> tuple[str, int | None]:
            try:
                r = requests.get(
                    f"{self.api}/proxies/{node}/delay",
                    params={"timeout": 10000, "url": CFG['thresholds']['node_test_url']},
                    headers=self._headers(),
                    timeout=15,
                )
                if r.status_code == 200:
                    return node, r.json().get("delay")
            except Exception:
                pass
            return node, None

        # 使用共享线程池
        pool = self._get_delay_pool()
        futures = {pool.submit(test_one, n): n for n in to_test}
        for f in concurrent.futures.as_completed(futures):
            node, delay = f.result()
            if delay is not None and delay < 10000:
                self._delays_cache[node] = delay
            else:
                self._delays_cache.pop(node, None)

    def _compute_weights(self) -> list[tuple[str, float]]:
        """权重 = 自适应排序: normal=延迟优先, pressure=调用次数优先。"""
        return compute_all_weights(
            nodes=self._nodes_cache,
            ip_scores=self._ip_scores_cache,
            delays=self._delays_cache,
            usage_counts=self._usage_count,
            mode=self._strategy_mode,
        )

    def _ensure_cache(self):
        """如缓存过期则刷新。存失效时间戳而非检测时刻，减少持锁时间。"""
        if time.time() - self._cache_ts < self.cache_ttl:
            return
        with self._lock:
            if time.time() - self._cache_ts < self.cache_ttl:
                return  # double-check: 另一线程已刷新
            self._refresh_data()
            self._cache_ts = time.time()

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

    def rotate(self, count_call: bool = True) -> str:
        """
        选一个节点（按自适应权重），切 sing-box selector，返回代理 URL。
        count_call=False 时不计入调用次数（用于 429 错误触发的轮换）。
        """
        self._ensure_cache()

        if not self._weights_cache:
            return f"http://127.0.0.1:{MIXED_PORT}"  # 兜底

        # 加权随机选一个（原子递增，自旋锁避免持锁发网络请求）
        with self._lock:
            nodes, weights = zip(*self._weights_cache)
            chosen = random.choices(nodes, weights=weights, k=1)[0]
            if count_call:
                self._usage_count[chosen] = self._usage_count.get(chosen, 0) + 1

        # 切 sing-box selector（锁外发请求，不阻塞其他读）
        try:
            requests.put(
                f"{self.api}/proxies/{self.selector}",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"name": chosen},
                timeout=3,
            )
        except Exception as e:
            print(f"  ⚠ proxy_pool 切换 {chosen} 失败: {e}")

        return f"http://127.0.0.1:{MIXED_PORT}"

    def set_strategy_mode(self, mode: str):
        """设置策略模式: "normal"=延迟优先, "pressure"=均匀分散"""
        if mode != self._strategy_mode:
            print(f"  🔄 proxy_pool 策略切换: {self._strategy_mode} → {mode}")
            self._strategy_mode = mode
            self._cache_ts = 0  # 强制刷新权重

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