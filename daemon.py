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
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 从共享配置导入
from config import CFG, NODE_INFO_DB, OPENCODE_FREE_PROVIDER, RECENT_WINDOW, BAD_NODES_TTL, BAD_NODES_MAX_TTL, MIXED_PORT, LOG_FORMAT, get_singbox_secret

# 全局复用连接 - 避免每次操作开/关 SQLite 连接
_db_conn: sqlite3.Connection | None = None
_db_lock = threading.RLock()  # RLock: init_node_info_db 里 _get_db 需重入
_BAD_NODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bad_nodes.json")
# 出口节点归属日志：每次成功切换追加 (ts,node)，供 per-egress 错误率归因用。
_EGRESS_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".egress_log.jsonl")
# bad_nodes cache (mtime-based, avoid repeated I/O)
_shared_bad_cache: dict[str, float] = {}
_shared_bad_mtime: float = 0
_shared_bad_check_ts: float = 0
_shared_bad_loaded: bool = False

# 统一时间戳：int 秒（与 proxy-global.js 对齐）
def _now_ts() -> int:
    return int(time.time())

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
# 死节点指数退避参数
_DEAD_TTL_MAX = BAD_NODES_MAX_TTL          # 最大 TTL（替代 float('inf）永久拉黑）

def _generate_ttl_stages() -> list[int]:
    """算法生成 TTL 阶梯：
    - 第 1-10 次：平滑渐进 30s → 120s (指数平滑)
    - 第 11-15 次：线性增长到 1h
    - 16+ 次：指数增长到 7 天
    """
    stages = []

    # 第 1-10 次：平滑指数渐进 30s → 120s
    # 使用指数插值：start * (end/start)^(i/(n-1))
    start, end = 30, 120
    for i in range(10):
        ratio = i / 9  # 0 到 1
        ttl = int(start * (end / start) ** ratio)
        stages.append(ttl)

    # 第 11-15 次：线性增长到 1h (3600s)
    for i in range(5):
        stages.append(300 + i * 600)  # 5m, 10m, 15m, 20m, 25m

    # 16+ 次：指数增长到 7 天
    base = 3600  # 1h
    for i in range(12):  # 足够到达 7 天
        stages.append(min(base * (2 ** i), _DEAD_TTL_MAX))

    return stages

# 模块加载时生成，避免懒加载竞态
_DEAD_TTL_STAGES: list[int] = _generate_ttl_stages()
_POOL_MIN_RATIO = 0.3                      # 降低保底：允许 70% 节点被拉黑
_dead_fail_count: dict[str, int] = {}  # 节点连续失败计数

_RESTART_COUNT = 0          # sing-box 连续重启计数（受 _restart_lock 保护）
_RESTART_LOCK = threading.Lock()  # 保护 _RESTART_COUNT 的线程安全读写
_LAST_HEALTH_LOG = 0        # 健康状态最后打印时间（防刷屏）
_ALL_DEAD_FALLBACK = False  # 全部节点死时是否已经切到了 okz
_NODE_DELAYS: dict[str, int] = {}  # 节点延迟缓存 (ms)
_NODE_CALL_COUNTS: dict[str, int] = {}  # 节点调用计数（仅成功调用）
_NODE_CALL_RESET_TS: float = 0  # 上次重置时间
_strategy_mode: str = "normal"  # "normal"=延迟优先, "pressure"=均匀分散
_state_lock = threading.RLock()  # 保护所有全局共享状态
_last_wal_checkpoint: float = 0  # WAL checkpoint 时间戳


def _update_strategy_mode(prev_mode: str, error_rate: float, total: int,
                          pressure_rate: float, normal_rate: float) -> str:
    """自适应策略回滞切换（纯函数，便于单测）。
    normal→pressure: error_rate ≥ pressure_rate 且 total ≥ 3。
    pressure→normal: error_rate ≤ normal_rate 或 total < 3（样本太少不判压）。"""
    if prev_mode == "normal":
        if error_rate >= pressure_rate and total >= 3:
            return "pressure"
        return "normal"
    else:
        if error_rate <= normal_rate or total < 3:
            return "normal"
        return "pressure"
# 健康检查后台任务状态
_health_check_future = None
_health_check_nodes = None
_health_check_current = None
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
            log(f"⚠ {self._name} 线程池队列已满，丢弃任务 {fn.__name__}")
            return None
        f = self._executor.submit(fn, *args, **kwargs)
        f.add_done_callback(lambda _: self._sem.release())
        return f
    def shutdown(self, wait=True):
        self._executor.shutdown(wait=wait)

# 增加 worker 数和队列深度：支持并发测试 90+ 节点不丢任务
_health_pool = _BoundedPool(max_workers=60, name_prefix="health")
_io_pool = _BoundedPool(max_workers=8, name_prefix="io")
def _cleanup_atexit():
    _health_pool.shutdown(wait=True)
    _io_pool.shutdown(wait=True)
    with _db_lock:
        if _db_conn:
            try: _db_conn.close()
            except Exception: pass

atexit.register(_cleanup_atexit)

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

