#!/usr/bin/env python3
"""
auto-switch-ip daemon v2 - 加固版

职责:
  1. 监控 9router 错误率 -> 自动切 sing-box 节点
  2. 每 30 秒主动轮换节点（IP 快速洗换）
  3. 节点延迟测试，过滤死节点（并发 10 个，10s 超时）
  4. sing-box watchdog（Clash API 200 黄金标准）
  5. 9router outboundProxyUrl 检查 + 自动修复
  6. 订阅过期检测 + 自动刷新
  7. 所有节点全死 -> fallback 到 okz:6696
  8. node|IP|地区 映射存储与展示
"""
import json
import os
import sys
import time
import sqlite3
import threading
import random as _random
import requests
import subprocess
import concurrent.futures
import socket
import traceback
import urllib3
from collections import deque
from datetime import datetime, timezone, timedelta
import re as _re
import logging
import atexit
import shutil
from logging.handlers import RotatingFileHandler
# daemon uses its own _delay_score; proxy_pool.py imports weights directly
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 从共享配置导入
from config import CFG, NODE_INFO_DB, FREE_PROVIDERS, RECENT_WINDOW, BAD_NODES_TTL, MIXED_PORT, LOG_FORMAT

# 全局复用连接 - 避免每次操作开/关 SQLite 连接
_db_conn: sqlite3.Connection | None = None
_db_lock = threading.RLock()  # RLock: init_node_info_db 里 _get_db 需重入
_proxy_429_detected: bool = False  # proxy_429 进程检测缓存
_proxy_429_check_ts: float = 0  # 上次检测时间
_BAD_NODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bad_nodes.json")
# bad_nodes cache (mtime-based, avoid repeated I/O)
_p429_bad_cache: dict[str, float] = {}
_p429_bad_mtime: float = 0
_p429_bad_check_ts: float = 0
_p429_bad_loaded: bool = False

def _get_db() -> sqlite3.Connection:
    """获取模块级 SQLite 连接（惰性初始化，WAL 模式）"""
    global _db_conn
    if _db_conn is None:
        with _db_lock:
            if _db_conn is None:  # double-checked locking
                _db_conn = sqlite3.connect(NODE_INFO_DB, check_same_thread=False, timeout=5)
                _db_conn.row_factory = sqlite3.Row
                _db_conn.execute("PRAGMA journal_mode=WAL")
                _db_conn.execute("PRAGMA busy_timeout=5000")
    return _db_conn

def init_node_info_db():
    """在 9router DB 中创建 node_info 表 + call_count 字段迁移"""
    with _db_lock:
        c = _get_db()
        c.execute("""
            CREATE TABLE IF NOT EXISTS node_info (
                node TEXT PRIMARY KEY,
                ip TEXT,
                country TEXT,
                city TEXT,
                isp TEXT,
                update_ts REAL,
                call_count INTEGER DEFAULT 0
            )
        """)
        # 增量迁移：旧表没有 call_count 列就加上
        try:
            c.execute("SELECT call_count FROM node_info LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE node_info ADD COLUMN call_count INTEGER DEFAULT 0")
        c.commit()

def get_node_info(node: str) -> dict | None:
    with _db_lock:
        c = _get_db()
        row = c.execute("SELECT node, ip, country, city, isp, update_ts, call_count FROM node_info WHERE node=?", (node,)).fetchone()
    return dict(row) if row else None


def set_node_info(node: str, ip: str, country: str = "", city: str = "", isp: str = "", call_count: int | None = None):
    with _db_lock:
        c = _get_db()
        if call_count is not None:
            # 仅更新计数
            c.execute("UPDATE node_info SET call_count=? WHERE node=?", (call_count, node))
        else:
            c.execute("""
                INSERT INTO node_info (node, ip, country, city, isp, update_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(node) DO UPDATE SET
                    ip=excluded.ip,
                    country=excluded.country,
                    city=excluded.city,
                    isp=excluded.isp,
                    update_ts=excluded.update_ts
            """, (node, ip, country, city, isp, time.time()))
        c.commit()



def format_node_display(node: str | None) -> str:
    """格式化 node 显示：node | IP | country/city"""
    if node is None:
        return "unknown"
    info = get_node_info(node)
    if info and info.get("ip"):
        parts = [info["ip"]]
        if info.get("country"):
            parts.append(f"{info['country']}")
        if info.get("city"):
            parts[-1] += f"/{info['city']}"
        if info.get("isp"):
            parts.append(info["isp"])
        return f"{node} | {' | '.join(parts)}"
    return node

# ============= 全局状态 =============
_DEAD_NODES: set[str] = set()
_DEAD_NODES_RELEASE_TS: dict[str, float] = {}
# 死节点指数退避参数（60s → 6min → 1h → 10h → 永久）
_DEAD_TTL_STAGES = [60, 360, 3600, 36000]  # 各阶段 TTL（秒）
_DEAD_TTL_PERMANENT = float('inf')         # 永久拉黑标记
_POOL_MIN_RATIO = 0.5                      # 最少保留 50% 节点可用
_dead_fail_count: dict[str, int] = {}  # 节点连续失败计数

