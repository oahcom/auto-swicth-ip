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
import tempfile
import functools
import ipaddress
import math
from logging.handlers import RotatingFileHandler
from typing import NamedTuple
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 从共享配置导入
from config import CFG, NODE_INFO_DB, OPENCODE_FREE_PROVIDER, RECENT_WINDOW, REJECT_WINDOW, BAD_NODES_TTL, BAD_NODES_MAX_TTL, MIXED_PORT, LOG_FORMAT, get_singbox_secret
from asip.atomic_io import append_line as _atomic_append_line
from asip.atomic_io import write_json as _atomic_write_json
from asip.atomic_io import update_json as _atomic_update_json
from asip.atomic_io import write_text as _atomic_write_text
from asip.clash_client import ClashClient
from asip.egress import TRACE_URL, probe_egress
from asip.redact import redact_secrets as _redact_secrets

# Clash API 的唯一 HTTP 通道。daemon 侧保留 `singbox_api` 这个名字是因为
# 测试与兄弟工具都以它为边界；内部实现统一到 asip，避免每个调用点各写一遍
# 状态码/超时/JSON 解析。
_CLASH = ClashClient(CFG["singbox"]["clash_api"], get_singbox_secret)

# 全局复用连接 - 避免每次操作开/关 SQLite 连接
_db_conn: sqlite3.Connection | None = None
_db_file_id: tuple[int, int] | None = None
_db_lock = threading.RLock()  # RLock: init_node_info_db 里 _get_db 需重入
_BAD_NODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bad_nodes.json")
# 出口节点归属日志：每次成功切换追加 (ts,node)，供 per-egress 错误率归因用。
_EGRESS_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".egress_log.jsonl")
# 池节点出口 IP 使用台账：ip -> 最后使用时刻（epoch）。按时间换线（blind 模式）
# 没有错误信号帮它验 IP，换线候选只能靠「这个 IP 刚被用过没」来避让 —— 窗口
# 见 CFG["traffic"]["ip_reuse_window_sec"]。数据源是换线成功后的
# fetch_node_ip_region（真实出口探测），不是某次切换时候选节点的预估 IP。
_NODE_IP_USED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".node_ip_used.json")
_NODE_IP_LOCK = threading.Lock()
_NODE_IP_USED: dict[str, float] = {}
def _node_ip_used_path() -> str:
    """IP 台账路径（NODE_IP_USED_FILE_PATH 可覆盖，测试用隔离；同
    `_egress_log_path()` 模式）。读写必须都走这里。"""
    return os.environ.get("NODE_IP_USED_FILE_PATH") or _NODE_IP_USED_FILE


def _load_node_ip_used() -> None:
    """启动时加载落盘的 IP 使用台账；坏文件当空台账（只丢过滤精度，不阻断启动）。"""
    global _NODE_IP_USED
    try:
        with open(_node_ip_used_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _NODE_IP_USED = {str(k): float(v) for k, v in data.items() if v}
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"✗ 读 IP 使用台账失败（当空台账）: {e}")


def _save_node_ip_used() -> None:
    """裁剪后落盘：窗口外的条目没有判据价值，留着只会越写越大。上限留
    24h 余量，让把窗口临时调大的部署能从历史里恢复一部分。"""
    now = time.time()
    keep = round(CFG["traffic"]["ip_reuse_window_sec"] or 21600) + 86400
    # 快照与落盘必须同锁：先放锁再写，另一个线程可能先写入更新的快照，
    # 随后本线程用旧快照覆盖它，丢掉刚记下的 IP。
    with _NODE_IP_LOCK:
        snapshot = {ip: ts for ip, ts in _NODE_IP_USED.items()
                    if now - ts < max(keep, 86400)}
        _write_json_atomic(_node_ip_used_path(), snapshot)


def _record_node_ip_used(ip: str | None, ts: float | None = None) -> None:
    """记一次「该出口 IP 刚被用来服务流量」。ts 缺省取当前时间。"""
    if not ip:
        return
    with _NODE_IP_LOCK:
        _NODE_IP_USED[ip] = float(ts or time.time())
    _save_node_ip_used()

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

def _db_file_identity() -> tuple[int, int] | None:
    try:
        st = os.stat(NODE_INFO_DB)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None

def _get_db() -> sqlite3.Connection:
    """获取模块级 SQLite 连接（惰性初始化，WAL 模式）。

    9router 重启/迁移可能用 rename/VACUUM INTO 替换 data.sqlite；旧连接会继续
    读被删除的 inode，导致错误率与配额计数永久冻结。这里按文件身份检测并重开。
    """
    global _db_conn, _db_file_id
    file_id = _db_file_identity()
    if (_db_conn is not None and file_id is not None
            and _db_file_id is not None and file_id != _db_file_id):
        with _db_lock:
            if _db_conn is not None and file_id != _db_file_id:
                try:
                    _db_conn.close()
                except sqlite3.Error:
                    pass
                _db_conn = None
    if _db_conn is None:
        with _db_lock:
            if _db_conn is None:  # double-checked locking
                _db_conn = sqlite3.connect(NODE_INFO_DB, check_same_thread=False, timeout=5)
                _db_conn.row_factory = sqlite3.Row
                _db_conn.execute("PRAGMA journal_mode=WAL")
                _db_conn.execute("PRAGMA busy_timeout=5000")
                _db_file_id = _db_file_identity()
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
# 出口 IP 历史日志：只追加、永不清理、启动不加载。与热表分开是因为热表每次
# 写入都要全量重写且只保留窗口内条目；历史放在里面会让每次换线重写整年数据。
_WARP_IP_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     ".warp_ip_history.jsonl")
# 当前 WARP 出口 IP，仅供日志标签用（健康行每 10s 打一条，不能现场探测）。
_ACTIVE_WARP_IP: str | None = None
# 最近一次成功切到的 WARP tag。外部进程（测试/手工/其他 agent）直接 PUT
# opencode-out=proxy 时没有日志、也不经过 _fallback_to_proxy 闸门；落池即断流，
# 主循环发现后用它回收。
_LAST_WARP_TAG: str | None = None
_LAST_POOL_RECLAIM_TS: float = 0.0
_POOL_RECLAIM_MIN_GAP_SEC = 10.0
# tag -> {"sel": 上次被选为出口的时刻, "ip": 已知的当前出口 IP}。
# 选线要挑「最久没被选过」的线：只看 IP 使用表不行 —— 出窗口的条目会被
# 清理，清理后所有备线并列最老，又退化成 members 顺序（实测 D/E 被选中 0 次）。
_WARP_LINE_STATE: dict[str, dict] = {}
# 主循环 / 换线线程 / 探测器 / 补刷线程多方读写。用 RLock + Condition：
# 换线只读表并等待探测器写入，不能再自己探测；状态层函数可重入获取。
_WARP_LINE_LOCK = threading.RLock()
_WARP_LINE_CV = threading.Condition(_WARP_LINE_LOCK)
_WARP_LINE_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     ".warp_line_state.json")
_WARP_LINE_SAVE_LOCK = threading.Lock()
_WARP_LINE_LAST_SAVE: float = 0.0
_PROBER_STARTED = False
_PROBER_THREAD: threading.Thread | None = None
_MODEL_PROBE_SEM: threading.BoundedSemaphore | None = None
_RESTART_SEM: threading.BoundedSemaphore | None = None
_PROBE_RATE_LOCK = threading.Lock()
_PROBE_RATE_TIMES: deque[float] = deque()
_last_proactive_warp_check: float = 0  # 主动轮换判据的上次查询时刻
# 健康摘要里展示的主动轮换进度。由判决函数写入，避免摘要再查一次库，
# 也保证日志显示的就是判据当时看到的数。
_proactive_progress: str = "-"
# 池侧定时轮换进度（blind 模式摘要用）。主循环每次触发/判定时更新，
# 摘要直接展示判据当时算出的时刻，不重算也不重查。
_POOL_PROACTIVE_LABEL: str = "-"
_last_warp_prewarm_ts: float = 0      # 上次预热备线的时刻
# 错误换线时的候选探测游标：每轮只实测前 N 条，下一轮从上次停下的位置继续。
# 全量探测 17 条要 15s，且每条烧一格配额 —— 换线越勤，池越快被烧空。
_PROBE_SLICE_CURSOR: int = 0
# 预热同样分轮：每轮只排 N 条，游标轮转覆盖全池。
_PREWARM_CURSOR: int = 0
# 出口 IP 可用周期样本：每段 = 换线生效 → 首次被 403/429 判不可用。
# {"ip", "cycle", "ended_at", "ok"}，只保留 24h。落盘跨重启保留。
_WARP_IP_CYCLES: list[dict] = []
_WARP_CYCLES_LOCK = threading.Lock()
_WARP_IP_CYCLES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".warp_ip_cycles.json")


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
    """同目录唯一临时文件 + fsync + replace，失败时记日志。

    台账是进程重启后唯一能恢复「哪些地址已被烧」的记录。直接 open(w) 会把
    原文件清零，崩溃/掉电就留下半截 JSON → 下次启动解析失败 → 脏地址当干净
    地址再切回去。临时文件名必须逐次唯一：多个线程写同一台账时，固定
    `<path>.tmp` 会让两段 JSON 交错。
    """
    try:
        _atomic_write_json(path, payload, mode=0o600)
    except Exception as e:
        log(f"✗ 写 {os.path.basename(path)} 失败: {e}")


def _save_warp_ip_used() -> None:
    """把 IP 使用记录落盘。

    进程内为空但盘上已有台账时拒绝写入：并发跑的测试进程/误启动的实例
    用空内存覆盖会把 16h 的 ban_until 全抹掉（已实际发生过）。
    """
    path = _warp_ip_used_path()
    if not _WARP_IP_USED:
        try:
            if os.path.getsize(path) > 2:
                log(f"  · 跳过空台账覆盖 {path}（盘上已有记录）")
                return
        except OSError:
            pass
    _write_json_atomic(path, {"used": _WARP_IP_USED})


_SEGMENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              ".warp_segment.json")


def _segment_path() -> str:
    return os.environ.get("WARP_SEGMENT_FILE_PATH") or _SEGMENT_FILE


_ACTIVE_SEG_LOCK = threading.Lock()
# 地址未知（启动补探/换线中/落池）时，下一次拿到 IP 需要按 IP 决定续算还是开新段。
_SEGMENT_PENDING = False


def _save_segment_start(ts: float, ip: str | None = None) -> None:
    """段起点落盘：重启后同一出口继续按同一段累计 150 格配额。

    显式传 ip，不读 `_ACTIVE_WARP_IP`：换线成功路径要先把新 IP 和段起点一起
    落盘，读取全局会让「写盘」与「改全局」的先后顺序决定落盘内容（实测出现
    新段配旧 IP，重启后段起点被 `_load_segment_start` 判为不同址而丢弃）。
    """
    try:
        _write_json_atomic(_segment_path(),
                           {"ts": float(ts), "ip": ip})
    except Exception as e:
        log(f"✗ 段起点落盘失败: {e}")


def _load_segment_start(ip: str | None, now: float | None = None) -> float:
    """只在出口 IP 未变时恢复段起点；换了地址就是新段。"""
    if not ip:
        return 0.0
    try:
        with open(_segment_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return 0.0
    except Exception as e:
        log(f"✗ 读取段起点失败: {e}")
        return 0.0
    if not isinstance(data, dict) or data.get("ip") != ip:
        return 0.0
    try:
        ts = float(data.get("ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    now = time.time() if now is None else now
    if ts <= 0 or now < ts - 60 or now - ts > 24 * 3600:
        return 0.0
    return ts


def _enter_segment(ts: float, ip: str | None) -> None:
    """开始一个新段：换线/换址/落池的唯一入口（调用方已持有需要的锁）。"""
    global _last_warp_switch_ok_ts, _ACTIVE_WARP_IP, _SEGMENT_PENDING
    with _ACTIVE_SEG_LOCK:
        _last_warp_switch_ok_ts = float(ts)
        _ACTIVE_WARP_IP = ip
        _SEGMENT_PENDING = ip is None
        _save_segment_start(ts, ip)


def _adopt_active_ip(ip: str, overwrite: bool = False) -> None:
    """补探/重探拿到出口 IP 后接管：同一 IP 只补段起点，不同 IP 开新段。

    overwrite=True 用于 sing-box 重启后的重探：会话已重开，即使地址恰好相同
    也是新段（旧段的 150 格计数属于旧会话，不能顺延）。
    """
    global _last_warp_switch_ok_ts, _ACTIVE_WARP_IP, _SEGMENT_PENDING
    with _ACTIVE_SEG_LOCK:
        same = (not overwrite and ip == _ACTIVE_WARP_IP
                and _last_warp_switch_ok_ts > 0)
        _ACTIVE_WARP_IP = ip
        _SEGMENT_PENDING = False
        if same:
            _save_segment_start(_last_warp_switch_ok_ts, ip)
        else:
            _last_warp_switch_ok_ts = time.time()
            _save_segment_start(_last_warp_switch_ok_ts, ip)


def _clear_active_ip() -> None:
    """出口未知/在池：只清全局地址，不改段起点（等下一次补探按 IP 恢复）。"""
    global _ACTIVE_WARP_IP, _SEGMENT_PENDING
    with _ACTIVE_SEG_LOCK:
        _ACTIVE_WARP_IP = None
        _SEGMENT_PENDING = True


def _begin_segment(ts: float) -> None:
    """兼容入口：只换段起点，不动当前地址。"""
    global _last_warp_switch_ok_ts
    with _ACTIVE_SEG_LOCK:
        _last_warp_switch_ok_ts = float(ts)
        _save_segment_start(ts, _ACTIVE_WARP_IP)


def _warp_ip_history_path() -> str:
    """历史日志路径（WARP_IP_HISTORY_FILE_PATH 可覆盖，测试隔离用）。"""
    return os.environ.get("WARP_IP_HISTORY_FILE_PATH") or _WARP_IP_HISTORY_FILE


def _append_warp_ip_history(ip: str, now: float, blocked: bool,
                            kind: str | None = None) -> None:
    """追加一条出口 IP 使用记录（一次换线一行）。

    只追加不重写：历史要永久保留，而热表每次写入都全量重写，两者混在一起
    会让换线路径的成本随时间线性增长。写入失败只记日志 —— 历史是旁路，
    不能因为它挡下换线。

    `k` 记录被哪类错误烧掉（"403" / "429"），缺失表示未标记或旧格式 ——
    旧读取方只认 ip/t/b，多一个键不影响它们。
    """
    rec = {"ip": ip, "t": now, "b": 1 if blocked else 0}
    if blocked and kind:
        rec["k"] = "403" if kind == "abuse403" else "429"
    try:
        _atomic_append_line(_warp_ip_history_path(), rec)
    except Exception as e:
        log(f"✗ 写 .warp_ip_history.jsonl 失败: {e}")


def _warp_ip_history_stats(path: str | None = None) -> dict:
    """流式聚合历史日志 → {ip: {"uses", "last_ts", "last_blocked_ts"}}。

    uses 是重连次数（该 IP 被选为出口或被标记的次数），last_ts 是上次连接时间。
    启动时不加载、只在调用时逐行读：历史永久保留，整年数据不能常驻内存。
    坏行跳过并计入 "bad_lines"，单行损坏不能毁掉整体统计。
    """
    stats: dict[str, dict] = {}
    bad = 0
    try:
        with open(path or _warp_ip_history_path(), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ip, t = rec["ip"], float(rec["t"])
                    if not isinstance(ip, str) or not ip:
                        raise ValueError("ip")
                except (ValueError, KeyError, TypeError):
                    bad += 1
                    continue
                s = stats.setdefault(ip, {"uses": 0, "last_ts": 0.0,
                                          "last_blocked_ts": None})
                s["uses"] += 1
                s["last_ts"] = max(s["last_ts"], t)
                if rec.get("b"):
                    s["last_blocked_ts"] = max(s["last_blocked_ts"] or 0.0, t)
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"✗ 读 .warp_ip_history.jsonl 失败: {e}")
    if bad:
        log(f"⚠ .warp_ip_history.jsonl 跳过 {bad} 行损坏记录")
    return stats


def _proxy_fallback_path() -> str:
    """落池时刻路径（WARP_PROXY_FALLBACK_FILE_PATH 可覆盖，测试隔离用）。"""
    return os.environ.get("WARP_PROXY_FALLBACK_FILE_PATH") or _PROXY_FALLBACK_FILE


def _load_proxy_fallback_ts() -> float:
    """惰性读取落盘的落池时刻；文件缺失/损坏返回 0，且只读一次。

    读失败不能挡住换线主流程 —— 最坏退化成「冷却从本次启动重新计」。
    """
    global _last_proxy_fallback_ts, _proxy_fallback_ts_loaded
    if _proxy_fallback_ts_loaded:
        return _last_proxy_fallback_ts
    _proxy_fallback_ts_loaded = True
    try:
        with open(_proxy_fallback_path(), "r", encoding="utf-8") as f:
            _last_proxy_fallback_ts = float(json.load(f).get("ts", 0))
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"✗ 加载 .warp_proxy_fallback.json 失败: {e}")
    return _last_proxy_fallback_ts


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


def _record_warp_cycle(ip: str | None, cycle: float, now: float, ok: int = 0,
                       blocked: int = 0) -> None:
    """记一条可用周期样本并落盘（只保留 24h）。

    cycle == 0 表示「切上去第一条请求就 403/429」（ok=0）。这类样本要留档
    以便日志统计，但**不参与**最小周期计算 —— 24h 实测 80 段里 13 段如此，
    算进去会让最小值恒为 1s，主动轮换变成每 tick 都换。

    ok/blocked 都是**整段累计**（success 条数 / 403+429 条数），不是滑动窗口
    里的条数：本函数要回答「这条出口一共服务了几条才死」，窗口口径答不了。
    """
    if not ip or cycle < 0:
        return
    # 真实可用寿命 = 首条成功 → 末条成功，而不是「换上去 → 首次报错」：
    # 后者把换上去到流量抵达的空窗算进寿命，又在第一次间歇性 403/429 处截断，
    # 实测中位只有 2s，而真实可用跨度中位 14s。主动轮换的切点必须按后者定，
    # 否则每个 IP 只用掉不到三成就被扔掉。
    with _WARP_IP_LOCK:
        entry = _WARP_IP_USED.get(ip)
    span = None
    if isinstance(entry, dict) and entry.get("first_ok_ts") and entry.get("last_ok_ts"):
        span = max(float(entry["last_ok_ts"]) - float(entry["first_ok_ts"]), 0.0)
    with _WARP_CYCLES_LOCK:
        _WARP_IP_CYCLES.append({"ip": ip, "cycle": float(cycle),
                                "span": span, "ended_at": now,
                                "ok": int(ok), "blocked": int(blocked)})
        _WARP_IP_CYCLES[:] = _prune_warp_cycles(_WARP_IP_CYCLES, now)
        snapshot = list(_WARP_IP_CYCLES)
        # 同锁落盘：放锁后再写会让另一个线程的更新快照被本线程的旧快照覆盖。
        _write_json_atomic(_warp_ip_cycles_path(), {"cycles": snapshot})


_WARP_REFRESH_LOCK = threading.RLock()
# 补刷分两段，必须拆成两个集合 —— 混在一起是候选池饿死的根因。
#   排队段（PENDING）：等连接自然结束，最长 ip_refresh_retry_sec(600s)。这期间
#     线**仍能服务**（interrupt_exist_connections=false，在途请求还走它），
#     换线也该能选中它 —— 所以不占并发名额、不参与候选排除。
#   重启段（REFRESHING）：真正 `systemctl restart` 的那 6~9s，此时才不可用。
# 旧实现把两段都塞进 REFRESHING，而活跃线的长连接（SSE，实测单条存活 936s）
# 让排队段几乎永远等不到空闲 —— 每条线一调度就占住名额约 300s。换线间隔 2~6s
# 意味着每分钟消耗 10~30 个名额，供给几乎为零：实测日志 25 次「备线 1 条」、
# 1 次「备线 0 条」后直接落池。名额语义只能是「同时重启几条」，不能是「排了几条」。
_WARP_REFRESHING: set[str] = set()
_WARP_REFRESH_PENDING: set[str] = set()


# ============= 模型层探测台账：按 /128 记，TTL 按结论持久性分两档 =============
# opencode 免费层一次 probe() 就烧掉一格配额。此前无任何记录：同一 IP 在每轮换线
# 的并发实测与每次脏线守护里被反复探测 —— 实测 18 分钟内被探 26 次，全是 quota。
#
# 键一律 /128，不按 /64 段聚合：段内其他 IP 的信息量不足以判死。控制变量后
# （排除「日志里没探过但生产流量用过」的 IP）：
#   已烧段里的**全新** /128   n=2469  OK=0   (0.0%)
#   未烧段里的**全新** /128   n=152   OK=3   (2.0%)   ← 决定性的反例
# 段本身不是充分条件；真正判死的是那个地址自己。
#
# TTL 按结论的**持久性**分两档，不是按好坏分：
#   地址级持久（quota / flagged）：重探同一 /128 有 99.2% 仍 quota，30h 内只出现
#     过 1 次恢复（间隔 56min）—— 留 6h，与生产 403 惩罚期同量级。
#   其余（OK / unreachable / err）：OK 的重探复现率 <10s 97.9% → <1h 76.5% →
#     ≥1h 43.2%；unreachable 与 err 更是探测侧的瞬时故障（日志实测同一地址 err
#     后 66s 就 OK）—— 这一档只留 2min。
# 后三个若套用长 TTL，一次探测抖动就把健康线路锁死 6h：重连已不作废台账，
# 期间 `_warp_model_usable` 一直跳过实测返回 False，刷新守卫还会反复重启它。
#
# 这是「脏 IP 预检查」的探测侧采样，与 `_WARP_IP_USED` 的生产侧采样是同一个概念
# （地址在某时刻不可用，随时间恢复），区别只在观测来源。两者合并留待后续 ——
# 先解决当前重复探测。
_WARP_PROBE_SEEN: dict[tuple[str, str], tuple[str, float]] = {}

# 地址最近一次被 403/429 标脏的时刻。只为一件事存在：拒收「起手早于标脏」的
# 在途探测结论（见 `_probe_cache_put`）。按 IP 不按 (tag, ip) —— 标脏是地址属性。
_PROBE_BLOCKED_AT: dict[str, float] = {}

# 只有这两个是地址自身给出的持久结论；探测侧故障不代表地址的长期状态。
_WARP_PROBE_PERSISTENT = frozenset({"quota", "flagged"})

# OK 单独一档，比瞬时档长、比持久档短。WHY 不能和 unreachable/err 一起按 2min
# 过期：OK 说的也是地址状态（这格配额此刻能用），而它的重探间隔中位是 212s ——
# 120s 的档位命中不到自己，同一地址每轮都重探一遍，实测 1980 次「前一次 OK」里
# 1831 次（92.5%）结论不变，那 38% 的探测量纯属白烧配额。
# 上限受「OK 后多久翻脸」约束：29 次 OK→quota 的间隔中位 188s、p90 更短，而 47 例
# 翻脸里 45 例期间发生过生产侧 429/403 换线 —— 地址是被真实流量烧的，那条路径会
# `_probe_cache_forget` 立即作废 OK，不依赖本 TTL 兜底。取 600s：命中率已饱和
# （1200s 只多 0.1 个点），再长只是把「凭旧 OK 选线」的窗口拉宽。
_WARP_PROBE_OK = frozenset({"OK"})

# 模型探测结论 → 生产侧惩罚类型。quota 是 FreeUsageLimitError（配额耗尽，24h），
# flagged 是 abusive traffic（信誉标记，6h）—— 与 `_classify_warp_error` 归出的
# rate429 / abuse403 完全同义，只是观测来源从生产流量换成了主动探测。
_MODEL_VERDICT_BLOCK_KIND = {"quota": "rate429", "flagged": "abuse403"}


def _probe_ttl(verdict: str) -> float:
    w = _warp()
    if verdict in _WARP_PROBE_PERSISTENT:
        return float(w.get("probe_persistent_ttl_sec", 21600))
    if verdict in _WARP_PROBE_OK:
        return float(w.get("probe_ok_ttl_sec", 600))
    return float(w.get("probe_transient_ttl_sec", 120))


def _probe_cache_get(tag: str, ip: str, now: float | None = None) -> str | None:
    """这条线在 TTL 内探过这个地址就复用结论，None 表示该探。"""
    if now is None:
        now = time.time()
    with _WARP_REFRESH_LOCK:
        hit = _WARP_PROBE_SEEN.get((tag, ip))
        if hit is None:
            return None
        verdict, ts = hit
        if now - ts > _probe_ttl(verdict):
            del _WARP_PROBE_SEEN[(tag, ip)]
            return None
        return verdict


def _probe_cache_verdict(tag: str | None, ip: str | None,
                         now: float | None = None) -> str | None:
    """该线对这个地址的实测结论；没探过或已过期返回 None。

    与 `_probe_cache_get` 同一张台账：探测器写、刷新判据读 —— 探到的 quota
    必须能反过来驱动「这条线该重启」，否则结论只进不出：线永远不被选中
    刷新，台账永远命中「已探过（跳过）」，脏线焊死在原地。
    """
    if not tag or not ip:
        return None
    return _probe_cache_get(tag, ip, now)


def _probe_cache_put(tag: str, ip: str, verdict: str,
                     now: float | None = None,
                     started_at: float | None = None) -> bool:
    """记下一条实测结论；`started_at` 早于该地址的标脏时刻时拒收并返回 False。

    拒收这一支处理**在途探测**：探测 1.4~8.7s，期间该地址完全可能已被 403/429
    烧掉并被 `_probe_cache_forget` 清空 —— 只删已有的条目挡不住紧接着返回的
    那次 `_probe_cache_put`，它会拿着标脏前起手的 OK 把台账重新填满。
    """
    if now is None:
        now = time.time()
    with _WARP_REFRESH_LOCK:
        if started_at is not None and started_at <= _PROBE_BLOCKED_AT.get(ip, 0.0):
            return False
        _WARP_PROBE_SEEN[(tag, ip)] = (verdict, now)
        # 按各自 TTL 清：命中同一 key 时上面已覆盖，这里只清别的地址。
        for k, (v, ts) in list(_WARP_PROBE_SEEN.items()):
            if now - ts > _probe_ttl(v):
                del _WARP_PROBE_SEEN[k]
    return True


def _probe_cache_forget(ip: str | None) -> None:
    """该地址一旦被 403/429 标记，作废它所有的探测结论。

    WHY 键控在 (tag, ip) 却按 ip 全清：被烧是**地址**的属性，不是某条隧道的。
    同一地址可能被两条线先后持有，只清当前 tag 会留下另一条的 OK 结论。

    WHY 必须清：模型模式下历史不否决候选，能不能用只认这张台账；而 OK 结论
    的 TTL 是 120s，远长于实测段寿命（cache 验收选中的段 p50 只有 6s）。于是
    「探过 OK → 被 429 烧掉 → 9 秒后又拿着那条旧 OK 切回去」成为闭环：
    实测 829 次换线里 170 次（20.5%）是同一个 IP 在 120s 内被重新选中，
    其中 158 次弹回所依据的 OK 早于该 IP 的标脏时刻 —— 证据比事实旧。
    作废后必须重新实测，实测要么给 quota 走刷新，要么给新的 OK。

    同时记下作废时刻（墓碑）：删表只能清掉已落地的结论，挡不住正在飞的那次
    探测回来补写，见 `_probe_cache_put` 的 `started_at` 判据。
    """
    if not ip:
        return
    now = time.time()
    with _WARP_REFRESH_LOCK:
        _PROBE_BLOCKED_AT[ip] = now
        for k in [k for k in _WARP_PROBE_SEEN if k[1] == ip]:
            del _WARP_PROBE_SEEN[k]
        # 墓碑只需覆盖「探测在途」那几秒，留最长 TTL 已远超需要；顺带定长。
        keep = now - _probe_ttl("quota")
        for k in [k for k, ts in _PROBE_BLOCKED_AT.items() if ts < keep]:
            del _PROBE_BLOCKED_AT[k]


# 每条线「离开主渠道」的时刻与此后累计的隧道重启次数。刷新日志据此回答
# 用户要的三件事：谁在刷、为什么刷、刷了几次 —— 计数从上次离开主渠道起算，
# 切回主渠道即清零（语义由用户指定）。
_WARP_OFF_AGE: dict[str, float] = {}
_WARP_REFRESH_COUNT: dict[str, int] = {}
# 该线最近一次拿到可用地址的时刻。有了它，日志里的「第 N 次刷」才不会把
# 「反复刷成功又反复自转变脏」读成「一次连续失败」：实测 WARP-CF-C 23:11:53
# 刷出 OK，5 分钟后同一 N（无重启）下地址已换成另一个被烧的地址 —— usque
# 自己会重连换址，那 67 分钟不是一次刷新，是 29 个短周期。
# 该线最近一次验收通过的 (时刻, 地址)。有了它，日志里的「第 N 次刷」才不会
# 把「反复刷成功又反复自转变脏」读成「一次连续失败」：实测 WARP-CF-C 23:11:53
# 刷出 OK，5 分钟后同一 N（无重启）下地址已换成另一个被烧的地址 —— usque
# 自己会重连换址，那 67 分钟不是一次刷新，是 29 个短周期。
#
# WHY 必须连地址一起存：usque 自重连会在同一 tag 下换址，只存时刻就答不出
# 「验的到底还是不是这个地址」。旧实现拿缓存 `_warp_line_ip` 当第二个源，
# 而那张缓存会被 `_invalidate_warp_line_ip` 在每次重启时清空、也会被换线
# 路径（不改验收记录地）写进新地址 —— 两个源各说各的，既会把没验过的地址
# 当成已验收（换线写了缓存、验收记录来自更早的另一个地址），也会在缓存被
# 清掉后把同一个已验证地址反复重探（每探一次烧一格配额）。
_WARP_LAST_ACCEPT: dict[str, tuple[float, str]] = {}


def _warp_last_accept(tag: str) -> tuple[float, str] | None:
    """该线上次验收通过的 (时刻, 地址)；没有则 None。"""
    acc = _WARP_LAST_ACCEPT.get(tag)
    return acc if isinstance(acc, tuple) and len(acc) == 2 else None


def _warp_refresh_progress(tag: str) -> str:
    """刷新进度短语；该线不在「离渠刷新」中则返回空串。

    两个计数各说各的事，必须都写清楚：`第 N 次刷` 是离渠以来的累计重启数，
    `X min` 是离渠以来的总时长 —— 后者包含成功后的静置期，**不是**一次刷新的
    耗时。实测 WARP-CF-C 在 65.4min / 29 次刷 里，有 45.6min 是 OK 着不动的；
    只印这两个数，读起来就像「刷了 65 分钟还是坏的」。

    再补 `上次成功 X min 前`：地址是 usque 自己在换的（同一 N 下也会变），
    「刚 OK 过又判 quota」是那个地址被换掉了，不是 OK 被无视。
    """
    off = _WARP_OFF_AGE.get(tag)
    if off is None:
        return ""
    n = _WARP_REFRESH_COUNT.get(tag, 0)
    acc = _warp_last_accept(tag)
    ago = f"，上次成功 {(time.time() - acc[0]) / 60:.1f}min 前" if acc else ""
    return f"（离渠后第 {n} 次刷，{(time.time() - off) / 60:.1f}min{ago}）"


def _warp_refresh_count_note(tag: str) -> str:
    """模型层日志用：离渠中显示离渠进度，否则显示本会话重启次数（0 次不显示）。

    备线未必当过出口 —— 那类线没有「离渠」起点，`_warp_refresh_progress`
    返回空串，日志就只剩一句「quota」，看不出它被刷过几次。
    """
    prog = _warp_refresh_progress(tag)
    if prog:
        return prog
    n = _WARP_REFRESH_COUNT.get(tag, 0)
    return f"（本会话重启 {n} 次）" if n else ""


def _track_offline_refresh(prev_tag: str | None, new_tag: str | None) -> None:
    """出口变更时维护「离渠刷新」计数。

    prev_tag 被换下 → 记为离渠起点、计数归零；new_tag 重新成为出口 → 结束
    这一轮（计数清零）。两条线同时成立时按序处理，互不干扰。
    """
    now = time.time()
    if prev_tag and prev_tag != new_tag:
        _WARP_OFF_AGE[prev_tag] = now
        _WARP_REFRESH_COUNT[prev_tag] = 0
    if new_tag:
        _WARP_OFF_AGE.pop(new_tag, None)
        _WARP_REFRESH_COUNT.pop(new_tag, None)


def _standby_cooldown_left(tag: str) -> float:
    """该线离渠后还要等多久才允许重启；不在离渠态或已过冷却返回 0。

    这是**下限**，不是放行条件：冷却期满只代表「离渠那一刻建的请求该走完了」，
    线上的连接可能活得更久。真正的放行由 `warp_line_busy` 的连接判据决定，
    见 `_refresh_idle_warp` 与补刷循环两处的组合用法。

    WHY 时间下限仍然要留：流在等首 token 时一个字节都不传，实测 TTFT
    p90=52.6s / max=532.7s（N=83），但连接年龄判据也只认 120s 以内
    （`_FIRST_BYTE_WAIT_MAX_SEC`）—— 年龄超过 120s 的连接不再算「可能仍在
    进行」，`warp_line_busy` 会把它当空闲。离渠后 120s 内不重启，是给
    「离渠瞬间才建立、还在等首字节」的连接留出被看到的窗口；过了 120s
    就交给连接判据，不再无脑等。
    """
    off = _WARP_OFF_AGE.get(tag)
    if off is None:
        return 0.0
    return max(0.0, _warp()["standby_cooldown_sec"] - (time.time() - off))


def _standby_defer_reason(tag: str) -> str | None:
    """该线此刻不该重启的原因；可以重启返回 None。

    判据是**有没有连接**，不是时间。一条连接都没有的线不存在会被掐断的请求，
    冷却对它只是空等 —— 实测 17 条备线常驻仅 2 条挂连接，其余全是空壳，
    按时间一刀切会让这些线白等 2 分钟才轮到刷新。时间下限只约束「有连接」
    这一支：那条线可能还在排空，重启就是掐断在途 SSE。
    """
    cool = _standby_cooldown_left(tag)
    if cool <= 0:
        return None
    stats = _warp_line_stats(tag)
    if stats is None:
        return f"离渠冷却剩 {cool:.0f}s（连接查询失败，保守等待）"
    if stats[2] == 0:
        return None
    return f"离渠冷却剩 {cool:.0f}s，仍有 {stats[2]} 条连接"


def _warp_ip_refresh_reason(ip: str | None, now: float,
                            tag: str | None = None) -> str:
    """低质原因的可读短语；健康地址返回空串。与 _warp_ip_needs_refresh 同源。"""
    if not ip:
        return "出口未知"
    verdict = _probe_cache_verdict(tag, ip, now)
    if verdict in _WARP_PROBE_PERSISTENT and not _warp_ip_is_blocked(ip, now):
        # 精确解禁已到期：旧结论不再作为低质理由（与 needs_refresh 同源）。
        verdict = None
    if verdict is not None and verdict != "OK":
        if not (verdict == "unreachable" and _warp_probe_blind(now)):
            return f"模型层 {verdict}"
    if _warp_ip_is_blocked(ip, now):
        with _WARP_IP_LOCK:
            entry = _WARP_IP_USED.get(ip) or {}
        return f"{entry.get('blocked_kind') or 'blocked'} 惩罚期"
    if _ip_history_is_truth():
        return "复用窗口内" if _warp_ip_recently_used(ip, now) else ""
    # 档 3 才是「出现过却一次 OK 都没拿到」。档 2 是探针验过但还没轮到它服务，
    # 那种地址此刻能服务，只是没撑过真实流量 —— 不当作低质原因报。
    return ("从无成功流量"
            if _warp_ip_rank(ip, now, _warp()["ip_reuse_guard_sec"])[0] == 3
            else "")


def _acquire_refresh_slot(tag: str) -> bool:
    """占住这条线的重启独占权；已被别的线程占着返回 False。

    WHY 不设全局上限：20 条隧道之间没有任何依赖，各自 systemctl restart 互不影响。
    排队换不来任何东西 —— 刷不完的脏线留在池里冒充候选，换线选中它还得当场同步
    刷新，代价更大。唯一需要互斥的是**同一条线**：两条线程同时重启同一条隧道会
    白烧一次探测（实测 WARP-CF-C 02:31:49~02:32:03 出现 #1#1#2#2#3）。

    WHY 删掉原来的全池上限：它的默认值就是成员数，平时根本不生效；而生效时报出来
    的「N 条在重启」是**上限常量**不是实际集合大小 —— 日志里 134 次「名额已满」中
    128 次（95.5%）其实是同一个 tag 自己撞自己。删掉后 False 的含义从此唯一：
    这条线正在别处被刷。
    """
    with _WARP_REFRESH_LOCK:
        if tag in _WARP_REFRESHING:
            return False
        _WARP_REFRESHING.add(tag)
        return True


def _release_refresh_slot(tag: str) -> None:
    with _WARP_REFRESH_LOCK:
        _WARP_REFRESHING.discard(tag)

# 脏线守护的探测中集合。探测不占用 _WARP_REFRESHING —— 那条集合会被换线路径
# 当作「不可选」，探测中的干净线若也算进去，换线会看到「备线 0 条」直接落池。
_WARP_PROBING: set[str] = set()
# 本轮因「仍在传数据」让出的线 → 冷却截止时刻。没有它守护每 5s 就会重派一次
# 探测线程（每次 2 次出口探测 ≈ 2~14s），把活线当靶子反复打；冷却让节奏回到
# 一条线最多每 refresh_delay_sec 被探一次。
_WARP_BUSY_UNTIL: dict[str, float] = {}
# 冷却等待的日志节流：同一轮 defer 只打一行，跨过 5s 门槛后重新打。
_WARP_DEFER_LOGGED: dict[str, bool] = {}
_last_proxy_pool_check: float = 0  # 上次探测轮换池可用性的 epoch 秒（双向兜底）
_proxy_dwell_since: float = 0  # 落到轮换池的时刻；0 = 现不在 proxy 上
# 上次落池的 epoch 秒，「每 3h 最多切一次」的冷却依据。落盘跨重启保留：
# 只存内存的话重启即清零，两个坏出口之间来回弹会立刻恢复。
_last_proxy_fallback_ts: float = 0
_proxy_fallback_ts_loaded = False   # 惰性读取只做一次
_PROXY_FALLBACK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".warp_proxy_fallback.json")
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

_BG_BUSY: set[str] = set()
_BG_LOCK = threading.Lock()


def _submit_once(key: str, fn, *args) -> bool:
    """后台跑一次；同名任务未结束就跳过。返回是否真的启动了。

    主循环不允许阻塞：探测（最慢 15s）、切节点、重启 sing-box 都丢到这里。
    同名任务不重入 —— 上一轮还没回来时再发一次，只会排出一串重复工作，
    而它们读的都是同一份已经过时的现场。
    """
    with _BG_LOCK:
        if key in _BG_BUSY:
            return False
        _BG_BUSY.add(key)

    def _run():
        try:
            fn(*args)
        except Exception as e:
            log(f"✗ 后台任务 {key} 异常: {e}")
        finally:
            with _BG_LOCK:
                _BG_BUSY.discard(key)

    threading.Thread(target=_run, daemon=True, name=f"bg-{key}").start()
    return True
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
    # 日志会带订阅源/探测细节，默认 0644 等于对同机其他用户公开；台账与
    # .env 都是 0600，日志不能例外。
    try:
        os.chmod(CFG["log_file"], 0o600)
    except OSError:
        pass
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt=LOG_FORMAT))
    _logger.addHandler(fh)
    # stdout handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG)
    sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt=LOG_FORMAT))
    _logger.addHandler(sh)

