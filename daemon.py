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
import fcntl
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
from config import CFG, NODE_INFO_DB, OPENCODE_FREE_PROVIDER, RECENT_WINDOW, REJECT_WINDOW, BAD_NODES_TTL, BAD_NODES_MAX_TTL, MIXED_PORT, LOG_FORMAT, get_singbox_secret

# 全局复用连接 - 避免每次操作开/关 SQLite 连接
_db_conn: sqlite3.Connection | None = None
_db_lock = threading.RLock()  # RLock: init_node_info_db 里 _get_db 需重入
_BAD_NODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bad_nodes.json")
# 出口节点归属日志：每次成功切换追加 (ts,node)，供 per-egress 错误率归因用。
_EGRESS_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".egress_log.jsonl")

def _egress_log_path() -> str:
    """egress_log 路径（EGRESS_LOG_FILE_PATH 可覆盖，测试用隔离；与 proxy-global.js
    的 BAD_NODES_FILE_PATH 同款）。读写必须都走这里，否则测试会写到生产文件。"""
    return os.environ.get("EGRESS_LOG_FILE_PATH") or _EGRESS_LOG_FILE
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

# 429 冷却池：与 _DEAD_NODES 语义不同——节点本身健康，只是刚被上游限流，
# 需要短暂让开让上游的速率窗口重置。不进指数退避、不影响 bad_nodes 导出。
# 实测（1998 条请求 × 1836 次切换归属）：429 后 0-5s 重试成功率 62.7%，
# 5-10s 已回升到 97.7%，10s 后稳定 93-98% —— 重置窗口是秒级，不是分钟级。
_COOLDOWN_NODES: dict[str, float] = {}     # node -> 可再次被选中的时刻

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
_last_successful_switch_ts: float = 0  # 上次成功切换的 epoch 秒，用于切分错误检测窗口
# WARP 隧道重连状态（opencode 专用出口，与节点轮换无关）
_WARP_LOCK = threading.Lock()
_last_warp_reconnect_ts: float = 0  # 上次发起重连的 epoch 秒，用于切分 abuse 403 窗口
# 上次换线**生效**的时刻。主动轮换按此切窗口：发起时刻早于生效时刻数秒，
# 换线期间旧出口仍在服务流量，用发起时刻会把旧线消耗算进新线配额。
_last_warp_switch_ok_ts: float = 0
_warp_cooldown_until: float = 0     # 冷却截止，防重连风暴
# WARP 出口 IP 使用记录：ip -> {ts: 最后使用时刻, blocked: 是否被 403/429 标记过}。
# 换线前校验目标线的当前出口 IP，窗口内用过就先刷新会话再切 ——
# 切回一个刚被 429/403 标记的地址等于没换。
# 落盘 .warp_ip_used.json，daemon 重启后保留污染记录。
_WARP_IP_USED: dict[str, dict] = {}
_WARP_IP_LOCK = threading.Lock()   # _WARP_IP_USED 跨线程读写（换线线程 + 主循环）
_WARP_IP_USED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".warp_ip_used.json")
# 当前 WARP 出口 IP，仅供日志标签用（健康行每 10s 打一条，不能现场探测）。
_ACTIVE_WARP_IP: str | None = None
# tag -> {"sel": 上次被选为出口的时刻, "ip": 已知的当前出口 IP}。
# 选线要挑「最久没被选过」的线：只看 IP 使用表不行 —— 出窗口的条目会被
# 清理，清理后所有备线并列最老，又退化成 members 顺序（实测 D/E 被选中 0 次）。
_WARP_LINE_STATE: dict[str, dict] = {}
_WARP_LINE_LOCK = threading.Lock()   # 主循环 / 换线线程 / 补刷线程三方读写
_last_proactive_warp_check: float = 0  # 主动轮换判据的上次查询时刻
_last_warp_prewarm_ts: float = 0      # 上次预热备线的时刻
# 出口 IP 可用周期样本：每段 = 换线生效 → 首次被 403/429 判不可用。
# {"ip", "cycle", "ended_at", "ok"}，只保留 24h。落盘跨重启保留。
_WARP_IP_CYCLES: list[dict] = []
_WARP_CYCLES_LOCK = threading.Lock()
_WARP_IP_CYCLES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".warp_ip_cycles.json")
_WARP_MIN_CYCLE_SEC: float = 0.0   # 0 = 无样本，回退到按请求数主动轮换
_last_cycle_recalc_ts: float = 0


def _warp_ip_used_path() -> str:
    """IP 使用记录路径（WARP_IP_USED_FILE_PATH 可覆盖，测试用隔离；与
    `_egress_log_path()` 同款）。读写都必须走这里，否则测试的桩数据会写进生产文件。"""
    return os.environ.get("WARP_IP_USED_FILE_PATH") or _WARP_IP_USED_FILE


def _load_warp_ip_used() -> None:
    """启动时加载落盘的 IP 使用记录（旧版文件里的 `seen` 键直接忽略）。"""
    try:
        with open(_warp_ip_used_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        # 兼容两种格式：{ip: {...}} 或 {"used": {...}}
        used = data.get("used", data) if isinstance(data, dict) else {}
        for ip, v in used.items():
            if isinstance(v, dict) and "ts" in v:
                _WARP_IP_USED[ip] = v
            elif isinstance(v, (int, float)):
                _WARP_IP_USED[ip] = {"ts": float(v), "blocked": False}
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"✗ 加载 .warp_ip_used.json 失败: {e}")


def _write_json_atomic(path: str, payload: dict) -> None:
    """先写同目录 .tmp 再 os.replace，失败时记日志。

    这两个文件是进程重启后唯一能恢复「哪些地址已被烧」的记录。直接 open(w)
    会把原文件清零，此时崩溃/掉电就留下半截 JSON，下次启动解析失败 → 记录
    全丢 → 窗口内的脏地址当干净地址再切回去（实测 22:22 重启正是这么丢了记录，
    22:27 切回已烧地址，403 持续 144s）。
    """
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception as e:
        log(f"✗ 写 {os.path.basename(path)} 失败: {e}")


def _save_warp_ip_used() -> None:
    """把 IP 使用记录落盘。"""
    _write_json_atomic(_warp_ip_used_path(), {"used": _WARP_IP_USED})


def _warp_ip_cycles_path() -> str:
    """周期样本路径（WARP_IP_CYCLES_FILE_PATH 可覆盖，测试用隔离）。"""
    return os.environ.get("WARP_IP_CYCLES_FILE_PATH") or _WARP_IP_CYCLES_FILE


def _load_warp_ip_cycles() -> None:
    """启动时加载周期样本，丢弃 24h 前的旧样本。"""
    try:
        with open(_warp_ip_cycles_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        kept = _prune_warp_cycles(data.get("cycles", data) if isinstance(data, dict) else data,
                                  time.time())
        with _WARP_CYCLES_LOCK:
            _WARP_IP_CYCLES[:] = kept
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"✗ 加载 .warp_ip_cycles.json 失败: {e}")


def _prune_warp_cycles(cycles, now: float) -> list[dict]:
    """只留 24h 内、字段合法的样本。纯函数，便于单测。"""
    keep_after = now - 86400
    out = []
    for c in cycles if isinstance(cycles, list) else []:
        if not isinstance(c, dict):
            continue
        try:
            if float(c["ended_at"]) >= keep_after and float(c["cycle"]) >= 0:
                out.append(c)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _record_warp_cycle(ip: str | None, cycle: float, now: float, ok: int = 0) -> None:
    """记一条可用周期样本并落盘（只保留 24h）。

    cycle == 0 表示「切上去第一条请求就 403/429」（ok=0）。这类样本要留档
    以便日志统计，但**不参与**最小周期计算 —— 24h 实测 80 段里 13 段如此，
    算进去会让最小值恒为 1s，主动轮换变成每 tick 都换。
    """
    if not ip or cycle < 0:
        return
    with _WARP_CYCLES_LOCK:
        _WARP_IP_CYCLES.append({"ip": ip, "cycle": float(cycle),
                                "ended_at": now, "ok": int(ok)})
        _WARP_IP_CYCLES[:] = _prune_warp_cycles(_WARP_IP_CYCLES, now)
        snapshot = list(_WARP_IP_CYCLES)
    _write_json_atomic(_warp_ip_cycles_path(), {"cycles": snapshot})


# 主动轮换的最小生效周期下限。实测切换动作本身耗时 p50=4s / p90=14s；
# 比 5s 更快地再切一次只会打断尚未生效的切换，隧道永远切不出一条能用的线。
_WARP_MIN_CYCLE_FLOOR_SEC = 5.0