_RESTART_COUNT = 0          # sing-box 连续重启计数
_LAST_HEALTH_LOG = 0        # 健康状态最后打印时间（防刷屏）
_ALL_DEAD_FALLBACK = False  # 全部节点死时是否已经切到了 okz
_NODE_DELAYS: dict[str, int] = {}  # 节点延迟缓存 (ms)
_NODE_CALL_COUNTS: dict[str, int] = {}  # 节点调用计数，用于宏观调控
_NODE_CALL_RESET_TS: float = 0  # 上次重置时间
_state_lock = threading.RLock()  # 保护所有全局共享状态
_last_wal_checkpoint: float = 0  # WAL checkpoint 时间戳
class _BoundedPool:
    """有界线程池：队列深度 ≤ max_workers*2，防止 Clash API 挂起队列暴涨 OOM。
    非阻塞：队列满时丢弃任务并记录警告，不阻塞 asyncio 事件循环。"""
    __slots__ = ('_executor', '_sem', '_name')
    def __init__(self, max_workers: int, name_prefix: str):
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=name_prefix)
        self._sem = threading.Semaphore(max_workers * 2)
        self._name = name_prefix
    def submit(self, fn, /, *args, **kwargs):
        # 非阻塞获取：队列满时返回 None 而非阻塞事件循环
        if not self._sem.acquire(blocking=False):
            log(f"⚠ {_name} 线程池队列已满，丢弃任务 {fn.__name__}")
            return None
        f = self._executor.submit(fn, *args, **kwargs)
        f.add_done_callback(lambda _: self._sem.release())
        return f
    def shutdown(self, wait=True):
        self._executor.shutdown(wait=wait)

_health_pool = _BoundedPool(max_workers=10, name_prefix="health")
_io_pool = _BoundedPool(max_workers=4, name_prefix="io")
atexit.register(lambda: (_health_pool.shutdown(wait=False), _io_pool.shutdown(wait=False)))

# ============= 日志 =============
_logger = None
_log_lock = threading.RLock()
_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB 轮转
_LOG_BACKUP_COUNT = 5              # 保留 5 个备份

def _init_logger():
    global _logger
    if _logger is not None:
        return
    _logger = logging.getLogger("auto-switch-ip")
    _logger.setLevel(logging.DEBUG)
    # 文件 handler（轮转）
    fh = RotatingFileHandler(CFG["log_file"], maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt=LOG_FORMAT))
    _logger.addHandler(fh)
    # stdout handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG)
    sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt=LOG_FORMAT))
    _logger.addHandler(sh)

def log(msg: str):
    """写日志到文件（轮转）+ stdout，线程安全"""
    global _logger
    with _log_lock:
        if _logger is None:
            _init_logger()
    _logger.info(msg)

def log_health(msg: str):
    """健康状态日志，每 10s 最多一条"""
    global _LAST_HEALTH_LOG
    now = time.time()
    if now - _LAST_HEALTH_LOG < 10:
        return
    _LAST_HEALTH_LOG = now
    log(msg)

# ============= 9router 监控 =============

def get_9router_error_rate() -> tuple[int, int, list]:
    """从 SQLite 读最近 N 个请求，返回 (total, errors, details_list)

    基于请求数量窗口：取最近 RECENT_REQUESTS 个免费 provider 请求，
    统计其中错误数。若错误数 >= ERROR_COUNT_THRESHOLD，触发切换。
    """
    try:
        with _db_lock:
            c = _get_db()
            # 取最近 N 个免费 provider 请求
            placeholders = ",".join("?" for _ in FREE_PROVIDERS)
            params = list(FREE_PROVIDERS) + [RECENT_WINDOW]
            rows = c.execute(f"""
                SELECT provider, model, status, timestamp
                FROM requestDetails
                WHERE provider IN ({placeholders})
                ORDER BY timestamp DESC LIMIT ?
            """, params).fetchall()

        if not rows:
            return 0, 0, []

        recent = [dict(r) for r in rows]
        total = len(recent)
        errors = sum(1 for r in recent if r.get("status") != "success")

        return total, errors, recent
    except Exception as e:
        log(f"✗ 读 SQLite 失败: {e}")
        return 0, 0, []

def _load_proxy_429_bad_nodes() -> dict[str, float]:
    """Read bad nodes from proxy_429's file. Returns {node: expire_ts}, expired filtered out.
    ponytail: cached per-mtime to avoid repeated I/O (checked every 5s at most)."""
    global _p429_bad_cache, _p429_bad_mtime, _p429_bad_check_ts, _p429_bad_loaded
    now = time.time()
    if now - _p429_bad_check_ts < 5:
        with _state_lock:
            return dict(_p429_bad_cache)
    _p429_bad_check_ts = now
    try:
        if os.path.exists(_BAD_NODES_FILE):
            mtime = os.path.getmtime(_BAD_NODES_FILE)
            if mtime == _p429_bad_mtime and _p429_bad_loaded:
                with _state_lock:
                    return dict(_p429_bad_cache)
            with open(_BAD_NODES_FILE) as f:
                nodes = json.load(f)
            filtered = {n: exp for n, exp in nodes.items() if exp > now}
            with _state_lock:
                _p429_bad_cache = filtered
            _p429_bad_mtime = mtime
            _p429_bad_loaded = True
            return filtered
    except Exception:
        pass
    _p429_bad_loaded = True
    with _state_lock:
        return dict(_p429_bad_cache)

def _detect_proxy_429() -> bool:
    """检测 proxy_429 是否在运行。缓存 5s。"""
    global _proxy_429_detected, _proxy_429_check_ts
    now = time.time()
    if now - _proxy_429_check_ts < 5:
        return _proxy_429_detected
    _proxy_429_check_ts = now
    try:
        r = subprocess.run(["pgrep", "-f", "python.*proxy_429"], capture_output=True, timeout=3)
        _proxy_429_detected = bool(r.stdout.strip())
    except Exception:
        _proxy_429_detected = False
    return _proxy_429_detected


def _expected_proxy_url() -> str:
    """Compute expected proxy URL based on proxy_429 presence."""
    return CFG["proxy_429"]["proxy_url"] if _detect_proxy_429() else f"http://127.0.0.1:{MIXED_PORT}"