def get_9router_error_rate() -> tuple[int, int, list, int]:
    """从 SQLite 读最近 N 个 OpenCode Free 请求，返回 (total, errors, details_list, consecutive_errors)

    明确针对 OpenCode Free provider（`requestDetails.provider = "opencode"`）。
    其他免费 provider 不参与切换决策——它们不触发 IP 轮换。

    检测策略：两层检测
    - 窗口检测：最近 RECENT_WINDOW 条的总错误率
    - 连续检测：从最新条往前数，连续失败的个数（防止 success 稀释 error）
    任一超过阈值就触发切换。
    """
    try:
        with _db_lock:
            c = _get_db()
            rows = c.execute(f"""
                SELECT provider, model, status, timestamp
                FROM requestDetails
                WHERE provider = ?
                ORDER BY timestamp DESC LIMIT ?
            """, (OPENCODE_FREE_PROVIDER, RECENT_WINDOW)).fetchall()

        if not rows:
            return 0, 0, [], 0

        recent = [dict(r) for r in rows]
        total = len(recent)
        errors = sum(1 for r in recent if r.get("status") != "success")

        consecutive_errors = 0
        for r in recent:
            if r.get("status") != "success":
                consecutive_errors += 1
            else:
                break

        if errors > 0:
            err_models = set(r.get("model", "?") for r in recent if r.get("status") != "success")
            log(f"  [OpenCode Free] {errors}/{total} 请求错误 | 连续失败={consecutive_errors} | 失败模型: {', '.join(sorted(err_models))}")

        return total, errors, recent, consecutive_errors
    except Exception as e:
        log(f"✗ 读 SQLite 失败: {e}")
        return 0, 0, [], 0

def _load_egress_log() -> list[dict]:
    """读 .egress_log.jsonl → 按 ts 升序的切换记录 [(ts, node), ...]。"""
    if not os.path.exists(_EGRESS_LOG_FILE):
        return []
    out = []
    try:
        with open(_EGRESS_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if "ts" in d and "node" in d:
                        out.append(d)
                except Exception:
                    continue
    except Exception as e:
        log(f"✗ 读 egress_log 失败: {e}")
        return []
    out.sort(key=lambda x: x["ts"])
    # 裁剪：仅保留最近 7 天，避免文件无限增长（append-only 无 truncate）。
    keep_after = int(time.time()) - 7 * 86400
    pruned = [d for d in out if d.get("ts", 0) >= keep_after]
    if len(pruned) != len(out):
        try:
            tmp = _EGRESS_LOG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for d in pruned:
                    f.write(json.dumps(d) + "\n")
            os.replace(tmp, _EGRESS_LOG_FILE)
        except Exception:
            pass
    return pruned


def _active_node_at(ts: int, switches: list[dict]) -> str | None:
    """返回 ts 时刻活跃的出口节点（最近一次 ts<=切换点 的 node）；无切换记录返回 None。"""
    active = None
    for s in switches:
        if s["ts"] <= ts:
            active = s["node"]
        else:
            break
    return active


def get_per_egress_error_rate(window_min: int = 60) -> dict[str, tuple[int, int, float]]:
    """按出口节点归属 opencode 请求错误率。

    根因：9router requestDetails 无 egress 字段，出口由 daemon 实时 PUT 设置。
    故用 .egress_log.jsonl 的时间窗把请求关联到"当时活跃节点"。

    返回 {node: (total, errors, rate)}，仅含 window_min 内、且有归属的请求。
    ponytail: 时间窗关联推断（切换点精度 ~0.2s），非逐连接 join；足以区分"节点级质量"，
    不足以精确到单个 request。升级路径：让 9router 在 insertions 写入 outboundNode 字段。
    """
    try:
        with _db_lock:
            c = _get_db()
            # 宽松时间窗下推：先粗滤 7 天，避免 requestDetails 2000+ 行全量解析。
            # 9router 存为 ISO 'YYYY-MM-DDTHH:MM:SS.fffZ'，词典序比较等价于时间序。
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
            rows = c.execute(
                """SELECT timestamp, status FROM requestDetails
                   WHERE provider = ? AND timestamp >= ?
                   ORDER BY timestamp ASC""",
                (OPENCODE_FREE_PROVIDER, cutoff),
            ).fetchall()
        if not rows:
            return {}
        switches = _load_egress_log()
        if not switches:
            return {}
        # 先解析所有请求为 epoch，按最晚请求时间的 window_min 窗口过滤。
        # 原因：9router 与 daemon 可能时钟不一致，用 wall-clock cutoff 会造成越来越多
        # 合格请求被排除（skew）。相对于最晚请求的相对窗口更稳。
        parsed: list[tuple[int, str]] = []
        max_ts = 0
        for r in rows:
            try:
                ts = int(datetime.fromisoformat(str(r[0]).replace("Z", "+00:00")).timestamp())
            except Exception:
                continue
            parsed.append((ts, r[1]))
            if ts > max_ts:
                max_ts = ts
        cutoff = max_ts - window_min * 60
        buckets: dict[str, list[int]] = {}
        for ts, status in parsed:
            if ts < cutoff:
                continue
            node = _active_node_at(ts, switches)
            if node is None:
                continue  # 请求早于任何切换记录，无法归属
            buckets.setdefault(node, []).append(1 if status != "success" else 0)
        result = {}
        for node, err_flags in buckets.items():
            total = len(err_flags)
            errors = sum(err_flags)
            result[node] = (total, errors, errors / total if total else 0.0)
        return result
    except Exception as e:
        log(f"✗ per-egress 错误率失败: {e}")
        return {}


def _load_shared_bad_nodes() -> dict[str, float]:
    """Read bad nodes from shared .bad_nodes.json. Returns {node: expire_ts}, expired filtered out.
    ponytail: cached per-mtime to avoid repeated I/O (checked every 5s at most).
    与 proxy-global.js 的 mtime 缓存不对称：proxy-global 在每次 HTTP 请求时检测，
    daemon 每5秒检查一次。快速连续写入场景下 proxy-global 可能先于 daemon 感知。
    修复路径：统一为 mtime 检测或事件通知（当前延迟影响 ≤5s）。
    Validation: skip expired, skip > MAX_TTL, skip non-dict entries, accept both legacy "-any" and canonical "host:port-scheme" formats, cap at MAX_TTL."""
    global _shared_bad_cache, _shared_bad_mtime, _shared_bad_check_ts, _shared_bad_loaded
    now = _now_ts()
    max_allowed = now + _DEAD_TTL_MAX

    # 先检查是否需要刷新 cache（不更新 check_ts，避免加载失败时标记为已检查）
    with _state_lock:
        if now - _shared_bad_check_ts < 5:
            return dict(_shared_bad_cache)
    
    try:
        if os.path.exists(_BAD_NODES_FILE):
            mtime = os.path.getmtime(_BAD_NODES_FILE)
            if mtime == _shared_bad_mtime and _shared_bad_loaded:
                with _state_lock:
                    return dict(_shared_bad_cache)
            with open(_BAD_NODES_FILE) as f:
                nodes = json.load(f)
            filtered = {}
            for n, v in nodes.items():
                if isinstance(v, dict) and "expire" in v:
                    expire_ts = v.get("expire", 0)
                    if expire_ts > now and expire_ts <= max_allowed:
                        filtered[n] = expire_ts
                    continue
                if isinstance(v, (int, float)) and v > now and v <= max_allowed:
                    filtered[n] = v
                    continue
            # 只有加载成功后才更新 check_ts 和 cache
            with _state_lock:
                _shared_bad_cache = filtered
                _shared_bad_mtime = mtime
                _shared_bad_check_ts = now  # 只有成功加载后才更新检查时间
                _shared_bad_loaded = True
            return filtered
    except Exception:
        log("⚠ _load_shared_bad_nodes 异常: " + traceback.format_exc())
    
    with _state_lock:
        _shared_bad_loaded = True
    with _state_lock:
        return dict(_shared_bad_cache)

def _expected_proxy_url() -> str:
    """Expected proxy URL: always sing-box mixed port."""
    return f"http://127.0.0.1:{MIXED_PORT}"


def check_9router_outbound_proxy() -> bool:
    """Ensure OpenCode Free proxyPool + outboundProxyEnabled correct. Skip during fallback."""
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
            pool_id = s.get("providerStrategies", {}).get(OPENCODE_FREE_PROVIDER, {}).get("proxyPoolId")
            if not pool_id:
                log(f"⚠ 未找到 {OPENCODE_FREE_PROVIDER} proxyPool 配置")
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

            # 2. 恢复 outboundProxyEnabled（before proxy-global.js: bypass left outboundProxyEnabled=False）
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
        headers = {"Authorization": f"Bearer {get_singbox_secret()}"}
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

def _record_egress(node: str) -> None:
    """切换成功后记录 (ts,node)，供 per-egress 错误率归因。append-only 原子写。"""
    try:
        with open(_EGRESS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _now_ts(), "node": node}) + "\n")
    except Exception as e:
        log(f"✗ 写 egress_log 失败: {e}")