_LOG_LINE_BREAKS = _re.compile(r"[\r\n\u2028\u2029\x85\v\f]+")
# 上游错误体/节点名可能带 ANSI 转义或其它 C0 控制符，能操纵终端显示或让
# 按行解析的脚本看到假内容。先剥 CSI/OSC 序列，再把剩余控制符换成空格。
_LOG_ANSI = _re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_LOG_CONTROL = _re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_LOG_MESSAGE_MAX = 4000


def _sanitize_log_message(msg: str) -> str:
    """把一条消息压成单行。

    WHY：日志格式是 `[时间] 消息`，消息里若带换行（节点名、上游错误体、
    订阅响应都可能带），后半段会冒充一条独立日志行，误导按行解析的
    运维脚本。这不是安全问题本身，但会掩盖真实的告警顺序。
    """
    text = msg if isinstance(msg, str) else str(msg)
    text = _LOG_ANSI.sub("", text)
    text = _LOG_LINE_BREAKS.sub(lambda _match: "\\n", text)
    text = _LOG_CONTROL.sub(" ", text)
    if len(text) > _LOG_MESSAGE_MAX:
        text = text[:_LOG_MESSAGE_MAX] + "…(truncated)"
    return text


def log(msg: str):
    """写日志到文件（轮转）+ stdout，线程安全"""
    global _logger
    with _log_lock:
        if _logger is None:
            _init_logger()
    _logger.info(_sanitize_log_message(msg))

def log_health(msg: str):
    """健康状态日志，每 10s 最多一条"""
    global _LAST_HEALTH_LOG
    now = time.time()
    if now - _LAST_HEALTH_LOG < 10:
        return
    _LAST_HEALTH_LOG = now
    log(msg)

# ============= 9router 监控 =============

# opencode 免费层的 429 换过文案：旧版是 "Rate limit exceeded"，现版是
# "Free usage limit reached. Subscribe to OpenCode Go..."（type 仍是
# FreeUsageLimitError）。只认旧文案时实测最近 2000 条错误里 1751 条 429 全被
# 判成「非出口级错误」→ consec_err 恒 0 → 87% 错误率下也永不换线。
# 两个文案都留：免费层可能按账号/时间灰度高版，不是全局切换。
def _classify_egress_error(body: str | None) -> str | None:
    """把一条错误归为出口级拦截，返回 'abuse403' / 'rate429' / None。

    WHY 必须区分：只有这两类按出口 IP 判定，换出口即拿到新配额/新信誉。
    其余错误归因给出口是错的 —— 实测 requestDetails 全表 605 条错误里
    507 条 429、18 条 "maximum context length"（请求本身太长）、80 条
    "Endpoint is unavailable"（上游 402，longcat 模型独占 77 条）。后两类
    与出口无关：108/108 条 402 在 3 秒内由 combo 的下一模型成功兜住。
    """
    if not body:
        return None
    if "abusive traffic" in body:
        return "abuse403"
    if "Rate limit exceeded" in body or "Free usage limit reached" in body \
            or "FreeUsageLimitError" in body:
        return "rate429"
    return None


class ErrorWindow(NamedTuple):
    """`get_9router_error_rate` 的返回结构（保持 tuple 解包兼容）。"""

    total: int
    errors: int
    details: list[dict]
    consecutive_errors: int
    reject_ratio: float