def check_9router_outbound_proxy() -> bool:
    """确保 opencode 的 proxyPool 和 outboundProxyEnabled 都正确。fallback 期间跳过。

    proxy_429 运行时代理地址 :7891，否则 :7890。
    同时恢复 outboundProxyEnabled=True（proxy_429 崩溃时可能残留 bypass 状态）。
    """
    with _state_lock:
        if _ALL_DEAD_FALLBACK:
            return True  # 已降级到 okz，不覆盖
    EXPECTED_URL = _expected_proxy_url()
    try:
        with _db_lock:
            c = _get_db()
            # 获取 opencode 的 proxyPoolId
            row = c.execute('SELECT data FROM settings WHERE id=1').fetchone()
            if not row:
                log("⚠ settings 表无数据")
                return False
            s = json.loads(row[0])
            pool_id = s.get("providerStrategies", {}).get("opencode", {}).get("proxyPoolId")
            if not pool_id:
                log("⚠ 未找到 opencode proxyPool 配置")
                return False

            changed = False

            # 1. 检查 proxyPool 当前代理地址
            pool_row = c.execute('SELECT data FROM proxyPools WHERE id=?', (pool_id,)).fetchone()
            if not pool_row:
                log(f"⚠ proxyPool {pool_id} 不存在")
                return False

            pool = json.loads(pool_row[0])
            url = pool.get("proxyUrl", "")
            if url != EXPECTED_URL:
                log(f"⚠ opencode proxyPool 异常 (url={url})，修复为 {EXPECTED_URL}...")
                pool["proxyUrl"] = EXPECTED_URL
                c.execute('UPDATE proxyPools SET data = ? WHERE id = ?', (json.dumps(pool), pool_id))
                changed = True

            # 2. 恢复 outboundProxyEnabled（proxy_429 崩溃遗留的 bypass 状态）
            if not s.get("outboundProxyEnabled", True):
                log("⚠ outboundProxyEnabled=False，恢复为 True")
                s["outboundProxyEnabled"] = True
                c.execute('UPDATE settings SET data=? WHERE id=1', (json.dumps(s),))
                changed = True

            if changed:
                c.commit()
                log(f"✓ 代理配置已修复 -> url={EXPECTED_URL}, outboundProxyEnabled=True")
            else:
                return True
        return True
    except Exception as e:
        log(f"✗ 检查代理配置失败: {e}")
        return False

# ============= Sing-box 操作 =============
def singbox_api(path: str, method: str = "GET", body: dict | None = None,
                timeout: int = 3, silent: bool = False) -> tuple[int, dict | None]:
    """通用 sing-box Clash API 调用"""
    try:
        headers = {"Authorization": f"Bearer {CFG['singbox']['secret']}"}
        if body:
            headers["Content-Type"] = "application/json"
        r = requests.request(
            method,
            f'{CFG["singbox"]["clash_api"]}{path}',
            headers=headers,
            json=body,
            timeout=timeout,
        )
        if r.status_code in (200, 204):
            return r.status_code, r.json() if r.content else None
        return r.status_code, None
    except requests.ConnectionError:
        if not silent:
            log(f"✗ Clash API 连接失败: {CFG['singbox']['clash_api']}{path}")
        return 0, None
    except Exception as e:
        if not silent:
            log(f"✗ Clash API 异常: {e}")
        return 0, None

def get_current_proxy() -> str | None:
    r, data = singbox_api(f'/proxies/{CFG["singbox"]["selector"]}')
    return data.get("now") if data else None

def get_all_proxy_nodes() -> list[str]:
    r, data = singbox_api(f'/proxies/{CFG["singbox"]["selector"]}')
    return data.get("all", []) if data else []

def switch_proxy(new_node: str) -> bool:
    r, _ = singbox_api(f'/proxies/{CFG["singbox"]["selector"]}',
                       method="PUT", body={"name": new_node})
    ok = r == 200 or r == 204
    if ok:
        log(f"✓ 切到: {new_node}")
    else:
        log(f"✗ 切代理失败 ({new_node}): status={r}")
    return ok