def switch_proxy(new_node: str) -> bool:
    """切换代理节点并验证切换生效。不 ban 旧节点——ban 只在错误触发路径处理。"""
    r, _ = singbox_api(f'/proxies/{CFG["singbox"]["selector"]}',
                       method="PUT", body={"name": new_node})
    if r != 200 and r != 204:
        log(f"✗ 切代理失败 ({new_node}): status={r}")
        return False

    # PUT 成功后，延迟 0.2s 等待变更生效，再通过 GET 验证 now == new_node
    time.sleep(0.2)
    _, verify_data = singbox_api(f'/proxies/{CFG["singbox"]["selector"]}')
    actual = verify_data.get("now") if verify_data else None
    if actual == new_node:
        log(f"✓ 切到: {new_node} (已验证)")
        _record_egress(new_node)
        return True
    else:
        log(f"✗ 切代理验证失败 ({new_node}): PUT 返回 {r} 但实际 now={actual}")
        return False


def _probe_node(node: str, timeout_ms: int, strict: bool = False) -> int | None:
    """统一节点探测核心函数

    Args:
        node: 节点名
        timeout_ms: 探测超时毫秒数
        strict: True=严格模式(连接错误/超时/SSL/DNS=明确失败), False=宽松模式(仅记录日志,给机会)

    Returns:
        延迟(ms) 如果成功, None 如果失败
    """
    try:
        r = requests.get(
            f'{CFG["singbox"]["clash_api"]}/proxies/{node}/delay',
            params={"timeout": timeout_ms,
                    "url": CFG["thresholds"]["node_test_url"]},
            headers={"Authorization": f"Bearer {get_singbox_secret()}"},
            timeout=timeout_ms / 1000 + 2,
        )
        if r.status_code == 200:
            data = r.json()
            delay = data.get("delay")
            if delay is not None:
                return delay
    except requests.exceptions.ConnectionError:
        if strict:
            return None
    except requests.exceptions.Timeout:
        if strict:
            return None
    except requests.exceptions.SSLError:
        if strict:
            return None
    except Exception as e:
        err_str = str(e).lower()
        if strict and ("dns" in err_str or "name" in err_str or "resolve" in err_str):
            return None
        # 宽松模式：记录日志但给机会
        if not strict:
            log(f"  ⚠ _probe_node {node} 异常: {e}")
    return None


def test_node_delay(node: str) -> int | None:
    """测试单个节点延迟（健康检查用），返回延迟ms或None"""
    return _probe_node(node, CFG["thresholds"]["node_test_timeout_ms"], strict=False)