def get_9router_error_rate(max_backoff_ts: int | None = None) -> ErrorWindow:
    """从 SQLite 读最近 N 个 OpenCode Free 请求，返回 ErrorWindow。

    reject_ratio：最近 REJECT_WINDOW 条中 403 占全部错误的比例。OpenCode 免费层只允许
    其自家客户端使用，403 FreeTierError 是客户端指纹拒绝，与出口 IP 无关——据此避免无效轮换。

    明确针对 OpenCode Free provider（`requestDetails.provider = "opencode"`）。
    其他免费 provider 不参与切换决策——它们不触发 IP 轮换。

    检测策略：两层检测
    - 窗口检测：最近 RECENT_WINDOW 条里**出口级**错误的数量
    - 连续检测：从最新条往前数，连续出口级失败的个数（防止 success 稀释 error）
    任一超过阈值就触发切换。

    两类错误口径必须与 `count_warp_blocked_since` 一致：只有 403/429 是出口
    问题。上游 402（"Endpoint is unavailable"）与请求超长（"maximum context
    length"）都不是 —— 前者实测 108/108 被 combo 的下一模型在 3 秒内兜住。

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
            return ErrorWindow(0, 0, [], 0, 0.0)

        recent = [dict(r) for r in rows]
        total = len(recent)
        # errors 只数**出口级**拦截，不是「所有非 success」。WHY：调用方
        # `_record_active_ip_success` 用 total-errors 当成功数，策略模式用它算
        # 错误率。把上游 402（longcat 独占 77/80 条，108/108 由 combo 下一模型
        # 3s 内兜住）和请求超长算进来，等于让一个健康出口的成功流量凭空变少，
        # 并让策略模式误切压力档。这两类换出口都解决不了。
        errors = 0
        consecutive_errors = 0
        for r in recent:
            if r.get("status") == "success":
                continue
            if not _classify_egress_error(r.get("data")):
                continue  # other（402/超长）：不计错误、不断连，同 count_warp_blocked_since
            errors += 1
        # 连续失败要从**最新一条**往前数：recent 是倒序（新→旧），成功即断。
        for r in recent:
            if r.get("status") == "success":
                break
            if _classify_egress_error(r.get("data")):
                consecutive_errors += 1

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
            err_models = set(r.get("model", "?") for r in recent
                             if r.get("status") != "success"
                             and _classify_egress_error(r.get("data")))
            other = sum(1 for r in recent if r.get("status") != "success"
                        and not _classify_egress_error(r.get("data")))
            tail = f"（另 {other} 条非出口错误不计）" if other else ""
            log(f"  [OpenCode Free] {errors}/{total} 出口级错误{tail} | 连续失败={consecutive_errors} | 403占比={reject_ratio:.0%} | 失败模型: {', '.join(sorted(err_models))}")

        return ErrorWindow(total, errors, recent, consecutive_errors, reject_ratio)
    except Exception as e:
        log(f"✗ 读 SQLite 失败: {e}")
        return ErrorWindow(0, 0, [], 0, 0.0)

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
        # 构建新内容前再读一次，把窗口内追加的记录合并（避免丢切换记录）
        seen = {(d["ts"], d["node"]) for d in kept}
        for d in _read(target):
            key = (d["ts"], d["node"])
            if key not in seen and d.get("ts", 0) >= keep_after:
                kept.append(d)
                seen.add(key)
        kept.sort(key=lambda x: x["ts"])
        _atomic_write_text(
            target,
            "".join(json.dumps(d) + "\n" for d in kept),
        )
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


def _probe_9router(path: str, timeout: float) -> int | None:
    """GET 9router 的一个端点，返回 HTTP 状态码；传输层失败返回 None。

    统一走 CFG["9router"]["base"]：9router 只有一个监听口，写死端口会在
    换部署（或原本就写错）时让健康检查永远失败。
    """
    try:
        r = requests.get(
            f"{CFG['9router']['base'].rstrip('/')}{path}",
            headers={"Authorization": "Bearer 9router-local"},
            timeout=timeout,
        )
        return r.status_code
    except requests.RequestException as e:
        log(f"⚠ 9router {path} 不可达: {e}")
        return None


def check_9router_health(timeout: float = 5.0) -> bool:
    """9router 是否可用：dashboard 可达 + v1 API 未返回 5xx。

    `/v1/models` 首次冷启动实测约 10s，固定 5s 超时会把它误判成不可达，
    因此单独给它更宽的预算。401/429 说明端点活着，不算故障。
    """
    status = _probe_9router("/dashboard", timeout)
    if status is None or status >= 500:
        if status is not None:
            log(f"⚠ 9router dashboard 返回 {status}")
        return False

    status = _probe_9router("/v1/models", max(timeout, 15.0))
    if status is not None and status >= 500:
        log(f"⚠ 9router v1 models API 返回 {status}")
        return False
    return status is not None


def check_9router_outbound_proxy() -> bool:
    """Ensure OpenCode Free proxyPool + outboundProxyEnabled correct. Skip during fallback."""
    with _state_lock:
        if _ALL_DEAD_FALLBACK:
            return True  # 已降级到 okz，不覆盖
    expected_url = _expected_proxy_url()
    changed = False
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

            # 1. 检查 proxyPool 当前代理地址
            pool_row = c.execute('SELECT data FROM proxyPools WHERE id=?', (pool_id,)).fetchone()
            if not pool_row:
                log(f"⚠ proxyPool {pool_id} 不存在")
                return False

            pool = json.loads(pool_row[0])
            url = pool.get("proxyUrl", "")
            if url != expected_url:
                log(f"⚠ opencode proxyPool 异常 (url={url})，修复为 {expected_url}...")
                pool["proxyUrl"] = expected_url
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
                log(f"✓ 代理配置已修复 -> url={expected_url}, outboundProxyEnabled=True")
            else:
                return True
    except Exception as e:
        log(f"✗ 检查代理配置失败: {e}")
        return False

    # 健康检查放在 _db_lock 之外：它是一次网络请求（最长 15s），持锁会挡住
    # 主循环的错误查询。配置已提交，检查失败只记日志、不回滚。
    if changed and not check_9router_health():
        log("⚠ 9router 配置修复后健康检查失败")
    return True

# ============= Sing-box 操作 =============
def singbox_api(path: str, method: str = "GET", body: dict | None = None,
                timeout: int = 3, silent: bool = False) -> tuple[int, dict | None]:
    """通用 sing-box Clash API 调用

    传输层失败返回 `(0, None)`（历史契约：调用方把 0 当「不可达」，与
    HTTP 4xx/5xx 的状态码区分）。响应体非 JSON 时返回 `(status, None)`。
    """
    try:
        status, data = _CLASH.request_json(
            method, path, body=body, timeout=timeout,
        )
    except Exception as e:
        if not silent:
            log(f"✗ Clash API 异常: {e}")
        # 已在上面记过具体原因，别让同一次失败再落一条「连接失败」。
        return 0, None
    if status is None:
        if not silent:
            log(f"✗ Clash API 连接失败: {CFG['singbox']['clash_api']}{path}")
        return 0, None
    # 历史契约：只有 2xx（200/204）才返回响应体。4xx/5xx 的 JSON 错误页
    # 不能被当成业务数据（调用方会读 data["now"] 之类）。
    if status in (200, 204):
        return status, data
    return status, None

def get_current_proxy() -> str | None:
    r, data = singbox_api(f'/proxies/{CFG["singbox"]["selector"]}')
    return data.get("now") if data else None

def get_all_proxy_nodes() -> list[str]:
    r, data = singbox_api(f'/proxies/{CFG["singbox"]["selector"]}')
    return data.get("all", []) if data else []

def _record_egress(node: str) -> None:
    """切换成功后记录 (ts,node)，供 per-egress 错误率归因。append-only 原子写。"""
    try:
        _atomic_append_line(_egress_log_path(), {"ts": _now_ts(), "node": node})
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
        _publish_clash_snapshot(CFG["singbox"]["selector"], {"now": new_node})
        _record_egress(new_node)
        global _last_successful_switch_ts
        _last_successful_switch_ts = _now_ts()
        return True
    else:
        log(f"✗ 切代理验证失败 ({new_node}): PUT 返回 {r} 但实际 now={actual}")
        return False

# ============= WARP 主备隧道（opencode 专用出口） =============
def _observe_9router() -> bool:
    """9router 流量观测开关。false = blind 模式：换线只按时间判据。

    每次现读而不是 import 时固化：单测用 monkeypatch.setitem 开关即可，
    不依赖 daemon 进程重启。
    """
    return bool(CFG.get("traffic", {}).get("observe_9router", True))


def _warp() -> dict:
    return CFG["warp"]


def _warp_member(tag: str | None) -> dict | None:
    return next((m for m in _warp()["members"] if m["tag"] == tag), None)


def active_warp_tag() -> str | None:
    """opencode-out 当前指向的 WARP 成员 tag；指向轮换池 proxy 时返回 None。

    优先读后台快照（见 `_clash_snapshot_loop`）：selector 只由本进程改，最多
    晚 clash_poll_sec(0.5s) 看到，而每次现查都是一发 HTTP。快照未建立时
    （启动早期）才直接查。
    """
    snap = _clash_snapshot(_warp()["selector"])
    if snap is not None:
        now = snap.get("now")
    else:
        _, data = singbox_api(f'/proxies/{_warp()["selector"]}', silent=True)
        now = data.get("now") if data else None
    return now if _warp_member(now) else None


def opencode_out_on_warp() -> bool:
    return active_warp_tag() is not None


# Clash selector 快照：后台线程每 clash_poll_sec(0.5s) 把两个 selector 各刷一次，
# 判定全读快照。WHY：主循环不允许阻塞 —— 每次 `singbox_api` 都是一发 HTTP（超时
# 3s），放在 20Hz 的循环里既烧 CPU，又会在 sing-box 卡住时把错误检测整段钉死。
#
# 按 selector 名分开存：`proxy`（轮换池）与 `opencode-out`（WARP 出口）是两个
# 独立的 selector，共用一个槽会让池轮换把 WARP 出口判定带偏。
# 缺 key = 该 selector 还没刷到过（启动早期），此时退回直接查询。
_CLASH_SNAP: dict[str, dict] = {}
_CLASH_SNAP_LOCK = threading.Lock()


def _clash_snapshot(selector: str) -> dict | None:
    with _CLASH_SNAP_LOCK:
        return _CLASH_SNAP.get(selector)


def _publish_clash_snapshot(selector: str, data: dict) -> None:
    """写入某个 selector 的快照。换线成功后立即调用，省得判定最多晚 clash_poll_sec。

    只覆盖 data 里给了的字段：`_switch_warp` 只带 now（它不关心 all），
    刷新线程带 all+now，谁先到都不该把对方那份清空。
    """
    with _CLASH_SNAP_LOCK:
        cur = dict(_CLASH_SNAP.get(selector) or {})
        for k in ("all", "now"):
            if data.get(k) is not None:
                cur[k] = data[k]
        cur.setdefault("all", [])
        _CLASH_SNAP[selector] = cur


def _pool_snapshot() -> tuple[list[str], str | None]:
    """轮换池快照 (nodes, current)。主循环用它代替现查 Clash API。"""
    snap = _clash_snapshot(CFG["singbox"]["selector"]) or {}
    return snap.get("all") or [], snap.get("now")


def _clash_snapshot_loop() -> None:
    while True:
        for sel in (CFG["singbox"]["selector"], _warp()["selector"]):
            try:
                _, data = singbox_api(f'/proxies/{sel}', silent=True)
                if data:
                    _publish_clash_snapshot(sel, data)
            except Exception as e:
                log(f"✗ 刷新 Clash 快照异常 ({sel}): {e}")
        time.sleep(CFG["intervals"]["clash_poll_sec"])


def _start_clash_snapshot_loop() -> None:
    """先同步取一次把两个 selector 都填上，再起刷新线程。

    不先填的话，启动后到第一轮刷新之间快照是空的，`active_warp_tag()` 和
    主循环的节点列表都会退回直接查询 —— 那是每 tick 一发 HTTP（超时 3s），
    20Hz 的主循环会原地卡住。这一次同步查询最多 6s，发生在进主循环之前。

    查不到也要写一条空快照：缺 key 只表示「还没初始化过」。否则 sing-box 挂掉时
    快照永远是空的，主循环每 tick 都会退回那个同步查询。
    """
    for sel in (CFG["singbox"]["selector"], _warp()["selector"]):
        _, data = singbox_api(f'/proxies/{sel}', silent=True)
        _publish_clash_snapshot(sel, data or {})
    threading.Thread(target=_clash_snapshot_loop, daemon=True,
                     name="clash-snapshot").start()


def _egress_label(current_proxy: str | None = None) -> str:
    """当前 opencode 出口的可读标签，用于健康行日志。

    形如 `WARP:WARP-CF-B(2a09:bac5:...)` 或 `PROXY:pq.aws50...`。
    出口种类一眼可辨 —— 排查时要先知道流量走的是哪条路，再去查该路的错误率。

    只读已知状态，不发探测：健康行每 10s 打一条，走 `warp_egress_ip` 的 SOCKS
    探测最长会阻塞主循环 12s。IP 取自换线时记下的 `_ACTIVE_WARP_IP`。
    """
    tag = active_warp_tag()
    if tag:
        return f"WARP:{tag}({_ACTIVE_WARP_IP or '?'})"
    return f"PROXY:{current_proxy or '?'}"


# 请求开始时刻 = 完成时刻 − 延迟。9router 在请求结束时才写行，而换线不掐断
# 在途请求（interrupt_exist_connections=false），延迟 p50 8.8s / p90 27.9s 与换线
# 间隔 p50 8s 同量级 —— 上一出口的 429 会带着新时间戳落进新窗口，被当成新出口
# 的错再触发一次换线。重放 34 次真实触发：按完成时刻 33 次命中，按开始时刻 20 次，
# 14 次纯误触发（都切上去就报错的那 1 条 429；该出口实际一条错都没收到）。

_START_TS_SQL = ("(julianday(timestamp) - 2440587.5) * 86400.0 - "
                 "COALESCE(CAST(CASE WHEN json_valid(data) "
                 "THEN json_extract(data, '$.latency.total') END AS REAL), 0) / 1000.0")


def count_warp_blocked_since(ts: float, limit: int | None = None) -> dict[str, int]:
    """统计**开始于** ts 之后、opencode 最近 limit 条（默认 RECENT_WINDOW）里的两类出口级拦截。

    - abuse403：IP 信誉拦截（"abusive traffic from this IP"）
    - rate429：IP 级限流（FreeUsageLimitError "Rate limit exceeded"）
    - ok：窗口内的成功数，供调用方判断当前出口是否还在服务流量
    - other_err：与出口无关的失败（上游 402、请求超长），仅供诊断
    - consec_err：窗口内从最新一条往前数连续**出口级**失败数，0 表示最新一条成功

    WHY 429 也算：两者都按出口 IP 判定，换出口即拿到新配额/新信誉。

    WHY consec_err/other_err 必须分开：换线判据只能建立在「这个出口有问题」上。
    实测 605 条错误里 18 条 "maximum context length"（请求本身太长）、80 条
    "Endpoint is unavailable"（上游 402，longcat 独占 77 条，108/108 由 combo
    下一模型在 3s 内兜住）。把它们算进连错会让两条无关错误就凑出 full_err，
    把一条干净的出口切走 —— 换线既不解 402 也不解超长，纯属误切。

    WHY 默认窗口至少 3 条（recent_window=2 不足）：换线门控要 `consec_err ≥ 3`，
    而连错数最多只能数到窗口长度 —— 2 条窗口下门控恒不成立，`full_err`（整窗
    全错）就成了唯一放行条件，两条错误就能把干净出口切走。取 max(RECENT_WINDOW, 3)
    让门控按原意工作；不动 config 数值（recent_window 仍管策略侧窗口）。
    回切判据传 limit=200——它要数的是「落池以来累计拦截 ≥5」，受默认窗口限制时
    最多只能数到 2 条，14:47 落池后 30 分钟里 1590 条 429 却永不回切 WARP 的
    根因即在此（见 _maybe_failback_to_warp）。
    """
    cutoff = float(ts) if ts > 0 else 0.0
    window = limit if limit is not None else max(int(RECENT_WINDOW), 3)
    # 索引预筛：开始时刻只会早于完成时刻，所以完成时刻早于 cutoff 的行必然不属于
    # 本窗口，可以直接用 idx_rd_provider(provider, timestamp) 挡掉。julianday()
    # 本身用不上索引，但外围下推之后全表只剩下 60 行左右要算。
    raw_floor = (datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                 if cutoff > 0 else "")

    try:
        with _db_lock:
            rows = _get_db().execute(f"""
                SELECT status, data FROM requestDetails
                WHERE provider = ? AND timestamp > ? AND {_START_TS_SQL} > ?
                ORDER BY {_START_TS_SQL} DESC LIMIT ?
            """, (OPENCODE_FREE_PROVIDER, raw_floor, cutoff, window)).fetchall()
    except Exception as e:
        log(f"✗ 统计 WARP 拦截失败: {e}")
        return {"abuse403": 0, "rate429": 0, "ok": 0, "other_err": 0,
                "consec_err": 0, "full_err": False}
    counts = {"abuse403": 0, "rate429": 0, "ok": 0, "other_err": 0,
              "consec_err": 0, "full_err": False}
    kinds = []
    for r in rows:
        if r["status"] == "success":
            counts["ok"] += 1
            kinds.append("ok")
            continue
        # 直接子串匹配即可，无需解析嵌套 JSON：生产行里 error 是**字符串化**的
        # 内层 JSON，其转义引号间的关键词在原始文本中逐字可见。
        # 实测 opencode 全表 1958 行：朴素子串与「解析到 response.error 再匹配」
        # 判定完全一致（0 行差异）。
        kind = _classify_egress_error(r["data"])
        kinds.append(kind or "other")
        if kind:
            counts[kind] += 1
        else:
            counts["other_err"] += 1
    # rows 按开始时刻倒序：从最新一条往前数连续**出口级**失败。换线门用的语义是
    # 「最近 RECENT_WINDOW 条里连续几条说明这个出口不能用了」，与回切的累计计数
    # 无关——传 limit 的调用方（回切）不用 consec_err/full_err。
    # other（402/超长）**不截断**也不累加：它既不能证明出口坏了，也不是成功，
    # 跳过继续往前数才能让一条夹在中间的 402 不把两侧的 429 割断。
    for kind in kinds:
        if kind == "ok":
            break
        if kind != "other":
            counts["consec_err"] += 1
    # 整窗无一成功 **且全窗都是出口级错误**才算风暴。只有 other 错误时不算：
    # 那说明请求本身或上游模型有问题，换出口解决不了。
    counts["full_err"] = (len(rows) >= window and counts["ok"] == 0
                          and counts["other_err"] == 0)
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


def count_segment_requests(ts: float) -> tuple[int, int]:
    """起点 ts 之后、opencode 的成功数与出口级错误数（无上限）。

    与 `count_warp_blocked_since` 的区别：那个是**滑动窗口**（看最近几条），
    这个是**整段累计**（看这条出口从上线到现在服务了多少条）。单 IP 承载量
    只能用后者统计 —— 窗口只有 2 条，用它算「每个 IP 能跑多少条」恒得 0~1。

    出口级错误只算 403/429：402 与超长与出口无关，算进去会把误切窗口的
    承载量系统性压低（正是用户指出的「只跑了几条不能作为参考」）。

    按请求**开始**时刻归属（`_START_TS_SQL`），不按完成时刻：9router 在请求
    结束时才写行，换线又不掐断在途请求 —— 实测 38.4% 的请求生命周期跨越一次
    换线。按完成时刻算，上一段的请求会被记到新 IP 头上，段样本被污染。
    """
    if ts <= 0:
        return 0, 0
    cutoff = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    raw_floor = cutoff
    try:
        with _db_lock:
            rows = _get_db().execute(f"""
                SELECT status, data FROM requestDetails
                WHERE provider = ? AND timestamp > ? AND {_START_TS_SQL} > ?
            """, (OPENCODE_FREE_PROVIDER, raw_floor, float(ts))).fetchall()
    except Exception as e:
        log(f"✗ 统计段内请求失败: {e}")
        return 0, 0
    ok = blocked = 0
    for r in rows:
        if r["status"] == "success":
            ok += 1
        elif _classify_egress_error(r["data"]):
            blocked += 1
    return ok, blocked


def count_dsv4_ok_since(ts: float) -> int:
    """ts 之后 deepseek-v4.1-flash-free 的**成功**调用数，即已消耗的配额格数。

    WHY 用 usageHistory 而不是 requestDetails：后者只有 2000 行滚动窗口，实测
    22.7 分钟就写满，60 秒窗口在高峰期会被截断；且它按完成时刻写，在途请求漏计。

    WHY 数成功而不是数请求：配额按成功的 dsv4 调用扣，429 不是请求条数的函数。
    按失败的请求数计数会把「被拒的请求」也算成消耗，429 风暴里反而切得更勤。

    WHY 强制走 idx_uh_ts：指定的 provider/model 组合先过滤要扫 18 万行（实测
    250ms），而本函数在主循环里每 5 秒调一次。走时间索引后 0.1ms。

    ponytail: 窗口起点是段起点，不按出口 IP 拆窗 —— 同一窗口内若发生换线，
    旧出口的消耗也会算进新出口。上限是单段内换线次数（实测主动段 60s 只切
    一次、429 段切完即重建窗口），偏差 <10 格（阈值 150 的 7%）。升级路径：
    usageHistory 没有出口 IP 列，要拆窗得 JOIN `.warp_ip_history.jsonl`。
    """
    if ts <= 0:
        return 0
    # cutoff 必须和 rows 同形（毫秒 + Z）：列里 100% 是 '...SS.mmmZ'（实测
    # 226211/226211），比较是字符串比较。写成 '...SS' 时，同一秒内任意
    # '.001Z'~'.999Z' 都 > 它（'25.' 按字符串大于 '25'），段起点那一秒里
    # 换线**之前**的调用会被算进新段 —— 方向是多算，且日志看不出来。
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    cutoff = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    try:
        with _db_lock:
            row = _get_db().execute(
                """SELECT count(*) FROM usageHistory INDEXED BY idx_uh_ts
                   WHERE timestamp > ? AND provider = ? AND model = ?
                     AND status = 'ok'""",
                (cutoff, OPENCODE_FREE_PROVIDER,
                 _warp()["proactive_quota_model"])).fetchone()
    except sqlite3.Error as e:
        # 只吞数据库错误。配置键缺失（KeyError）是编程错误，吞掉会让 requests
        # 模式永久返回 0 —— 换线判据静默失效，日志之外无从察觉。
        log(f"✗ 统计 dsv4 成功数失败: {e}")
        return 0
    return int(row[0] if row else 0)


def _current_mode() -> tuple[str, str | None]:
    """(模式, 当前 tag)：warp / proxy / unknown。

    直查 sing-box，查不到才退回 0.5s 快照：快照固定滞后，连切两次时会漏打一次
    模式日志，故不能优先读它；但看门狗重启前 sing-box 本就是死的，快照里的最后
    已知模式是唯一能用来恢复出口的信息。两者都没有才是 unknown —— **不能并进
    proxy**，否则一次重启就把 WARP 静默换成轮换池。
    """
    _, data = singbox_api(f'/proxies/{_warp()["selector"]}', silent=True)
    if data:
        now = data.get("now")
    else:
        snap = _clash_snapshot(_warp()["selector"])
        if snap is None:
            return ("unknown", None)
        now = snap.get("now")
    return ("warp", now) if _warp_member(now) else ("proxy", None)


def _switch_warp(tag: str, interrupt: bool = False) -> bool:
    """翻转 opencode-out selector。interrupt=False 时在途请求不断。

    interrupt=True 用于错误触发的换线：9router / opencode 用 HTTP/2 长连接，
    只改 selector 不会迁移已建立的连接，旧连接继续走烧掉的出口 —— 实测换线
    后 429 还要再报 2 分钟才恢复，期间探针（新连接）却全是 OK。换线时一并
    请求 sing-box 断开旧连接，下一次请求才会真正落到新线。

    成功后立即把快照的 now 改写为 tag：判定（尤其是补刷前的「这条线是不是
    已经成为出口」）读的就是快照，晚 0.5s 看到就可能重启一条正在服务的隧道。

    模式切换（warp <-> proxy）在这里打日志：所有出口变更都经过这个函数，
    在调用方各打一条会漏掉 sing-box 重启后的线路恢复那条路径。

    before 由 `_current_mode()` 直查 sing-box（只在查不到时才退回快照）：快照固定
    滞后 0.5s，连切两次时会漏打一次模式日志。
    """
    before = _current_mode()
    body = {"name": tag}
    if interrupt:
        body["interrupt_exist_connections"] = True
    r, _ = singbox_api(f'/proxies/{_warp()["selector"]}', method="PUT", body=body)
    if r not in (200, 204):
        log(f"✗ 切换 WARP 线路失败 ({tag}): status={r}")
        return False
    _publish_clash_snapshot(_warp()["selector"], {"now": tag})
    after = ("warp", tag) if _warp_member(tag) else ("proxy", None)
    if after[0] != before[0]:
        bl = before[1] or ("轮换池" if before[0] == "proxy" else "未知")
        log(f"⇄ 出口模式切换: {before[0]} -> {after[0]}"
            f"（{bl} -> {after[1] or '轮换池'}）")
    # 用户口径：刷新次数从「离开主渠道」起算到 OK。切到别处即开始计时，
    # 切回这条线（成为当前出口）即结束并把计数清零 —— 下次离渠重新从 0 数。
    _track_offline_refresh(before[1], after[1] if after[0] == "warp" else None)
    if after[0] == "warp":
        global _LAST_WARP_TAG
        _LAST_WARP_TAG = tag
    return True


def _reclaim_foreign_pool(now: float | None = None) -> bool:
    """把外部写入的 opencode-out=proxy 拉回 WARP。

    禁用落池后，selector 只应由本进程改。但测试进程、手工 curl、其他 agent
    都能直连 Clash API 静默翻转（实测 06:14/06:16/06:26/07:02 各一次，都没有
    `⇄ 出口模式切换` 日志，`落池已禁用` 计数为 0）。落池即断流，所以每个主
    循环 tick 校验一次；发现即切回最近使用的 WARP 线。
    """
    global _LAST_POOL_RECLAIM_TS
    if _warp().get("proxy_fallback_enabled", False):
        return False
    now = time.time() if now is None else now
    if now - _LAST_POOL_RECLAIM_TS < _POOL_RECLAIM_MIN_GAP_SEC:
        return False
    mode, _tag = _current_mode()
    if mode != "proxy":
        return False
    target = _LAST_WARP_TAG if _warp_member(_LAST_WARP_TAG) else _warp()["members"][0]["tag"]
    _LAST_POOL_RECLAIM_TS = now
    log(f"⚠ 检测到外部落池（opencode-out=proxy），回收至 {target}")
    return _switch_warp(target)


def _restart_service(service: str, tag: str | None = None) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", service],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            log(f"✗ 重启 {service} 失败: {r.stderr.strip()[:200]}")
        elif tag:
            # 用户口径：刷了几次从「离开主渠道」起算，每次真正重启算一次。
            _WARP_REFRESH_COUNT[tag] = _WARP_REFRESH_COUNT.get(tag, 0) + 1
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


# 判「请求可能仍在进行」的连接年龄上限。首 token 延迟实测 p50=1.7s / p90=52.6s /
# max=532.7s（N=83）；取 120s 覆盖 p90 且留一倍余量。上限是必要的：9router 会复用
# 连接，实测活跃线上有年龄中位 476s 的长期连接，没有它就会把整条线钉死在「忙」上。
_FIRST_BYTE_WAIT_MAX_SEC = 120.0


# 连接表快照的复用窗口。每个判闲调用都要读整张 /connections，20 条线各自
# 复测时按线各查一次就是把 daemon 变成 curl 风暴（实测每线每秒一次，load
# 3.4）。1s 内复用同一份快照：判闲本身要睡窗口，快照旧 1s 不影响结论。
_CONN_SNAPSHOT_TTL_SEC = 1.0
_conn_snapshot: tuple[float, dict | None] = (0.0, None)
_conn_snapshot_lock = threading.Lock()


def _connections_json() -> dict | None:
    """Clash 连接表快照；失败也缓存，避免 Clash 挂掉时每个等待者各起一次 curl。"""
    global _conn_snapshot
    with _conn_snapshot_lock:
        ts, data = _conn_snapshot
        if time.time() - ts < _CONN_SNAPSHOT_TTL_SEC:
            return data
        # 走 HTTP 客户端而不是 curl：token 放 argv 会被 `ps` 看到（同一泄漏面
        # 在订阅刷新处已用 0600 临时文件规避）。读取失败仍缓存 None，否则
        # Clash 挂掉时每个等待者都会各发一次请求。
        try:
            data = _CLASH.connections(timeout=5)
        except Exception as e:
            data = None
            log_health(f"⚠ 读取 Clash 连接表失败: {e}")
        # 时间戳取请求**完成**时刻：请求本身最长 5s，若用开始时刻入库，
        # 返回时已超过 1s TTL，每个等待者都会立刻重发（串行风暴）。
        _conn_snapshot = (time.time(), data)
        return data


def _warp_line_stats(tag: str) -> tuple[int, int, int] | None:
    """该 tag 上的 (累计字节, 年轻连接数, 总连接数)；查询失败返回 None。

    「可能仍在进行」= 连接年龄在 `_FIRST_BYTE_WAIT_MAX_SEC` 内。**不要求
    download==0**：旧判据加了这条件，把「已收到部分 token、正等下一个」的
    连接全部漏掉 —— 12:22 实测被重启掐断的 4 条连接各已收 ~1MB、年龄
    94~116s，12s 窗口内零增长，恰好同时命中两个漏洞。
    """
    data = _connections_json()
    if data is None:
        return None
    mine = [c for c in (data.get("connections") or [])
            if tag in (c.get("chains") or [])]
    now = time.time()
    waiting = 0
    for c in mine:
        try:
            started = datetime.fromisoformat(c["start"]).timestamp()
        except Exception:
            continue
        if now - started < _FIRST_BYTE_WAIT_MAX_SEC:
            waiting += 1
    # 总连接数是「零连接快速判闲」的依据：一条连接都没有的线不可能被重启掐断，
    # 不必再付一个观察窗。实测 17 条备线常驻只有 2 条挂连接，88% 的探测是在
    # 空壳线上白等 12s。
    return (sum((c.get("upload") or 0) + (c.get("download") or 0)
                for c in mine), waiting, len(mine))


# 错误换线路径的判闲观察窗。12s（`busy_window_sec`）是给后台补刷的：那条
# 路径可以慢慢等。换线路径上每多等一秒，坏出口就多服务一秒失败请求 ——
# 按候选逐条 12s 时最坏 ~120s 才切走（codex review 指出）。2s 是 HEAD 的
# 既有默认，这里把它显式化，不再依赖参数默认值。
_ERROR_PATH_BUSY_WINDOW_SEC = 2.0

# 被切下的主渠道排空时的判闲观察窗。必须比 12s 长得多：流式响应在 token
# 之间会静默，实测 p99=8.1s / max=16.1s（N=1460），12s 窗口有 0.3% 落在
# 静默里；而 12:25 实测 4 条各收 ~1MB、年龄 94~116s 的连接在 12s 零增长后
# 被重启掐断。取 30s：覆盖静默分布的 max 并留近一倍余量。
#
# WHY 只有这条路径付长窗：候选线的重启不需要窗口（它不在服务流量，等端口
# 就绪即可），错误换线路径每多等一秒坏出口就多服务一秒失败请求。只有
# 「刚被切下、仍在排空在途请求」的线才既需要等、又等得起。
_DRAIN_BUSY_WINDOW_SEC = 30.0


def warp_line_busy(tag: str, window: float | None = None) -> bool | None:
    """该线此刻是否仍在服务请求；查不到返回 None（调用方保守处理）。

    窗口默认取 `busy_window_sec`(12s)，那是给后台补刷 / 脏线守护的。错误
    换线路径必须显式传短窗（`_ERROR_PATH_BUSY_WINDOW_SEC`），否则每个候选
    都要先睡满 12s。

    三档判定，不等窗口就出结论的两档排在前面：
      0. 零连接 —— 立即判闲；
      1. 有年龄在 `_FIRST_BYTE_WAIT_MAX_SEC` 内的连接 —— 立即判忙；
      2. 只剩老连接 —— 付一个观察窗比字节增量。

    WHY 不先睡窗口：调用方是 1s 一轮的重测循环，睡窗口等于把重测间隔拖到
    窗口长度（排空路径 30s）。实测 WARP-CF-19 因此每 90s 才测一次，那条线
    在等待期一直占着旧 IP，备线池随之变窄。年轻连接本来就不看字节（见下），
    老连接才需要窗口。

    WHY 判据 1 不可省：流式响应会长时间零字节。stall 实测覆盖两种形态：
      a) 首 token 之前完全不传（TTFT p50=1.7s / p90=52.6s / max=532.7s，N=83）；
      b) 收到部分 token 后等下一个（12:22 实测被重启掐断的 4 条连接已各收
         ~1MB、年龄 94~116s，但 12s 窗口内零增长）。
    只看字节增量时两者都与空闲线无异，判闲后重启，客户端收到 upstream
    connection lost。旧判据只抓 (a)（要求 download==0），(b) 全部漏检。

    WHY 判据 1 不能退化成「连接数 > 0」：9router 复用连接，实测活跃线上有年龄
    中位 476s 的长期连接（WARP-CF 90 个采样里 58 个有连接、最小年龄全是 476s+）。
    按连接数判忙会让这些线永远刷不到新鲜 IP。用年龄上限把它们排除，只留
    「建立不久、请求可能仍在进行」的那部分；排除掉的老连接才轮到判据 2。
    """
    if window is None:
        window = float(_warp().get("busy_window_sec", 12.0))
    s0 = _warp_line_stats(tag)
    if s0 is None:
        return None
    # 零连接＝不可能掐断任何请求，直接判闲。实测 19 条备线常驻只有 2~4 条挂
    # 连接，其余全是空壳 —— 挨个等 12s 把一个 5s 一轮的守护拖成 60s+ 一轮，
    # 而出口 IP 存活中位只有 12.5s，脏线因此滞留远超一个 IP 的寿命。
    if s0[2] == 0:
        return False
    # 有年轻连接＝请求还在生命周期里，直接判忙，不必等观察窗。这一支本来就不
    # 看字节：12:22 被掐断的 4 条连接各已收 ~1MB、年龄 94~116s，落在本支里，
    # 窗口内零增长照样判忙。等窗口唯一的效果是把调用方的重测间隔从 1s 拖到
    # 30s —— 实测 WARP-CF-19 因此 90s 才重测一次，期间白占着备线名额和旧 IP。
    if s0[1] > 0:
        return True
    # 走到这里只剩「有连接但全部超过 120s」：它们是被 9router 复用的老连接，
    # 多半空闲。字节增速才是判据，此时才值得付一个观察窗。
    time.sleep(window)
    s1 = _warp_line_stats(tag)
    if s1 is None:
        return None
    b0 = s0[0]
    b1, conns = s1[0], s1[1]
    return b1 > b0 or conns > 0


def _wait_warp_idle(member: dict, timeout_sec: float | None = None,
                    window: float | None = None) -> bool:
    """等到该线不再传输数据；超时返回 False（调用方自行决定是否强刷）。

    默认用 idle_wait_sec(90s) —— 补刷路径可以慢慢等，它不阻塞流量。错误换线
    路径必须同时传更短的超时与更短的观察窗：那里每多等一秒，坏出口就多服务
    一秒失败请求。
    """
    deadline = time.time() + (timeout_sec if timeout_sec is not None
                              else _warp()["idle_wait_sec"])
    while time.time() < deadline:
        if warp_line_busy(member["tag"], window) is False:
            return True
        time.sleep(1)
    return False


def warp_egress_ip(port: int, timeout_sec: float = 20.0) -> str | None:
    """经 usque SOCKS 查 Cloudflare trace；隧道不通或 warp=off 返回 None。

    Cloudflare 的 WARP 会话地址重连即换（同一 IP 视为重连未生效）。
    实测 B/C 两条隧道偶发 >8s 响应（触发超时），20s 内稳定在 1.2-1.4s。
    这是**已稳定的线**该给的耐心，默认值即按它取。

    刚 `systemctl restart` 完的隧道不是这个形态：要么一两秒就通，要么根本没
    起来，多等 18s 不会变好，只把换线线程钉在原处。所以刷新路径传短超时
    （见 `_refresh_idle_warp`），把单次尝试的最坏耗时从 32s 压到 19s。
    """
    return probe_egress(
        socks_port=port,
        socks_host=_warp()["socks_host"],
        urls=(TRACE_URL,),
        timeout=timeout_sec,
        require_warp=True,
    ).ip


def _probe_refresh_egress(port: int) -> str | None:
    """刷新路径取出口：轮询到隧道真正通为止，而不是两次预算后就放弃。

    WHY 不再用「短预算两次」：实测同一批生产 device 配置、无守护进程干扰下，
    从进程启动到出口可用的时间是 5.0/7.6/7.8/7.9/7.9/8.3s（n=6，全部成功），
    而复刻原「7s 再 12s」两次探测的对照里 6 条有 5 条首探超时、全靠第二探才
    拿到 —— 首探预算压在可用时间中位数上，等于抛硬币。抛输的那次被判失败，
    调用方重启隧道、计时归零，隧道于是永远停在冷启动段：实测 1 小时 763 次
    systemd Started（20 条线各约 94s 一次），每次都在它即将可用时被掐掉。

    WHY 轮询而不是把单次预算调大：隧道可用后首查只要 1.5~2.3s，慢的是「还没
    可用」这一步。单次大预算会让已坏的隧道白占线程一整个预算；轮询则每次只
    花 5s，任一跳变好立刻返回。上限沿用 `ready_timeout_sec` 的量级乘以本次
    最多尝试次数，避免真的无限等。
    """
    w = _warp()
    per_try = w["refresh_probe_timeout_sec"]
    deadline = time.time() + w["ready_timeout_sec"] * w["max_attempts"]
    while True:
        ip = warp_egress_ip(port, timeout_sec=per_try)
        if ip:
            return ip
        if time.time() >= deadline:
            return None
        time.sleep(1)


def _set_warp_line_ip(tag: str, ip: str) -> None:
    with _WARP_LINE_LOCK:
        st = _WARP_LINE_STATE.setdefault(tag, {})
        st["ip"] = ip
        st["ip_ts"] = time.time()


def _warp_line_ip(tag: str) -> str | None:
    with _WARP_LINE_LOCK:
        return _WARP_LINE_STATE.get(tag, {}).get("ip")


def _warp_line_ip_ts(tag: str) -> float:
    """该线出口 IP 上次被实际验证的时刻；0 = 未验证过。

    验证地址一致性：只有当前缓存的 IP 与验收记录的 IP 一致时，才返回 ip_ts。
    验收记录在 _WARP_LAST_ACCEPT[tag] = (ts, ip)。
    """
    with _WARP_LINE_LOCK:
        st = _WARP_LINE_STATE.get(tag, {})
        acc = _WARP_LAST_ACCEPT.get(tag)
        if acc and st.get("ip") == acc[1]:
            return float(st.get("ip_ts") or 0.0)
        return 0.0


def _invalidate_warp_line_ip(tag: str) -> None:
    """隧道重启后旧出口地址必然失效，清掉它，避免快速路径继续采信。

    只清时间戳不够：`ip` 留着但 `ip_ts=0` 会让读方把它当成「刚验证过」的反面
    （时间戳为 0 时 `now - ts` 极大，天然被 TTL 挡掉）。两个都清更直白。
    """
    with _WARP_LINE_CV:
        st = _WARP_LINE_STATE.get(tag)
        if st:
            st.pop("ip", None)
            st.pop("ip_ts", None)
            st["last_ok_ts"] = 0.0
            st["last_ok_ip"] = None
        _WARP_LINE_CV.notify_all()


# ---------------------------------------------------------------------------
# 出口状态表：唯一探测器写，换线路径只读 + 等待。
# 旧字段（ip/ip_ts）保留兼容旧守护/测试；新路径只信 last_ok_ip/last_ok_ts。
# ---------------------------------------------------------------------------
_LINE_DEFAULTS = {
    "ip": None,
    "ip_ts": 0.0,
    "last_ok_ts": 0.0,
    "last_ok_ip": None,
    "last_verdict": "",
    "last_probe_ts": 0.0,
    "next_probe_ts": 0.0,
    "fail_streak": 0,
    "restart_count": 0,
    "restart_ts": 0.0,
    "next_restart_ts": 0.0,
    "warming": False,
    "port_ok": False,
    "sel": 0.0,
    "epoch": 0,
}


def _warp_line_state_path() -> str:
    return (os.environ.get("WARP_LINE_STATE_FILE_PATH")
            or _WARP_LINE_STATE_FILE)


def _warp_line_state_locked(tag: str) -> dict:
    st = _WARP_LINE_STATE.get(tag)
    if not isinstance(st, dict):
        st = _WARP_LINE_STATE[tag] = {}
    for k, v in _LINE_DEFAULTS.items():
        st.setdefault(k, v)
    return st


def _warp_line_update_locked(tag: str, **fields) -> dict:
    st = _warp_line_state_locked(tag)
    st.update(fields)
    return st


def _warp_line_update(tag: str, **fields) -> dict:
    with _WARP_LINE_CV:
        st = _warp_line_update_locked(tag, **fields)
        _WARP_LINE_CV.notify_all()
    return st


def _warp_line_state(tag: str) -> dict:
    with _WARP_LINE_CV:
        return dict(_warp_line_state_locked(tag))


def _save_warp_line_state(force: bool = False) -> None:
    global _WARP_LINE_LAST_SAVE
    now = time.time()
    with _WARP_LINE_SAVE_LOCK:
        if not force and now - _WARP_LINE_LAST_SAVE < 5.0:
            return
        _WARP_LINE_LAST_SAVE = now
    with _WARP_LINE_CV:
        payload = {"version": 1,
                   "lines": {t: dict(s) for t, s in _WARP_LINE_STATE.items()}}
    path = _warp_line_state_path()
    if not payload["lines"]:
        try:
            if os.path.getsize(path) > 2:
                log(f"  · 跳过空状态表覆盖 {path}（盘上已有记录）")
                return
        except OSError:
            pass
    _write_json_atomic(path, payload)


def _load_warp_line_state() -> None:
    """恢复状态表；过期 OK 在启动时直接作废，避免拿重启前的证据选线。"""
    try:
        with open(_warp_line_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        log(f"✗ 加载 .warp_line_state.json 失败: {e}")
        return
    lines = data.get("lines", data) if isinstance(data, dict) else {}
    if not isinstance(lines, dict):
        return
    now = time.time()
    fresh = _line_fresh_sec()
    with _WARP_LINE_CV:
        for tag, raw in lines.items():
            if not isinstance(raw, dict):
                continue
            st = _warp_line_state_locked(tag)
            st.update({k: v for k, v in raw.items() if k in _LINE_DEFAULTS})
            if (st.get("last_ok_ip") != st.get("ip")
                    or now - float(st.get("last_ok_ts") or 0) > fresh):
                st["last_ok_ts"] = 0.0
                st["last_ok_ip"] = None
            st["warming"] = False
        _WARP_LINE_CV.notify_all()
    log(f"✓ 已恢复 {len(lines)} 条 WARP 线路状态")


def _line_fresh_sec() -> float:
    return float(_warp().get("line_fresh_sec",
                             _warp().get("probe_transient_ttl_sec", 120)))


def _line_unknown_retry() -> float:
    return float(_warp().get("probe_unknown_retry_sec", 5))


def _line_transient_retry(fail_streak: int = 1) -> float:
    base = float(_warp().get("probe_transient_retry_sec",
                             _warp().get("probe_transient_ttl_sec", 120)))
    cap = float(_warp().get("probe_transient_retry_max_sec", 600))
    return min(base * (2 ** max(0, fail_streak - 1)), cap)


def _line_persistent_retry() -> float:
    return float(_warp().get("probe_persistent_retry_sec",
                             _warp().get("probe_persistent_ttl_sec", 21600)))


def _line_block_remaining(ip: str | None, now: float) -> float:
    if not ip:
        return 0.0
    with _WARP_IP_LOCK:
        entry = _WARP_IP_USED.get(ip)
    if not isinstance(entry, dict) or not entry.get("blocked"):
        return 0.0
    try:
        return max(0.0, float(_warp_block_remaining(entry, now)))
    except Exception:
        return 0.0


def _warp_line_ok_fresh(tag: str, now: float | None = None,
                        fresh_sec: float | None = None) -> bool:
    if now is None:
        now = time.time()
    if fresh_sec is None:
        fresh_sec = _line_fresh_sec()
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        ip = st.get("ip")
        if not ip or st.get("last_ok_ip") != ip:
            return False
        return now - float(st.get("last_ok_ts") or 0.0) <= fresh_sec


def _warp_line_needs_probe(tag: str, now: float) -> tuple[bool, float]:
    """是否需要探测，以及下一次最早该看的时刻。"""
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        next_ts = float(st.get("next_probe_ts") or 0.0)
        restart_ts = float(st.get("next_restart_ts") or 0.0)
        # 重启中的线不能派探测：`_probe_one_line` 会因 warming 立即返回，
        # 主循环每轮又把它算进 due，形成 0.1s 级的线程自旋。
        if st.get("warming") or now < restart_ts:
            return False, max(next_ts, restart_ts, now + 1.0)
        if now < next_ts:
            return False, next_ts
        ip = st.get("ip")
        if not ip:
            return True, now
        last_ok = float(st.get("last_ok_ts") or 0.0)
        if (st.get("last_ok_ip") == ip
                and now - last_ok <= _line_fresh_sec()):
            return False, last_ok + _line_fresh_sec()
        return True, now


def _warp_line_record_observation(tag: str, ip: str | None, verdict: str,
                                  ts: float | None = None,
                                  started_at: float | None = None,
                                  epoch: int | None = None) -> bool:
    """写入一次探测结论；返回 False 表示结论早于地址标脏，已丢弃。"""
    if ts is None:
        ts = time.time()
    if ip and started_at is not None:
        with _WARP_REFRESH_LOCK:
            if started_at <= _PROBE_BLOCKED_AT.get(ip, 0.0):
                return False
    verdict = verdict or "unknown"
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        # 重启世代不同 = 这条结论来自旧会话。墙钟比较在回拨/长探测下都不可靠，
        # 世代是唯一不会误收旧证据的判据。
        if epoch is not None and int(st.get("epoch") or 0) != int(epoch):
            return False
        # 探测启动后这条线可能已被换线路径记为 OK（`_warp_line_note_ok`）。
        # 晚到的未知结论不能把更新的 OK 清掉，否则又回到「探不到就重探」。
        if (started_at is not None
                and float(st.get("last_ok_ts") or 0.0) > started_at):
            return False
        old_ip = st.get("ip")
        if ip:
            if old_ip and old_ip != ip:
                st["last_ok_ts"] = 0.0
                st["last_ok_ip"] = None
            st["ip"] = ip
            st["ip_ts"] = ts
            st["port_ok"] = True
        st["last_verdict"] = verdict
        st["last_probe_ts"] = ts
        if verdict == "OK":
            st["fail_streak"] = 0
            st["last_ok_ts"] = ts
            st["last_ok_ip"] = st.get("ip")
            st["next_probe_ts"] = ts + _line_fresh_sec()
        else:
            st["last_ok_ts"] = 0.0
            st["last_ok_ip"] = None
            st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
            if verdict == "unknown":
                wait = _line_unknown_retry()
            elif verdict in ("quota", "flagged", "blocked"):
                wait = max(_line_persistent_retry(),
                           _line_block_remaining(st.get("ip"), ts))
            elif verdict == "blind":
                wait = _line_transient_retry(1)
            else:
                wait = _line_transient_retry(st["fail_streak"])
            st["next_probe_ts"] = ts + wait
        _WARP_LINE_CV.notify_all()
    _save_warp_line_state()
    return True


def _warp_line_note_ok(tag: str, ip: str, ts: float) -> None:
    """生产成功流量也是 OK 来源；只更新内存，避免每 5s 落盘。

    成功必须能证明「此刻的地址可用」：台账已判 blocked 的地址、重启前的
    请求（`ts <= restart_ts`）、与线缓存地址不一致的旧连接，都不算。
    否则一条迟到的旧成功会把刚重启的新会话拖回旧地址。
    """
    if not tag or not ip:
        return
    now = time.time()
    if _warp_ip_is_blocked(ip, now):
        return
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        cached = st.get("ip")
        if cached and cached != ip:
            return
        if ts <= float(st.get("restart_ts") or 0.0):
            return
        st["ip"] = ip
        st["ip_ts"] = max(float(st.get("ip_ts") or 0.0), ts)
        st["last_ok_ts"] = max(float(st.get("last_ok_ts") or 0.0), ts)
        st["last_ok_ip"] = ip
        st["last_verdict"] = "OK"
        st["fail_streak"] = 0
        # 真正的成功覆盖历史停车：quota/blocked 的线级停车只是 IP 惩罚的
        # 镜像，地址已被实测服务成功就不再成立。保留 max() 会让一条 OK 线
        # 在 16h 内「不算 OK 也不被探测」（实测 WARP-CF-I）。
        st["next_probe_ts"] = max(float(ts), now) + _line_fresh_sec()
        _WARP_LINE_CV.notify_all()


def _warp_line_invalidate(ip: str | None, now: float | None = None) -> None:
    """某地址被生产流量标脏：所有持有它的线立即失去 OK，等待重探。"""
    if not ip:
        return
    if now is None:
        now = time.time()
    with _WARP_LINE_CV:
        for st in _WARP_LINE_STATE.values():
            if st.get("ip") == ip:
                st["last_ok_ts"] = 0.0
                st["last_ok_ip"] = None
                st["last_verdict"] = "blocked"
                st["next_probe_ts"] = min(
                    float(st.get("next_probe_ts") or now),
                    now + _line_unknown_retry())
            elif st.get("last_ok_ip") == ip:
                # 这条线已经换到别的地址，旧证据只需作废，不能把它的
                # 新地址也停车；但历史 16h 停车不能继续拖着一条没被证实
                # 的新地址，收敛到一个 fresh 窗口内。
                st["last_ok_ts"] = 0.0
                st["last_ok_ip"] = None
                if st.get("last_verdict") == "OK":
                    st["last_verdict"] = "unknown"
                st["next_probe_ts"] = min(
                    float(st.get("next_probe_ts") or now),
                    now + _line_fresh_sec())
        _WARP_LINE_CV.notify_all()
    _save_warp_line_state()


def _warp_line_fresh_candidates(exclude_tag: str | None,
                                now: float | None = None) -> list[tuple[dict, str]]:
    if now is None:
        now = time.time()
    with _WARP_REFRESH_LOCK:
        refreshing = set(_WARP_REFRESHING)
    fresh = _line_fresh_sec()
    guard = float(_warp()["ip_reuse_guard_sec"])
    out: list[tuple[dict, str]] = []
    with _WARP_LINE_CV:
        for m in _warp()["members"]:
            tag = m["tag"]
            if tag == exclude_tag or tag in refreshing:
                continue
            st = _warp_line_state_locked(tag)
            ip = st.get("ip")
            if not ip or st.get("warming"):
                continue
            if (st.get("last_ok_ip") != ip
                    or now - float(st.get("last_ok_ts") or 0.0) > fresh):
                continue
            out.append((m, ip))
    out = [(m, ip) for m, ip in out if not _warp_ip_is_blocked(ip, now)]
    seen: set[str] = set()
    deduped: list[tuple[dict, str]] = []
    for m, ip in out:
        if ip in seen:
            continue
        seen.add(ip)
        deduped.append((m, ip))
    deduped.sort(key=lambda p: _warp_ip_rank(p[1], now, guard))
    return deduped


def _warp_line_wait_fresh(exclude_tag: str | None,
                          deadline: float) -> list[tuple[dict, str]]:
    while True:
        now = time.time()
        cands = _warp_line_fresh_candidates(exclude_tag, now)
        if cands:
            return cands
        if now >= deadline:
            return []
        next_due = deadline
        with _WARP_LINE_CV:
            for tag, st in _WARP_LINE_STATE.items():
                if tag == exclude_tag:
                    continue
                due = float(st.get("next_probe_ts") or 0.0)
                if not due:
                    due = now + _line_unknown_retry()
                next_due = min(next_due, due)
                last_ok = float(st.get("last_ok_ts") or 0.0)
                if st.get("last_ok_ip") == st.get("ip") and last_ok:
                    next_due = min(next_due, last_ok + _line_fresh_sec())
        timeout = min(max(0.05, deadline - now),
                      max(0.05, next_due - now), 1.0)
        with _WARP_LINE_CV:
            _WARP_LINE_CV.wait(timeout=timeout)


def _warp_line_selected(tag: str, ip: str, ts: float) -> None:
    with _WARP_LINE_CV:
        _warp_line_update_locked(tag, sel=ts, ip=ip, ip_ts=ts)
        _WARP_LINE_CV.notify_all()
    _save_warp_line_state()


def _warp_line_restart_backoff(fail_streak: int) -> float:
    seq = _warp().get("restart_backoff_sec", [2, 5, 10, 20, 60])
    if not isinstance(seq, list) or not seq:
        seq = [2, 5, 10, 20, 60]
    base = float(seq[min(max(0, fail_streak - 1), len(seq) - 1)])
    cap = float(_warp().get("restart_backoff_max_sec", 120))
    return min(base, cap)


def _warp_line_restart(member: dict, reason: str) -> bool:
    """统一重启入口：只有确定失败才走到这里，且 active 线永不重启。"""
    tag = member["tag"]
    if tag == active_warp_tag():
        log(f"  · 当前出口 {tag} 不重启（失败时换线，{reason}）")
        return False
    now = time.time()
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        if st.get("warming") or now < float(st.get("next_restart_ts") or 0.0):
            return False
        st["warming"] = True
        st["epoch"] = int(st.get("epoch") or 0) + 1
    got_slot = False
    try:
        busy = warp_line_busy(tag, _ERROR_PATH_BUSY_WINDOW_SEC)
        if busy is not False:
            with _WARP_LINE_CV:
                st = _warp_line_state_locked(tag)
                st["warming"] = False
                st["next_restart_ts"] = now + float(
                    _warp().get("restart_cooldown_sec", 5))
                st["next_probe_ts"] = st["next_restart_ts"]
            return False
        got_slot = _acquire_refresh_slot(tag)
        if not got_slot:
            with _WARP_LINE_CV:
                st = _warp_line_state_locked(tag)
                st["warming"] = False
                st["next_probe_ts"] = now + float(
                    _warp().get("restart_cooldown_sec", 5))
            return False
        if tag == active_warp_tag():
            _release_refresh_slot(tag)
            got_slot = False
            with _WARP_LINE_CV:
                _warp_line_state_locked(tag)["warming"] = False
            return False
    except Exception as e:
        # 判忙/拿槽异常若把 warming 留在 True，这条线会被所有探测路径永久跳过。
        if got_slot:
            _release_refresh_slot(tag)
        with _WARP_LINE_CV:
            st = _warp_line_state_locked(tag)
            st["warming"] = False
            st["next_probe_ts"] = now + float(
                _warp().get("restart_cooldown_sec", 5))
        log(f"  ✗ {tag} 重启前置检查异常: {e}")
        return False
    restart_sem = _RESTART_SEM
    got_restart_slot = False
    if restart_sem is not None:
        got_restart_slot = restart_sem.acquire(timeout=5)
        if not got_restart_slot:
            _release_refresh_slot(tag)
            with _WARP_LINE_CV:
                st = _warp_line_state_locked(tag)
                st["warming"] = False
                st["next_restart_ts"] = now + float(
                    _warp().get("restart_cooldown_sec", 5))
                st["next_probe_ts"] = st["next_restart_ts"]
            return False
    log(f"↻ 重启 {tag}（{reason}）")
    prev_ip = _warp_line_state(tag).get("ip")
    try:
        _invalidate_warp_line_ip(tag)
        ok = (_restart_service(member["service"], tag)
              and _wait_port_ready(member["port"],
                                   _warp()["ready_timeout_sec"]))
        if not ok:
            with _WARP_LINE_CV:
                st = _warp_line_state_locked(tag)
                st["warming"] = False
                st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
                st["next_restart_ts"] = time.time() + _warp_line_restart_backoff(
                    st["fail_streak"])
                st["next_probe_ts"] = st["next_restart_ts"]
            log(f"  ✗ {tag} 重启后未就绪（{reason}）")
            return False
        ip = _probe_refresh_egress(member["port"])
        now = time.time()
        # 重启后的地址若在台账里仍处惩罚期，停探到确切解禁：这条地址已经
        # 被判脏，再探只是把同一结论再买一次。
        block_left = _line_block_remaining(ip, now) if ip else 0.0
        with _WARP_LINE_CV:
            st = _warp_line_state_locked(tag)
            st["warming"] = False
            st["restart_count"] = int(st.get("restart_count") or 0) + 1
            st["restart_ts"] = now
            st["next_restart_ts"] = 0.0
            st["last_ok_ts"] = 0.0
            st["last_ok_ip"] = None
            # 重启只换地址，不撤销该线已记录在案的惩罚：quota 的 16h /
            # blocked 的 6h 是上游按 IP 给出的恢复时刻，重启把它改回 +5s
            # 等于让惩罚失效（同址重启会立刻再烧一次配额）。
            prev_next = float(st.get("next_probe_ts") or 0.0)
            if not ip:
                # 隧道重启了但出口地址拿不到：这是一次未完成的换址，不能
                # 当成「新 IP」清掉 quota 停车（否则 5s 后重探再烧一次）。
                # 台账里的 ban_until 仍然有效；这里只按退避排下一次重启。
                st["last_verdict"] = "unknown"
                st["next_probe_ts"] = now + _line_unknown_retry()
                st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
                st["next_restart_ts"] = now + _warp_line_restart_backoff(
                    st["fail_streak"])
            else:
                same_ip = ip == prev_ip
                # 台账判据优先：重启换到另一个同样被禁的地址也要停到解禁。
                if block_left > 0:
                    st["next_probe_ts"] = max(prev_next, now + block_left)
                elif (same_ip and prev_next > now
                        and st.get("last_verdict") in ("quota", "flagged", "blocked")):
                    st["next_probe_ts"] = prev_next
                else:
                    st["last_verdict"] = "unknown"
                    st["next_probe_ts"] = now + _line_unknown_retry()
                st["ip"] = ip
                st["ip_ts"] = now
                st["port_ok"] = True
                st["next_restart_ts"] = 0.0
            _WARP_LINE_CV.notify_all()
        _save_warp_line_state()
        log(f"  {'✓' if ip else '✗'} {tag} 重启后出口={ip or '探测失败'}")
        return bool(ip)
    except Exception as e:
        with _WARP_LINE_CV:
            st = _warp_line_state_locked(tag)
            st["warming"] = False
            st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
            st["next_restart_ts"] = time.time() + _warp_line_restart_backoff(
                st["fail_streak"])
            st["next_probe_ts"] = st["next_restart_ts"]
        log(f"  ✗ {tag} 重启异常: {e}")
        return False
    finally:
        _release_refresh_slot(tag)
        if got_restart_slot and restart_sem is not None:
            restart_sem.release()


def _probe_rate_allow() -> bool:
    limit = int(_warp().get("probe_max_per_min", 30))
    if limit <= 0:
        return True
    now = time.time()
    with _PROBE_RATE_LOCK:
        while _PROBE_RATE_TIMES and now - _PROBE_RATE_TIMES[0] > 60:
            _PROBE_RATE_TIMES.popleft()
        if len(_PROBE_RATE_TIMES) >= limit:
            return False
        _PROBE_RATE_TIMES.append(now)
        return True


def _probe_one_line(member: dict) -> None:
    tag = member["tag"]
    now = time.time()
    st = _warp_line_state(tag)
    if st.get("warming"):
        return
    active = active_warp_tag()
    if (tag == active and st.get("ip")
            and now - float(st.get("ip_ts") or 0.0)
            < float(_warp().get("ip_observe_sec", 120))
            and _warp_line_ok_fresh(tag, now)):
        return
    started_at = time.time()
    epoch = int(_warp_line_state(tag).get("epoch") or 0)
    ip = warp_egress_ip(member["port"])
    if not ip:
        _warp_line_record_observation(tag, None, "unknown", time.time(),
                                      started_at, epoch)
        return
    cur = _warp_line_state(tag)
    if tag != active and _warp_line_ok_fresh(tag, now) and cur.get("ip") == ip:
        _warp_line_update(tag, next_probe_ts=now + _line_fresh_sec())
        return
    if _warp_ip_is_blocked(ip, now):
        _warp_line_record_observation(tag, ip, "blocked", time.time(),
                                      started_at, epoch)
        if tag == active:
            _reconnect_warp_now("探测发现当前出口处于惩罚期", time.time())
        else:
            _warp_line_restart(member, "出口处于惩罚期")
        return
    if _warp_probe_blind(now):
        _warp_line_record_observation(tag, ip, "blind", time.time(),
                                      started_at, epoch)
        return
    if not _probe_rate_allow():
        _warp_line_update(tag, next_probe_ts=now + 5.0)
        return
    sem = _MODEL_PROBE_SEM
    if sem is None:
        ok = _warp_model_usable(member, ip)
    else:
        with sem:
            ok = _warp_model_usable(member, ip)
    verdict = "OK" if ok else (_PROBE_LAST_VERDICT.get(tag) or "err")
    if not ok and verdict == "OK":
        # `_warp_model_usable` 的「证据比事实旧」等早退路径给不出本次结论，
        # 沿用上一次残留的 OK 会把失败记成假成功。
        verdict = "err"
    if not _warp_line_record_observation(tag, ip, verdict,
                                         time.time(), started_at, epoch):
        return
    if verdict == "OK":
        return
    if tag == active:
        _reconnect_warp_now(f"探测失败 {verdict}", time.time())
    else:
        _warp_line_restart(member, f"模型层 {verdict}")


def _warp_prober_loop() -> None:
    while True:
        now = time.time()
        active = active_warp_tag()
        due: list[tuple[float, dict]] = []
        for m in _warp()["members"]:
            tag = m["tag"]
            with _WARP_LINE_CV:
                if tag in _WARP_PROBING:
                    continue
            needs, due_ts = _warp_line_needs_probe(tag, now)
            if needs:
                due.append((due_ts, m))
        free = int(_warp().get("probe_concurrency", 4)) - len(_WARP_PROBING)
        if due and free > 0:
            due.sort(key=lambda p: p[0])
            for _, m in due[:free]:
                tag = m["tag"]
                with _WARP_LINE_CV:
                    if tag in _WARP_PROBING:
                        continue
                    _WARP_PROBING.add(tag)

                def _run(member: dict = m, tag: str = tag) -> None:
                    try:
                        _probe_one_line(member)
                    except Exception as e:
                        log(f"  ✗ 探测 {tag} 异常: {e}")
                    finally:
                        with _WARP_LINE_CV:
                            _WARP_PROBING.discard(tag)
                            _WARP_LINE_CV.notify_all()

                threading.Thread(target=_run, daemon=True,
                                 name=f"warp-probe-{tag}").start()
            continue
        if free <= 0:
            time.sleep(0.2)
            continue
        next_due = now + float(_warp().get("prober_idle_wait_sec", 30))
        with _WARP_LINE_CV:
            for tag, st in _WARP_LINE_STATE.items():
                next_due = min(next_due,
                               float(st.get("next_probe_ts") or 0.0) or next_due)
        with _WARP_LINE_CV:
            _WARP_LINE_CV.wait(timeout=max(0.1, next_due - time.time()))


def _start_warp_prober() -> None:
    global _PROBER_STARTED, _PROBER_THREAD, _MODEL_PROBE_SEM, _RESTART_SEM
    with _WARP_REFRESH_LOCK:
        if _PROBER_STARTED:
            return
        _PROBER_STARTED = True
    _MODEL_PROBE_SEM = threading.BoundedSemaphore(
        max(1, int(_warp().get("model_probe_concurrency", 2))))
    _RESTART_SEM = threading.BoundedSemaphore(
        max(1, int(_warp().get("restart_concurrency", 4))))
    _PROBER_THREAD = threading.Thread(target=_warp_prober_loop,
                                      daemon=True, name="warp-prober")
    _PROBER_THREAD.start()
    log("✓ WARP 单实例探测器已启动（状态表驱动换线）")


def _port_open(port: int) -> bool:
    """隧道 SOCKS 端口是否在监听。本地 connect 通常 <1ms，可放进换线关键路径。"""
    try:
        with socket.create_connection((_warp()["socks_host"], port), timeout=0.5):
            return True
    except OSError:
        return False


def _cached_warp_standby(standby: list[dict], now: float,
                         guard: float) -> list[tuple[dict, str]]:
    """返回缓存仍可信的备线 (member, ip)，按选线优先级排好序。

    WHY 需要：换线耗时的中位 2s、p90 7s，全部花在同步探测出口 IP 上；真正
    切 selector 是 0s。而 403/429 在换线后中位 3s 就到达 —— 探测本身就把
    「抢在报错之前换线」的窗口吃光了。脏线守护每 ~5s 已经探过每条非当前
    出口的线，结果就在 `_WARP_LINE_STATE` 里，直接拿来用即可。

    「可信」= 两个条件同时成立，缺一不可：
      ① `probe_ok_ttl_sec` 内模型层实测过且结论为 OK —— 只验过「能出网」不算，
         出口此刻是否还有配额只有模型层说得准；
      ② 同一窗口内这个 IP 没被切过去当出口 —— 切过说明它已经在服务生产流量，
         上游随时会把它烧掉。实测出口 IP 存活中位仅 9s，一个「验过但已被切过」
         的地址与新地址在可用性上没有差别。
    两条件都不成立时该候选直接跳过，由调用方退回同步探测 + 实测的慢路径。

    TTL 取 12s：守护的探测节奏是 5s + 单次探测耗时（实测 1.2~7s），12s 覆盖
    """
    ttl = _warp().get("probe_cache_ttl_sec", 12)
    ok_ttl = float(_warp().get("probe_ok_ttl_sec", 600))
    out: list[tuple[dict, str]] = []
    for m in standby:
        ip = _warp_line_ip(m["tag"])
        ts = _warp_line_ip_ts(m["tag"])
        if not ip or now - ts > ttl:
            continue
        # ① 模型层实测过且 OK。`_probe_cache_verdict` 只在 TTL 内返回结论，
        # 故 "非 None 且 == OK" 已同时表达「验过」与「新鲜」。
        if _probe_cache_verdict(m["tag"], ip, now) != "OK":
            continue
        # ② 窗口内没被切过。`_warp_ip_last_ts` 读 `ts`（最近一次被选为出口的
        # 时刻），从未用过返回 0，`now - 0` 必然大于窗口，天然放行。
        if now - _warp_ip_last_ts(ip) < ok_ttl:
            continue
        if _ip_disqualified(ip, now, guard):
            continue
        if not _port_open(m["port"]):
            continue
        out.append((m, ip))
    out.sort(key=lambda p: _warp_ip_rank(p[1], now, guard))
    # 同一 IP 可能被多条隧道同时持有（重连竞态），只保留最优的一条。
    # out 已按档位/休息时长排序，首条即最优。
    seen = set()
    deduped = []
    for m, ip in out:
        if ip in seen:
            continue
        seen.add(ip)
        deduped.append((m, ip))
    return deduped


def _dedupe_probe_pairs(pairs: list[tuple[dict, str]],
                        skip_ip: str | None = None) -> list[tuple[dict, str]]:
    """按 IP 去重并跳过指定地址，供模型实测前收敛探测次数。

    每条候选实测消耗该出口一格配额；同址多线重复探测只会得到同一个结论。
    """
    seen = set()
    out = []
    for m, ip in pairs:
        if not ip or ip == skip_ip or ip in seen:
            continue
        seen.add(ip)
        out.append((m, ip))
    return out


def _exclude_active_ip(pairs: list[tuple[dict, str]],
                       active_ip: str | None) -> list[tuple[dict, str]]:
    """候选出口不能与当前出口或彼此重复 —— 换到同一个地址等于没换。

    WHY 必须硬拦：实测 817 次换线里 111 次（13.7%）「新出口 == 旧出口」，
    全部是 rate429 触发，切过去后仍然限流，白白重启了目标线（每 IP 同一时刻
    只属于一条隧道，重复即无效）。20 条隧道并行时同一 IP 也可能被两条线同时
    拿到（CF 会话重复分配），去重保留首条即最优（调用方已按选线优先级排序）。

    全被滤空时退回原列表：宁可换到旧地址，也不要因为一条理论约束把流量甩到
    轮换池（池有 3h 冷却，代价远大于一次无效换线）。
    """
    seen = set()
    out = []
    for m, ip in pairs:
        if ip == active_ip or ip in seen:
            continue
        seen.add(ip)
        out.append((m, ip))
    return out or pairs


def _warp_ip_last_ts(ip: str | None) -> float:
    """该出口 IP 最后一次作为 egress 的 epoch 秒；从未用过返回 0（最老）。"""
    if not ip:
        return 0.0
    # 取值也在锁内：entry 是活字典，锁外读 ts 会与换线线程的写入竞争。
    with _WARP_IP_LOCK:
        entry = _WARP_IP_USED.get(ip)
        ts = (entry.get("ts") if isinstance(entry, dict) else entry) if entry else None
    return float(ts) if ts is not None else 0.0


def _warp_block_ttl(kind: str | None) -> float:
    """按烧掉该 IP 的错误类型给出惩罚期（秒）。

    abuse403 六小时刷新、rate429 默认六十秒；kind 缺失（旧记录）按 24h
    保守处理。429 的精确恢复时刻（header/body/message 解析链）经 ban_after
    写进 ban_until 后在这里覆盖默认值；解析失败是常态（生产库不落响应头），
    60s 默认让 IP 尽快回池、靠 ban_until 承载长封禁。
    """
    w = _warp()
    if kind == "abuse403":
        return w["blocked_guard_sec"]
    if kind == "rate429":
        return w["rate429_guard_sec"]
    # legacy：kind 缺失的旧账只认 blocked_ttl_sec 的 24h 保守值，不吃 429 的
    # 60s 默认 —— 无法判别的旧记录不能因为默认缩短而被提前放回候选池。
    return w.get("blocked_ttl_sec", 86400)


def _latest_rate429_body_since(since_ts: float) -> str | None:
    """生产路径最近一条 rate429 行的 data 原文；窗口里没有 429 返回 None。

    requestDetails 只落 status/data，不存响应头 —— 生产侧的取数只能走
    body/message 层；header 层（Retry-After 秒数 / HTTP 日期）只在探测路径
    `_warp_model_usable` 出现，那边直接把 parse 结果当 ban_after 传入。
    索引预筛（timestamp >）与 _START_TS_SQL 精筛（开始时刻 >）的分工见
    count_warp_blocked_since。LIKE 是可取数物质的关键词窄化：LIMIT 1 DESC
    会被一段里最新的 403 顶掉，把它变成「429 里的最新一条」才能取到目标；
    取出来仍过 _classify_egress_error 复核（分类唯一权威，LIKE 只是预筛）。
    """
    floor = (datetime.fromtimestamp(max(since_ts, 0.0), tz=timezone.utc)
             .strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        with _db_lock:
            row = _get_db().execute(f"""
                SELECT data FROM requestDetails
                WHERE provider = ? AND timestamp > ? AND {_START_TS_SQL} > ?
                  AND (data LIKE '%FreeUsageLimitError%'
                       OR data LIKE '%Rate limit exceeded%'
                       OR data LIKE '%Free usage limit reached%')
                ORDER BY {_START_TS_SQL} DESC LIMIT 1
            """, (OPENCODE_FREE_PROVIDER, floor, max(since_ts, 0.0))).fetchone()
    except Exception as e:
        log(f"✗ 读取最近 429 行失败: {e}")
        return None
    data = str(row["data"]) if row and row["data"] else None
    return data if data and _classify_egress_error(data) == "rate429" else None


def _parse_rate429_cooldown(body: str | None) -> int | None:
    """429 精确冷却秒数：响应体字段 → 报错文案（网络取数层 header 优先序
    在 probe_warp.parse_retry_after，含 header 秒数 / HTTP 日期）。

    返回 None = 本条证据解析不出时长，由调用方落 config 默认
    （rate429_guard_sec=60）。钳制 [1s, 24h] 已在 probe_warp 内完成，这里
    不再夹一遍，避免两处上限漂移。
    """
    import probe_warp
    return probe_warp.parse_retry_after(None, body or "")


def _as_ts(value) -> float | None:
    """把台账里的时间字段解析成 float；损坏/NaN/Inf 一律当缺失。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _warp_ban_until(entry: dict) -> tuple[float | None, str]:
    """解析 `ban_until` / `ban_kind`，坏值当作没有精确封禁。

    这两个字段来自落盘台账，可能被手工编辑、旧版本写入或半截写入污染。
    选线热路径不能因为一个字符串就抛异常 —— 坏值只丢精确封禁，仍按 6h/24h
    默认窗口保守处理。
    """
    until = _as_ts(entry.get("ban_until"))
    if until is None or until <= 0:
        return None, ""
    # 只有 429 的 Retry-After 是可信的精确封禁。旧版曾把响应头套到 403 上，
    # 那种 ban_until 只会把 6h 默认窗口缩短；缺失/未知 kind 也按默认窗口。
    kind = entry.get("ban_kind")
    if kind != "rate429":
        return None, ""
    return until, kind