def _recompute_min_warp_cycle(now: float) -> float:
    """重算 24h 内「成功服务过 ≥1 条后出错」的段的**典型**周期，返回生效值。

    取中位而非最小（原因见函数体内的注释）。只统计 cycle > 0 且 ok > 0 的
    样本：cycle=0 是「切上去第一条请求就错」，不反映可用寿命。
    无样本返回 0（调用方回退到按请求数轮换）。
    """
    global _WARP_MIN_CYCLE_SEC
    with _WARP_CYCLES_LOCK:
        recent = _prune_warp_cycles(_WARP_IP_CYCLES, now)
        usable = [c for c in recent
                  if float(c["cycle"]) > 0 and int(c.get("ok", 0) or 0) > 0]
    total = len(recent)
    dropped = total - len(usable)
    if not usable:
        _WARP_MIN_CYCLE_SEC = 0.0
        log(f"↻ 最小可用周期重算: 无可用样本（24h {total} 条，首错即废 {dropped} 条已排除），"
            f"主动轮换回退到按请求数")
        return 0.0
    # 用中位而非最小值：最小值由「最倒霉的那一条线」决定 —— 24h 窗口里只要
    # 出现一条成功服务 1 条后就死的段，最小值就塌到 0.1s 级，主动轮换变成
    # 每几秒换一次（实测 98 条可用样本：min 0.1s、p10 2.4s、中位 20.2s、
    # p75 21.5s）。中位才代表「一条线典型能服务多久」。
    vals = sorted(float(c["cycle"]) for c in usable)
    median = vals[len(vals) // 2]
    effective = max(median, _WARP_MIN_CYCLE_FLOOR_SEC)
    _WARP_MIN_CYCLE_SEC = effective
    log(f"↻ 典型可用周期重算: 中位 {median:.1f}s（min {vals[0]:.1f}s / "
        f"p25 {vals[len(vals)//4]:.1f}s / p75 {vals[3*len(vals)//4]:.1f}s，"
        f"可用样本 {len(usable)} 条，首错即废 {dropped} 条已排除）→ 生效 {effective:.0f}s")
    return effective


_WARP_REFRESH_LOCK = threading.RLock()
_WARP_REFRESHING: set[str] = set()  # 正在延迟刷新的线，防重复调度
_last_proxy_pool_check: float = 0  # 上次探测轮换池可用性的 epoch 秒（双向兜底）
_proxy_dwell_since: float = 0  # 落到轮换池的时刻；0 = 现不在 proxy 上
_proxy_fail_streak: int = 0     # 池健康探测连续失败次数（回切判据之一）
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

def get_9router_error_rate(max_backoff_ts: int | None = None) -> tuple[int, int, list, int, int]:
    """从 SQLite 读最近 N 个 OpenCode Free 请求，
    返回 (total, errors, details_list, consecutive_errors, reject_ratio)

    reject_ratio：最近 REJECT_WINDOW 条中 403 占全部错误的比例。OpenCode 免费层只允许
    其自家客户端使用，403 FreeTierError 是客户端指纹拒绝，与出口 IP 无关——据此避免无效轮换。

    明确针对 OpenCode Free provider（`requestDetails.provider = "opencode"`）。
    其他免费 provider 不参与切换决策——它们不触发 IP 轮换。

    检测策略：两层检测
    - 窗口检测：最近 RECENT_WINDOW 条的总错误率
    - 连续检测：从最新条往前数，连续失败的个数（防止 success 稀释 error）
    任一超过阈值就触发切换。

    max_backoff_ts：上次成功切换的 epoch 秒。传入后只统计切换点之后的请求，
    避免新节点刚上线就背切换前旧节点留下的错误（6-16s 内被误拉黑）。
    """
    try:
        with _db_lock:
            c = _get_db()
            rows = c.execute(f"""
                SELECT provider, model, status, timestamp, data
                FROM requestDetails
                WHERE provider = ?
                ORDER BY timestamp DESC LIMIT ?
            """, (OPENCODE_FREE_PROVIDER, RECENT_WINDOW)).fetchall()
            reject_rows = c.execute("""
                SELECT status, timestamp, data
                FROM requestDetails
                WHERE provider = ?
                ORDER BY timestamp DESC LIMIT ?
            """, (OPENCODE_FREE_PROVIDER, REJECT_WINDOW)).fetchall()
        if max_backoff_ts is not None:
            # 9router timestamp 为 ISO UTC 字符串，词典序比较等价于时间序
            cutoff = datetime.fromtimestamp(max_backoff_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            rows = [r for r in rows if str(r["timestamp"]) > cutoff]
            reject_rows = [r for r in reject_rows if str(r["timestamp"]) > cutoff]
        reject_rows = [dict(r) for r in reject_rows]

        if not rows:
            return 0, 0, [], 0, 0

        recent = [dict(r) for r in rows]
        total = len(recent)
        errors = sum(1 for r in recent if r.get("status") != "success")

        consecutive_errors = 0
        for r in recent:
            if r.get("status") != "success":
                consecutive_errors += 1
            else:
                break

        rejected_403 = reject_errors = 0
        for r in reject_rows:
            if r.get("status") == "success":
                continue
            reject_errors += 1
            try:
                resp = (json.loads(r.get("data") or "{}") or {}).get("response") or {}
            except (ValueError, TypeError):
                continue
            if resp.get("status") == 403:
                rejected_403 += 1
        reject_ratio = rejected_403 / reject_errors if reject_errors else 0.0

        if errors > 0:
            err_models = set(r.get("model", "?") for r in recent if r.get("status") != "success")
            log(f"  [OpenCode Free] {errors}/{total} 请求错误 | 连续失败={consecutive_errors} | 403占比={reject_ratio:.0%} | 失败模型: {', '.join(sorted(err_models))}")

        return total, errors, recent, consecutive_errors, reject_ratio
    except Exception as e:
        log(f"✗ 读 SQLite 失败: {e}")
        return 0, 0, [], 0, 0

def _load_egress_log() -> list[dict]:
    """读 .egress_log.jsonl → 按 ts 升序的切换记录 [(ts, node), ...]。"""
    path = _egress_log_path()
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
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
    # 仅返回窗口内的记录（裁剪见 prune_egress_log）——读取路径不做写操作：
    # 这里的 read-modify-write 会与 daemon 的 append 竞争，丢掉期间的切换记录，
    # 而 _active_node_at 会因此把后续请求归因给上一个节点（错误的出口）。
    keep_after = int(time.time()) - 7 * 86400
    return [d for d in out if d.get("ts", 0) >= keep_after]


def prune_egress_log():
    """裁剪 .egress_log.jsonl 到最近 7 天。由 daemon 主循环周期性调用。

    单纯 read → 写 tmp → os.replace 仍会丢掉写入窗口内的 append；这里在
    rename 之前重新读一次并集，把窗口内新追加的记录一并写回。
    """
    target = _egress_log_path()
    if not os.path.exists(target):
        return 0
    try:
        def _read(path):
            recs = []
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if "ts" in d and "node" in d:
                            recs.append(d)
                    except Exception:
                        continue
            return recs

        keep_after = int(time.time()) - 7 * 86400
        before = _read(target)
        kept = [d for d in before if d.get("ts", 0) >= keep_after]
        if len(kept) == len(before):
            return 0
        tmp = target + ".tmp"
        # 写 tmp 后、rename 前，把期间追加进来的记录合并（避免丢切换记录）
        seen = {(d["ts"], d["node"]) for d in kept}
        for d in _read(target):
            key = (d["ts"], d["node"])
            if key not in seen and d.get("ts", 0) >= keep_after:
                kept.append(d)
                seen.add(key)
        kept.sort(key=lambda x: x["ts"])
        with open(tmp, "w", encoding="utf-8") as f:
            for d in kept:
                f.write(json.dumps(d) + "\n")
        os.replace(tmp, target)
        removed = len(before) - len(kept)
        if removed > 0:
            log(f"✓ egress_log 裁剪: 移除 {removed} 条 7 天前记录")
        return removed
    except Exception as e:
        log(f"✗ egress_log 裁剪失败: {e}")
        return 0


def _active_node_at(ts: int, switches: list[dict]) -> str | None:
    """返回 ts 时刻活跃的出口节点（最近一次 ts<=切换点 的 node）；无切换记录返回 None。"""
    active = None
    for s in switches:
        if s["ts"] <= ts:
            active = s["node"]
        else:
            break
    return active


# 上一次归因查询失败的原因（None = 成功或无数据）
_last_egress_error: str | None = None

def _set_egress_error(reason: str):
    global _last_egress_error
    _last_egress_error = reason

def _clear_egress_error():
    _set_egress_error("")

def get_last_egress_error() -> str | None:
    """返回上次 get_per_egress_error_rate 的失败原因（供 CLI 区分无数据/出错）"""
    return _last_egress_error

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
        # 提前返回的空结果都带上原因，供 CLI 区分"确实没数据"与"查询出错"
        # 无数据是正常状态（还没请求 / 还没切换过），不记为失败
        if not rows:
            _clear_egress_error()
            return {}
        switches = _load_egress_log()
        if not switches:
            _clear_egress_error()
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
        _clear_egress_error()
        return result
    except Exception as e:
        log(f"✗ per-egress 错误率失败: {e}")
        _set_egress_error(str(e))
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
        path = _egress_log_path()
        with open(path, "a", encoding="utf-8") as f:
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
        global _last_successful_switch_ts
        _last_successful_switch_ts = _now_ts()
        return True
    else:
        log(f"✗ 切代理验证失败 ({new_node}): PUT 返回 {r} 但实际 now={actual}")
        return False

# ============= WARP 主备隧道（opencode 专用出口） =============
def _warp() -> dict:
    return CFG["warp"]


def _warp_member(tag: str | None) -> dict | None:
    return next((m for m in _warp()["members"] if m["tag"] == tag), None)


def active_warp_tag() -> str | None:
    """opencode-out 当前指向的 WARP 成员 tag；指向轮换池 proxy 时返回 None。"""
    _, data = singbox_api(f'/proxies/{_warp()["selector"]}', silent=True)
    now = data.get("now") if data else None
    return now if _warp_member(now) else None


def opencode_out_on_warp() -> bool:
    return active_warp_tag() is not None


def _egress_label(current_proxy: str | None = None) -> str:
    """当前 opencode 出口的可读标签，用于健康行日志。

    形如 `WARP-CF-B(2a09:bac5:...)` 或 `proxy(pq.aws50...)`。
    出口种类一眼可辨 —— 排查时要先知道流量走的是哪条路，再去查该路的错误率。

    只读已知状态，不发探测：健康行每 10s 打一条，走 `warp_egress_ip` 的 SOCKS
    探测最长会阻塞主循环 12s。IP 取自换线时记下的 `_ACTIVE_WARP_IP`。
    """
    tag = active_warp_tag()
    if tag:
        return f"{tag}({_ACTIVE_WARP_IP or '?'})"
    return f"proxy({current_proxy or '?'})"


def count_warp_blocked_since(ts: float) -> dict[str, int]:
    """统计 ts 之后 opencode 最近 RECENT_WINDOW 条里的两类出口级拦截。

    - abuse403：IP 信誉拦截（"abusive traffic from this IP"）
    - rate429：IP 级限流（FreeUsageLimitError "Rate limit exceeded"）
    - ok：窗口内的成功数，供调用方判断当前出口是否还在服务流量

    WHY 429 也算：两者都按出口 IP 判定，换出口即拿到新配额/新信誉。
    实测 16:24 切 WARP 成员后成功率 3.5% → 9.3%（90s 窗口）。

    WHY 窗口是 10 而非 50：与代理侧 `get_9router_error_rate` 对齐 ——
    那里看的也是最近 RECENT_WINDOW 条。窗口拉长到 50 会让一条陈旧的 403
    在切换后仍算数，而切换点过滤（ts 之后的才计）本就是为了只看新增错误。
    """
    cutoff = (datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
              if ts > 0 else "")
    try:
        with _db_lock:
            rows = _get_db().execute("""
                SELECT status, data FROM requestDetails
                WHERE provider = ? AND timestamp > ?
                ORDER BY timestamp DESC LIMIT ?
            """, (OPENCODE_FREE_PROVIDER, cutoff, RECENT_WINDOW)).fetchall()
    except Exception as e:
        log(f"✗ 统计 WARP 拦截失败: {e}")
        return {"abuse403": 0, "rate429": 0, "ok": 0}
    counts = {"abuse403": 0, "rate429": 0, "ok": 0}
    for r in rows:
        if r["status"] == "success":
            counts["ok"] += 1
            continue
        # 直接子串匹配即可，无需解析嵌套 JSON：生产行里 error 是**字符串化**的
        # 内层 JSON，其转义引号间的关键词在原始文本中逐字可见。
        # 实测 opencode 全表 1958 行：朴素子串与「解析到 response.error 再匹配」
        # 判定完全一致（0 行差异）。
        body = r["data"] or ""
        if "abusive traffic" in body:
            counts["abuse403"] += 1
        elif "Rate limit exceeded" in body:
            counts["rate429"] += 1
    return counts


def count_opencode_since(ts: float) -> int:
    """ts 之后 opencode 的请求条数（封顶 200 条，够判主动轮换阈值即可）。

    WHY 不复用 count_warp_blocked_since 的 ok 数：那个窗口只有最近 10 条
    （约 10s），而主动轮换要判「这条出口已经被用了多少条」—— 实测同一 IP
    在首次 403/429 前的请求数中位数 28（p25=10，p75=73）。
    """
    if ts <= 0:
        return 0
    cutoff = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with _db_lock:
            rows = _get_db().execute("""
                SELECT 1 FROM requestDetails
                WHERE provider = ? AND timestamp > ? LIMIT 200
            """, (OPENCODE_FREE_PROVIDER, cutoff)).fetchall()
    except Exception as e:
        log(f"✗ 统计出口配额失败: {e}")
        return 0
    return len(rows)


def _switch_warp(tag: str) -> bool:
    """翻转 opencode-out selector。interrupt_exist_connections=false，在途请求不断。"""
    r, _ = singbox_api(f'/proxies/{_warp()["selector"]}', method="PUT", body={"name": tag})
    if r not in (200, 204):
        log(f"✗ 切换 WARP 线路失败 ({tag}): status={r}")
        return False
    return True


def _restart_service(service: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", service],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            log(f"✗ 重启 {service} 失败: {r.stderr.strip()[:200]}")
        return r.returncode == 0
    except Exception as e:
        log(f"✗ 重启 {service} 异常: {e}")
        return False


def _wait_port_ready(port: int, timeout_sec: float) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with socket.create_connection((_warp()["socks_host"], port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _warp_line_bytes(tag: str) -> int | None:
    """该 tag 上所有连接的累计字节；查询失败返回 None。"""
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "5", "-H",
             f"Authorization: Bearer {get_singbox_secret()}",
             f'{CFG["singbox"]["clash_api"]}/connections'],
            capture_output=True, text=True, timeout=8,
        )
        data = json.loads(r.stdout)
    except Exception:
        return None
    return sum((c.get("upload") or 0) + (c.get("download") or 0)
               for c in (data.get("connections") or [])
               if tag in (c.get("chains") or []))


def warp_line_busy(tag: str, window: float = 2.0) -> bool | None:
    """该线此刻是否仍在传输数据；查不到返回 None（调用方保守处理）。

    WHY 不用连接数：实测活跃线上恒有 7~10 条连接且从不归零（SSE 长连接
    + 9router 连接复用，实测单条存活 936s），按"连接数=0"等下去必然超时，
    被换下的线因此永远刷不到新鲜 IP。

    用两次快照的字节增量判活跃：只要还有一条连接在传数据，总量就会增长。
    连接被关闭时总量回落，同样说明该线已闲下来。
    """
    b0 = _warp_line_bytes(tag)
    if b0 is None:
        return None
    time.sleep(window)
    b1 = _warp_line_bytes(tag)
    if b1 is None:
        return None
    return b1 > b0


def _wait_warp_idle(member: dict) -> bool:
    """等到该线不再传输数据；超时返回 False（调用方自行决定是否强刷）。"""
    deadline = time.time() + _warp()["idle_wait_sec"]
    while time.time() < deadline:
        if warp_line_busy(member["tag"]) is False:
            return True
        time.sleep(2)
    return False


def warp_egress_ip(port: int) -> str | None:
    """经 usque SOCKS 查 Cloudflare trace；隧道不通或 warp=off 返回 None。

    Cloudflare 的 WARP 会话地址重连即换（同一 IP 视为重连未生效）。
    实测 B/C 两条隧道偶发 >8s 响应（触发超时），20s 内稳定在 1.2-1.4s。
    """
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "20", "--socks5-hostname",
             f'{_warp()["socks_host"]}:{port}',
             "https://www.cloudflare.com/cdn-cgi/trace"],
            capture_output=True, text=True, timeout=25,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    trace = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    if trace.get("warp") != "on":
        return None
    return trace.get("ip") or None


def _warp_line_sel(tag: str) -> float:
    """该线上次被选为出口的时刻；从未被选过返回 0（最老）。"""
    with _WARP_LINE_LOCK:
        return float(_WARP_LINE_STATE.get(tag, {}).get("sel", 0.0))


def _set_warp_line_ip(tag: str, ip: str) -> None:
    with _WARP_LINE_LOCK:
        _WARP_LINE_STATE.setdefault(tag, {})["ip"] = ip


def _warp_line_ip(tag: str) -> str | None:
    with _WARP_LINE_LOCK:
        return _WARP_LINE_STATE.get(tag, {}).get("ip")


def _warp_line_selected(tag: str, ip: str, ts: float) -> None:
    with _WARP_LINE_LOCK:
        _WARP_LINE_STATE[tag] = {"sel": ts, "ip": ip}


def _warp_ip_last_ts(ip: str | None) -> float:
    """该出口 IP 最后一次作为 egress 的 epoch 秒；从未用过返回 0（最老）。"""
    if not ip:
        return 0.0
    # 取值也在锁内：entry 是活字典，锁外读 ts 会与换线线程的写入竞争。
    with _WARP_IP_LOCK:
        entry = _WARP_IP_USED.get(ip)
        ts = (entry.get("ts") if isinstance(entry, dict) else entry) if entry else None
    return float(ts) if ts is not None else 0.0


def _warp_ip_recently_used(ip: str | None, now: float,
                           window: float | None = None) -> bool:
    """该出口 IP 是否在 window 内**有过成功流量**。

    判据是 `last_ok_ts`（最近一次成功响应的时刻），不是 `ts`（最近一次
    被选为出口的时刻）。区别在于：一条被选上去后立刻 403 的线，`ts` 很新
    但 `last_ok_ts` 是 0/很旧 —— 它其实从没服务成功过，不该因为"刚用过"
    而被排除；反过来，一条正在稳定服务的线即使被选中很久，也算"刚成功过"。
    没有 last_ok_ts 的老记录退回按 ts 判定（迁移期）。
    """
    if not ip:
        return False
    if window is None:
        window = _warp()["ip_reuse_guard_sec"]
    with _WARP_IP_LOCK:
        entry = _WARP_IP_USED.get(ip)
        ok = entry.get("last_ok_ts") if isinstance(entry, dict) else None
    # 本函数只回答「窗口内有没有成功流量」，没有成功记录答 False。这与
    # 「该不该用」不是一回事 —— 没成功过也没被标记的地址（实测 97/350 条）
    # 由 _warp_ip_rank 排到第 1 档，不当新鲜地址直接切，但仍可作兜底。
    return bool(ok) and now - float(ok) < window


def _warp_ip_is_blocked(ip: str | None) -> bool:
    """该出口 IP 是否已被 403/429 标记。"""
    if not ip:
        return False
    with _WARP_IP_LOCK:
        entry = _WARP_IP_USED.get(ip)
    return bool(isinstance(entry, dict) and entry.get("blocked"))


_last_success_probe: tuple[int, int] | None = None


_ACTIVE_IP_INIT_TS: float = 0.0


def _maybe_init_active_ip(now: float) -> None:
    """`_ACTIVE_WARP_IP` 为空时后台补探一次。

    启动时探测只做一次，那时 sing-box 可能还没就绪，`warp_egress_ip()` 返回
    None；此后该变量只在换线成功时赋值。若一直没触发换线（流量正常），它就
    永远是 None，成功流量打点全部因 `if not ip: return` 丢弃 —— 实测重启后
    1 分钟仍全是 `ip=None`。探测要 curl（最长 20s），不能放在主循环里同步做，
    所以丢给后台线程，30s 最多一次。
    """
    global _ACTIVE_IP_INIT_TS
    if _ACTIVE_WARP_IP or now - _ACTIVE_IP_INIT_TS < 30:
        return
    _ACTIVE_IP_INIT_TS = now

    def _probe() -> None:
        global _ACTIVE_WARP_IP
        m = _warp_member(active_warp_tag())
        if m:
            ip = warp_egress_ip(m["port"])
            if ip:
                _ACTIVE_WARP_IP = ip
                log(f"  · 当前出口 IP 补探成功: {ip}（启动时探测未就绪）")

    threading.Thread(target=_probe, daemon=True, name="active-ip-init").start()


def _record_active_ip_success(total: int, errors: int) -> None:
    """把「该出口 IP 有成功流量」记进 last_ok_ts。

    判据是窗口内**成功条数的增量**：total 与 errors 都只统计最近
    RECENT_WINDOW 条，成功数 = total - errors。增量 > 0 就说明这段时间里
    有请求打成功了 —— 这正是选线要的「3 小时内有过成功流量」。
    不能用「窗口里 success>0」：窗口滑动时同一条旧成功会被反复计入，
    一个早已死掉的地址会永远看起来「刚刚还成功过」。
    """
    global _last_success_probe
    ok_now = total - errors
    prev_ip, prev_ok = _last_success_probe or (None, None)
    ip = _ACTIVE_WARP_IP
    _last_success_probe = (ip, ok_now)
    if prev_ok is None or prev_ip != ip or not ip:
        return
    # 窗口里成功数没有增长 → 这一轮没有新的成功流量。窗口是跨 IP 的滑动窗口，
    # 单看「窗口里有过 success」会把几分钟前别的出口的成功反复算到当前 IP 上。
    if ok_now <= prev_ok:
        return
    now = time.time()
    with _WARP_IP_LOCK:
        if not isinstance(_WARP_IP_USED.get(ip), dict):
            # 重启后隧道换出的新地址此前没有记录（只有被选为出口时才写），
            # 不补一条的话成功流量永远打不上点，选线日志恒为「有成功记录 0 条」。
            _warp_ip_used_locked(ip, now, False)
        _WARP_IP_USED[ip]["last_ok_ts"] = now
        _save_warp_ip_used()


def _warp_ip_never_used(ip: str | None) -> bool:
    """该出口 IP 是否从未出现在使用记录里 —— 首选，必然没被 opencode 见过。"""
    if not ip:
        return False
    with _WARP_IP_LOCK:
        return ip not in _WARP_IP_USED


def _warp_ip_rank(ip: str | None, now: float, window: float) -> tuple[int, float]:
    """选线优先级：越小越优先。返回 (档位, 排序值)。

    三档（对应「按使用来区分」）：
      0. 从未出现过 —— 必然干净；
      1. 出现过但**没有任何成功流量** —— 切上去就报错、或从没真正服务过。
         这类不能当新鲜地址直接切（实测 350 条记录里 97 条属于此类），
         排在有成功记录的地址后面，但仍可作兜底；
      2. 有过成功流量 —— 按「距最近一次成功」排序，越久越优先（窗口内
         越久未成功 = 越接近恢复）。
    窗口外的地址由调用方先筛掉，不进本函数比较。
    """
    with _WARP_IP_LOCK:
        entry = _WARP_IP_USED.get(ip) if ip else None
    if entry is None:
        return (0, 0.0)
    ok = entry.get("last_ok_ts") if isinstance(entry, dict) else None
    if not ok:
        return (1, 0.0)
    # 取负：调用方用 min()，而「越久没成功越该优先」意味着间隔越大排序值要越小。
    return (2, -(now - float(ok)))


def _warp_guard_for_reason(reason: str) -> float:
    """按触发原因给出复用窗口（秒）。

    403 与 429 是两个独立的轴，恢复速度不同：
    - abuse403 = 出口信誉被标记。选取 3 小时内没用过的地址。
    - rate429 = 按 IP 的限流，间隔要求更长：选取 24 小时内没用过的地址。
      （实测同一出口 110s 内 ok/429 交替 8:4，单看时间会以为它很快恢复；
      但那是同一段会话内的波动，换线要挑的是长期没碰过的地址。）
    """
    if "abuse403" in reason:
        return _warp()["blocked_guard_sec"]
    if "rate429" in reason:
        return _warp()["rate429_guard_sec"]
    return _warp()["ip_reuse_guard_sec"]


def _warp_rest_time(ip: str | None, now: float) -> float:
    """该出口 IP 的休息时间（距上次使用的秒数）；从未用过视为从纪元起休息。"""
    return now - _warp_ip_last_ts(ip) if ip else now


def _warp_avg_rest_time(now: float) -> float:
    """已知出口 IP 的平均休息时间（秒）；无样本返回 0。

    ponytail: 只统计当前表内的 IP（窗口内），不是历史相邻使用间隔的均值 ——
    表里没有逐次使用历史。够用作兜底路径的可读性判据；要精确分布得给每个 IP
    存使用序列。
    """
    with _WARP_IP_LOCK:
        rests = [now - float((e.get("ts") if isinstance(e, dict) else e) or 0)
                 for e in _WARP_IP_USED.values()]
    return sum(rests) / len(rests) if rests else 0.0


def _warp_ip_survival(ip: str | None) -> float | None:
    """该出口 IP 上次被烧掉前存活了多久（秒）；无记录返回 None。

    供事后评估"哪个 /64 段、哪种复用间隔更容易被烧"，不参与选线。实测寿命
    与休息时长无关，选线只看新鲜度（`_warp_ip_recently_used`）与线路轮转。
    """
    if not ip:
        return None
    with _WARP_IP_LOCK:
        entry = _WARP_IP_USED.get(ip)
        return entry.get("served_sec") if isinstance(entry, dict) else None


def _mark_warp_ip_used(ip: str | None, now: float, blocked: bool = False) -> None:
    """记录出口 IP 的本次使用；blocked=True 表示该 IP 已被 403/429 标记。
    顺手清理已出窗口的条目（表长恒定为窗口内 IP 数）。

    换线线程与主循环都会写，必须持锁；否则并发 setdefault + 全量落盘会丢更新。
    """
    if not ip:
        return
    with _WARP_IP_LOCK:
        _warp_ip_used_locked(ip, now, blocked)


def _warp_ip_used_locked(ip: str, now: float, blocked: bool) -> None:
    """持锁版本的写入主体（调用方必须已持有 _WARP_IP_LOCK）。

    记录 `first_seen` / `blocked_ts` / `served_sec`：只存"最后一次使用时刻"
    无法回答"这个地址活了多久"。实测"休息越久越耐用"不成立（休息 <15min 复用
    存活中位 153s，>60min 反而 23s），所以这些字段只用于事后评估，不参与选线。
    """
    fresh = {"ts": now, "blocked": blocked, "first_seen": now,
             "blocked_ts": None, "served_sec": None, "last_ok_ts": None}
    entry = _WARP_IP_USED.setdefault(ip, fresh)
    if isinstance(entry, dict):
        entry["ts"] = now
        if blocked and not entry.get("blocked"):
            entry["blocked_ts"] = now
            entry["served_sec"] = now - float(entry.get("first_seen") or now)
        entry["blocked"] = entry.get("blocked", False) or blocked
    else:
        # 兼容旧格式（float）
        _WARP_IP_USED[ip] = fresh
    cutoff = now - _warp()["ip_reuse_guard_sec"]
    # 被标记过的 IP 保留更久：把整段烧掉的时间窗拉长到 blocked_ttl_sec，
    # 否则一个 10 分钟前的 blocked 记录会在下次换线时被清掉，又去试同一段。
    blocked_cutoff = now - _warp().get("blocked_ttl_sec", 3600)
    for k in list(_WARP_IP_USED):
        v = _WARP_IP_USED[k]
        ts = v.get("ts") if isinstance(v, dict) else v
        keep = blocked_cutoff if (isinstance(v, dict) and v.get("blocked")) else cutoff
        if ts < keep:
            del _WARP_IP_USED[k]
    _save_warp_ip_used()


def _refresh_idle_warp(member: dict) -> str | None:
    """重启隧道，直到拿到一个窗口内未使用过的出口 IP；失败返回 None。

    单次重启可能拿回同一地址（WARP 会话未真正重连），故按 max_attempts 重试。
    调用方要么据此跳过这条线，要么（闲置线）留给下次切回前的校验再刷新。
    """
    backoff = _warp()["retry_backoff_sec"]
    for attempt in range(1, _warp()["max_attempts"] + 1):
        try:
            ok = (_restart_service(member["service"])
                  and _wait_port_ready(member["port"], _warp()["ready_timeout_sec"]))
        except Exception as e:
            log(f"  ✗ {member['tag']} 刷新异常: {e}")
            return None
        if ok:
            ip = warp_egress_ip(member["port"])
            # 刷新后的判据是「这条地址没有近期成功流量」。但刚重启的隧道换出的
            # 新地址本来就没成功过，按这个判据必然通过 —— 正是想要的：新地址
            # 优先。若重连没真正生效、拿回的还是刚烧掉的那个，它若有近期成功
            # 记录就会被挡下（没有成功记录的地址见 _warp_ip_recently_used 注释）。
            if (ip and not _warp_ip_recently_used(ip, time.time())
                    and not _warp_ip_is_blocked(ip)):
                log(f"  ↻ {member['tag']} 会话已刷新: {ip}")
                _set_warp_line_ip(member["tag"], ip)
                return ip
            log(f"  ✗ {member['tag']} 刷新 #{attempt} 出口仍是近期用过的地址: {ip or '探测失败'}")
        else:
            log(f"  ✗ {member['tag']} 刷新后端口未就绪")
        time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
    return None


_last_dirty_reclaim_ts: float = 0.0


def _reclaim_dirty_warp_lines(now: float) -> None:
    """任一 WARP 备线的出口是被标记过的脏 IP，就后台重连到换出干净地址。

    WHY 不等换线时再刷：停在 proxy 时主循环不调预热，五条线的脏 IP 会一直留着，
    `_warp_any_reachable()` 恒 False → 回切永远不发生（实测 18:50~19:20 连续 30
    分钟全 403）。当前出口线交给换线路径处理 —— 重启它会掐断在途连接。
    """
    global _last_dirty_reclaim_ts
    if now - _last_dirty_reclaim_ts < _warp()["prewarm_sec"]:
        return
    _last_dirty_reclaim_ts = now
    active = active_warp_tag()
    for m in _warp()["members"]:
        if m["tag"] == active:
            continue
        with _WARP_REFRESH_LOCK:
            if m["tag"] in _WARP_REFRESHING:
                continue
            _WARP_REFRESHING.add(m["tag"])
        threading.Thread(target=_reclaim_dirty_warp_worker, args=(m,),
                         daemon=True, name=f"warp-reclaim-{m['tag']}").start()


def _reclaim_dirty_warp_worker(member: dict) -> None:
    """探测该线出口；脏 IP 就重启隧道，直到换出未被标记的地址或超时。"""
    try:
        deadline = time.time() + _warp()["ip_refresh_retry_sec"]
        while time.time() < deadline:
            ip = warp_egress_ip(member["port"])
            if not ip:
                time.sleep(3)
                ip = warp_egress_ip(member["port"])
            if ip and not _warp_ip_is_blocked(ip):
                _set_warp_line_ip(member["tag"], ip)
                return
            log(f"  ↻ {member['tag']} 出口 {ip or '探测失败'} 疑似脏 IP，重连换线")
            if _refresh_idle_warp(member):
                return
            time.sleep(_warp()["refresh_delay_sec"])
        log(f"  ✗ {member['tag']} {_warp()['ip_refresh_retry_sec']}s 内未换出干净出口")
    finally:
        with _WARP_REFRESH_LOCK:
            _WARP_REFRESHING.discard(member["tag"])


# 补刷结果计数。补刷是后台线程，失败只在换线撞上「无可用备线」时才暴露，
# 而那时已经在付 2~3 分钟的代价 —— 得让成功率在日志里可读。
_WARP_REFRESH_STATS = {"ok": 0, "retry": 0, "fail": 0}


def _bump_refresh_stat(key: str) -> None:
    """累计补刷结果，每 10 次打一行汇总。多个补刷线程并发写，需持锁。"""
    with _WARP_REFRESH_LOCK:
        _WARP_REFRESH_STATS[key] += 1
        total = sum(_WARP_REFRESH_STATS.values())
        snapshot = dict(_WARP_REFRESH_STATS)
    if total % 10 == 0:
        log(f"↻ WARP 补刷统计: {snapshot}")


def _retry_refresh_idle_warp(member: dict) -> bool:
    """把"推迟刷新"的线补刷回来，直到拿到新鲜出口 IP。返回是否刷出。

    WHY 必须补：单次超时就放弃会让这条线永远停在旧地址上 —— 实测
    WARP-CF-C 被换下 8 次，7 次因 40s 内仍有连接而放弃，结果 13 次换线里
    7 次切回同一个 10 分钟前的地址，切回 53s 后即遭 403 爆发。
    等不到空闲就继续等，直到这条线真的被刷新或超出重试上限。
    """
    deadline = time.time() + _warp()["ip_refresh_retry_sec"]
    while time.time() < deadline:
        # 已经拿到窗口外的新鲜 IP 就不要再重启：`_refresh_idle_warp` 每成功
        # 一次就换掉一个会话，重复调用等于把刚预热好的地址白白烧掉。
        known = _warp_line_ip(member["tag"])
        if known and not _warp_ip_recently_used(known, time.time()):
            _bump_refresh_stat("ok")
            return True
        idle = _wait_warp_idle(member)
        # 判在重启之前而不是本轮开头：换线线程的快照可能早于本次调度，等空闲的这
        # 40s 内这条线也可能被切成当前出口。此刻重启它就是掐断正在服务的在途连接
        # （upstream connection lost）；它下次被换下时 reconnect_warp 会重新调度
        # 补刷。非空闲分支不查当前出口 —— 那一路本就不会重启。
        if idle and active_warp_tag() == member["tag"]:
            log(f"  ↻ {member['tag']} 已被选为当前出口，取消本次刷新")
            return False
        if idle and _refresh_idle_warp(member):
            _bump_refresh_stat("ok")
            return True
        _bump_refresh_stat("retry")
        log(f"  ⚠ {member['tag']} 仍在传数据，{_warp()['idle_wait_sec']}s 后重试刷新")
    _bump_refresh_stat("fail")
    log(f"  ✗ {member['tag']} 超过 {_warp()['ip_refresh_retry_sec']}s 仍未刷出新鲜出口")
    return False

def _schedule_idle_refresh(member: dict) -> None:
    """换线后延迟刷新闲置线，使其在下次需要时已是新鲜 IP。

    WHY 延迟而非立即：interrupt_exist_connections=false 让在途请求仍走刚被换下的
    线，立即重启会把它们全掐断（9router 报 upstream connection lost，客户端
    表现为 API Error）。延迟到连接自然结束后再刷。

    WHY 需要预热：新鲜度守卫（reconnect_warp 里的 _refresh_idle_warp）是同步的，
    探测+重启要 7~9s，期间流量仍停在已被标记的旧出口上 —— 实测 17:36:55 那次
    换线因此多花了 9s。预先刷好，守卫就不必在换线路径上等。
    """
    with _WARP_REFRESH_LOCK:
        if member["tag"] in _WARP_REFRESHING:
            return
        _WARP_REFRESHING.add(member["tag"])

    def _run():
        try:
            time.sleep(_warp()["refresh_delay_sec"])
            # 等到连接自然结束再重启：usque 重启会掐断所有经它的连接，
            # 与 sing-box 的 interrupt_exist_connections 无关。
            # 只调补刷：它在成功时已经刷新过，再无条件刷一次等于二次重启
            # （又换一个 IP，还可能把刚刷出的好 IP 换掉），失败时强刷则
            # 违背「等空闲再刷」的初衷 —— 400s 等待白费且掐断在途连接。
            if not _retry_refresh_idle_warp(member):
                log(f"  ✗ {member['tag']} 补刷超时，本次放弃")
        except Exception as e:
            log(f"  ✗ {member['tag']} 延迟刷新异常: {e}")
        finally:
            with _WARP_REFRESH_LOCK:
                _WARP_REFRESHING.discard(member["tag"])

    threading.Thread(target=_run, daemon=True,
                     name=f"warp-refresh-{member['tag']}").start()


def _pool_nodes() -> list[str] | None:
    """轮换池节点列表（排除 WARP 成员与 direct）；查询失败返回 None。"""
    try:
        _, data = singbox_api(f'/proxies/{CFG["singbox"]["selector"]}', silent=True)
    except Exception:
        return None
    if not data:
        return None
    return [x for x in (data.get("all") or [])
            if x != "direct" and not x.startswith("WARP-")]


def _proxy_pool_healthy(sample: int = 3, nodes: list[str] | None = None) -> bool | None:
    """轮换池是否还能服务 opencode 流量；样本全失败才算不可用，查不到返回 None。

    全池 83 个逐个探测代价过高（每个 15s 超时），只取样本；但样本必须随机、
    并发：固定取前 3 个时，只要这 3 个恰好挂了就把整个池判死，daemon 立刻
    回切 WARP —— 代理池资源一点没用上。随机 3 个同时挂的概率极低。
    串行 3 个最坏 45s 卡在主循环上，并发后只受最慢那个约束。

    nodes 可由调用方预先取好复用（回切判据要同时算黑名单占比）。
    """
    if nodes is None:
        nodes = _pool_nodes()
        if nodes is None:
            return None
    if not nodes:
        return None
    picked = _random.sample(nodes, min(sample, len(nodes)))
    timeout_ms = CFG["thresholds"]["node_test_timeout_ms"]

    def _probe(node: str):
        try:
            return _probe_node(node, timeout_ms)
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(picked), thread_name_prefix="pool-probe") as ex:
        futs = [ex.submit(_probe, n) for n in picked]
        for f in futs:
            try:
                if f.result() is not None:
                    return True
            except Exception as e:
                log(f"  ✗ 池探测异常: {e}")
                continue
    # 采样全失败 = 池不可用。缺这一行时函数隐式返回 None，而调用方判
    # `is False`（None = 查询失败，不得据此判定不可用）→ 池全死却永不切回
    # WARP，正是 INC-002 的失败模式：故障态伪装成未知态。
    return False
def _probe_warp_egress(members: list[dict]) -> dict[str, str | None]:
    """并发探测多条线的出口 IP（tag -> IP）。

    串行时每条上限 12s（`--max-time 8` + 超时 12），5 条最坏 60s，而换线与
    `_warp_any_reachable` 都在主循环/换线路径上同步等它。实测并发探 5 条共
    6.7s（单条 1.2~7s）：耗时由最慢那条决定，与隧道数无关。
    """
    if not members:
        return {}
    out: dict[str, str | None] = {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(members), thread_name_prefix="warp-probe") as ex:
        futs = {ex.submit(warp_egress_ip, m["port"]): m["tag"] for m in members}
        for f in concurrent.futures.as_completed(futs):
            try:
                ip = f.result()
                out[futs[f]] = ip
                if ip:
                    # 记下 tag→IP：预热/脏线巡检靠它判断「这条线现在出去是什么地址」。
                    _set_warp_line_ip(futs[f], ip)
            except Exception as e:
                log(f"  ✗ 探测 {futs[f]} 出口异常: {e}")
                out[futs[f]] = None
    return out


def _warp_any_reachable() -> bool:
    """所有 WARP 隧道里是否至少有一条能出**未被标记**的出口 IP。

    WHY 不能只看连通：换到一个刚被 403 标记的 IP 等于没换。实测 22:36:28
    切到 2a09:bac1:19c0:18::1d2:c4 后 403 仍 10/10，再换两个 IP 才恢复 ——
    同一 /32 段被整体标记时，连通探测全部通过。
    """
    ips = _probe_warp_egress(_warp()["members"])
    with _WARP_IP_LOCK:
        blocked = {ip for ip, e in _WARP_IP_USED.items() if (e or {}).get("blocked")}
    return any(ip and ip not in blocked for ip in ips.values())



def _maybe_failback_to_warp(now: float) -> bool:
    """停在轮换池上时判断是否该回切 WARP。返回是否发起了切换。

    WHY 不按「WARP 复活」回切：WARP 隧道只要探测通过就算复活，但探测本身会
    抖动，实测 00:02:45 落到 proxy、00:02:52 就回切（7s），83 节点池一条
    opencode 请求都没服务过。代理池是兜底资源，能服务流量就该让它服务完。

    回切依据（任一成立）：
    1. 池健康探测连续 `proxy_fail_streak` 次全失败 —— 池真的挂了。
    2. 落池后 opencode 仍被 403/429 拦截 ≥ `proxy_blocked_threshold` 条 ——
       换到池上依然限流，说明池节点 IP 也被烧了。
    3. 黑名单节点占比 ≥ `proxy_dead_ratio` —— 池子可用容量耗光。
    同时要求至少一条 WARP 隧道能出未被标记的 IP，否则切回去也是白切。
    """
    global _last_proxy_pool_check, _proxy_fail_streak
    if opencode_out_on_warp():
        return False
    if _last_proxy_pool_check + CFG["intervals"]["proxy_check_sec"] > now:
        return False
    _last_proxy_pool_check = now

    nodes = _pool_nodes() or []
    # 1. 池健康：连续失败计数
    healthy = _proxy_pool_healthy(nodes=nodes)
    # None = 查不到（Clash API 抖动），既不能算失败也不能算成功：清零会把
    # 已积累的连续失败证据抹掉，下次还得重新攒够阈值。
    if healthy is True:
        _proxy_fail_streak = 0
    elif healthy is False:
        _proxy_fail_streak += 1
    pool_dead = _proxy_fail_streak >= CFG["intervals"]["proxy_fail_streak"]

    # 2. 落池后仍在被出口级拦截
    since = _proxy_dwell_since or 0
    blocked = count_warp_blocked_since(since) if since else {"abuse403": 0, "rate429": 0}
    still_limited = (blocked["abuse403"] + blocked["rate429"]
                     >= CFG["intervals"]["proxy_blocked_threshold"])

    # 3. 黑名单占比
    dead_ratio = len(_DEAD_NODES & set(nodes)) / len(nodes) if nodes else 0.0
    pool_saturated = dead_ratio >= CFG["intervals"]["proxy_dead_ratio"]

    if not (pool_dead or still_limited or pool_saturated):
        return False
    if not _warp_any_reachable():
        return False
    why = []
    if pool_dead:
        why.append(f"池探测连续失败 {_proxy_fail_streak} 次")
    if still_limited:
        why.append(f"落池后仍拦截 {blocked['abuse403'] + blocked['rate429']} 条")
    if pool_saturated:
        why.append(f"黑名单占比 {dead_ratio:.0%}")
    log(f"↻ 轮换池已不堪用（{' + '.join(why)}），切回 WARP")
    # 只有真的发起了切换才清空证据：冷却中或已有换线在跑时 _reconnect_warp_now
    # 返回 False，此时清计数等于把已积累的判据丢掉，下次还要重新攒。
    started = _reconnect_warp_now(" + ".join(why), now)
    if started:
        _proxy_fail_streak = 0
    return started


def _fallback_to_proxy(active: dict | None, old_ip: str | None, now: float) -> bool:
    """所有隧道都不可用时切到轮换池 proxy —— 不重启隧道，所以不掐断在途连接。

    WHY 优于原地重连：原地重连要 `systemctl restart usque-*`，有几秒静默；
    若 D-Bus 已不可用（user session 正在退出）则必然失败，流量会干等在
    一个已失效的出口上。实测 2026-10-19 19:09 正是如此：三条线同时探测
    失败 → 原地重连 → `Failed to connect to bus: Connection refused` ×3
    → 流量停在坏出口，403-abuse 每分钟 10~116 条持续 7 分钟。
    切 proxy 只是一次 PUT，秒级且不断流。

    隧道恢复后由下次换线自动切回：reconnect_warp 在 active 为 None（即
    停在 proxy 上）时会把全部成员作为候选。
    """
    if not _switch_warp("proxy"):
        log("  ✗ 切轮换池失败，退回原地重连")
        if active is None:
            return False
        return _restart_warp_inplace(active, old_ip)
    # 用切换生效时刻，不用调用方传入的 now：reconnect_warp 的 now 取自探测/重启
    # 之前，可能已过几分钟，拿它当落池起点会把落池前的旧 403/429 也算进去。
    landed = time.time()
    if active is not None:
        _mark_warp_ip_used(old_ip, landed)
        _schedule_idle_refresh(active)
    global _proxy_dwell_since
    _proxy_dwell_since = landed     # 落池时刻：回切判据只统计此后的拦截
    log("✓ 已切到轮换池 proxy（WARP 隧道全部不可用）")
    return True


def _restart_warp_inplace(member: dict, old_ip: str | None) -> bool:
    """单线兜底：原地重启当前隧道换会话地址（有几秒断流）。"""
    backoff = _warp()["retry_backoff_sec"]
    for attempt in range(1, _warp()["max_attempts"] + 1):
        log(f"  ↻ {member['tag']} 原地重连 #{attempt}")
        ok = (_restart_service(member["service"])
              and _wait_port_ready(member["port"], _warp()["ready_timeout_sec"]))
        if ok:
            new_ip = warp_egress_ip(member["port"])
            now = time.time()
            if (new_ip and new_ip != old_ip
                    and not _warp_ip_recently_used(new_ip, now)):
                _mark_warp_ip_used(new_ip, now)
                log(f"✓ WARP 出口已更换: {old_ip or '未知'} -> {new_ip}")
                return True
            log(f"  ✗ {member['tag']} 出口不新鲜: {new_ip or '探测失败'}")
        else:
            log(f"  ✗ {member['tag']} {_warp()['ready_timeout_sec']}s 内未就绪")
        time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
    return False


def reconnect_warp(reason: str) -> bool:
    """把 opencode 流量换到另一条出口新鲜（窗口外）的隧道上。

    备线在窗口内用过就先重启它换会话 IP（此时它闲置，重启安全），再切过去
    （PUT selector 秒级、在途请求不断）。备线都不可用才原地重连当前线
    （有几秒断流）。

    不刷新刚换下的那条：在途请求仍走它，重启会掐断连接。
    """
    members = _warp()["members"]
    now = time.time()
    # 已停在轮换池 proxy 上时 active 为 None：挑一条新鲜隧道切回去即可。
    active = _warp_member(active_warp_tag())
    old_ip = warp_egress_ip(active["port"]) if active else None
    # 不做「地址池耗尽」短路：旧判据看 USED 全 blocked，而触发换线时刚把当前出口
    # 标 blocked，会让它短暂"全 blocked" —— 实测 05:14~06:52 五次假耗尽，把流量
    # 甩到同样被限流的池上再弹回。备线探测本来就会丢弃 blocked 出口，最坏多花
    # 一轮并发探测（~7s）。
    if active is None:
        log(f"↻ WARP 换线 ({reason})：当前在轮换池 proxy，尝试切回 WARP 隧道")
    else:
        _mark_warp_ip_used(old_ip, now)  # 当前线用到此刻，换下后窗口内不再可选
        log(f"↻ WARP 换线 ({reason})：当前 {active['tag']} 出口={old_ip or '未知'}")

    standby = members if active is None else [x for x in members if x["tag"] != active["tag"]]
    # 补刷中的线先排除再探测：补刷线程会在它闲下来后重启它换 IP，此刻切过去
    # 等于把在途请求放在一台即将被重启的隧道上（掐断 → upstream connection
    # lost）。补刷窗口从 60s 拉长到 600s 后撞上的概率显著上升。
    with _WARP_REFRESH_LOCK:
        refreshing = set(_WARP_REFRESHING)
    standby = [m for m in standby if m["tag"] not in refreshing]
    probed = _probe_warp_egress(standby)
    probes: list[tuple[dict, str]] = []   # (member, ip)，探测到出口的备线
    blocked_lines: list[dict] = []        # 出口已被标记、待刷新换 IP 的备线
    for m in standby:
        ip = probed.get(m["tag"])
        if not ip:
            log(f"  ↻ 备线 {m['tag']} 不通，先拉起")
            if not (_restart_service(m["service"])
                    and _wait_port_ready(m["port"], _warp()["ready_timeout_sec"])):
                log(f"  ✗ 备线 {m['tag']} 未就绪，跳过")
                continue
            ip = warp_egress_ip(m["port"])
        if not ip:
            log(f"  ✗ 备线 {m['tag']} 探测失败，跳过")
            continue
        if _warp_ip_is_blocked(ip):
            # 被标记 ≠ 永久不可用：每条隧道同一时刻只有一个出口 IP，重启才会换
            # 会话。全跳过会让候选池恒为 0（403 风暴几秒就能烧完全部备线），
            # 流量被甩到同样被限流的 proxy 池 —— 19:16 的兜底死循环即由此而来。
            log(f"  ↻ 备线 {m['tag']} 出口 {ip} 已被标记，排队刷新")
            blocked_lines.append(m)
            continue
        probes.append((m, ip))

    # 备线出口全被标记时刷新一条拿新会话 IP，刷新不出才落 proxy 池 —— 池只是
    # 最后兜底，不该是「地址全被标记」的直接结果。备线此刻不在服务流量，重启
    # 不会掐断在途请求。
    if not probes:
        for m in blocked_lines:
            ip = _refresh_idle_warp(m)
            if ip:
                probes.append((m, ip))
                break
            log(f"  ✗ {m['tag']} 刷新后仍无可用出口")

    # 选线三级优先：
    #   1) 从未用过的地址 —— 必然没被 opencode 见过，优先级最高；
    #   2) 窗口外没用过的（窗口按触发原因取：403→3h，429→24h）；
    #   3) 都不满足时兜底复用最久未用的（见下面 elif probes 分支）。
    # 第 1 级单列是因为「从没用过」和「用过但已久」在 _warp_ip_last_ts 上都
    # 可能返回很小的值，混在一起排序会让刚清理掉的旧地址冒充新地址。
    guard = _warp_guard_for_reason(reason)
    # 已被 403/429 标记的地址在探测阶段就被剔除了，这里只剩可尝试的备线。
    usable = [p for p in probes if not _warp_ip_recently_used(p[1], now, guard)]
    n_new = sum(1 for p in probes if _warp_ip_never_used(p[1]))
    n_ok = sum(1 for p in probes if _warp_ip_rank(p[1], now, guard)[0] == 2)
    log(f"  ↻ 选线: 从没用过 {n_new} 条, 有成功记录 {n_ok} 条, "
        f"{guard/3600:.0f}h 内无成功流量合计 {len(usable)} 条 / 备线 {len(probes)} 条")
    if usable:
        m, ip = min(usable, key=lambda p: _warp_ip_rank(p[1], now, guard))
    elif probes:
        # 极端兜底：所有备线 IP 都在复用窗口内。按「最久没用过」选一条再刷新。
        # WHY 不用"平均休息时间"排序：实测休息时长与存活无关（<15min 复用的
        # 存活中位 153s，>60min 反而 23s，n=6~26），拿它当判据只会更频繁地
        # 复用同几个地址；这里只用它把最久未用的排前面，且选完仍要刷新。
        m, ip = max(probes, key=lambda p: now - _warp_ip_last_ts(p[1]))
        log(f"  ⚠ 兜底选线: {m['tag']} 上次使用 {(now - _warp_ip_last_ts(ip))/60:.1f}min 前，"
            f"IP={ip} 仍在复用窗口内，将先刷新会话")
        guard = _warp()["ip_reuse_guard_sec"]
        log(f"  ↻ {m['tag']} 出口 {ip} 在 {guard}s 内用过，先刷新会话 IP")
        # 刷新要重启隧道，会掐断该线上所有在途连接 → 9router 报 upstream
        # connection lost。备线上的连接是健康的，不该为换线牺牲；被标记的
        # 当前线才该牺牲。所以：有连接就先等它自然结束，等不到改刷另一条
        # 闲置备线，都没有则原地重连当前线。实测 18:56:52 在 WARP-CF 还有
        # 7 条连接时强刷，换线耗时 48s 且掐断连接。
        # 判据是"还在传数据"而非"有连接"：实测活跃线恒有 7~10 条连接且
        # 从不归零（见 warp_line_busy）。
        if warp_line_busy(m["tag"]) and not _wait_warp_idle(m):
            idle = next(((x, v) for x, v in probes[1:]
                         if warp_line_busy(x["tag"]) is False), None)
            if not idle:
                log("  ⚠ 备线均在服务在途连接，强刷会掐断，切轮换池兜底")
                return _fallback_to_proxy(active, old_ip, now)
            m, ip = idle
        ip = _refresh_idle_warp(m)
        if not ip:
            log(f"  ✗ 备线 {m['tag']} 刷新后仍无新鲜出口，切轮换池兜底")
            return _fallback_to_proxy(active, old_ip, now)
    else:
        log("  ⚠ 无可用备线，切轮换池兜底")
        return _fallback_to_proxy(active, old_ip, now)

    if not _switch_warp(m["tag"]):
        return _fallback_to_proxy(active, old_ip, now)
    # 用切换生效时刻：now 取自探测/重启之前，可能已过数分钟。拿它当 IP 使用
    # 起点会让 24h 复用窗口提前结束，地址被提前放回候选池。
    _mark_warp_ip_used(ip, time.time())
    switched_at = time.time()
    _warp_line_selected(m["tag"], ip, switched_at)  # 轮转公平性
    global _ACTIVE_WARP_IP, _proxy_dwell_since, _last_warp_switch_ok_ts
    _last_warp_switch_ok_ts = switched_at
    _ACTIVE_WARP_IP = ip
    _proxy_dwell_since = 0          # 回到 WARP，驻留计时清零
    _from = active["tag"] if active else "proxy"
    log(f"✓ WARP 换线成功: {_from} -> {m['tag']} (出口 {ip})")
    # 延迟刷新刚换下的线：立即重启会掐断仍在走它的在途请求，延迟到连接
    # 自然结束；预先刷好则下次切回时无需在换线路径上同步等 7~9s。
    if active is not None:
        _schedule_idle_refresh(active)
    # 任一备线出口疑似脏就不能留着：重连到换出优质 IP 为止（补刷内部有重试上限）。
    # 同时最多两条在刷，避免多条隧道同时重启。
    for bm in blocked_lines:
        if bm["tag"] == m["tag"]:
            continue
        with _WARP_REFRESH_LOCK:
            if len(_WARP_REFRESHING) >= 2:
                break
        _schedule_idle_refresh(bm)
    return True


def _warp_reconnect_worker(reason: str, cooldown_on_ok: float):
    """cooldown_on_ok 由调度处算好（含退避），这里不重算，否则会覆盖掉放大的冷却。"""
    global _warp_cooldown_until
    ok = False
    try:
        ok = reconnect_warp(reason)
    except Exception as e:
        log(f"✗ WARP 重连异常: {e}")
    finally:
        cooldown = cooldown_on_ok if ok else _warp()["cooldown_fail_sec"]
        _warp_cooldown_until = time.time() + cooldown
        _WARP_LOCK.release()



def maybe_reconnect_warp(now: float) -> None:
    """累计到阈值就异步换线，避免阻塞主循环（探测最长 ~40s）。

    两类出口级拦截共用同一条换线路径，阈值各不相同：
    - abuse403 阈值低（默认 1）：出现即说明当前出口已被标记，多等只是继续失败。
    - rate429 阈值高（默认 5）：429 比 403 频繁得多（实测 2~5 req/s），
      单条就换线会让两条隧道在 20/40/80s 节奏上反复重启。

    冷却固定 cooldown_ok_sec，不随连续失败放大：换到窗口外的新鲜 IP 是每次换线
    的前提（见 reconnect_warp 的新鲜度守卫），只要换到的是新 IP 就值得换。
    曾用 2^n 退避"避免隧道反复重启"，实测在 429 风暴期退避会一路涨到 600s 封顶
    且永不回落，17:02~17:09 九分钟零换线、1752 条请求全废 —— 抑制换线比换线本身
    代价更大。
    """
    global _last_warp_reconnect_ts, _warp_cooldown_until
    blocked = count_warp_blocked_since(_last_warp_reconnect_ts)
    triggered = [k for k, thr in (("abuse403", _warp()["block_threshold"]),
                                  ("rate429", _warp()["rate429_threshold"]))
                 if blocked[k] >= thr]
    if not triggered:
        return
    # 采样与标记必须在冷却判断之前：403 多在换线后 1 秒内出现（实测首错延迟
    # 中位 1s、16/45 段首请求即错），落进冷却期就被丢弃的话，这些段既不会
    # 被记成周期样本，出口 IP 也不会被标脏 —— 换线时它会作为「干净地址」被
    # 再次选中。本段是否已采过，直接查样本表里有没有 ended_at >= 本段起点，
    # 不必另设「已采样」标志（那个标志跨段漏重置会让整段采不到样本）。
    active = _warp_member(active_warp_tag())
    active_ip = warp_egress_ip(active["port"]) if active else None
    # 起点为 0 表示还没成功换过线，没有「段」可言，不能拿历史样本冒充
    # （ended_at >= 0 恒真，会把首个错误段整个跳过）。
    with _WARP_CYCLES_LOCK:
        sampled = _last_warp_switch_ok_ts > 0 and any(
            float(c.get("ended_at", 0)) >= _last_warp_switch_ok_ts
            for c in _WARP_IP_CYCLES)
    if not sampled:
        if active:
            _mark_warp_ip_used(active_ip, now, blocked=True)
        # 只有成功服务过 ≥1 条的段才算「可用周期」；首请求即错的段记 ok=0。
        if active_ip and _last_warp_switch_ok_ts > 0:
            cycle = now - _last_warp_switch_ok_ts
            if cycle > 0:
                # 用本段起点（换线生效时刻）统计成功数，不能复用上面的
                # blocked —— 它的窗口起点是换线**发起**时刻，跨段会算进上一段。
                seg_ok = count_warp_blocked_since(_last_warp_switch_ok_ts).get("ok", 0)
                _record_warp_cycle(active_ip, cycle, now, ok=seg_ok)
    if now < _warp_cooldown_until:
        return
    if not _WARP_LOCK.acquire(blocking=False):
        return
    delay = _warp()["cooldown_ok_sec"]
    _last_warp_reconnect_ts = _now_ts()
    _warp_cooldown_until = now + delay  # 先占位，防 tick 重入
    reason = " + ".join(f"{k} ×{blocked[k]}" for k in triggered)
    threading.Thread(target=_warp_reconnect_worker,
                     args=(reason, delay),
                     daemon=True, name="warp-reconnect").start()


def _reconnect_warp_now(reason: str, now: float, ignore_cooldown: bool = False) -> bool:
    """立刻发起一次换线，绕开 403/429 阈值。返回是否真的发起了。

    WHY 需要绕开阈值：`maybe_reconnect_warp()` 的触发依赖
    `count_warp_blocked_since()` 统计到足够的下游错误。但「隧道复活」这件事
    本身不产生任何 403/429 —— 只看下游错误，daemon 会永远停在 proxy 上。

    ignore_cooldown 仅供主动轮换：计算出的最小周期可能短于 cooldown_ok_sec(20s)，
    受冷却约束就永远等不到触发。`_WARP_LOCK` 无论如何都要抢到 —— 那是并发换线
    的互斥，不是节流。错误触发路径保持受冷却约束。
    """
    global _last_warp_reconnect_ts, _warp_cooldown_until
    if not ignore_cooldown and now < _warp_cooldown_until:
        return False
    if not _WARP_LOCK.acquire(blocking=False):
        return False
    delay = _warp()["cooldown_ok_sec"]
    _last_warp_reconnect_ts = _now_ts()
    _warp_cooldown_until = now + delay  # 先占位，防 tick 重入
    threading.Thread(target=_warp_reconnect_worker,
                     args=(reason, delay),
                     daemon=True, name="warp-reconnect").start()
    return True


def _maybe_proactively_rotate_warp(now: float) -> None:
    """当前出口还在成功服务时就换线，不必等 403 冲断在途对话。

    有周期样本时按「24h 内成功服务过的段的典型周期（中位）」换线；无样本时回退到
    「已服务请求数」。时间判据曾因**自证循环**被弃用：采样写在冷却判断之后，
    换线后 1 秒内的 403 全被 20s 冷却丢弃，样本最小值恒等于冷却值。f68ab66
    起采样先于冷却判断，样本反映真实周期，判据恢复可用。

    周期下限 5s 短于 cooldown_ok_sec(20s)，受冷却就永远等不到触发，故成功换线后
    的 cooldown 绕过（并发仍由 `_WARP_LOCK` 保证）；换线失败后的冷却不绕，
    那是防隧道雷暴的。请求数回退路径保持受冷却约束。

    """
    global _last_proactive_warp_check, _last_cycle_recalc_ts
    # 计数要查 SQLite，5s 一次足够：配额以秒级速度消耗，多查无益。
    if now - _last_proactive_warp_check < 5:
        return
    _last_proactive_warp_check = now
    # 周期样本重算（按时间判断，不查库）
    if now - _last_cycle_recalc_ts >= _warp()["min_cycle_recalc_sec"]:
        _last_cycle_recalc_ts = now
        _recompute_min_warp_cycle(now)
    # 用「换线生效时刻」而非「发起时刻」：发起早于生效数秒，期间旧出口仍在
    # 服务流量，用发起时刻会把旧线的消耗算进新线配额。
    # 两者都为 0 表示本次启动后还没换过线。此时**不要**直接返回后从零计数：
    # service 重启后 `_last_warp_switch_ok_ts` 归零，若拿 0 当窗口起点，
    # `count_opencode_since` 会把重启前已经服务过的那一段整段算进来 ——
    # 实测 16:31:08 重启、16:32:18（70 秒后）就报「已服务 63 条」并换线，
    # 而那条线实际只服务了几条。改用该出口 IP 在 `.warp_ip_used.json` 里的
    # 使用时刻恢复段起点，跨重启仍然正确。
    start = _last_warp_switch_ok_ts or _last_warp_reconnect_ts
    if start <= 0:
        # 内存变量重启即归零，但 sing-box 的 selector 不变 —— 现探当前出口，
        # 再用它在 IP 表里的使用时刻恢复段起点（两者都可能探测失败，失败就
        # 放弃本轮，下一 tick 再试，不会误判）。
        member = _warp_member(active_warp_tag())
        ip = warp_egress_ip(member["port"]) if member else None
        with _WARP_IP_LOCK:
            entry = _WARP_IP_USED.get(ip) if ip else None
        start = float(entry.get("ts") or 0) if isinstance(entry, dict) else 0.0
        if start <= 0:
            return
    if _WARP_MIN_CYCLE_SEC > 0:
        elapsed = now - start
        if elapsed >= _WARP_MIN_CYCLE_SEC:
            # 只绕 cooldown_ok：上次换线失败时 `_warp_cooldown_until` 是
            # cooldown_fail_sec，那是防隧道雷暴的，不能绕。
            _reconnect_warp_now(
                f"主动轮换 周期 {elapsed:.0f}s ≥ {_WARP_MIN_CYCLE_SEC:.0f}s",
                now, ignore_cooldown=_last_warp_reconnect_ts <= _last_warp_switch_ok_ts)
        return
    if now < _warp_cooldown_until:
        return
    n = count_opencode_since(start)
    if n >= _warp()["proactive_after_requests"]:
        _reconnect_warp_now(f"主动轮换 已服务 {n} 条", now)


def _prewarm_standby_warp(now: float) -> None:
    """后台巡检各条备线：出口疑似脏就重连，直到换出优质 IP。

    WHY 需要：ip_reuse_guard_sec=24h，用过的线 IP 全在窗口内；不预热的话换线时
    只有「刚换下、已被补刷」的那一条是新鲜的 —— 实测流量长期只在 WARP-CF 与
    WARP-CF-B 之间往返。另一类脏线是 403/429 标记过的地址：它可能从没有过成功
    流量，只看「窗口内有无成功」会把它当新鲜线跳过，脏 IP 永远刷不掉。

    每轮只启动一条，由并发上限保证同时最多两条在刷。
    """
    global _last_warp_prewarm_ts
    if now - _last_warp_prewarm_ts < _warp()["prewarm_sec"]:
        return
    _last_warp_prewarm_ts = now
    # 没有 opencode 流量时不刷：预热是为了「下次换线有现成新鲜 IP」，没人
    # 在用时重启隧道只是白白换掉一个会话。24 小时窗口与 ip_reuse_guard_sec 一致。
    if count_opencode_since(now - 86400) == 0:
        return
    with _WARP_REFRESH_LOCK:
        # 补刷中的线不能当换线目标（会被重启掐断）；同时最多两条，留足可用备线。
        if len(_WARP_REFRESHING) >= 2:
            return
        refreshing = set(_WARP_REFRESHING)
    active = active_warp_tag()

    def _is_fresh(m: dict) -> bool:
        """该线已知出口 IP 是否优质（未被标记且窗口外）；IP 未知按旧会话处理。"""
        ip = _warp_line_ip(m["tag"])
        return (bool(ip) and not _warp_ip_is_blocked(ip)
                and not _warp_ip_recently_used(ip, now))

    # 已知 IP 仍新鲜的线不用再刷：重启会白换掉一个好会话。
    cands = [m for m in _warp()["members"]
             if m["tag"] != active and m["tag"] not in refreshing and not _is_fresh(m)]
    if not cands:
        return
    m = min(cands, key=lambda x: _warp_line_sel(x["tag"]))
    log(f"↻ 预热备线 {m['tag']}（换线时直接切，省掉同步刷新）")
    _schedule_idle_refresh(m)


def _rotate_on_error(errors: int, total: int, nodes: list[str], now: float) -> float:
    """WARP 未启用时的旧路径：错误触发节点轮换。返回新的冷却时间戳（0=未切换）。"""
    current = get_current_proxy()
    # 错误触发只换 IP，不拉黑节点。
    # WHY: 错误率无法区分「节点故障」与「上游限流」。实测最近 40 条错误
    # 100% 是 opencode FreeUsageLimitError(429)，而被拉黑节点复测 9/10
    # 连通正常（485-3557ms）——429 恰恰证明链路通（TCP/TLS/HTTP 走完），
    # 拉黑纯属误杀，曾把节点池抽干至 dead=50/56。判死交给基于真实探测的
    # 健康检查 _apply_test_results，它有 60% 保底守卫。
    if current:
        _cool_down_node(current)
        log(f"  ❄ {current} 进 429 冷却 {CFG['intervals']['429_cooldown_sec']}s")

    check_and_handle_fallback(nodes, current or "")

    # 错误触发轮换不计入调用次数（429 的请求不算成功调用）
    next_node = pick_next_node(current or "", nodes, set(), count_call=False)
    if not next_node:
        log(f"⚠ 无可用节点 (dead={len(_DEAD_NODES)} cooldown={len(_COOLDOWN_NODES)})")
        return 0
    reason = f"err={errors}/{total}"
    delay_info = f" ({_NODE_DELAYS[next_node]}ms)" if next_node in _NODE_DELAYS else ""
    log(f"→ 切代理 ({reason}): {format_node_display(current)} -> {format_node_display(next_node)}{delay_info}")
    if switch_proxy(next_node):
        update_node_info_async(next_node)
        return now + CFG["intervals"]["cooldown_after_switch_sec"]
    return 0


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


def test_node_quick(node: str, timeout_ms: int = 10000) -> bool:
    """快速可用性探测（切换前验证用），返回True/False

    超时从 3s 提升到 10s，与健康检查的 15s 对齐。原 3s 误杀大量慢节点
    （跨洋 600-1200ms 延迟 + TLS 握手），导致 56 节点池在 23:51 爆发式拉黑。
    """
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
        probe_ok = test_node_quick(n, timeout_ms=10000)
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


def _cool_down_node(node: str, seconds: int | None = None):
    """把刚被上游限流(429)的节点放进冷却池，短暂让开让速率窗口重置。

    与 _DEAD_NODES 不同：不判死、不进指数退避、不导出到 bad_nodes——
    节点本身是健康的（连通性探测正常），只是这一刻上游不接收请求。
    """
    secs = seconds if seconds is not None else CFG["intervals"]["429_cooldown_sec"]
    with _state_lock:
        _COOLDOWN_NODES[node] = time.time() + secs


def _prune_cooldown():
    """清掉冷却已到期的节点（需在 _state_lock 内调用或自行加锁）。"""
    now = time.time()
    with _state_lock:
        for n in [n for n, exp in _COOLDOWN_NODES.items() if exp <= now]:
            _COOLDOWN_NODES.pop(n, None)


def _filter_candidates(current: str, all_nodes: list, fail_set: set) -> list[str]:
    """Filter usable candidates, apply anti-sticky, fallback if < 5."""
    cleanup_dead_ttl()
    _prune_cooldown()

    def usable(n):
        if n == "direct" or n == current:
            return False
        if n in fail_set or n in _DEAD_NODES or n in _recent_nodes:
            return False
        if _COOLDOWN_NODES.get(n, 0) > time.time():
            return False   # 刚吃 429，让上游限流窗口过去
        return True

    with _state_lock:
        candidates = [n for n in all_nodes if usable(n)]
        # 候选不足时临时放行最近节点，但仍须排除冷却中的节点——
        # 刚吃 429 的节点必须让开，否则池子抽干时切回它只会继续 429。
        if len(candidates) < 5:
            now = time.time()
            for n in _recent_nodes:
                if len(candidates) >= 5:
                    break
                if (n not in fail_set and n not in _DEAD_NODES and n != current
                        and n not in candidates and _COOLDOWN_NODES.get(n, 0) <= now):
                    candidates.append(n)
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

    增加: 选中后快速探测可用性（预检，不判死），失败则重选（最多 3 次）。
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
        # 快速可用性探测（10s 超时），不更新 _NODE_DELAYS
        if test_node_quick(chosen, timeout_ms=10000):
            with _state_lock:
                _recent_nodes.append(chosen)
                if count_call:
                    _NODE_CALL_COUNTS[chosen] = _NODE_CALL_COUNTS.get(chosen, 0) + 1
            return chosen
        else:
            # 探测失败：只是当前候选不可用，不判死——快速探测是切换前预检，
            # 不是健康检查，不该消耗指数退避 TTL 或把节点拉黑（会误杀慢节点）。
            # 从候选中移除，重试；3 次后返回 None 交由上层兜底。
            candidates = [n for n in candidates if n != chosen]
            dedup_skip.add(chosen)
            log(f"  ⚠ 快速探测未通过，重选: {chosen} (尝试 {attempt + 1}/3)")

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

        # 重启后 selector 回到配置里的 default（主隧道）。记住重启前的选择，
        # 否则每小时订阅刷新都会把 opencode 拽回刚被判为被标记的那条线。
        prev_warp = active_warp_tag()

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
            if prev_warp and prev_warp != _warp()["members"][0]["tag"]:
                _switch_warp(prev_warp)
                log(f"  ↺ WARP 线路恢复为 {prev_warp}")
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
def _count_outbounds(path: str) -> int:
    """数配置文件里的代理节点数（不存在/损坏返回 0）"""
    try:
        with open(path) as f:
            cfg = json.load(f)
        return len([o for o in cfg.get("outbounds", [])
                    if o.get("type") not in ("direct", "selector")])
    except Exception:
        return 0

def _discard(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass

def _stderr_reason(stderr: str) -> str:
    """从子进程 stderr 提取最有诊断价值的一行。

    traceback 的异常行不一定在最后（多个 traceback / 后续警告会让它被淹没），
    优先找最后一条 `XxxError:` / `XxxException:` 行。
    """
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    if not lines:
        return "no stderr"
    exc = [l for l in lines if _re.search(r"\w+(Error|Exception|Warning):", l)]
    return (exc[-1] if exc else lines[-1])[:200]

def _config_check_error(path: str) -> str:
    """用 sing-box 校验配置；通过返回 ""，否则返回错误摘要。"""
    try:
        r = subprocess.run([CFG["singbox"]["binary"], "check", "-c", path],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:
        return f"check 无法执行: {e}"
    if r.returncode == 0:
        return ""
    msg = (r.stderr or r.stdout).strip().splitlines()
    return msg[-1][:200] if msg else f"rc={r.returncode}"

# 多个订阅源合并：okz（主力）+ hd.23n.club + dasho.pqjc.site + dasho.xn--cp3a08l.com（补充）
_SUB_URLS = [
    CFG["okz"]["sub_url"],
    CFG.get("hd23n", {}).get("sub_url", ""),
    CFG.get("dasho", {}).get("sub_url", ""),
    CFG.get("dasho_alt", {}).get("sub_url", ""),
]

_REFRESH_LOCK = threading.Lock()   # 串行化订阅刷新：启动路径与主循环会撞在一起

def refresh_subscription() -> bool:
    """拉所有订阅源 + merge 去重 + 生成 sing-box config + 重启 sing-box

    非阻塞获取锁：已有刷新在跑时直接返回 False，不再排队。两个并发刷新
    会写同一个 .tmp（交错内容让 _count_outbounds 读到 0），且第二次
    restart 会 pkill 掉第一次刚恢复的连接。
    """
    if not _REFRESH_LOCK.acquire(blocking=False):
        log("⚠ 已有订阅刷新在进行，跳过本次")
        return False
    try:
        return _refresh_subscription_locked()
    finally:
        _REFRESH_LOCK.release()

def _refresh_subscription_locked() -> bool:
    log("🔄 刷新订阅...")
    try:
        parse_script = os.path.join(os.path.dirname(__file__), "parse_subscription.py")
        config_file = CFG["singbox"]["config_file"]
        urls = [u for u in _SUB_URLS if u]
        if not urls:
            log("✗ 无可用订阅源")
            return False
        # 先写临时文件，节点数骤降时拒绝覆盖（订阅源故障/套餐过期时会返回
        # 占位节点，一旦覆盖会把正常节点池整个换掉）
        tmp_file = config_file + ".tmp"
        r = subprocess.run(
            [sys.executable, parse_script,
             "--out", tmp_file,
             "--urls", *urls],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            log(f"✗ 订阅解析失败 (rc={r.returncode}): {_stderr_reason(r.stderr)}")
            _discard(tmp_file)
            return False
        new_n = _count_outbounds(tmp_file)
        old_n = _count_outbounds(config_file)
        if new_n == 0:
            log("✗ 刷新产出 0 节点，保留现有配置")
            _discard(tmp_file)
            return False
        # 节点数骤降：old_n 为 0（配置缺失/损坏/编码异常）时护栏不可用，
        # 此时若旧配置本来存在，宁可拒绝也不要冒风险覆盖
        if old_n == 0 and os.path.exists(config_file):
            log("✗ 现有配置不可读（0 节点），无法判断刷新是否合理，保留现有配置")
            _discard(tmp_file)
            return False
        if old_n and new_n < old_n * 0.5:
            log(f"✗ 刷新节点数骤降 ({old_n} → {new_n})，疑似订阅源故障，保留现有配置")
            _discard(tmp_file)
            return False
        # 最后一道防线：promote 前用 sing-box 自身校验。单个坏节点
        # （非法 pbk/sid、缺 password 等）会让整个配置 FATAL，而一旦覆盖，
        # 好节点池就没了 —— 只能等下一小时刷新重装同一份毒配置。
        err = _config_check_error(tmp_file)
        if err:
            log(f"✗ 新配置未通过 sing-box check，保留现有配置: {err}")
            _discard(tmp_file)
            return False
        os.replace(tmp_file, config_file)
        log(f"✓ 订阅刷新完毕 ({old_n} → {new_n} 节点)，重启 sing-box 加载新配置...")
        return restart_singbox()
    except Exception as e:
        log(f"✗ 订阅刷新失败: {e}")
        _discard(config_file + ".tmp")   # 超时/异常时清除残留（含凭据）
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
_DAEMON_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".daemon.lock")
_DAEMON_LOCK_FD: int | None = None


def _acquire_single_instance_lock() -> None:
    """拒绝第二个 daemon 实例（PID 锁）。

    WHY 必须加：两个实例各自持有内存里的 `_WARP_IP_USED`，又都整份覆写
    `.warp_ip_used.json` —— IP 去重会互相失效。实测 2026-10-19 19:12:20 与
    19:15:01 两次启动并存期间，4 次在 2~9 秒内切回同一个刚被烧掉的 IP，
    日志里同秒重复行也集中出现在那段时间。锁文件不删除（unlink 会让后来者
    拿到新 inode，锁形同虚设），进程退出时由内核释放。
    """
    global _DAEMON_LOCK_FD
    fd = os.open(_DAEMON_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        log(f"✗ 已有 daemon 实例在运行（{_DAEMON_LOCK_FILE} 被占用），本实例退出")
        raise SystemExit(0)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _DAEMON_LOCK_FD = fd   # 持有到进程退出；不能 close，否则锁就放了


def main():
    _acquire_single_instance_lock()
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
    log("  错误触发: 上次成功切换后出现任意 1 条新错误即立即再切")
    log("=" * 50)

    # 初始化 node_info 表
    init_node_info_db()
    # 恢复跨重启的出口 IP 使用/污染记录与可用周期样本
    _load_warp_ip_used()
    _load_warp_ip_cycles()
    # 日志标签用的当前 WARP 出口 IP：启动时探一次，之后由换线路径更新
    global _ACTIVE_WARP_IP
    _m = _warp_member(active_warp_tag())
    if _m:
        _ACTIVE_WARP_IP = warp_egress_ip(_m["port"])
    else:
        # 启动时就停在 proxy 上：落池时刻从此刻算，否则「落池后仍拦截」
        # 判据会拿 0 当起点，把落池前的旧 403/429 也算进去。
        global _proxy_dwell_since
        _proxy_dwell_since = time.time()

    # 状态
    cooldown_until = 0
    last_singbox_check = 0
    last_health_check = 0
    # 启动时不立即触发刷新；但配置文件缺失时必须立刻重建，否则 sing-box
    # 起不来且要等到下一个 1h 刷新周期（配置不入库，全新 clone/误删即此状态）
    last_sub_refresh = 0 if not os.path.exists(CFG["singbox"]["config_file"]) else time.time()
    last_node_info_cleanup = time.time()
    last_proxy_check = 0
    last_summary_ts = 0
    last_proactive_rotate = 0

    global _RESTART_COUNT, _NODE_CALL_RESET_TS, _last_wal_checkpoint, _strategy_mode
    global _health_check_future, _health_check_nodes, _health_check_current
    global _last_warp_reconnect_ts
    with _RESTART_LOCK:
        _RESTART_COUNT = 0
    _NODE_CALL_RESET_TS = time.time()
    # 只统计本次运行之后出现的 abuse 403：否则重启后会把历史错误一并计数，
    # 每次启动都白换一次线
    _last_warp_reconnect_ts = _now_ts()

    # 启动检查：如果首次查询失败，等待几秒再重试（可能正在重启）
    if not all_nodes:
        log("⚠ sing-box 首次查询超时，等待5s重试...")
        time.sleep(5)
        all_nodes = get_all_proxy_nodes()
        if not all_nodes:
            # 配置文件不存在时直接重启必然失败（要白等 30s+60s 且 9router 走直连），
            # 先重建再重启。配置不入库，全新 clone / 误删即此状态。
            if not os.path.exists(CFG["singbox"]["config_file"]):
                log("⚠ 配置文件缺失，先重建订阅配置...")
                refresh_subscription()
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
            total, errors, _, consecutive_errors, reject_ratio = get_9router_error_rate()
            _maybe_init_active_ip(now)
            _record_active_ip_success(total, errors)

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
            # 由下方“切换后是否出现新错误”决定；reject_ratio 仅用于日志诊断。

            # ---- 3. 主动轮换（每 N 秒加权选新节点） ----
            # opencode 走 WARP 时换轮换池节点对它毫无作用（出口由 usque 决定），
            # 只会白白消耗节点调用计数并制造日志噪音。仅在 opencode 真的落在
            # 轮换池上时才轮换。
            if (not opencode_out_on_warp()
                    and now - last_proactive_rotate > CFG["intervals"]["proactive_rotate_sec"]):
                current = cached_current_proxy
                nodes = cached_all_nodes

                if nodes:
                    next_node = pick_next_node(current or "", nodes, set())
                    if next_node and next_node != current:
                        reason = "主动轮换"
                        delay_info = f" ({_NODE_DELAYS[next_node]}ms)" if next_node in _NODE_DELAYS else ""
                        log(f"→ 切代理 ({reason}): {format_node_display(current)} -> {format_node_display(next_node)}{delay_info}")
                        if switch_proxy(next_node):
                            # 与错误触发分支共用防抖：主动轮换后也不该被错误信号立刻再切
                            cooldown_until = now + CFG["intervals"]["cooldown_after_switch_sec"]
                            update_node_info_async(next_node)
                    elif not next_node:
                        log(f"⚠ 主动轮换: 无可用节点 (dead={len(_DEAD_NODES)})")

                last_proactive_rotate = now

            # ---- 3b. 清理过期 node_info (每 10 分钟) ----
            if now - last_node_info_cleanup > 600:
                last_node_info_cleanup = now
                prune_egress_log()   # 附带裁剪 egress_log（读取路径不再承担写操作）
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

            # ---- 3c0. 双向兜底的反向：停在 proxy 上时，池挂了或 WARP 复活都切回 ----
            _maybe_failback_to_warp(now)
            # 任一备线出口是脏 IP 就重连换干净地址 —— 与当前在 WARP 还是 proxy 无关。
            _reclaim_dirty_warp_lines(now)

            # 任一隧道出口疑似脏（403/429 标记或窗口内用过）就重连，直到换出优质
            # IP；掉在 proxy 池上时也要继续，否则池本身就是脏地址时永远回不来。
            _prewarm_standby_warp(now)

            # ---- 3c. 错误触发 ----
            if opencode_out_on_warp():
                # opencode 走 WARP 时换轮换池节点无效（出口由 usque 决定），
                # 只做隧道换线。
                # WHY 不能套用下面的 _last_successful_switch_ts 门控：它由主动轮换
                # （步骤 3，每 60s）刷新，用它当窗口起点会把爆发期间的 abuse 403
                # 整段吞掉 —— 实测 15:47~15:52 连续 252 条 403 一次换线都没触发。
                maybe_reconnect_warp(now)
                # 主动轮换只为「不等报错」—— 403 一出现，在途对话已经断了。
                _maybe_proactively_rotate_warp(now)
            else:
                # 用户要求：切换后只要出现新的错误就立即再切，不受冷却限制。
                # max_backoff_ts=上次成功切换时刻，天然排除切换前的旧错误，
                # 同一条错误不会导致连续重复切换。
                _, errors_after_switch, _, _, _ = get_9router_error_rate(
                    max_backoff_ts=_last_successful_switch_ts or None
                )
                if errors_after_switch >= 1:
                    cooldown_until = _rotate_on_error(
                        errors, total, cached_all_nodes, now) or cooldown_until

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
                cool_count = len(_COOLDOWN_NODES)
                err_threshold = 1 - CFG["thresholds"]["opencode_min_success_rate"]
            with _RESTART_LOCK:
                rc_display = _RESTART_COUNT
            log_health(f"出口={_egress_label(cached_current_proxy)} "
                       f"节点={len(cached_all_nodes)} 错误={errors}/{total} "
                       f"err>{(err_threshold*100):.0f}%→切"
                       f"冷却至={time.strftime('%H:%M:%S', time.localtime(cooldown_until)) if cooldown_until>0 else '-'}"
                       f" dead={dead_count} cool={cool_count} 策略={_strategy_mode}"
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