def test_node_quick(node: str, timeout_ms: int = 3000) -> bool:
    """快速可用性探测（切换前验证用），返回True/False"""
    delay = _probe_node(node, timeout_ms, strict=True)
    return delay is not None


def _collect_test_nodes(nodes: list[str], current: str) -> list[str]:
    """收集待测节点（需在锁内调用）"""
    cleanup_dead_ttl()

    to_test = [n for n in nodes
               if n != "direct" and n != current and n not in _DEAD_NODES]
    # 仅测试 TTL 已到的死节点（允许恢复），保持指数退避语义
    now = time.time()
    for n in list(_DEAD_NODES):
        if n != "direct" and n != current and _DEAD_NODES_RELEASE_TS.get(n, 0) <= now:
            to_test.append(n)
            _DEAD_NODES.discard(n)
            _DEAD_NODES_RELEASE_TS.pop(n, None)
            log(f"  复活重测: {n}")
    return to_test


def _concurrent_test_delays(nodes: list[str]) -> dict[str, int | None]:
    """并发测试节点延迟，返回 {node: delay或None}"""
    results = {}
    futures = {}
    for n in nodes:
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
        results[node] = delay
    return results


def _apply_test_results(nodes: list[str], results: dict[str, int | None], now: float) -> tuple[set[str], set[str]]:
    """应用测试结果到全局状态（需在锁内调用）"""
    alive = set()
    dead = set()
    for node, delay in results.items():
        if delay is not None and delay < CFG["thresholds"]["node_test_timeout_ms"]:
            alive.add(node)
            _NODE_DELAYS[node] = delay
            _dead_fail_count.pop(node, None)
        else:
            dead.add(node)
            _NODE_DELAYS.pop(node, None)
            _dead_fail_count[node] = _dead_fail_count.get(node, 0) + 1
            fail_count = _dead_fail_count[node]
            if fail_count > len(_DEAD_TTL_STAGES):
                ttl = _DEAD_TTL_MAX
                ttl_desc = "永久"
            else:
                ttl = _DEAD_TTL_STAGES[fail_count - 1]
                ttl_desc = f"{ttl}s"
            alive_count = len(nodes) - len(_DEAD_NODES)
            # 保底: 永远允许 (1-ratio) 节点拉黑。边界: alive == floor 时不再拉黑
            # （strict < 导致 alive=3/10 时 still-blacklist，越过 70% 上限 — off-by-one）。
            if alive_count <= len(nodes) * _POOL_MIN_RATIO:
                log(f"  ⚠ {node} 跳过拉黑（保底: {alive_count}/{len(nodes)} <= {_POOL_MIN_RATIO*100:.0f}%）")
                _dead_fail_count.pop(node, None)
            else:
                _DEAD_NODES.add(node)
                _DEAD_NODES_RELEASE_TS[node] = now + ttl
                log(f"  ✗ {node} 死 (连续失败 {fail_count} 次, TTL {ttl_desc})")
    return dead, alive


# ============= 节点健康检查 =============
def batch_test_nodes(nodes: list[str], current: str) -> tuple[set[str], set[str]]:
    """并发批量测节点延迟，返回 (dead, alive)"""
    now = time.time()
    with _state_lock:
        to_test = _collect_test_nodes(nodes, current)

    if not to_test:
        return set(_DEAD_NODES), set()

    # 无锁并发测试
    results = _concurrent_test_delays(to_test)

    # 锁内应用结果
    with _state_lock:
        dead, alive = _apply_test_results(nodes, results, now)

    log(f"  节点测试: {len(alive)}✓/{len(dead)}✗")
    return dead, alive

def _run_health_check_async(nodes: list[str], current: str) -> tuple[set[str], set[str]]:
    """后台异步运行健康检查，供主循环非阻塞调用"""
    return batch_test_nodes(nodes, current)

def cleanup_dead_ttl():
    """清理过期的死节点 TTL，重置连续失败计数（给节点公平重试机会）

    Shared bad_nodes from proxy-global.js + daemon。
    三阶段设计：1) 锁内收集快照 2) 无锁网络探测 3) 锁内应用结果
    """
    # 阶段 1：锁内收集快照
    p429_bad = _load_shared_bad_nodes()  # 无锁读取

    with _state_lock:
        now = time.time()
        max_allowed = now + _DEAD_TTL_MAX
        import_max_allowed = now + 3600  # 导入时上限 1h

        # 收集过期节点快照
        expired = [n for n, ts in _DEAD_NODES_RELEASE_TS.items() if ts < now]

        # 收集导入所需数据快照
        p429_import = {}
        if p429_bad:
            for n, exp in p429_bad.items():
                if n not in _DEAD_NODES and exp > now and exp <= max_allowed:
                    p429_import[n] = min(exp, import_max_allowed)

        # 收集导出所需数据快照
        dead_snapshot = set(_DEAD_NODES)
        release_ts_snapshot = dict(_DEAD_NODES_RELEASE_TS)

    # 阶段 2：无锁网络探测（探测过期节点）
    revived = []
    for n in expired:
        probe_ok = test_node_quick(n, timeout_ms=3000)
        if probe_ok:
            revived.append(n)
        else:
            # 探测失败：记录但不修改状态，下阶段统一处理
            log(f"  ⚠ {n} TTL 到但探测失败，延长 TTL 5 分钟")

    # 预取 total_count 避免在锁内做网络请求（减少 _state_lock 持有时间）
    total_count = 0
    try:
        _, _data = singbox_api(f'/proxies/{CFG["singbox"]["selector"]}', timeout=2)
        total_count = len(_data.get("all", [])) if _data else 0
    except Exception:
        pass

    # 阶段 3：锁内应用结果
    with _state_lock:
        now = time.time()
        max_allowed = now + _DEAD_TTL_MAX

        # 应用复活结果
        for n in revived:
            _DEAD_NODES.discard(n)
            _DEAD_NODES_RELEASE_TS.pop(n, None)
            _dead_fail_count.pop(n, None)
        if revived:
            log(f"  释放 {len(revived)} 个黑名单节点 (TTL 到且探测通过，fail_count 已重置): {revived}")

        # 处理探测失败的过期节点：延长 TTL + fail_count+1
        failed_expired = [n for n in expired if n not in revived]
        for n in failed_expired:
            if n in _DEAD_NODES_RELEASE_TS:  # 可能已被并发移除
                _DEAD_NODES_RELEASE_TS[n] = time.time() + 300  # 额外 5 分钟
                _dead_fail_count[n] = _dead_fail_count.get(n, 0) + 1

        # 导入 proxy-global 的坏节点
        for n, capped_exp in p429_import.items():
            if n not in _DEAD_NODES:
                # 再次验证（双重检查模式）
                if capped_exp <= now or capped_exp > max_allowed:
                    continue
                _DEAD_NODES.add(n)
                _DEAD_NODES_RELEASE_TS[n] = capped_exp

        # 导出到共享文件（total_count 已预取）
        _export_dead_nodes_locked(total_count)