def _warp_block_window(entry: dict) -> float:
    """该 IP 惩罚判定的回看窗口：精确封禁优先于同类型默认值。

    与 `_warp_block_remaining` 用同一套取数规则。`_warp_ip_recently_used` 靠
    这个窗口回答「窗口内是否成功过」；如果固定按 24h 保守回看，上游给出
    4.6h Retry-After 的地址会在恢复后继续被判为「窗口内用过」18h，
    精确封禁等于只在探测路径生效、选线路径没生效。
    """
    stamps = _warp_block_stamps(entry)
    if not stamps:
        return _warp_block_ttl(None)
    ban_until, ban_kind = _warp_ban_until(entry)
    window = 0.0
    for k, t in stamps.items():
        # 只有 ban_until 晚于该类型起点才算「确切封禁」。旧台账可能把 stamp
        # 更新得比 ban_until 还新（上一版写入顺序留下的），那种组合按默认窗口。
        if ban_until is not None and k == ban_kind and ban_until > t:
            window = max(window, ban_until - t)
        else:
            window = max(window, _warp_block_ttl(k))
    return max(window, 0.0)


def _warp_block_stamps(entry: dict) -> dict[str, float]:
    """各类惩罚期的起点（epoch 秒）。

    WHY 按类型分开存：同一 IP 可能先被 403 烧、几小时后再被 429 烧，两类
    惩罚各算各的，解除时间取较晚的那个。只留一个 (kind, start) 会让后一次
    标记覆盖前一次 —— 403 之后再烧成 429，仍按 6h 解禁，提前 18h 放回。
    旧记录只有 blocked_ts + blocked_kind：按 kind 归到对应一类，kind 缺失
    归到 rate429（24h 保守）。
    """
    stamps = entry.get("blocked_ts_by_kind")
    if isinstance(stamps, dict) and stamps:
        return {k: float(v) for k, v in stamps.items() if v is not None}
    if not entry.get("blocked"):
        return {}
    start = entry.get("blocked_ts")
    if start is None:
        start = entry.get("last_ok_ts")
    if start is None:
        start = entry.get("ts")
    if start is None:
        return {}
    kind = entry.get("blocked_kind")
    # 只读不回写：调用点有的在锁外，回写会与持锁的写入竞争、丢掉刚记的起点。
    # 旧记录不必单独迁移 —— 该 IP 下次被标记时回退链读出的结果会写成新格式。
    # kind 缺失归到 legacy（blocked_ttl_sec 的 24h 保守档），不冒充 rate429：
    # 60s 默认只属于「有 kind 但没解析到精确值」的新账。
    return {(kind if kind in ("abuse403", "rate429") else "legacy"): float(start)}


def _warp_block_remaining(entry: dict, now: float) -> float:
    """剩余惩罚期（秒），<=0 表示已恢复。多类标记取最晚解除的那一类。

    上游 429 带 `Retry-After` 时，探针把确切恢复时刻写进 `ban_until`；它替换
    同类型的默认惩罚期（rate429 的默认 60s），不是与之取最大值 —— 默认值只是
    没有观测时的兜底回池节奏。403 等其它类型仍按各自窗口并行，仍遵守「按多的来」。
    """
    stamps = _warp_block_stamps(entry)
    if not stamps:
        return _warp_block_ttl(None)
    ban_until, ban_kind = _warp_ban_until(entry)
    remain = 0.0
    for k, t in stamps.items():
        # 同 `_warp_block_window`：ban_until 必须晚于该类型起点，否则是旧版
        # 台账留下的不一致组合，按默认窗口保守处理。
        if ban_until is not None and k == ban_kind and ban_until > t:
            remain = max(remain, ban_until - now)
        else:
            remain = max(remain, t + _warp_block_ttl(k) - now)
    return remain


def _warp_ip_recently_used(ip: str | None, now: float,
                           window: float | None = None) -> bool:
    """该出口 IP 是否**仍在惩罚期内**、窗口内有过成功流量。

    判据是 `last_ok_ts`（最近一次成功响应的时刻），不是 `ts`（最近一次
    被选为出口的时刻）。区别在于：一条被选上去后立刻 403 的线，`ts` 很新
    但 `last_ok_ts` 是 0/很旧 —— 它其实从没服务成功过，不该因为"刚用过"
    而被排除；反过来，一条正在稳定服务的线即使被选中很久，也算"刚成功过"。
    没有 last_ok_ts 的老记录退回 ts 判定（迁移期）。

    窗口取该 IP 记过的各类惩罚期中最长的一档：先被 403 烧过（6h）、没过多久
    又被 429 烧过（默认 60s；解析到精确值时更长，上限 24h）的地址取更晚的
    那条 —— 规则是「按多的来」。过了惩罚期的
    地址降级为**次级优质**，可以复用而不是永久排除 —— 这正是它重新进入候选池
    的路径。未被标记的地址沿用调用方给的 window。
    """
    if not ip:
        return False
    with _WARP_IP_LOCK:
        entry = _WARP_IP_USED.get(ip)
        ok = entry.get("last_ok_ts") if isinstance(entry, dict) else None
    stamps = (_warp_block_stamps(entry)
              if isinstance(entry, dict) and entry.get("blocked") else {})
    if stamps:
        window = _warp_block_window(entry)
    elif window is None:
        window = _warp()["ip_reuse_guard_sec"]
    # 本函数只回答「窗口内有没有成功流量」，没有成功记录答 False。这与
    # 「该不该用」不是一回事 —— 没成功过也没被标记的地址（实测 97/350 条）
    # 由 _warp_ip_rank 排到第 2 档，不当新鲜地址直接切，但仍可作兜底。
    return bool(ok) and now - float(ok) < window


def _warp_ip_is_blocked(ip: str | None, now: float | None = None) -> bool:
    """该出口 IP 当前是否仍处于 403/429 惩罚期。

    两类拦截的恢复期不同，各自计时后取**最晚解除**的那一类：
      - abuse403：出口 IP 信誉标记，6 小时后刷新（blocked_guard_sec）；
      - rate429：出口 IP 级限流，24 小时后刷新（rate429_guard_sec）；
      - kind 缺失（旧记录）：按 24 小时保守处理（blocked_ttl_sec）。
    同一 IP 先 403 后 429 时两条惩罚线并行，解除时间取晚的（用户规则「按多的来」）。
    满期即视为已恢复，降级为**次级优质**地址、可以复用 —— 惩罚期结束不是永久报废。

    `now` 由调用方传入以便同一函数体内多次判定用同一时刻；缺省取当前时间。
    """
    if not ip:
        return False
    with _WARP_IP_LOCK:
        entry = _WARP_IP_USED.get(ip)
    if not isinstance(entry, dict) or not entry.get("blocked"):
        return False
    if now is None:
        now = time.time()
    return _warp_block_remaining(entry, now) > 0


def _ip_history_is_truth() -> bool:
    """本模式是否仍把 IP 历史（脏/复用窗口）当作选线判据。

    模型层探测开启时，历史不再参与判断：一条线能不能用，以此刻的实测为准。
    历史是启发式（「这个地址被烧过」「窗口内成功过」），而 429 按出口段计、
    恢复期几小时，两者经常与事实不一致 —— 实测重启后从没成功过的新地址，
    首条请求照样 429；反之旧地址过了惩罚期完全可以再用。测得出来就不猜。

    探测通道失明时也退回历史判据（见 `_warp_probe_blind`）。「测得出来就不猜」
    的前提是探测器还看得见；它整批报 unreachable 时什么都不代表，拿它当真
    等于用观测者的故障去否定被观测对象。
    """
    return not _warp().get("refresh_model_check", True) or _warp_probe_blind()