def test_node_delay(node: str) -> int | None:
    """测试单个节点延迟，超时返回 None"""
    try:
        r = requests.get(
            f'{CFG["singbox"]["clash_api"]}/proxies/{node}/delay',
            params={"timeout": CFG["thresholds"]["node_test_timeout_ms"],
                    "url": CFG["thresholds"]["node_test_url"]},
            headers={"Authorization": f"Bearer {CFG['singbox']['secret']}"},
            timeout=15,  # 外层等更久，sing-box 本身 10s 超时
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("delay")
    except Exception:
        pass
    return None

# ============= 节点健康检查 =============
def batch_test_nodes(nodes: list[str], current: str) -> tuple[set[str], set[str]]:
    """并发批量测节点延迟，返回 (dead, alive)"""
    now = time.time()
    # 先清理过期 TTL，给 dead 节点复活机会
    cleanup_dead_ttl()

    with _state_lock:
        to_test = [n for n in nodes
                   if n != "direct" and n != current and n not in _DEAD_NODES]
        # 如果可测节点太少，尝试重测已过期但还在 blacklist 的节点
        if len(to_test) < 3:
            for n in list(_DEAD_NODES):
                if n != "direct" and n != current and _DEAD_NODES_RELEASE_TS.get(n, 0) <= now:
                    to_test.append(n)
                    _DEAD_NODES.discard(n)
                    _DEAD_NODES_RELEASE_TS.pop(n, None)
                    log(f"  复活重测: {n}")

    if not to_test:
        return set(_DEAD_NODES), set()

    alive = set()
    dead = set()
    futures = {}
    for n in to_test:
        f = _health_pool.submit(test_node_delay, n)
        if f is not None:
            futures[f] = n
    for f in concurrent.futures.as_completed(futures):
        node = futures[f]
        try:
            delay = f.result()
        except Exception as e:
            delay = None
            log(f"  ⚠ {node} 延迟测试异常: {e}")
        if delay is not None and delay < CFG["thresholds"]["node_test_timeout_ms"]:
            alive.add(node)
            with _state_lock:
                _NODE_DELAYS[node] = delay
                _dead_fail_count.pop(node, None)
        else:
            dead.add(node)
            with _state_lock:
                _NODE_DELAYS.pop(node, None)
                _dead_fail_count[node] = _dead_fail_count.get(node, 0) + 1
                fail_count = _dead_fail_count[node]
                # 指数退避：60s → 6min → 1h → 10h → 永久
                stage_idx = fail_count - 1
                if stage_idx >= len(_DEAD_TTL_STAGES):
                    ttl = _DEAD_TTL_PERMANENT
                    ttl_desc = "永久"
                else:
                    ttl = _DEAD_TTL_STAGES[stage_idx]
                    ttl_desc = f"{ttl}s"
                # 保底：活跃节点不足一半时不拉黑（严格小于才停）
                alive_count = len(nodes) - len(_DEAD_NODES)
                if alive_count < len(nodes) * _POOL_MIN_RATIO:
                    log(f"  ⚠ {node} 跳过拉黑（保底: {alive_count}/{len(nodes)} < {_POOL_MIN_RATIO*100:.0f}%）")
                    _dead_fail_count.pop(node, None)
                else:
                    _DEAD_NODES.add(node)
                    _DEAD_NODES_RELEASE_TS[node] = now + ttl
                    log(f"  ✗ {node} 死 (连续失败 {fail_count} 次, TTL {ttl_desc})")

    log(f"  节点测试: {len(alive)}✓/{len(dead)}✗")
    return dead, alive

def cleanup_dead_ttl():
    """清理过期的死节点 TTL，重置连续失败计数（给节点公平重试机会）

    同时从 proxy_429 的 bad_nodes 文件导入坏节点标记。
    """
    # 先读 proxy_429 文件（无锁）
    p429_bad = _load_proxy_429_bad_nodes()

    with _state_lock:
        now = time.time()
        expired = [n for n, ts in _DEAD_NODES_RELEASE_TS.items() if ts < now]
        for n in expired:
            _DEAD_NODES.discard(n)
            _DEAD_NODES_RELEASE_TS.pop(n, None)
            _dead_fail_count.pop(n, None)  # 重置 fail count，TTL 到了就给新机会
        if expired:
            log(f"  释放 {len(expired)} 个黑名单节点 (TTL 到，fail_count 已重置)")

        # 导入 proxy_429 标记的坏节点（如果有），保留原始 expiry
        if p429_bad:
            for n, exp in p429_bad.items():
                if n not in _DEAD_NODES:
                    _DEAD_NODES.add(n)
                    _DEAD_NODES_RELEASE_TS[n] = exp  # 保留 proxy_429 的原始 TTL



_last_switch_country: str | None = None  # 上次切换的节点国家，用于 429 时规避
_recent_nodes: deque = deque(maxlen=5)  # 最近用过的节点，P2C 排除


def _delay_score(node: str, delays: dict[str, int], median: int, min_d: int, max_d: int) -> float:
    """延迟评分：以中位数为分水岭，>中位数陡峭下降，<中位数平缓。

    - d <= median: 线性从 1.0 降到 0.5（平缓）
    - d > median: 平方/指数下降到 0.0（陡峭惩罚慢节点）
    目的：不优先抢最快节点，而是重点把特别慢的踢出去。
    """
    d = delays.get(node)
    if d is None:
        return 0.1

    # 防除零：median=0 时所有延迟都为 0，直接返回 1.0
    if median == 0:
        return 1.0

    if d <= median:
        # 平缓：median 处 0.5，最快处 1.0
        if min_d == median:
            return 1.0
        return 1.0 - 0.5 * (d / median)
    else:
        # 陡峭：median 处 0.5，最大延迟处 0.0
        if max_d == median:
            return 0.5
        ratio = (d - median) / (max_d - median)
        return 0.5 * (1 - ratio * ratio)  # 二次下降，可改成 1 - ratio**3 更陡


def _precompute_delay_stats(all_delays: list[int]) -> tuple[int, int, int]:
    """预计算延迟统计：median, min, max。空列表返回 (0,0,0)。"""
    if not all_delays:
        return 0, 0, 0
    sorted_delays = sorted(all_delays)
    median = sorted_delays[len(sorted_delays) // 2]
    min_d = sorted_delays[0]
    max_d = sorted_delays[-1]
    return median, min_d, max_d


def _filter_candidates(current: str, all_nodes: list, fail_set: set) -> list[str]:
    """Filter usable candidates, apply anti-sticky, fallback if < 5."""
    cleanup_dead_ttl()

    def usable(n):
        if n == "direct" or n == current:
            return False
        if n in fail_set or n in _DEAD_NODES or n in _recent_nodes:
            return False
        return True

    with _state_lock:
        candidates = [n for n in all_nodes if usable(n)]
        # ponytail: 候选不足时临时放行最近节点，不弹出 deque（保持防粘记忆）
        if len(candidates) < 5:
            for n in _recent_nodes:
                if len(candidates) >= 5:
                    break
                if n not in fail_set and n not in _DEAD_NODES and n != current and n not in candidates:
                    candidates.append(n)
            if len(candidates) < 5:
                candidates = [n for n in all_nodes
                              if n != "direct" and n != current
                              and n not in fail_set and n not in _DEAD_NODES]
    return candidates


def _load_candidate_stats(candidates: list[str]) -> tuple[dict[str, int], dict[str, str]]:
    """Load call_count and IP from DB for candidates. Returns (call_map, ip_map)."""
    call_map: dict[str, int] = {}
    ip_map: dict[str, str] = {}
    with _db_lock:
        c = _get_db()
        placeholders = ",".join("?" for _ in candidates)
        rows = c.execute(
            f"SELECT node, ip, country, call_count FROM node_info WHERE node IN ({placeholders})",
            candidates
        ).fetchall()
        for row in rows:
            call_map[row["node"]] = row["call_count"] or 0
            ip = row["ip"] or ""
            if ip:
                ip_map[row["node"]] = ip
    return call_map, ip_map


def _dedup_by_ip(candidates: list[str], call_map: dict[str, int], ip_map: dict[str, str]) -> set[str]:
    """Return set of nodes to skip (duplicates by IP, keep lowest call_count)."""
    ip_groups: dict[str, list[str]] = {}
    for n in candidates:
        ip = ip_map.get(n, "")
        if ip:
            ip_groups.setdefault(ip, []).append(n)
        else:
            ip_groups.setdefault(n, []).append(n)
    dedup_skip: set[str] = set()
    for ip, group in ip_groups.items():
        if len(group) > 1:
            sorted_group = sorted(group, key=lambda n: call_map.get(n, 0))
            for n in sorted_group[1:]:
                dedup_skip.add(n)
    return dedup_skip


def _p2c_select(candidates: list[str], dedup_skip: set[str], call_map: dict[str, int]) -> str:
    """Pick from candidates using P2C + score (-call_count, delay_score)."""
    pool = [n for n in candidates if n not in dedup_skip] or candidates
    with _state_lock:
        usage_counts = {n: call_map.get(n, 0) + _NODE_CALL_COUNTS.get(n, 0) for n in candidates}
        delays = dict(_NODE_DELAYS)
    all_delays = [d for d in delays.values() if d is not None]
    median, dmin, dmax = _precompute_delay_stats(all_delays)

    def _score(n):
        calls = usage_counts.get(n, 0)
        ds = _delay_score(n, delays, median, dmin, dmax)
        return (-calls, ds)

    k = min(2, len(pool))
    chosen = _random.sample(pool, k)[0] if k < 2 else max(_random.sample(pool, k), key=_score)
    return chosen


def pick_next_node(current: str, all_nodes: list, fail_set: set) -> str | None:
    """P2C + 自适应分数：每次随机抽两个候选，选分数高的。

    _score = (-call_count, delay_score) 字典序比较
    - 主序：调用次数越少越好（流量分散为核心目标）
    - 次序：调用次数相同时，延迟分高的优先（同次数时选快节点）
    延迟分散→陡峭优先，延迟趋同→分散到其他节点。
    最近使用过的 5 个节点不参与选择（anti-sticky）。
    """
    candidates = _filter_candidates(current, all_nodes, fail_set)

    if not candidates:
        log(f"⚠ 无可用节点 (dead={len(_DEAD_NODES)} fail={len(fail_set)} current={current or 'none'} total={len(all_nodes)})")
        return None

    call_map, ip_map = _load_candidate_stats(candidates)
    dedup_skip = _dedup_by_ip(candidates, call_map, ip_map)
    chosen = _p2c_select(candidates, dedup_skip, call_map)

    with _state_lock:
        _recent_nodes.append(chosen)
        _NODE_CALL_COUNTS[chosen] = _NODE_CALL_COUNTS.get(chosen, 0) + 1
    return chosen

# ============= Sing-box Watchdog (Windows) =============
def _singbox_pids() -> list[int]:
    """获取 Linux sing-box 进程 PID（精确匹配二进制路径）"""
    try:
        singbox_bin = CFG["singbox"]["binary"]
        r = subprocess.run(
            ["pgrep", "-f", f"^{_re.escape(singbox_bin)} run"],
            capture_output=True, text=True, timeout=5,
        )
        return [int(p) for p in r.stdout.split() if p.isdigit()]
    except Exception:
        return []


def is_singbox_alive() -> bool:
    """Check sing-box Clash API + proxy port. Hard timeout 4s to avoid blocking main loop."""
    deadline = time.time() + 4
    # 1. Clash API must respond
    code, _ = singbox_api("/proxies", silent=True, timeout=min(2, deadline - time.time() + 0.5))
    if code != 200:
        pids = _singbox_pids()
        if pids:
            log(f"⚠ Clash API 不通但进程还在 (PID={pids})，触发 watchdog 重启")
        return False

    if time.time() >= deadline:
        log(f"⚠ Clash API 正常但端口检查超时")
        return True

    # 2. Proxy port must accept connection (fast check)
    try:
        sock = socket.create_connection(("127.0.0.1", MIXED_PORT), timeout=min(2, deadline - time.time() + 0.5))
        sock.close()
    except (ConnectionRefusedError, OSError, TimeoutError):
        log(f"⚠ Clash API 正常但代理端口 {MIXED_PORT} 不通，触发 watchdog 重启")
        return False

    return True

_restart_lock = threading.Lock()

def _set_outbound_proxy(url: str = "", enabled: bool = False) -> None:
    """Atomically set 9router outboundProxyUrl + outboundProxyEnabled via json_set. Thread-safe."""
    try:
        with _db_lock:
            c = _get_db()
            enabled_int = 1 if enabled else 0
            c.execute("""
                UPDATE settings SET data = json_set(data,
                    '$.outboundProxyUrl', ?,
                    '$.outboundProxyEnabled', ?
                ) WHERE id=1
            """, (url, enabled_int))
            c.commit()
    except Exception as e:
        log(f"✗ 设置 outboundProxy 失败: {e}")

def restart_singbox() -> bool:
    """重启 sing-box，互斥锁防止并发调用。停服前绕过代理，启动后恢复。"""
    global _RESTART_COUNT
    if not _restart_lock.acquire(blocking=False):
        log("  ⚠ restart_singbox 已在运行，跳过并发调用")
        return False
    try:
        log(f"⚠ 重启 sing-box (连续失败 {_RESTART_COUNT}/{CFG['singbox']['max_restart_attempts']})...")

        # 停服前绕过代理，避免重启窗口请求报错
        _set_outbound_proxy(enabled=False)
        singbox_bin = CFG["singbox"]["binary"]
        subprocess.run(
            ["pkill", "-f", f"^{_re.escape(singbox_bin)} run"],
            capture_output=True, timeout=10)
        time.sleep(2)

        subprocess.run(
            ["fuser", "-k", f"{MIXED_PORT}/tcp"],
            capture_output=True, timeout=5)
        time.sleep(1)

        config_file = CFG["singbox"]["config_file"]
        log_dir = os.path.dirname(CFG["log_file"])

        # ponytail: 显式保存文件句柄，避免 fd 泄漏
        log_path = os.path.join(log_dir, "singbox.log")
        # 单文件轮转：>10MB 时备份为 .1 并截断
        try:
            if os.path.getsize(log_path) > 10 * 1024 * 1024:
                bak = log_path + ".1"
                if os.path.exists(bak):
                    os.remove(bak)
                shutil.move(log_path, bak)
                log(f"  singbox.log 轮转 (>{10}MB)")
        except Exception:
            pass
        log_file = open(log_path, "a")
        try:
            subprocess.Popen(
                [singbox_bin, "run", "-c", config_file],
                stdout=log_file, stderr=subprocess.STDOUT,
                start_new_session=True)
        finally:
            log_file.close()
        time.sleep(CFG["singbox"]["startup_grace_sec"])

        if is_singbox_alive():
            _RESTART_COUNT = 0
            log("✓ sing-box 重启成功")
            # 恢复代理
            _set_outbound_proxy(url=_expected_proxy_url(), enabled=True)
            return True
        else:
            _RESTART_COUNT += 1
            log(f"✗ sing-box 重启失败 (第 {_RESTART_COUNT} 次)")
            # 重启失败也要恢复代理，避免 9router 裸奔
            _set_outbound_proxy(url=_expected_proxy_url(), enabled=True)
            return False
    except Exception as e:
        _RESTART_COUNT += 1
        log(f"✗ sing-box 重启异常: {e}")
        # 异常分支也要恢复代理
        _set_outbound_proxy(url=_expected_proxy_url(), enabled=True)
        return False
    finally:
        _restart_lock.release()


# ============= 订阅刷新 =============
def refresh_subscription() -> bool:
    """重新拉 okz 订阅 + 生成 sing-box config + 重启 sing-box"""
    log("🔄 刷新订阅...")
    try:
        parse_script = os.path.join(os.path.dirname(__file__), "parse_subscription.py")
        config_file = CFG["singbox"]["config_file"]
        subprocess.run(
            [sys.executable, parse_script,
             "--out", config_file],
            capture_output=True, text=True, timeout=60)
        log("✓ 订阅刷新完毕，重启 sing-box 加载新配置...")
        return restart_singbox()
    except Exception as e:
        log(f"✗ 订阅刷新失败: {e}")
        return False

# ============= 降级逻辑 =============
def check_and_handle_fallback(all_nodes: list, current: str) -> bool:
    """检查是否全部节点都死了，是则降级到 okz:6696

    返回 True 表示已经做了降级操作。
    """
    global _ALL_DEAD_FALLBACK

    with _state_lock:
        dead_snapshot = set(_DEAD_NODES)
    alive = [n for n in all_nodes
             if n != "direct" and n not in dead_snapshot]
    if alive:
        # 有可用节点，如果之前降级了，恢复到 sing-box
        if _ALL_DEAD_FALLBACK:
            _ALL_DEAD_FALLBACK = False
            try:
                _set_outbound_proxy(
                    _expected_proxy_url(),
                    enabled=True
                )
                log("🔁 节点恢复，outboundProxyUrl 已从 okz 恢复为 sing-box")
            except Exception as e:
                log(f"✗ 恢复 outboundProxyUrl 失败: {e}")
        return False

    # 全部死了
    if _ALL_DEAD_FALLBACK:
        return True  # 已经降级了

    log("🚨 全部节点不可用！降级到 okz:6696")
    _ALL_DEAD_FALLBACK = True
    # 通过 SQLite 把 9router 切到 okz
    try:
        _set_outbound_proxy(CFG["okz"]["proxy_url"], enabled=True)
        log("✓ 已降级到 okz:6696")
    except Exception as e:
        log(f"✗ 降级失败: {e}")
    return True

# ============= IP/地区查询（切换后异步） =============
def fetch_node_ip_region(node: str) -> tuple[str, str, str, str] | None:
    """通过 7890 代理查该节点出口 IP + 地区信息

    返回 (ip, country, city, isp) 或 None
    """
    try:
        # 1. 拿出口 IP（通过 sing-box 代理）
        r = requests.get("https://api.ipify.org",
                         proxies={"http": f"http://127.0.0.1:{MIXED_PORT}", "https": f"http://127.0.0.1:{MIXED_PORT}"},
                         timeout=10)
        ip = r.text.strip()
        if not ip:
            return None
        # 2. 查地区（直连 ip-api.com，绕过系统代理 6696）
        r2 = requests.get(f"http://ip-api.com/json/{ip}?fields=country,city,isp",
                          proxies={"http": None, "https": None}, timeout=5)
        if r2.status_code == 200:
            d = r2.json()
            country = d.get("country", "")
            city = d.get("city", "")
            isp = d.get("isp", "")
            return ip, country, city, isp
        return ip, "", "", ""
    except requests.exceptions.ConnectionError as e:
        log(f"  ✗ {node} IP查询连接失败: {e}")
        return None
    except requests.exceptions.Timeout as e:
        log(f"  ✗ {node} IP查询超时: {e}")
        return None
    except Exception as e:
        log(f"  ✗ {node} IP查询异常: {e}")
        return None

def update_node_info_async(node: str):
    """切换后后台更新 node 信息（非阻塞，用线程池限制并发）"""
    def _worker():
        try:
            info = fetch_node_ip_region(node)
            if info:
                ip, country, city, isp = info
                set_node_info(node, ip, country, city, isp)
                log(f"  📍 {format_node_display(node)}")
            else:
                log(f"  ⚠ 无法获取 {node} 的 IP/地区信息")
        except Exception as e:
            log(f"  ✗ 更新 {node} 信息失败: {e}")

    _io_pool.submit(_worker)

# ============= 主循环 =============
def main():
    all_nodes = get_all_proxy_nodes()

    log("=" * 50)
    log("auto-switch-ip daemon v2 启动 (加固版)")
    log(f"  sing-box: {CFG['singbox']['clash_api']}")

    log(f"  节点数: {len(all_nodes)}")
    log(f"  轮换: 每 {CFG['intervals']['proactive_rotate_sec']}s P2C")
    log(f"  健康: 每 {CFG['intervals']['node_health_sec']}s 批量测延迟")
    log(f"  订阅: 每 {CFG['intervals']['subscription_refresh_sec']}s 刷新")
    log(f"  Watchdog: 每 {CFG['intervals']['singbox_watchdog_sec']}s")
    # log(f"  慢性排除: 连续 {_CHRONIC_FAIL_THRESHOLD}+ 批次死亡永久排除")
    log(f"  IP去重: 同出口IP保留调用次数最低的节点")
    log(f"  interrupt_exist_connections: false (不杀现有连接)")
    log(f"  错误触发: 连续 {RECENT_WINDOW} 请求中成功率 <{CFG['thresholds']['opencode_min_success_rate']*100:.0f}%")
    log("=" * 50)

    # 初始化 node_info 表
    init_node_info_db()

    # 状态
    last_switch_ts = 0
    cooldown_until = 0
    fail_set: set[str] = set()
    _fail_set_release_ts: dict[str, float] = {}
    last_singbox_check = 0
    last_health_check = 0
    last_sub_refresh = 0
    last_proxy_check = 0
    last_summary_ts = 0
    last_proactive_rotate = 0

    global _RESTART_COUNT, _NODE_CALL_RESET_TS, _last_wal_checkpoint
    _RESTART_COUNT = 0  # 确保在 main() 作用域内也是 global
    _NODE_CALL_RESET_TS = time.time()

    # 启动检查
    if not all_nodes:
        log("⚠ sing-box 不通，尝试启动...")
        if not restart_singbox():
            log("✗ sing-box 启动失败，等 30s 重试")
            time.sleep(30)
            if not restart_singbox():
                log("✗ 无法启动 sing-box，降级到 okz:6696")
                check_and_handle_fallback([], "")
                time.sleep(60)

        all_nodes = get_all_proxy_nodes()
        if all_nodes:
            log(f"✓ sing-box 启动成功，节点数={len(all_nodes)}")
    else:
        log(f"✓ sing-box OK，节点数={len(all_nodes)}")

    # 首次节点健康测试（异步后台跑，不阻塞主循环）
    _initial_health_done = threading.Event()
    if all_nodes:
        current = get_current_proxy()
        log("🔍 首次节点健康测试（后台）...")
        def _initial_health_check():
            try:
                dead, alive = batch_test_nodes(all_nodes, current or "")
                # ponytail: batch_test_nodes 已更新 _DEAD_NODES 和 TTL（含指数退避），无需重复操作
                log(f"🔍 首轮健康测试完成: dead={len(dead)} alive={len(alive)}")
            except Exception as e:
                log(f"✗ 首轮健康测试异常: {e}")
            finally:
                _initial_health_done.set()
        threading.Thread(target=_initial_health_check, daemon=True).start()
    else:
        _initial_health_done.set()

    while True:
        now = time.time()
        # ---- 缓存 Clash API 结果：每循环只请求一次 ----
        # 合并 /proxies/proxy 一次调用得 now+all（减少 Clash API 请求次数）
        _r, _data = singbox_api(f'/proxies/{CFG["singbox"]["selector"]}')
        cached_all_nodes = _data.get("all", []) if _data else []
        cached_current_proxy = _data.get("now") if _data else None
        try:
            # ---- 清理过期 fail_set（每轮循环） ----
            expired_fail = [n for n, ts in _fail_set_release_ts.items() if ts < now]
            for n in expired_fail:
                fail_set.discard(n)
                _fail_set_release_ts.pop(n, None)

            # ---- 0. 9router 代理配置检查 (每 60s) ----
            if now - last_proxy_check > CFG["intervals"]["proxy_check_sec"]:
                check_9router_outbound_proxy()
                last_proxy_check = now

            # ---- 1. 读取错误率 ----
            total, errors, recent = get_9router_error_rate()

            # ---- 2b. 错误触发轮换 ----
            should_error = (total >= 2 and errors / total > (1 - CFG["thresholds"]["opencode_min_success_rate"]))
            # ponytail: threshold=2 instead of 3 to catch 100% failure in low-traffic windows
            in_cooldown = now < cooldown_until

            # ---- 3. 主动轮换（每 N 秒加权选新节点） ----
            if now - last_proactive_rotate > CFG["intervals"]["proactive_rotate_sec"]:
                current = cached_current_proxy
                nodes = cached_all_nodes

                if nodes:
                    next_node = pick_next_node(current or "", nodes, fail_set)
                    if next_node and next_node != current:
                        reason = "主动轮换"
                        delay_info = f" ({_NODE_DELAYS[next_node]}ms)" if next_node in _NODE_DELAYS else ""
                        log(f"→ 切代理 ({reason}): {format_node_display(current)} -> {format_node_display(next_node)}{delay_info}")
                        if switch_proxy(next_node):
                            last_switch_ts = now
                            update_node_info_async(next_node)
                    elif not next_node:
                        log(f"⚠ 主动轮换: 无可用节点 (dead={len(_DEAD_NODES)} fail={len(fail_set)})")

                last_proactive_rotate = now

            # ---- 3b. 错误触发轮换（错误率高时立即切） ----
            if should_error and not in_cooldown:
                # 拿当前真实节点（不用缓存，确保标记正确）
                current = get_current_proxy()
                nodes = cached_all_nodes

                if nodes:
                    if current:
                        fail_set.add(current)
                        _fail_set_release_ts[current] = now + BAD_NODES_TTL  # TTL 5 分钟

                    check_and_handle_fallback(nodes, current or "")

                    next_node = pick_next_node(current or "", nodes, fail_set)
                    if next_node:
                        reason = f"err={errors}/{total}"
                        delay_info = f" ({_NODE_DELAYS[next_node]}ms)" if next_node in _NODE_DELAYS else ""
                        log(f"→ 切代理 ({reason}): {format_node_display(current)} -> {format_node_display(next_node)}{delay_info}")
                        if switch_proxy(next_node):
                            cooldown_until = now + CFG["intervals"]["cooldown_after_switch_sec"]
                            update_node_info_async(next_node)
                    else:
                        log(f"⚠ 无可用节点 (dead={len(_DEAD_NODES)} fail={len(fail_set)})")

            # ---- 5. 节点健康检查 (每 300s) ----
            if now - last_health_check > CFG["intervals"]["node_health_sec"]:
                if _initial_health_done.is_set():
                    nodes = cached_all_nodes
                    current = cached_current_proxy
                    if nodes and current:
                        dead, alive = batch_test_nodes(nodes, current)
                        # ponytail: batch_test_nodes 已更新 _DEAD_NODES 和 TTL（含指数退避），无需重复操作
                        # 清理活过来的节点
                        with _state_lock:
                            for n in list(_DEAD_NODES):
                                if n in alive:
                                    _DEAD_NODES.discard(n)
                                    _DEAD_NODES_RELEASE_TS.pop(n, None)

                        # 降级检查
                        check_and_handle_fallback(nodes, current)
                last_health_check = now

            # ---- 6. 订阅刷新 (每 1h，后台执行避免阻塞主循环) ----
            if now - last_sub_refresh > CFG["intervals"]["subscription_refresh_sec"]:
                _io_pool.submit(refresh_subscription)
                last_sub_refresh = now

            # ---- 7. Sing-box watchdog ----
            if now - last_singbox_check > CFG["intervals"]["singbox_watchdog_sec"]:
                if not is_singbox_alive():
                    if _RESTART_COUNT < CFG["singbox"]["max_restart_attempts"]:
                        restart_singbox()
                        cooldown_until = now + 30  # 重启后冷却
                    else:
                        log(f"🚨 sing-box 连续重启 {_RESTART_COUNT} 次失败，降级")
                        check_and_handle_fallback([], "")
                else:
                    # 恢复后清除重启计数
                    if _RESTART_COUNT > 0:
                        _RESTART_COUNT = 0
                        log("✓ sing-box watchdog 恢复")

                last_singbox_check = now

            # ---- 8. 节点调用计数重置 (每 20min) ----
            if now - _NODE_CALL_RESET_TS > 1200:
                with _state_lock:
                    _NODE_CALL_COUNTS.clear()
                with _db_lock:
                    _get_db().execute("UPDATE node_info SET call_count=0")
                    _get_db().commit()
                _NODE_CALL_RESET_TS = now
                log("  ♻ 节点调用计数已重置")

            # ---- 8b. WAL checkpoint (每 5min) ---
            if now - _last_wal_checkpoint > 300:
                try:
                    with _db_lock:
                        _get_db().execute("PRAGMA wal_checkpoint(PASSIVE)")
                    _last_wal_checkpoint = now
                except Exception as e:
                    log(f"  ⚠ WAL checkpoint 失败: {e}")

            # ---- 9. 健康摘要（每 10s 最多一条） ----
            with _state_lock:
                dead_count = len(_DEAD_NODES)
                err_threshold = 1 - CFG["thresholds"]["opencode_min_success_rate"]
            log_health(f"节点={len(cached_all_nodes)} 错误={errors}/{total} "
                       f"err>{(err_threshold*100):.0f}%→切"
                       f"冷却至={time.strftime('%H:%M:%S', time.localtime(cooldown_until)) if cooldown_until>0 else '-'}"
                       f" dead={dead_count} singbox={'🟢' if _RESTART_COUNT==0 else '🔴'}")

            time.sleep(CFG["intervals"]["monitor_sec"])

        except KeyboardInterrupt:
            log("=== 退出 ===")
            break
        except Exception as e:
            log(f"✗ 主循环异常: {e}")
            log(traceback.format_exc())
            time.sleep(10)

if __name__ == "__main__":
    main()