def _export_dead_nodes_locked(total_count: int = 0):
    """导出死节点到共享文件（需在 _state_lock 内调用）。total_count 应在锁外预取。"""
    if not _DEAD_NODES:
        return

    try:
        # 安全阀：Clash API 不通或节点数为 0 → Clash 自身有问题。
        # 注意：只跳过本次导出，不清空已标记的 _DEAD_NODES——否则单次 API 抖动
        # 会抹掉 proxy-global.js 以 "-any" 键写入的共享坏节点状态（原 bug）。
        if total_count == 0:
            log("  ⚠ Clash API 不通或节点数为 0，跳过 bad_nodes 导出（保留已标记状态）")
            return

        # 防全误判：死节点超过总节点 60% 时跳过导出
        if len(_DEAD_NODES) > total_count * (1 - _POOL_MIN_RATIO):
            log(f"  ⚠ 跳过 bad_nodes 导出: {len(_DEAD_NODES)}/{total_count} 死节点 >60%")
            return

        shared = {}
        if os.path.exists(_BAD_NODES_FILE):
            with open(_BAD_NODES_FILE) as f:
                shared = json.load(f)

        # 只追加 daemon 发现的死节点，不覆盖 proxy-global 已有的条目
        for n in _DEAD_NODES:
            if n not in shared:
                shared[n] = {"expire": _DEAD_NODES_RELEASE_TS.get(n, _now_ts() + BAD_NODES_TTL), "reason": "daemon_dead"}

        tmp = _BAD_NODES_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(shared, f)
        os.replace(tmp, _BAD_NODES_FILE)
    except Exception:
        pass  # 写失败不影响主逻辑



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
    """Pick from candidates using P2C + adaptive score.

    Mode is read from _strategy_mode under _state_lock to avoid race.
    normal:   delay优先, 同延迟按调用次数 (ds, -calls)
    pressure: 调用次数优先, 同次数按延迟 (-calls, ds)
    """
    pool = [n for n in candidates if n not in dedup_skip] or candidates
    with _state_lock:
        usage_counts = {n: call_map.get(n, 0) + _NODE_CALL_COUNTS.get(n, 0) for n in candidates}
        delays = dict(_NODE_DELAYS)
        mode = _strategy_mode
    all_delays = [d for d in delays.values() if d is not None]
    median, dmin, dmax = _precompute_delay_stats(all_delays)

    def _score(n):
        calls = usage_counts.get(n, 0)
        ds = _delay_score(n, delays, median, dmin, dmax)
        if mode == "pressure":
            return (-calls, ds)   # 均匀分散: 调用少的优先
        return (ds, -calls)       # 正常: 延迟低的优先

    k = min(2, len(pool))
    chosen = _random.sample(pool, k)[0] if k < 2 else max(_random.sample(pool, k), key=_score)
    return chosen