# 探测通道失明的判定：一整批探测全部 `unreachable` 才算。单条 `unreachable`
# 是出口段的真实瞬时故障（退避窗口 2min 已按此设），而整批同时失败只可能是
# 探测通道本身出了问题 —— 实测 16:50:18 一批 17 条同一秒全灭，重启换 IP 后
# 16:50:50~52 全部 OK，期间白烧 17 条隧道的重启与配额。
_WARP_PROBE_BLIND_SEC = 120.0
# 「整批」的最小样本数：错误换线每轮只测 probe_candidates_per_switch(2) 条，
# 低于这个数时 all(unreachable) 对单条恒真 —— 一次瞬断就锁死 120s 实测。
_WARP_PROBE_BLIND_MIN_BATCH = 3
_probe_blind_until: float = 0.0
# 各线最近一次探测结论，供 `_note_probe_batch` 判断「整批」而非单条。
_PROBE_LAST_VERDICT: dict[str, str] = {}


def _warp_probe_blind(now: float | None = None) -> bool:
    """模型层探测通道此刻是否失明（整批 unreachable 后的降级窗口内）。"""
    if now is None:
        now = time.time()
    return now < _probe_blind_until


def _note_probe_batch(verdicts: list[str]) -> None:
    """记录一批探测结论；**多条**同时 unreachable 才宣布探测器失明。

    样本数必须够大才谈得上「整批」：`probe_candidates_per_switch` 默认 2，
    错误换线路径每轮只测 1~2 条，单条瞬断就满足「全 unreachable」—— 那不是
    通道坏了，是该出口此刻没打通。一次误判锁 120s，期间 `_warp_probe_blind()`
    让 `_warp_model_usable` 直接返回 True，所有候选**跳过实测被放行**，于是
    quota 地址被切上去、首条请求就 429。实测 2026-10-26 触发 47 次这样的放行，
    其中一次门槛是「本批 1 条」（`all()` 对单元素恒真）。
    """
    global _probe_blind_until
    if len(verdicts) >= _WARP_PROBE_BLIND_MIN_BATCH and all(
            v == "unreachable" for v in verdicts):
        _probe_blind_until = time.time() + _WARP_PROBE_BLIND_SEC
        log(f"  ⚠ 本批 {len(verdicts)} 条模型层探测全 unreachable —— 判定探测通道失明，"
            f"{_WARP_PROBE_BLIND_SEC:.0f}s 内退回历史判据")


def _ip_disqualified(ip: str | None, now: float, guard: float) -> bool:
    """该出口是否因历史原因被排除：仍在惩罚期，或窗口内有过成功流量。

    惩罚期**两模式都生效**：那是「这个地址确实被拦过」的确定事实，不是启发式。
    模型模式旁路的只是复用窗口（「窗口内成功过」—— 按段计、几小时恢复，
    经常与事实不一致），不能连惩罚期一起放行。实测 7 个烧毁段里 648 个 /128
    被逐个探测、608 个（94%）判 quota，全是本该在探测前就被挡住的。
    """
    if _warp_ip_is_blocked(ip, now):
        return True
    if not _ip_history_is_truth():
        return False
    return _warp_ip_recently_used(ip, now, guard)


def _warp_ip_needs_refresh(ip: str | None, now: float,
                           tag: str | None = None) -> bool:
    """该出口是否低质到该被后台刷掉；IP 未知（None）也视为低质。

    模型模式下「低质」只认确定的证据，不用启发式：
      - 仍在 403/429 惩罚期 —— 这条地址确实被拦过；
      - 出现过却连一次 OK 都没拿到（档位 3）—— 切上去就报错那批。
    从没出现过的新地址不算低质，否则刚刷好的线会被当脏线反复重启。
    原模式沿用复用窗口判定，语义不变。
    """
    if not ip:
        return True
    # 本线实测不通过（quota/err/unreachable）就是「此刻不能服务 opencode」，
    # 与 blocked 同义：只有重启换会话才可能好转。判据复用已有结论，不额外探测。
    # 通道失明期间例外：那批 unreachable 说的是探测器自己没打通，不是地址被拒
    # （见 `_warp_probe_blind`）—— 照它重启只会白烧隧道与配额。
    verdict = _probe_cache_verdict(tag, ip, now)
    if verdict in _WARP_PROBE_PERSISTENT:
        # 精确解禁（Retry-After）可能早于 6h 缓存 TTL 到期：惩罚期已过就不该
        # 再凭旧结论重启，交给下一次模型实测重新判定。
        return _warp_ip_is_blocked(ip, now)
    if verdict is not None and verdict != "OK":
        return not (verdict == "unreachable" and _warp_probe_blind(now))
    if _warp_ip_is_blocked(ip, now):
        return True
    if _ip_history_is_truth():
        return _warp_ip_recently_used(ip, now)
    # 只有档 3（出现过却连一次 OK 都没拿到）该刷。档 2 是「探针验过、只是还没轮到
    # 它服务」—— 那正是机制一要保住的资产，刷它等于把好不容易验出的好地址丢掉。
    # WHY 写 == 3 而不是 == 2：加上探针档后档位从三档变四档，沿用 == 2 会把
    # 「验过未用」当成脏线反复重启，正好把机制一变成反向优化。
    return _warp_ip_rank(ip, now, _warp()["ip_reuse_guard_sec"])[0] == 3


def _warp_model_test_many(pairs: list[tuple[dict, str]]) -> dict[str, bool]:
    """并发测多条线的模型层（tag -> 是否可用）。

    串行时每条 1.4~8.7s，换线路径上串行测 9 条就是半分钟 —— 那段时间流量
    还在坏出口上。并发后耗时由最慢那条决定。
    """
    if not pairs:
        return {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(pairs), thread_name_prefix="warp-model") as ex:
        futs = {ex.submit(_warp_model_usable, m, ip): m for m, ip in pairs}
        out = {futs[f]["tag"]: (f.result() if not f.exception() else False)
               for f in concurrent.futures.as_completed(futs)}
    # 整批一起失败才是通道故障；单条 unreachable 是该出口的真实瞬态。
    _note_probe_batch([_PROBE_LAST_VERDICT.get(m["tag"], "") for m, _ in pairs])
    return out


def _admit_tested(probes: list[tuple[dict, str]],
                  tested: dict[str, bool]) -> list[tuple[dict, str]]:
    """只放行本轮实测通过的候选；没测到不等于可用。

    WHY：分批探测为省配额只测一部分，若按「没失败」放行，未测的线会被当成
    可用切过去 —— 配额省了，却切到一条没验证的 429 出口。放行按 tag 而非
    出口 IP：同址的其它线可能隧道已坏，仅凭 IP 相同不能证明它可用。
    """
    return [(m, ip) for m, ip in probes if tested.get(m["tag"])]


def _slice_probe_batch(pairs: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    """错误换线只取下一小批候选实测，控制单次延迟与配额消耗。

    WHY：全量实测 14~19 条约 15s，且每条探针都占该出口一格免费配额。错误风暴里
    每 10~25s 换一次线，等于自己把池里的配额烧光，之后无论切到哪条都 429 —— 这
    正是「明明在换线，却要 2 分钟才恢复」的机制。游标让下一轮从上次停下的位置
    继续，几轮内仍能覆盖全池。
    """
    limit = int(_warp().get("probe_candidates_per_switch", 0) or 0)
    if limit <= 0 or limit >= len(pairs):
        return pairs
    global _PROBE_SLICE_CURSOR
    start = _PROBE_SLICE_CURSOR % len(pairs)
    batch = (pairs + pairs)[start:start + limit]
    _PROBE_SLICE_CURSOR = (start + limit) % len(pairs)
    return batch


_last_success_probe: tuple[int, int] | None = None


_ACTIVE_IP_INIT_TS: float = 0.0


def _probe_active_ip(tag: str) -> str | None:
    """探 tag 的出口 IP；探测期间已换线则返回 None。

    探测最长 20s，期间 selector 完全可能已经切走。把旧线的地址记到新线上，
    比暂时留空更糟 —— 空值只会让补探再来一次，错值会一直骗到下次换线。
    """
    m = _warp_member(tag)
    if not m:
        return None
    ip = warp_egress_ip(m["port"])
    return ip if ip and active_warp_tag() == tag else None


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
        tag = active_warp_tag()
        ip = _probe_active_ip(tag) if tag else None
        if ip:
            _adopt_active_ip(ip)
            log(f"  · 当前出口 IP 补探成功: {ip}（启动时探测未就绪）")

    threading.Thread(target=_probe, daemon=True, name="active-ip-init").start()


def _refresh_active_warp_ip() -> None:
    """重探当前出口 IP，覆盖已有的旧值。

    sing-box 重启会重开隧道会话，出口地址随之改变；`_ACTIVE_WARP_IP` 原先只在
    换线成功时写入，重启恢复同一 tag 时地址已变而变量不变 —— 健康行于是把旧 IP
    配在新会话上，直到下次换线才自愈。探测要 curl（最长 20s），丢给后台线程；
    调用方本来就在重启路径上，不阻塞主循环。
    """
    def _probe() -> None:
        tag = active_warp_tag()
        if tag is None:
            # 重启后落在池里（或还没恢复线路），旧 IP 不属于任何出口。
            if _current_mode()[0] == "proxy":
                _clear_active_ip()
            return
        ip = _probe_active_ip(tag)
        if ip:
            _adopt_active_ip(ip, overwrite=True)

    threading.Thread(target=_probe, daemon=True, name="active-ip-refresh").start()


def recent_success_ts(limit: int) -> list[float]:
    """最近 limit 条 opencode 成功请求各自的写入时刻（epoch 秒），新的在前。

    取请求自己的 timestamp，不是读库时刻。WHY：9router 每 5s 批量落库
    （或攒满 20 条），一批请求在同一个 tick 才可见 —— 用读库时刻当「成功发生
    时刻」，整批被压成一个瞬间，段跨度恒为 0。
    """
    limit = max(1, min(int(limit), 200))
    try:
        with _db_lock:
            rows = _get_db().execute("""
                SELECT timestamp FROM requestDetails
                WHERE provider = ? AND status = 'success'
                ORDER BY rowid DESC LIMIT ?
            """, (OPENCODE_FREE_PROVIDER, limit)).fetchall()
    except Exception as e:
        log(f"✗ 读最近成功时刻失败: {e}")
        return []
    out = []
    for r in rows:
        if not r[0]:
            continue
        try:
            out.append(datetime.fromisoformat(
                str(r[0]).replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            continue
    return out


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
    # 寿命跨度用这条成功请求**自己的时间戳**，不用 `now`。9router 每 5s 批量落库
    # （或攒满 20 条），一批成功在同一个 tick 才被看到 —— 拿 now 记的话，一个只
    # 活 7s 的出口整段只有 1~2 次打点，first_ok_ts == last_ok_ts，span 恒为 0。
    # 实测 89 条样本 80 条 span=0，中位被顶成 0，寿命判据退化成固定下限。
    # 用请求时间戳复算后中位 6.9s（p25 2.4 / p75 11.3），代调用方的窗口口径。
    # 取这一轮新增的那几条（增量 = ok_now - prev_ok），而不是只取最新一条：
    # 批量落库时一次能看见好几条成功，它们的时刻各不相同，只取一条会把跨度又
    # 压成单点。
    fresh = recent_success_ts(ok_now - prev_ok)
    if not fresh:
        return
    with _WARP_IP_LOCK:
        if not isinstance(_WARP_IP_USED.get(ip), dict):
            # 重启后隧道换出的新地址此前没有记录（只有被选为出口时才写），
            # 不补一条的话成功流量永远打不上点，选线日志恒为「有成功记录 0 条」。
            _warp_ip_used_locked(ip, now, False)
        entry = _WARP_IP_USED[ip]
        # first_ok_ts 是本段「可用寿命」的起点。比 ts 还旧说明是上一个 stint
        # 留下的（同址在出窗口后被再次选中），必须重置，否则跨度跨段串算。
        first = entry.get("first_ok_ts")
        if not first or float(first) < float(entry.get("ts") or 0):
            entry["first_ok_ts"] = min(fresh)
        entry["last_ok_ts"] = max(float(entry.get("last_ok_ts") or 0), max(fresh))
        _save_warp_ip_used()
    tag = active_warp_tag()
    if tag:
        _warp_line_note_ok(tag, ip, max(fresh))


def _warp_ip_never_used(ip: str | None) -> bool:
    """该出口 IP 是否从未出现在使用记录里 —— 首选，必然没被 opencode 见过。"""
    if not ip:
        return False
    with _WARP_IP_LOCK:
        return ip not in _WARP_IP_USED


def _warp_ip_prefix(ip: str | None) -> str | None:
    """出口 IP 的 /64；解析失败返回 None（不影响排序）。"""
    if not ip:
        return None
    try:
        return str(ipaddress.ip_network(ip + "/64", strict=False))
    except ValueError:
        return None


@functools.lru_cache(maxsize=8192)
def _warp_ip_prefix_cached(ip: str) -> str | None:
    return _warp_ip_prefix(ip)


def _warp_prefix_rest(ip: str | None, now: float) -> float | None:
    """同一 /64 内最近一次使用距今的秒数；该段从未用过返回 None。

    opencode 的免费额度按出口段而不是单个 /128 计：换线拿到的常是同一 /64
    里的新地址，首条请求照样 429。9router 近 2h 的失败流量按段静置时长分组，
    有成功的段占比 5min 内 73%（n=41）/ 5-15min 100%（n=21）/ >30min 91%
    （n=35）—— 刚烧过的段里换地址几乎等于没换。这里只把静置时长当同档内的
    排序依据，档位语义不变。
    """
    net = _warp_ip_prefix_cached(ip) if ip else None
    if not net:
        return None
    latest = 0.0
    with _WARP_IP_LOCK:
        for other, entry in _WARP_IP_USED.items():
            if _warp_ip_prefix_cached(other) != net:
                continue
            ts = entry.get("ts") if isinstance(entry, dict) else entry
            latest = max(latest, float(ts or 0))
    return now - latest if latest else None


def _warp_ip_rank(ip: str | None, now: float, window: float) -> tuple[int, int, float]:
    """选线优先级：越小越优先。返回 (档位, 段是否刚用过, 排序值)。

    四档（对应「按使用来区分」）：
      0. 从未出现过 —— 最优，必然没被 opencode 见过；
      1. 次级优质 —— **真的撑过生产流量**，且已过对应的惩罚期（403 六小时 /
         429 默认六十秒；解析到精确 Retry-After/body/message 时以 ban_until
         为准，上限 24h）。过了惩罚期的 IP 可以复用，不是废址；档内按「距
         上次成功的时长」排序，越久越靠前，这只是次级优质内部的先后，与恢复
         进度无关。已验证能服务，优先于没成功过的地址；
      2. 探针验过但从未服务生产 —— 空跑里拿到过 OK，但一次真实流量都没撑过。
         配额可能在下一格就被烧，也可能永远轮不到它上场；比「从未验过」差
         （后者至少可能是个新鲜好地址），但比「验过且失败」强；
      3. 出现过但**没有任何成功记录** —— 切上去就报错、或从没真正服务过。
         排在最后，仅作兜底。
    仍处于惩罚期内的地址由调用方先筛掉，不进本函数比较。
    中间位是 /64 静置判定（见 `_warp_prefix_rest`）：同档内优先选刚烧过的
    /64 之外的地址，跨档不参与比较 —— 档位仍是主判据。
    """
    with _WARP_IP_LOCK:
        entry = _WARP_IP_USED.get(ip) if ip else None
    rest = _warp_prefix_rest(ip, now)
    hot = 1 if (rest is not None
                and rest < _warp().get("prefix_rest_sec", 300)) else 0
    if entry is None:
        return (0, hot, 0.0)
    ok = entry.get("last_ok_ts") if isinstance(entry, dict) else None
    if ok:
        # 取负：调用方用 min()，而「越久没成功越该优先」意味着间隔越大排序值要越小。
        return (1, hot, -(now - float(ok)))
    probe_ok = entry.get("probe_ok_ts") if isinstance(entry, dict) else None
    if probe_ok:
        # 同样取负：验得越久（越接近该结果过期）越该先用一次，否则等到 TTL
        # 过期就白验了。
        return (2, hot, -(now - float(probe_ok)))
    return (3, hot, 0.0)


def _warp_guard_for_reason(reason: str) -> float:
    """按触发原因给出复用窗口（秒）。

    403 用 6h 窗口（出口 IP 信誉标记，六小时刷新）；429 用默认冷却窗口
    （六十秒，解析到精确 Retry-After/body/message 时由 ban_until 覆盖）。
    换线对两类都有效，它们都是出口 IP 级拦截。
    """
    if "abuse403" in reason:
        return _warp()["blocked_guard_sec"]
    if "rate429" in reason:
        return _warp()["rate429_guard_sec"]
    return _warp()["ip_reuse_guard_sec"]


def _warp_rest_time(ip: str | None, now: float) -> float:
    """该出口 IP 的休息时间（距上次使用的秒数）；从未用过视为从纪元起休息。"""
    return now - _warp_ip_last_ts(ip) if ip else now


def _mark_warp_ip_used(ip: str | None, now: float, blocked: bool = False,
                       kind: str | None = None,
                       forget_probe: bool = True,
                       ban_after: float | None = None) -> None:
    """记录出口 IP 的本次使用；blocked=True 表示该 IP 已被 403/429 标记。

    `kind` 为 "abuse403" / "rate429"，决定惩罚期是 6h 还是 24h；缺省时
    该条记录按 24h 保守处理。`ban_after` 是上游 429 响应给出的确切剩余秒数
    （Retry-After），有它时替换该类型的默认惩罚期，见 `_warp_block_remaining`。
顺手清理已出窗口的条目（表长恒定为窗口内 IP 数）。

    `forget_probe=False` 供模型探测自己记账时用：那是**同一次观测**的产物，
    清掉它只会让下次调用重探一遍、白烧一格配额（实测同一地址连探两次）。
    生产侧标记（403/429）才需要作废旧结论 —— 那种情况下台账里的 OK 早于标脏，
    属于「证据比事实旧」，留着会让脏地址被重新选中。

    换线线程与主循环都会写，必须持锁；否则并发 setdefault + 全量落盘会丢更新。
    """
    if not ip:
        return
    with _WARP_IP_LOCK:
        _warp_ip_used_locked(ip, now, blocked, kind, ban_after)
    # 在锁外清：两张台账没有嵌套需求，拿完就放，避免与 _record_warp_cycle
    # 的 IP→CYCLES 顺序交叉成环。
    if blocked and forget_probe:
        _probe_cache_forget(ip)
    if blocked:
        _warp_line_invalidate(ip, now)
    # 历史记在锁外：只追加文件，不碰 _WARP_IP_USED，让磁盘 I/O 不挡换线线程。
    # 只有本函数写历史 —— 每请求成功采样路径也调 _warp_ip_used_locked（为补
    # last_ok_ts），记在那里会让日志按请求量爆炸。
    _append_warp_ip_history(ip, now, blocked, kind)


def _warp_ip_used_locked(ip: str, now: float, blocked: bool,
                         kind: str | None = None,
                         ban_after: float | None = None) -> None:
    """持锁版本的写入主体（调用方必须已持有 _WARP_IP_LOCK）。

    记录 `first_seen` / `blocked_ts` / `served_sec`：只存"最后一次使用时刻"
    无法回答"这个地址活了多久"。实测"休息越久越耐用"不成立（休息 <15min 复用
    存活中位 153s，>60min 反而 23s），所以这些字段只用于事后评估，不参与选线。

    惩罚期按类型分别记在 `blocked_ts_by_kind` 里，解除时间取最晚的一类：
    同一 IP 先 403 后 429 时，两条时钟并行，不会被后一次标记覆盖成更短的
    403 窗口（否则提前 18h 放回候选池）。同一类型在本轮惩罚期内的重复标记
    保留最早的起点；该类型已满期再被烧则是新一轮，起点重新计 —— 否则恢复
    过一次的地址会永远被判为「已恢复」。`blocked_ts` / `blocked_kind` 保留
    作展示与旧记录兼容，取当前最早的一类。

    `ban_until` 来自上游 429 的 Retry-After（探针实测）：它是**确切**恢复
    时刻，优先于该类型的默认窗口；没有观测时仍用 6h/24h 保守值。
    """
    fresh = {"ts": now, "blocked": blocked,
             "blocked_kind": kind if blocked else None, "first_seen": now,
             "blocked_ts": now if blocked else None,
             "blocked_ts_by_kind": {}, "served_sec": None, "last_ok_ts": None,
             "probe_ok_ts": None, "ban_until": None, "ban_kind": None}
    entry = _WARP_IP_USED.setdefault(ip, fresh)
    if not isinstance(entry, dict):
        # 兼容旧格式（float）
        _WARP_IP_USED[ip] = entry = fresh
    entry["ts"] = now
    if blocked:
        # 已满期的类型先剔除：它属于上一轮，重新标记要开新一轮。
        # 只在已标记时读旧起点 —— 干净地址没有惩罚起点，回退链会把 last_ok_ts
        # 当成起点，让本轮惩罚期从上次成功时刻起算。
        prev_ban_until = _as_ts(entry.get("ban_until"))
        prev_ban_kind = entry.get("ban_kind")
        prev = _warp_block_stamps(entry) if entry.get("blocked") else {}
        stamps = {}
        for k, t in prev.items():
            until = t + _warp_block_ttl(k)
            if prev_ban_kind == k and prev_ban_until is not None:
                # 精确封禁替换默认窗口：到期即算上一轮结束，新 429 必须重开一轮。
                until = prev_ban_until
            if now < until:
                stamps[k] = t
        new_round = not stamps
        eff_kind = kind if kind in ("abuse403", "rate429") else "rate429"
        # 必须在 setdefault 之前判断：否则本轮新写入的 rate429 stamp 会让
        # 「上一轮 ban_kind 已不在 stamps」永远为假，过期 ban_until 留在原地
        # 直接盖掉新 429 的 24h 默认窗口（实测：Retry-After 到期后再烧，
        # `_warp_ip_is_blocked` 立刻返回 False）。
        if prev_ban_kind is not None and prev_ban_kind not in stamps:
            entry["ban_until"] = None
            entry["ban_kind"] = None
        stamps.setdefault(eff_kind, now)
        entry["blocked_ts_by_kind"] = stamps
        entry["blocked_ts"] = min(stamps.values())
        entry["blocked_kind"] = min(stamps, key=stamps.get)
        if new_round:
            entry["served_sec"] = now - float(entry.get("first_seen") or now)
        # 只有 429 有可信的 Retry-After；403（flagged）仍按 6h 窗口，
        # 不能被响应里偶然出现的 rate-limit 头缩短。
        if ban_after is not None and eff_kind == "rate429":
            secs = _as_ts(ban_after)
            if secs is not None and secs > 0:
                secs = min(max(secs, 1.0), 86400.0)
                until = now + secs
                prev_until = _as_ts(entry.get("ban_until"))
                entry["ban_until"] = max(until, prev_until) if prev_until and prev_until > now else until
                entry["ban_kind"] = "rate429"
    entry["blocked"] = entry.get("blocked", False) or blocked
    w = _warp()
    cutoff = now - w["ip_reuse_guard_sec"]
    # 被标记过的 IP 保留到惩罚期结束：取两档恢复期（403 6h）与 blocked_ttl_sec
    # 的最大值兜底；解析到精确封禁的条目由下方 ban_until > now 单独保护，
    # 不会因默认窗口缩短（429 现在 60s）被提前清掉放回候选池。
    blocked_cutoff = now - max(w.get("blocked_guard_sec", 86400),
                               w.get("rate429_guard_sec", 86400),
                               w.get("blocked_ttl_sec", 86400))
    for k in list(_WARP_IP_USED):
        v = _WARP_IP_USED[k]
        # 上游给出长于默认窗口的确切封禁时，条目必须留到 ban_until。
        ban_until = _as_ts(v.get("ban_until")) if isinstance(v, dict) else None
        if ban_until is not None and ban_until > now:
            continue
        ts = v.get("ts") if isinstance(v, dict) else v
        keep = blocked_cutoff if (isinstance(v, dict) and v.get("blocked")) else cutoff
        if ts < keep:
            del _WARP_IP_USED[k]
    _save_warp_ip_used()

def _warp_model_usable(member: dict, ip: str) -> bool:
    """刷新后的模型层验收：这条线此刻能否真正服务 opencode 免费层。

    原模式（refresh_model_check=False）只看 IP 新鲜度。探测与 CLI
    `probe_warp.py` 共用同一份指纹定义；每次探测消耗该出口一格配额。

    同一地址在 TTL 内只探一次：重复探测同一结论只是白烧配额（实测 26 次同一
    IP 全是 quota）。台账按「tag + /128」键控，TTL 按结论持久性分档；已被台账
    判脏（惩罚期内）的地址在入口处直接跳过，不再按线重探。
    """
    w = _warp()
    if not w.get("refresh_model_check", True):
        return True
    tag = member["tag"]
    if _warp_ip_is_blocked(ip):
        # 台账（403/429 惩罚期）已经判这个地址脏，再探一次只是把同一个结论
        # 再买一遍；脏 IP 不耗额度是既有事实，不是重启后的推断。
        _PROBE_LAST_VERDICT[tag] = "blocked"
        log(f"  · {tag} 出口 {ip} 台账已判脏，跳过实测"
            f"{_warp_refresh_count_note(tag)}")
        return False
    cached = _probe_cache_get(tag, ip)
    # 只复用「地址已被上游拒绝」且封禁仍在生效的结论；OK 绝不能跳过实测。
    # OK 的 TTL 是 600s，而地址被生产流量烧掉只需几秒 —— 命中旧 OK 等于把
    # 已知会 429 的地址直接切上去，实测每轮换线后的 429 爆发都落在这种线上。
    # 负结论也要看 ban_until：上游给过 Retry-After 时，它可能比缓存 TTL 先到期；
    # 此时必须重探，否则「精确解禁」在模型模式下会被缓存多锁住最多 1.4h。
    if cached in _WARP_PROBE_PERSISTENT and _warp_ip_is_blocked(ip):
        # 记录本条目的结论，供 `_note_probe_batch` 判定整批失明；否则被缓存
        # 跳过的 tag 会沿用上一次（可能是别的地址）的旧值，掩盖全批异常。
        _PROBE_LAST_VERDICT[tag] = cached
        log(f"  · {tag} 出口 {ip} TTL 内已探过: {cached}（跳过）"
            f"{_warp_refresh_count_note(tag)}")
        return False
    if _warp_probe_blind():
        # 通道失明时不探、也不否决：探出来的只会是通道的故障，还要白烧一格配额。
        # 此刻「能不能用」没有观测来源，退回历史判据（调用方据
        # `_ip_history_is_truth` 走三档与复用窗口）。
        log(f"  ⚠ {tag} 出口 {ip} 探测通道失明，跳过实测改用历史判据")
        return True
    # 探测要 1.4~8.7s，期间该地址可能已被 403/429 标脏并作废台账。带上起手
    # 时刻，让 `_probe_cache_put` 拒收这种「证据比事实旧」的结论（见其 docstring）。
    started_at = time.time()
    try:
        import probe_warp
        verdict, _sec, _detail, retry_after = probe_warp.probe(
            member["port"], w.get("refresh_model_timeout_sec", 15))
    except Exception as e:
        # 探测抛异常与 unreachable 同源（探测器自己没跑通）。必须记进本批
        # 结论，否则整批异常时 `_note_probe_batch` 看到的是旧 OK/空串，
        # 失明机制失效，每条线都被判「地址坏了」白重启。
        _PROBE_LAST_VERDICT[tag] = "unreachable"
        log(f"  ✗ {tag} 模型探测异常: {e}")
        return False
    if not _probe_cache_put(tag, ip, verdict, started_at=started_at):
        # 也不记 last_ok_ts：那会把已烧地址重新认证成「刚成功过」，下一轮选线
        # 照样按次级优质把它排回候选池首位。
        log(f"  ✗ {tag} 出口 {ip} 实测 {verdict} 起手早于该地址标脏，结论作废")
        return False
    _PROBE_LAST_VERDICT[tag] = verdict
    ok = verdict == "OK"
    # 只有 quota(429) 的 Retry-After 会写进惩罚期；403 带限流头不算。
    retry_after = retry_after if verdict == "quota" else None
    hint = (f"（Retry-After {retry_after / 3600:.1f}h）"
            if retry_after and retry_after > 0 else "")
    log(f"  {'✓' if ok else '✗'} {tag} 出口 {ip} 模型层: {verdict}{hint}"
        f"{_warp_refresh_count_note(tag)}")
    if ok:
        # 实测通过 = 这条地址此刻确实能服务，记进 `probe_ok_ts`。否则同址仍停在
        # 档位 3（一次 OK 都没拿到），后台会把刚验过的线当低质反复重启，白烧配额。
        now = time.time()
        # 字典改动与落盘必须在同一把锁内：`_warp_model_test_many` 会并发调
        # 本函数，放锁后再写会让较旧的快照覆盖较新的台账。
        with _WARP_IP_LOCK:
            entry = _WARP_IP_USED.get(ip)
            if not isinstance(entry, dict):
                entry = _WARP_IP_USED[ip] = {"ts": now, "blocked": False,
                                             "first_seen": now}
            # WHY 不写 `last_ok_ts`：那个字段的语义是「该地址真的撑过生产流量」，
            # 由 `_record_active_ip_success` 独占。探针 OK 只说明「此刻配额还没
            # 耗尽」，下一个请求就可能被烧，也可能永远轮不到它上场。混写会让
            # `_warp_ip_rank` 把「只在空跑里验过」的地址排进次级优质档，与真正
            # 撑过流量的地址同权 —— 实测台账 2945 条里 738 条属于这种「验过没用过」。
            entry["probe_ok_ts"] = max(float(entry.get("probe_ok_ts") or 0), now)
            _save_warp_ip_used()
    elif verdict in _MODEL_VERDICT_BLOCK_KIND:
        # 模型层判 quota/flagged 与生产侧 429/403 是同一件事（前者是
        # FreeUsageLimitError，后者是 abusive traffic），都该落进 blocked 台账：
        # 光记探测缓存的话，「脏 IP 在探测之前就排除」这条设计对模型模式完全失效。
        # 实测 7 个烧毁段里 648 个 /128 被逐个探测、608 个（94%）判 quota —— 段内
        # 每个新地址都要白烧一格配额才被认出来，而段级静置本可以提前挡住。
        _mark_warp_ip_used(ip, time.time(), blocked=True,
                           kind=_MODEL_VERDICT_BLOCK_KIND[verdict],
                           forget_probe=False, ban_after=retry_after)
    return ok


def _refresh_idle_warp(member: dict, reason_hint: str | None = None) -> str | None:
    """重启隧道，直到拿到一条可用的出口 IP；失败返回 None。

    单次重启可能拿回同一地址（WARP 会话未真正重连），故按 max_attempts 重试。
    「可用」的定义随模式变：模型模式看实测探测，原模式看窗口内未使用过。
    调用方要么据此跳过这条线，要么（闲置线）留给下次切回前的校验再刷新。

    刷新原因由该线当前出口的低质类型推导（与 `_warp_ip_needs_refresh` 同源）：
    「都刷一样」看不出为什么刷，事后无法判断某条线是被 429 拖住还是纯配额耗尽。
    """
    t = member["tag"]
    # 还要不要等：看这条线**有没有连接**，不是看离渠多久。
    # 时间下限只在有连接时才生效（见 `_standby_defer_reason`）：空壳线白等
    # 两分钟毫无意义，而有连接的线哪怕过了 120s 也可能还在排空。
    defer = _standby_defer_reason(t)
    if defer:
        log(f"  ⏸ {t} {defer}，暂不重启")
        return None
    # 调用方可能已算好原因时传进来（脏线守护在清空旧 IP 之前算的）；否则自己推导。
    reason = (reason_hint
              or _warp_ip_refresh_reason(_warp_line_ip(t), time.time(), t)
              or "主动刷新")
    off = _WARP_OFF_AGE.get(t)
    reason = (f"离渠刷新/{reason}" if off is not None
              else f"预热/{reason}")
    backoff = _warp()["retry_backoff_sec"]
    for attempt in range(1, _warp()["max_attempts"] + 1):
        _invalidate_warp_line_ip(member["tag"])  # 重启后旧地址必然失效
        try:
            ok = (_restart_service(member["service"], member["tag"])
                  and _wait_port_ready(member["port"], _warp()["ready_timeout_sec"]))
        except Exception as e:
            log(f"  ✗ {member['tag']} 刷新异常: {e}")
            return None
        if ok:
            # 短超时：这是刚重启的隧道，不是已稳定的线。见 config 同名参数注释。
            ip = _probe_refresh_egress(member["port"])
            # 模型模式只看实测：IP 历史（脏/复用窗口）不再是否决项，它只在
            # 原模式里当判据。见 _ip_history_is_truth。
            now = time.time()
            if not ip:
                log(f"  ✗ {t} 刷新 #{attempt}{_warp_refresh_progress(t)}"
                    f" 原因={reason} 出口探测失败")
            elif _ip_disqualified(ip, now, _warp()["ip_reuse_guard_sec"]):
                log(f"  ✗ {t} 刷新 #{attempt}{_warp_refresh_progress(t)}"
                    f" 原因={reason} 出口仍在复用窗口内: {ip}")
            elif _warp_model_usable(member, ip):
                log(f"  ✓ {t} 刷新成功{_warp_refresh_progress(t)}"
                    f" 原因={reason} 新出口 {ip}")
                _set_warp_line_ip(member["tag"], ip)
                # 记下这次验收：脏线守护据此静置，不再 5s 后又把同一条刚刷好的线
                # 派出去重探一遍（那会白烧一格配额）。
                # 记下这次验收的**时刻和地址**：脏线守护据此静置，不再 5s 后又
                # 把同一条刚刷好的线派出去重探一遍（那会白烧一格配额）；等 usque
                # 自己重连换了址，地址对不上就重新走判定 —— 那正是该复检的时刻。
                _WARP_LAST_ACCEPT[t] = (now, ip)
                return ip
        else:
            log(f"  ✗ {member['tag']} 刷新后端口未就绪")
        time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
    return None


def _refresh_line_serialized(member: dict) -> tuple[str | None, bool]:
    """同步刷新一条线，与后台补刷/按线 worker 共用按线锁。

    返回 ``(ip, acquired)``；``acquired=False`` 表示这条线正在别处刷新，
    本轮不该重启它（重启只会拿到同一会话，白烧一次探测与一次在途中断）。
    """
    tag = member["tag"]
    if not _acquire_refresh_slot(tag):
        return None, False
    try:
        return _refresh_idle_warp(member), True
    finally:
        _release_refresh_slot(tag)


_dirty_janitor_started = False


def _reclaim_dirty_pass() -> None:
    """一次脏线巡检：为每条非当前出口、未在补刷的线启动探测/重连。

    当前出口不在此处理：重启它会掐断在途连接，该由换线路径换下后再刷。
    """
    active = active_warp_tag()
    for m in _warp()["members"]:
        if m["tag"] == active:
            continue
        with _WARP_REFRESH_LOCK:
            # 排队中等空闲的线（PENDING）仍可用，不该挡掉探测：它随时可能被
            # 换线选中，届时 `_retry_refresh_idle_warp` 会因「已选为当前出口」退出。
            # 离渠冷却期内的线不探也不重启：它刚被换下，在途请求可能仍在走它，
            # 重启即掐断（upstream connection lost）。闸门放在这里而不是 worker
            # 内部，省掉每轮一次白跑的出口探测。空壳线不受影响 —— 冷却本身
            # 只在有连接时才生效（见 `_standby_defer_reason`）。
            # 刚验收过就歇一会儿：worker 之间不共享「这条线刚刷好」这个事实，
            # 上一个 worker 拿到 OK 后 return，5s 后本轮又派一个新 worker ——
            # 它探到的还是那个地址，而 usque 随时可能自己重连换址，换成的新地址
            # 在台账里没有结论就按「从没成功过」判脏，于是每 5s 白烧一格配额。
            # 实测 WARP-CF-C 67 分钟内 20 次重启里 18 次判 quota、只有 3 次 OK。
            # 静置窗口取与 `probe_cache_ttl_sec` 同量级：短于它只会重复看同一个地址。
            #
            # 静置窗口只在「地址仍是验收时那个」时生效 —— 记录里带了地址，就是
            # 为了区分这两种情况。usque 自重连换址是常态（相邻两次判定间隔 10~30s
            # 时多数已换址），换掉的地址即便在台账里仍是「已验证好 IP」，池子也已经
            # 指向一个不存在的出口。所以地址一变就必须重新判定，不能继续静置到窗口
            # 结束 —— 否则脏线守护会整段窗口内放着一个已换址的线不管。
            acc = _warp_last_accept(m["tag"])
            if (acc and acc[1] == _warp_line_ip(m["tag"])
                    and time.time() - acc[0] < _warp().get("probe_cache_ttl_sec", 12)):
                continue
            if (m["tag"] in _WARP_REFRESHING or m["tag"] in _WARP_REFRESH_PENDING
                    or m["tag"] in _WARP_PROBING
                    or time.time() < _WARP_BUSY_UNTIL.get(m["tag"], 0)
                    or _standby_defer_reason(m["tag"])):
                continue
            _WARP_PROBING.add(m["tag"])
        threading.Thread(target=_reclaim_dirty_warp_worker, args=(m,),
                         daemon=True, name=f"warp-reclaim-{m['tag']}").start()


def _dirty_line_janitor() -> None:
    """常驻脏线守护：没有周期闸门，只要存在脏 IP 就一直重连到换出干净地址。

    WHY 不用周期任务：实测出口 IP 存活中位 12.5s，任何按分钟计的闸门都意味着
    脏线会滞留远超一个 IP 的寿命，换线时可选干净线随之变少。5s 只是防打爆
    （每条线每轮一次探测），不是调度周期。
    """
    while True:
        try:
            _reclaim_dirty_pass()
        except Exception as e:
            log(f"✗ 脏线守护异常: {e}")
        time.sleep(5)


def _line_worker_config() -> dict:
    """warp.line_worker 的读点。每次现读 CFG，便于测试热改配置。"""
    return _warp().get("line_worker") or {}


def _line_worker_enabled() -> bool:
    return bool(_line_worker_config().get("enabled"))


def _line_worker_dry_run() -> bool:
    """缺省 dry_run=True：没显式关掉之前，worker 只观测。"""
    return bool(_line_worker_config().get("dry_run", True))


def _line_worker_owns_scheduling() -> bool:
    """worker 是否接管旧守护。dry_run 只观测，旧守护必须继续干活。

    灰度期若只凭 `enabled` 就让三条旧路径让路，worker 又只打「应…」不执行，
    窗口内就没有任何预热/补刷/脏线重连 —— 观测到了问题却没人处理。
    """
    return _line_worker_enabled() and not _line_worker_dry_run()


def _start_line_workers() -> None:
    """每条隧道起一个常驻 worker，接管「判质量 → 重启 → 验收 → 换线」。

    非 dry-run 启用后旧的三个调度器会互相让路：不再起脏线抽检，主循环也不再调
    `_prewarm_standby_warp`，`_schedule_idle_refresh` 直接返回 —— 否则同一条
    线会被两套逻辑各重启一次，白烧一次出口探测。dry_run 期旧路径继续运行，
    否则灰度窗口内没有任何实际维护动作。
    """
    import warp_line_worker
    dry_run = _line_worker_dry_run()
    interval = _line_worker_config().get("interval_sec", 30)
    members = _warp()["members"]
    warp_line_worker.start(sys.modules[__name__], members,
                           dry_run=dry_run, interval_sec=interval)
    log(f"✓ WARP 线 worker: {len(members)} 条, dry_run={dry_run}, "
        f"间隔={interval}s")


def _start_dirty_line_janitor() -> None:
    global _dirty_janitor_started
    with _WARP_REFRESH_LOCK:
        if _dirty_janitor_started:
            return
        _dirty_janitor_started = True
    threading.Thread(target=_dirty_line_janitor, daemon=True,
                     name="warp-janitor").start()


def _reclaim_dirty_warp_worker(member: dict) -> None:
    """探测该线出口；确认是脏 IP 才占用补刷标记并重启，直到换出干净地址或超时。"""
    try:
        deadline = time.time() + _warp()["ip_refresh_retry_sec"]
        while time.time() < deadline:
            ip = warp_egress_ip(member["port"])
            if not ip:
                time.sleep(3)
                ip = warp_egress_ip(member["port"])
            # 探到地址不等于地址优质：被 403/429 标记过、或用过却从没有过
            # 成功流量的线仍是低质，必须换掉。判据与预热、补刷同源。
            if ip and not _warp_ip_needs_refresh(ip, time.time(), member["tag"]):
                # 地址没变、且这条线已验收过它就免测：usque 自重连是换址的**唯一**
                # 来源，地址一样就意味着还是那个验证过的会话，重探只是白烧一格。
                # 比对源必须是验收记录本身 —— 拿缓存 `_warp_line_ip` 当第二个源
                # 会两头失真：缓存被 `_invalidate_warp_line_ip` 清空时，同一个已
                # 验地址会被当作「没验过」反复重探；而换线路径写缓存时不更新验收
                # 记录，缓存里的新地址会冒用旧地址的验收结论。
                acc = _warp_last_accept(member["tag"])
                if acc and acc[1] == ip:
                    _WARP_LAST_ACCEPT[member["tag"]] = (time.time(), ip)
                    return
                # 但「不在台账里」只是「没被证伪」，不是「已验证」：模型模式下
                # 还要当场实测，否则池子里存的全是没验过的地址。实测首探 OK 率
                # 全新地址 5.47%（56/1024）、T2 0.13%（1/795），而成功过的地址
                # 95.44% —— 不验收等于把池子交给最差的那一档，换线时才逐条付
                # 学费（实测 1817 次首探判 quota、只有 1268 次 OK）。
                # 这也让 `_WARP_LAST_ACCEPT` 名副其实：记录的是「验收通过」。
                if (_ip_history_is_truth()
                        or _warp_model_usable(member, ip)):
                    _set_warp_line_ip(member["tag"], ip)
                    _WARP_LAST_ACCEPT[member["tag"]] = (time.time(), ip)
                    return
            # 探测期间这条线可能已被换线线程切成当前出口：此刻重启会掐断在途
            # 连接（upstream connection lost）。判据必须落在重启之前。
            if active_warp_tag() == member["tag"]:
                log(f"  ↻ {member['tag']} 已被选为当前出口，取消脏线重连")
                return
            # 只有确认脏才抢占重生名额：探测阶段不能占，否则干净线被当成
            # 「不可选」，换线会报「备线 0 条」直接落池。
            # 抢不到说明这条线正被另一条线程刷，该让出：不该两条线程同时重启它。
            # 重启会掐断这条线上所有在途 SSE（9router 注入 upstream
            # connection lost）。脏线不在服务流量时才刷得安全。
            # 旧实现没有这一步，02:44 换线后 19 秒把 CF/B/C 全部重启，
            # 正好掐在刚被换下、仍在传流的连接上。
            # 判在抢名额之前：探测要 2s，占着名额会让换线路径误报「名额已满」。
            if warp_line_busy(member["tag"]) is not False:
                # 交给守护下一轮（refresh_delay_sec=20s 后）重测：留在这里
                # 死等会占住一个线程最长 600s，而每 2s 一次连接查询在 20 条
                # 线上叠加曾把 load 推到 3.4。等不到空闲说明它仍在服务流量，
                # 20s 的复测节奏已经足够早 —— 真正要救的是补刷路径的 90s。
                _WARP_BUSY_UNTIL[member["tag"]] = time.time() + _warp()["refresh_delay_sec"]
                return
            if not _acquire_refresh_slot(member["tag"]):
                time.sleep(_warp()["refresh_delay_sec"])
                continue
            try:
                _t = member["tag"]
                _why = _warp_ip_refresh_reason(ip, time.time(), member["tag"]) or "疑似脏 IP"
                log(f"  ↻ {_t} 出口 {ip or '探测失败'} 刷 IP{_warp_refresh_progress(_t)}"
                    f" 原因={_why}")
                _invalidate_warp_line_ip(member["tag"])
                if _refresh_idle_warp(member, reason_hint=_why):
                    return
            finally:
                _release_refresh_slot(member["tag"])
            # 刷新失败时隧道已被重启或冷却闸门挡下，缓存的地址不再代表这条线
            # 当前的样子 —— 下一轮重新探测，免得拿旧结论反复刷同一条线。
            ip = None
            time.sleep(2)
        log(f"  ✗ {member['tag']} {_warp()['ip_refresh_retry_sec']}s 内未换出干净出口")
    finally:
        with _WARP_REFRESH_LOCK:
            _WARP_PROBING.discard(member["tag"])
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
        # 已经拿到优质 IP 就不要再重启：`_refresh_idle_warp` 每成功一次就换掉
        # 一个会话，重复调用等于把刚预热好的地址白白烧掉。但「有地址」不等于
        # 「优质」—— 模型模式下原先见 IP 就返回，实测 239 次 ok 全是空转，
        # 脏线一次都没真刷过。
        known = _warp_line_ip(member["tag"])
        if known and not _warp_ip_needs_refresh(known, time.time(), member["tag"]):
            _bump_refresh_stat("ok")
            return True
        # 冷却期内也要常回来测：连接随时可能结束，30s 一觉会让这条线在旧 IP 上
        # 多占半分钟，备线池随之变窄。1s 与 _wait_warp_idle 的轮询节奏对齐，
        # 更短只是在 Clash API 上空转（单次查询 5s 超时）。
        # 日志只在该线首轮被拦时打一行，免得每 1s 刷屏。
        defer = _standby_defer_reason(member["tag"])
        if defer:
            _bump_refresh_stat("retry")
            if not _WARP_DEFER_LOGGED.get(member["tag"]):
                log(f"  ⏸ {member['tag']} {defer}，冷却中每 1s 复测")
                _WARP_DEFER_LOGGED[member["tag"]] = True
            time.sleep(1)
            continue
        _WARP_DEFER_LOGGED.pop(member["tag"], None)
        idle = _wait_warp_idle(member, window=_DRAIN_BUSY_WINDOW_SEC)
        # 判在重启之前而不是本轮开头：换线线程的快照可能早于本次调度，等空闲的这
        # 40s 内这条线也可能被切成当前出口。此刻重启它就是掐断正在服务的在途连接
        # （upstream connection lost）；它下次被换下时 reconnect_warp 会重新调度
        # 补刷。非空闲分支不查当前出口 —— 那一路本就不会重启。
        if idle and active_warp_tag() == member["tag"]:
            log(f"  ↻ {member['tag']} 已被选为当前出口，取消本次刷新")
            return False
        if idle:
            # 名额只在真正重启前抢：等空闲的那 90s 里这条线还能服务流量，
            # 占着名额会让其余备线永远排不上队（详见 _WARP_REFRESHING 注释）。
            if not _acquire_refresh_slot(member["tag"]):
                _bump_refresh_stat("retry")
                # 名额只按 tag 独占，撞上只说明这条线正被另一条线程刷。
                # 旧文案报的是上限常量并说「N 条在重启」，读日志会误以为被全池挤掉。
                log(f"  ⏸ {member['tag']} 已被其他线程刷，本轮让出")
                # 让出不该睡满 idle_wait_sec(90s)：单次刷新只要 8~15s，
                # 另一条线程刷完这条线通常几十秒内就有结果，睡 90s 只是把
                # 复测推后。按 refresh_delay_sec(20s) 的节奏回来即可。
                time.sleep(_warp()["refresh_delay_sec"])
                continue
            try:
                if _refresh_idle_warp(member):
                    _bump_refresh_stat("ok")
                    return True
            finally:
                _release_refresh_slot(member["tag"])
        _bump_refresh_stat("retry")
        # 不在这里补睡：`_wait_warp_idle` 已经每 1s 测了一次、连续忙满 90s 才
        # 走到这里，再睡 90s 等于把复测间隔拉成 180s —— 连接结束时该线仍占着
        # 旧 IP 和资源。直接进下一轮，下一轮开头就重新查连接。
        log(f"  ⚠ {member['tag']} 连续 {_warp()['idle_wait_sec']}s 仍在传数据，继续复测")
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
    if _line_worker_owns_scheduling():
        # 常驻 worker 每轮都会判这条线；这里再排一次会与 worker 抢同一条隧道。
        # dry_run 不接管：worker 只观测，旧路径继续按原语义预热/补刷。
        return
    with _WARP_REFRESH_LOCK:
        # 只登记排队，不占名额：这条线在等到空闲之前一直能服务流量。
        if member["tag"] in _WARP_REFRESH_PENDING or member["tag"] in _WARP_REFRESHING:
            return
        _WARP_REFRESH_PENDING.add(member["tag"])

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
                _WARP_REFRESH_PENDING.discard(member["tag"])

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
    # 模型模式下历史不算数：能探到地址就可回切，可用性由换线路径实测决定。
    if not _ip_history_is_truth():
        return any(ips.values())
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

    # 2. 落池后仍在被出口级拦截。必须传大 limit：判据是「落池以来累计 ≥5 条」，
    # 默认窗口 recent_window=2 只能数到 2 条，永远凑不满 5 → 14:47 落池后
    # 30 分钟里 1590 条 429 也回不去 WARP。
    # blind 模式无 9router 数据可看（流量不经它），判据 2 整条跳过，回切只看
    # 池探测与黑名单占比 —— 纯网络判据。
    since = _proxy_dwell_since or 0
    if _observe_9router() and since:
        blocked = count_warp_blocked_since(since, limit=200)
    else:
        blocked = {"abuse403": 0, "rate429": 0, "ok": 0, "other_err": 0,
                   "consec_err": 0, "full_err": False}
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
    global _proxy_dwell_since, _last_warp_switch_ok_ts, _last_proxy_fallback_ts
    if not _warp().get("proxy_fallback_enabled", False):
        # 落池已禁用（2026-10-26）：池内节点全不可用，落池即断流。
        # 没有当前线可重连时直接放弃，交给下一轮错误重试。
        log("⚠ 落池已禁用，改为原地重连 WARP 线")
        if active is None:
            return False
        return _restart_warp_inplace(active, old_ip)
    cooldown = _warp()["proxy_fallback_cooldown_sec"]
    last_ts = _load_proxy_fallback_ts()
    since_last = now - last_ts
    # ts=0 表示从没落过池（无状态文件），不能当成「刚好在冷却刚开始」。
    if last_ts > 0 and since_last < cooldown:
        remain_min = int((cooldown - since_last) / 60) + 1
        # 冷却期内改切其它 WARP 成员。这里是有意的盲切，不探 IP 新鲜度：
        # 备选是让流量停在一个正在被拦的出口上，盲切至少换一次会话机会。
        # 天花板：可能落到一个已烧的 IP，届时由 403 触发下一次错误换线兜住。
        for m in _warp()["members"]:
            if active is not None and m["tag"] == active["tag"]:
                continue
            if not _port_open(m["port"]) or not _switch_warp(m["tag"]):
                continue
            _enter_segment(time.time(), None)
            _proxy_dwell_since = 0
            log(f"↻ 轮换池冷却中（剩 {remain_min}min），改切 WARP 隧道 {m['tag']}")
            return True
        if active is None:
            return False
        return _restart_warp_inplace(active, old_ip)
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
    _last_proxy_fallback_ts = landed
    _write_json_atomic(_proxy_fallback_path(), {"ts": landed})
    # 错误触发的换线同样是一次「本段结束」：主动轮换的计时起点必须重置，
    # 否则刚落池就按上一段的已服务时长判到寿，秒级再切一次（实测 00:12:00
    # 落池后 22s 就报了 27s≥27s）。
    _enter_segment(landed, None)
    _proxy_dwell_since = landed     # 落池时刻：回切判据只统计此后的拦截
    log("✓ 已切到轮换池 proxy（WARP 隧道全部不可用）")
    return True


def _restart_warp_inplace(member: dict, old_ip: str | None) -> bool:
    """单线兜底：原地重启当前隧道换会话地址（有几秒断流）。"""
    global _last_warp_switch_ok_ts, _ACTIVE_WARP_IP
    backoff = _warp()["retry_backoff_sec"]
    for attempt in range(1, _warp()["max_attempts"] + 1):
        log(f"  ↻ {member['tag']} 原地重连 #{attempt}")
        _invalidate_warp_line_ip(member["tag"])
        ok = (_restart_service(member["service"], member["tag"])
              and _wait_port_ready(member["port"], _warp()["ready_timeout_sec"]))
        if ok:
            new_ip = _probe_refresh_egress(member["port"])
            now = time.time()
            # 原地重连是落池也失败后的最后兜底。新旧会话相同时重试是死循环，
            # 与本模式无关；地址换了之后的历史判据/实测则按当前模式走。
            if (new_ip and new_ip != old_ip
                    and not _ip_disqualified(new_ip, now,
                                             _warp()["ip_reuse_guard_sec"])
                    and _warp_model_usable(member, new_ip)):
                log(f"✓ WARP 出口已更换: {old_ip or '未知'} -> {new_ip}")
                # selector 没变（还是在同一条线上），但会话地址换了：不更新的话
                # 健康行会一直显示旧 IP，成功流量打点也记在旧地址上。
                _enter_segment(now, new_ip)  # 本段结束：主动轮换计时重新开始
                return True
            log(f"  ✗ {member['tag']} 出口不新鲜: {new_ip or '探测失败'}")
        else:
            log(f"  ✗ {member['tag']} {_warp()['ready_timeout_sec']}s 内未就绪")
        time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
    return False


def _reconnect_warp_by_table(reason: str) -> bool:
    """只读状态表换线：无 120s 内 OK 就等待，不现场探测、不重启、不落池。"""
    global _ACTIVE_WARP_IP, _proxy_dwell_since, _last_warp_switch_ok_ts
    now = time.time()
    active = _warp_member(active_warp_tag())
    old_ip = _ACTIVE_WARP_IP if active else None
    if active:
        _mark_warp_ip_used(old_ip, now)
        log(f"↻ WARP 换线 ({reason})：当前 {active['tag']} 出口={old_ip or '未知'}")
    else:
        log(f"↻ WARP 换线 ({reason})：当前在轮换池 proxy，尝试切回 WARP 隧道")
    cands = _warp_line_fresh_candidates(
        active["tag"] if active else None, now)
    if not cands:
        deadline = now + float(_warp().get("switch_wait_sec", 20))
        log("  · 暂无 120s 内 OK 的备线，等待探测器写入")
        cands = _warp_line_wait_fresh(
            active["tag"] if active else None, deadline)
    if not cands:
        log("  ⚠ 等待超时仍无新鲜备线，本次不切（不重启、不落池）")
        return False
    cands = [(m, ip) for m, ip in cands if ip != old_ip]
    if not cands:
        log("  ⚠ 新鲜备线均与当前出口同址，本次不切")
        return False
    for m, ip in cands:
        if not _acquire_refresh_slot(m["tag"]):
            continue
        try:
            if not _warp_line_ok_fresh(m["tag"], time.time()):
                continue
            if _warp_ip_is_blocked(ip, time.time()):
                continue
            if m["tag"] == active_warp_tag():
                continue
            if not _switch_warp(m["tag"]):
                continue
            switched_at = time.time()
            _mark_warp_ip_used(ip, switched_at)
            _warp_line_selected(m["tag"], ip, switched_at)
            _enter_segment(switched_at, ip)
            _proxy_dwell_since = 0
            _from = active["tag"] if active else "proxy"
            log(f"✓ WARP 换线成功: {_from} -> {m['tag']} (出口 {ip})")
            return True
        finally:
            _release_refresh_slot(m["tag"])
    log("  ⚠ 新鲜备线切换均失败，本次不切")
    return False


def reconnect_warp(reason: str) -> bool:
    """把 opencode 流量换到另一条出口新鲜（窗口外）的隧道上。

    备线在窗口内用过就先重启它换会话 IP（此时它闲置，重启安全），再切过去
    （PUT selector 秒级、在途请求不断）。备线都不可用才原地重连当前线
    （有几秒断流）。

    不刷新刚换下的那条：在途请求仍走它，重启会掐断连接。
    """
    if _PROBER_STARTED:
        return _reconnect_warp_by_table(reason)
    global _ACTIVE_WARP_IP, _proxy_dwell_since, _last_warp_switch_ok_ts
    members = _warp()["members"]
    now = time.time()
    # 已停在轮换池 proxy 上时 active 为 None：挑一条新鲜隧道切回去即可。
    active = _warp_member(active_warp_tag())
    # 当前出口 IP 走内存缓存，不再同步探测：换线关键路径上每多一次 curl 就是
    # 1.2~7s，而它只用于给旧地址打 blocked 标记。缓存缺失时才探测一次兜底。
    old_ip = _ACTIVE_WARP_IP if active else None
    if active and not old_ip:
        old_ip = warp_egress_ip(active["port"])
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
    # 只排除**正在重启**的线：那几秒它确实不可用，切过去会掐断在途请求。
    # 排队等空闲的（PENDING）不在排除范围 —— 它还在服务，且换线选中它时
    # `_retry_refresh_idle_warp` 会看到「已是当前出口」自行退出。
    # 旧实现排除整个 REFRESHING 集合，而排队段一占就是 ~300s（等不到空闲），
    # 等于把 11 个成员的池子常年压缩到 1 条候选（实测日志 25 次「备线 1 条」）。
    with _WARP_REFRESH_LOCK:
        restarting = set(_WARP_REFRESHING)
    standby_all = standby
    standby = [m for m in standby if m["tag"] not in restarting]
    # 全部备线都在重启：没有候选不是「无备线」，而是它们几秒后就会带着新会话回来。
    # 并发不限后这个窗口必然出现（预热波次会同时拉起全部脏线），而落池有 3h 冷却、
    # 池本身也受限流 —— 宁可本次不刷新，等下一次错误重试。
    if standby_all and not standby:
        log("  ⚠ 备线均在后台重启，本次换线不刷新（等下一次错误重试）")
        return False
    guard = _warp_guard_for_reason(reason)

    probes: list[tuple[dict, str]] = []   # (member, ip)，探测到出口的备线
    blocked_lines: list[dict] = []        # 出口已被标记、待刷新换 IP 的备线
    # 快速路径：脏线守护刚探过的地址直接拿来用，把 2~7s 的同步探测移出关键路径。
    # 没有可用缓存时才退回逐条探测（缓存过期、守护在重启隧道等）。
    fast = _cached_warp_standby(standby, now, guard)
    if fast:
        # 全部候选都要收下，不能只取第一条：缓存命中即跳过探测是快的，但一条
        # 候选意味着「这条不合格就只能落池」——10 条隧道下其余 9 条白起了。
        # fast 已按选线优先级排好序，后面按顺序取第一条能用的即可。
        probes.extend(fast)
        log(f"  ⚡ 缓存命中 {len(fast)} 条备线（首条 {fast[0][0]['tag']} 出口 "
            f"{fast[0][1]}，{now - _warp_line_ip_ts(fast[0][0]['tag']):.0f}s 前验证），优先复用")
    # 缓存只覆盖 12s 内验过出口的线。命中不足 2 条时候选池只剩 0~1 条，
    # 唯一候选在模型层测不过就直接落池 —— 此时补探未命中的线，用 2~7s 换
    # 候选广度；≥2 条则保持纯缓存路径，不为广度牺牲换线延迟（e72cc72 的取舍）。
    cached_tags = {m["tag"] for m, _ in fast}
    rest = ([m for m in standby if m["tag"] not in cached_tags]
            if len(fast) < 2 else [])
    if not rest and fast:
        log("  ⚡ 缓存候选充足，跳过探测")
    if rest:
        if fast:
            log(f"  ⚡ 缓存命中仅 {len(fast)} 条，补探其余 {len(rest)} 条备线")
        probed = _probe_warp_egress(rest)
        for m in rest:
            ip = probed.get(m["tag"])
            if not ip:
                log(f"  ↻ 备线 {m['tag']} 不通，先拉起")
                if not (_restart_service(m["service"], m["tag"])
                        and _wait_port_ready(m["port"], _warp()["ready_timeout_sec"])):
                    log(f"  ✗ 备线 {m['tag']} 未就绪，跳过")
                    continue
                ip = warp_egress_ip(m["port"])
            if not ip:
                log(f"  ✗ 备线 {m['tag']} 探测失败，跳过")
                continue
            # 惩罚期两个模式都排除，且必须在探测**之前**：脏 IP 挡在入池前才省得
            # 下一轮白烧一格配额，探完再否决等于每格配额都付了学费。复用窗口则只
            # 在原模式生效 —— 窗口内用过但没被标记的线要继续留在 probes 里，走下面
            # 的「全部不新鲜 → 兜底选休息最久」分支；模型模式由实测决定。
            if _warp_ip_is_blocked(ip, now):
                # 被标记 ≠ 永久不可用：每条隧道同一时刻只有一个出口 IP，重启才会换
                # 会话。全跳过会让候选池恒为 0（403 风暴几秒就能烧完全部备线），
                # 流量被甩到同样被限流的 proxy 池 —— 19:16 的兜底死循环即由此而来。
                log(f"  ↻ 备线 {m['tag']} 出口 {ip} 已被标记，排队刷新")
                blocked_lines.append(m)
                continue
            probes.append((m, ip))

    # 模型模式：候选必须先实测。缓存快速路径只验过出口 IP（可能此刻已 429），
    # 出口探测也只是「能出网」，都不代表能给 opencode 服务。测不过的线与原模式
    # 里「出口已被标记」的线同义 —— 都只有重连换会话 IP 才有意义，故并入
    # blocked_lines 走同一条刷新路径：同步刷一条拿本次候选，其余后台并发补刷。
    if not _ip_history_is_truth() and probes:
        # 同一 IP 可能被多条线持有，先按 IP 去重；当前出口马上要被排除，也不再
        # 为它烧一格配额。结论按 IP 生效：同址的其它线即使没被测到也要一起丢弃。
        to_test_all = _dedupe_probe_pairs(probes, old_ip)
        to_test = _slice_probe_batch(to_test_all)
        if len(to_test) < len(to_test_all):
            log(f"  ↻ 本轮实测 {len(to_test)}/{len(to_test_all)} 条候选"
                "（限流省配额，下一轮接着测）")
        tested = _warp_model_test_many(to_test)
        failed = [m for m, _ in to_test if not tested.get(m["tag"])]
        if failed:
            log(f"  ↻ {len(failed)} 条备线模型层实测不通过，重连换会话 IP")
            blocked_lines.extend(failed)
        # 本轮没测到的候选不放行：模型模式下「没测」不等于「可用」。
        probes = _admit_tested(probes, tested)


    # 备线出口全被标记时刷新一条拿新会话 IP，刷新不出才落 proxy 池 —— 池只是
    # 最后兜底，不该是「地址全被标记」的直接结果。备线此刻不在服务流量，重启
    # 不会掐断在途请求。
    #
    # WHY 同步路径只试一条：`_refresh_idle_warp` 单条最坏 3×(12s 等端口 +
    # 探测 + 退避 10s)。串行刷 N 条时这段时间里流量一直打在已被拦的出口上。
    # 实测 02:40:43→02:41:52 在 CF-B/C/F 上串行刷新失败，造成 69 秒换线空窗，
    # 同期三个 10 秒桶 100% 失败。一条刷不出就把其余交给后台补刷，让这次换线
    # 尽快结束、下一次 429 重新挑线。
    # 同步刷新前先看它是否已在被其他线程刷：名额拿到手也只说明没满。
    # 已在刷的线重启会拿到同一个会话，再刷一次只是浪费一次探测。
    with _WARP_REFRESH_LOCK:
        already = set(_WARP_REFRESHING)
    # 只有「本来有脏线、但全在后台刷」才早退。没有备线、探测失败的场景
    # blocked_lines 本来就是空，那是真正的无候选，必须照旧落池。
    all_busy = bool(blocked_lines) and all(m["tag"] in already for m in blocked_lines)
    blocked_lines = [m for m in blocked_lines if m["tag"] not in already]
    if not probes and all_busy:
        # 此刻不重启任何线，交给后台刷完。旧实现会直接落轮换池，而池在冷却中
        # 又被盲切回来 —— 风暴里三次「备线在服务连接 / 名额已满 → 切轮换池 →
        # 冷却中 → 盲切」空转。
        log("  ⚠ 脏备线均在后台补刷，本次换线不刷新（等下一次错误重试）")
        return False
    if not probes:
        # 冷却中的线重启必然返回 None（见 `_refresh_idle_warp` 入口），排到最后：
        # 先试还能刷的，全在冷却就直接把这条同步路径让给后台补刷。
        blocked_lines.sort(key=lambda m: _standby_cooldown_left(m["tag"]) > 0)
        # 全在冷却时直接放弃本次同步刷新：冷却线重启必然返回 None，硬刷一次
        # 只会把这次换线推进落池（池有 3h 冷却，代价远大于等下一次错误重试）。
        if blocked_lines and all(_standby_cooldown_left(m["tag"]) > 0 for m in blocked_lines):
            log("  ⚠ 脏备线均在离渠冷却中，本次换线不刷新（等下一次错误重试）")
            return False
        for m in blocked_lines[:1]:
            # 与后台补刷/按线 worker 共用同一把按线锁：同步路径不占锁时，
            # 两条线程会同时 restart 同一条隧道（实测 #1#1#2#2#3），白烧一次
            # 会话与探测。拿不到锁说明这条线正在别处刷新，等下一次错误重试。
            ip, acquired = _refresh_line_serialized(m)
            if not acquired:
                log(f"  ⚠ 脏备线 {m['tag']} 正在后台刷新，本次换线不刷新（等下一次错误重试）")
                return False
            if ip:
                probes.append((m, ip))
                break
            log(f"  ✗ {m['tag']} 刷新后仍无可用出口")
        for rest in blocked_lines[1:]:
            _schedule_idle_refresh(rest)

    # 上面两条分支都会往 probes 里加条目（探测/缓存、同步刷新），去重必须放在
    # 它们之后，否则刷新拿回的新地址照样可能是当前出口本身。
    probes = _exclude_active_ip(probes, old_ip)

    # 选线四级优先：
    #   1) 从未用过的地址 —— 必然没被 opencode 见过，优先级最高；
    #   2) 次级优质：真撑过生产流量、且已过对应惩罚期的地址（403 6h / 429 24h），
    #      可复用，不是废址；档内按距上次成功的时长排先后；
    #   3) 探针验过但一次生产流量都没撑过 —— 此刻能服务，只是还没轮到它上场；
    #   4) 都不满足时兜底复用最久未用的（见下面 elif probes 分支）。
    # 第 1 级单列是因为「从没用过」和「用过但已久」在 _warp_ip_last_ts 上都
    # 可能返回很小的值，混在一起排序会让刚清理掉的旧地址冒充新地址。
    # 模型模式下 probes 里只剩刚实测通过的候选，历史不得再否决它们：惩罚期已在
    # 入池前生效（缓存路径 / 探测循环 / 刷新路径都查过），这里再拿旧台账复查一遍
    # 就是把「证据比事实旧」重新引入 —— 实测 OK 的地址被上一段的标记抹掉，候选
    # 全丢、白落一次轮换池（池有 3h 冷却）。原模式照旧按历史过滤。
    # ponytail: 探测到选中之间（1.4~8.7s）被生产流量标脏的地址仍会被选上。那是
    # 事实变了、证据没变，由下一条 403/429 触发换线兜住；要彻底消除得在选中
    # 瞬间再查一次惩罚期，代价是每条候选一次台账读取 —— 量级小，但收益同样小。
    usable = (probes if not _ip_history_is_truth()
              else [p for p in probes if not _ip_disqualified(p[1], now, guard)])
    # 与当前出口同址的候选等于没换。全部如此时走下面的兜底分支重启换 IP，
    # 而不是切到相同地址、被下一次 429 原样打回来。
    usable = [p for p in usable if p[1] != old_ip]
    if _ip_history_is_truth():
        n_new = sum(1 for p in probes if _warp_ip_never_used(p[1]))
        n_ok = sum(1 for p in probes if _warp_ip_rank(p[1], now, guard)[0] == 1)
        n_probe = sum(1 for p in probes if _warp_ip_rank(p[1], now, guard)[0] == 2)
        log(f"  ↻ 选线: 从没用过 {n_new} 条, 次级优质 {n_ok} 条, "
            f"验过未用 {n_probe} 条, "
            f"{guard/3600:.0f}h 内无成功流量合计 {len(usable)} 条 / 备线 {len(probes)} 条")
    else:
        log(f"  ↻ 选线: 模型层实测可用 {len(probes)} 条")
    if usable:
        m, ip = min(usable, key=lambda p: _warp_ip_rank(p[1], now, guard))
    elif probes:
        # 极端兜底：所有备线 IP 都在复用窗口内。按「最久没用过」选一条再刷新。
        # WHY 不用"平均休息时间"排序：实测休息时长与存活无关（<15min 复用的
        # 存活中位 153s，>60min 反而 23s，n=6~26），拿它当判据只会更频繁地
        # 复用同几个地址；这里只用它把最久未用的排前面，且选完仍要刷新。
        # 冷却中的线排最后：选到它 `_refresh_idle_warp` 会直接返回 None，
        # 白白把这次换线推到落池。仍留在候选里是兜底 —— 全都冷却时宁可当场
        # 放弃落池，也不该拿一条刚被换下的线开刀。
        m, ip = max(probes, key=lambda p: (_standby_cooldown_left(p[0]["tag"]) <= 0,
                                           now - _warp_ip_last_ts(p[1])))
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
        # 这里用的是错误轮换路径上的短等待，不是补刷的 idle_wait_sec(90s)：
        # 这条路径上每多等一秒，坏出口就多服务一秒失败请求。实测 40s 上限把
        # 「首错→换线生效」拉到 p90=25s / max=206s。
        if warp_line_busy(m["tag"], _ERROR_PATH_BUSY_WINDOW_SEC) and not _wait_warp_idle(
                m, timeout_sec=_warp().get("fallback_idle_wait_sec", 3),
                window=_ERROR_PATH_BUSY_WINDOW_SEC):
            idle = next(((x, v) for x, v in probes[1:]
                         if warp_line_busy(x["tag"], _ERROR_PATH_BUSY_WINDOW_SEC) is False), None)
            if not idle:
                log("  ⚠ 备线均在服务在途连接，强刷会掐断，切轮换池兜底")
                return _fallback_to_proxy(active, old_ip, now)
            m, ip = idle
        # 这条线正被后台线程刷着：重启会拿到同一个会话，再刷一次只是白烧探测。
        # 落池不是这里的答案 —— 池有 3h 冷却，而那条后台线程几十秒内就有结果。
        # 本次换线放弃，等下一次 429 重新挑线。
        if not _acquire_refresh_slot(m["tag"]):
            log(f"  ⚠ 备线 {m['tag']} 正在后台刷新，本次换线不刷新（等下一次错误重试）")
            return False
        try:
            ip = _refresh_idle_warp(m)
        finally:
            _release_refresh_slot(m["tag"])
        if not ip:
            log(f"  ✗ 备线 {m['tag']} 刷新后仍无新鲜出口，切轮换池兜底")
            return _fallback_to_proxy(active, old_ip, now)
    else:
        log("  ⚠ 无可用备线，切轮换池兜底")
        return _fallback_to_proxy(active, old_ip, now)

    # 探测/选线耗时数秒，按线 worker 可能已拿到这条线的刷新槽并开始重启。
    # 此时 PUT 切过去会切到一条正在冷启动的线。选中后占住按线锁到 PUT 完成；
    # 切过去之后 worker 会看到本线是 active，不会再重启它。
    if not _acquire_refresh_slot(m["tag"]):
        log(f"  ⚠ 选中备线 {m['tag']} 正在被刷新，本次换线放弃（等下一次错误重试）")
        return False
    try:
        switched = _switch_warp(m["tag"])
    finally:
        _release_refresh_slot(m["tag"])
    if not switched:
        return _fallback_to_proxy(active, old_ip, now)
    # 静置时长要在标记本次使用之前算：标完再看恒为 0，日志就成了「/64 静置 0min」。
    rest = _warp_prefix_rest(ip, now)
    # 用切换生效时刻：now 取自探测/重启之前，可能已过数分钟。拿它当 IP 使用
    # 起点会让 24h 复用窗口提前结束，地址被提前放回候选池。
    _mark_warp_ip_used(ip, time.time())
    switched_at = time.time()
    _warp_line_selected(m["tag"], ip, switched_at)  # 轮转公平性
    _set_warp_line_ip(m["tag"], ip)  # 刚验证过：刷新缓存时刻，供下次快速选线
    _enter_segment(switched_at, ip)
    _proxy_dwell_since = 0          # 回到 WARP，驻留计时清零
    _from = active["tag"] if active else "proxy"
    log(f"✓ WARP 换线成功: {_from} -> {m['tag']} (出口 {ip}, "
        f"{'新段' if rest is None else f'/64 静置 {rest/60:.0f}min'})")
    # 延迟刷新刚换下的线：立即重启会掐断仍在走它的在途请求，延迟到连接
    # 自然结束；预先刷好则下次切回时无需在换线路径上同步等 7~9s。
    if active is not None:
        _schedule_idle_refresh(active)
    # 任一备线出口疑似脏就不能留着：重连到换出优质 IP 为止（补刷内部有重试上限）。
    # 真正重启时由 _acquire_refresh_slot 卡并发。按名额截断反而会让脏线留在池里
    # 冒充候选，选到它就得当场同步刷新。
    for bm in blocked_lines:
        if bm["tag"] == m["tag"]:
            continue
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
    # 出口 IP 直接取换线时记下的值，不再同步探一遍：`warp_egress_ip` 走 SOCKS
    # 查 CF trace，实测 1.2~7s（上限 25s），而这段正好卡在「发现错误→起手换线」
    # 的最短链路上。`_ACTIVE_WARP_IP` 由启动探测和每次换线成功维护；为空时
    # `_maybe_init_active_ip` 会后台补探，最多漏记一段样本。
    active_ip = _ACTIVE_WARP_IP if active else None
    # 起点为 0 表示还没成功换过线，没有「段」可言，不能拿历史样本冒充
    # （ended_at >= 0 恒真，会把首个错误段整个跳过）。
    with _WARP_CYCLES_LOCK:
        sampled = _last_warp_switch_ok_ts > 0 and any(
            float(c.get("ended_at", 0)) >= _last_warp_switch_ok_ts
            for c in _WARP_IP_CYCLES)
    # 生效窗口内、且生效之后一条错误都没有 → 本轮的触发全部来自上一段出口的
    # 在途请求。此时标脏会把刚验证过的新出口记成坏地址（403 默认 6h、
    # 429 默认 60s / 解析到精确值时最长 24h），记周期还会让本段
    # 永远采不到真正的结束点（sampled 恒真）。实测 46 次触发里 39 次是这一类。
    # 旧连接豁免窗口：窗口内的 403/429 可能是上一段的在途长连接，不标脏、也不
    # 记周期样本。但只对「新出口还没报过错」成立：一旦出现换线后开始的错误，
    # `fresh` 非空，新出口真坏了照旧标脏（否则坏 IP 会被豁免窗口放行 2 分钟）。
    fresh = (count_warp_blocked_since(_last_warp_switch_ok_ts)
             if _last_warp_switch_ok_ts > 0 else {})
    stale_only = (now - _last_warp_switch_ok_ts
                  < _warp().get("switch_stale_grace_sec", 120)
                  and not (fresh.get("abuse403", 0) or fresh.get("rate429", 0)))
    if not sampled and not stale_only:
        if active:
            # 两类出口级拦截并行、各自惩罚期，解除时间取最晚（用户规则「按多的来」）。
            # 只记一种 kind 会被后一次覆盖：403 之后再烧 429，仍按默认窗口解禁，
            # 提前放回候选池。因此两类都达阈值时，各自记一次，blocked_ts_by_kind
            # 留下两个起点，_warp_block_remaining 取 max 后锁到较晚的一个。
            # 429 的精确冷却从本段最近一条 429 的 body/message 解析（生产库无
            # 响应头，header 层只作用在探测路径 _warp_model_usable）；解析不到
            # 传 None → 落 config 默认（60s）。403 不套响应时长（见 _warp_ip_used_locked）。
            rate429_ban = None
            if "rate429" in triggered:
                rate429_ban = _parse_rate429_cooldown(
                    _latest_rate429_body_since(_last_warp_reconnect_ts))
            for k in triggered:
                _mark_warp_ip_used(active_ip, now, blocked=True, kind=k,
                                   ban_after=rate429_ban if k == "rate429" else None)
        # 只有成功服务过 ≥1 条的段才算「可用周期」；首请求即错的段记 ok=0。
        if active_ip and _last_warp_switch_ok_ts > 0:
            cycle = now - _last_warp_switch_ok_ts
            if cycle > 0:
                # 用**整段累计**的成功数，不能用上面那个滑动窗口的 ok ——
                # 窗口只有 2 条，ok 恒为 0 或 1：实测 4058 条周期样本里 82%
                # 记成 ok=0，把「这条线服务过 40 条」误判成「一条没服务过」，
                # 于是寿命判据的样本被系统性剔除，单 IP 承载量也统计不出来。
                seg_ok, seg_blocked = count_segment_requests(_last_warp_switch_ok_ts)
                _record_warp_cycle(active_ip, cycle, now, ok=seg_ok,
                                   blocked=seg_blocked)
    # 用户规则：最近两条请求都报错仍允许不切，第三条连错才换线。放在采样/标脏
    # 之后 —— 连错 1~2 条时出口已收到 403，仍需把该 IP 标脏、记周期样本，只是
    # 暂不换线。实测 90 次真实触发里这条门控抑制 11 次，其中 3 次窗口内成功 ≥5
    # （出口还健康，切走就是误切）。`full_err`（整窗都是出口级错误、无一成功）
    # 时 consec_err 必 >= 窗口长度，不受影响。
    if blocked.get("consec_err", 0) < 3 and not blocked.get("full_err"):
        return
    if now < _warp_cooldown_until and not blocked.get("full_err"):
        return
    # 两次换线生效间隔的硬闸，含 full_err 风暴例外 —— 上面三条都能被 bypass，
    # 这条不能。`_last_warp_switch_ok_ts` 是上一段**生效**时刻（见 reconnect_warp
    # 的 switched_at），不是触发时刻；差别就是 触发→生效 那 p50 4 秒。
    if now - _last_warp_switch_ok_ts < _warp().get("min_switch_gap_sec", 5):
        return
    # 触发条件已满足，委托给统一入口处理冷却与并发
    full_err_bypass = blocked.get("full_err", False)
    _reconnect_warp_now(
        " + ".join(f"{k} ×{blocked[k]}" for k in triggered),
        now,
        ignore_cooldown=full_err_bypass
    )


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
    prev_cooldown = _warp_cooldown_until
    _last_warp_reconnect_ts = _now_ts()
    _warp_cooldown_until = now + delay  # 先占位，防 tick 重入
    try:
        threading.Thread(target=_warp_reconnect_worker,
                         args=(reason, delay),
                         daemon=True, name="warp-reconnect").start()
    except Exception as e:
        # 线程没起来，worker 的 finally 不会跑：不还锁会让后续换线永久被拒。
        _warp_cooldown_until = prev_cooldown
        _WARP_LOCK.release()
        log(f"✗ 换线线程启动失败: {e}")
        return False
    return True


def _pool_proactive_label(now: float, last_rotate: float, rotate_gap: float) -> str:
    """池侧定时轮换进度标签：换线间隔 + 已过时长 + 下次换线时刻。

    blind 模式下时钟是唯一反馈，运维要能一眼看出「多久换一次、下次几点换」，
    所以间隔用配置值 rotate_gap 明写，不藏在进度数字里。

    last_rotate 为 0（刚启动，本轮还没换过）时以 now − rotate_gap 为基准，
    避免打出「已过 5 位数秒数」这种没意义的数字。
    """
    base = last_rotate if last_rotate else now - rotate_gap
    due_ts = time.strftime("%H:%M:%S", time.localtime(base + rotate_gap))
    return (f"池定时 间隔{rotate_gap:.0f}s 已过{max(0.0, now - base):.0f}s "
            f"下次≈{due_ts}")


def _proactive_mode() -> str:
    """主动轮换判据：fixed（固定秒数）/ requests（成功的 dsv4 调用数）。

    两者**互斥**，只有 `proactive_mode` 选中的那个生效。

    `lifetime` 已移除：段跨度（首条成功→末条成功）跟的是**请求间隔**而不是
    IP 寿命 —— 实测中位 3.8s，而请求本身 p50 6.5s。生效值恒被 60s 地板顶住，
    等于一个测不准的固定 60s。死因是 429 按成功调用扣配额，与时钟无关。
    """
    mode = str(_warp().get("proactive_mode", "fixed")).lower()
    if not _observe_9router():
        # blind 模式强制 fixed：requests 判据查 usageHistory（9router 库），
        # 流量不经 9router 时计数恒 0，等于永不主动换线。
        return "fixed"
    if mode not in ("requests", "fixed"):
        return "fixed"
    return mode


def _maybe_proactively_rotate_warp(now: float) -> None:
    """当前出口还在成功服务时就换线，不必等 429 冲断在途对话。

    两种判据由 `proactive_mode` 二选一（互斥）：
      fixed    — 按固定秒数，不看流量；
      requests — 按当前出口上**成功的** dsv4 调用（配额格数）。

    时间判据的缺陷：实测主动段（60s 到点切走）dsv4 成功中位 113.5 / p90 175
    —— 60s 上限本身就在扔掉还没用完的配额；而 429 段存活中位仅 6s。两个分布
    都不随时钟走，固定周期只能靠猜。配额判据与配额扣减同源，流量自适应。

    成功换线后绕过 cooldown_ok：阈值可能短于它，受冷却就永远等不到触发
    （并发仍由 `_WARP_LOCK` 保证）；换线失败后的冷却不绕，那是防隧道雷暴的。

    """
    global _last_proactive_warp_check, _proactive_progress
    # 计数要查 SQLite，5s 一次足够：配额以秒级速度消耗，多查无益。
    # fixed 模式的判据是纯时长，不查库，但同样只需秒级精度。
    if now - _last_proactive_warp_check < 5:
        return
    _last_proactive_warp_check = now
    mode = _proactive_mode()
    # 用「换线生效时刻」而非「发起时刻」：发起早于生效数秒，期间旧出口仍在
    # 服务流量，用发起时刻会把旧线的消耗算进新线配额。
    # 两者都为 0 表示本次启动后还没换过线。此时**不要**直接返回后从零计数：
    # service 重启后 `_last_warp_switch_ok_ts` 归零，若拿 0 当窗口起点，
    # 配额计数会把重启前已经服务过的那一段整段算进来 —— 实测 16:31:08 重启、
    # 16:32:18（70 秒后）就报「已服务 63 条」并换线，而那条线实际只服务了
    # 几条。改用该出口 IP 在 `.warp_ip_used.json` 里的使用时刻恢复段起点，
    # 跨重启仍然正确。
    start = _last_warp_switch_ok_ts or _last_warp_reconnect_ts
    if start <= 0:
        # 内存变量重启即归零，但 sing-box 的 selector 不变 —— 用已知的出口 IP
        # 在 IP 表里的使用时刻恢复段起点。不在这里现探：`warp_egress_ip` 走
        # SOCKS，最长 20s，而本函数在 20Hz 的主循环里。`_ACTIVE_WARP_IP` 由
        # 启动探测与每次换线维护，为空就放弃本轮，下一 tick 再试。
        ip = _ACTIVE_WARP_IP
        with _WARP_IP_LOCK:
            entry = _WARP_IP_USED.get(ip) if ip else None
        start = float(entry.get("ts") or 0) if isinstance(entry, dict) else 0.0
        if start <= 0:
            _proactive_progress = "起点未知"
            return

    if mode == "fixed":
        elapsed = now - start
        target = float(_warp()["proactive_fixed_sec"])
        lead = float(_warp().get("switch_lead_sec", 3.0))
        threshold = max(target - lead, 1.0)
        # 「下次 ≈」不是精确调度：换线在下一个 ≥20s 的 tick 里触发，
        # 且提前量 lead 也只是目标提前，实际时刻随 tick 抖动 ±20s。
        next_ts = time.strftime("%H:%M:%S", time.localtime(start + threshold))
        # 间隔标配置值 target 而非 threshold：运维看的是「多久换一次」，
        # threshold 含提前量（target − lead），只在下次≈时刻里体现。
        _proactive_progress = (f"fixed 间隔{target:.0f}s 已{elapsed:.0f}s "
                               f"下次≈{next_ts}")
        # 固定时长不看样本：运维要求「无论流量快慢每 N 秒换一次」时用。
        if elapsed >= threshold:
            _reconnect_warp_now(
                f"主动轮换 固定 {elapsed:.0f}s ≥ {threshold:.0f}s"
                f"（{target:.0f}s − 提前量 {lead:.0f}s）",
                now, ignore_cooldown=_last_warp_reconnect_ts <= _last_warp_switch_ok_ts)
        return

    n = count_dsv4_ok_since(start)
    target = int(_warp()["proactive_after_dsv4_ok"])
    _proactive_progress = f"requests {n}/{target}格"
    if n >= target:
        _reconnect_warp_now(
            f"主动轮换 已用配额 {n} 格 dsv4 ≥ {target}",
            now, ignore_cooldown=_last_warp_reconnect_ts <= _last_warp_switch_ok_ts)


def _validate_startup_egress(member: dict) -> None:
    """启动时当前出口若低质，立刻换线。

    sing-box 重启后 selector 复位到配置里的 default（主隧道 WARP-CF），而它可能
    正处 429 惩罚期或已被模型层判 quota。主动轮换/错误换线都要等主循环第一轮
    才跑，不在这里纠正就等于「重启后先白烧一段 IP，等第一条 429 才被换走」
    ——实测 2026-10-22 13:45 重启后 WARP-CF 立即 429 ×3。
    """
    ip = _ACTIVE_WARP_IP
    if not ip or not _warp_ip_needs_refresh(ip, time.time(), member["tag"]):
        return
    log(f"  ⚠ 启动时当前出口 {member['tag']} 出口 {ip} 判定为低质"
        f"（{_warp_ip_refresh_reason(ip, time.time(), member['tag']) or '无成功流量'}），启动即换线")
    # 换线是异步线程：这里不能马上复探 active_warp_tag() —— 读到的是旧线，
    # 白等一次 20s 出口探测，还会把低质 IP 写回 `_ACTIVE_WARP_IP`。
    # 成功路径自己会更新它（见 reconnect_warp 的 `_ACTIVE_WARP_IP = ip`）。
    if not _reconnect_warp_now("启动校验 当前出口低质", time.time()):
        log("  ✗ 启动换线未发起（冷却/锁占用），留给主循环下一轮")


def _prewarm_standby_warp(now: float) -> None:
    """后台巡检各条备线：出口疑似脏就重连，直到换出优质 IP。

    WHY 需要：ip_reuse_guard_sec=24h，用过的线 IP 全在窗口内；不预热的话换线时
    只有「刚换下、已被补刷」的那一条是新鲜的 —— 实测流量长期只在 WARP-CF 与
    WARP-CF-B 之间往返。另一类脏线是 403/429 标记过的地址：它可能从没有过成功
    流量，只看「窗口内有无成功」会把它当新鲜线跳过，脏 IP 永远刷不掉。

    每轮最多排 `prewarm_max_per_round` 条（默认 4），游标轮转覆盖全池：池内各线
    互不依赖，同一条线内部互斥；但全量 18 条/轮会自己烧掉备线配额，与错误换线
    同理。旧实现每轮只取一条最久没被选中的，实测全库只预热过 11 次且集中在两三条
    线上，脏线永远轮不完 —— 故用「小批 + 轮转」而不是「一条」或「全部」。
    """
    global _last_warp_prewarm_ts
    if now - _last_warp_prewarm_ts < _warp()["prewarm_sec"]:
        return
    _last_warp_prewarm_ts = now
    # 没有 opencode 流量时不刷：预热是为了「下次换线有现成新鲜 IP」，没人
    # 在用时重启隧道只是白白换掉一个会话。判据读 requestDetails，只在观测
    # 开启时有效 —— blind 模式下流量不经 9router，计数恒 0，若也照此跳过，
    # 备线永远不预热，换线候选全是旧会话，所以 blind 直接放弃这条闸门。
    # 24 小时窗口与 ip_reuse_guard_sec 一致。
    if _observe_9router() and count_opencode_since(now - 86400) == 0:
        return
    # 名额满不代表要放弃整轮：排队段（PENDING）不占名额，前面刷完自然轮到。
    # 旧实现见到名额满就直接 return，一轮一条都排不上。
    with _WARP_REFRESH_LOCK:
        busy = set(_WARP_REFRESHING) | set(_WARP_REFRESH_PENDING)
    active = active_warp_tag()

    # 已知 IP 仍优质的线不用再刷：重启会白换掉一个好会话。
    cands = [m for m in _warp()["members"]
             if m["tag"] != active and m["tag"] not in busy
             and _warp_ip_needs_refresh(_warp_line_ip(m["tag"]), now, m["tag"])]
    if not cands:
        return
    limit = int(_warp().get("prewarm_max_per_round", 0) or 0)
    if limit > 0 and limit < len(cands):
        global _PREWARM_CURSOR
        total = len(cands)
        start = _PREWARM_CURSOR % total
        _PREWARM_CURSOR = (start + limit) % total
        cands = (cands + cands)[start:start + limit]
    for m in cands:
        _schedule_idle_refresh(m)
    whys = "; ".join(
        f"{m['tag']}({_warp_ip_refresh_reason(_warp_line_ip(m['tag']), now, m['tag']) or '未知'})"
        for m in cands)
    log(f"↻ 预热 {len(cands)} 条低质备线: {whys}")



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
    # `strict` 保留给调用方表达意图（严格/宽松），但两者都必须返回 None：
    # 探测不到延迟就是探测不到，宽松模式不能把失败伪装成 0ms。失败明细由
    # 调用方统一汇报，这里不逐条写日志——健康检查一轮会探几十个节点。
    return _CLASH.delay(
        node, timeout_ms, url=CFG["thresholds"]["node_test_url"],
    )


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

        def _merge(shared):
            if not isinstance(shared, dict):
                shared = {}
            # 只追加 daemon 发现的死节点，不覆盖 proxy-global/cron 已有的条目
            for n in _DEAD_NODES:
                if n not in shared:
                    shared[n] = {"expire": _DEAD_NODES_RELEASE_TS.get(n, _now_ts() + BAD_NODES_TTL),
                                 "reason": "daemon_dead"}
            return shared

        # 与 self_evolve_checkpoint 共用同一把 flock：两边都做 read-modify-write，
        # 无锁并发会互相覆盖（cron 刚写进的坏节点被 daemon 的旧快照抹掉）。
        _atomic_update_json(_BAD_NODES_FILE, _merge, default={},
                            lock_path=_BAD_NODES_FILE + ".lock")
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


def _skip_recent_node_ips(candidates: list[str], ip_map: dict[str, str],
                          now: float | None = None) -> set[str]:
    """按时间换线（blind 模式）的脏 IP 过滤：窗口内被任何节点用过的出口 IP
    对应节点跳过。这是 blind 模式唯一的 IP 反馈 —— 时钟不会告诉它哪个地址
    刚被烧过，台账会。IP 未知（node_info 还没回填）的候选不受限：没有历史
    就是干净候选，这条曲线由 fetch_node_ip_region 成功后补齐。
    traffic-aware 模式返回空集 —— 那里有实时错误信号验 IP，不需要启发式。
    """
    if _observe_9router():
        return set()
    window = float(CFG["traffic"].get("ip_reuse_window_sec") or 0)
    if window <= 0 or not _NODE_IP_USED:
        return set()
    now = time.time() if now is None else now
    with _NODE_IP_LOCK:
        used = dict(_NODE_IP_USED)
    skip: set[str] = set()
    for n in candidates:
        ip = ip_map.get(n) or ""
        if ip and now - used.get(ip, 0.0) < window:
            skip.add(n)
    if skip:
        log(f"  ⏳ 脏IP避让: {len(skip)} 个候选出口在 {window/3600:.0f}h 内用过，本轮跳过")
    return skip


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
    dedup_skip |= _skip_recent_node_ips(candidates, ip_map)

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
            log(f"⚠ Clash API 不通但进程还在 (PID={pids})")
        return False

    if time.time() >= deadline:
        log(f"⚠ Clash API 正常但端口检查超时")
        return True

    # 2. Proxy port must accept connection (fast check)
    try:
        sock = socket.create_connection(("127.0.0.1", MIXED_PORT), timeout=min(2, deadline - time.time() + 0.5))
        sock.close()
    except (ConnectionRefusedError, OSError, TimeoutError):
        log(f"⚠ Clash API 正常但代理端口 {MIXED_PORT} 不通")
        return False

    return True


# 判死前必须连续失败的次数。核心红线「服务探测必须 3 次连续失败才标 down」：
# 单次超时就重启 sing-box，等于用一次网络抖动换一次全量在途 SSE 中断
# （客户端看到 upstream connection lost）。实测 12:57:43 重启成功、33 秒后
# 又因一次探测失败再重启一次；16:37:18 那次同一秒打了两条重启日志。
_PROBE_FAIL_STREAK = 3
# 就地重试的间隔。**不能靠 watchdog 的 30s 周期凑次数** —— 三次跨 60~90s，
# 真死了的 sing-box 要多服务一分半的失败请求。4s 总预算内分三次，单次仍
# 保持 2s 上限，与原来的探测强度一致。
_PROBE_RETRY_GAP_SEC = 1.5


def _probe_singbox_with_retry() -> bool:
    """连续 `_PROBE_FAIL_STREAK` 次失败才判死；任一次成功即复活计数。

    只在 watchdog 路径用。`restart_singbox` 重启后的自检仍调 `is_singbox_alive`
    单次 —— 那里刚等过 `startup_grace_sec`，进程要么起来了要么没起，
    再等三次只会拖长重启窗口。
    """
    for _ in range(_PROBE_FAIL_STREAK):
        if is_singbox_alive():
            return True
        time.sleep(_PROBE_RETRY_GAP_SEC)
    return False

_restart_lock = threading.Lock()

def _set_outbound_proxy(url: str = "", enabled: bool = False,
                        attempts: int = 3) -> bool:
    """设置 9router 出站代理，失败重试；返回是否最终成功。

    调用点在重启窗口前后：这里静默失败会让 9router 继续走已停的隧道（或
    绕过代理裸奔），而调用方无从判断。数据库偶发 locked 值得重试，不该
    一次失败就放弃。
    """
    enabled_int = 1 if enabled else 0
    for attempt in range(max(1, attempts)):
        try:
            with _db_lock:
                c = _get_db()
                c.execute("""
                    UPDATE settings SET data = json_set(COALESCE(data, '{}'),
                        '$.outboundProxyUrl', ?,
                        '$.outboundProxyEnabled', ?
                    ) WHERE id=1
                """, (url, enabled_int))
                c.commit()
            return True
        except Exception as e:
            if attempt + 1 >= max(1, attempts):
                log(f"✗ 设置 outboundProxy 失败（已重试 {attempts} 次）: {e}")
                return False
            time.sleep(0.2 * (attempt + 1))
    return False


def _reset_all_lines_after_session_restart() -> None:
    """sing-box 重开所有隧道会话：旧世代/旧地址证据一次性作废。

    只清当前线的地址不够：所有线的底层连接都已重建，重启前发出的探测无论
    何时返回都属于旧会话。世代 +1 让晚到结论在 `_warp_line_record_observation`
    处直接丢弃；清 ip/last_ok 让换线路径不会把旧地址当新鲜候选。
    """
    with _WARP_LINE_CV:
        for st in _WARP_LINE_STATE.values():
            st["epoch"] = int(st.get("epoch") or 0) + 1
            st.pop("ip", None)
            st.pop("ip_ts", None)
            st["last_ok_ts"] = 0.0
            st["last_ok_ip"] = None
            st["warming"] = False
        _WARP_LINE_CV.notify_all()
    _save_warp_line_state()


def restart_singbox() -> bool:
    """重启 sing-box，互斥锁防止并发调用。停服前绕过代理，启动后恢复。"""
    global _RESTART_COUNT, _ACTIVE_WARP_IP
    if not _restart_lock.acquire(blocking=False):
        log("  ⚠ restart_singbox 已在运行，跳过并发调用")
        return False
    try:
        with _RESTART_LOCK:
            restart_count = _RESTART_COUNT
        log(f"⚠ 重启 sing-box (连续失败 {restart_count}/{CFG['singbox']['max_restart_attempts']})...")

        # 重启后 selector 回到配置里的 default（主隧道）。记住重启前的选择，
        # 否则每小时订阅刷新都会把 opencode 拽回刚被判为被标记的那条线。
        # 用三态模式而非 active_warp_tag()：后者把「在池」和「查不到」都压成 None，
        # 而看门狗重启前的 sing-box 往往正好是死的 —— 那种情况下不该动出口。
        prev_mode, prev_warp = _current_mode()

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
        # sing-box 日志可能带节点地址与密码；显式 0600，不沿用 umask。
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass
        with os.fdopen(log_fd, "a") as log_file:
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
            elif prev_mode == "proxy" and _warp().get("proxy_fallback_enabled", False):
                # 重启前停在轮换池，重启后 selector 回到 default(WARP-CF)：
                # 不切回去等于静默改变出口模式，落池冷却也就白等了。
                _switch_warp("proxy")
                _clear_active_ip()   # 会话已重开，旧 IP 不再属于任何线路
            elif prev_mode == "proxy":
                # 落池已禁用：保持 default(WARP-CF)，不把流量送回死池。
                _clear_active_ip()
                log("  ⚠ 落池已禁用，重启后保持默认 WARP 出口")
            elif prev_mode == "unknown":
                log("  ⚠ 重启前查不到出口模式，selector 停在 default，交由下次换线校正")
            # 所有隧道都是新会话：旧世代/旧地址证据一次性作废，再从新会话
            # 补探出口并按 IP 决定「续算还是新段」。
            _reset_all_lines_after_session_restart()
            _enter_segment(time.time(), None)
            if prev_warp:
                # sing-box 重启会重开隧道会话，出口 IP 可能已变；不重探的话健康行
                # 会一直把旧 IP 配在新 tag 上，直到下一次换线。
                _refresh_active_warp_ip()
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
    # requests 的异常串会带完整订阅 URL（token 在 query 或 /pq/ 路径里），
    # 这一行会进 0600 的 daemon.log 之外，也可能被转述到控制面板。
    return _redact_secrets((exc[-1] if exc else lines[-1])[:200])

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
        # token 不能进 argv：`ps`/`/proc/<pid>/cmdline` 对同机用户可见。
        # 用 0600 的临时文件传参，解析完立即删除。
        fd, urls_file = tempfile.mkstemp(prefix="asip-subs-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(urls) + "\n")
            r = subprocess.run(
                [sys.executable, parse_script,
                 "--out", tmp_file,
                 "--urls-file", urls_file],
                capture_output=True, text=True, timeout=120)
        finally:
            try:
                os.unlink(urls_file)
            except OSError:
                pass
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
                # 这里是「该出口 IP 真在被服务」的准确时刻 —— 不是切换捏造的
                # 预估，是 ipify 实测回来的地址。blind 模式的 6h 复用窗口以此为准。
                _record_node_ip_used(ip)
                log(f"  📍 {format_node_display(node)}")
            else:
                log(f"  ⚠ 无法获取 {node} 的 IP/地区信息")
        except Exception as e:
            log(f"  ✗ 更新 {node} 信息失败: {e}")

    _io_pool.submit(_worker)

# ============= 主循环用到的后台任务 =============
# 主循环是 20Hz 的判定环，不是执行器：下面这些函数的单次耗时都在秒~分钟级，
# 放进循环等于让「发现 403」排队等一个探测跑完。
_rd_seq = 0


def _rd_watermark() -> int:
    """requestDetails 的最大 rowid —— 表有没有新行的 O(1) 判据。

    WHY 不用 timestamp：它由 9router 客户端写入，实测 439/2000 行与 rowid
    不同序（同一事务里多条记录的时间戳可以乱序落库）。rowid 单调递增，
    每次 INSERT 都让水位线上移，正是「有没有新请求」这个问题要的东西。
    9router 的清理走 `DELETE ... ORDER BY timestamp ASC`，删的是最旧的行，
    不会让 max(rowid) 回落。
    """
    global _rd_seq
    try:
        with _db_lock:
            row = _get_db().execute(
                "SELECT max(rowid) FROM requestDetails").fetchone()
        _rd_seq = int(row[0] or 0)
    except Exception as e:
        log(f"✗ 读 requestDetails 水位线失败: {e}")
    return _rd_seq


def _rotate_pool_proactive(current: str | None, nodes: list[str], now: float) -> None:
    """轮换池内的主动切节点（opencode 落在池上时才用）。"""
    next_node = pick_next_node(current or "", nodes, set())
    if not next_node:
        log(f"⚠ 主动轮换: 无可用节点 (dead={len(_DEAD_NODES)})")
        return
    if next_node == current:
        return
    delay_info = f" ({_NODE_DELAYS[next_node]}ms)" if next_node in _NODE_DELAYS else ""
    log(f"→ 切代理 (主动轮换): {format_node_display(current)} -> "
        f"{format_node_display(next_node)}{delay_info}")
    if switch_proxy(next_node):
        _set_pool_cooldown(now + CFG["intervals"]["cooldown_after_switch_sec"])
        update_node_info_async(next_node)


def _rotate_on_error_bg(errors: int, total: int, nodes: list[str], now: float) -> None:
    """池内错误轮换后台版：把冷却时间戳写回模块级变量，主循环只读。"""
    until = _rotate_on_error(errors, total, nodes, now)
    if until:
        _set_pool_cooldown(until)


_pool_cooldown_until = 0.0


def _set_pool_cooldown(ts: float) -> None:
    global _pool_cooldown_until
    _pool_cooldown_until = max(_pool_cooldown_until, ts)


def _cleanup_node_info(nodes: list[str]) -> None:
    prune_egress_log()   # 附带裁剪 egress_log（读取路径不再承担写操作）
    if not nodes:
        return
    try:
        valid_set = set(nodes)
        with _db_lock:
            c = _get_db()
            # 批量删除过期节点：单条参数化 DELETE + 显式 commit，
            # 避免循环内 DELETE 因未提交被 SQLite 隐式事务丢弃（原 bug）。
            placeholders = ",".join("?" * len(valid_set))
            c.execute(f"DELETE FROM node_info WHERE node NOT IN ({placeholders})",
                      tuple(valid_set))
            c.commit()
        log(f"  ♻ 清理过期 node_info 完成 (保留 {len(valid_set)} 个有效节点)")
    except Exception as e:
        log(f"  ⚠ 清理 node_info 失败: {e}")


def _reset_node_call_counts() -> None:
    with _state_lock:
        _NODE_CALL_COUNTS.clear()
    with _db_lock:
        _get_db().execute("UPDATE node_info SET call_count=0")
        _get_db().commit()
    log("  ♻ 节点调用计数已重置")


def _wal_checkpoint() -> None:
    try:
        with _db_lock:
            _get_db().execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception as e:
        log(f"  ⚠ WAL checkpoint 失败: {e}")


def _singbox_watchdog() -> None:
    """探活 → 必要时重启。探测最慢 ~12s、重启要几十秒，只能在后台跑。"""
    global _pool_cooldown_until, _RESTART_COUNT
    if _probe_singbox_with_retry():
        with _RESTART_LOCK:
            if _RESTART_COUNT > 0:
                _RESTART_COUNT = 0
                log("✓ sing-box watchdog 恢复")
        return
    with _RESTART_LOCK:
        restart_count = _RESTART_COUNT
    if restart_count < CFG["singbox"]["max_restart_attempts"]:
        restart_singbox()
        _pool_cooldown_until = max(_pool_cooldown_until, time.time() + 30)
    else:
        log(f"🚨 sing-box 连续重启 {restart_count} 次失败，降级")
        check_and_handle_fallback([], "")


def _maybe_failback_to_warp_bg(now: float) -> None:
    """池→WARP 的回切判定。要探整池健康（最慢 15s），后台跑。

    闸门必须先在这里判：`_submit_once` 每次都会起一个线程，而本函数在主循环
    里 20Hz 被调用 —— 把闸门留给 `_maybe_failback_to_warp` 内部，等于每秒
    空转 20 个线程去跑一句时间比较。
    """
    if opencode_out_on_warp():
        return
    if _last_proxy_pool_check + CFG["intervals"]["proxy_check_sec"] > now:
        return
    _submit_once("failback-to-warp", _maybe_failback_to_warp, now)


def _reap_health_check() -> None:
    """收后台健康检查的结果（非阻塞：future 未完成就跳过）。"""
    global _health_check_future, _health_check_nodes, _health_check_current
    with _state_lock:
        if _health_check_future is None or not _health_check_future.done():
            return
        future = _health_check_future
        check_nodes = _health_check_nodes
        check_current = _health_check_current
        _health_check_future = None
        _health_check_nodes = None
        _health_check_current = None
    try:
        _dead, alive = future.result()
        with _state_lock:
            for n in list(_DEAD_NODES):
                if n in alive:
                    _DEAD_NODES.discard(n)
                    _DEAD_NODES_RELEASE_TS.pop(n, None)
        if check_nodes and check_current:
            check_and_handle_fallback(check_nodes, check_current)
    except Exception as e:
        log(f"⚠ 后台健康检查异常: {e}")


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


def _log_startup_banner(all_nodes: list[str]) -> None:
    """启动配置摘要：只读 CFG，不产生副作用，让 main 只保留编排逻辑。"""
    log(f"  sing-box: {CFG['singbox']['clash_api']}")
    log(f"  节点数: {len(all_nodes)}")
    log(f"  轮换: 每 {CFG['intervals']['proactive_rotate_sec']}s P2C")
    log(f"  健康: 每 {CFG['intervals']['node_health_sec']}s 批量测延迟")
    log(f"  订阅: 每 {CFG['intervals']['subscription_refresh_sec']}s 刷新")
    if _observe_9router():
        log("  流量观测: on — 9router requestDetails 错误信号驱动换线")
        log(f"  自适应: 429率>{CFG['thresholds']['pressure_error_rate']:.0%}→均匀分散, <{CFG['thresholds']['normal_error_rate']:.0%}→延迟优先")
        log(f"  错误触发: 出口级拦截(403≥{_warp()['block_threshold']}/429≥{_warp()['rate429_threshold']})"
            f" 且连续≥3条才切；上游 402/请求超长不计")
    else:
        log("  流量观测: off — blind 模式，不读 9router，纯按时间轮换"
            f" (WARP 每 {CFG['warp']['proactive_fixed_sec']}s)"
            f" + 池 每 {CFG['intervals']['proactive_rotate_sec']}s")
        log(f"  换线间隔: WARP {CFG['warp']['proactive_fixed_sec']}s / 池 "
            f"{CFG['intervals']['proactive_rotate_sec']}s（下次换线时刻见健康摘要 轮换W/轮换P）")
        log(f"  脏IP避让: 出口IP在 {CFG['traffic']['ip_reuse_window_sec']/3600:.0f}h 内用过则跳过"
            f"（台账 .node_ip_used.json；0=关闭）")
    log(f"  Watchdog: 每 {CFG['intervals']['singbox_watchdog_sec']}s")
    log("  IP去重: 同出口IP保留调用次数最低的节点")
    log("  调用计数: 仅统计成功调用, 429轮换不计入")
    log("  interrupt_exist_connections: false (不杀现有连接)")
    log("=" * 50)


def main():
    _acquire_single_instance_lock()
    # Log first, then fetch (avoid hanging before any output)
    log("=" * 50)
    log("auto-switch-ip daemon v2 启动 (加固版)")

    all_nodes = get_all_proxy_nodes()
    _log_startup_banner(all_nodes)

    # 初始化 node_info 表
    init_node_info_db()
    # 恢复跨重启的出口 IP 使用/污染记录与可用周期样本
    _load_warp_ip_used()
    _load_warp_ip_cycles()
    _load_warp_line_state()
    _load_node_ip_used()
    # 探测线程晚于「出口 IP + 段起点」恢复再启动：先启动会让首轮探测
    # 在 `_ACTIVE_WARP_IP` 还是 None 时把当前线当备线，重启正在服务的隧道。
    prober_enabled = bool(_warp().get("prober_enabled", True))
    # 日志标签用的当前 WARP 出口 IP：启动时探一次，之后由换线路径更新
    global _ACTIVE_WARP_IP, _last_warp_switch_ok_ts, _SEGMENT_PENDING
    _m = _warp_member(active_warp_tag())
    _startup_ip = warp_egress_ip(_m["port"]) if _m else None
    if _m and not prober_enabled:
        _validate_startup_egress(_m)
    elif not _m:
        # 启动时就停在 proxy 上：落池时刻从此刻算，否则「落池后仍拦截」
        # 判据会拿 0 当起点，把落池前的旧 403/429 也算进去。
        global _proxy_dwell_since
        _proxy_dwell_since = time.time()
    with _ACTIVE_SEG_LOCK:
        _ACTIVE_WARP_IP = _startup_ip
        _last_warp_switch_ok_ts = _load_segment_start(_startup_ip)
        _SEGMENT_PENDING = _startup_ip is None

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
    global _last_warp_reconnect_ts, _proactive_progress, _POOL_PROACTIVE_LABEL
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
        # blind 模式不代管 9router 的生死：它可能根本没跑，重启它的
        # systemd 单元也不是本 daemon 的职责（横幅已声明 blind）。
        if _observe_9router() and not check_9router_health():
            log("⚠ 9router 不可达，等待 3s 重试...")
            time.sleep(3)
            if not check_9router_health():
                log("✗ 9router 仍不可达，尝试重启...")
                # Try to restart 9router via systemd if available
                try:
                    subprocess.run(["systemctl", "--user", "restart", "nine-router-src.service"], timeout=30)
                except Exception:
                    pass
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

    # 主循环不等待：任何会阻塞的操作都不在这里做。
    # 实测各步的最坏耗时 —— Clash API 3s/次、SOCKS 探测 20s、节点预检 10s、
    # sing-box watchdog 4s、池探测 15s。放在循环里意味着「发现 403」可能排在
    # 一个 15s 的探测后面才被看到，而那段时间每个请求都在失败。
    # 现在循环体只剩三件事：读一个 int 水位线、按定时器派发后台任务、写日志。
    _start_clash_snapshot_loop()
    # 单实例探测器接管所有线路的探测/状态写入；旧守护仅在回滚开关下启动。
    if prober_enabled:
        _start_warp_prober()
    else:
        # 脏线守护：常驻线程，脏 IP 一出现就重连换干净地址，不依赖主循环周期。
        if _line_worker_enabled():
            _start_line_workers()
        if not _line_worker_owns_scheduling():
            _start_dirty_line_janitor()
    last_watermark = -1
    next_error_eval = 0.0
    total = errors = consecutive_errors = 0
    reject_ratio = 0.0
    cached_all_nodes, cached_current_proxy = _pool_snapshot()
    while True:
        now = time.time()
        try:
            # ---- 1. 只在 requestDetails 有新行时才做判定 ----
            # `get_9router_error_rate` 要扫 requestDetails 并解析 data 列，实测
            # 3.3ms；20Hz 轮询等于 6.6% CPU 空转。水位线查询走 provider 索引，
            # 2µs。没有新行就说明「没有任何新错误」，正是最该什么都不做的时候。
            # blind 模式（observe=False）整段跳过：requestDetails 一行都不读，
            # 换线只认时间判据（下方主动轮换 + 池轮换）。
            observe = _observe_9router()
            new_rows = False
            if observe:
                wm = _rd_watermark()
                new_rows = wm != last_watermark
                if new_rows:
                    last_watermark = wm
                    total, errors, details, consecutive_errors, reject_ratio = get_9router_error_rate()
                    # errors 只数出口级拦截，不能拿 total-errors 当成功数（402/超长
                    # 会被误算成成功）。成功数直接数 status，才是这个出口真的served 的流量。
                    ok_count = sum(1 for r in details if r.get("status") == "success")
                    _record_active_ip_success(total, total - ok_count)
            # 出口 IP 补探与观测开关无关：WARP 主动轮换的段起点恢复要用
            # `_ACTIVE_WARP_IP`，函数自带 30s 节流，每 tick 调用一次没有成本。
            _maybe_init_active_ip(now)
            # 节点列表每 tick 都读：读的是后台快照（一次加锁 + 两次字段访问），
            # 而健康检查/池轮换用的是它。只在有新行时读的话，没有流量的时段里
            # 订阅刷新进来的新节点永远进不了列表。
            cached_all_nodes, cached_current_proxy = _pool_snapshot()

            # ---- 0. 9router 代理配置检查 (每 60s) ----
            # SQLite 读写，不在热路径上，但也不该让循环等它 —— 丢后台。
            # blind 模式跳过：9router 停用（或不跑）时这项检查只会每 60s 刷
            # 一条配置不匹配的 warning，除误导外没有任何产出。
            if observe and now - last_proxy_check > CFG["intervals"]["proxy_check_sec"]:
                last_proxy_check = now
                _submit_once("proxy-config", check_9router_outbound_proxy)

            # ---- 1b. 自适应策略切换（带回滞） ----
            # 纯算术，留在主线程：切模式要立刻影响下一次选节点。
            # blind 模式没有 9router 错误率可算，_strategy_mode 保持 normal，
            # 池内选点维持延迟优先策略。
            if observe:
                error_rate = errors / total if total > 0 else 0
                with _state_lock:
                    old_mode = _strategy_mode
                    _strategy_mode = _update_strategy_mode(
                        _strategy_mode, error_rate, total,
                        CFG["thresholds"]["pressure_error_rate"], CFG["thresholds"]["normal_error_rate"],
                    )
                    switched = old_mode != _strategy_mode
                if switched:
                    log(f"🔄 策略切换: {old_mode} → {_strategy_mode} (429率={error_rate:.0%}, {errors}/{total}, 连续失败={consecutive_errors})")

            # ---- 1c. 错误触发（走 WARP 换线或池内轮换） ----
            # 两条路径都要扫 requestDetails（1.3ms / 3.3ms），且持 `_db_lock` ——
            # 20Hz 下等于每 tick 把后台干活的线程挡一次。只在两种情况下查：
            # 有新行（新错误必然带来新行，立刻判），或距上次判定已 1s（冷却期内
            # 被压下的那批错误要补打一次，不能等下一行新数据才换线）。
            # 9router 每 5s 批量落库，所以 1s 的补打已远密于数据产生的速度。
            # blind 模式整段跳过：错误触发换线只认 requestDetails 信号。
            if observe and (new_rows or now >= next_error_eval):
                next_error_eval = now + 1.0
                if opencode_out_on_warp():
                    # 只做隧道换线：opencode 走 WARP 时换轮换池节点无效（出口由
                    # usque 决定）。
                    # WHY 不能套用 _last_successful_switch_ts 门控：它由池轮换刷新，
                    # 用它当窗口起点会把爆发期间的 abuse 403 整段吞掉 —— 实测
                    # 15:47~15:52 连续 252 条 403 一次换线都没触发。
                    maybe_reconnect_warp(now)
                else:
                    # 用户要求：切换后只要出现新的错误就立即再切，不受冷却限制。
                    # max_backoff_ts=上次成功切换时刻，天然排除切换前的旧错误。
                    # 换节点要预检（最慢 30s），丢后台。
                    _, errors_after_switch, _, _, _ = get_9router_error_rate(
                        max_backoff_ts=_last_successful_switch_ts or None
                    )
                    if errors_after_switch >= 3:
                        _submit_once("pool-rotate-on-error", _rotate_on_error_bg,
                                     errors, total, cached_all_nodes, now)

            # 主动轮换不为「不等报错」—— 403 一出现，在途对话已经断了。
            # 放在水位线判断之外：fixed 模式的判据就是时长，没有新请求时
            # 恰恰更需要靠它来换（否则低峰期一条线会一直干着）。
            #
            # 但它只对 WARP 出口有意义：落在 proxy 池上时「WARP 出口已服务多久」
            # 无从谈起，照判据换线等于把流量从池里硬拽回 WARP，而错误触发路径
            # 5 秒后又把它踢回池 —— 实测 00:17:24 切回 WARP、00:17:29 又落池，
            # 两套兜底互相打架。回 WARP 只有一条路：`_maybe_failback_to_warp`。
            #
            # blind 模式这里就是唯一的 WARP 换线来源（错误触发整路关闭），
            # `_proactive_mode` 会强制 fixed，纯时钟判据。
            if opencode_out_on_warp():
                _maybe_proactively_rotate_warp(now)
            else:
                # 落在池上时本判据不适用于 WARP 出口；留着上一段的数字会让
                # 摘要看起来像「配额在涨」，实际根本没在计。
                _proactive_progress = "不适用(池)"

            # ---- 3. 主动轮换轮换池（只在 opencode 落在池上时才有意义） ----
            # 进度标注每 tick 刷新（纯 local time 格式化，O(1)）：换线间隔、
            # 已过时长、下次换线时刻三件套，格式见 _pool_proactive_label。
            if not opencode_out_on_warp():
                _POOL_PROACTIVE_LABEL = _pool_proactive_label(
                    now, last_proactive_rotate,
                    float(CFG["intervals"]["proactive_rotate_sec"]))
            else:
                _POOL_PROACTIVE_LABEL = "不适用(WARP)"
            if (not opencode_out_on_warp()
                    and now - last_proactive_rotate > CFG["intervals"]["proactive_rotate_sec"]):
                last_proactive_rotate = now
                if cached_all_nodes:
                    _submit_once("pool-proactive-rotate", _rotate_pool_proactive,
                                 cached_current_proxy, cached_all_nodes, now)

            # ---- 3b. 清理过期 node_info (每 10 分钟) ----
            if now - last_node_info_cleanup > 600:
                last_node_info_cleanup = now
                _submit_once("node-info-cleanup", _cleanup_node_info, cached_all_nodes)

            # ---- 3c0. 双向兜底的反向：停在 proxy 上时池挂了才切回 ----
            # 内部要探测整池健康（最慢 15s），丢后台。
            _maybe_failback_to_warp_bg(now)

            # 任一隧道出口疑似脏就重连；掉在 proxy 池上时也要继续，
            # 否则池本身就是脏地址时永远回不来。单实例探测器启用后由它独占
            # 探测/重启，旧补刷与 line_worker 都不再参与，避免同线双刷。
            if not prober_enabled and not _line_worker_owns_scheduling():
                _prewarm_standby_warp(now)

            # ---- 5. 节点健康检查 (每 300s) ----
            if now - last_health_check > CFG["intervals"]["node_health_sec"]:
                if _initial_health_done.is_set() and cached_all_nodes and cached_current_proxy:
                    with _state_lock:
                        if _health_check_future is None or _health_check_future.done():
                            _health_check_nodes = cached_all_nodes
                            _health_check_current = cached_current_proxy
                            _health_check_future = _io_pool.submit(
                                _run_health_check_async, cached_all_nodes, cached_current_proxy)
                    last_health_check = now
            else:
                _reap_health_check()

            # ---- 6. 订阅刷新 (每 1h) ----
            if now - last_sub_refresh > CFG["intervals"]["subscription_refresh_sec"]:
                _io_pool.submit(refresh_subscription)
                last_sub_refresh = now

            # ---- 7. Sing-box watchdog ----
            if now - last_singbox_check > CFG["intervals"]["singbox_watchdog_sec"]:
                last_singbox_check = now
                _submit_once("singbox-watchdog", _singbox_watchdog)

            # ---- 8. 节点调用计数重置 (每 20min，pressure 模式下跳过) ----
            if now - _NODE_CALL_RESET_TS > 1200:
                _NODE_CALL_RESET_TS = now
                if _strategy_mode == "pressure":
                    log("  ♻ 调用计数跳过重置 (pressure 模式，保持均匀分散)")
                else:
                    _submit_once("node-call-reset", _reset_node_call_counts)

            # ---- 8b. WAL checkpoint (每 5min) ---
            if now - _last_wal_checkpoint > 300:
                _last_wal_checkpoint = now
                _submit_once("wal-checkpoint", _wal_checkpoint)

            # ---- 8c. 回收外部落池 ----
            # 禁用落池后 selector 只应由本进程改；外部写入没有日志可追，
            # 只能每 tick 对账一次（内部自限 10s）。
            _reclaim_foreign_pool(now)

            # ---- 9. 健康摘要（每 10s 最多一条） ----
            with _state_lock:
                dead_count = len(_DEAD_NODES)
                cool_count = len(_COOLDOWN_NODES)
                err_threshold = 1 - CFG["thresholds"]["opencode_min_success_rate"]
            with _RESTART_LOCK:
                rc_display = _RESTART_COUNT
            cool_until = max(_warp_cooldown_until, _pool_cooldown_until)
            log_health(f"出口={_egress_label(cached_current_proxy)} "
                       f"节点={len(cached_all_nodes)} 错误={errors}/{total} "
                       f"err>{(err_threshold*100):.0f}%→切"
                       f"冷却至={time.strftime('%H:%M:%S', time.localtime(cool_until)) if cool_until>now else '-'}"
                       f" dead={dead_count} cool={cool_count} 策略={_strategy_mode}"
                       f" 观测={'on' if observe else 'off(blind→时间轮换)'}"
                       f" 轮换W={_proactive_progress}轮换P={_POOL_PROACTIVE_LABEL}"
                       f" singbox={'🟢' if rc_display==0 else '🔴'}")

        except KeyboardInterrupt:
            log("=== 退出 ===")
            break
        except Exception as e:
            log(f"✗ 主循环异常: {e}")
            log(traceback.format_exc())
        time.sleep(CFG["intervals"]["monitor_sec"])

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

def cli_ip_history(argv: list[str]) -> None:
    """CLI: 打印出口 IP 历史（重连次数 / 上次连接 / 上次被标记），按次数降序。"""
    limit, only = 20, None
    args = argv[1:]          # argv[0] 是子命令名 ip-history
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            try:
                limit = max(int(args[i + 1]), 1)
            except ValueError:
                print("--limit 需要正整数")
                sys.exit(2)
            i += 2
        elif args[i] == "--ip" and i + 1 < len(args):
            only = args[i + 1]; i += 2
        else:
            print("用法: daemon.py ip-history [--ip <addr>] [--limit N]")
            sys.exit(0 if args[i] in ("-h", "--help") else 2)

    stats = _warp_ip_history_stats()
    rows = [(ip, s) for ip, s in stats.items() if only is None or ip == only]
    if only is None:
        rows.sort(key=lambda r: (r[1]["uses"], r[1]["last_ts"]), reverse=True)
        rows = rows[:limit]
    if not rows:
        print("（无历史记录）")
        return
    print(f"{'IP':<44} {'重连':>4}  {'上次连接':<19} 被标记")
    for ip, s in rows:
        last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["last_ts"]))
        blk = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["last_blocked_ts"]))
               if s.get("last_blocked_ts") else "-")
        print(f"{ip:<44} {s['uses']:>4}  {last:<19} {blk}")


if __name__ == "__main__":
    import sys, signal

    def _sigterm_handler(signum, frame):
        log("收到 SIGTERM，优雅关闭中...")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)
    if len(sys.argv) > 1 and sys.argv[1] == "rotate":
        cli_rotate()
    elif len(sys.argv) > 1 and sys.argv[1] == "ip-history":
        cli_ip_history(sys.argv[1:])
    else:
        main()