def pick_next_node(current: str, all_nodes: list, fail_set: set, count_call: bool = True) -> str | None:
    """P2C + 自适应分数：每次随机抽两个候选，选分数高的。

    自适应策略:
    - normal 模式:   延迟优先 (ds, -calls) — 快节点优先用
    - pressure 模式: 调用次数优先 (-calls, ds) — 均匀分散
    count_call=False 时, 选中节点不计入调用次数（用于 429 错误触发的轮换）

    增加: 选中后快速探测可用性，失败则标记为死节点并重选（最多 3 次）。
    """
    global _strategy_mode
    candidates = _filter_candidates(current, all_nodes, fail_set)

    if not candidates:
        log(f"⚠ 无可用节点 (dead={len(_DEAD_NODES)} fail={len(fail_set)} current={current or 'none'} total={len(all_nodes)})")
        return None

    call_map, ip_map = _load_candidate_stats(candidates)
    dedup_skip = _dedup_by_ip(candidates, call_map, ip_map)

    # 最多尝试 3 个候选，避免死循环
    for attempt in range(3):
        chosen = _p2c_select(candidates, dedup_skip, call_map)

        # 快速可用性探测（3s 超时），不更新 _NODE_DELAYS
        if test_node_quick(chosen, timeout_ms=3000):
            with _state_lock:
                _recent_nodes.append(chosen)
                if count_call:
                    _NODE_CALL_COUNTS[chosen] = _NODE_CALL_COUNTS.get(chosen, 0) + 1
            return chosen
        else:
            # 探测失败：指数退避 TTL（与健康检查一致），从候选中移除，重试
            with _state_lock:
                # Only increment fail_count if this is a new failure (not already in _DEAD_NODES)
                if chosen in _DEAD_NODES:
                    # Already tracked, use existing fail_count for TTL calculation
                    fail_count = _dead_fail_count.get(chosen, 0)
                else:
                    _DEAD_NODES.add(chosen)
                    fail_count = _dead_fail_count.get(chosen, 0) + 1
                    _dead_fail_count[chosen] = fail_count
                if fail_count > len(_DEAD_TTL_STAGES):
                    ttl = _DEAD_TTL_MAX
                    ttl_desc = "永久"
                else:
                    ttl = _DEAD_TTL_STAGES[fail_count - 1]
                    ttl_desc = f"{ttl}s"
                _DEAD_NODES_RELEASE_TS[chosen] = time.time() + ttl
            log(f"  ✗ 快速探测失败，标记死节点并重选: {chosen} (失败{fail_count}次, TTL={ttl}s) (尝试 {attempt + 1}/3)")
            # 从候选中移除
            candidates = [n for n in candidates if n != chosen]
            dedup_skip.add(chosen)

            if not candidates:
                log(f"⚠ 重选后无可用节点")
                return None

    log(f"⚠ 3 次快速探测均失败，放弃")
    return None

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
                UPDATE settings SET data = json_set(COALESCE(data, '{}'),
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
        with _RESTART_LOCK:
            restart_count = _RESTART_COUNT
        log(f"⚠ 重启 sing-box (连续失败 {restart_count}/{CFG['singbox']['max_restart_attempts']})...")

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

        # 单文件轮转：>10MB 时备份为 .1 并截断
        log_path = os.path.join(log_dir, "singbox.log")
        try:
            if os.path.getsize(log_path) > 10 * 1024 * 1024:
                bak = log_path + ".1"
                if os.path.exists(bak):
                    os.remove(bak)
                shutil.move(log_path, bak)
                log(f"  singbox.log 轮转 (>{10}MB)")
        except Exception as e:
            log(f"⚠ singbox 日志轮转失败: {e}")

        # 使用文件路径而非打开的句柄，避免 fd 泄漏
        # subprocess 会自动打开/关闭文件
        with open(log_path, "a") as log_file:
            subprocess.Popen(
                [singbox_bin, "run", "-c", config_file],
                stdout=log_file, stderr=subprocess.STDOUT,
                start_new_session=True)
        time.sleep(CFG["singbox"]["startup_grace_sec"])

        if is_singbox_alive():
            with _RESTART_LOCK:
                _RESTART_COUNT = 0
            log("✓ sing-box 重启成功")
            # 恢复代理
            _set_outbound_proxy(url=_expected_proxy_url(), enabled=True)
            return True
        else:
            with _RESTART_LOCK:
                _RESTART_COUNT += 1
                cur_rc = _RESTART_COUNT
            log(f"✗ sing-box 重启失败 (第 {cur_rc} 次)")
            # 重启失败也要恢复代理，避免 9router 裸奔
            _set_outbound_proxy(url=_expected_proxy_url(), enabled=True)
            return False
    except Exception as e:
        with _RESTART_LOCK:
            _RESTART_COUNT += 1
        log(f"✗ sing-box 重启异常: {e}")
        # 异常分支也要恢复代理
        _set_outbound_proxy(url=_expected_proxy_url(), enabled=True)
        return False
    finally:
        _restart_lock.release()


# ============= 订阅刷新 =============
# 多个订阅源合并：okz（主力）+ hd.23n.club + dasho.pqjc.site + dasho.xn--cp3a08l.com（补充）
_SUB_URLS = [
    CFG["okz"]["sub_url"],
    CFG.get("hd23n", {}).get("sub_url", ""),
    CFG.get("dasho", {}).get("sub_url", ""),
    CFG.get("dasho_alt", {}).get("sub_url", ""),
]

def refresh_subscription() -> bool:
    """拉所有订阅源 + merge 去重 + 生成 sing-box config + 重启 sing-box"""
    log("🔄 刷新订阅...")
    try:
        parse_script = os.path.join(os.path.dirname(__file__), "parse_subscription.py")
        config_file = CFG["singbox"]["config_file"]
        urls = [u for u in _SUB_URLS if u]
        if not urls:
            log("✗ 无可用订阅源")
            return False
        subprocess.run(
            [sys.executable, parse_script,
             "--out", config_file,
             "--urls", *urls],
            capture_output=True, text=True, timeout=120)
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
    # Log first, then fetch (avoid hanging before any output)
    log("=" * 50)
    log("auto-switch-ip daemon v2 启动 (加固版)")
    log(f"  sing-box: {CFG['singbox']['clash_api']}")

    all_nodes = get_all_proxy_nodes()
    log(f"  节点数: {len(all_nodes)}")
    log(f"  轮换: 每 {CFG['intervals']['proactive_rotate_sec']}s P2C")
    log(f"  健康: 每 {CFG['intervals']['node_health_sec']}s 批量测延迟")
    log(f"  订阅: 每 {CFG['intervals']['subscription_refresh_sec']}s 刷新")
    log(f"  Watchdog: 每 {CFG['intervals']['singbox_watchdog_sec']}s")
    log(f"  自适应: 429率>{CFG['thresholds']['pressure_error_rate']:.0%}→均匀分散, <{CFG['thresholds']['normal_error_rate']:.0%}→延迟优先")
    log(f"  IP去重: 同出口IP保留调用次数最低的节点")
    log(f"  调用计数: 仅统计成功调用, 429轮换不计入")
    log(f"  interrupt_exist_connections: false (不杀现有连接)")
    log(f"  错误触发: 连续 {RECENT_WINDOW} 请求中成功率 <{CFG['thresholds']['opencode_min_success_rate']*100:.0f}%")
    log("=" * 50)

    # 初始化 node_info 表
    init_node_info_db()

    # 状态
    cooldown_until = 0
    last_singbox_check = 0
    last_health_check = 0
    last_sub_refresh = time.time()  # 启动时不立即触发刷新
    last_node_info_cleanup = time.time()
    last_proxy_check = 0
    last_summary_ts = 0
    last_proactive_rotate = 0

    global _RESTART_COUNT, _NODE_CALL_RESET_TS, _last_wal_checkpoint, _strategy_mode
    with _RESTART_LOCK:
        _RESTART_COUNT = 0
    _NODE_CALL_RESET_TS = time.time()

    # 启动检查：如果首次查询失败，等待几秒再重试（可能正在重启）
    if not all_nodes:
        log("⚠ sing-box 首次查询超时，等待5s重试...")
        time.sleep(5)
        all_nodes = get_all_proxy_nodes()
        if not all_nodes:
            log("⚠ sing-box 不通，尝试启动...")
            _set_outbound_proxy(enabled=False)  # 先 bypass
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
            # ---- 0. 9router 代理配置检查 (每 60s) ----
            if now - last_proxy_check > CFG["intervals"]["proxy_check_sec"]:
                check_9router_outbound_proxy()
                last_proxy_check = now

            # ---- 1. 读取错误率 ----
            total, errors, _, consecutive_errors = get_9router_error_rate()

            # ---- 1b. 自适应策略切换（带回滞） ----
            error_rate = errors / total if total > 0 else 0
            with _state_lock:  # 与 _p2c_select 读取同锁，保证 mode 与 usage_counts/delays 快照一致
                old_mode = _strategy_mode
                _strategy_mode = _update_strategy_mode(
                    _strategy_mode, error_rate, total,
                    CFG["thresholds"]["pressure_error_rate"], CFG["thresholds"]["normal_error_rate"],
                )
                switched = old_mode != _strategy_mode
            if switched:
                log(f"🔄 策略切换: {old_mode} → {_strategy_mode} (429率={error_rate:.0%}, {errors}/{total}, 连续失败={consecutive_errors})")

            # ---- 2b. 错误触发轮换 ----
            should_error = (total >= 2 and errors / total > (1 - CFG["thresholds"]["opencode_min_success_rate"])) or consecutive_errors >= 2
            # ponytail: threshold=2 instead of 3 to catch 100% failure in low-traffic windows
            in_cooldown = now < cooldown_until

            # ---- 3. 主动轮换（每 N 秒加权选新节点） ----
            if now - last_proactive_rotate > CFG["intervals"]["proactive_rotate_sec"]:
                current = cached_current_proxy
                nodes = cached_all_nodes

                if nodes:
                    next_node = pick_next_node(current or "", nodes, set())
                    if next_node and next_node != current:
                        reason = "主动轮换"
                        delay_info = f" ({_NODE_DELAYS[next_node]}ms)" if next_node in _NODE_DELAYS else ""
                        log(f"→ 切代理 ({reason}): {format_node_display(current)} -> {format_node_display(next_node)}{delay_info}")
                        if switch_proxy(next_node):
                            update_node_info_async(next_node)
                    elif not next_node:
                        log(f"⚠ 主动轮换: 无可用节点 (dead={len(_DEAD_NODES)})")

                last_proactive_rotate = now

            # ---- 3b. 清理过期 node_info (每 10 分钟) ----
            if now - last_node_info_cleanup > 600:
                last_node_info_cleanup = now
                try:
                    current_all = cached_all_nodes
                    if current_all:
                        valid_set = set(current_all)
                        with _db_lock:
                            c = _get_db()
                            # 批量删除过期节点：单条参数化 DELETE + 显式 commit，
                            # 避免循环内 DELETE 因未提交被 SQLite 隐式事务丢弃（原 bug）。
                            placeholders = ",".join("?" * len(valid_set))
                            c.execute(
                                f"DELETE FROM node_info WHERE node NOT IN ({placeholders})",
                                tuple(valid_set),
                            )
                            c.commit()
                            log(f"  ♻ 清理过期 node_info 完成 (保留 {len(valid_set)} 个有效节点)")
                except Exception as e:
                    log(f"  ⚠ 清理 node_info 失败: {e}")

            # ---- 3c. 错误触发轮换（错误率高时立即切） ----
            if should_error and not in_cooldown:
                # 拿当前真实节点（不用缓存，确保标记正确）
                current = get_current_proxy()
                nodes = cached_all_nodes

                if nodes:
                    if current:
                        with _state_lock:
                            _DEAD_NODES.add(current)
                            _DEAD_NODES_RELEASE_TS[current] = now + BAD_NODES_TTL  # TTL 5 分钟

                    check_and_handle_fallback(nodes, current or "")

                    # 错误触发轮换不计入调用次数（429 的请求不算成功调用）
                    next_node = pick_next_node(current or "", nodes, set(), count_call=False)
                    if next_node:
                        reason = f"err={errors}/{total}"
                        delay_info = f" ({_NODE_DELAYS[next_node]}ms)" if next_node in _NODE_DELAYS else ""
                        log(f"→ 切代理 ({reason}): {format_node_display(current)} -> {format_node_display(next_node)}{delay_info}")
                        if switch_proxy(next_node):
                            cooldown_until = now + CFG["intervals"]["cooldown_after_switch_sec"]
                            update_node_info_async(next_node)
                    else:
                        log(f"⚠ 无可用节点 (dead={len(_DEAD_NODES)})")

            # ---- 5. 节点健康检查 (每 300s，后台异步执行不阻塞主循环) ----
            if now - last_health_check > CFG["intervals"]["node_health_sec"]:
                if _initial_health_done.is_set():
                    # 健康检查前直接从 Clash API 获取最新节点列表
                    try:
                        hc_r, hc_data = singbox_api(f'/proxies/{CFG["singbox"]["selector"]}')
                        if hc_data:
                            cached_all_nodes = hc_data.get("all", [])
                            cached_current_proxy = hc_data.get("now")
                    except Exception:
                        pass
                    nodes = cached_all_nodes
                    current = cached_current_proxy
                    if nodes and current:
                        # 启动后台健康检查，避免阻塞主循环（原子检查+提交）
                        with _state_lock:
                            if _health_check_future is None or _health_check_future.done():
                                _health_check_nodes = nodes
                                _health_check_current = current
                                _health_check_future = _io_pool.submit(_run_health_check_async, nodes, current)
                        last_health_check = now
            else:
                # 检查后台健康检查是否完成
                with _state_lock:
                    future_done = _health_check_future is not None and _health_check_future.done()
                    if future_done:
                        future = _health_check_future
                        check_nodes = _health_check_nodes
                        check_current = _health_check_current
                        _health_check_future = None
                        _health_check_nodes = None
                        _health_check_current = None
                    else:
                        future = None

                if future is not None:
                    try:
                        dead, alive = future.result()
                        # 清理活过来的节点
                        with _state_lock:
                            for n in list(_DEAD_NODES):
                                if n in alive:
                                    _DEAD_NODES.discard(n)
                                    _DEAD_NODES_RELEASE_TS.pop(n, None)
                        # 降级检查
                        if check_nodes and check_current:
                            check_and_handle_fallback(check_nodes, check_current)
                    except Exception as e:
                        log(f"⚠ 后台健康检查异常: {e}")

            # ---- 6. 订阅刷新 (每 1h，后台执行避免阻塞主循环) ----
            if now - last_sub_refresh > CFG["intervals"]["subscription_refresh_sec"]:
                _io_pool.submit(refresh_subscription)
                last_sub_refresh = now

            # ---- 7. Sing-box watchdog ----
            if now - last_singbox_check > CFG["intervals"]["singbox_watchdog_sec"]:
                if not is_singbox_alive():
                    with _RESTART_LOCK:
                        restart_count = _RESTART_COUNT
                    if restart_count < CFG["singbox"]["max_restart_attempts"]:
                        restart_singbox()
                        cooldown_until = now + 30  # 重启后冷却
                    else:
                        log(f"🚨 sing-box 连续重启 {restart_count} 次失败，降级")
                        check_and_handle_fallback([], "")
                else:
                    # 恢复后清除重启计数
                    with _RESTART_LOCK:
                        if _RESTART_COUNT > 0:
                            _RESTART_COUNT = 0
                            log("✓ sing-box watchdog 恢复")

                last_singbox_check = now

            # ---- 8. 节点调用计数重置 (每 20min，pressure 模式下跳过) ----
            if now - _NODE_CALL_RESET_TS > 1200:
                if _strategy_mode == "pressure":
                    log("  ♻ 调用计数跳过重置 (pressure 模式，保持均匀分散)")
                else:
                    with _state_lock:
                        _NODE_CALL_COUNTS.clear()
                    with _db_lock:
                        _get_db().execute("UPDATE node_info SET call_count=0")
                        _get_db().commit()
                    log("  ♻ 节点调用计数已重置")
                _NODE_CALL_RESET_TS = now

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
            with _RESTART_LOCK:
                rc_display = _RESTART_COUNT
            log_health(f"节点={len(cached_all_nodes)} 错误={errors}/{total} "
                       f"err>{(err_threshold*100):.0f}%→切"
                       f"冷却至={time.strftime('%H:%M:%S', time.localtime(cooldown_until)) if cooldown_until>0 else '-'}"
                       f" dead={dead_count} 策略={_strategy_mode}"
                       f" singbox={'🟢' if rc_display==0 else '🔴'}")

            time.sleep(CFG["intervals"]["monitor_sec"])

        except KeyboardInterrupt:
            log("=== 退出 ===")
            break
        except Exception as e:
            log(f"✗ 主循环异常: {e}")
            log(traceback.format_exc())
            time.sleep(10)

def cli_rotate():
    """CLI: pick next node and switch, print JSON result for 9router to consume."""
    import sys, json as _json
    current = get_current_proxy()
    all_nodes = get_all_proxy_nodes()
    if not all_nodes:
        print(_json.dumps({"error": "no nodes available"}))
        sys.exit(1)
    next_node = pick_next_node(current or "", all_nodes, set())
    if not next_node:
        print(_json.dumps({"error": "no suitable node found", "current": current}))
        sys.exit(1)
    ok = switch_proxy(next_node)
    info = get_node_info(next_node) or {}
    print(_json.dumps({
        "ok": ok,
        "from": current,
        "to": next_node,
        "ip": info.get("ip", ""),
        "country": info.get("country", ""),
    }))

if __name__ == "__main__":
    import sys, signal

    def _sigterm_handler(signum, frame):
        log("收到 SIGTERM，优雅关闭中...")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)
    if len(sys.argv) > 1 and sys.argv[1] == "rotate":
        cli_rotate()
    else:
        main()