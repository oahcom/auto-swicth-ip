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
import bisect
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
import secrets
import socketserver
import http.server
import traceback
import urllib.error
import urllib.request
import urllib3
from collections import deque
from datetime import datetime, timezone, timedelta
import re as _re
import logging
import atexit
import grp
import pwd
import shutil
import tempfile
import functools
import ipaddress
import math
from logging.handlers import RotatingFileHandler
from typing import NamedTuple
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 从共享配置导入
from config import CFG, NODE_INFO_DB, OPENCODE_FREE_PROVIDER, RECENT_WINDOW, REJECT_WINDOW, TRANSPORT_LOOKBACK_SEC, BAD_NODES_TTL, BAD_NODES_MAX_TTL, MIXED_PORT, LOG_FORMAT, get_singbox_secret, consumer_slots, consumer_slot_config_path
from asip.atomic_io import append_line as _atomic_append_line
from asip.atomic_io import write_json as _atomic_write_json
from asip.atomic_io import update_json as _atomic_update_json
from asip.atomic_io import write_text as _atomic_write_text
from asip.clash_client import ClashClient
from asip.egress import TRACE_URL, probe_egress
from asip.redact import redact_secrets as _redact_secrets

# WARP 渠道模型（块 = colo）与夜间锁策略。两者都是纯函数模块，import 它们不会
# 触发任何 IO；daemon 只在这里取策略，不再自己算窗口与白名单。
import warp_blocks
import warp_night
import warp_wg

# 项目根目录。`__file__` 在 daemon 里只有一个来源，其余函数不得自己再算一遍
# —— 各算各的在 AST 用例里会 NameError，在远端也容易漂移。
_PROJECT = os.path.dirname(os.path.abspath(__file__))

# Clash API 的唯一 HTTP 通道。daemon 侧保留 `singbox_api` 这个名字是因为
# 测试与兄弟工具都以它为边界；内部实现统一到 asip，避免每个调用点各写一遍
# 状态码/超时/JSON 解析。
_CLASH = ClashClient(CFG["singbox"]["clash_api"], get_singbox_secret)

# 每槽位独立 sing-box 的 Clash API 客户端缓存（按 clash_api_port 分桶）。
_SLOT_CLASH: dict = {}

def _slot_clash(slot) -> "ClashClient":
    port = slot["clash_api_port"]
    c = _SLOT_CLASH.get(port)
    if c is None:
        c = ClashClient("http://127.0.0.1:%d" % port, get_singbox_secret)
        _SLOT_CLASH[port] = c
    return c

def slot_singbox_api(slot, path, method="GET", body=None, timeout=3, silent=False):
    """调某个槽位 sing-box 的 Clash API（每台独立进程，独立端口）。"""
    try:
        status, data = _slot_clash(slot).request_json(method, path, body=body, timeout=timeout)
    except Exception as e:
        if not silent:
            log("slot %s Clash API 异常: %s" % (slot["name"], e))
        return 0, None
    if status is None:
        if not silent:
            log("slot %s Clash API 连接失败" % slot["name"])
        return 0, None
    return status, data

def _slot_by_proxy_url(url):
    """按代理 URL 的端口定位槽位：7890、7892-7896 -> slot-1..6。

    用 URL 解析而不是在整串里找第一个 `:数字`：带凭据的
    `http://user:pass@127.0.0.1:7892` 会被旧正则误取成 `pass@127...` 之前
    根本不存在的端口。只认环回主机，外部代理不存在「本机槽位」。
    """
    if not url or not isinstance(url, str):
        return None
    from urllib.parse import urlsplit
    raw = url.strip()
    try:
        p = urlsplit(raw if "://" in raw else "//" + raw)
        if (p.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            return None
        port = p.port
    except (ValueError, TypeError):
        return None
    if not port:
        return None
    for s in consumer_slots():
        if s["port"] == port:
            return s
    return None


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
# ===== 轮换池（订阅节点） vs WARP 渠道（隧道）的状态边界 =====
#
# 渠道 = 隧道线（WG/H2）、它的出口 IP、落地的 colo/块 —— 状态前缀 `_WARP_*`。
# 池  = 机场订阅节点 + selector `proxy` —— 状态是下面这几个。
#
# 两边**不共享**变量：池的 dead/cooldown/delay 只描述订阅节点能不能连，
# 渠道的探测/换线/块归属全走 `_WARP_*` 与 warp_wg。唯一的历史耦合是
# `_export_dead_nodes_locked` 把池的死节点写进 `.bad_nodes.json`（原本给
# 已删除的 proxy-global.js 读），见那边的注释。
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
# 换线历史：[(生效时刻, tag), ...]。只用来回答「某个时刻哪条线是出口」——
# `_last_warp_switch_ok_ts` 只有最近一次，把两次换线之间的空档也会算成后一条
# 线的存活，回答不了「这条 429 在途时走的是谁」。
# cap 128：客户端的换线请求最多迟到几十秒（实测请求耗时 p90=26s / max=37s），
# 一分钟内的换线次数远到不了这个深度。
_WARP_SWITCH_HISTORY: list[tuple[float, str, str | None]] = []
_WARP_SWITCH_HISTORY_MAX = 128
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


def _record_udp_bad_node(node: str, now: float) -> None:
    """实测握手失败（TCP 通但拿不到出口）的节点落盘，供 `warp_wg.udp_capable`
    过滤。不设过期：节点 UDP 能力是相对稳定的网络属性，靠后续成功握手清除
    （`_rehome_one` 拿到出口时会调 `_udp_bad_clear`，见下）。"""
    try:
        path = warp_wg.udp_bad_nodes_path()
        data = {}
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        data[str(node)] = {"ok": False, "ts": now}
        _write_json_atomic(str(path), data)
    except Exception as e:
        log(f"✗ 写 udp_bad_nodes 失败: {e}")


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


def _save_segment_start(ts: float) -> None:
    """段起点落盘：重启后同一出口继续按同一段累计 150 格配额。"""
    try:
        _write_json_atomic(_segment_path(),
                           {"ts": float(ts), "ip": _ACTIVE_WARP_IP})
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


# 进程内是否真的换过线。与 `_last_warp_switch_ok_ts > 0` 不等价：那个值在
# 启动时由 `_load_segment_start()` 从 `.warp/ipv6_segments.json` 恢复（同一个
# 出口 IP 上一段的起点），用来给 `count_warp_blocked_since` 切窗口。它不是
# 「刚换过线」—— 重启前跑了两小时的段，恢复出来是两小时前；而重启前刚切过
# 的段，恢复出来是几秒前。后者会让证据闸在刚启动时把换线锁死最多 120 秒，
# 而那正是出口可能最脏的时刻（sing-box 刚复位到配置里的 default）。
_SWITCHED_IN_PROCESS = False


def _begin_segment(ts: float) -> None:
    global _last_warp_switch_ok_ts, _SWITCHED_IN_PROCESS
    _last_warp_switch_ok_ts = float(ts)
    # 只有这个入口代表「本进程真的把流量切到了新出口」，证据闸才认它。
    _SWITCHED_IN_PROCESS = True
    _save_segment_start(ts)


def _record_warp_switch_history(tag: str | None, at: float,
                                ip: str | None = None) -> None:
    """记一次换线生效，供 `_warp_line_at` 回溯。

    写入点必须在 selector 真的翻过去之后 —— 记在发起时会把「没翻成的换线」
    也算进去，回溯时给出错误的线。`tag` 为 None 表示出口切到了轮换池：必须
    落一条记录，否则落池期间回溯会停在落池前最后一条隧道上，把一条早已不
    承载流量的线判成「这条 429 的发出者」。

    `ip` 必须**当时**取，不能在回溯时再问 `_warp_line_ip(tag)`：那条线在 429
    迟到的几十秒里很可能已被重启换了会话地址（实测同 tag 相邻判定间隔
    10~30s 时 99.4% 换了地址），事后取到的是新地址，会把干净的记成脏。

    刻意不取 `_WARP_LOCK`：调用方 `_switch_warp` 常运行在已持有该锁的换线
    线程里（锁跨线程持有到 worker 的 finally），再取一次就是死锁。
    `list.append` / 切片在 CPython 下是原子的，多线程同时追加只会让相邻两条
    记录的时刻先后微调，不会损坏结构。
    """
    if tag is None:
        # 落池：必须落一条「当时不在任何隧道上」的记录。漏了它，回溯会停在前
        # 一条隧道上，把一条早已不承载流量的线判成这条 429 的发出者。
        _WARP_SWITCH_HISTORY.append((float(at), None, None))
    else:
        _WARP_SWITCH_HISTORY.append((float(at), str(tag), ip or None))
    if len(_WARP_SWITCH_HISTORY) > _WARP_SWITCH_HISTORY_MAX:
        del _WARP_SWITCH_HISTORY[:-_WARP_SWITCH_HISTORY_MAX]


# ===== 换线表（exit ledger）=====
#
# `_WARP_SWITCH_HISTORY` 记的是「某时刻哪条线是出口」，用来把迟到的 429 回溯到
# 发出时的线。它记不住**旧地址去哪了** —— 而调用方（9router）手里只有地址，
# 而且可能是几分钟前缓存的地址。要回答「这条 429 追的地址还在不在承载流量」，
# 需要的是一张 (旧地址 → 新地址, 切换时刻) 的表。
#
# 为什么必须带时刻：只比 IP 是否相等，遇到**主动换线**（主循环、探测判死、
# 定时轮换，全都不经过 9router）就会误判 —— 客户端缓存停在旧地址上，永远
# 判成「已换走」，于是每次都回 already_retired、从不换线，彻底卡死。
# 带上切换时刻才能分辨：
#   - 429 的请求时刻 **早于** 那次切换 → 它打在旧出口上，隧道已经换掉了，
#     重连即可；
#   - 请求时刻 **晚于** 切换 → 它打在新出口上，客户端缓存过期，必须再换线。
#
# 只留最近 N 条：这是一张旁路证据表，不是永久审计日志。地址在表里查不到
# 就等于「无从判断」，退回正常换线（多烧一格配额，不会卡死）。
_EXIT_LEDGER: list[dict] = []
_EXIT_LEDGER_MAX = 64
_EXIT_LEDGER_LOCK = threading.Lock()


def _exit_ledger_note(old_ip: str | None, new_ip: str | None,
                      at: float, tag: str | None = None) -> None:
    """记一次出口地址变更：old_ip 从此刻起不再是出口，new_ip 接替。

    `新出口凭什么被判定可用` 一并记下 —— 这是排障里最容易被误读的一格：
    `OK` 全部来自模型层探测，而探测请求极小、与真实业务流量不是一回事。
    实测 2026-11-05 21:47：WARP-H2-SJC-35 探测 OK，切上去后业务请求照样 429。
    不标来源，「表里说 OK」就会被当成「这条线能干活」。
    """
    proof = _exit_ledger_proof(tag, new_ip) if new_ip else None
    with _EXIT_LEDGER_LOCK:
        _EXIT_LEDGER.append({
            "old": old_ip or None,
            "new": new_ip or None,
            "at": float(at),
            "tag": tag,
            "ok": proof,
        })
        if len(_EXIT_LEDGER) > _EXIT_LEDGER_MAX:
            del _EXIT_LEDGER[:-_EXIT_LEDGER_MAX]


def _exit_ledger_proof(tag: str | None, ip: str | None) -> dict | None:
    """这条出口被记为可用的依据。

    三种来源，判读方式完全不同：
      - `probe`  —— 刚做的模型层实测。`ok_ts` 就是本次检查的时刻。
      - `ttl`    —— **复查**结果来自 TTL 缓存：这个地址在 `ok_ts` 那时验过，
                    此刻没有重验。地址在这期间被生产流量烧掉，这里看不出来。
      - `none`   —— 没有模型层结论（探测关闭 / 从没探过 / 结论已过期）。

    探测的请求体是固定的极小 payload（见 probe_warp.py），成功只说明这个出口
    此刻能通过 opencode 免费层的**指纹与配额**两道闸，不代表它能承载实际业务量。
    """
    if not tag:
        return None
    try:
        with _WARP_LINE_CV:
            st = _warp_line_state_locked(tag)
            ok_ip = st.get("last_ok_ip")
            ok_ts = float(st.get("last_ok_ts") or 0.0)
            cur_ip = st.get("ip")
        if not ok_ts or ok_ip != cur_ip or cur_ip != ip:
            return {"src": "none", "ok_ts": ok_ts or None}
        verdict = (_PROBE_LAST_VERDICT or {}).get(tag)
        # 探测刚做过时 `last_probe_ts` 与 `last_ok_ts` 同一刻；TTL 复用则
        # `last_probe_ts` 不变。用两者是否相等区分，不需要另存标志。
        dec = ("ttl" if abs(ok_ts - float(st.get("last_probe_ts") or 0.0)) > 0.5
               else "probe")
        return {"src": dec, "ok_ts": ok_ts, "cached": verdict}
    except Exception:
        return None


def _exit_ledger_since(ip: str, before_ts: float) -> float | None:
    """`ip` 是在哪个时刻之后不再是出口的；查不到返回 None。

    `before_ts` 是调用方发出那条 429 请求的时刻 —— 只在「这次请求发出之后」
    发生的切换才算数。一次请求可能在同一个地址上跑了几十秒，期间出口换了两次；
    取最后一次「旧地址是这个 ip」的记录即可，那才是它失效的时刻。

    查不到分两种情况，都返回 None（调用方退回正常换线）：
      - 这个地址从没进过表（客户端缓存早于表容量，或根本不是 WARP 地址）；
      - 它进过表，但失效时刻早于这次请求 —— 说明请求发出时它还是出口。这时
        「已换走」不成立：请求打的是一个当时有效的出口。
    """
    if not ip:
        return None
    with _EXIT_LEDGER_LOCK:
        hits = [e["at"] for e in _EXIT_LEDGER if e["old"] == ip]
    if not hits:
        return None
    at = max(hits)
    return at if at > before_ts else None


def _exit_ledger_since_install(ip: str | None) -> float | None:
    """这个地址是**什么时候**成为出口的；查不到返回 None。

    与 `_exit_ledger_since` 互补：那个回答「什么时候被换走」，这个回答「什么时候
    接上的」。调用方要判断「我缓存的那个地址此刻还作不作数」，两个都需要 ——
    只知道它没被换走，不等于知道它已经在这一位待了多久。

    来源按可信度排：
      1. 换线表里 `new == ip` 的最新一条 —— 那次切换就是它的起点；
      2. `_WARP_SWITCH_HISTORY` 里该地址的最新一条 —— 原地重连（selector 不动、
         只有地址变）只写这里，换线表也会写，但它是过程记录更全的一份。
    都没有说明这个地址早于本进程的记录窗口，返回 None —— 调用方退回「不知道」。
    """
    if not ip:
        return None
    with _EXIT_LEDGER_LOCK:
        hits = [e["at"] for e in _EXIT_LEDGER if e.get("new") == ip]
    if hits:
        return max(hits)
    # 换线表只留 64 条，长时间没换线时会把起点挤出去；过程历史留 128 条兜底。
    try:
        hist = [h[0] for h in list(_WARP_SWITCH_HISTORY) if h[2] == ip]
    except Exception:
        return None
    return max(hist) if hist else None


def _warp_line_at(ts: float) -> tuple[str | None, str | None] | None:
    """`ts` 那一刻的 (出口线, 该线的出口地址)。三态，调用方必须分开处理：

    - `None` —— **不知道**（历史是空的、或 ts 早于现存首条）。历史被截断过，
      首条之前那段无从得知，这时只能按旧行为用当前线。
    - `(None, None)` —— **知道，且那一刻不是任何隧道**（停在轮换池）。那条 429
      属于池节点，不归任何隧道，不许标脏。
    - `(tag, ip)` —— 那一刻出口是 tag，ip 是它当时的地址（可能为 None）。

    地址必须随记录一起给，不能在调用处再问 `_warp_line_ip(tag)`：那条线在 429
    迟到的几十秒里很可能已被重启换了会话地址（实测同 tag 相邻判定间隔 10~30s
    时 99.4% 换了地址），事后取到的是新地址，会把干净的记成脏。

    用二分而不是从尾部扫：历史按时间单调递增，尾部扫描在「429 迟到 37 秒、
    其间换了十几次线」时要走十几步，而二分恒定。

    键和记录必须**从同一份快照**里取。先 `[h[0] for h in hist]` 算索引、再
    `hist[i]` 取值是错的：写线程的 `del hist[:-MAX]` 会让列表整体前移，同一个
    索引就指向另一条记录 —— 实测并发压测 20 万次读给出 21 次**错误的线**
    （真值 L100，返回 L101），尾部还会 IndexError。这里先切片复制，之后所有
    下标都只在副本上算。

    不取 `_WARP_LOCK`：换线 worker 会跨线程持锁数秒（`_reconnect_warp_now`
    抢锁、worker 的 finally 才释放），这里取锁会把 /rotate 一起卡住、超过调用方
    2s 超时。

    **排序不能省**：写入点记的是「这条路什么时候知道地址变了」，不是「地址什么
    时候生效」。`_restart_warp_inplace` 在模型实测**之前**取 `now`，实测要 1.4~8.7s
    （实测区间），期间主循环可能又换了一次线 —— 于是列表里出现 `[(t+3, C), (t, B)]`。
    对非单调序列二分不是「差一格」，是返回**另一条线**：实测 `ts=t+4` 真实出口是
    C，返回 B。`sorted` 是稳定排序，时刻相同的两条保持追加先后，与无排序时的
    「后追加者胜」一致。
    """
    hist = list(_WARP_SWITCH_HISTORY)
    if not hist:
        return None
    hist.sort(key=lambda h: h[0])
    i = bisect.bisect_right([h[0] for h in hist], ts) - 1
    if i < 0:
        return None
    return hist[i][1], hist[i][2]


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
#     线**仍能服务**（换线只改 selector，在途请求还走它），
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
# 跨线可复用的结论：判的是地址（配额 / 信誉），不是某条隧道当时通不通。
_WARP_PROBE_ADDRESS_SCOPED = _WARP_PROBE_OK | _WARP_PROBE_PERSISTENT
# 正在探测中的 (tag -> ip)：用来量「同一地址是否被多条线同时探」。
_PROBING_IP: dict[str, str] = {}

# 模型探测结论 → 生产侧惩罚类型。quota 是 FreeUsageLimitError（配额耗尽，24h），
# flagged 是 abusive traffic（信誉标记，6h）—— 与 `_classify_warp_error` 归出的
# rate429 / abuse403 完全同义，只是观测来源从生产流量换成了主动探测。
_MODEL_VERDICT_BLOCK_KIND = {"quota": "rate429", "flagged": "abuse403"}


# 探测缓存命中统计，暴露在 /debug/state，用来回答「同一地址被探了几次」。
_PROBE_STATS = {"hit": 0, "miss": 0, "cross_tag_reuse": 0, "expired": 0,
                "inflight_dup": 0}


def _probe_ttl(verdict: str) -> float:
    w = _warp()
    if verdict in _WARP_PROBE_PERSISTENT:
        return float(w.get("probe_persistent_ttl_sec", 21600))
    if verdict in _WARP_PROBE_OK:
        return float(w.get("probe_ok_ttl_sec", 1800))
    return float(w.get("probe_transient_ttl_sec", 120))


def _probe_cache_lookup(tag: str, ip: str,
                        now: float | None = None) -> tuple[str, float] | None:
    """命中返回 (结论, 落账时刻)；None 表示该探。

    落账时刻要单独给出来：调用方判「能不能靠这条 OK 跳过实测」看的是**年龄**，
    而缓存的 TTL（600s）只说明结论还作数，不说明它还新鲜。"""
    if now is None:
        now = time.time()
    with _WARP_REFRESH_LOCK:
        hit = _WARP_PROBE_SEEN.get((tag, ip))
        if hit is not None:
            verdict, ts = hit
            if now - ts <= _probe_ttl(verdict):
                _PROBE_STATS["hit"] += 1
                return verdict, ts
            del _WARP_PROBE_SEEN[(tag, ip)]
            _PROBE_STATS["expired"] += 1
        best: tuple[float, str] | None = None
        for (t2, ip2), (v2, ts2) in _WARP_PROBE_SEEN.items():
            if ip2 != ip or t2 == tag:
                continue
            # 只复用「关于地址」的结论。unreachable / err / unknown 说的是
            # **这条隧道**当时没跑通，换条线同样的地址完全可能探通 —— 复用
            # 它等于拿 A 线的故障去否决 B 线的地址，白丢一次供给。
            if v2 not in _WARP_PROBE_ADDRESS_SCOPED:
                continue
            if now - ts2 > _probe_ttl(v2):
                continue
            if best is None or ts2 > best[0]:
                best = (ts2, v2)
        if best is not None:
            _PROBE_STATS["cross_tag_reuse"] += 1
            return best[1], best[0]
        _PROBE_STATS["miss"] += 1
        return None


def _probe_cache_get(tag: str, ip: str, now: float | None = None) -> str | None:
    """这条线在 TTL 内探过这个地址就复用结论，None 表示该探。"""
    hit = _probe_cache_lookup(tag, ip, now)
    return hit[0] if hit else None


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


def _probe_verdict_for_ip(ip: str | None,
                          now: float | None = None) -> str | None:
    """按出口 IP 反查最近一次**地址级**实测结论（quota / OK / ...）。

    排查用：/rotate 拿到的只有「请求发出时那个出口 IP」，不知道它当时挂在
    哪条线上；而 quota 是按 /128 记的、跨线复用（见 _probe_cache_lookup 的
    地址级分支）。按 IP 反查才能直接回答「这条请求撞上的那个地址此前是否
    已被实测判 quota」，不必拿事后的多线日志去凑时间戳。

    只认地址级结论：unreachable / err 说的是那条隧道当时没跑通，换条线同
    样的地址完全可能探通，拿来当这个地址的结论会误判。返回 None 表示
    「TTL 内没探过这个地址」，不是「这个地址没问题」。

    「换线前实测」的结论写在 _PROBE_BEFORE_ROTATE（tag|ip），是同一台探测器
    对同一地址的实测、只是另一张台账；主表没命中时回退查它，否则刚做过的
    OK/quota 判定会被漏掉。
    """
    if not ip:
        return None
    if now is None:
        now = time.time()
    best: tuple[float, str] | None = None
    with _WARP_REFRESH_LOCK:
        # list() 是快照：探测线程会并发写这张表，直接迭代可能 RuntimeError。
        for (_tag2, ip2), (v2, ts2) in list(_WARP_PROBE_SEEN.items()):
            if ip2 != ip or v2 not in _WARP_PROBE_ADDRESS_SCOPED:
                continue
            if now - ts2 > _probe_ttl(v2):
                continue
            if best is None or ts2 > best[0]:
                best = (ts2, v2)
    if best is not None:
        return best[1]
    fallback: tuple[float, str] | None = None
    try:
        for key, rec in list(globals().get("_PROBE_BEFORE_ROTATE", {}).items()):
            if not isinstance(key, str) or not key.endswith("|" + ip):
                continue
            if not isinstance(rec, dict):
                continue
            v = rec.get("verdict")
            if v not in _WARP_PROBE_ADDRESS_SCOPED:
                continue
            ts = float(rec.get("ts") or 0)
            if now - ts > _probe_ttl(v):
                continue
            if fallback is None or ts > fallback[0]:
                fallback = (ts, v)
    except Exception:
        fallback = None
    return fallback[1] if fallback else None


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
    # 档 3 才是「出现过却一次 OK 都没拿到」。档 1/2 都不是低质：档 1 是探针
    # 刚验过、此刻确实能服务，档 2 是撑过生产流量（只是没有新鲜探针结论）。
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
# 最近一次 `warp_egress_ip` 拿到的完整 trace（port/ip/colo/loc）。
# 只用来把落点顺手记进 `wg_colo_map.json`；带上 port 是为了让调用方确认
# 「这条结果确实是我这次探测的」，并发探测时不会记到别人的 colo。
_LAST_EGRESS_TRACE: dict = {}
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
# 连续「等不到备线」的次数。备线判据是「120s 内出现过 OK」，全池惩罚期里
# 该条件永远无法满足（没 OK 就没备线 → 不换线 → 更不会 OK）。连续多次走到
# 这条死路才允许落池，避免一条线抖动就把流量甩到池上。见 `_warp_exhaustion_escape`。
_WARP_EXHAUST_STREAK: int = 0
_NODE_DELAYS: dict[str, int] = {}  # 节点延迟缓存 (ms)
# 延迟样本的落账时刻。没有它，_NODE_DELAYS 只能「有/无」二值：一次成功的
# 快速预检就把节点钉成快节点，之后永远不再更新 —— 实测池子 142 个节点里
# 只有 3 个有延迟数据，P2C 的「延迟优先」退化成随机（49 个活节点里只有
# 26.5% 的选中落在活的上面）。超过 _DELAY_TTL 的样本按「未知」处理。
_NODE_DELAY_TS: dict[str, float] = {}
_NODE_DELAY_TTL = 900.0
# 有这以内的成功样本就不再做切换前预检。与 TTL 分开：TTL 决定「还能不能拿它
# 排序」，这个决定「还需不需要花 1.5s 验一次连通」。3 分钟足够短，坏掉的节点
# 会被健康检查按自己的节奏抓出来。
_PROBE_SKIP_FRESH_SEC = 180.0
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
    """有界线程池：队列深度默认 max_workers*2，防止 Clash API 挂起队列暴涨 OOM。
    非阻塞：队列满时丢弃任务并记录警告，不阻塞 asyncio 事件循环。

    `queue` 单独可调：首轮健康检查会在**同一瞬间**提交全部机场节点，而节点数
    随订阅增长（实测 2026-11-03 已到 122 个）。队列深度按 worker 数推导时，
    122 个任务打 120 个名额，启动那一秒固定丢 2 个，节点延迟表就缺两条。
    """
    __slots__ = ('_executor', '_sem', '_name')
    def __init__(self, max_workers: int, name_prefix: str, queue: int | None = None):
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=name_prefix)
        self._sem = threading.Semaphore(queue if queue is not None else max_workers * 2)
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
# 队列给到 4×worker：首轮会把订阅里的**全部**节点一次性提交，队列深度必须
# 覆盖节点数而不是 worker 数的倍数。256 对当前 160 条订阅节点留了余量。
_health_pool = _BoundedPool(max_workers=60, name_prefix="health", queue=256)
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

# ── 第七闸口独立日志 ──────────────────────────────────────────────────────
# 口径与 deploy/asip-cli.sh 的 _RE_GATE 一致：这里命中哪些行，gate.log 就收
# 哪些行，「单独开来」才有意义。含 slot-7 的动态行（"⇢ slot-7 报…"、
# "…重试改走 slot-7…"）同样会命中 —— 这些是频率最高的一批。
_GATE_LOG_RE = _re.compile(r"第七闸口|秒切兜底|slot-7|池闸|代理池兜底|池内轮换")
_GATE_LOG_MAX_BYTES = 5 * 1024 * 1024
_GATE_LOG_BACKUP_COUNT = 5


class _GateLineFilter(logging.Filter):
    """只放行池闸行：挂在 gate.log 的 handler 上。"""

    def filter(self, record):
        return bool(_GATE_LOG_RE.search(record.getMessage()))


class _MainLineFilter(logging.Filter):
    """独占模式下主 daemon.log 排除池闸行；mirror 模式不挂。"""

    def filter(self, record):
        return not _GATE_LOG_RE.search(record.getMessage())


def _is_gate_handler(h) -> bool:
    return isinstance(h, RotatingFileHandler) and getattr(h, "_asip_gate", False)


def _side_effects_off() -> bool:
    """本进程是不是"不该留下痕迹"的那种。

    2026-10-28 四轮审查（严重）：只要有人 import 这个模块调一下 log()，假事件
    （"✗ 刷新节点数骤降 (39 → 17)"）就会写进生产 daemon.log。事后 grep 会得出
    "线上拦了 3 次"的结论，而真相是一次都没拦过 —— 日志是运维唯一的账本。
    所以：**只有 systemd 拉起的那个进程才写文件**（systemd 会设 INVOCATION_ID），
    其它情况（pytest、审查脚本、手工跑）一律只写 stdout。
    """
    # 顺序有讲究：先认"这是测试/演练"，再认后门。反过来的话，只要有人把
    # ASIP_ALLOW_FILE_LOG=1 写进 .env（unit 直接吃这个文件），pytest 就会重新
    # 往生产 daemon.log 写假事件 —— 四轮定的那条"严重"一行环境变量就重开了。
    if "pytest" in sys.modules or os.environ.get("ASIP_NO_SIDE_EFFECTS") == "1":
        return True
    if os.environ.get("ASIP_ALLOW_FILE_LOG") == "1":
        return False
    return not os.environ.get("INVOCATION_ID")


_logger_retry_ts = 0.0             # 上次尝试建文件 handler 的时间


def _logger_has_file() -> bool:
    try:
        import logging.handlers as _lh
        return any(isinstance(h, _lh.RotatingFileHandler)
                   and not getattr(h, "_asip_gate", False)
                   for h in _logger.handlers)
    except Exception:
        return False


def _logger_has_gate_file() -> bool:
    try:
        import logging.handlers as _lh
        return any(isinstance(h, _lh.RotatingFileHandler)
                   and getattr(h, "_asip_gate", False)
                   for h in _logger.handlers)
    except Exception:
        return False


def _init_gate_log_handler():
    """给 _logger 挂上 gate.log 的专属 handler（只收池闸行）。

    与主文件 handler 完全独立：gate.log 打不开/被 root 改了属主，最多损失
    这一份副本，主 daemon.log 与 journal 照常。同样只有 systemd 拉起的进程
    才建文件 handler —— pytest / 手工 import 不许往生产 gate.log 写假事件。
    """
    if _logger is None or _side_effects_off() or not CFG.get("gate_log_file"):
        return
    try:
        gh = RotatingFileHandler(CFG["gate_log_file"],
                                 maxBytes=_GATE_LOG_MAX_BYTES,
                                 backupCount=_GATE_LOG_BACKUP_COUNT)
        gh._asip_gate = True        # _logger_has_file() 靠它区分主/池闸 handler
        gh.setLevel(logging.DEBUG)
        gh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s",
                                          datefmt=LOG_FORMAT))
        gh.addFilter(_GateLineFilter())
        try:
            os.chmod(CFG["gate_log_file"], 0o600)
        except OSError:
            pass
        _logger.addHandler(gh)
    except Exception as e:
        print(f"⚠ 第七闸口日志打不开（{e}），池闸行仍写 stdout/journal",
              file=sys.stderr)


def _init_logger():
    global _logger, _logger_retry_ts
    _logger_retry_ts = time.time()
    if _logger is not None:
        return
    _logger = logging.getLogger("auto-switch-ip")
    _logger.setLevel(logging.DEBUG)
    # 文件 handler（轮转）。
    # 2026-10-28 四轮审查（严重）：这里以前没有兜底。daemon.log 一旦变成
    # root:root 0600（有人用 root 手工跑过一次就够），open("a") 抛 PermissionError
    # → 进程启动即崩 → systemd Restart=always 无限重启，且没有任何自愈路径。
    # 日志写不出来不该让整条代理链停摆：打不开就只留 stdout handler（进 journal）。
    fh = None
    if not _side_effects_off():
        try:
            fh = RotatingFileHandler(CFG["log_file"], maxBytes=_LOG_MAX_BYTES,
                                     backupCount=_LOG_BACKUP_COUNT)
        except Exception as e:
            print(f"⚠ 日志文件打不开（{e}），本次只写 stdout/journal；"
                  f"修法：sudo chown administrator {CFG['log_file']}", file=sys.stderr)
            fh = None
    # 日志会带订阅源/探测细节，默认 0644 等于对同机其他用户公开；台账与
    # .env 都是 0600，日志不能例外。**只在真拿到了文件 handler 时才 chmod** ——
    # 否则 pytest / 手工 import 也会去改生产日志的权限，"只读不碰"就是句空话。
    if fh is not None:
        try:
            os.chmod(CFG["log_file"], 0o600)
        except OSError:
            pass
    if fh is None and _side_effects_off():
        # 手工起的那份守护进程没有文件日志 —— 这件事本身要有痕迹，否则排查时
        # 会误以为"代码没跑"。
        print("（本进程不是 systemd 拉起的：只写 stdout，不写 daemon.log、"
              "不记护栏样本）", file=sys.stderr)
    if fh is not None:
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt=LOG_FORMAT))
        if not CFG.get("gate_log_mirror"):
            fh.addFilter(_MainLineFilter())   # 独占：池闸行只进 gate.log
        _logger.addHandler(fh)
    # stdout handler：journal 必须保持全量，不挂任何 filter —— 拆文件不能
    # 把 journalctl 的统一排障链路弄断。
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG)
    sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt=LOG_FORMAT))
    _logger.addHandler(sh)
    _init_gate_log_handler()

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
    """写日志到文件（轮转）+ stdout，线程安全。

    2026-10-28 五轮审查（中）：`_init_logger()` 一旦跑过就不再重建，所以"文件
    handler 建失败"是**一次性判定** —— root 把 daemon.log 变成 root:root 之后，
    就算人把属主修好，这个进程到死也不会再写文件。现在每隔 5 分钟重试一次。
    """
    global _logger
    # 重建必须和 emit 在同一把锁里。第六轮实测：重建在锁外时，另一线程正好
    # emit，会撞上 handlers 已被清空的瞬间 —— 720 条里丢 120 条（17%），而且
    # 丢的那些连 stdout 都没有（record 根本没被任何 handler 拿到）。重建本来是
    # 为了把账本修回来，结果每修一次就撕一页。
    with _log_lock:
        try:
            # 文件 handler 还在、但文件被人删了：fd 还指着已删除的
            # inode，日志静默写进虚空，直到进程重启。这种情况
            # _logger_has_file() 看不出来，得看路径还在不在。
            # gate.log 同样要查：两份账本要么都在、要么一起重建，别让
            # 其中一份静默丢行（池闸日志恰恰是排 429 时最需要的那份）。
            gate_ok = (not CFG.get("gate_log_file")
                       or (_logger_has_gate_file()
                           and os.path.exists(CFG["gate_log_file"])))
            stale = (_logger is not None and not _side_effects_off()
                     and time.time() - _logger_retry_ts > 300
                     and (not _logger_has_file()
                          or not os.path.exists(CFG["log_file"])
                          or not gate_ok))
            if stale:
                for h in list(_logger.handlers):
                    try:
                        h.close()
                    except Exception:
                        pass
                _logger.handlers.clear()
                _logger = None
        except Exception:
            pass
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


def _classify_transport_error(body: str | None) -> bool:
    """9router 自身传输层故障（Bun sidecar 被杀/冷却）产生的 503。

    与出口无关——换线/轮换对它无效，所以不计进出口级 errors，也绝不能记
    惩罚台账。但 2026-10-26 实测 1338 条 503 爆发期间健康摘要恒显「错误=0/5」，
    主人看到的不是无错，而是 asip 失明——必须单独计数并告警。
    """
    return bool(body) and "UpstreamTransportError" in body


_LAST_503_WARN = 0.0


def _warn_transport_503(consecutive: int) -> None:
    """sidecar 挂起告警，10 分钟节流（503 批量落库下每秒都会重触）。

    处理路径见 `_verify_transport_failure`：先探出口，探不通换线，探通则清理
    僵死的 opencode 连接（换线后旧链的 keep-alive 连接会被继续复用）。
    """
    global _LAST_503_WARN
    now = time.time()
    if now - _LAST_503_WARN < 600.0:
        return
    _LAST_503_WARN = now
    log(f"⚠ [OpenCode Free] 连续 {consecutive} 条 503 UpstreamTransportError —— "
        f"等待出口探测：不通则换线，通则清理僵死连接（见 transport-probe）")


_LAST_503_CLASSIFY_WARN = 0.0


def _response_status(body: str | None) -> int | None:
    """从 requestDetails.data 里取上游 HTTP 状态码；取不到返回 None。"""
    try:
        resp = (json.loads(body or "{}") or {}).get("response") or {}
        return int(resp.get("status"))
    except (ValueError, TypeError, AttributeError):
        return None


def _warn_unclassified_503(unclassified: int) -> None:
    """503 存在却一条都没被认成传输层 → 分类器可能已失效，10 分钟节流告警。

    WHY 要这条：`_classify_transport_error` 靠 body 里的
    "UpstreamTransportError" 字面量。9router 一改文案，503 就静默回落
    other_err，健康摘要 503 归零 —— 观测失效比故障本身更难发现。
    """
    global _LAST_503_CLASSIFY_WARN
    now = time.time()
    if now - _LAST_503_CLASSIFY_WARN < 600.0:
        return
    _LAST_503_CLASSIFY_WARN = now
    log(f"⚠ [OpenCode Free] 最近 {unclassified} 条 503 未被识别为传输层故障 —— "
        f"9router 文案可能已变，`_classify_transport_error` 需更新")


class ErrorWindow(NamedTuple):
    """`get_9router_error_rate` 的返回结构（保持 tuple 解包兼容）。"""

    total: int
    errors: int
    details: list[dict]
    consecutive_errors: int
    reject_ratio: float
    # 尾部带默认值：`ErrorWindow(0, 0, [], 0, 0.0)` 的早退构造无需同步。
    transport_errors: int = 0


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
            # 传输层错误按**时间**回看，不按行数：高流量时 RECENT_WINDOW(5) 条
            # 只覆盖 ~2s，503 在 10s tick 前就被新行挤出窗口，检测永远为 0，
            # 僵死连接就不会被清理。LIKE 走全表但有 timestamp 索引先缩小范围。
            transport_cutoff = (datetime.now(timezone.utc)
                                - timedelta(seconds=TRANSPORT_LOOKBACK_SEC)
                                ).strftime("%Y-%m-%dT%H:%M:%S")
            if max_backoff_ts is not None:
                backoff_cutoff = datetime.fromtimestamp(
                    max_backoff_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                if backoff_cutoff > transport_cutoff:
                    transport_cutoff = backoff_cutoff
            transport_recent = c.execute("""
                SELECT COUNT(*) FROM requestDetails
                WHERE provider = ? AND timestamp >= ?
                  AND data LIKE '%UpstreamTransportError%'
            """, (OPENCODE_FREE_PROVIDER, transport_cutoff)).fetchone()[0]
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

        # 503 单独观测：不计入 errors（换线无效），但连续 ≥3 必须告警。
        # 连续性判定：503 之外遇任何其他行（success 或真 429/403）都断——
        # 能拿到 429 说明 sidecar 活着，故障已结束。
        transport_errors = sum(1 for r in recent
                               if _classify_transport_error(r.get("data")))
        # 窗口内可能一条都没有（见 transport_cutoff 注释），取时间回看的最大值。
        transport_errors = max(transport_errors, int(transport_recent or 0))
        if transport_errors:
            consecutive_503 = 0
            for r in recent:
                if r.get("status") == "success" or \
                        _classify_egress_error(r.get("data")):
                    break
                if _classify_transport_error(r.get("data")):
                    consecutive_503 += 1
                else:
                    break
            if consecutive_503 >= 3:
                _warn_transport_503(consecutive_503)
        else:
            unclassified_503 = sum(1 for r in recent
                                   if _response_status(r.get("data")) == 503)
            if unclassified_503:
                _warn_unclassified_503(unclassified_503)

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

        return ErrorWindow(total, errors, recent, consecutive_errors, reject_ratio,
                           transport_errors)
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
            # 全新 9router 的 settings 表是空的：initDatabase 只建表，默认值由
            # mergeWithDefaults 在内存里补。以前这里直接 return False，于是下面的
            # 全局 outboundProxyUrl 永远不会被写 —— 表现为「部署脚本说指到 7890 了」，
            # 实际 9router 一直走直连。只写我们关心的两个键，其余交给 9router 自己的
            # DEFAULT_SETTINGS 合并（不在这里复制一份，避免上游加字段后漂移）。
            row = c.execute('SELECT data FROM settings WHERE id=1').fetchone()
            if not row:
                log("ℹ settings 表无行，写入 outboundProxy 初值")
                c.execute("INSERT INTO settings(id, data) VALUES(1, '{}')")
                row = ("{}",)
            s = json.loads(row[0]) if row and row[0] else {}
            pool_id = s.get("providerStrategies", {}).get(OPENCODE_FREE_PROVIDER, {}).get("proxyPoolId")
            if not pool_id:
                # 没有 per-provider pool 不代表全局出站代理没生效：outboundProxyUrl
                # 是设置级的，opencode 未单独绑定 pool 时走它。缺 pool 只提示，不早退。
                log(f"ℹ {OPENCODE_FREE_PROVIDER} 未绑定 proxyPool（走全局 outboundProxy）")

            # 1. 检查 proxyPool 当前代理地址（仅在 provider 真的绑定了 pool 时）
            if pool_id:
                pool_row = c.execute('SELECT data FROM proxyPools WHERE id=?', (pool_id,)).fetchone()
                if not pool_row:
                    log(f"⚠ proxyPool {pool_id} 不存在，跳过池地址校验")
                else:
                    pool = json.loads(pool_row[0])
                    url = pool.get("proxyUrl", "")
                    if url != expected_url:
                        log(f"⚠ opencode proxyPool 异常 (url={url})，修复为 {expected_url}...")
                        pool["proxyUrl"] = expected_url
                        c.execute('UPDATE proxyPools SET data = ? WHERE id = ?', (json.dumps(pool), pool_id))
                        changed = True

            # 2. 全局 outboundProxyUrl / outboundProxyEnabled
            # 默认值是 false + 空串（见 9router DEFAULT_SETTINGS），所以首次部署必然
            # 走进这里。这一条才是「把 9router 出口指到 7890」真正的落点。
            if s.get("outboundProxyUrl") != expected_url or not s.get("outboundProxyEnabled"):
                log(f"⚠ outboundProxy 异常 (url={s.get('outboundProxyUrl')!r}, "
                    f"enabled={s.get('outboundProxyEnabled')})，修复为 {expected_url}")
                s["outboundProxyUrl"] = expected_url
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


def _opencode_connection_started_at(conn: dict) -> float | None:
    """Clash API 的 start 是 ISO8601；解析失败返回 None（这条不动）。"""
    raw = (conn or {}).get("start")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _close_opencode_connections(chain_tag: str | None, min_age_sec: float,
                                reason: str = "") -> int:
    """关闭指定链上已僵死的 opencode 长连接（age >= min_age_sec）。

    WHY 只动 opencode、且要卡 age：换线后旧链上的在途请求本应跑完，立即
    全关会掐断它们；而被复用的僵尸连接（隧道静默死亡、本地 socket 仍
    ESTAB、数据黑洞）不关就会让后续每条请求白等满超时。正常请求 p50 6.5s，
    120s 以上的连接已不可能是在途请求，关掉是安全的。
    """
    status, data = singbox_api("/connections", silent=True)
    if status != 200 or not isinstance(data, dict):
        return 0
    now = time.time()
    closed = 0
    for conn in data.get("connections") or []:
        meta = conn.get("metadata") or {}
        host = str(meta.get("host") or meta.get("destinationIP") or "")
        # 必须是 opencode.ai 本身或其子域：`endswith("opencode.ai")` 会连
        # `evilopencode.ai` 一起关掉，误伤别的服务的长连接。
        if not (host == "opencode.ai" or host.endswith(".opencode.ai")):
            continue
        chains = conn.get("chains") or []
        if chain_tag and (not chains or chains[0] != chain_tag):
            continue
        started = _opencode_connection_started_at(conn)
        if started is None or now - started < min_age_sec:
            continue
        cid = conn.get("id")
        if not cid:
            continue
        st, _ = singbox_api(f"/connections/{cid}", method="DELETE", silent=True)
        if st in (200, 204):
            closed += 1
    if closed:
        log(f"✓ 清理 {closed} 条{reason}连接（{chain_tag or '当前链'}，"
            f"≥{min_age_sec:.0f}s）")
    return closed


_CLEANUP_LOCK = threading.Lock()
_CLEANUP_RUNNING = False
_LAST_CLEANUP_SKIP_LOG = 0.0


def _cleanup_connection_worker(chain_tag: str | None, min_age_sec: float,
                               reason: str) -> None:
    """线程体：清理异常必须落日志。线程里抛异常只会进 stderr，
    daemon.log 里看不到，等于清理静默失效。"""
    global _CLEANUP_RUNNING
    try:
        _close_opencode_connections(chain_tag, min_age_sec, reason)
    except Exception as e:
        log(f"✗ 连接清理异常（{chain_tag or '当前链'}）: {e}")
    finally:
        with _CLEANUP_LOCK:
            _CLEANUP_RUNNING = False


def _schedule_connection_cleanup(chain_tag: str | None, min_age_sec: float,
                                 reason: str = "") -> None:
    """清理走线程：一次 /connections + N 次 DELETE，不占主循环。

    单飞：429 风暴期换线最短 5s 一次，每次起线程会导致 /connections 扫描
    与 DELETE 叠加；同一时刻只保留一个清理任务，后来的直接跳过（下一轮
    换线/探针会再触发）。
    """
    global _CLEANUP_RUNNING, _LAST_CLEANUP_SKIP_LOG
    if not _warp().get("cleanup_enabled", False):
        # 关掉的 WHY：DELETE 不区分「僵死的池化 socket」和「正在跑的在途请求」，
        # 实测会掐断正常流式响应（客户端报 upstream connection lost），而 sidecar
        # 收到 socket hang up 又产生新 503，反过来再触发清理。
        now = time.time()
        if now - _LAST_CLEANUP_SKIP_LOG >= 3600:
            _LAST_CLEANUP_SKIP_LOG = now
            log(f"· 连接清理已禁用（cleanup_enabled=false），跳过：{reason or '未注明'}"
                f" tag={chain_tag}")
        return
    with _CLEANUP_LOCK:
        if _CLEANUP_RUNNING:
            return
        _CLEANUP_RUNNING = True
    try:
        threading.Thread(target=_cleanup_connection_worker,
                         args=(chain_tag, min_age_sec, reason),
                         daemon=True, name="conn-cleanup").start()
    except Exception as e:
        with _CLEANUP_LOCK:
            _CLEANUP_RUNNING = False
        log(f"✗ 连接清理线程启动失败: {e}")


# ---------------------------------------------------------------------------
# 换线后修复被切下来的线（2026-11-09 主人定）。
#
# 错误切换保留，但被切下来的线不能带着坏节点躺回候选池 —— 否则下一轮
# 选线又选中它、再死一次（实测 08:22–08:45 WG-1↔WG-9↔ICN-13 每 15~30s
# 来回弹跳，正是「换线不修节点」的后果）。502/断流这类「节点延迟大、
# 超时不可用」既不是 429 配额也不是 colo 变化（同 colo）—— 正确动作就是
# 在**同一个渠道块内**换一个延迟好的可用节点。
# ---------------------------------------------------------------------------

# 同一条线两次修复的冷却表（tag → 冷却到期时刻）。
_DEMOTED_REPAIR_COOLDOWN: dict[str, float] = {}
# 单飞：同一条线的修复线程不重入（触发点在换线关键路径上，可能密集到来）。
_DEMOTED_REPAIR_RUNNING: set[str] = set()
_DEMOTED_REPAIR_LOCK = threading.Lock()


def _demoted_repair_applies(reason: str) -> bool:
    """换线原因是否属于「节点/链路级」故障 —— 只有这类修节点才有意义。

    rate429 / 惩罚期 / 低质启动校验 / 主动轮换都是 IP 或配额级：节点本身
    是好的，换掉等于白丢一条能用的链路。transport ×N（出口探测失败）与
    （opencode 链路连续悬挂）才是「这条线的落地节点延迟大/超时不可用」
    的直接证据 —— 主人定性：这类该换的是渠道块内的 IP，不是跨块换出口。
    """
    if not reason:
        return False
    if any(k in reason for k in ("rate429", "主动轮换", "惩罚期", "低质")):
        return False
    return "transport" in reason


def _schedule_demoted_node_repair(old_tag: str, reason: str | None) -> None:
    """异步触发：本函数在换线关键路径（_WARP_LOCK / refresh slot 持有期间）
    被调用，判定与 I/O 都不能挡换线 —— 全部丢给 worker 线程。"""
    if not _warp().get("demoted_node_repair_enabled", True):
        return
    with _DEMOTED_REPAIR_LOCK:
        if old_tag in _DEMOTED_REPAIR_RUNNING:
            return
        _DEMOTED_REPAIR_RUNNING.add(old_tag)
    try:
        threading.Thread(target=_demoted_node_repair_worker,
                         args=(old_tag, reason), daemon=True,
                         name="demoted-repair").start()
    except Exception as e:
        with _DEMOTED_REPAIR_LOCK:
            _DEMOTED_REPAIR_RUNNING.discard(old_tag)
        log(f"✗ 修复被切线线程启动失败（{old_tag}）: {e}")


def _demoted_node_repair_worker(old_tag: str, reason: str | None) -> None:
    """线程体：异常必须落日志 —— 线程里抛异常只进 stderr，daemon.log
    里看不到，等于修复静默失效（同 `_cleanup_connection_worker` 的教训）。"""
    try:
        _demoted_node_repair(old_tag, reason)
    except Exception as e:
        log(f"✗ 修复被切线异常（{old_tag}）: {e}")
    finally:
        with _DEMOTED_REPAIR_LOCK:
            _DEMOTED_REPAIR_RUNNING.discard(old_tag)


def _demoted_node_repair(old_tag: str, reason: str | None) -> None:
    """把刚被切下来的线修好：同渠道块（colo）内换到延迟好的可用节点。

    H2：`_h2_repair` 换 selector，零成本、不重启、不断流（自带 60s 节流
    与整池重试窗口）。WG：同块选点 → `_rehome_one` 写配置 → `rebound=True`
    重启一次 —— 它刚被 demote，不在承载流量，重启无害；rebound 让夜间锁/
    渠道锁/换地址熔断让路：熔断拦的是「同一节点换地址」，这里换的就是节点。
    CF/usque 没有可换的落地节点，跳过。
    """
    member = _warp_member(old_tag)
    if not member or old_tag == active_warp_tag():
        return          # 已不是成员（重渲染）/ 又切回来了：都不动
    now = time.time()
    with _DEMOTED_REPAIR_LOCK:
        if now < _DEMOTED_REPAIR_COOLDOWN.get(old_tag, 0.0):
            return
        # 进入即占冷却：失败也别在下次换线时立刻重试，风暴期会复制换线弹跳。
        _DEMOTED_REPAIR_COOLDOWN[old_tag] = now + float(
            _warp().get("demoted_node_repair_cooldown_sec", 600))
    with _WARP_REFRESH_LOCK:
        busy = old_tag in _WARP_REFRESHING or old_tag in _WARP_REFRESH_PENDING
    if busy:
        return          # 正被后台刷新/预热：那条路径自己会处理，别抢
    if _warp_line_state(old_tag).get("stop_probe"):
        return          # 渠道锁判定「换地址也买不到配额」：不是节点问题
    kind = member.get("kind")
    if kind == "h2":
        if _h2_repair(member):
            log(f"  ⇄ {old_tag} 换线后修复：已换同块节点（{reason}）")
        return
    if kind != "wg":
        return
    # ---- WG：同块选点（先承载能力，再延迟缓存升序，未测过的垫后）----
    cur = (warp_wg._read_state().get("lines") or {}).get(old_tag) or {}
    cur_node = cur.get("node")
    if not cur_node:
        return
    try:
        pool = {n["tag"] for n in warp_wg.udp_capable(warp_wg.load_nodes())}
        colo_map = warp_wg.load_colo_map()
        blocks_map = warp_blocks.load_map()
        exclude = {n for n in warp_wg.line_nodes().values() if n}
    except Exception as e:
        log(f"  · 修复 {old_tag} 读节点表失败: {e}")
        return
    cur_colo = (colo_map.get(cur_node) or {}).get("colo")
    if not cur_colo:
        return          # 当前节点没有实测 colo：同块无从谈起，交给轮换/重定位
    exclude |= {str(s.get("ban_backfill_node"))
                for s in list(_WARP_LINE_STATE.values())
                if isinstance(s, dict) and s.get("ban_backfill_node")}
    # 当前节点不进排除集（别的线仍可能绑它），但绝不作为候选 ——
    # 修复的目的就是离开这个节点。
    cands = [t for t, v in colo_map.items()
             if t != cur_node
             and isinstance(v, dict) and v.get("colo") == cur_colo
             and v.get("ok") is not False and t in pool and t not in exclude]
    if not cands:
        log(f"  · {old_tag} 同块（{cur_colo}）无其他可用节点，修复跳过")
        return
    # alive rank（0 能承载/1 没测过/2 实测不能）为主判据，延迟为次 ——
    # 一台测过不能承载 WARP 的低延迟节点比没测过的高延迟节点更没用。
    cands.sort(key=lambda t: (_warp_alive_rank(t, blocks_map),
                              _NODE_DELAYS.get(t) or 10 ** 9, t))
    # 只探前 3 个：一次 TCP 探测最多 6s，同块通常 2~4 个候选
    for node in cands[:3]:
        if _node_tcp_ok(node) is False:
            continue
        r = _rehome_one(old_tag, node)
        if r == "err":
            continue
        if r == "ok":
            _warp_line_restart(member, "换线后同块修节点", rebound=True)
            log(f"  ⇄ {old_tag} 换线后修复：{cur_node} -> {node}"
                f"（同块 {cur_colo}，延迟 {_NODE_DELAYS.get(node, '?')}ms，{reason}）")
        return          # gone / skip：候选不在池里，没有更多可做的
    log(f"  · {old_tag} 同块（{cur_colo}）候选前 3 个均不可达，修复跳过")


def _after_warp_switch(old_tag: str | None, interrupt: bool = False,
                       reason: str | None = None) -> None:
    """换线后清掉旧链上的 opencode 长连接。interrupt=False 时不清理。

    interrupt 是清理开关，不是 sing-box 的参数（那个字段不存在，见
    `_switch_warp` 的 docstring）。语义：「这次换线要不要把在途请求打断」。

    - 错误触发（403/429/探测判死/transport 503）→ True。旧出口已被判死，
      留在它上面的每条请求都在白等超时，断掉让 9router 重连到新出口才是止损。
    - 主动轮换（配额用满 150 格 / fixed 周期）→ False。旧出口是健康的，
      在途请求应当跑完；实测主动轮换从不报 429，没有需要打断的东西。

    WHY 不能一律清理：age 筛不出僵尸。实测 age>=120s 的连接 **85% 仍在传
    字节**（age>=180s 时 100%，最长 242s 仍在传），清它们等于掐断在途请求。
    所以只在旧出口确定不可用时才做。

    old_tag 为 None 表示换线前停在轮换池 proxy：此时没有「旧 WARP 链」可清，
    而 `_close_opencode_connections` 把 None 当通配符（清全部链），会连刚切
    过去的新链一起清 —— 直接跳过。

    reason 透传给「换线后修复被切线」（见 `_demoted_repair_applies`）：节点级
    故障（transport）顺手修节点；IP/配额级与主动轮换不修。
    """
    if old_tag is not None and _demoted_repair_applies(reason or ""):
        _schedule_demoted_node_repair(old_tag, reason)
    if not interrupt or old_tag is None:
        return
    _schedule_connection_cleanup(
        old_tag,
        float(_warp().get("cleanup_old_chain_age_sec", 120)),
        reason="旧链",
    )


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
# 在途请求（只改 selector，旧连接仍走旧出口），延迟 p50 8.8s / p90 27.9s 与换线
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
    - transport：9router Bun 传输悬挂产生的 503（无上游字节）。不是出口级
      证据（sidecar 全局故障也会产生），单独计数供「先探出口再换线」使用，
      不进 consec_err/full_err。
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
                "consec_err": 0, "full_err": False, "transport": 0}
    counts = {"abuse403": 0, "rate429": 0, "ok": 0, "other_err": 0,
              "consec_err": 0, "full_err": False, "transport": 0}
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
            # 仍算 other_err（full_err 语义要求「全窗都是出口级错误」），
            # 但额外记一份 transport 计数，供探出口后再决定是否换线。
            if _classify_transport_error(r["data"]):
                counts["transport"] += 1
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


def _switch_warp(tag: str, *, ip: str | None = None) -> bool:
    """翻转 opencode-out selector。在途请求不会因换线而断。

    只改 selector 不会迁移已建立的连接：9router / opencode 用 HTTP/2 长连接，
    旧连接继续走烧掉的出口 —— 实测换线后 429 还要再报 2 分钟才恢复，期间
    探针（新连接）却全是 OK。真正的断连由 `_after_warp_switch` 调
    `DELETE /connections/{id}` 完成，见 `_close_opencode_connections`。

    不在此处发 `interrupt_exist_connections`：sing-box v1.13 的 Clash API
    `UpdateProxyRequest` 只有 `name` 一个字段（experimental/clashapi/
    proxies.go:158），`SelectOutbound(tag)` 也不收这个开关，它读的是构造时的
    配置值；且 inbound 连接经 `NewConnectionEx` 派发时根本不登记 interrupt 组。
    实测传 true 与不传，换线前 12 条连接均 12/12 存活。

    成功后立即把快照的 now 改写为 tag：判定（尤其是补刷前的「这条线是不是
    已经成为出口」）读的就是快照，晚 0.5s 看到就可能重启一条正在服务的隧道。

    模式切换（warp <-> proxy）在这里打日志：所有出口变更都经过这个函数，
    在调用方各打一条会漏掉 sing-box 重启后的线路恢复那条路径。

    `ip` 是**切过去那条线的新出口地址**，由调用方在选线时就已知道（`probes`
    里带着）。历史记录必须用调用方传入的这个值：本函数内部读线状态表只能读到
    这条线切换**之前**的地址 —— 两个调用方都是「先切 selector、后写线地址表」，
    在这里读表必然读到陈旧值，而迟到的 429 回溯时正好拿它去定罪。缺省 None
    表示调用方也不知道，此时不记录地址，回溯会退化成「不定罪」。

    before 由 `_current_mode()` 直查 sing-box（只在查不到时才退回快照）：快照固定
    滞后 0.5s，连切两次时会漏打一次模式日志。
    """
    before = _current_mode()
    r, _ = singbox_api(f'/proxies/{_warp()["selector"]}', method="PUT", body={"name": tag})
    if r not in (200, 204):
        log(f"✗ 切换 WARP 线路失败 ({tag}): status={r}")
        return False
    _publish_clash_snapshot(_warp()["selector"], {"now": tag})
    after = ("warp", tag) if _warp_member(tag) else ("proxy", None)
    # 只无条件写历史，不取 `_WARP_LOCK`：这条路径常在 `_reconnect_warp_now`
    # 已经持有该锁的换线线程里执行（锁跨线程持有到 worker 的 finally），
    # 再取一次会让换线线程永久阻塞、锁永不释放。
    # 不加锁的代价只是「一次换线的记录时刻晚于另一次」时二分可能取错一格，
    # 而记录本身不可丢 —— 丢了会把一条线整个时期的请求判成无主。
    #
    # ip 传 None 是刻意的：本函数只翻 selector，**取不到新出口地址**。线状态表
    # 里此刻仍是这条线切换前的旧地址 —— 两个调用方都是「先 _switch_warp，后
    # `_set_warp_line_ip` / `_warp_line_selected`」，在这里读表必然读到陈旧值，
    # 而迟到的 429 回溯时正好用这个地址去定罪。记 None 会让回溯退化成
    # 「知道那条线、但不知道地址」→ 不标脏任何 IP，比记错地址安全。
    _record_warp_switch_history(tag if after[0] == "warp" else None,
                                time.time(), ip)
    # 换线表同时记一笔「旧地址 → 新地址」。旧地址取进程级的那个（selector 刚翻
    # 完，线表里各线的 ip 还是切换前的值，不能当作「旧出口」—— 旧出口是**此刻
    # 之前承载流量的那一个**，只有 `_ACTIVE_WARP_IP` 知道）。
    _exit_ledger_note(_ACTIVE_WARP_IP, ip, time.time(), tag)
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


# 本 daemon 跑在系统级单元里，但以 User=administrator 运行：config.py 用
# pwd.getpwuid(os.getuid()).pw_dir 取家目录，以 root 跑会指向 /root，
# 9router 的库、.env、.warp 全部找不到。代价是非 root 进程重启不了系统单元，
# 于是走 /usr/local/bin/asip-unit（sudoers 里 NOPASSWD 放行的白名单包装，
# 只认 usque-warp*.service 与 nine-router-src.service）。sudoers 不允许命令
# 参数带通配符，所以白名单只能收在那个脚本里，不能写在这。
_UNIT_CTL = "/usr/local/bin/asip-unit"


def _load_tier_disabled() -> set[str]:
    """本档位下**停用**的 WG 线 tag（实现在 `warp_blocks.tier_disabled`）。

    档位只表达「哪些 WG 单元该跑」，不删 members、不动设备、不改 enable：
    停掉的线随时 `start` 就能回来，切换成本只有几秒。缺文件/坏文件一律返回
    空集合 —— 宁可全开，也不要因为台账读不出来把整批线冻死。

    只统计 **members 里声明的** WG 线（`declared`），不含 23+ 的闲置补位设备：
    档位管的是「哪些声明线在跑」，补位池是另一回事。
    """
    declared = {m["tag"] for m in (CFG.get("warp") or {}).get("members") or []
                if m.get("kind") == "wg"}
    if not declared:
        # 声明为空多半个 `.env` 没读进来（`WARP_WG_FAMILIES` 缺失时
        # `_append_wg_members` 直接 return）。运行期遇到这种瞬态必须**保持
        # 现有停用集合**：返回空集 = 把 30 条停用的 WG 线全部解禁。
        # 只有 import 期（_TIER_DISABLED 尚未绑定）才退回「没有档位」。
        kept = globals().get("_TIER_DISABLED")
        if isinstance(kept, set):
            log("  ⚠ 档位失效：members 里没有 WG 线声明，保持现有停用集合"
                "（检查 .env 的 WARP_WG_FAMILIES / WARP_WG_NODES）")
            return set(kept)
        log("  ⚠ 档位失效：members 里没有 WG 线声明"
            "（检查 .env 的 WARP_WG_FAMILIES / WARP_WG_NODES），WG 全部按启用处理")
        return set()
    return warp_blocks.tier_disabled(declared)


_TIER_DISABLED: set[str] = _load_tier_disabled()


def _tier_mtime() -> float:
    try:
        return warp_blocks.map_path().with_name("tiers.json").stat().st_mtime
    except OSError:
        return 0.0


_TIER_MTIME: float = _tier_mtime()


def _refresh_tier_disabled() -> bool:
    """tiers.json 变了就重算停用集合，返回「是否变了」。

    此前 _TIER_DISABLED 是 import 期常量：改档位不重启 daemon 就永不生效，
    而 _tier_reconcile 又只在启动时跑一次 —— 于是「tiers.json 写着 h2-only、
    WG 单元还在跑还在探」可以一直持续到下次重启。档位是运维动作，必须热生效。
    """
    global _TIER_DISABLED, _TIER_MTIME
    mt = _tier_mtime()
    if mt == _TIER_MTIME:
        return False
    # 台账读不到 / 形状不对时既不推进 mtime 也不重算集合。旧版先写 mtime
    # 再读，一次瞬态失败就把这次变更永久吞掉；而 _load_tier_disabled 的
    # 降级语义是「空集合 = 全部启用」，会把 30 条停用 WG 单元批量拉起来。
    try:
        _, spec = warp_blocks.tier_spec()
    except Exception as e:
        log(f"  ⚠ 档位台账读取异常，保持现有停用集合：{e}")
        return False
    if mt == 0.0 or spec is None:
        log("  ⚠ 档位台账读不到/形状不对，保持现有停用集合（不批量解禁）")
        return False
    fresh = _load_tier_disabled()
    _TIER_MTIME = mt                 # 读成功之后再推进
    _TIER_DISABLED = fresh
    log(f"  · 档位变更：停用 {len(_TIER_DISABLED)} 条 WG 线")
    return True

def _line_enabled(member: dict) -> bool:
    """该成员在当前档位下是否应当运行。非 WG 成员永远为真。"""
    return member.get("tag") not in _TIER_DISABLED


def _h2_channels_mtime() -> float:
    try:
        return os.stat(os.path.join(_PROJECT, ".warp", "h2", "channels.json")).st_mtime
    except OSError:
        return 0.0

_H2_CHANNELS_MTIME: float = _h2_channels_mtime()

def _h2_members_from_ledger() -> list[dict]:
    """从渲染台账重建 H2 成员列表。读不到/形状不对返回空列表（调用方保持现状）。"""
    try:
        with open(os.path.join(_PROJECT, ".warp", "h2", "channels.json"),
                  encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for r in rows:
        try:
            out.append({
                "tag": r["member_tag"],
                "port": int(r["socks_port"]),
                "service": r["unit"],
                "kind": "h2",
                "index": int(r["index"]),
                "colo": r.get("colo"),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out

def _refresh_h2_members() -> bool:
    """channels.json 变了就重建 CFG["warp"]["members"] 的 H2 段，返回「是否变了」。

    WHY 必须热生效（2026-11-11 实测事故）：
      `config._append_h2_members` 只在 import 期读一次台账，而
      `_run_h2_sync` 会在运行中重渲染台账（deploy/render_h2_lines.py）。
      重渲染会重排编号：实测 19:13 之后台账 112 条里有 56 条换成 112~167
      的新编号，daemon 内存里却还是旧的 0~111 那批 tag。后果是双份的：
        · `_warp_member(tag)` 查不到新 tag → 新线进不了换线候选、不会被
          `_warp_line_restart` 按名重启，日志里永远不出现它们的名字；
        · `_scan_running_tunnels` 的 declared 表也查不到 → 这些单元退回
          `WARP-H2-SCAN-<idx>` 假身份被探测，结论写在假名下
          （实测台账 103 条 SCAN 记录，其中 56 条正好对应新编号的真成员）。
      「在跑、有流量、结论却记在一个换线永远查不到的名字上」—— 这就是
      「blocked 之后不再被探测/重启」与「一直无样本」的共同成因。

    WHY 读失败时保持现状：空列表会把整批 H2 线从名单抹掉，比不刷新更糟
    （换线候选、探测目标、members[0] 兜底全都会跟着塌）。
    """
    global _H2_CHANNELS_MTIME, _TUNNEL_SCAN_TS
    mt = _h2_channels_mtime()
    if mt == _H2_CHANNELS_MTIME:
        return False
    fresh = _h2_members_from_ledger()
    if not fresh:
        # **不推进 mtime**：台账正在被重渲染时读到半截内容是瞬态，下个 tick
        # 还得再试。旧版先写 mtime 再读，一次失败就把这次重排永久吞掉。
        log("  ⚠ H2 台账已变更但读不出成员，名单保持不变（下轮重试）")
        return False
    _H2_CHANNELS_MTIME = mt
    members = _warp()["members"]
    before = sum(1 for m in members if m.get("kind") == "h2")
    # 原地切片赋值：CFG["warp"]["members"] 是共享对象，各处持的都是这个引用，
    # 换成 `members = fresh` 会让老引用全部指向旧列表。
    members[:] = [m for m in members if m.get("kind") != "h2"] + fresh
    dropped = _drop_orphan_scan_state()
    # 扫描缓存按「端口→tag」缓存 60s，不清就会继续用旧 tag 探一轮。
    _TUNNEL_SCAN_TS = 0.0
    log(f"  · H2 台账变更：名单 {before} → {len(fresh)} 条"
        + (f"，清理 {dropped} 条假身份结论" if dropped else ""))
    return True

def _drop_orphan_scan_state() -> int:
    """单元已归到声明 tag 名下 → 丢掉同名 WARP-H2-SCAN-<idx> 的结论。

    状态属于「这个端口上的隧道」；名字换了，旧名字下的结论不再指向任何东西，
    留着只会让报表显示 OK 而候选池里没有它。`_load_warp_line_state` 里的同名
    逻辑是启动时的一次性版本，这里是运行中的持续版本。
    """
    svc = {m.get("service") for m in (_warp().get("members") or [])
           if m.get("service")}
    with _WARP_LINE_CV:
        dead = [t for t in list(_WARP_LINE_STATE)
                if isinstance(t, str) and t.startswith("WARP-H2-SCAN-")
                and f"usque-h2-{t.rsplit('-', 1)[-1]}.service" in svc]
        for t in dead:
            del _WARP_LINE_STATE[t]
    return len(dead)

def _unit_ctl(action: str, service: str) -> subprocess.CompletedProcess:
    """对系统级单元执行 action。

    优先用 asip-unit（sudo 免密白名单）；它不存在时退回直接 systemctl ——
    没装 sudoers 规则的机器上至少给出一条清楚的报错，而不是 FileNotFoundError。
    """
    if os.path.exists(_UNIT_CTL):
        return subprocess.run(
            ["sudo", "-n", _UNIT_CTL, action, service],
            capture_output=True, text=True, timeout=20,
        )
    return subprocess.run(
        ["systemctl", action, service],
        capture_output=True, text=True, timeout=20,
    )


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
    # 外部翻转不经过 `_switch_warp`，所以历史里没有「这一刻在池上」的记录。
    # 不补的话，落池期间发出的 429 回溯会停在池之前最后一条隧道上，把一条
    # 早已不承载流量的线判成发出者。落池已禁用时 `_fallback_to_proxy` 走
    # `_restart_warp_inplace`，同样不落记录 —— 这里是唯一能补的地方。
    _record_warp_switch_history(None, now)
    log(f"⚠ 检测到外部落池（opencode-out=proxy），回收至 {target}")
    # 不传 ip：`_ACTIVE_WARP_IP` 是**被换下**那条 WARP 线的地址，而 target 是
    # 另一条线（`_LAST_WARP_TAG` 或 members[0]），两者通常不是同一条。传它等于
    # 在 target 名下记一个不属于它的地址，迟到 429 回溯时会拿这个地址定罪。
    # 不知道地址时记 None，回溯退化成「知道这条线、不知道地址」→ 不标脏。
    return _switch_warp(target)


def _systemctl_scope(service: str | None = None) -> list[str]:
    """隧道单元装在哪个总线：系统级优先，没有才用用户级。

    daemon 自身可能跑在系统总线（无 user bus，`--user` 直接报
    "Failed to connect to bus: No medium found"），也可能跑在用户级。
    按单元是否存在分别判断，不靠 daemon 自己的 cgroup —— 隧道和 daemon
    不必同总线，迁移期间两者会短暂并存。
    """
    # 历史坑：WG 线一度装成用户级单元（~/.config/systemd/user），而 daemon 在
    # 系统总线 —— 每次重启都撞 "Interactive authentication required"（实测
    # 04:27~04:29 每条 WG 线都「重启后未就绪」）。2026-10-28 起 WG 线与
    # usque-warp 一样装成系统级单元（User=$(id -un)），两者共用同一条
    # polkit 放行规则，这里不再需要按单元名分流。
    # 纯 WG 部署没有 usque-warp.service；只探 usque 会把系统级单元误判成
    # 用户级，之后每次 restart 都打空。
    for unit in ("usque-warp.service", "warp-wg-1.service"):
        try:
            r = subprocess.run(["systemctl", "cat", unit],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return ["systemctl"]
        except Exception:
            pass
    return ["systemctl", "--user"]


def _restart_warp_line(member: dict, rebind: bool = False) -> bool:
    """重启一条 WARP 线；WG 的 v4 渠道在重试时先换节点。

    v6 渠道每次重启都换地址（实测 4/4，连 /64 都变），不需要换节点。
    v4 渠道的地址由 WARP 落地 colo 决定，同 colo 重启只会拿回同一个地址，
    只能换节点 —— 这是唯一需要 rebind 的场景，别把它加到 v6 的热路径上。
    """
    # 全局限速装在真正动手的地方：除了 _warp_line_restart，还有
    # _refresh_idle_warp / _inplace_reconnect / 脏线守护三条旁路，闸只开在
    # 上层会被绕过（2026-10-28 三方审查 P1-3）。
    if _night_block_restart(member["tag"], "刷新", time.time()):
        return False
    if not _line_enabled(member):
        return False
    if not _restart_rate_allow(time.time()):
        return False
    if rebind and member.get("kind") == "wg" and member.get("family") == "v4":
        try:
            # 本地导入：测试用 sys.modules 换掉整个 warp_wg 来隔离真服务操作。
            import warp_wg
            if warp_wg.rebind(int(member["index"])):
                log(f"  ⇄ {member['tag']} 换节点（v4 出口随 colo 走，重启不会换址）")
            else:
                log(f"  ✗ {member['tag']} 无可用后备节点，按原节点重启")
        except Exception as e:
            log(f"  ✗ {member['tag']} 换节点失败: {e}")
    return _restart_service(member["service"], member["tag"])


def _restart_service(service: str, tag: str | None = None) -> bool:
    # 与 `_restart_warp_line` 同一道闸，但**这里才是最后一道**：订阅刷新换绑定
    # （`daemon.py` 的 `_apply_moved_bindings`）直接调本函数，绕过上面所有判据。
    # 档位停用的线若在这里被 restart，等于档位形同虚设。
    if tag and tag in _TIER_DISABLED:
        return False
    try:
        # 系统级单元 + daemon 以 User=administrator 跑：没有 root，systemctl
        # restart 会被拒。asip-unit 是 sudoers NOPASSWD 放行的白名单包装。
        # 先走它；机器上没装（开发机跑用户级单元）才退回普通 systemctl。
        if os.path.exists(_UNIT_CTL):
            r = subprocess.run(
                ["sudo", "-n", _UNIT_CTL, "restart", service],
                capture_output=True, text=True, timeout=20,
            )
        else:
            cmd = [*_systemctl_scope(service), "restart", service]
            env = None
            if "--user" in cmd:
                env = dict(os.environ)
                runtime = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
                env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=env)
        if r.returncode != 0:
            log(f"重启 {service} 失败: {r.stderr.strip()[:200]}")
        elif tag:
            # 用户口径：刷了几次从离开主渠道起算，每次真正重启算一次。
            _WARP_REFRESH_COUNT[tag] = _WARP_REFRESH_COUNT.get(tag, 0) + 1
        return r.returncode == 0
    except Exception as e:
        log(f"重启 {service} 异常: {e}")
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

# 连续多少次拿不到出口地址才判「隧道半死」。单次 unknown 是探测侧瞬态
# （日志实测同一地址 err 后 66s 即 OK），重启它等于白掐一次在途连接。
_UNKNOWN_RESTART_STREAK = 10

# 被切下的主渠道排空时的判闲观察窗。必须比 12s 长得多：流式响应在 token
# 之间会静默，实测 p99=8.1s / max=16.1s（N=1460），12s 窗口有 0.3% 落在
# 静默里；而 12:25 实测 4 条各收 ~1MB、年龄 94~116s 的连接在 12s 零增长后
# 被重启掐断。取 30s：覆盖静默分布的 max 并留近一倍余量。
#
# WHY 只有这条路径付长窗：候选线的重启不需要窗口（它不在服务流量，等端口
# 就绪即可），错误换线路径每多等一秒坏出口就多服务一秒失败请求。只有
# 「刚被切下、仍在排空在途请求」的线才既需要等、又等得起。
_DRAIN_BUSY_WINDOW_SEC = 30.0


# ---------------------------------------------------------------------------
# 排空后再回收（2026-11-11，主人裁定）。换线只翻 selector（既有），出口连接池
# 由 9router 按端口精确失效（9router 6c4b2aba）；本段补齐第三环——刚被换下的
# 线必须等它上面的存量请求走完才允许重启回收。三段缺一不可：缺 ② 排空等不到
# 收敛，缺 ③ selector 留下的存量请求会被重启掐断（实测在用线重启后 30s 内
# p50 9 个在途请求、报错率 4 倍于基线）。
#
# 判闲不按时间，按**字节流事实**（_warp_line_stats 的 conns / young / 字节
# 增量，主人 2026-11-11 裁定：字节流是可探测的）：零连接立即回收；有年轻连接
# （< _FIRST_BYTE_WAIT_MAX_SEC）立即判忙；只剩老连接则连续 drain_quiet_rounds
# 轮字节零增长才回收。但探测有盲区（TTFT 长尾实测 max 532.7s），必须设上限：
# 超过 drain_max_wait_sec 强制回收，H2 线延迟高一档、上限翻倍（主人裁定
# 「预估翻个倍」）。所有判定**非阻塞**：不达标就拒绝，探针按 drain_poll_sec
# 回来复查——探针并发 32，睡窗口会占死线程池。
# ---------------------------------------------------------------------------

_DRAIN_LOCK = threading.Lock()
# tag -> 被翻下 selector 的时刻（_slot_note_switch 写入；回收后清除）
_RETIRED_AT: dict[str, float] = {}
# tag -> {rounds, last_bytes}：老连接字节静止轮数（_drain_gate 维护）
_DRAIN_PENDING: dict[str, dict] = {}
# tag -> 首次「判坏但仍在槽」的时刻：先等真实报障换线，超过
# drain_force_retire_sec 仍没被换走才主动认领切槽（只翻 selector）
_BAD_ON_SLOT: dict[str, float] = {}


def _drain_max_wait_sec(member: dict) -> float:
    """回收上限（秒）。H2 线延迟高一档（TTFT p90 52.6s / max 532.7s），翻倍。

    基值对齐 _FIRST_BYTE_WAIT_MAX_SEC(120s)：上限绝不能在年轻连接窗口内
    触发，否则等于绕开年轻连接保护。所以上限只逮「连接已老、字节增量连续
    为零仍不静止」的病态长尾——改动前这类请求是跟着 2s 短窗线重启每
    5~7s 被掐一次的。
    """
    base = float(_warp().get("drain_max_wait_sec", 120))
    return base * 2.0 if member.get("kind") == "h2" else base


def _lines_in_use() -> set[str]:
    """此刻正挂在 selector 上服务流量的线：主出口 + 全部槽位当前指向的线。

    优先读 _SLOT_HEALTH 的 tag 台账（纯内存；_slot_put → _slot_note_switch
    是槽位 selector 唯一的变更入口，台账与真实 selector 同步）。daemon 重启
    后的启动窗口里台账尚未建全（tag 为 None）而 sing-box 里的 selector 仍指
    着真实在用的线——这正是最需要真实集合的时刻，回退 _slot_peers()（≤6 发
    HTTP；_warp_line_restart 既有路径本就有 warp_line_busy 的 HTTP 查询，
    不引入新的持锁 HTTP 类别）。
    """
    used: set[str] = set()
    a = active_warp_tag()
    if a:
        used.add(a)
    # 第七闸口没有线台账（tag 恒 None），不把它摘出去的话 missing 恒真，
    # 每次都退化成全槽 HTTP 兜底。
    gate_name = (_proxy_gate_slot() or {}).get("name")
    with _SLOT_STATE_LOCK:
        hs = [h for n, h in _SLOT_HEALTH.items() if n != gate_name]
        tags = {h["tag"] for h in hs if h.get("tag")}
        missing = any(not h.get("tag") for h in hs)
    if missing:
        for s in consumer_slots():
            if s["name"] == gate_name:
                continue
            t, _ip = _slot_current(s)
            if t:
                used.add(t)
    else:
        used |= tags
    return used


def _note_bad_maybe_retire(tag: str, now: float, reason: str = "") -> None:
    """判坏但仍在槽：先记账等真实报障换线（只翻 selector），超时才主动认领。

    探针结论只做线索，生产槽位以真实报障为准（文档第七/八节教训：不用 3KB
    探针结论去切生产槽）。超过 drain_force_retire_sec 仍未被真实报障换走，
    用 _slot_begin_repair 原子认领防并发双切；reason 用 proactive，不给
    地址记脏（判坏结论来自探针，不是生产事实）。
    """
    with _DRAIN_LOCK:
        first = _BAD_ON_SLOT.setdefault(tag, now)
    if now - first < float(_warp().get("drain_force_retire_sec", 120)):
        return
    kicked = False
    try:
        owners = _slot_line_owners().get(tag, [])
    except Exception:
        owners = []
    for name in owners:
        slot = _slot_by_name(name)
        if slot is None:
            continue
        try:
            cur, _ip = _slot_current(slot)
        except Exception:
            continue
        if cur != tag:
            continue
        if not _slot_begin_repair(name, "drain_force_retire"):
            continue
        threading.Thread(target=_repair_slot, args=(slot, "proactive"),
                         daemon=True, name="retire-" + name).start()
        kicked = True
    if kicked:
        with _DRAIN_LOCK:
            _BAD_ON_SLOT.pop(tag, None)
        if now - _NIGHT_LOG_TS.get("retire:" + tag, 0.0) > 300.0:
            _NIGHT_LOG_TS["retire:" + tag] = now
            log("  ⏏ %s 判坏但在槽超 %.0fs 未被真实报障换走 → "
                "主动认领切槽（只翻 selector，%s）"
                % (tag, now - first, reason))


def _drain_gate(member: dict, now: float,
                reason: str = "") -> tuple[bool, str]:
    """回收闸：刚退役的线必须按字节流事实判空才允许重启。

    返回 (allow, note)；allow=True 时本线排空状态已清理。本函数不做任何
    阻塞等待、不写线状态——拒绝只由调用方推 next_probe_ts。

    顺序：在槽拒绝（先摘流量）→ 上限（字节流探测有盲区，必须有界）→
    零连接立即回收 → 年轻连接立即判忙 → 老连接数连续字节零增长轮数。
    """
    tag = member["tag"]
    if not _warp().get("drain_before_recycle_enabled", True):
        return True, "开关关闭"
    if tag in _lines_in_use():
        _note_bad_maybe_retire(tag, now, reason)
        return False, "仍在槽上服务流量，先摘流量再回收"
    with _DRAIN_LOCK:
        retired = _RETIRED_AT.get(tag)
        if retired is None:
            # 台账没有退役记录 ≠ 它没服务过流量。账本由 _slot_note_switch
            # 维护，主出口换线（_switch_warp/_fast_switch）、人工动
            # sing-box、daemon 重启丢内存都会绕过它——静默放行等于重演
            # 「在途被掐断」（fn_xxx 故障同根：账本说没有就放行）。字节流
            # 事实优先：名下有连接 = 在或刚在服务流量 → 按「刚退役」起表
            # 排空，同样受上限约束；零连接才真正放行。
            stats0 = _warp_line_stats(tag)
            if stats0 is None or stats0[2] == 0:
                return True, "未经槽位服役且零连接（游离线）"
            _RETIRED_AT[tag] = now
            retired = now
            if now - _NIGHT_LOG_TS.get("drainhole:" + tag, 0.0) > 300.0:
                _NIGHT_LOG_TS["drainhole:" + tag] = now
                log("  ⚠ %s 无退役记录却有 %d 条连接（台账洞：selector 变更"
                    "未走 _slot_note_switch）→ 按刚退役起表排空"
                    % (tag, stats0[2]))
        elapsed = now - retired
        cap = _drain_max_wait_sec(member)
        if elapsed > cap:
            _RETIRED_AT.pop(tag, None)
            _DRAIN_PENDING.pop(tag, None)
            note = "排空超上限 %.0fs → 强制回收" % cap
            if member.get("kind") == "h2":
                note = "排空超上限 %.0fs（H2x2）→ 强制回收" % cap
            return True, note
        stats = _warp_line_stats(tag)
        if stats is None:
            return False, "连接快照不可用，下轮复查"
        bytes_, young, conns = stats
        if conns == 0:
            _RETIRED_AT.pop(tag, None)
            _DRAIN_PENDING.pop(tag, None)
            return True, "排空：零连接"
        if young > 0:
            _DRAIN_PENDING[tag] = {"rounds": 0, "last_bytes": bytes_}
            return False, "有年轻连接（请求仍在生命周期内）"
        pend = _DRAIN_PENDING.setdefault(tag, {"rounds": 0,
                                               "last_bytes": bytes_})
        if bytes_ != pend["last_bytes"]:
            pend["rounds"] = 0
            pend["last_bytes"] = bytes_
            return False, "仍有字节流"
        pend["rounds"] += 1
        need = max(1, int(_warp().get("drain_quiet_rounds", 2)))
        if pend["rounds"] >= need:
            _RETIRED_AT.pop(tag, None)
            _DRAIN_PENDING.pop(tag, None)
            return True, "排空：连续 %d 轮无字节增长" % need
        return False, ("等待字节流静止（%d/%d 轮，已等 %.0fs）"
                       % (pend["rounds"], need, elapsed))


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
    res = probe_egress(
        socks_port=port,
        socks_host=_warp()["socks_host"],
        urls=(TRACE_URL,),
        timeout=timeout_sec,
        require_warp=True,
    )
    # Cloudflare trace 里本来就带 `colo=` / `loc=`，此前被直接丢掉。留住它，
    # 节点→colo 的台账就能跟着生产探测自我更新，不必单独跑
    # `warp_wg.survey()`（逐节点起 sing-box，一次几十秒）。
    # 存进模块变量而不是改返回值：`warp_egress_ip` 有十几处调用方与测试桩，
    # 改签名等于把它们全部打断，而这个信息只有探测器要用。
    global _LAST_EGRESS_TRACE
    # `getattr` 而不是直接取属性：测试里 `probe_egress` 常被换成只带 `ip`
    # 的替身对象（`namedtuple`/`SimpleNamespace`），硬取会让整条探测路径
    # 在测试里抛出 AttributeError 而不是走正常逻辑。
    # `error` 是「拿不到出口」的**原因**（egress.py 的 _CURL_RC 分类）：
    # connect_refused = 端口没监听（单元没起）、dns_fail = 节点域名解析不了、
    # recv_fail = 隧道建立后被打断、timeout = 到点没响应、warp_off = 通了但
    # 没走 WARP。不记它的话，日志里只剩「探测失败」四个字，四种完全不同的
    # 故障在报表上长得一模一样。
    _LAST_EGRESS_TRACE = {"port": int(port), "ip": res.ip,
                          "colo": getattr(res, "colo", None),
                          "loc": getattr(res, "loc", None),
                          "error": getattr(res, "error", None)}
    return res.ip


# 「拿不到出口」的原因 → 人话。键来自 `asip/egress.py` 的 `_CURL_RC`。
# 分这几类的意义：`connect_refused` 是单元/端口的问题（本地可修），
# `dns_fail`/`recv_fail` 是节点或机场的问题，`timeout` 是链路慢，
# `warp_off` 是隧道通了但 CF 没给 WARP 会话 —— 处置动作完全不同。
_EGRESS_HINT = {
    "connect_refused": "端口没监听（单元没起或崩了）",
    "dns_fail": "节点域名解析失败",
    "tls_fail": "TLS 握手失败",
    "recv_fail": "隧道建立后被打断",
    "http_error": "出口侧返回 HTTP 错误",
    "timeout": "到点无响应",
    "warp_off": "通了但未走 WARP",
    "empty_reply": "空响应",
    "bad_body": "响应不是 trace 格式",
    "invalid_ip": "返回的不是合法地址",
    "curl_missing": "本机缺 curl",
    "rc_other": "curl 其它错误",
}


def _egress_error_hint(port: int) -> str:
    """上一次 `warp_egress_ip(port)` 失败的原因（人话）。拿不到就返回空串。"""
    trace = _LAST_EGRESS_TRACE
    if not isinstance(trace, dict) or trace.get("port") != int(port):
        return ""
    err = trace.get("error")
    return _EGRESS_HINT.get(str(err), str(err) if err else "")


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

    轮询间隔**恒为 0，不等待**（主人 2026-11-11 拍板）：两次探测之间不插任何
    停顿，失败就立刻重探。禁止改成 `sleep(>0)`、禁止退避曲线、禁止把它做成
    配置项 —— 见 `docs/established-facts.md` 12b 条（第 13 条讲的是 H2 三条件）。
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
        # 间隔 0：不等待，直接重探。sleep(0) 只让出 GIL、并保留假时钟的推进点，
        # 它本身不是等待 —— 任何 >0 的间隔都是被人改回来的，不是设计。
        time.sleep(0)


def _record_line_ip_drift(tag: str, old_ip: str | None, new_ip: str) -> None:
    """线内换了会话地址时补一条历史 —— 地址漂移不只有换线一种。

    实测 `.warp_line_state.json` 36 条线里 **17 条 `ip_ts` 晚于 `sel`**，即
    「选这条线之后它的出口地址又变了」是常态而非边角。历史只记换线的话，
    这段时间里发出的 429 回溯拿到的是这条线**上一次**的地址，把早已不承载
    流量的地址记成新坏账（实测：请求经 B 发出，回溯给 A）。

    只记**当前出口**那条线。全池都记的话，20 条线每分钟一轮探测就能往
    128 条的历史里灌满无关漂移，把真正的换线记录挤掉 —— 而迟到 429 只可能
    来自正在承载流量的那条。

    `active_warp_tag()` 在 `_WARP_LINE_LOCK` 与 `_WARP_LOCK` 之外（快照读），
    本函数可能在持 `_WARP_LINE_LOCK` / `_WARP_LINE_CV` 时被调用。
    """
    if not new_ip or new_ip == old_ip:
        return
    try:
        if active_warp_tag() != tag:
            return
    except Exception:
        return
    _record_warp_switch_history(tag, time.time(), new_ip)


def _set_warp_line_ip(tag: str, ip: str) -> None:
    with _WARP_LINE_LOCK:
        st = _WARP_LINE_STATE.setdefault(tag, {})
        old_ip = st.get("ip")
        st["ip"] = ip
        st["ip_ts"] = time.time()
    _record_line_ip_drift(tag, old_ip, ip)


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
    # 本进程内曾拿到过出口地址。与 `ip` 不同，它**不被隧道重启清空** ——
    # 「这条线在这台机器上到底通过没有」是判断「惩罚期重启还有没有意义」
    # 的依据（见 `_line_node_cannot_carry`）。
    "ever_ip": False,
    # `restart_count`（进程终身累计）已删：全库零读取者，唯一的报表用的是
    # `restart_hist` 的窗口内计数，而它累计到 4 万+ 只是磁盘垃圾。
    "restart_hist": [],
    "restart_ts": 0.0,
    "next_restart_ts": 0.0,
    # `quota_until` / `quota_ip` 已删（2026-11-05，见既定事实 23d）。不在这里
    # 声明就是把它们从盘上丢掉：`_warp_line_state_locked` 只收本表的键，
    # 旧台账里的残留字段加载时自然被滤掉，不需要额外的清理函数。
    "warming": False,
    "port_ok": False,
    "sel": 0.0,
    # 当天模型层实测样本：[(ts, verdict)]，只留当天、最多 channel_ban_max_samples 条。
    # 锁定判据是「当天最近 1000 条是否**全部** quota」—— 一条 OK 就否决。
    # 旧口径是 1h 窗口 100% 封禁 + 铺满半窗，只数最旧一条、不看空洞，而且
    # 锁定期间攒下的样本会在次日到期时互相续期（实测 WG-21 锁了 17h，期间
    # 出过 7 次 OK）。
    # 默认 None 不能用 []：_LINE_DEFAULTS 是共享对象，setdefault 会让所有渠道
    # 拿到同一个 list，于是一条线的样本会记进另一条线的窗口（自检抓到的）。
    "ban_probes": None,
    # 「惩罚期短路」观测：[(起, 止, 条数, 结论)]，游程形状同 `ban_probes`。
    # WHY 必须声明进本表：`_load_warp_line_state` **只恢复本表声明过的键**
    # （`st.update({k: v for k, v in raw.items() if k in _LINE_DEFAULTS})`），
    # 漏声明 = 写进盘上、读不回来 —— 实测 2026-11-11：磁盘上 156 个 tag，
    # 带 `blocked_notes` 的是 0 个，报表永远看不到这些观测。
    # 与上面 `penalty_restart_streak` 是同一个坑（那里的注释已专门警告过）。
    # WHY 不写进 `ban_probes`：`ban_probes` 是**渠道锁**的判据样本，
    # `_channel_ban_hold_hit` 要求当天样本**全部**判 quota 才锁线。把
    # blocked 混进去会让线永远锁不上 —— blocked 不是「渠道被烧穿」，
    # 是「这一个地址在惩罚期、换个地址就好」。所以它是独立字段、独立
    # trim、只供报表展示「这条线确实被探过、结论是 blocked」。
    # 默认 None 不用 []：同一个共享对象坑（`setdefault` 会让所有渠道
    # 拿到同一个 list）。
    "blocked_notes": None,
    # 连续探测不到出口地址的起点（0 = 当前能拿到地址）。连续
    # channel_noaddr_hold_sec(5h) 拿不到地址就锁渠道：隧道/节点已经死了，
    # 重启换地址也买不到任何东西。
    "ban_noaddr_since": 0.0,
    "ban_hold_until": 0.0,
    # 锁定期间不探测（主人 2026-11-04 定）。锁到期由 `_rehome_held_lines` 清掉，
    # 顺带清掉 `ban_probes` —— 不清的话第二天第一条实测进来时窗口里还躺着
    # 昨天的 quota，锁会立刻续到后天。
    "stop_probe": False,
    # 锁定前这条线的落地节点，次日 08:00 换回来；None = 没被重定位过。
    "ban_hold_home": None,
    # 重定位的目标节点。只用来对账：`warp_wg.apply`/订阅换绑定会整表重写
    # `warp_wg_state.json` 把 node 抹回 home（实测 21:40 一次抹掉 10 条），
    # 每分钟拿它和实际绑定比一次，不一致就补写。
    "ban_hold_node": None,
    # 重定位后那次重启是否已成功；0 表示配置写了但进程还没换过来。
    "ban_hold_ready": 0.0,
    # 补位渠道：被封的这条线由**另一条**渠道顶替（空闲设备线写到 ban 率最低
    # 的 colo 上），次日 08:00 解封时撤除。记在**被封那条线**上，进程重启后
    # 才撤得干净 —— 只放内存里的话重启一次就永远撤不掉。
    "ban_backfill_tag": None,
    "ban_backfill_node": None,
    # ── 「重启换地址」熔断状态（见 `_penalty_restart_fuse`）─────────────────
    # 必须在这张表里才活得过分号：`_load_warp_line_state` **只恢复本表声明过的
    # 键**，漏声明 = 每次 daemon 重启（部署、`Restart=always` 拉起、OOM、人工
    # restart）都把全池熔断清零。而重启恰恰是这个闸门要压的动作 —— 自己一重启
    # 就把闸门复位，等于没有闸门。实测 2026-11-10 06:48~06:58 连做 16 次部署
    # 重启，152 条线的 streak 全被打回 0，10 分钟 1225 次重启、0 条熔断。
    "penalty_restart_streak": 0,
    # 熔断截止时刻（epoch）。过期由时间比较自然失效，不需要额外的清理步骤。
    "ip_stuck_until": 0.0,
    # 最近几次重启换回来的出口地址。默认 None 不用 []：`_LINE_DEFAULTS` 是
    # 共享对象，`setdefault` 会让所有渠道拿到同一个 list（与 `ban_probes` 同一个坑）。
    "restart_ip_hist": None,
    # 连续「重启后拿不到任何出口地址」的次数（见 `_restart_outcome_note`）。
    # 与上面的配额熔断分开：那个判地址好坏（已按主人裁定禁用），这个判重启
    # 这个动作有没有效果。同样必须进本表才活得过分号。
    "futile_restart_streak": 0,
}

# 渠道锁到期还原的节流时刻（主循环每分钟对账一次）。
_CHANNEL_REHOME_TS = [0.0]


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
    else:
        # **条数保护**：盘上已有 N 条、这次要写 M 条且 M < N —— 拒绝覆盖。
        # WHY 需要：`_save_warp_line_state` 落的是整个内存状态表。如果此刻
        # 内存里的表还没加载盘上数据（例如启动早期、或某个清理函数在
        # `_load_warp_line_state()` 之前被调用），落盘的就是"只剩被清理的
        # 那一条" —— 实测 2026-11-11：3 条假数据被覆盖成 1 条，其余整条
        # 丢失，且没有任何日志。
        # 这条保护是**兜底**：它挡的不是某一个具体调用点，而是"整表覆盖"
        # 这类危险写法的**所有**未来变体。
        try:
            if os.path.getsize(path) > 2:
                with open(path, "r", encoding="utf-8") as f:
                    old = json.load(f)
                old_lines = old.get("lines", old) if isinstance(old, dict) else {}
                if isinstance(old_lines, dict) and len(old_lines) > len(payload["lines"]):
                    log(f"  ⚠ 拒绝覆盖状态表 {path}：盘上 {len(old_lines)} 条 > "
                        f"内存 {len(payload['lines'])} 条（疑似状态表未加载就落盘）")
                    return
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
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
    # 扫描期的兜底 tag（`WARP-H2-SCAN-<idx>`）在单元被渲染声明后就成了孤儿：
    # 台账里留着它的 OK，而换线只认声明 tag —— 于是 `asip chan` 显示 OK、
    # 候选池里永远没有它，也不会有任何日志解释原因（2026-11-08 实测
    # SCAN-31/33/34 即 SIN-31/HKG-33/HKG-34 的前身，OK 结论冻结了 5 小时）。
    # 单元已有声明归属就丢掉孤儿：状态属于「这个端口上的隧道」，名字换了，
    # 旧名字下的结论不再指向任何东西。仍无归属的单元照旧保留。
    declared_svc = {m.get("service") for m in (_warp().get("members") or [])}
    lines = {t: st for t, st in lines.items()
             if not (isinstance(t, str) and t.startswith("WARP-H2-SCAN-")
                     and f"usque-h2-{t.rsplit('-', 1)[-1]}.service" in declared_svc)}
    now = time.time()
    fresh = _line_fresh_sec()
    with _WARP_LINE_CV:
        for tag, raw in lines.items():
            if not isinstance(raw, dict):
                continue
            st = _warp_line_state_locked(tag)
            st.update({k: v for k, v in raw.items() if k in _LINE_DEFAULTS})
            # 重启是人工动作，不该继承重启前的小时预算：事故中 cap=20 会在启动后
            # 把每条线锁死 40~55 分钟（存档 restart_hist 里 228~245 条）。
            st["restart_hist"] = []
            st["warming"] = False
            # 同一处净化时间戳字段：下面 `_rehome_held_lines` / `_channel_ban_label`
            # / `_warp_line_restart` 全是裸 `float(...)`，盘上写个 ISO 字符串
            # （人改文件最自然的写法）就会让每分钟的 channel-rehome 整趟抛
            # ValueError，锁的线永远还原不了。
            for _k in ("ban_hold_until", "ban_hold_ready", "ban_noaddr_since",
                       "restart_ts", "last_ok_ts", "next_restart_ts",
                       "next_probe_ts", "ip_ts"):
                if not isinstance(st.get(_k), (int, float)) or isinstance(st.get(_k), bool):
                    st[_k] = 0.0
            if st.get("ban_hold_home") is not None and not isinstance(st.get("ban_hold_home"), str):
                st["ban_hold_home"] = None
            if st.get("ban_hold_node") is not None and not isinstance(st.get("ban_hold_node"), str):
                st["ban_hold_node"] = None
            for _k in ("ban_backfill_tag", "ban_backfill_node"):
                if st.get(_k) is not None and not isinstance(st.get(_k), str):
                    st[_k] = None
            # 盘上写 "false"/1 之类非布尔值时，`st.get("stop_probe")` 会恒真，
            # 这条线从此永不探测也永不复活 —— 必须夹成布尔。
            if not isinstance(st.get("stop_probe"), bool):
                st["stop_probe"] = False
            # `blocked_notes` 类型净化：盘上被写成别的形状时，报表的
            # `_expand_ban_probes` 展开会抛异常，把整个报表打挂。
            # WHY 与 `ban_probes` 分开处理：`ban_probes` 下面有一段会把它
            # 归 `[]` 并 trim（那是它原本的语义）；`blocked_notes` 没有那段，
            # 这里归 `None` —— 与 `_LINE_DEFAULTS` 的默认值一致，且写入点
            # （`_channel_blocked_note`）自己会判 isinstance 再重建。
            if not isinstance(st.get("blocked_notes"), list):
                st["blocked_notes"] = None
            if (st.get("last_ok_ip") != st.get("ip")
                    or now - float(st.get("last_ok_ts") or 0) > fresh):
                st["last_ok_ts"] = 0.0
                st["last_ok_ip"] = None
                # `ip` 一起清：它和 `last_ok_ts` 是同一份证据的两半 —— 地址
                # 只在「刚验证过」时才可信。留着陈旧的 `ip` 会让同块轮回守护
                # 把一条绑在死节点上的线当成「有出口」（实测 2026-11-02：WG-22
                # 绑到死节点 4 分钟，`ip` 还是上一次会话的旧地址，守护不动它）。
                st.pop("ip", None)
                st.pop("ip_ts", None)
            st["warming"] = False
            # 渠道锁是跨重启的结论（锁到次日 8 点），必须保留；但非当天的
            # 样本在启动时裁掉，否则隔夜重启会把昨天的样本算进「当天」。
            if not isinstance(st.get("ban_probes"), list):
                st["ban_probes"] = []
            _channel_ban_trim(st["ban_probes"], now)
        _WARP_LINE_CV.notify_all()
    log(f"✓ 已恢复 {len(lines)} 条 WARP 线路状态")


def _line_fresh_sec() -> float:
    return float(_warp().get("line_fresh_sec",
                             _warp().get("probe_transient_ttl_sec", 120)))


# ── 渠道 ban 率锁 ────────────────────────────────────────────
# 口径是「渠道」不是 colo：一条线固定落在一个 colo 上，整段烧穿时锁线最直接。
# 只在模型层实测上计数（生产流量判定的 quota 会走 blocked 台账那条路，不重复记）。
def _channel_ban_hold_secs(now: float) -> float:
    """距下一个解封时刻（次日 reset_hour）还有多少秒。"""
    lt = time.localtime(now)
    reset = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                         int(_warp().get("channel_ban_reset_hour", 8)),
                         0, 0, 0, 0, -1))
    if reset <= now:
        reset += 86400
    return reset - now


def _channel_ban_trim(samples: list, now: float) -> list:
    """只留当天样本；同结论相邻样本合并成段（保留最新）。纯函数。

    落盘形状是**游程**：`[(ts_start, ts_end, count, verdict), ...]`。
    原始点 `[(ts, verdict)]` 也认（向后兼容磁盘上的旧文件、调用方手工构造）。

    WHY RLE：`ban_probes` 是状态文件里唯一会无界增长的字段 —— 实测 2026-11-04
    19k 条样本占了 526 KB / 565 KB，而探测是每秒级写入，状态表按 5s 节流落盘
    ⇒ **9.4 GB/天**。判据本身只问「是否全部 quota」，不关心条数，但排查要看
    时序，所以不能按条数截断。RLE 同结论连续段压掉 39 倍（526 KB → 14 KB），
    样本一条不丢。

    **不按时间间隙拆段**：合并键只有 verdict。实测 gap<=60s 时拆段会让压缩比
    从 39x 掉到 3.6x —— 探测间隔本来就在几十秒级跳动，时间连续性不含信息。

    畸形元素必须在这里丢掉：`_load_warp_line_state` 用它裁剪盘上的
    `ban_probes`，而那里的 try 只包住 open/json.load —— 一条 `[null]` 就能让
    `for t, v in samples` 抛 TypeError，进程退出、systemd 10s 后拉起、同一个
    文件再崩一次，死循环。执行顺序：先判元素形状，再解包。
    """
    day = time.localtime(now)[:3]
    runs: list[list] = []
    for e in samples:
        if not isinstance(e, (list, tuple)) or len(e) < 2:
            continue
        if not isinstance(e[0], (int, float)) or isinstance(e[0], bool):
            continue
        # 段：`[起, 止, 条数, 结论]`；旧格式是 `(ts, 结论)`。两种都认，
        # 但结论的位置不同 —— 段格式取 e[3]，旧格式取 e[1]。搞混会把段尾
        # 时间戳当成结论，判据变成「全部 quota」恒假、渠道永远锁不上。
        if len(e) >= 4 and isinstance(e[2], int) and not isinstance(e[2], bool):
            start, end, count = float(e[0]), float(e[1]), int(e[2])
            verdict = str(e[3])
        else:
            start = end = float(e[0])
            count = 1
            verdict = str(e[1])
        if time.localtime(start)[:3] != day or time.localtime(end)[:3] != day:
            continue
        if runs and runs[-1][3] == verdict:
            runs[-1][1] = end
            runs[-1][2] += count
        else:
            runs.append([start, end, count, verdict])
    samples[:] = runs
    return samples


def _channel_ban_expand(samples: list) -> list:
    """把段展开成 `[(ts, verdict)]`，供只关心条数的调用方（报表 / 单测）。"""
    out: list[tuple[float, str]] = []
    for e in samples or []:
        if not isinstance(e, (list, tuple)) or len(e) < 2:
            continue
        if len(e) >= 4 and isinstance(e[2], int):
            out.extend([(float(e[1]), str(e[3]))] * max(0, int(e[2])))
        else:
            out.append((float(e[0]), str(e[1])))
    return out


def _channel_blocked_note(tag: str, now: float) -> None:
    """记一次「探测拿到地址、但该地址已在台账惩罚期」的观测。

    WHY 单独记、不写进 `ban_probes`：`ban_probes` 是**渠道锁**的判据样本，
    `_channel_ban_hold_hit` 要求当天样本**全部**判 quota 才锁线（出现任何
    非 quota 结论就否决）。把 blocked 混进去会让这条线永远锁不上 ——
    blocked 不是「渠道被烧穿」，是「这一个地址在惩罚期，换个地址就好」。

    WHY 又必须记：`_probe_one_line` 拿到地址后先查台账（`_warp_ip_is_blocked`），
    命中就直接 return，**根本不走到模型层实测**。于是这条线的状态是
    「判了 blocked、`last_verdict` 写着 blocked、但 `ban_probes` 是空的」——
    报表只能印「无样本」，读起来像「什么都没发生过」。实测 2026-11-11：
    27 条 blocked 线里 12 条零样本，冻结 26 小时没人知道原因。

    落盘形状沿用 `ban_probes` 的游程 `[起, 止, 条数, 结论]`，同结论相邻段
    合并 —— 这条路径每分钟可触发数次，不压缩会让状态文件无界增长。
    """
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        notes = st.get("blocked_notes")
        if not isinstance(notes, list):
            notes = st["blocked_notes"] = []
        notes.append([now, now, 1, "blocked"])
        _channel_ban_trim(notes, now)
    _save_warp_line_state()


def _channel_ban_hold_hit(samples: list, now: float) -> bool:
    """当天最近 N 条模型层实测是否**全部**判 quota。

    口径（2026-11-04 主人定）：样本里出现任何非 quota 结论（OK / flagged）
    就否决 ——「能出 OK 的渠道不许锁」。条数不足 `channel_ban_min_samples`
    说明今天还没被打够，也不算烧穿。

    旧口径是「1h 窗口 100% 封禁 + 铺满半窗」，只看最旧一条、不看空洞，
    锁定期间攒的样本还会在次到期时互相续期（实测 WG-21 锁了 17h，期间出过
    7 次 OK）；而且锁定后不再探测，窗口永远凑不出新证据解锁。

    样本只收地址级结论（OK / quota / flagged）。探测自身失败（err /
    unreachable）不入账：它说的是探测器或链路不通，不是「渠道烧穿」——那类
    故障由重启路径和「连续 5h 无出口地址」那条判据处理，混进来只会让
    真烧穿的渠道因为探测抖动而永远锁不上。
    """
    _channel_ban_trim(samples, now)
    n = sum(int(e[2]) for e in samples if len(e) >= 4)
    if n < int(_warp().get("channel_ban_min_samples", 1000)):
        return False
    return all(e[3] == "quota" for e in samples)


def _channel_ban_note(tag: str, verdict: str, now: float) -> None:
    """记一次模型层实测结论；当天样本全 quota 就给这条线写 ban_hold_until。"""
    # CF/usque 是直连 WARP 隧道池，没有可换的落地节点，锁它等于把直连出口
    # 白关到 08:00 —— 主人定性「永远都不能禁掉」。所以 CF 只是**不写锁**，
    # 不豁免采样：早先这里直接 return，ban_probes 从此不再增长，报表显示
    # 「无样本」，而 last_verdict 还停在锁之前那次的 quota —— 两个字段互相
    # 矛盾，读起来就是「原因标错了」。样本照记，末尾跳过写锁。
    cf_direct = tag.startswith("WARP-CF")
    if cf_direct:
        st = _warp_line_state(tag)
        if st.get("ban_hold_until") or st.get("stop_probe"):
            # 只清锁，不清 ban_probes：那是报表唯一的样本来源。
            _warp_line_update(tag, ban_hold_until=0.0, stop_probe=False)
            _save_warp_line_state()
    if not _warp().get("channel_ban_hold_enabled", True):
        # 总闸关掉后把锁解除：留着等于「关了还在锁」。但**不能清 ban_hold_home**
        # —— 那是有线在临时 colo 上的唯一记录，清了这条线就永远回不去了。
        # 到期分支会看到 until<=now 且 home 还在，下一轮把它还原。
        st = _warp_line_state(tag)
        if st.get("ban_hold_until") or st.get("ban_probes") or st.get("stop_probe"):
            # stop_probe 必须一起清：只在关闸时解 ban_hold_until 的话，CF/H2 线
            # （没有 home、走不到还原分支）会永久停在「不再探测」。
            _warp_line_update(tag, ban_hold_until=0.0, ban_probes=[],
                              stop_probe=False, ban_noaddr_since=0.0)
            _save_warp_line_state()
        return
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        samples = st.get("ban_probes")
        if not isinstance(samples, list):
            samples = st["ban_probes"] = []
        # 直接追加**段**：`[起, 止, 条数, 结论]`。`_channel_ban_trim` 会把
        # 同结论的相邻段并起来，所以这里不需要先展开再压回去。
        samples.append([now, now, 1, str(verdict)])
        _channel_ban_trim(samples, now)
        # CF 直连永不写锁：锁线没有可换的落地
        # 节点（_rehome_held_lines 对 CF 是空操作），只会把直连出口关掉。
        if cf_direct:
            if _channel_ban_held(tag, now):
                st["ban_hold_until"] = 0.0
                st["stop_probe"] = False
                _save_warp_line_state()
            return
        # 只看「当前仍在锁定中」：过期的 ban_hold_until 必须能重新锁上，
        # 否则渠道在前一天锁过之后永远不再锁（字段留着非零值，条件恒真）。
        if _channel_ban_held(tag, now) or not _channel_ban_hold_hit(samples, now):
            return
        until = now + _channel_ban_hold_secs(now)
        st["ban_hold_until"] = until
        st["stop_probe"] = True
        # 探测排期本身不看 next_probe_ts，但 prober 的空闲等待看它：留一个
        # 过期值会让它在锁定期内每 0.1s 醒一次空转一整天。
        st["next_probe_ts"] = until
    _save_warp_line_state(force=True)
    total = sum(int(e[2]) for e in samples if len(e) >= 4)
    log(f"  · {tag} 当天 {total} 次实测全部 quota，渠道锁定到 "
        f"{time.strftime('%m-%d %H:%M', time.localtime(until))}，不再重启、不再探测")
    _submit_once("channel-rehome", _rehome_held_lines, now)


def _channel_noaddr_note(tag: str, got_ip: bool, now: float) -> None:
    """连续 channel_noaddr_hold_sec 探测不到出口地址 → 锁渠道。

    与 quota 锁分开计：拿不到地址说的是隧道/节点死了，不是渠道被烧穿。
    重启换地址对死节点无效，锁是唯一不白烧的动作。期间任何一次拿到地址就
    清零，所以它只对「一直连不上」的线生效。
    """
    if not _warp().get("channel_ban_hold_enabled", True):
        return
    if got_ip:
        with _WARP_LINE_CV:
            st = _warp_line_state_locked(tag)
            if st.get("ban_noaddr_since"):
                st["ban_noaddr_since"] = 0.0
        return
    # 补位渠道不锁：它的意义就是「顶替一条被封的线」，锁住它等于让被顶的那条
    # 线整夜没人补位。实测 2026-11-04 补位线 WG-23/24/25 被锁到
    # `ban_hold_until=498288h`（约 57 年）—— 它们本来就在死节点上，5h 计数器
    # 一到点就把自己锁死，而 `_channel_ban_note` 的 quota 分支早有这道跳过
    # （这里漏了）。它坏了由 `_backfill_open` 重开。
    m = _warp_member(tag)
    if m and m.get("backfill_for"):
        with _WARP_LINE_CV:
            st = _warp_line_state_locked(tag)
            if st.get("ban_noaddr_since"):
                st["ban_noaddr_since"] = 0.0
        return
    hit_sec = float(_warp().get("channel_noaddr_hold_sec", 5 * 3600))
    # **H2 先换节点**。这条锁的判据是「重启换的是同一节点的地址，白烧」，而
    # selector 换节点不重启、不断流、零成本 —— 对 H2 不成立。实测 2026-11-05
    # NRT-13/14、TPE-5/6/18、SLC-11/12 被锁到次日 08:00，其中 NRT 两条当时
    # 就有 4 个 TCP 可达的候选。整池试遍（`_h2_next_node` 返回 None）才锁。
    if m and m.get("kind") == "h2" and _h2_repair(m):
        return
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        since = float(st.get("ban_noaddr_since") or 0.0)
        if not since:
            st["ban_noaddr_since"] = since = now
        if (now - since < hit_sec or _channel_ban_held(tag, now)):
            return
        until = now + _channel_ban_hold_secs(now)
        st["ban_hold_until"] = until
        st["stop_probe"] = True
        st["next_probe_ts"] = until       # 同上：别让 prober 空转等一个过期时刻
    _save_warp_line_state(force=True)
    log(f"  · {tag} 连续 {(now - since) / 3600:.1f}h 探测不到出口地址"
        f"（> {hit_sec / 3600:.1f}h），渠道锁定到 "
        f"{time.strftime('%m-%d %H:%M', time.localtime(until))}，不再重启、不再探测")
    _submit_once("channel-rehome", _rehome_held_lines, now)


def _channel_ban_held(tag: str, now: float) -> bool:
    return float(_warp_line_state(tag).get("ban_hold_until") or 0.0) > now


# ── 渠道锁定后的重定位 / 次日还原 ──────────────────────────────
# 被锁的渠道按「最近 1h ban 率最低的 colo」换落地节点，次日 08:00 换回原节点。
# 原节点记在 ban_hold_home 里，只记一次；还原不看是否还在锁定期 —— 锁到点时
# 本来就已经过了 08:00，两个动作是同一次维护。
def _channel_rehome_target(now: float, exclude: set[str] | None = None,
                           ignore_enabled: bool = False
                           ) -> tuple[str, str] | None:
    """取最近 1h ban 率最低、且样本足够的 colo → (colo, 节点 tag)。

    `exclude` 是本轮已经用掉的节点：一批线同时被锁时共用一个目标等于把它们
    挤到同一个 colo，下一轮该 colo 立刻烧穿。候选还必须同时在 **UDP 池**里
    （`_rehome_one` 按这个池判成员）——`wg_colo_map` 与实际池不是同一集合，
    NRT 的首位节点就不在池里，写下去只会得到 `gone`。

    跨调用的占用也要排除：同一时刻只锁了一条线时，单轮 `exclude` 是空的，
    它照样会被分到别的线已经绑着的节点上（既定事实第 20 条）。
    """
    exclude = set(exclude or ())
    try:
        exclude |= {n for n in warp_wg.line_nodes().values() if n}
    except Exception:
        pass
    # 补位渠道占着的节点同样要排除。只信 `line_nodes()` 不够：补位用的是
    # 台账里没有的空闲设备线，`_rehome_one` 只在 tag 已存在时才写 node，
    # 这个节点只记在 `ban_backfill_node` 上。
    exclude |= {st["ban_backfill_node"] for st in list(_WARP_LINE_STATE.values())
                if isinstance(st, dict) and st.get("ban_backfill_node")}
    # `ignore_enabled` 给补位用：补位是硬要求（主人 2026-11-04），重定位
    # 总闸关掉时它仍必须开。
    if not ignore_enabled and not _warp().get("channel_rehome_enabled", True):
        return None
    try:
        pool = {n["tag"] for n in warp_wg.udp_capable(warp_wg.load_nodes())}
        colo_map = warp_wg.load_colo_map()
        blocks_map = warp_blocks.load_map()
    except Exception:
        return None
    for colo in _lowest_ban_colos(now):
        nodes = [t for t, v in colo_map.items()
                 if isinstance(v, dict) and v.get("colo") == colo
                 and v.get("ok") is not False and t in pool and t not in exclude]
        if not nodes:
            continue
        # ban 率最低的 colo 里也可能只剩跑不了 WARP 的节点。实测 WG-1 被锁到
        # `cn.bing.com:443~13-any`（wg=0/4）后每分钟重启一次、连跑 70 次都拿不到
        # 出口 —— 隧道死了，`ban_hold_ready` 永远写不进去，重定位循环永不收敛。
        # 排序而不是过滤：整块都没测过时仍要能选中（新节点还没跑过 diag）。
        nodes.sort(key=lambda t: _warp_alive_rank(t, blocks_map))
        if _warp_alive_rank(nodes[0], blocks_map) == 2:
            log(f"  ⚠ 重定位目标 {colo} 的候选都实测承载不了 WARP"
                f"（{len(nodes)} 个），仍选 {nodes[0]}")
        return colo, nodes[0]
    return None


def _lowest_ban_colos(now: float) -> list[str]:
    """最近 `channel_ban_window_sec` 内 ban 率从低到高的 colo（样本不足的不算）。"""
    try:
        import ipv6_segments
        rates = ipv6_segments.colo_ban_rates(
            float(_warp().get("channel_ban_window_sec", 3600)), now)
    except Exception as e:
        log(f"  ✗ 读 colo ban 率失败: {e}")
        return []
    floor = int(_warp().get("channel_rehome_min_samples", 10))
    cand = [(b / n, c) for c, (n, b) in rates.items() if n >= floor and c != "UNKNOWN"]
    cand.sort()
    return [c for _rate, c in cand]


def _rehome_held_lines(now: float | None = None) -> None:
    """把被锁渠道换到低 ban 率 colo，并把锁到期的换回原节点。

    写完配置必须**自己重启这条线**：`_warp_line_restart` 是唯一能重启它的
    路径，而它被同一个 `ban_hold_until` 挡着（实测 19:59~20:46 十条线写进
    LAX 配置后一次都没重启，出口仍是原 colo）。所以这里用 `rebound=True`
    走同一入口的换节点分支 —— 锁拦的是「为同一个烧穿池反复重启」，换掉池子
    之后那一次重启正是锁想要的结果。

    还要对账 `ban_hold_node`：`warp_wg.apply` 与订阅换绑定会整表重写
    `warp_wg_state.json`，把 node 抹回 home（实测 21:40 一次抹掉 10 条）。
    每分钟比一次，不一致就补写并重启。
    """
    if not _warp().get("channel_ban_hold_enabled", True):
        # 关闸时先把所有锁清掉，再把停放在临时 colo 上的线还原回原节点。
        # **不能只靠 `_channel_ban_note` 的关闸分支**：那条分支要探测才会执行，
        # 而 `stop_probe` 恰好挡住探测 —— CF/H2 没有 home、在下面被 continue
        # 掉，于是永久卡在「不探测、不解锁」。关闸是回滚动作，必须立刻生效。
        now = time.time() if now is None else now
        for tag, st in list(_WARP_LINE_STATE.items()):
            if not isinstance(st, dict):
                continue
            _backfill_close(tag)      # 关闸是回滚：补位渠道一并撤掉
            if st.get("ban_backfill_tag"):
                # 台账行已丢、`_backfill_close` 直接返回 False 的那种。
                _backfill_stop(st["ban_backfill_tag"])
                _warp_line_update(tag, ban_backfill_tag=None,
                                  ban_backfill_node=None)
            if float(st.get("ban_hold_until") or 0.0) or st.get("stop_probe"):
                _warp_line_update(tag, ban_hold_until=0.0, ban_probes=[],
                                  stop_probe=False, ban_noaddr_since=0.0)
        for tag, st in list(_WARP_LINE_STATE.items()):
            if not isinstance(st, dict) or not st.get("ban_hold_home"):
                continue
            home = str(st["ban_hold_home"])
            r = _rehome_one(tag, home)
            if r in ("ok", "gone", "skip"):
                if r == "ok":
                    member = _warp_member(tag)
                    if member:
                        _warp_line_restart(member, "渠道锁关闭还原", rebound=True)
                _warp_line_update(tag, ban_hold_home=None, ban_hold_node=None,
                                  ban_hold_ready=0.0, ban_probes=[],
                                  stop_probe=False, ban_noaddr_since=0.0)
                log(f"  ⇢ {tag} 渠道锁已关闭，落地节点还原为 {home}")
        return
    now = time.time() if now is None else now
    used: set[str] = set()
    # 到期即解锁，与有没有 home 无关：CF/H2 线没有落地节点可还原，旧实现直接
    # `continue`，锁到期后 `stop_probe` 与当天 quota 样本原样留着 —— 实测
    # WG-21 到期 19s 就被旧样本重新锁到次日。
    # `ban_hold_until > 0` 是必须的：从没锁过的线这个字段是 0，不带它每分钟
    # 都会清掉正在攒的样本，1000 条永远攒不满。释放后把时间戳归零，否则下一
    # 分钟还会再清一次（新攒的样本同样被抹掉）。
    for tag, st in list(_WARP_LINE_STATE.items()):
        if not isinstance(st, dict):
            continue
        until = float(st.get("ban_hold_until") or 0.0)
        # **H2 的「取不到出口」锁可以在锁定期内直接解**。这条锁的前提是
        # 「重启换的是同一个节点的地址，白烧」，而 selector 换节点不重启、
        # 不断流、零成本 —— 前提不成立。实测 2026-11-05 NRT-13/14、TPE-5/6/18、
        # SLC-11/12 被锁到次日 08:00，其中 NRT 两条当时就有 4 个 TCP 可达的
        # 候选，整晚一次探测都没有。换成了就解锁放回探测；换不成（整池死）
        # 保持锁定，`_h2_repair` 自带 60s 节流。
        # 只认 `ban_noaddr_since`：quota 锁说的是「这个地址被烧」，节点本身
        # 是好的，换掉等于白丢一条能用的链路。
        if until > now and float(st.get("ban_noaddr_since") or 0.0):
            _m = _warp_member(tag)
            if _m and _m.get("kind") == "h2" and _h2_repair(_m):
                _warp_line_update(tag, ban_hold_until=0.0, ban_hold_ready=0.0,
                                  stop_probe=False, ban_noaddr_since=0.0)
                log(f"  ⇢ {tag} 换到可用节点，解除「取不到出口」锁")
                continue
        # 释放条件（三者都必须先满足 until <= now，否则会把还在锁里的线放出来）：
        #   ① 0 < until <= now —— 正常到期，也覆盖旧版本留下、没有 stop_probe
        #      字段的存量锁（不认它的话，旧锁到期后样本不清，下一条实测就重锁）；
        #   ② until == 0 且 stop_probe —— 人工清零 / 关闸半途崩过，否则永不复活；
        # 从没锁过的线（until=0、stop_probe=False）必须跳过，否则每分钟清一次
        # 正在攒的样本，1000 条永远攒不满。
        if until <= now and (until > 0.0 or st.get("stop_probe")
                             or st.get("ban_backfill_tag")):
            # 解封第一步先撤补位渠道：它就是为了顶这一天的封禁而开的。
            _backfill_close(tag)
            # **H2 的「取不到出口」锁：先换节点再解锁**。解锁只是让它重新被
            # 探测；不换节点的话「解锁 → 探测 → 3 次无地址 → 换节点 → 整池试遍
            # → 5h 又被锁」一圈回到原点，这条线一整天没出过口（实测 NRT-13 /
            # SLC-11 / TPE-5/6 各 700 分钟无探测）。换节点不重启、无成本。
            # 只对 `ban_noaddr_since` 锁生效：quota 锁是「地址被烧」，那时节点
            # 本身是好的，换掉等于白白丢掉一条能用的链路。
            m = _warp_member(tag)
            if (m and m.get("kind") == "h2"
                    and float(st.get("ban_noaddr_since") or 0.0)):
                _h2_repair(m)
            _warp_line_update(tag, ban_hold_until=0.0, ban_hold_ready=0.0,
                              stop_probe=False, ban_probes=[],
                              ban_noaddr_since=0.0)
    # 锁定中但还没有补位渠道的：开一条。这是「一个渠道被封，必定要有另外
    # 一个渠道补位」的执行点 —— 放在这里而不是 `_channel_ban_note`，因为
    # 开补位要起单元/刷新订阅（秒级），而那条路径在探测线程上。
    # 先对账再开：台账与锁线对不上时（开/撤半途异常）先修好，否则
    # ① 有主人没行的锁线永远被跳过、实际上没人补位；② 没主人的行永远撤不掉。
    _backfill_reconcile(now)
    used_bf: set[str] = used
    for tag, st in list(_WARP_LINE_STATE.items()):
        if not isinstance(st, dict) or st.get("ban_backfill_tag"):
            continue
        if float(st.get("ban_hold_until") or 0.0) <= now:
            continue
        _backfill_open(tag, now, used_bf)
    for tag, st in list(_WARP_LINE_STATE.items()):
        if not isinstance(st, dict):
            continue
        until = float(st.get("ban_hold_until") or 0.0)
        home = st.get("ban_hold_home")
        want = st.get("ban_hold_node")
        member = _warp_member(tag)
        if member and not _line_enabled(member):
            # 档位外的线不重定位：重定位会写节点配置，解禁后带着改过的绑定回来。
            continue
        if member and member.get("backfill_for"):
            # 补位渠道的位置是**指定**的（ban 率最低的那个 colo）：让它走
            # 重定位会把它搬离补位目标，补位就名存实亡。
            continue
        if until <= now:
            if not home:
                continue
            # 已经停在原节点上：还原的目的已达成，别再重写一次配置再试一次
            # 重启。以前这里不看当前节点，实测 08:00~11:50 重试 978 次只成功
            # 4 次，220 次重启打在本来就正确的节点上；而每分钟一次的
            # `_rehome_one` 把 `rebind_ts` 一直顶到最新，同块轮回的
            # 「刚动过的线不判」于是对这批线永远成立。
            # 必须同时确认「写回之后重启过」：`_rehome_one` 只写配置文件，
            # 不重启进程就还在旧节点上跑。
            if _line_node(tag) == home and _line_restarted_after_write(tag):
                # 走 "ok" 分支，与真正还原成功的线共用同一段收尾（清 home
                # 与 ban_window），否则它每分钟重新落进这里，永远收不了尾。
                r, ok = "ok", True
            else:
                r = _rehome_one(tag, str(home))
                ok = False
                # "skip" 也算结束：非 WG 线没有可换的落地节点，重试一万次
                # 同一个答案。
                if r in ("ok", "gone", "skip"):
                    ok = True
                    if r == "ok":
                        ok = (bool(member)
                              and _warp_line_restart(member, "渠道锁到期还原",
                                                     rebound=True))
                # 重启没成功就不能清 home：它是这条线停在临时 colo 上的唯一
                # 记录。清了之后进程仍跑新节点，台账却以为已经回家，永远
                # 不会再试。留到下一分钟再还原（`_rehome_one` 是幂等的）。
            if ok:
                # **必须一起清 `ban_probes`**：不清的话，解封前那批 quota
                # 样本还在，08:00 之后再记一条就直接重新锁到次日 08:00 ——
                # "锁一天" 退化成 "永久锁定"（实测 WG-21 08:00:19 复现）。
                _warp_line_update(tag, ban_hold_home=None, ban_hold_node=None,
                                  ban_hold_ready=0.0, ban_probes=[],
                                  stop_probe=False, ban_noaddr_since=0.0)
                if r == "ok":
                    log(f"  ⇢ {tag} 渠道锁到期，落地节点还原为 {home}")
                else:
                    log(f"  ⚠ {tag} 原节点已不可用，放弃还原，保留当前节点")
            else:
                log(f"  ⚠ {tag} 渠道锁到期，但还原重启未成功，"
                    f"下轮重试（原节点 {home}）")
            continue
        if want:
            used.add(str(want))
            if _line_node(tag) == want:
                if not st.get("ban_hold_ready") and member:
                    # 停在一个**实测承载不了 WARP** 的节点上：重启换不到出口，
                    # `ban_hold_ready` 永远写不进去，于是每分钟重定位一次 ——
                    # 实测 WG-1 停在 wg=0/4 的 `cn.bing.com:443~13-any` 上每 40s
                    # 重启一次、连跑 71 次（近 30 分钟重定位重启 119 次它占 71）。
                    # 只对 rank 2（实测不能）改投：rank 1 是没测过，那正是待验证
                    # 状态，不该因为没数据就搬走。
                    blocks_map = warp_blocks.load_map()
                    # family 只用来给 `_warp_alive_rank` 选证据列，取成员声明值。
                    # **不读状态表**：那里的 `family` 只在 `warp_wg.apply()` 时写
                    # 一次、从不随配置更新，改过拓扑就成旧世代残留 —— 实测本机
                    # `.env` 是 `WARP_WG_FAMILIES=v6`（全 v6），状态表里
                    # WG-1/2/3 却还记着上一代 `v4,v6,v6,v6` 的 v4。照它判会让
                    # 这三条线去查 v4 的 diag 记录（它们跑的是 v6）。
                    fam = (member or {}).get("family")
                    if _warp_alive_rank(str(want), blocks_map, fam) == 2:
                        target = _channel_rehome_target(now, used)
                        # 目标也必须**实测更好**才改投。全都 rank 2 时改投只是
                        # 把同一个死循环挪到下一个节点（每分钟换一台、永不重启）；
                        # 那种情况还不如原地重启，至少不额外烧一次写盘。
                        if target and _warp_alive_rank(target[1], blocks_map,
                                                       fam) < 2:
                            colo, node = target
                            if _rehome_one(tag, node) == "ok":
                                used.add(node)
                                _warp_line_update(tag, ban_hold_node=node,
                                                  ban_hold_ready=0.0)
                                log(f"  ⇢ {tag} 锁定节点 {want} 实测承载不了 "
                                    f"WARP，改投 {colo}（{node}）")
                                if _warp_line_restart(member, "渠道重定位",
                                                      rebound=True):
                                    _warp_line_update(tag, ban_hold_ready=now)
                                continue
                    if _warp_line_restart(member, "渠道重定位", rebound=True):
                        _warp_line_update(tag, ban_hold_ready=now)
                continue
            # 被 apply / 订阅换绑定抹回 home 了：补写并重启。
            if _rehome_one(tag, str(want)) == "ok" and member:
                _warp_line_update(tag, ban_hold_ready=0.0)
                if _warp_line_restart(member, "渠道重定位", rebound=True):
                    _warp_line_update(tag, ban_hold_ready=now)
                log(f"  ⇢ {tag} 落地节点被覆盖，已补写回 {want}")
            continue
        if home:
            continue
        # 非 WG 线（CF/H2）没有可换的落地节点，`_rehome_one` 只会返回 "skip"。
        # 先问再取目标：不然它们每轮都白扫 11.9MB 历史（实测单次 222ms），
        # 而且 `skip` 时 home 仍是 None，下轮又重来 —— 永不收敛。
        if not member or member.get("kind") != "wg" or not member.get("index"):
            continue
        target = _channel_rehome_target(now, used)
        if target is None:
            continue
        colo, node = target
        origin = _line_node(tag) or node
        r = _rehome_one(tag, node)
        if r == "ok":
            used.add(node)          # 写成功才占位；"gone"/"err" 没落地，不该占
            _warp_line_update(tag, ban_hold_home=origin, ban_hold_node=node,
                              ban_hold_ready=0.0)
            ok = bool(member) and _warp_line_restart(member, "渠道重定位",
                                                     rebound=True)
            if ok:
                _warp_line_update(tag, ban_hold_ready=now)
            log(f"  ⇢ {tag} 渠道锁定，落地节点换到 {colo}（{node}）"
                f"{'' if ok else '（重启未成功，下轮重试）'}，次日 08:00 换回 {origin}")
        elif r == "gone":
            log(f"  ⚠ {tag} 重定位目标 {node} 不在 UDP 池，跳过")


def _line_node(tag: str) -> str | None:
    try:
        return warp_wg.line_nodes().get(tag)
    except Exception:
        return None


def _line_restarted_after_write(tag: str) -> bool:
    """这条线自上次写回节点配置之后，是否真的重启过。

    `_rehome_held_lines` 每分钟对账一次。只按 `_line_node(tag) == home` 跳过
    是不够的：`_rehome_one` 写的是配置文件，**不重启就不生效**，进程可能仍跑
    在旧节点上。反过来，若已经重启过（`restart_ts` 晚于写盘时刻），目的就真的
    达成了，再写一遍配置 + 再重启一次纯属白掐在途连接 —— 实测 08:00~11:50
    重试 978 次只成功 4 次，其中 220 次重启打在本来就正确的节点上。

    台账缺失（读不到 `warp_wg_state.json`，例如属主被 root 抢走）时返回
    False：宁可多写一次，也不要把「读不到」当成「已经还原」。
    """
    try:
        ts = float(warp_wg._read_state()["lines"][tag].get("rebind_ts") or 0)
    except (KeyError, TypeError, ValueError):
        return False
    return ts > 0 and float(_warp_line_state(tag).get("restart_ts") or 0) >= ts


def _record_line_colo(tag: str, colo: str | None, loc: str | None,
                      ip: str | None) -> None:
    """把一次成功探测里的落点写进 `wg_colo_map.json`（节点→colo 台账）。

    WHY 在探测器里顺手做，而不是定期跑 `warp_wg.survey()`：
    `survey()` 要给每个节点单独起一个 sing-box、等它握手（一次几十秒），
    而生产探测**本来就已经拿到了整条 Cloudflare trace**（`colo=` / `loc=`
    就在响应里），此前被 `probe_egress` 直接丢掉。实测 2026-11-05：
    WG-3/WG-4 的落地节点 `downloadcfpro…~9-vl` / `~10-vl` 从没跑过 survey，
    块表里没有它们 ⇒ `_same_block_standby` 判不出「同块」直接放弃，
    两条线在死节点上无限重启。

    只写 `ok: true` 的条目：一次失败的探测说明不了落点在哪，写进去反而
    会把已知的良好记录冲成 `colo=None`（`survey(merge=True)` 同样只在
    成功时更新，这里保持一致）。
    """
    if not (colo and ip):
        return
    try:
        path = warp_wg.colo_map_path()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {"nodes": {}}
        nodes = data.setdefault("nodes", {})
        cur = nodes.get(tag) or {}
        if cur.get("colo") == colo and cur.get("ok"):
            return                      # 没变化，不刷盘
        nodes[tag] = {"colo": colo, "loc": loc, "v4": ip if ":" not in ip else None,
                      "ok": True}
        data["updated"] = int(time.time())
        warp_wg.write_json(str(path), data, mode=0o600, indent=2)
    except Exception as e:
        log(f"  · {tag} 记录 colo 失败: {e}")


_H2_CLASH: ClashClient | None = None
_H2_CLASH_MTIME = -1.0


def _h2_clash() -> ClashClient:
    """`asip-h2-singbox` 的 Clash API 客户端（端口取自渲染产物，与主配置不同）。"""
    global _H2_CLASH, _H2_CLASH_MTIME
    path = os.path.join(_PROJECT, ".warp", "h2", "singbox.json")
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0.0
    if _H2_CLASH is None or mtime != _H2_CLASH_MTIME:
        try:
            with open(path, encoding="utf-8") as f:
                ctrl = ((json.load(f).get("experimental") or {})
                        .get("clash_api") or {}).get("external_controller") or ""
        except (OSError, ValueError):
            ctrl = ""
        _H2_CLASH = ClashClient(f"http://{ctrl or '127.0.0.1:9091'}",
                                get_singbox_secret)
        _H2_CLASH_MTIME = mtime
    return _H2_CLASH


def _h2_select(member: dict | None, node: str) -> str:
    """H2 换节点 = `PUT /proxies/h2-sel-<i>`，**不重启任何进程**。

    节点必须已由 `render_h2_lines.py` 预写成候选出站；不在候选里时 sing-box
    回 400（池子要重渲染，返回 "gone"），selector 不存在回 404（进程还在跑
    旧配置，换线静默失效 —— 返回 "gone" 让调用方别再每轮重试）。
    """
    i = (member or {}).get("index")
    if i is None:
        return "skip"
    ob = f"h2-{node}"[:120]          # 出站按节点全局唯一（渲染器去重）
    status, _ = _h2_clash().request_json("PUT", f"proxies/h2-sel-{int(i)}",
                                         body={"name": ob})
    if status in (200, 204):
        return "ok"
    if status in (400, 404):
        return "gone"
    return "err"


# 每条 H2 线**本轮已试过**的候选节点；拿到出口地址时清空（见 `_probe_one_line`）。
# 没有它，整池失效的线（实测 ORD 池只有 1 个、SLC/LAX 池全是 bilibili）会每
# 60s 在死节点之间空转：PUT 本身无害，但每次都会清掉 `ip` 并触发一整轮探测。
_H2_TRIED: dict[str, set[str]] = {}
# 整池试遍仍拿不到出口的 H2 线 → 判死时刻。重启 usque 换的是同一个死节点上的
# 新会话，重启没有意义（见 `_line_node_cannot_carry`）。拿到出口或换节点成功
# 即清空；`_H2_POOL_DEAD_RETRY_SEC` 后再重走一遍池子，免得节点恢复后永久停产。
_H2_POOL_DEAD: dict[str, float] = {}
_H2_POOL_DEAD_RETRY_SEC = 1800.0


def _h2_next_node(member: dict | None) -> str | None:
    """这条 H2 线候选池里**下一个没试过**的节点；整池试遍返回 None。

    不探活：候选池的顺序已经是渲染器按可达性排过的（`_reachable_order`），
    而这里每探一个都要起一个临时 sing-box（机场域名只有订阅 DoH 解析得了），
    全池 23 个就是 23 个进程 —— 正是要修的那种风暴。按池序轮转即可。
    """
    if not member:
        return None
    i = member.get("index")
    if i is None:
        return None
    names, now_ob = _h2_clash().selector_state(f"h2-sel-{int(i)}")
    cands = [o[len("h2-"):] for o in (names or []) if o.startswith("h2-")]
    if not cands:
        return None
    tried = _H2_TRIED.setdefault(member["tag"], set())
    k = cands.index(now_ob[len("h2-"):]) if (now_ob or "").startswith("h2-") \
        and now_ob[len("h2-"):] in cands else -1
    for step in range(1, len(cands) + 1):
        node = cands[(k + step) % len(cands)]
        if node not in tried:
            return node
    return None


def _h2_repair(member: dict | None) -> bool:
    """H2 线取不到出口时先换节点（无重启）；换成了返回 True。"""
    if not member:
        return False
    tag = member["tag"]
    now = time.time()
    if now < _NODE_NEXT_CHECK.get(tag, 0.0):
        return False
    # 判死是暂时的：整池不通可能只是机场抖动。过了重试窗口把整池重新走一遍
    # （否则节点恢复后这条线永远停产 —— `_h2_next_node` 只认 `_H2_TRIED`）。
    dead = _H2_POOL_DEAD.get(tag)
    if dead and now - dead >= _H2_POOL_DEAD_RETRY_SEC:
        _H2_POOL_DEAD.pop(tag, None)
        _H2_TRIED.pop(tag, None)
    nxt = _h2_next_node(member)
    _NODE_NEXT_CHECK[tag] = now + float(
        _warp().get("node_rotation_interval_sec", 60))
    if not nxt:
        # 只有**试过候选**才判死：`_h2_next_node` 在 Clash API 读不到池子时
        # 同样返回 None（共享 sing-box 重启中），那不是节点的结论。
        if _H2_TRIED.get(tag):
            # `setdefault`：这里每 60s 被调一次，重写时间戳会让重试窗口永远不到点。
            _H2_POOL_DEAD.setdefault(tag, now)   # 整池试遍，等重渲染/重试窗口
        return False
    if _rehome_one(tag, nxt) != "ok":
        return False
    _H2_POOL_DEAD.pop(tag, None)
    _H2_TRIED.setdefault(tag, set()).add(nxt)
    # 新落地节点的出口地址必然不同：清掉旧地址，让探测器按新链路重新判。
    _invalidate_warp_line_ip(tag)
    log(f"  ⇄ {tag} 连续取不到出口 → 换节点 {nxt}（selector，未重启）")
    return True


def _rehome_one(tag: str, node: str | None) -> str:
    """把一条线写到指定落地节点。

    返回 "ok" / "skip"（不是可换线的成员或没配 index，不该重试）/ "gone"
    （节点不在可用池里，原节点可能已下架）/ "err"（写失败，可重试）。

    WG 线：写该线自己的 `lineN/singbox.json`，由调用方重启生效。
    H2 线：`PUT` 共享进程里这条线的 selector，立即生效、不重启（见 `_h2_select`）。
    usque（WARP-CF*）没有可换的落地节点，出口只随本机 colo 变。
    """
    if not node:
        return "skip"
    m = _warp_member(tag)
    if m and m.get("kind") == "h2":
        return _h2_select(m, node)
    if not m or m.get("kind") != "wg" or not m.get("index"):
        return "skip"
    try:
        nodes = {n["tag"]: n for n in warp_wg.udp_capable(warp_wg.load_nodes())}
        if node not in nodes:
            return "gone"
        # family 取成员声明值（`WARP_WG_FAMILIES`），**不读状态表**。
        # 状态表里的 `family` 只在 `warp_wg.apply()` 时写一次，之后既不重建也不
        # 随配置变 —— 改过拓扑的机器上它是**旧世代残留**。本机实测：
        # `.env` 是 `WARP_WG_FAMILIES=v6`（22 节点 × 1 渠道，全 v6），而状态表
        # 里 WG-1/2/3 还记着上一代 `v4,v6,v6,v6` 的 v4。照着状态表重写会把三条
        # 线全写成 `prefer_ipv4`，v4 出口只由 colo 决定 ⇒ 三条线挤成同一个
        # `104.28.205.117`（实测；既定事实第 27 条：v4 渠道数不应超过 colo 数）。
        # `write_line(family=None)` 自身也按 `line_family(i)` 算，与 apply 同源。
        warp_wg.write_line(int(m["index"]), nodes[node],
                           family=m.get("family"))
        st = warp_wg._read_state()
        line = (st.get("lines") or {}).get(tag)
        if isinstance(line, dict):
            line["node"] = node
            line["rebind_ts"] = int(time.time())
            warp_wg.write_json(str(warp_wg.state_path()), st, mode=0o600, indent=2)
        return "ok"
    except Exception as e:
        log(f"  ✗ 渠道重定位 {tag} -> {node} 失败: {e}")
        return "err"
    except SystemExit as e:
        # `warp_wg.load_nodes()` 在读不到 singbox-config.json 时 raise SystemExit
        # （BaseException，上面那个 except Exception 接不住）。它会顺着
        # `_rehome_held_lines` / `_node_rotation_pass` 一路冒出线程的
        # `except Exception`，把 warp-node-rotation 线程**永久打死**，
        # `_start_node_rotation` 只启动一次、没有自愈。
        log(f"  ✗ 渠道重定位 {tag} -> {node} 失败（配置不可读）: {e}")
        return "err"


# ── 补位渠道 ────────────────────────────────────────────────────
# 一条渠道被封，必须**另有**一条渠道顶上：拿已注册但没启用的空闲设备线，
# 在 ban 率最低的 colo 上开一条「复制」渠道，发布进主配置，让 opencode 能
# 切过去；次日 08:00 解封时停单元、撤成员、把 selector 还原。
#
# 成员身份落在 `.warp/backfill.json` 而不是只在内存里：`deploy/members.py`
# 是独立进程（`refresh_main_outbounds` 起子进程），只改 daemon 的
# `CFG["warp"]["members"]` 它看不见，出口根本发不出去；而且 daemon 重启
# 一次补位渠道就凭空消失，08:00 也就无从撤销。
_BACKFILL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              ".warp", "backfill.json")


def _backfill_rows() -> list[dict]:
    try:
        with open(_BACKFILL_FILE, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _wg_line_state(i: int) -> str:
    """一条 WG 设备线的单元状态：`installed` / `missing` / `masked`。

    补位判据（`_backfill_spare_indices`）与自造（`_ensure_backfill_line`）都要
    问同一个问题，**必须共用这一份**：mask 判定一旦漂移，自造就会把主人手工
    停用（软链 `/dev/null`）的线重新装回单元，等于自动撤销他的决定。
    """
    unit = f"/etc/systemd/system/{warp_wg.unit_name(i)}"
    try:
        real = os.path.realpath(unit)
    except OSError:
        return "missing"
    if real == "/dev/null":
        return "masked"
    return "installed" if os.path.exists(real) else "missing"


def _backfill_spare_indices() -> list[int]:
    """已注册、还没进 members、且**单元已安装**的空闲设备线序号。

    三项都要查，少一项就会挑中一条起不来的线：

    - `profile.conf` 存在 = 设备注册过
    - 不在 `members` 里 = 没在服务流量
    - **单元文件存在且不是 /dev/null** = 真能 `systemctl start`

    最后一项是 2026-11-04 踩的坑：`line30` 的 `profile.conf` 在（注册过），
    但 `warp-wg-30.service` 从没生成过 —— 补位挑中它，报
    `Unit warp-wg-30.service not found`，然后 `continue` 到下一轮又挑中
    同一个 30（27 被 mask 跳过、28/29 已在 members），补位永远开不起来。
    单元由 `deploy/wg-lines-install.sh` 生成，注册与装单元是**两个**步骤。
    """
    tags = {m.get("tag") for m in _warp()["members"]}
    out = []
    # ponytail: 上界写死 99 —— 设备线是安装期注册的，几十条量级；
    # 真超过时把上界改成扫目录取 max。
    for i in range(1, 100):
        if not (warp_wg.line_dir(i) / "profile.conf").exists():
            continue
        if warp_wg.line_tag(i) in tags:
            continue
        # 档位停用的线不进补位候选：停用的 WG 线成员仍在 members 里，但补位
        # 一旦 start 它就把档位悄悄推翻了。补位真正该用的是 line23+ 闲置设备。
        if warp_wg.line_tag(i) in _TIER_DISABLED:
            continue
        # mask（/dev/null）与「根本没装过」都是起不来的，一并排除。
        if _wg_line_state(i) != "installed":
            continue
        out.append(i)
    return out


# 开补位失败后的重试间隔。主循环每分钟调一次；不节流的话「没有空闲线」
# 这种持续状态会每分钟打一行日志、并把 11.9MB 的 ban 率历史重扫一遍。
_BACKFILL_TRY_TS: dict[str, float] = {}
_BACKFILL_RETRY_SEC = 600.0


# 自造补位设备线的限速：注册一台 wgcf 设备烧一格 CF 额度（实测约 9 台/窗口），
# 爆封时一轮里多个渠道同时缺补位，一次性全注册会把窗口吃光。
_WG_CREATE_GAP = 600.0
# 线号硬上限，防跑飞：补位线在 08:00 解封时只是**停掉**（profile 与单元保留），
# 下次被封会复用，所以总量本应停在「并发被封数」量级。但若补位判据哪天回归出
# bug（比如新线装完单元却不被 `_backfill_spare_indices` 认），这条路径会每 10
# 分钟造一台直到把 CF 额度与线号空间吃光。宁可停下报错，也不无声膨胀。
_WG_MAX_LINES = 64


def _last_wg_register_ts() -> float:
    """最近一次注册 WG 设备的时刻 = 最新 `profile.conf` 的 mtime。

    不用进程内变量：daemon 一重启就归零，爆封期一重启就能连烧几格 CF 额度
    （实测 2026-11-04 19:35 重启后 8 秒内连造两条线）。`profile.conf` 由
    `register_line` 写、别处不改，它的 mtime 就是「最后注册时刻」，天然跨重启。
    **装单元不碰它** —— 装单元不烧额度，不该被限速。
    """
    newest = 0.0
    for i in range(1, 100):
        try:
            newest = max(newest, (warp_wg.line_dir(i) / "profile.conf").stat().st_mtime)
        except OSError:
            continue
    return newest


def _install_wg_unit(i: int) -> bool:
    """装一条 WG 设备线的 systemd 单元（含 daemon-reload）。

    两条约束叠在一起，决定了只能这么调：

    - 单元要落进 `/etc/systemd/system`，daemon 以普通用户跑 ⇒ 必须 sudo
      （本机 `/etc/sudoers.d/90-administrator-nopasswd`；只用 `-n`，没有免密
      就立刻失败，绝不在服务进程里挂起等密码）
    - **unit 自己带 `ProtectSystem=strict`，`/etc` 在它的 mount namespace 里是
      只读的** —— `sudo` 不换 namespace，所以直接 `sudo bash wg-lines-install.sh`
      会写进只读挂载：`/etc/systemd/system/warp-wg-32.service: Read-only file
      system`。实测 2026-11-04 补位自造连续失败两次。

    所以走 `systemd-run`：新起一个**独立**的 transient 单元，它在宿主
    namespace 里跑，不受 daemon 那份沙箱约束。`--wait` 拿它自己的退出码，
    `--pipe` 把子进程的 stdout/stderr 接回来 —— **少了 `--pipe` 两个流全是
    空的**（实测 `--wait` 单独用时 `capture_output` 拿到 `[]`），失败时日志里
    只剩一句「单元安装失败」而没有任何原因。

    传给脚本的用户/家目录必须显式给：以 root 跑脚本时 `id -u`=0，
    `getent passwd 0` 得到 `/root`，单元会写成 `User=root` + `HOME=/root`，
    而 `.warp` 下的凭据属 administrator —— 那条线起来就读不到自己的配置。
    """
    argv = [os.path.join(_PROJECT, "deploy", "wg-lines-install.sh"), "--line", str(i)]
    if os.geteuid() != 0:
        pw = pwd.getpwuid(os.getuid())
        argv = ["sudo", "-n", "systemd-run", "--pipe", "--wait", "--collect",
                "--quiet",
                f"--unit=asip-wg-install-{os.getpid()}-{i}-{int(time.time())}",
                f"--setenv=ASIP_RUN_USER={pw.pw_name}",
                f"--setenv=ASIP_RUN_GROUP={grp.getgrgid(pw.pw_gid).gr_name}",
                f"--setenv=ASIP_HOME_DIR={pw.pw_dir}",
                "/bin/bash", *argv]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        log(f"  ⚠ 补位设备线 {i} 单元安装失败: "
            f"{(r.stderr or r.stdout or '').strip()[-140:]}")
        return False
    return True


def _ensure_backfill_line(max_new: int = 1) -> list[int]:
    """没有可用补位线时自造一条，返回当前可补位线号。

    补位判据 `_backfill_spare_indices` 要求「已注册 **且** 单元已装」。
    `warp_wg.register_line` 只写 `profile.conf`，单元归
    `deploy/wg-lines-install.sh` 另一步 —— 只注册不装单元等于不存在
    （实测 line31 注册了一整晚，日志每轮都报「没有能起设备的空闲线」）。

    先补「已注册但没装单元」的（不烧 CF 注册额度，**不受限速**），再注册
    新设备（每 `_WG_CREATE_GAP` 秒最多一台，CF 对 wgcf 约 9 台/窗口）。
    """
    spare = _backfill_spare_indices()
    if spare or max_new <= 0:
        return spare

    tags = {m.get("tag") for m in _warp()["members"]}
    # ① 已注册、没单元、也没被 mask 的线：装个单元就能用。
    for i in range(1, 100):
        if not (warp_wg.line_dir(i) / "profile.conf").exists():
            continue
        if warp_wg.line_tag(i) in tags:
            continue
        # 档位停用的线：装单元本身无害，但装完就会被上面当补位候选挑中并
        # start，等于绕过档位。这里一并排除。
        if warp_wg.line_tag(i) in _TIER_DISABLED:
            continue
        # mask（/dev/null）是主人停用的线，不许自动恢复；已装的也不用动。
        if _wg_line_state(i) != "missing":
            continue
        if _install_wg_unit(i):
            return _backfill_spare_indices()
        return []

    # ② 一台储量都没有：注册新设备，接着最大槽位往后编。
    registered = [i for i in range(1, 100)
                  if (warp_wg.line_dir(i) / "profile.conf").exists()]
    if len(registered) >= _WG_MAX_LINES:
        log(f"  ✗ 设备线已达上限 {_WG_MAX_LINES} 条，不再自造；"
            f"检查补位判据是否把新线漏掉了")
        return []
    if time.time() - _last_wg_register_ts() < _WG_CREATE_GAP:
        return []
    start = (max(registered) if registered else 0) + 1
    for i in range(start, start + max_new):
        try:
            warp_wg.register_line(i)
        except Exception as e:
            # 多半是 429。退避已在 register_line 里做过，这里只记账不重试。
            log(f"  ⚠ 补位设备 {i} 注册失败，下轮再试: {type(e).__name__}")
            return []
        if _install_wg_unit(i):
            break
    return _backfill_spare_indices()


def _backfill_open(tag: str, now: float, used: set[str]) -> str | None:
    """给被封的 `tag` 开一条补位渠道，返回补位 tag；开不起来返回 None。

    「封禁率最低的渠道其 colo 的复制」= 在 ban 率最低、还有空节点的那个
    colo 上，用空闲设备线起一条和现有渠道同规格（WG/v6）的新线。
    """
    member = _warp_member(tag)
    if not member or member.get("backfill_for"):
        # 补位渠道自己也被封时不再套娃：否则一条封禁能无限拉新线。
        return None
    if now - _BACKFILL_TRY_TS.get(tag, 0.0) < _BACKFILL_RETRY_SEC:
        return None
    # `ignore_enabled=True`：补位是硬要求，不随 `channel_rehome_enabled` 关。
    target = _channel_rehome_target(now, used, ignore_enabled=True)
    if target is None:
        _BACKFILL_TRY_TS[tag] = now
        log(f"  ⚠ {tag} 渠道被封，但 ban 率最低的 colo 没有可用节点，无法补位")
        return None
    colo, node = target
    spares = _backfill_spare_indices()
    if not spares:
        # 一台储量都没有：先自造（装单元 / 注册设备），**本轮就用上**。
        # 放到循环外是有意的 —— 若放在失败分支里，新线要等下一个
        # `_BACKFILL_RETRY_SEC`(600s) 才被看见，扁平化后同一次调用即可补位。
        try:
            spares = _ensure_backfill_line()
        except Exception as e:
            log(f"  ⚠ 补位设备线自造失败: {type(e).__name__}: {e}")
        if spares:
            log(f"  ＋ 已备好补位设备线 {','.join(map(str, spares))}")
    for idx in spares:
        btag, unit = warp_wg.line_tag(idx), warp_wg.unit_name(idx)
        port = _warp()["wg"]["base_port"] + idx - 1
        entry = {"tag": btag, "port": port, "service": unit, "kind": "wg",
                 "index": idx, "family": "v6", "backfill_for": tag, "node": node}
        _warp()["members"].append(entry)
        if _rehome_one(btag, node) != "ok":   # 写 line{idx}/singbox.json
            _warp()["members"].remove(entry)
            continue
        # 先把单元拉起来：起不来就别发布，免得把一个死端口挂进主配置。
        # `_unit_ctl` 的 subprocess 有 timeout，超时会抛 —— 不接住的话这里
        # 留下一条已在 members 里、却没进台账的线，下一轮会再开一条补位。
        try:
            cr = _unit_ctl("start", unit)
        except Exception as e:
            cr = type("R", (), {"returncode": -1, "stdout": "", "stderr": str(e)})()
        if cr.returncode != 0:
            _warp()["members"].remove(entry)
            log(f"  ⚠ 补位设备线 {btag} 起不来，换下一条: "
                f"{(cr.stderr or cr.stdout or '').strip()[:160]}")
            continue
        break
    else:
        _BACKFILL_TRY_TS[tag] = now
        # 区分「没有空闲设备」与「有设备但单元没装」—— 前者是容量问题，
        # 后者跑一次 deploy/wg-lines-install.sh 就能补，处置完全不同。
        undeployed = [i for i in range(1, 100)
                      if (warp_wg.line_dir(i) / "profile.conf").exists()
                      and warp_wg.line_tag(i) not in
                      {m.get("tag") for m in _warp()["members"]}
                      and _wg_line_state(i) == "missing"]
        hint = (f"（已注册但未部署单元: line{','.join(map(str, undeployed[:5]))}）"
                if undeployed else "")
        # 到这里说明自造也没造出来（CF 429 / 只读挂载 / 注册失败），
        # 下一轮 `_BACKFILL_RETRY_SEC` 后再试。
        log(f"  ✗ {tag} 渠道被封，没有能起设备的空闲线可补位{hint}")
        return None
    if not _wait_port_ready(port, _warp()["ready_timeout_sec"]):
        log(f"  ⚠ 补位渠道 {btag} 端口 {port} 未就绪，仍然发布（prober 会继续探）")
    try:
        _atomic_write_json(_BACKFILL_FILE, _backfill_rows() + [entry])
        try:
            warp_wg.refresh_main_outbounds()
        except Exception as e:
            log(f"  ✗ 补位渠道 {btag} 发布 warp-outbound.json 失败: {e}")
        if not refresh_subscription(reason=f"渠道补位 {btag}"):
            log(f"  ⚠ 补位渠道 {btag} 已写入 warp-outbound.json，"
                f"但主配置本轮未刷新（下次刷新后生效）")
    except Exception as e:
        # 台账写失败（磁盘满 / 权限）不能把整趟 `_rehome_held_lines` 带崩：
        # 别的锁线还在等还原。行丢了由 `_backfill_reconcile` 下轮重开。
        log(f"  ✗ 补位渠道 {btag} 台账写入失败: {e}（下轮对账会重开）")
    finally:
        # 发布成功与否都必须记账：`_atomic_write_json` / `_unit_ctl` 都会抛，
        # 没记的话 `ban_backfill_tag` 为空，下一轮会再开一条补位 —— 前一条
        # 单元在跑、成员在表里，却再也没人撤得掉（撤销只认这个字段）。
        _warp_line_update(tag, ban_backfill_tag=btag, ban_backfill_node=node)
        _save_warp_line_state(force=True)
    used.add(node)
    log(f"  ⇢ {tag} 渠道被封，补位渠道 {btag} 已在 {colo}（{node}）上线，"
        f"次日 {int(_warp().get('channel_ban_reset_hour', 8)):02d}:00 撤除")
    # 被封的那条正好在服务流量：切到补位渠道，补位才算真的补上。
    if active_warp_tag() == tag:
        _switch_warp(btag)
    return btag


def _backfill_close(tag: str) -> bool:
    """解封：撤掉 `tag` 的补位渠道并还原 selector。返回是否还有事要做。"""
    btag = _warp_line_state(tag).get("ban_backfill_tag")
    if not btag:
        return False
    _backfill_stop(btag, back=tag)
    _atomic_write_json(_BACKFILL_FILE,
                       [r for r in _backfill_rows() if r.get("tag") != btag])
    try:
        warp_wg.refresh_main_outbounds()
    except Exception as e:
        log(f"  ✗ 撤补位渠道时发布 warp-outbound.json 失败: {e}")
    refresh_subscription(reason=f"渠道补位撤销 {btag}")
    _warp_line_update(tag, ban_backfill_tag=None, ban_backfill_node=None)
    _save_warp_line_state(force=True)
    _BACKFILL_TRY_TS.pop(tag, None)
    log(f"  ⇢ {tag} 渠道解封，补位渠道 {btag} 已撤除")
    return True


def _backfill_stop(btag: str, back: str | None = None) -> None:
    """停掉补位单元并把它从 members 里摘掉（不碰台账行，调用方负责）。

    `back` 是 selector 的首选去向（原线）；它已经不在 members 里时退到
    第一条别的成员。先切走再撤成员：selector 指着一个从配置里消失的 tag
    会 404。

    单元名优先从成员取，成员不在时按 tag 后缀反推 —— 台账行丢过之后
    （`_atomic_write_json` 抛异常 / 文件被删）成员已经不在了，不反推的话
    这条单元会一直跑下去。
    """
    if active_warp_tag() == btag:
        target = back if back and _warp_member(back) else next(
            (m["tag"] for m in _warp()["members"] if m.get("tag") != btag), None)
        if target:
            _switch_warp(target)
    member = _warp_member(btag)
    svc = (member or {}).get("service") or ""
    if not svc:
        try:
            svc = warp_wg.unit_name(int(str(btag).rsplit("-", 1)[-1]))
        except ValueError:
            svc = ""
    if svc:
        try:
            _unit_ctl("stop", svc)
        except Exception as e:
            log(f"  ⚠ 停补位单元 {svc} 失败: {e}")
    members = _warp()["members"]
    # 按 tag 删，不按对象身份：`_warp_member` 可能返回等值副本。
    members[:] = [m for m in members if m.get("tag") != btag]


def _backfill_reconcile(now: float) -> None:
    """每分钟对账补位台账与锁线，修两种半途状态。

    ① 行没有主人：锁线的 `ban_backfill_tag` 丢了（文件损坏 / 人工改），
       或解封时只清了锁没清行 —— 单元还在跑、成员还在表里，正常路径
       （按 `ban_backfill_tag` 撤）再也撤不掉它。
    ② 主人没有行：`_atomic_write_json` 抛过。锁线以为有补位，开补位循环
       永远跳过它 —— 那条渠道实际上没人补位，违背「被封必有补位」。
       停掉残留单元、清空主人字段，下一轮重新开。
    """
    rows = _backfill_rows()
    row_tags = {r.get("tag") for r in rows}
    locked = {tag: st for tag, st in list(_WARP_LINE_STATE.items())
              if isinstance(st, dict)
              and float(st.get("ban_hold_until") or 0.0) > now}
    for tag, st in locked.items():
        btag = st.get("ban_backfill_tag")
        if btag and btag not in row_tags:
            _backfill_stop(btag)
            _warp_line_update(tag, ban_backfill_tag=None, ban_backfill_node=None)
            log(f"  ⇢ 补位渠道 {btag} 的台账行已丢失，已停掉并清空 "
                f"{tag} 的补位记录，下一轮重开")
    owned = {st.get("ban_backfill_tag") for st in locked.values()
             if st.get("ban_backfill_tag")}
    keep = [r for r in rows if r.get("tag") in owned]
    if len(keep) == len(rows):
        return
    for r in rows:
        if r.get("tag") not in owned:
            _backfill_stop(str(r.get("tag")))
            log(f"  ⇢ 补位渠道 {r.get('tag')} 没有认领的锁线，已撤除")
    _atomic_write_json(_BACKFILL_FILE, keep)
    try:
        warp_wg.refresh_main_outbounds()
    except Exception as e:
        log(f"  ✗ 撤孤儿补位渠道时发布 warp-outbound.json 失败: {e}")
    refresh_subscription(reason="补位渠道对账")


# ── 同块节点轮回 ────────────────────────────────────────────────
# 用户语义：一个节点变慢/不可用时，把这条线换到**同一个 colo 块**里的另一个
# 可用节点。与「换线」（换出口 IP）和「探测」（判 IP 质量）是两件事 ——
# 这里只看**落地节点本身**还通不通。
#
# 为什么必须独立于现有两条路径：`validate_bindings` 只在节点从订阅池里消失时
# 才换（节点还在、只是不通就不管），`_restart_warp_line(rebind=True)` 只在 v4
# 线重试时触发。v6 线的节点死掉后，daemon 会一直重启一条注定连不上的线。

# 节点 TCP 结论缓存：{node: (ts, bool|None)}。TTL 与探测节奏同量级 ——
# 每条线每 5s 探一次会把机场的 DNS 和节点都打爆，而节点「死」不是秒级状态。
_NODE_TCP_SEEN: dict[str, tuple[float, bool | None]] = {}


def _node_tcp_ok(node: str, timeout: float = 6.0) -> bool | None:
    """落地节点能否转发 TCP。None = **无法判断**。

    WHY 必须经 sing-box 而不是本进程直接连：机场节点域名只有订阅里的 DoH
    解析得了。实测直接用 `socket.create_connection` 时 19 条线全部
    TimeoutError/gaierror，而其中 8 条当时正在正常出流量 —— 拿系统 DNS 的
    失败当节点死亡，会把整批健康线判死并无谓重启。

    None 单独一档：解析/起探测进程失败是**观测者**的问题，不是被观测对象的
    状态，与 `_warp_probe_blind` 同一条原则 —— 测不出来就不猜。
    """
    now = time.time()
    hit = _NODE_TCP_SEEN.get(node)
    if hit and now - hit[0] < 30:
        return hit[1]
    try:
        ob = warp_wg.node_outbound(node)
    except Exception:
        return None
    if ob is None:
        return None
    try:
        res = warp_wg.probe_node_tcp(ob, timeout=timeout)
    except Exception:
        # 探测进程自己崩了（写配置失败、subprocess 起不来）是观测者的问题，
        # 不是节点状态。抛出去会把整轮轮回打成异常，一条节点拖住其余全部。
        return None
    _NODE_TCP_SEEN[node] = (now, res)
    return res


def _line_node_cannot_carry(tag: str) -> bool:
    """这个节点在这个「惩罚期重启」路径上**重启没有意义**吗。

    两种都算：
      1. 节点实测承载不了 WARP（rank 2）—— 换多少次地址都拿不到出口；
      2. 这条线**从没拿到过出口地址**（`ip` 从未写入）—— 惩罚期重启是为了
         换一个新的 /128，而「从没出过口」说明问题在节点或链路上，不在地址。

    只看已有结论，不现场探测：这是秒级高频路径，加探测会把主循环拖垮。
    rank 1（没测过）不算不能承载 —— 未知不等于坏。
    """
    try:
        m = _warp_member(tag)
        if not m:
            # 扫描发现的隧道（`WARP-H2-SCAN-<idx>`，见 `_scan_running_tunnels`）
            # **不在 `members` 声明里**，`_warp_member` 永远返回 None，函数就
            # 直接落到下面的 `warp_wg` 台账查找 —— 那里也没有它（node=None）
            # → 恒返回 False。于是这批线绕开了「从没出过口就别重启」这条判据，
            # 被无限重启。
            # 实测 2026-11-10 09:00 前后 15 分钟：322 次重启里 **226 次（70%）**
            # 打在这 47 条线上。它们的状态完全一致：`ever_ip` 全 False、`ip` 全空、
            # `last_ok_ts` 全 0、`last_verdict` 全 unknown、`fail_streak` 204~206
            # —— 一次都没通过，重启只是把同一个死节点再拉一遍。
            # 判据与被跳过的声明线分支同一口径：没出过口 + 探过 → 重启无意义。
            st = _warp_line_state(tag)
            if not bool(st.get("ever_ip")) and st.get("last_probe_ts"):
                return True
            return False
        if m.get("kind") == "h2":
            # H2 的落地节点在共享 sing-box 的 selector 里，**不在 `warp_wg` 台账**
            # —— 查台账永远 `node=None`，下面整段对 H2 从来没生效过。实测
            # 2026-11-05 SLC-11/12/24 整池（4 个 bilibili 节点）TCP 全死、
            # `ever_ip` 始终 False，仍每 ~2 分钟重启一次 usque-h2-N。
            # 判据只用 `_h2_repair` 已经算过的结论（它每 60s 亲自巡一遍池子），
            # 这里不额外打 Clash API —— 本函数在探测热路径上。
            #
            # ① **探过但从没出过口**（与 WG 分支同一口径）：问题在落地节点或
            #    链路，重启 usque 拿到的是同一节点上的新会话。实测 2026-11-05
            #    SLC-11/12/24 换完节点 1~2s 就被重启掐掉，新节点一次都没被
            #    探测过；1779 次重启一次也没出过口。
            # ② 出过口的线相反：更可能是本机会话僵死，重启是唯一修法，只有
            #    整池试遍（`_H2_POOL_DEAD`）才判死。
            st = _warp_line_state(tag)
            if not bool(st.get("ever_ip")) and st.get("last_probe_ts"):
                return True
            return tag in _H2_POOL_DEAD
        line = (warp_wg._read_state().get("lines") or {}).get(tag) or {}
        node = line.get("node")
        if not node:
            return False
        st = _warp_line_state(tag)
        # **不能只看当前的 `ip`**：`_invalidate_warp_line_ip` 在每次隧道重启时
        # 都会清空它，刚重启的线必然 `ip=None`。拿它当判据会让这条线永远不再
        # 重启（连本来能恢复的也停掉）。看的是「这条线在本进程里成功出过口吗」
        # ——`ever_ip` 由 `_warp_line_record_observation` 在拿到地址时置位。
        ever = bool(st.get("ever_ip"))
        if not ever and st.get("last_probe_ts"):
            return True
        return _warp_alive_rank(str(node), warp_blocks.load_map(),
                                line.get("family")) == 2
    except Exception:
        return False


def _warp_alive_rank(tag: str, nodes_map: dict, family: str | None = None) -> int:
    """节点实测能不能承载 WARP：0 能 / 1 没测过 / 2 实测不能。

    只用于**排序**，从不淘汰 —— 节点会恢复，而把没测过的排除掉等于让整池
    只剩测过的那几台（实测 rotation 176 个槽位里 135 个从没测过，而块内
    实测可用的 14 台 NRT 节点只有 1 台进过 rotation）。

    `family=None`：这条线还不确定跑哪族（换线目标先于线号确定）。此时已测
    的族**全部**失败才算不能用。已测两族的 5 台节点里 0 台出现两族结论相反，
    跨族兜底不会把能用的判死。
    """
    e = nodes_map.get(tag) or {}
    fams = (("v4_",) if family == "v4"
            else ("wg_",) if family == "v6" else ("wg_", "v4_"))
    # 指定了族就**只看那一族**：拿 v4 的失败去否决一条 v6 线是错证据 ——
    # 实测 `cn.bing.com:443~10-any` 只有 v4 实测（0/2）、从没跑过 v6，
    # 旧写法会把它判成 v6 也不能用。没测过就是没测过（rank 1）。
    measured = [(e.get(f + "n") or 0, e.get(f + "ok") or 0) for f in fams]
    measured = [x for x in measured if x[0]]
    if not measured:
        return 1
    return 0 if any(ok for _n, ok in measured) else 2


def _same_block_standby(tag: str, now: float) -> str | None:
    """同块里第一个 TCP 可达的候选节点；当前节点除外。

    **v4 线例外**：v4 出口地址只由落地 colo 决定（实测同一节点重启就在
    .211/.243 间跳，与节点无关），换同 colo 的节点拿回的是同一个地址池 ——
    对它来说「换节点」必须换到**别的 colo** 才等于「换地址池」。
    v6 相反：地址每次重连都换（连 /64 都变），同 colo 换节点就够。
    """
    import warp_blocks
    st = warp_wg._read_state()
    line = (st.get("lines") or {}).get(tag) or {}
    cur = line.get("node")
    rot = [t for t in (line.get("rotation") or []) if t != cur]
    if not rot:
        return None
    nodes_map = warp_blocks.load_map()
    home_colo = (nodes_map.get(cur) or {}).get("colo")
    if home_colo is None:
        # 块表里没有当前节点（`_channel_rehome_target` 挑目标用的是
        # `load_colo_map()`，两张表不同源；实测 LAX 4 个池内节点只有 1 个在块表）。
        # **不能直接放弃**：实测 WG-3/WG-4 的当前节点
        # `downloadcfpro…~9-vl` / `~10-vl` 从没跑过 diag，块表里根本没有它们，
        # 于是这里恒返 None —— 两条线永远换不了节点，只能在同一个
        # 「收 TCP 不转发 UDP:2408」的节点上无限重启（实测 fail_streak 197 / 88）。
        #
        # 退化为**不按块分组**，只按承载能力排序：块信息缺席时「同块」无从判断，
        # 但那不等于没有候选 —— 把整条 rotation 交给下面的 rank 排序，
        # rank 0（实测能承载）自然会排到前面。v4 线仍然吃亏（它靠换 colo 换
        # 地址池，没 colo 判定不了），但总好过钉死在死节点上。
        order = list(rot)
    else:
        same = [t for t in rot if (nodes_map.get(t) or {}).get("colo") == home_colo]
        others = [t for t in rot if t not in same]
        # v4：跨块优先。同 colo 换节点拿回同一 /24，等于没换地址池。
        order = (others + same) if line.get("family") == "v4" else (same + others)
    # 块内顺序定了之后，同档内把**实测不能承载**的压到最后（稳定排序，块序不动）。
    # 不加这条时轮换是在赌：实测 WG-1 在 HKG/NRT 两个 wg=0/4 的节点之间来回
    # 换了 12 次，每次都是「节点 TCP 通但拿不到出口」；39 次同块轮回里 5 分钟内
    # 拿到出口的只有 15 次（38%）。
    fam = line.get("family")
    order.sort(key=lambda t: _warp_alive_rank(t, nodes_map, fam))
    # 跳过已被别的线绑着的节点：多条线挤到同一台上等于把「分散风险」变成
    # 「集中风险」。实测 2026-11-02 一轮误换之后 WG-1/WG-2 同时绑到
    # `cn.bing.com~13`、WG-8/WG-9 同时绑到 `pq.aws48` —— 那一台的 UDP 一挂，
    # 两条线一起死。留当前节点自己（tag 会先被排除）不影响。
    # 占用集必须**每次现读**：批量换节点时同一轮里前面的线刚改过绑定，任何
    # 缓存快照都会让后面的线把同一个「已空出」的节点再选一遍。实测 2026-11-10
    # 一轮把 15 条 WG 线全换到同一个 PIT 节点（出口清一色 ORD）。
    try:
        live = warp_wg._read_state().get("lines") or {}
    except Exception:
        live = st.get("lines") or {}
    taken = {ln.get("node") for t, ln in live.items()
             if t != tag and isinstance(ln, dict)}
    free = [t for t in order if t not in taken]
    for t in (free or order):
        if _node_tcp_ok(t):
            return t
    return None


# 同一时刻最多几个节点探测在跑。每次探测都要起一个 sing-box 进程，
# 29 条线无上限并发 = 29 个进程同时抢 CPU，反而让全部探测超时。
_NODE_PROBE_SLOTS = threading.Semaphore(4)
# 下一轮最早什么时候再探这条线（秒）。探测要起进程，不能 5s 一轮。
_NODE_NEXT_CHECK: dict[str, float] = {}
# 连续判死的计数：{tag: {"node": str, "n": int, "ts": float}}。
# 单次不通只记一笔、下一轮复查 —— 机场抖动与探测冷启都会让一次探测失败，
# 立刻换节点会让整池每两轮翻一次面。
_NODE_FAILS: dict[str, dict] = {}


def _node_rotation_pass(now: float | None = None) -> None:
    """同块轮回：当前绑定节点不通的线，换到同块可用节点。

    只换**节点**，重启交给调用方（与 `_rehome_one` 同源：写配置 + 更新台账）。
    对 v6 线是必要的 —— 它们原本从不换节点。

    节流：每条线最多 `node_rotation_interval_sec`(60s) 探一次。探测是「起一个
    sing-box 再连一次」，5s 一轮 × 29 条线会把机器打满，而节点死亡不是秒级
    状态 —— 一次没通可以是机场抖动，连续两轮都没通才值得换。
    """
    if not (_warp().get("node_rotation_enabled", True)):
        return
    now = time.time() if now is None else now
    interval = float(_warp().get("node_rotation_interval_sec", 60))
    st = warp_wg._read_state()
    active = active_warp_tag()
    for tag, line in (st.get("lines") or {}).items():
        if not isinstance(line, dict) or not line.get("node"):
            continue
        if tag == active:
            continue                      # 当前出口不换节点（会掐断在途）
        bf = _warp_member(tag)
        # 档位外的线不换节点：_rehome_one 会写配置 + 改台账，重启虽被最后
        # 一道闸拦住，绑定已经被改掉了 —— 解禁回来就是一条跑偏的线。
        if bf and not _line_enabled(bf):
            continue
        if bf and bf.get("backfill_for"):
            # 补位渠道的落地节点是补位目标本身（ban 率最低的那个 colo），
            # 换个节点就补错地方了；它坏了由 `_backfill_open` 重开。
            continue
        if now < _NODE_NEXT_CHECK.get(tag, 0.0):
            continue
        cur = line["node"]
        # 刚动过的线不判：隧道握手 + 出口地址就绪要好几十秒，这期间 `ip` 天然
        # 是空的。两个来源都要看 ——
        # ① `rebind_ts`：本守护换节点时写；
        # ② `restart_ts`：探测器/补刷重启隧道时写。`_invalidate_warp_line_ip`
        #    在每次隧道重启时把 `ip` 清空，所以重启后的线必然 `ip=None`，
        #    而它可能只是刚换了个出口地址、线本身是好的。实测 2026-11-02
        #    只挡 ① 的话 WG-3 被误换（它刚被探测器重启过一次，重启后 42s
        #    就被判「拿不到出口」，换到了一个已知的死节点）。
        moved = max(float(line.get("rebind_ts") or 0),
                    float(_warp_line_state(tag).get("restart_ts") or 0))
        # 冷静期是「给刚重启的隧道几十秒起来」。但一条线反复重启、每次还是
        # 取不到出口，这个前提就不成立了 —— 而每次重启都把 `restart_ts` 顶到
        # 现在，冷静期便永远成立，轮回一次也轮不到它。实测 2026-11-05
        # WARP-WG-4 `fail_streak` 13、每 ~40s 重启一次、`restart_ts` 永远是
        # 新的，轮回日志里从没出现过它。用 `fail_streak` 破这个循环：连续
        # 取不到出口说明重启已经证明无用，该换的是节点。
        if (now - moved < interval * 2
                and int(_warp_line_state(tag).get("fail_streak") or 0)
                < _UNKNOWN_RESTART_STREAK):
            _NODE_NEXT_CHECK[tag] = now + interval
            continue
        with _NODE_PROBE_SLOTS:
            ok = _node_tcp_ok(cur)
        if ok is not False:
            # 节点本身通，但**出口**可能长期不可用。两条判据，任一成立就换：
            #
            # ① 本小时重启打满：实测 WARP-WG-10 节点 TCP 全通、却连续 341 次
            #    取不到出口地址，拿到地址也立刻判 quota 惩罚 12.9h。重启换的
            #    是同一个节点的地址池 —— 只有换节点才能换到别的池。
            #
            # ② 节点 TCP 通、但这条线**从没取到过出口**（且已探过至少两轮）：
            #    这是「节点收 TCP、不转发 UDP:2408」的唯一可观测信号。实测
            #    2026-11-02 七条线（WG-4/6/11/13/18/19/20）起不来的正是这类
            #    节点，而 `_node_tcp_ok` 全判健康 —— 不换的话它们会一直躺着。
            #    需要 `last_probe_ts` 有值才判：刚 apply 完还没人探过的线，
            #    `last_ok_ts=0` 不说明任何问题，换上备线也只是再猜一次。
            st_line = _warp_line_state(tag)
            hist = [t for t in (st_line.get("restart_hist") or [])
                    if isinstance(t, (int, float)) and now - t < 3600]
            cap = int(_warp().get("restart_max_per_hour_per_line", 20))
            if cap > 0 and len(hist) >= cap:
                _NODE_FAILS[tag] = {"node": cur, "n": 2, "ts": now,
                                    "why": f"出口长期不可用({len(hist)} 次重启)"}
            elif not st_line.get("ip"):
                # 节点 TCP 通、但这条线**连出口地址都没拿到** —— 落到下面的
                # 两轮判死。实测：`_node_tcp_ok` 对 `cn.bing.com~32/~33`、
                # `unamecf~4` 全返回 True，而它们的 trace 一条也拿不到 ——
                # 节点收 TCP 但不转发 UDP:2408，是「重启救不了、只有换节点
                # 能救」的那一类。
                #
                # **判据必须是 `ip` 不是 `last_ok_ts`**：后者在
                # `verdict=blocked/quota` 时被主动清零（`_warp_line_record`
                # 里明明拿到了地址也清），而 blocked 说的是「这个出口 IP 被
                # opencode 限流」，不是「隧道不通」。实测踩过：拿 `last_ok_ts`
                # 判，22 条线全被标成死线，包括正在正常出流量的 PHX。
                # `ip` 只在真拿到 WARP 出口地址时才写入，才是「隧道起来了」。
                #
                # 仍需连续 3 轮：刚 apply 完的线还没来得及探测，`ip` 天然为空。
                seen = _NODE_FAILS.get(tag) or {}
                if seen.get("node") != cur:
                    _NODE_FAILS[tag] = {"node": cur, "n": 0, "ts": now}
                _NODE_FAILS[tag]["n"] = int(_NODE_FAILS[tag].get("n") or 0) + 1
                if _NODE_FAILS[tag]["n"] < 3:
                    _NODE_NEXT_CHECK[tag] = now + interval
                    log(f"  · {tag} 节点 {cur} TCP 通但拿不到出口地址"
                        f"（第 {_NODE_FAILS[tag]['n']} 轮观察，第 3 轮确认）")
                    continue
                _NODE_FAILS[tag]["why"] = "节点 TCP 通但拿不到出口地址(UDP 不通)"
                _record_udp_bad_node(cur, now)
            else:
                # 节点通、出口也有：这条线是健康的，把上一轮的观察计数清掉。
                # 不清的话，一次「TCP 通但没出口」留下的 n=1 会一直躺着，下一
                # 次只要再来一次失败就直接凑够 3 轮换节点 —— 实测 WG-8 在
                # 22:30 与 23:11 两次换节点，而全日志「首次探测不通」只有 1 条。
                _NODE_FAILS.pop(tag, None)
                _NODE_NEXT_CHECK[tag] = now + interval
                continue
        # 第一次判死只记一笔，下一轮再探：单次失败可能是机场抖动或探测冷启。
        fail = _NODE_FAILS.get(tag) or {}
        if fail.get("node") != cur:
            _NODE_FAILS[tag] = {"node": cur, "n": 1, "ts": now}
            _NODE_NEXT_CHECK[tag] = now + interval
            log(f"  · {tag} 节点 {cur} 首次探测不通，下一轮复查")
            continue
        _NODE_FAILS.pop(tag, None)
        _NODE_NEXT_CHECK[tag] = now + interval
        nxt = _same_block_standby(tag, now)
        if not nxt:
            log(f"  ⚠ {tag} 节点 {cur} 不通，同块无可用后备")
            continue
        if _rehome_one(tag, nxt) != "ok":
            continue
        why = "节点不通" if ok is False else (fail.get("why") or "出口长期不可用")
        log(f"  ⇄ {tag} {why} → 同块换到 {nxt}")
        # 被渠道锁住的线：锁管的是「别为同一个烧穿池反复重启」，不管「把线
        # 钉死在一个 UDP 不通的节点上」。把锁的目标节点改成轮回刚选中的这个，
        # 否则 `_rehome_held_lines` 下一分钟就把它写回去 —— 实测 WG-21
        # 08:50~11:50 被两边来回改了 6 次，每次都白搭一条「渠道重定位」重启。
        # `ban_hold_home` 不动：08:00 仍要还原到锁定前的原节点。
        if _channel_ban_held(tag, now):
            _warp_line_update(tag, ban_hold_node=nxt)
        m = _warp_member(tag)
        if not m:
            continue
        # 夜间锁**不拦换节点后的重启**。它拦的是「为一个被封的出口反复重启同
        # 一个节点」—— 重启换的是同一个地址池，白烧。换节点已经把地址池换掉
        # 了，不重启等于配置写进文件、进程还在用旧节点：线照样死，而守护以
        # 为自己已经救过它了。实测 2026-11-02：WG-22 换到 `~33` 后没重启，
        # 出口探测一直拿不到结果。
        # 换节点后必须清零重启配额：这个上限本意是「同一条线别无限重启」，
        # 而换节点已经把「重启同一个坏节点」这个前提换掉了。不清零的话，
        # 触发轮回的那条线（重启次数正是打满的）会被 `_warp_line_restart`
        # 直接拒掉 —— 配置换了、进程没重启，等于什么都没做。
        with _WARP_LINE_CV:
            _warp_line_state_locked(tag)["restart_hist"] = []
            _warp_line_state_locked(tag)["next_restart_ts"] = 0.0
        # 走重启治理，不要直接 `_restart_service`：探测器、补刷、脏线守护都在
        # `_warp_line_restart` 后面共享 per-line 锁、5s 冷却、指数退避与每小时
        # 上限。绕过它等于让两套逻辑对同一条线各重启一次，白烧一次探测。
        # `rebound=True`：刚换过节点，夜间锁/渠道锁/额度隔离/小时配额都得让路
        # —— 它们拦的是「同一个地址池反复重启」，而这里池子已经换了。
        if (_warp_line_restart(m, "同块轮回", rebound=True)
                and _channel_ban_held(tag, now)):
            # 换节点那次重启成功 = 渠道锁想要的「停在新节点上跑」已达成，
            # 记下 ready，`_rehome_held_lines` 就不会再补一次重定位重启。
            _warp_line_update(tag, ban_hold_ready=now)


def _line_unknown_retry() -> float:
    # 兜底也必须是 0：探不到结论就立刻重新排队，不插入任何等待。
    # 主人 2026-11-11 定标，见 docs/established-facts.md 12b。
    return float(_warp().get("probe_unknown_retry_sec", 0))


def _line_probe_cadence() -> float:
    # 缺配置时的兜底也必须是 0（探完立刻重新排队），不能是历史值 60：
    # 曾经 36 条线 × 60s 把吞吐锁死在 36 条/分钟。主人 2026-11-11 定标：探测
    # 不设等待时间，`config.line_probe_cadence_sec` 也不许被改成 >0。
    return float(_warp().get("line_probe_cadence_sec", 0))


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


# 这里曾有第二份 `_line_probe_cadence`（历史遗留的重复定义，后者覆盖前者）。
# 2026-11-11 审查后合并：两份定义会让「改了前面那份其实没生效」，
# 正好削弱红线的防篡改能力。唯一定义在文件前段，静态用例守住它。


# ── 夜间刷新锁 ──────────────────────────────────────────────
# 策略（窗口算术、放行判定）在 warp_night.py：纯函数、能单测、不用起线程。
# 这里剩下的全是 IO —— 读 CFG、查当前出口、打日志、按块生成白名单。
# WHY 夜间要锁：上游免费层配额按出口 IP 计，次日 08:00 才重置。20:00→08:00
# 之间被烧的地址，重启只会换到另一个同样被烧的新 /128 —— 10-31 夜间实测
# 17036 次重启、0 次刷新成功，全部白烧，还把每条线在途连接掐断了一万多次。
_NIGHT_LOG_TS: dict[str, float] = {}
# 块白名单缓存：(取数时刻, 线路 tag 集合)。夜里每条线的每次重启判定都会问
# 一遍，而实测表 127KB、线路台账只在 apply 时变。
_BLOCK_ALLOW_TTL = 30.0
_block_allow_cache: tuple[float, frozenset[str]] | None = None


def _night_window() -> dict:
    return warp_night.window(CFG)


def _night_active(now: float | None = None) -> bool:
    return warp_night.active(_night_window(), now)


def _block_night_tags() -> set[str]:
    """按块生成的夜间白名单（线路 tag）。块模型未启用时返回空集。

    模板是块、落点是线路：块回答「哪个 colo 夜里还值得刷」（封禁率 < 100%），
    线路台账回答「哪条线现在落在那个 colo」。拿渠道名当白名单会是空的 ——
    现网成员叫 `WARP-WG-7`，不叫 `WARP-BLK-NRT-REG-0`。

    任何异常都退回空集（＝沿用配置里的 allow）：夜里最坏的结果是「按老名单锁」，
    而抛出去会让重启判定直接崩掉，那等于整池不设防。
    """
    global _block_allow_cache
    if not warp_blocks.enabled(CFG):
        return set()
    now = time.time()
    if _block_allow_cache and now - _block_allow_cache[0] < _BLOCK_ALLOW_TTL:
        return set(_block_allow_cache[1])
    try:
        tags = warp_blocks.night_line_tags(CFG, warp_wg.line_nodes())
    except Exception as e:
        log(f"  · 块白名单生成失败，退回配置 allow：{type(e).__name__}: {e}")
        return set()
    _block_allow_cache = (now, frozenset(tags))
    return tags


def _night_allow_tags() -> set[str]:
    """白名单与 sing-box 实际成员取交集。

    不在 `opencode-out` 里的线刷出地址也切不过去 —— `.warp/warp-outbound.json`
    自 10-28 就没再更新，CF-E/CF-F/WG-13 至今不在里面。让它们占着白名单只会把
    夜里有限的刷新次数花在切不走的地址上。快照未建立时（启动早期）退回全量。

    块模型启用时白名单由块生成（docs/warp-channel-allocation.md 第四节），配置
    里的 `allow` 只在块表不可用时兜底 —— 两处同时生效就退回「硬编码 8 个 tag」
    的老毛病：块扩到 28 条渠道后，名单里还是那 8 个。
    """
    allow = _block_night_tags() or {
        t for t in (_night_window().get("allow") or []) if isinstance(t, str)}
    snap = _clash_snapshot(_warp()["selector"])
    members = snap.get("all") if snap else None
    if not members:
        return allow
    inter = allow & set(members)
    # 交集为空（配置改了但 warp-outbound 没重建）时退回不锁：照交集执行等于
    # 夜里一条线都不许刷，比不锁更糟 —— 失败要落在「无限制」这一侧。
    return inter or allow


def _night_block_restart(tag: str, reason: str, now: float) -> bool:
    """夜间窗口内非白名单线的重启拦下，返回 True 表示已拦。

    只管重启，不管探测：探到 quota 照样记账，只是不花一次重启去换一个同样
    quota 的地址。当前出口例外 —— 它不走重启换线，而 `_restart_warp_inplace`
    是「所有备线都不新鲜」时的最后兜底，锁住它等于夜里彻底断流。
    """
    # 窗口判定放在最前：它是纯本地算术，而 active_warp_tag() 在快照未建立时
    # 是一发 HTTP。白天不该为了一条永不生效的闸多付这次调用。
    nw = _night_window()
    if not _night_active(now):
        return False
    if warp_night.restart_allowed(nw, now, tag, active_warp_tag(), _night_allow_tags()):
        return False
    # 按 tag 节流：单时间戳会让 20 条线夜里 30 秒内只打一条，读日志的人
    # 会以为只有那一条被拦，实际整池都在锁里。
    if now - _NIGHT_LOG_TS.get(tag, 0.0) > 30.0:
        _NIGHT_LOG_TS[tag] = now
        log(f"  · 夜间锁 {nw.get('start_hour')}:00-{nw.get('end_hour')}:00 "
            f"{tag} 不在存活白名单，跳过重启（{reason}）")
    return True


def _night_label(now: float) -> str:
    """健康行里的夜锁摘要：`关` / `20-8:3线` / `待20点/3线`。"""
    return warp_night.label(_night_window(), now, len(_night_allow_tags()))


def _channel_ban_label(now: float) -> str:
    """健康行里的渠道锁摘要：`关` / `3线→08:00` / `2线·1待重启`。"""
    if not _warp().get("channel_ban_hold_enabled", True):
        return "关"
    with _WARP_LINE_CV:
        held = [t for t, st in list(_WARP_LINE_STATE.items())
                if isinstance(st, dict)
                and float(st.get("ban_hold_until") or 0.0) > now]
        pending = sum(1 for t in held
                      if not _WARP_LINE_STATE[t].get("ban_hold_ready"))
    if not held:
        return "无"
    when = int(_warp().get("channel_ban_reset_hour", 8))
    return (f"{len(held)}线→{when:02d}:00" + (f"·{pending}待重启" if pending else "")
            + (f"·{len(_backfill_rows())}补位" if _backfill_rows() else ""))


def _warp_line_needs_probe(tag: str, now: float) -> tuple[bool, float]:
    """是否需要探测，以及下一次最早该看的时刻。

    节奏只由 last_probe_ts 推出，与上次结论无关 —— 结论决定「探完之后做什么」，
    不决定「下次还探不探」。刻意不读 next_probe_ts：那个字段由结论驱动，
    读它就把结论又变回了调速依据。
    """
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        # 锁定期间不探测：旧实现锁了还照探，探出的 quota 又进窗口，到期时
        # 互相续期（WG-21 锁了 17h，期间其实出过 7 次 OK）。
        if float(st.get("ban_hold_until") or 0.0) > now:
            return False, float(st["ban_hold_until"])
        ip = st.get("ip")
        if not ip:
            # 同上：返回**最后探测时刻**（没探过=0，最优先），否则所有无地址
            # 的线都返回 `now`，排序并列——并发槽位被固定的前 N 条占满。
            return True, float(st.get("last_probe_ts") or 0.0)
        # 隔离期内的线不参与探测排期：否则 prober 每轮都判它 due、spawn 完就
        # continue，把 CPU 烧成一个核（2026-10-28 实测 warp-prober 单线程 46%）。
        last_ok = float(st.get("last_ok_ts") or 0.0)
        if (st.get("last_ok_ip") == ip
                and now - last_ok <= _line_fresh_sec()):
            return False, last_ok + _line_fresh_sec()
        due = float(st.get("last_probe_ts") or 0.0) + _line_probe_cadence()
        if now < due:
            return False, due
        # **已到期时按「最后被探的时刻」排**，而不是返回同一个 `due=现在`。
        # 调用方 `due.sort(key=p[0])` 取前 `probe_concurrency` 条；cadence=0 时
        # 每条到期线的 due 都等于 `last_probe_ts`（过去），刚探完的反而更小，
        # 排序几乎恒定 —— 同一批线每轮被选中，尾部（GIG/LHR/SJC 等后加的
        # H2 线）几分钟到几十分钟一次也轮不上。实测 2026-11-05 13:56：
        # 35 条到期、并发 32，LHR-29/BOM-25/GIG-28 的 `last_probe_ts`
        # 停在 30/28/17 分钟前不动。用「越久没探越优先」即可让尾部候补进来。
        return True, float(st.get("last_probe_ts") or 0.0)


def _warp_line_record_observation(tag: str, ip: str | None, verdict: str,
                                  ts: float | None = None,
                                  started_at: float | None = None) -> bool:
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
        # 探测启动后这条线可能已被换线路径记为 OK（`_warp_line_note_ok`）。
        # 晚到的未知结论不能把更新的 OK 清掉，否则又回到「探不到就重探」。
        if (started_at is not None
                and float(st.get("last_ok_ts") or 0.0) > started_at):
            return False
        # 探测期间这条线可能已重启换了地址（`_warp_line_restart` 会把 ip_ts
        # 推到 now）。旧地址的结论对新地址无效：写回去等于把 st["ip"] 退回旧值，
        # 再按旧地址的惩罚期把整条线停探（quota 16h），新地址白等了。
        if (ip and started_at is not None
                and float(st.get("ip_ts") or 0.0) > started_at
                and st.get("ip") != ip):
            return False
        old_ip = st.get("ip")
        if ip:
            if old_ip and old_ip != ip:
                st["last_ok_ts"] = 0.0
                st["last_ok_ip"] = None
            st["ip"] = ip
            st["ip_ts"] = ts
            st["port_ok"] = True
            # 本进程内**曾经**拿到过出口。与 `ip` 不同，它不被隧道重启清空 ——
            # 「这条线在这台机器上到底有没有通过」是判断「重启还有没有意义」
            # 的依据（见 `_line_node_cannot_carry`）。
            st["ever_ip"] = True
        st["last_verdict"] = verdict
        # 这条结论属于哪个地址：闸口不再自己探测，改为读这里的结论
        # （见 _channel_verdict_snapshot）。没有这层对齐，线重启换了地址之后
        # 旧地址的判决会被当成新地址的判决用。
        st["last_verdict_ip"] = st.get("ip")
        st["last_probe_ts"] = ts
        if verdict == "OK":
            st["fail_streak"] = 0
            st["last_ok_ts"] = ts
            st["last_ok_ip"] = st.get("ip")
            st["next_probe_ts"] = ts + _line_fresh_sec()
        else:
            st["last_ok_ts"] = 0.0
            st["last_ok_ip"] = None
            # `fail_streak` 只统计 **拿不到出口地址**（unknown）。它的唯一消费点
            # 是 `_probe_one_line` 里「连续 N 次取不到出口地址 → 隧道半死」的
            # 重启判据，而 quota/blocked 说明隧道**完全正常**、只是这个地址被封。
            # 旧实现无条件累加，实测 WARP-WG-17 因此涨到 2457（它每次都拿到出口
            # 地址、每次都判 quota），日志写「连续 2457 次取不到出口地址」，
            # 与事实相反 —— 真故障反而被这堆噪声埋掉。
            if verdict == "unknown":
                st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
            else:
                # `port_closed` 也不进 fail_streak：那是进程没了（systemd 的活），
                # 不是「线不健康」。单元被删后仍留在 members 的线（实测
                # CF-C/G/H/I 2026-10-28 缩容时删了单元）原先会把它累加到 23 万，
                # 报表照着印 `unknown`，读起来像「现在探测失败」。
                st["fail_streak"] = 0
            # 非 OK 结论一律回到固定节奏：惩罚期与持久重探地板只说明
            # 「这个地址大概还不能用」，是观测的输入，不是停止观测的理由。
            # unknown（连出口 IP 都没拿到）单独用 _line_unknown_retry()：
            # cadence 已是 0，拿 0 当重试会变成「探不出结果就立刻再撞」的死循环。
            if verdict == "unknown":
                st["next_probe_ts"] = ts + _line_unknown_retry()
            else:
                st["next_probe_ts"] = ts + _line_probe_cadence()
        _WARP_LINE_CV.notify_all()
    _record_line_ip_drift(tag, old_ip, ip)
    _save_warp_line_state()
    return True


def _warp_line_note_ok(tag: str, ip: str, ts: float) -> None:
    """生产成功流量也是 OK 来源；只更新内存，避免每 5s 落盘。"""
    if not tag or not ip:
        return
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        old_ip = st.get("ip")
        if old_ip != ip:
            st["last_ok_ts"] = 0.0
            st["last_ok_ip"] = None
        st["ip"] = ip
        st["ip_ts"] = max(float(st.get("ip_ts") or 0.0), ts)
        st["last_ok_ts"] = max(float(st.get("last_ok_ts") or 0.0), ts)
        st["last_ok_ip"] = ip
        st["last_verdict"] = "OK"
        st["fail_streak"] = 0
        st["next_probe_ts"] = 0
        _WARP_LINE_CV.notify_all()
    _record_line_ip_drift(tag, old_ip, ip)


def _warp_line_invalidate(ip: str | None, now: float | None = None) -> None:
    """某地址被生产流量标脏：所有持有它的线立即失去 OK，等待重探。"""
    if not ip:
        return
    if now is None:
        now = time.time()
    with _WARP_LINE_CV:
        for st in _WARP_LINE_STATE.values():
            if st.get("ip") == ip or st.get("last_ok_ip") == ip:
                st["last_ok_ts"] = 0.0
                st["last_ok_ip"] = None
                if st.get("ip") == ip:
                    st["last_verdict"] = "blocked"
                    st["next_probe_ts"] = min(
                        float(st.get("next_probe_ts") or now),
                        now + _line_unknown_retry())
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
                # 节奏只由 last_probe_ts 推出（同 _warp_line_needs_probe），
                # next_probe_ts 可能存着旧的惩罚期时间，不能拿来决定何时醒。
                due = float(st.get("last_probe_ts") or 0.0) + _line_probe_cadence()
                if due <= now:
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


_GLOBAL_RESTART_HIST: list[float] = []
_GLOBAL_RESTART_LOCK = threading.Lock()
_GLOBAL_RESTART_LOG_TS = [0.0]


def _restart_rate_allow(now: float) -> bool:
    """全局重启节流：任何原因的重启都记一次。

    restart_max_per_hour_per_line 只管单条线，19 条线叠起来仍能到每分钟上百次
    （实测 2h05m 内 10028 次）。每重启一次就掐断该线在途连接，9router 侧表现为
    fetch failed / 502，所以全局限速是最后一道闸，与单线配额互不替代。
    """
    cap = int(_warp().get("restart_max_per_minute", 12))
    if cap <= 0:
        return True
    with _GLOBAL_RESTART_LOCK:
        _GLOBAL_RESTART_HIST[:] = [t for t in _GLOBAL_RESTART_HIST if now - t < 60.0]
        if len(_GLOBAL_RESTART_HIST) >= cap:
            if now - _GLOBAL_RESTART_LOG_TS[0] > 30.0:
                _GLOBAL_RESTART_LOG_TS[0] = now
                log(f"  · 全局重启节流：近 60s 已重启 {len(_GLOBAL_RESTART_HIST)} 次"
                    f"（上限 {cap}/分钟），跳过")
            return False
        _GLOBAL_RESTART_HIST.append(now)
        return True


def _warp_line_restart_allowed(st: dict, now: float) -> bool:
    """滑动一小时窗口内的重启配额。只拦重启，不拦探测。

    重启成功时 next_restart_ts 会被清零，重启失败时的退避也管不住成功路径，
    所以频率必须单独记账 —— 否则一次成功就换一次新地址，最快 5s 一轮。
    """
    cap = int(_warp().get("restart_max_per_hour_per_line", 20))
    hist = [t for t in (st.get("restart_hist") or [])
            if isinstance(t, (int, float)) and now - t < 3600]
    st["restart_hist"] = hist
    # cap<=0 = 不设上限。重启是「换掉一个确定不能用的地址」，压次数等于
    # 让烂地址继续占着线路；现在唯一的间隔来源是 restart_cooldown_sec（固定值）。
    if cap <= 0:
        return True
    return len(hist) < cap


def _penalty_restart_fuse(tag: str, now: float) -> bool:
    """「重启后仍不可用」累计到阈值就熔断；True 表示本次已触发熔断。

    2026-11-09 15:12 实测：WARP-H2-NRT-0/1/63 重启后拿到的是全新 /128，
    2 秒后模型层仍判 quota（Retry-After 17.3h），3 分钟内各重启 20+ 次。
    这说明这条线在当前落地节点上换不出配额 —— 重启只是每隔十几秒掐断
    一次在途连接，9router 侧全是 aborted。熔断后交给池内其他待命线。
    只管「重启换地址」这条路；tunnel_dead（连地址都拿不到）不在此列。

    调用点约定（2026-11-10 修正）：**先问 `_penalty_restart_fused` 再重启，
    重启真的发生了才调本函数计数**。反过来先计数会把被冷却期/判忙闸拦下的
    轮次也算进去，三次探测失败就能冻住一条本来正常的线。
    """
    fuse = max(2, int(_warp().get("restart_ip_stuck_fuse", 3)))
    hold = float(_warp().get("restart_ip_stuck_hold_sec", 900))
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        # 已在熔断期：直接告诉调用方「别重启」，但不再递增、不再刷日志 ——
        # 探测每 10s 一轮，若每轮都重新数到阈值再喊一次，一条线就能刷出
        # 几百行「熔断至」（实测 4 分钟 535 行）。
        if float(st.get("ip_stuck_until") or 0.0) > now:
            return True
        n = int(st.get("penalty_restart_streak") or 0) + 1
        st["penalty_restart_streak"] = n
        hit = n >= fuse
        if hit:
            st["penalty_restart_streak"] = 0
            st["ip_stuck_until"] = now + hold
    if hit:
        _save_warp_line_state()
        log(f"  ⛔ {tag} 连续 {n} 次「重启后仍不可用」，换地址换不出配额 → "
            f"熔断至 {time.strftime('%H:%M', time.localtime(now + hold))}，"
            f"改由池内其他线承担")
    return hit


def _penalty_restart_fused(tag: str, now: float) -> bool:
    """这条线此刻是否在「重启换地址换不出可用地址」的熔断期（只读，不计数）。

    WHY 必须与 `_penalty_restart_fuse` 分开：后者每调一次就 +1，而调用点原先
    都摆在**重启之前** —— 于是被冷却期（`next_restart_ts`）、判忙闸
    （`warp_line_busy`）、槽位限流拦下的那些轮次也各记了一次，三次探测失败
    （实际一次重启都没发生）就能把一条本来正常的线冻 900s。改成「先查熔断，
    重启真的发生了再计数」才对得上「连续 N 次重启后仍不可用」这个语义。
    """
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        return float(st.get("ip_stuck_until") or 0.0) > now


def _restart_outcome_note(tag: str, got_ip: bool, now: float,
                          reason: str) -> bool:
    """记一次隧道重启的结果；返回 True 表示「已给这条线踩下刹车」。

    判据是**重启这个动作有没有效果**，不是地址好不好：
      - 拿到出口地址 → 计数清零，说明重启是有用的动作；
      - 连续 N 次都拿不到任何出口地址 → 问题在节点或链路，不在地址。再重启只是
        每 5~15s 掐断一次在途连接。

    WHY 必须与 `_penalty_restart_fuse` 分开记：那个判的是「重启后地址仍被判
    quota/blocked」，属上游配额结论，2026-11-09 已按主人裁定禁用（夜里会把池子
    打空）。这个判的是纯粹的本机事实 —— 隧道起得来、端口通、却拿不到出口 ——
    跟配额无关，禁用配额熔断不该顺带把它一起关掉。

    WHY 只推迟 `next_restart_ts`、不写 `ip_stuck_until`、不碰 `stop_probe`：
    刹车不是判决。这条线照旧参与探测与选线，只是不再被打桩重启；等它自己恢复
    （或轮回把它换到别的节点）就继续工作。

    实测 2026-11-10 08:30 前后 12 分钟：263 次重启里 194 次以「到点无响应」
    收场，47 条扫描线的 `fail_streak` 涨到 188 —— 同一个死节点被反复重启，
    重启名额全被它们吃掉。
    """
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        if got_ip:
            st["futile_restart_streak"] = 0
            return False
        n = int(st.get("futile_restart_streak") or 0) + 1
        limit = max(2, int(_warp().get("restart_futile_limit", 6)))
        if n < limit:
            st["futile_restart_streak"] = n
            return False
        hold = float(_warp().get("restart_futile_hold_sec", 900))
        st["futile_restart_streak"] = 0
        st["next_restart_ts"] = max(float(st.get("next_restart_ts") or 0.0),
                                   now + hold)
    _save_warp_line_state()
    log(f"  ⏸ {tag} 连续 {limit} 次重启都没换出出口地址（{reason}）→ "
        f"暂停重启至 {time.strftime('%H:%M', time.localtime(now + hold))}，"
        f"交给轮回/换节点，探测照常")
    return True


def _warp_line_restart(member: dict, reason: str,
                       rebound: bool = False,
                       tunnel_dead: bool = False) -> bool:
    """统一重启入口：只有确定失败才走到这里，且 active 线永不重启。

    `rebound=True` 表示调用方**刚把这条线换到另一个落地节点**。此时夜间锁与
    渠道锁都必须让路：它们拦的是「为同一个被封地址池反复烧重启」，而换节点
    已经换掉了池子。不重启等于配置写进文件、进程还在跑旧节点 —— 线照样死，
    守护却以为自己救过它了（实测 2026-11-02：WG-22 换到 `~33` 后一直没重启）。

    `tunnel_dead=True` 是「连出口地址都拿不到」。渠道锁拦的是「换地址也买不到
    配额」，而拿不到地址是隧道本身坏了 —— 重启是唯一能修的动作。实测 19:43
    至今 532 次拦截里有 95 次是这一类，那批线整晚是死的。
    """
    tag = member["tag"]
    if tag == active_warp_tag():
        log(f"  · 当前出口 {tag} 不重启（失败时换线，{reason}）")
        return False
    now = time.time()
    # 「换地址熔断」：这条线连续几次重启后拿回的是同一个出口，说明它的落地
    # 节点只分配这一两个地址（WG 实测 104.28.205.115 / .117 来回跳，30 分钟
    # 重启 174 次），再重启也换不到新配额，只是每 8~10 秒掐断一次在途连接。
    # 熔断期内不重启，让它退到备位，流量由池里别的待命线承担。
    # 重启换 IP 的规则不变：**换得到**才继续重启。
    if not rebound and not tunnel_dead:
        stuck_until = float(_warp_line_state(tag).get("ip_stuck_until") or 0.0)
        if stuck_until > now:
            if now - _NIGHT_LOG_TS.get("stuck:" + tag, 0.0) > 300.0:
                _NIGHT_LOG_TS["stuck:" + tag] = now
                log(f"  · {tag} 换地址熔断中（至 "
                    f"{time.strftime('%H:%M', time.localtime(stuck_until))}），"
                    f"跳过重启（{reason}）")
            return False
    if not rebound and _night_block_restart(tag, reason, now):
        return False
    # 排空闸（2026-11-11）：刚被换下的线必须按字节流事实判空才允许回收。
    # 放在额度/夜间/渠道锁之后（策略层拒绝优先于排空），warming=True 之前
    # （拒绝时绝不置 warming，否则这条线会被所有探测路径永久跳过）。不放
    # 进 CV 锁：闸内 _lines_in_use / _warp_line_stats 在启动窗口含 HTTP。
    allow, note = _drain_gate(member, now, reason)
    if not allow:
        try:
            with _WARP_LINE_CV:
                _warp_line_state_locked(tag)["next_probe_ts"] = (
                    now + float(_warp().get("drain_poll_sec", 5)))
        except Exception:
            pass
        if now - _NIGHT_LOG_TS.get("drain:" + tag, 0.0) > 300.0:
            _NIGHT_LOG_TS["drain:" + tag] = now
            log("  · %s 暂不回收：%s（%s）" % (tag, note, reason))
        return False
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        if st.get("warming") or now < float(st.get("next_restart_ts") or 0.0):
            return False
        ban_hold = float(st.get("ban_hold_until") or 0.0)
        # 开关是总闸：关掉之后已写入的锁也必须立刻失效，否则回滚还得手改
        # `.warp_line_state.json`。
        if ban_hold > now and not rebound and _warp().get(
                "channel_ban_hold_enabled", True) and not tunnel_dead:
            # 渠道锁：当天实测全 quota 或连续 5h 无出口，重启只会换到同一 colo
            # 的另一个已烧地址。按 tag 节流日志，否则每条线每次探测都会刷一行。
            if now - _NIGHT_LOG_TS.get("ban:" + tag, 0.0) > 300.0:
                _NIGHT_LOG_TS["ban:" + tag] = now
                log(f"  · {tag} 渠道锁定中（至 "
                    f"{time.strftime('%m-%d %H:%M', time.localtime(ban_hold))}），"
                    f"跳过重启（{reason}）")
            return False
        if not _warp_line_restart_allowed(st, now) and not rebound:
            log(f"  · {tag} 本小时重启已达上限，跳过重启（{reason}）")
            return False
        # 档位停用的线在这里就短路，别落到 `_restart_warp_line` 再被拦 ——
        # 那条路会先打「尝试重启」再打「重启后未就绪」，档位正常工作时刷成
        # 故障样。按 tag 节流 5 分钟，与其他锁的日志口径一致。
        if not _line_enabled(member):
            if now - _NIGHT_LOG_TS.get("tier:" + tag, 0.0) > 300.0:
                _NIGHT_LOG_TS["tier:" + tag] = now
                log(f"  · {tag} 档位停用中，跳过重启（{reason}）")
            return False
        st["warming"] = True
    got_slot = False
    try:
        busy = warp_line_busy(tag, _ERROR_PATH_BUSY_WINDOW_SEC)
        # `tunnel_dead` 不判忙：这条线连出口地址都拿不到，挂在它上面的连接
        # 本来就出不去，为它们保留隧道等于让线一直死着。`warp_line_busy`
        # 查不到时返回 None（未知），未知也不该成为「永不重启」的理由 ——
        # 实测 2026-11-10 06:39 这条线因此整晚没被救回来。
        if busy is not False and not tunnel_dead:
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
    log(f"↻ 尝试重启 {tag}（{reason}）")
    # 计数落在真正调用 systemctl 之前：重启失败同样掐断过在途连接，
    # 只统计成功会让「重启后未就绪」这条路径绕过上限。
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        st["restart_hist"] = ([t for t in (st.get("restart_hist") or [])
                               if isinstance(t, (int, float))
                               and now - t < 3600] + [now])
    prev_ip = _warp_line_state(tag).get("ip")
    try:
        _invalidate_warp_line_ip(tag)
        ok = (_restart_warp_line(member)
              and _wait_port_ready(member["port"],
                                   _warp()["ready_timeout_sec"]))
        if not ok:
            with _WARP_LINE_CV:
                st = _warp_line_state_locked(tag)
                st["warming"] = False
                st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
                st["next_restart_ts"] = time.time() + float(
                    _warp().get("restart_cooldown_sec", 5))
                st["next_probe_ts"] = st["next_restart_ts"]
            log(f"  ✗ {tag} 重启后未就绪（{reason}）")
            _restart_outcome_note(tag, False, time.time(), reason)
            return False
        ip = _probe_refresh_egress(member["port"])
        now = time.time()
        stuck_until_new = 0.0
        # 重启后的地址若在台账里仍处惩罚期，停探到确切解禁：这条地址已经
        # 被判脏，再探只是把同一结论再买一次。
        block_left = _line_block_remaining(ip, now) if ip else 0.0
        with _WARP_LINE_CV:
            st = _warp_line_state_locked(tag)
            st["warming"] = False
            st["restart_ts"] = now
            # 冷却对成功路径同样有效：清零等于「重启成功就能立刻再重启」，
            # 而 restart_max_per_hour_per_line 已设为 0（不设上限），
            # 此时 restart_cooldown_sec 是唯一的间隔来源 —— 清零就一个都没有。
            st["next_restart_ts"] = now + float(
                _warp().get("restart_cooldown_sec", 5))
            st["last_ok_ts"] = 0.0
            st["last_ok_ip"] = None
            # 重启的目的是换一个能用地址，所以必须马上知道新地址的结果。
            # 停探只保留「同一地址刚从台账读到惩罚期」这一种：那不是猜测，
            # 是台账里的确定事实，探它等于把同一结论再买一次。
            if block_left > 0:
                st["next_probe_ts"] = max(
                    float(st.get("next_probe_ts") or 0.0), now + block_left)
            else:
                # **不许在这里写 `last_verdict`**（a377b4b 引入、2cd6845 留下的
                # 老写法 `st["last_verdict"] = "unknown"`）。`unknown` 的语义是
                # 「探测跑完了但没拿到出口 IP」，而这条路径刚刚拿到了地址，只是
                # 还没做模型层探测 —— 用探测失败的词覆盖真实结论是错的词。
                # 后果是实质性的：重启是 5~15s 一次的高频动作，字段被反复刷成
                # `unknown`，`asip chan` 把它当「当前状态」印出来，于是 33/78 条
                # 线看起来全是 unknown，读起来像「根本没在探测」。实测 2026-11-05
                # 16:40：20 秒内 42 条线的 `last_probe_ts` 全在推进，30 分钟
                # 1459 条模型层结论 —— 探测一直在跑，坏的是这个字段。
                # 保留上一条真实结论；「地址是新的、还没探过」由 `last_ok_ts == 0`
                # 加 `ip_ts > last_probe_ts` 表达，报表那边已有 `无地址` 口径。
                st["next_probe_ts"] = now + _line_unknown_retry()
            old_ip = st.get("ip")
            if ip:
                st["ip"] = ip
                st["ip_ts"] = now
                st["port_ok"] = True
                st.pop("egress_error", None)
                # 记「每次重启换回来的地址」。连续拿回同一个地址 = 这条线的
                # 落地节点只发这一个出口，重启换 IP 对它无效，必须熔断 ——
                # 否则就是每 8~10 秒重启一次、每次掐断在途连接的死循环。
                fuse = max(2, int(_warp().get("restart_ip_stuck_fuse", 3)))
                hist = [x for x in (st.get("restart_ip_hist") or [])
                        if isinstance(x, str)]
                hist.append(ip)
                st["restart_ip_hist"] = hist[-fuse:]
                if (len(st["restart_ip_hist"]) >= fuse
                        and len(set(st["restart_ip_hist"])) == 1):
                    stuck_until_new = now + float(
                        _warp().get("restart_ip_stuck_hold_sec", 900))
                    st["ip_stuck_until"] = stuck_until_new
            else:
                # 取不到地址时不推进历史：那不是「换地址失败」，是隧道坏了。
                hist = [x for x in (st.get("restart_ip_hist") or [])
                        if isinstance(x, str)]
                st["restart_ip_hist"] = hist[-(max(2, int(_warp().get(
                    "restart_ip_stuck_fuse", 3))) - 1):] or []
                # 落盘失败原因，让 `asip chan` 能显示「为什么拿不到出口」，
                # 而不是只印一个 `无地址`。值是人话，已过 `_EGRESS_HINT`。
                st["egress_error"] = _egress_error_hint(member["port"]) or "unknown"
            _WARP_LINE_CV.notify_all()
        _record_line_ip_drift(tag, old_ip, ip)
        if stuck_until_new:
            log(f"  ⛔ {tag} 连续 {fuse} 次重启后出口仍是 {ip}，"
                f"重启换不到新地址 → 熔断至 "
                f"{time.strftime('%H:%M', time.localtime(stuck_until_new))}，"
                f"改由池内其他线承担")
        _save_warp_line_state()
        # 失败要带**原因**：`探测失败` 四个字把「端口没监听」「节点域名解析不了」
        # 「隧道通了但没走 WARP」「到点没响应」压成同一句话，四种故障在报表上
        # 一模一样，只能靠人翻上去看上下文。原因来自 egress.py 的 _CURL_RC。
        why = ""
        if not ip:
            why = _egress_error_hint(member["port"])
        log(f"  {'✓' if ip else '✗'} {tag} 重启后出口="
            f"{ip or ('探测失败 / ' + why if why else '探测失败')}")
        _restart_outcome_note(tag, bool(ip), time.time(), reason)
        return bool(ip)
    except Exception as e:
        with _WARP_LINE_CV:
            st = _warp_line_state_locked(tag)
            st["warming"] = False
            st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
            st["next_restart_ts"] = time.time() + float(
                _warp().get("restart_cooldown_sec", 5))
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
    if st.get("warming") or st.get("stop_probe"):
        return
    active = active_warp_tag()
    if (tag == active and st.get("ip")
            and now - float(st.get("ip_ts") or 0.0)
            < float(_warp().get("ip_observe_sec", 120))
            and _warp_line_ok_fresh(tag, now)):
        return
    started_at = time.time()
    # 端口没监听就别花 20 秒去等一个不可能来的响应。实测 2026-11-05 本机：
    # WARP-CF-C(1082) / WARP-CF-G/H/I(1086-1088) / WARP-WG-23(1132) 的单元
    # 早就没了，每条仍在每轮探测里白等满 20s 超时——41 条线里 5 条这么烧，
    # 一轮并发预算被吃掉四分之一。`_port_open` 本地 connect <1ms。
    if not _port_open(member["port"]):
        _channel_noaddr_note(tag, False, time.time())
        # 结论记 `port_closed` 而**不是** `unknown`：进程没了是 systemd 的活，
        # daemon 重启等于抢它的活。也不进 `fail_streak`（见
        # `_warp_line_record_observation`），否则报表上会永远挂着一句
        # 几天前的「unknown」。
        _warp_line_record_observation(tag, None, "port_closed",
                                      time.time(), started_at)
        return
    ip = warp_egress_ip(member["port"])
    if ip:
        # 落点台账跟着生产流量自更新：这条线绑的是哪个落地节点、它实际出口
        # 在哪个 colo —— 一次成功探测就够了，不需要单独的 survey。
        # 台账的键是**落地节点**（`wg_colo_map.json` 的 nodes 表按节点 tag
        # 索引，与 `survey()` / `load_colo_map()` 同一口径），不是线路 tag。
        # trace 由 `warp_egress_ip` 顺手存在 `_LAST_EGRESS_TRACE` 里；
        # 端口对不上说明那次结果不是本次探测的，宁可不记。
        tr = _LAST_EGRESS_TRACE or {}
        node = _line_node(tag) if tr.get("port") == int(member["port"]) else None
        if node:
            _record_line_colo(str(node), tr.get("colo"), tr.get("loc"), ip)
    if not ip:
        _channel_noaddr_note(tag, False, time.time())
        _warp_line_record_observation(tag, None, "unknown", time.time(), started_at)
        streak = int(_warp_line_state(tag).get("fail_streak") or 0)
        # H2 线先换节点：落地节点在共享 sing-box 里，`_rehome_one` 走 selector，
        # 换完不重启任何进程。连续 3 次拿不到出口就换 —— 重启 usque 换的是同一个
        # 节点上的新会话，节点不转发 TCP 到 CF 时重启多少次都没用。
        if (tag != active and member.get("kind") == "h2" and streak >= 3
                and _h2_repair(member)):
            return
        if (tag != active and streak >= _UNKNOWN_RESTART_STREAK
                and _port_open(member["port"])):
            # 节点根本不转发 UDP 时，重启只是把同一个节点再拉一遍；更糟的是
            # 每次重启都刷新 `restart_ts`，把同块轮回的冷静期（2×60s）永远
            # 顶住 —— 轮回一次也轮不到它，线就焊死在这个节点上。实测
            # 2026-11-05 WG-4 `fail_streak` 41、每 ~60s 重启一次、
            # `ever_ip` 始终 False，而轮回日志里从没出现过它。
            # 停掉重启，换节点交给轮回（它换完自己会重启）。
            if _line_node_cannot_carry(tag):
                return
            _warp_line_restart(member, f"连续 {streak} 次取不到出口地址",
                               tunnel_dead=True)
        return
    # 拿到地址就清零「连续无地址」计时 —— 必须放在下面所有早退分支之前，
    # 否则一条刚恢复的线走「OK 新鲜」早退时计时不清，之后每次失败都从旧起点
    # 算起，5 小时那条判据会被一次历史故障带偏。
    _channel_noaddr_note(tag, True, now)
    # 这条链路能出地址了：H2 的候选池从头开始记（见 `_h2_next_node`）。
    if member.get("kind") == "h2":
        _H2_TRIED.pop(tag, None)
        _H2_POOL_DEAD.pop(tag, None)
    cur = _warp_line_state(tag)
    if tag != active and _warp_line_ok_fresh(tag, now) and cur.get("ip") == ip:
        return
    if _warp_ip_is_blocked(ip, now):
        _warp_line_record_observation(tag, ip, "blocked", time.time(), started_at)
        # 这条路径**不写** `ban_probes`（那是渠道锁的判据样本，blocked 混进去
        # 会让线永远锁不上），但必须留下痕迹：早退分支根本走不到模型层实测，
        # 于是状态是「判了 blocked、`last_verdict` 写着 blocked、样本却是空的」。
        # 报表看到零样本就会印「无样本」，把「这个地址在惩罚期、换地址就好」
        # 读成「什么都没发生过」。见 `_channel_blocked_note` 的 WHY。
        _channel_blocked_note(tag, now)
        if tag == active:
            _reconnect_warp_now("探测发现当前出口处于惩罚期", time.time())
        elif _line_node_cannot_carry(tag):
            # **节点本身不转发 UDP 时，重启同一个节点是死循环**：惩罚期重启
            # 换的是这个节点下的新 /128，节点要是不通 2408，换多少次都拿不到
            # 出口。实测 2026-11-05 WG-4 停在 rank 1 节点上每 8~10s 重启一次、
            # 日志刷满「出口处于惩罚期」，而 `_same_block_standby` 明明能给它
            # 选出备线 —— 这条路径根本没问过节点能不能承载。
            # 换节点交给同块轮回（它有自己的两轮判死节流），这里只是别重启。
            return
        else:
            # 惩罚期重启 = 换出口地址 = 新 IP 的新配额（2026-11-09 主人裁定：
            # 不同 IP 的额度不一样）。但「换得到」才有意义：连续几次换回来
            # 的地址照样判 quota，说明这条线在当前节点上换不出配额，重启就
            # 只剩掐断在途连接这一个副作用 —— 熔断它，让池里其他线承担。
            if _penalty_restart_fused(tag, now):
                return
            if _warp_line_restart(member, "出口处于惩罚期"):
                _penalty_restart_fuse(tag, now)
        return
    if _warp_probe_blind(now):
        _warp_line_record_observation(tag, ip, "blind", time.time(), started_at)
        return
    if not _probe_rate_allow():
        # 全局速率闸是瞬时的资源限制，不是结论。不改 last_probe_ts
        # （那等于谎报「探过了」），让下一轮重新尝试。
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
                                         time.time(), started_at):
        return
    # 探测耗时落渠道级：闸口不再自己打探测（见 _channel_verdict_snapshot），
    # 选槽排序的延迟判据从这里取。带地址对齐，换出口即失效。
    _warp_line_update(tag, last_probe_sec=round(time.time() - started_at, 3),
                      last_probe_sec_ip=ip, last_probe_sec_ts=time.time())
    # 渠道锁的样本只认地址级持久结论（quota/flagged），与台账同一口径。
    # OK 也入账：它是「这条渠道还能出 OK」的唯一证据，而当天样本只要出现一条
    # 非 quota 就不再锁（`_channel_ban_hold_hit`）。
    # blocked 必须入账：它是「这个地址已被判惩罚期」，与 quota 同属地址级结论。
    # 漏掉它时 CF 线（结论几乎全是 blocked）的 ban_probes 永远为空，报表印
    # 「无样本」而 last_verdict 仍是上一次的 quota —— 就是主人说的「原因标错」。
    if verdict == "OK" or verdict in _WARP_PROBE_PERSISTENT or verdict == "blocked":
        _channel_ban_note(tag, verdict, time.time())
    if verdict == "OK":
        # 真拿到 OK 就把熔断与计数一起清掉：线已经好了，没有理由继续压着它。
        with _WARP_LINE_CV:
            st = _warp_line_state_locked(tag)
            if st.get("penalty_restart_streak") or st.get("ip_stuck_until"):
                st["penalty_restart_streak"] = 0
                st.pop("ip_stuck_until", None)
        with _DRAIN_LOCK:
            _BAD_ON_SLOT.pop(tag, None)
        return
    if tag == active:
        _reconnect_warp_now(f"探测失败 {verdict}", time.time())
    else:
        # quota(429) 也重启：换出口地址 = 换一个有独立配额的新 IP
        # （2026-11-09 主人裁定：quota 一律按出口 IP 处理，见 INCIDENTS.md INC-012 更正）。
        # 但和 blocked 路径一样要问「换得到吗」：实测 2026-11-10 07:00 之后
        # 3 分钟 272 次重启、池内 152 条线**一条都没熔断**，重启换回来的全新
        # /128 首探照样 quota —— WARP 段被整体烧穿时，重启换不到配额，只换来
        # 每 30s 掐断一次在途连接（9router 侧表现为 503 / 字节流中断）。
        # 同口径处理：连续 N 次「重启后仍不可用」就熔断 900s，交给池里其他线。
        # active 线不走这里（上面 `_reconnect_warp_now` 负责），所以生产出口
        # 不会被熔断冻住。
        if _penalty_restart_fused(tag, now):
            return
        if _warp_line_restart(member, f"模型层 {verdict}"):
            _penalty_restart_fuse(tag, now)


# 单元是否在跑的缓存：{单元名: (是否在跑, 检查时刻)}。
# `systemctl is-active` 每次约 5~15ms，而 prober 每秒遍历全部成员，直接调会把
# 主循环拖慢；缓存 60s 足够 —— 单元被启停是人工操作，不需要秒级感知。
_UNIT_RUNNING_CACHE: dict[str, tuple[bool, float]] = {}
_UNIT_RUNNING_TTL = 60.0


def _warp_member_unit_running(member: dict, now: float) -> bool:
    """该成员对应的 systemd 单元此刻是否在跑。

    WHY 要判这个：`config.py` 的 members 是**声明**，不等于**在跑**。
    实测 usque-warp-c/g/h/i 四条是 disabled（2026-10-28 缩容时把设备让给了
    H2），但 members 里仍留着它们。prober 照样每秒探一次，每次都探不到出口，
    `fail_streak` 累积到 14 万~22 万，日志刷「连续 223495 次取不到出口地址」，
    把真正的故障埋掉。

    H2 线同理：`usque-h2-*.service` 没装时探它只是白烧一次 socks 连接。
    """
    svc = member.get("service")
    if not svc:
        return True                      # 没有单元概念（理论上不该出现），照旧探
    hit = _UNIT_RUNNING_CACHE.get(svc)
    if hit and now - hit[1] < _UNIT_RUNNING_TTL:
        return hit[0]
    try:
        r = subprocess.run(["systemctl", "is-active", svc],
                           capture_output=True, text=True, timeout=5)
        running = r.stdout.strip() == "active"
    except (OSError, subprocess.TimeoutExpired):
        running = True                   # 查不到就按在跑处理，不能因为查询失败停探
    _UNIT_RUNNING_CACHE[svc] = (running, now)
    return running


# 端口 → (tag, service)。探测目标 = 声明成员 ∪ 这些正在监听的隧道。
# WHY 不能只看 members：实测 2026-11-05 远端 `usque-h2-25/26/28/29/30`
# **五个单元在跑**，但它们的 tag 不在 `channels.json` 里，于是这五条隧道
# 从来没被探测过 —— 报表上表现为「有线但永远没有结论」，而 daemon 日志里
# 一次都不出现它们的名字。声明是渲染产物，单元是事实；探测要跟事实走。
_TUNNEL_SCAN_PORTS: dict[int, tuple[str, str]] = {}
_TUNNEL_SCAN_TS = 0.0
_TUNNEL_SCAN_TTL = 60.0


def _scan_running_tunnels(now: float) -> dict[int, tuple[str, str]]:
    """扫 systemd 里所有在跑的隧道单元，返回 {socks_port: (tag, service)}。

    端口由单元名推出，与 `config.py` 的端口契约一致：
      usque-h2-<N>  → tag `WARP-H2-<COLO>-<N>` 从 channels.json 取，取不到就用
                      `WARP-H2-SCAN-<N>`；端口 1180+N
      warp-wg-<N>   → tag `WARP-WG-<N>`；端口 1110+(N-1)（见 config 的 WG 段）
      usque-warp-<x>→ 端口来自 CFG 声明的成员，扫不到就跳过

    **不复用 `_warp_member_unit_running` 的缓存键**：那个按 service 缓存单条
    结论，这里是批量扫一遍。60s 一次，成本可忽略。
    """
    global _TUNNEL_SCAN_PORTS, _TUNNEL_SCAN_TS
    if _TUNNEL_SCAN_PORTS and now - _TUNNEL_SCAN_TS < _TUNNEL_SCAN_TTL:
        return _TUNNEL_SCAN_PORTS
    declared = {m.get("service"): m for m in _warp()["members"] if m.get("service")}
    found: dict[int, tuple[str, str]] = {}
    try:
        r = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=active",
             "--no-legend", "--plain",
             "usque-h2-*.service", "warp-wg-*.service", "usque-warp-*.service"],
            capture_output=True, text=True, timeout=10)
        names = [ln.split()[0] for ln in (r.stdout or "").splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return _TUNNEL_SCAN_PORTS              # 查不到就沿用上一轮，别把探测停掉
    for svc in names:
        m = declared.get(svc)
        if m:
            found[int(m["port"])] = (m["tag"], svc)
            continue
        # 未声明的单元：按名字推端口与 tag。端口契约见 config.py 的注释。
        import re
        hit = re.fullmatch(r"usque-h2-(\d+)\.service", svc)
        if hit:
            idx = int(hit.group(1))
            found[1180 + idx] = (f"WARP-H2-SCAN-{idx}", svc)
            continue
        hit = re.fullmatch(r"warp-wg-(\d+)\.service", svc)
        if hit:
            found[1110 + int(hit.group(1)) - 1] = (f"WARP-WG-{hit.group(1)}", svc)
    _TUNNEL_SCAN_PORTS, _TUNNEL_SCAN_TS = found, now
    return found


def _probe_targets(now: float) -> list[dict]:
    """本轮该考虑探测的成员：**声明成员 ∪ 正在跑的隧道**。

    声明（`members`）是渲染产物，会落后于现场：实测 2026-11-05 远端
    `usque-h2-25/26/28/29/30` 五个单元在跑，tag 却不在 `channels.json` 里，
    于是五条隧道从未被探测 —— 有流量、有日志，就是没有结论。**单元在跑就
    必须探测**，tag 归属是记账问题，不是探测前提。

    未声明的单元用 `WARP-H2-SCAN-<idx>` / `WARP-WG-<n>` 作为 tag：状态表
    需要按 tag 索引，而扫描到的单元没有渲染产物里的名字。它们照样走完整
    探测流程（拿地址、判 quota/blocked、写状态），只是**不进换线候选**
    （`_warp_member` 查不到它们，selector 里也没有）。

    手动配置照旧有效：`members` 里的线永远在列表里，不受扫描影响
    （`_scan_running_tunnels` 只补，不删）。
    """
    out: dict[str, dict] = {}
    for m in _warp()["members"]:
        out[m["tag"]] = m
    declared_ports = {int(m["port"]) for m in _warp()["members"] if m.get("port")}
    for port, (tag, svc) in _scan_running_tunnels(now).items():
        if tag in out or port in declared_ports:
            continue
        out[tag] = {"tag": tag, "port": port, "service": svc,
                    "scan_only": True}
    return list(out.values())


def _warp_prober_loop() -> None:
    while True:
        now = time.time()
        active = active_warp_tag()
        due: list[tuple[float, dict]] = []
        for m in _probe_targets(now):
            tag = m["tag"]
            # 档位外的线不探：单元可能还在跑（改档位没重启、reconcile 漏停），
            # 单元判据看不出来；用档位判据兜一道，别让停用线继续烧探测与日志。
            if not _line_enabled(m):
                continue
            if not _warp_member_unit_running(m, now):
                continue
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
    ok_ttl = float(_warp().get("probe_ok_ttl_sec", 1800))
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
        # 时刻），**从未用过要显式放行**：不能靠 `now - 0 > 窗口` 兜，那只在
        # 真实墙钟成立。用例用合成时钟（now=1000）时 `1000-0 < 1800` 会把从没
        # 用过的地址判成「刚用过」——`probe_ok_ttl_sec` 从 600 抬到 1800
        # （T-225）后 5 个缓存用例一起转红，就是这条隐式假设破的。
        used_ts = _warp_ip_last_ts(ip)
        if used_ts and now - used_ts < ok_ttl:
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
    """把台账里的时间字段解析成 float；损坏/NaN/Inf 一律当缺失。

    `OverflowError` 必须一起接住：`json.loads` 会把超长整数字面量解析成
    Python int，`float(10**400)` 抛的不是 ValueError，于是会穿出 `/rotate`
    的解析段、整条换线请求被丢弃（回 500 → 客户端当「没换线」）。
    """
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
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
    地址降级为**档 2（次级优质）**，可以复用而不是永久排除 —— 这正是它重新
    进入候选池的路径。未被标记的地址沿用调用方给的 window。
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
    # 档 1（探针刚 OK）与档 2（撑过生产）在此函数里不做区分，它只看 last_ok_ts。
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
    # 只有档 3（出现过却连一次 OK 都没拿到）该刷。档 1（探针刚 OK）与档 2
    # （撑过生产流量）都不是低质 —— 刷它们等于把好不容易验出/服务过的地址丢掉。
    # WHY 写 == 3：加上探针档后档位从三档变四档，写 == 2 会把「验过未用」
    # 当成脏线反复重启，正好把机制一变成反向优化。
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
        global _ACTIVE_WARP_IP
        tag = active_warp_tag()
        ip = _probe_active_ip(tag) if tag else None
        if ip:
            _ACTIVE_WARP_IP = ip
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
        global _ACTIVE_WARP_IP
        tag = active_warp_tag()
        if tag is None:
            # 重启后落在池里（或还没恢复线路），旧 IP 不属于任何出口。
            if _current_mode()[0] == "proxy":
                _ACTIVE_WARP_IP = None
            return
        ip = _probe_active_ip(tag)
        if ip:
            _ACTIVE_WARP_IP = ip

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


# maxsize 必须大于台账条目数：`_warp_prefix_rest` 每算一个候选就要把整张台账
# 的 IP 各解析一遍，缓存装不下就等于没有缓存。台账实测 14842 条而原来只给 8192，
# 每轮扫描都把缓存整个冲掉 —— py-spy 实测单次 126ms、12 个候选合计 1.35s，
# 占换线请求线程 28/36 的样本，且全程握着 `_WARP_IP_LOCK`，把并发请求一起拖慢。
# 放到 65536 后同一基准 12 次合计 10ms。台账按 24h 窗口自然淘汰，条目数有上界。
@functools.lru_cache(maxsize=65536)
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
      1. **探针刚验过 OK** —— 空跑实测此刻确实能服务。排在「撑过生产流量」
         之前：探测器每条线都在跑，这个结论是**此刻**的；而 `last_ok_ts` 只由
         当前出口挣得、且每次重启清零，它奖励的是「一直没被换下」而不是
         「现在还能用」。档内按「距上次探针 OK 的时长」排，**越新越优先** ——
         刚刷出来的 OK 说明这个地址这一刻还没被烧，压着它是等它过期；
      2. 次级优质 —— **真的撑过生产流量**，且已过对应的惩罚期（403 六小时 /
         429 默认六十秒；解析到精确 Retry-After/body/message 时以 ban_until
         为准，上限 24h）。过了惩罚期的 IP 可以复用，不是废址；档内同样越新
         越优先。没有新鲜的探针结论时，它仍是有过真实成功的那一批；
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
    probe_ok = entry.get("probe_ok_ts") if isinstance(entry, dict) else None
    if probe_ok:
        # 越新越优先：取正，调用方用 min()，刚验出来的 OK 排序值最小。
        return (1, hot, now - float(probe_ok))
    ok = entry.get("last_ok_ts") if isinstance(entry, dict) else None
    if ok:
        # 同样越新越优先。
        return (2, hot, now - float(ok))
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
    hit = _probe_cache_lookup(tag, ip)
    cached = hit[0] if hit else None
    # TTL 内的 OK 直接复用、不再实测（省配额）。
    #
    # 这里原来是**禁止**复用的：地址被生产流量烧掉只要几秒，踩着 600s 的旧 OK
    # 切上去，实测每轮换线后的 429 爆发都落在这种线上。主人 2026-10-28 明确说
    # 「600 秒的 OK 会让已被烧掉的地址被切上去，这个是可以接受的」—— 权衡是
    # 省配额优先，代价是复用被烧地址的概率。要收回来就删掉这一段。
    if cached == "OK":
        _PROBE_LAST_VERDICT[tag] = cached
        # 结论可能来自另一条线（同址跨线复用），日志必须说清来源，
        # 否则排障时会把「别的线验过」当成「这条线验过」。
        log(f"  · {tag} 出口 {ip} TTL 内已有 OK"
            f"（{int(time.time() - hit[1])}s 前落账，跳过实测）"
            f"{_warp_refresh_count_note(tag)}")
        return True
    # 负结论：地址已被上游拒绝且封禁仍在生效。
    # 也要看 ban_until：上游给过 Retry-After 时，它可能比缓存 TTL 先到期；
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
    # 并发重复：同一地址挂在两条线上时，两条线会各自起探（缓存里还没有结论）。
    # 实测 104.28.222.43 同时挂在 WG-1 与 WG-9。这里先只计数，量出来再决定跳过 ——
    # 跳过会改变「这条线本轮有没有结论」的语义，不能顺手改。
    with _WARP_LINE_CV:
        for _t2, _ip2 in list(_PROBING_IP.items()):
            if _ip2 == ip and _t2 != tag:
                _PROBE_STATS["inflight_dup"] += 1
                break
        _PROBING_IP[tag] = ip
    try:
        import probe_warp
        try:
            verdict, _sec, _detail, retry_after = probe_warp.probe(
                member["port"], w.get("refresh_model_timeout_sec", 30))
        finally:
            # 必须 finally：探测抛异常时也要把登记清掉，否则这条 tag 的
            # `_PROBING_IP` 永远留着旧地址，之后同址的别的线会被误记成并发重复。
            with _WARP_LINE_CV:
                _PROBING_IP.pop(tag, None)
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
    if verdict == "noprobe":
        # 没装 bun：探测通道不存在，不是这条线不能用。当作失明处理 —— 不否决、
        # 不写台账（写进去会把每条线都标成 unreachable，下次换线直接没候选）。
        log(f"  ⚠ {tag} 出口 {ip} 无探测器（未装 bun），跳过实测改用历史判据")
        return True
    _PROBE_LAST_VERDICT[tag] = verdict
    ok = verdict == "OK"
    # 只有 quota(429) 的 Retry-After 会写进惩罚期；403 带限流头不算。
    # Retry-After 不再触发任何全局冻结：配额按出口 IP 计，换地址即新配额
    # （2026-11-09 主人裁定，详见 INCIDENTS.md INC-012 更正段）。
    retry_after = retry_after if verdict == "quota" else None
    hint = (f"（Retry-After {retry_after / 3600:.1f}h）"
            if retry_after and retry_after > 0 else "")
    log(f"  {'✓' if ok else '✗'} {tag} 出口 {ip} 模型层: {verdict}{hint}"
        f"{_warp_refresh_count_note(tag)}")
    if ok:
        # 实测通过 = 这条地址此刻确实能服务，记进 `probe_ok_ts`。这是选线的
        # **档 1**（见 `_warp_ip_rank`）—— 探测器每条线都在跑，所以它是唯一
        # 「此刻」级别的正面证据；不记则同址停在档 3，后台把刚验过的线当低质
        # 反复重启，白烧配额。
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
        # 夜间锁在进入重试循环前短路：否则 3 次尝试 × 退避全部空转，日志还
        # 会写成「端口未就绪」，把运维眼里的「被锁」误报成「线路故障」。
        if _night_block_restart(member["tag"], reason, time.time()):
            log(f"  · {member['tag']} 夜间锁生效，跳过刷新（{reason}）")
            return None
        _invalidate_warp_line_ip(member["tag"])  # 重启后旧地址必然失效
        try:
            # v4 第一次重启就换节点：v4 出口只由落地 colo 决定，重启同一个
            # 节点拿回的是同一个地址池（实测在 .211/.243 间跳），重启等于白做。
            # v6 相反 —— 每次重连地址就变（连 /64 都变），换节点没有必要，
            # 只在反复失败后才换。
            ok = (_restart_warp_line(
                      member,
                      rebind=(member.get("family") == "v4"
                              or attempt > 1))
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


# ---- 沉睡池体检调度（并入 daemon 主循环，不另开 systemd unit / cron）----
# WHY（2026-11-11 实测事故，见 DESIGN-2026-11-11.md 第 12 部分）：
#   dormant_probe.py 是 `.dormant_state.json` 的唯一生产者，
#   parse_subscription._load_dormant_state() 是唯一消费者，但部署里从来
#   没有任何调用方（无 timer / 无 cron / 部署脚本也没接）。状态文件从未
#   生成过，于是每轮配置刷新都打印"沉睡池状态读不到，本轮全部节点进池子"，
#   判死的节点反复回流到 selector —— 整套降级机制从部署起就没生效过。
#   消费者在 daemon 的配置刷新链路里，所以生产者也挂在同一条链路上，
#   不需要第二个服务；daemon 不在线时配置也不会刷新，跑体检没有意义。
# 不引入第二套配置：周期、单轮预算、续跑退出码全部直接取脚本的常量。
# dormant_probe 顶层只有常量与函数定义，import 它不会起进程、不会读配置。
from dormant_probe import POOL_RECHECK_HOURS, DEFAULT_MAX_SECONDS, RC_MORE

_DORMANT_PROBE_RUNNING = False
_DORMANT_PROBE_NEXT_TS = 0.0
_DORMANT_PROBE_PERIOD_SEC = POOL_RECHECK_HOURS * 3600
# 主人 2026-11-11 定标：探测不等待、禁止设置等待时间。失败与「未完待续」
# 一律立即重试（NEXT_TS=0），不设任何退避窗口；防刷屏靠日志节流与脚本
# 自身的运行耗时，不靠 sleep。
_DORMANT_PROBE_RETRY_SEC = 0
_DORMANT_PROBE_BUDGET_SEC = DEFAULT_MAX_SECONDS
# 兜底：脚本会按 --max-seconds 自己正常退出，这个 timeout 只防它彻底卡死。
_DORMANT_PROBE_TIMEOUT_SEC = DEFAULT_MAX_SECONDS + 300
_DORMANT_PROBE_ATTEMPT_FILE = ".dormant_probe_attempt"
_DORMANT_PROBE_LOG_FILE = ".dormant_probe.log"
# 与 dormant_probe.py 里 main() 的 flock 撞车返回值一致。
# 这不是「体检失败」，是「另一个实例正在跑」，不能计入失败也不能刷屏。
_DORMANT_PROBE_RC_BUSY = 7
_DORMANT_PROBE_LOCK_FILE = ".dormant_probe.lock"
_DORMANT_PROBE_FAIL_STREAK = 0


def _dormant_probe_attempt_ts() -> float:
    """上次**发起**体检的时间，只用于诊断与重启后的观测，不做调度闸门。

    WHY 不再是闸门：主人定标「探测不等待、禁止设置等待时间」。以前这里用
    30 分钟窗口挡重跑，结果连 RC_MORE（分批续跑，进度已存盘）都要等半小时，
    247 条节点要拖好几个小时。现在改由脚本的 pending 断点续跑 + 状态文件
    updated=0 表达「没跑完」，调度器下一 tick 直接继续，不引入任何等待。
    """
    try:
        with open(os.path.join(_PROJECT, _DORMANT_PROBE_ATTEMPT_FILE),
                  encoding="utf-8") as f:
            return float((f.read() or "0").strip())
    except (OSError, ValueError):
        return 0.0


def _mark_dormant_probe_attempt() -> None:
    try:
        with open(os.path.join(_PROJECT, _DORMANT_PROBE_ATTEMPT_FILE),
                  "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _dormant_state_updated() -> float:
    """状态文件里上一次成功落盘的时间；读不到/没生成过返回 0。

    用文件里的 updated 而不是进程内变量：daemon 重启不该再触发一次全量体检，
    而状态文件真丢了（误删 / 新机首装）必须立刻重跑。
    """
    try:
        with open(os.path.join(_PROJECT, ".dormant_state.json"), encoding="utf-8") as f:
            return float((json.load(f) or {}).get("updated") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def _dormant_probe_lock_busy() -> bool:
    """瞬时探测：另一个体检实例是否正在跑。**不等待、不持有、不设窗口**。

    WHY（2026-11-11 22:32 实测）：服务重启时 `KillMode=process` 只杀主进程，
    上一轮体检子进程残留并持有 flock。新 daemon 每个 tick 都 spawn 一个
    `dormant_probe.py`，3 秒内 30 次全部 `rc=7` 退出，还被当成连续失败刷了
    4 条 FAIL 日志（连续 1/10/20/30 次）。flock 本身是对的，错在调度器把
    「撞锁跳过」记成了「体检失败」，并且明知在跑还不停起子进程。

    这里只做一次 LOCK_EX|LOCK_NB 探测，拿到立刻释放；拿不到就返回 True。
    调用方下一 tick 会再探 —— 没有任何等待时间，符合「探测不等待」定标。
    """
    try:
        fd = os.open(os.path.join(_PROJECT, _DORMANT_PROBE_LOCK_FILE),
                     os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return False            # 探不了就当没人在跑，交给脚本自己的锁兜底
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _maybe_run_dormant_probe() -> None:
    """主循环每 tick 调一次；到点才在后台线程里跑一次体检。

    子进程隔离是必须的：dormant_probe.py 会 import config、另起一个 sing-box
    临时实例，塞进 daemon 进程会污染 CFG 并把主服务拖住。脚本自身还有 flock，
    与这里的运行标志构成双保险。
    """
    global _DORMANT_PROBE_RUNNING, _DORMANT_PROBE_NEXT_TS
    if _DORMANT_PROBE_RUNNING:
        return
    now = time.time()
    if now < _DORMANT_PROBE_NEXT_TS:
        return
    updated = _dormant_state_updated()
    if updated > 0 and now - updated < _DORMANT_PROBE_PERIOD_SEC:
        # 状态还在有效期内：把下次到期时间钉在"上次成功 + 6h"，之后每 tick
        # 只比一个浮点数，不再反复读文件。
        _DORMANT_PROBE_NEXT_TS = updated + _DORMANT_PROBE_PERIOD_SEC
        return
    # updated=0 有两种含义：状态文件不存在，或本轮只存了进度（未完成）。
    # 两者都必须立刻跑 —— 后者的 pending 断点保证不会重复全量。
    if _dormant_probe_lock_busy():
        # 别的实例（服务重启残留 / 运维手动跑）正在体检：不 spawn、不记失败、
        # 不设任何等待时间，下一 tick 再探。锁释放后立刻就能跑起来。
        return
    _mark_dormant_probe_attempt()
    _DORMANT_PROBE_RUNNING = True

    def _run():
        global _DORMANT_PROBE_RUNNING, _DORMANT_PROBE_NEXT_TS, _DORMANT_PROBE_FAIL_STREAK
        try:
            r = subprocess.run(
                [sys.executable, os.path.join(_PROJECT, "dormant_probe.py"),
                 "--max-seconds", str(_DORMANT_PROBE_BUDGET_SEC)],
                cwd=_PROJECT, capture_output=True, text=True,
                timeout=_DORMANT_PROBE_TIMEOUT_SEC)
            # 完整输出必须落盘。实测第一次自动体检 225/247 条 error，
            # 但日志只留了最后 4 行，首条故障原因、实例什么时候垮的
            # 全都查不到 —— 只报告摘要等于把证据丢掉。
            try:
                with open(os.path.join(_PROJECT, _DORMANT_PROBE_LOG_FILE),
                          "w", encoding="utf-8") as f:
                    f.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')} "
                            f"exit={r.returncode}\n")
                    f.write(r.stdout or "")
                    if r.stderr:
                        f.write("\n--- stderr ---\n" + r.stderr)
            except OSError:
                pass
            tail = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()][-4:]
            if r.returncode == 0:
                log("  · 沉睡池体检完成：" + " | ".join(tail)
                    + f"（完整输出：{_DORMANT_PROBE_LOG_FILE}）")
                _DORMANT_PROBE_NEXT_TS = time.time() + _DORMANT_PROBE_PERIOD_SEC
                _DORMANT_PROBE_FAIL_STREAK = 0
            elif r.returncode == _DORMANT_PROBE_RC_BUSY:
                # 探锁与子进程拿锁之间有 TOCTOU 窗口，仍可能撞上。
                # 这是「跳过」不是「失败」：不计连续失败、不刷屏、不等待。
                _DORMANT_PROBE_NEXT_TS = 0.0
            elif r.returncode == RC_MORE:
                # 预算用尽、进度已存盘：这不是失败，是分批。立刻续跑剩下的，
                # 绝不能套失败退避，否则 247 条要拖好几个小时。
                log("  · 沉睡池体检分批续跑：" + " | ".join(tail[-2:])
                    + "（进度已存，立即继续，不等待）")
                _DORMANT_PROBE_NEXT_TS = 0.0
                _DORMANT_PROBE_FAIL_STREAK = 0
            else:
                _DORMANT_PROBE_FAIL_STREAK += 1
                if _DORMANT_PROBE_FAIL_STREAK == 1 or _DORMANT_PROBE_FAIL_STREAK % 10 == 0:
                    err = [x.strip() for x in (r.stderr or "").splitlines()
                           if x.strip()][-2:]
                    log(f"  ⚠ 沉睡池体检失败 exit={r.returncode}"
                        f"（连续 {_DORMANT_PROBE_FAIL_STREAK} 次）："
                        + " | ".join(tail + err)
                        + f"（立即重试，不等待；完整输出：{_DORMANT_PROBE_LOG_FILE}）")
                _DORMANT_PROBE_NEXT_TS = 0.0
        except subprocess.TimeoutExpired:
            log(f"  ⚠ 沉睡池体检超时（{_DORMANT_PROBE_TIMEOUT_SEC}s），进度已在脚本侧落盘；"
                "遗留临时实例由脚本下次的 kill_orphans 清理")
            _DORMANT_PROBE_NEXT_TS = 0.0
        except Exception as e:
            log(f"  ⚠ 沉睡池体检异常：{e}")
            _DORMANT_PROBE_NEXT_TS = 0.0
        finally:
            _DORMANT_PROBE_RUNNING = False

    try:
        threading.Thread(target=_run, daemon=True, name="dormant-probe").start()
    except Exception as e:
        # 线程没起来必须把运行标志复位，否则调度永久卡死（没有第二个地方清它）
        _DORMANT_PROBE_RUNNING = False
        _DORMANT_PROBE_NEXT_TS = 0.0
        log(f"  ⚠ 沉睡池体检线程启动失败：{e}")


def _dirty_line_janitor() -> None:
    """常驻脏线守护：没有周期闸门，只要存在脏 IP 就一直重连到换出干净地址。

    WHY 不用周期任务：实测出口 IP 存活中位 12.5s，任何按分钟计的闸门都意味着
    脏线会滞留远超一个 IP 的寿命，换线时可选干净线随之变少。5s 只是防打爆
    （每条线每轮一次探测），不是调度周期。
    """
    while True:
        try:
            # 档位是运维动作：tiers.json 一变就重算停用集合并双向对账，
            # 别等到下次重启才生效（此前改档位对运行中的 daemon 完全无效）。
            if _refresh_tier_disabled():
                _tier_reconcile()
            # H2 台账同理：render_h2_lines.py 会在运行中重排编号，名单不跟着
            # 换就会出现「线在跑、结论却记在 SCAN 假名下」。
            _refresh_h2_members()
        except Exception as e:
            log(f"✗ 档位热更新异常: {e}")
        try:
            # 先换节点、再刷 IP：节点本身死了的话，后面的出口探测必然失败，
            # 白等一轮超时；换到活的节点后同一轮就能继续判 IP 质量。
            _node_rotation_pass()
        except Exception as e:
            log(f"✗ 同块轮回异常: {e}")
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


_node_rotation_started = False


def _node_rotation_loop() -> None:
    """常驻同块轮回守护：只看落地节点本身还通不通。

    与脏线守护分开成独立线程：那个只在 `prober_enabled=False` 的回滚路径下
    启动，而生产走 `prober_enabled=True`（单实例探测器）。把轮回塞进那条分支
    等于写了不跑 —— 实测就是这样：`_node_rotation_pass` 挂上去以后一次都没
    执行过。
    """
    while True:
        try:
            _node_rotation_pass()
        except Exception as e:
            log(f"✗ 同块轮回异常: {e}")
        time.sleep(float(_warp().get("node_rotation_interval_sec", 60)))


def _start_node_rotation() -> None:
    global _node_rotation_started
    with _WARP_REFRESH_LOCK:
        if _node_rotation_started:
            return
        _node_rotation_started = True
    if not _warp().get("node_rotation_enabled", True):
        log("· 同块轮回已关闭（node_rotation_enabled=false）")
        return
    threading.Thread(target=_node_rotation_loop, daemon=True,
                     name="warp-node-rotation").start()
    log("✓ 同块轮回守护已启动（pid 线程）")


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

    WHY 延迟而非立即：换线只改 selector，在途请求仍走刚被换下的
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
            # 这与 selector 无关（换线本身不动已建连接）。
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


def _is_pool_node(tag: str | None) -> bool:
    """这个 tag 属不属于代理池（订阅节点）。

    池与 WARP 渠道的唯一边界判据，所有「这段逻辑该不该处理它」的问题都问这里，
    不要散落成 startswith("WARP-") 或 `_warp_member(x) is None`。两条都只描述了
    当前命名习惯，不是契约：WARP 成员可能是任意 tag，池节点理论上也可能改名。
    """
    if not tag or tag == "direct":
        return False
    return not tag.startswith("WARP-") and _warp_member(tag) is None


def _pool_nodes() -> list[str] | None:
    """轮换池节点列表（排除 WARP 成员与 direct）；查询失败返回 None。"""
    try:
        _, data = singbox_api(f'/proxies/{CFG["singbox"]["selector"]}', silent=True)
    except Exception:
        return None
    if not data:
        return None
    return [x for x in (data.get("all") or []) if _is_pool_node(x)]


def _proxy_pool_healthy(sample: int = 3, nodes: list[str] | None = None,
                        timeout_ms: int | None = None) -> bool | None:
    """轮换池是否还能服务 opencode 流量；样本全失败才算不可用，查不到返回 None。

    全池 83 个逐个探测代价过高（每个 15s 超时），只取样本；但样本必须随机、
    并发：固定取前 3 个时，只要这 3 个恰好挂了就把整个池判死，daemon 立刻
    回切 WARP —— 代理池资源一点没用上。随机 3 个同时挂的概率极低。
    串行 3 个最坏 45s 卡在主循环上，并发后只受最慢那个约束。

    nodes 可由调用方预先取好复用（回切判据要同时算黑名单占比）。
    timeout_ms 供落池路径覆盖：那条路在 `_WARP_LOCK` 内，不能按 15s 等。
    """
    if nodes is None:
        nodes = _pool_nodes()
        if nodes is None:
            return None
    if not nodes:
        return None
    picked = _random.sample(nodes, min(sample, len(nodes)))
    timeout_ms = (CFG["thresholds"]["node_test_timeout_ms"] if timeout_ms is None
                  else int(timeout_ms))

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


_PROXY_HEALTH_CACHE: tuple[float, bool | None] | None = None


def _proxy_pool_healthy_cached(now: float | None = None) -> bool | None:
    """落池前那次池健康实探，带节流缓存。

    WHY 必须节流：`_fallback_to_proxy` 在 `_WARP_LOCK` 内被调用，而健康判定
    要真连节点。全池枯竭期换线是每秒级的（2026-11-09 21:00–23:00 两小时 937
    次换线），每次都探等于把锁长期占住，`/rotate` 的 2s 客户端超时会全数打空
    —— 那正好是 9router 换线重放要用的通道。

    TTL 取 `proxy_health_cache_sec`(120s)：坏消息最多滞后 2 分钟，而锁的占用
    从"每次换线"降到"每 2 分钟一次"。落池后的回切判据仍走不带缓存的
    `_proxy_pool_healthy`（它有自己的 `proxy_check_sec` 节流）。
    """
    global _PROXY_HEALTH_CACHE
    now = time.time() if now is None else now
    ttl = float(_warp().get("proxy_health_cache_sec", 120))
    if _PROXY_HEALTH_CACHE is not None and now - _PROXY_HEALTH_CACHE[0] < ttl:
        return _PROXY_HEALTH_CACHE[1]
    # 单节点超时压到 5s：3 个并发，最坏 ~5s 出结论，而不是 15s。
    ok = _proxy_pool_healthy(timeout_ms=5000)
    _PROXY_HEALTH_CACHE = (now, ok)
    return ok


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
                   "consec_err": 0, "full_err": False, "transport": 0}
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
    global _ACTIVE_WARP_IP
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
            # 盲切：这条线的地址没探测过，传 None 而不是外面的 `ip`
            # （那是**当前**出口的地址，不是目标线的）。
            if not _port_open(m["port"]) or not _switch_warp(m["tag"]):
                continue
            _begin_segment(time.time())
            _proxy_dwell_since = 0
            log(f"↻ 轮换池冷却中（剩 {remain_min}min），改切 WARP 隧道 {m['tag']}")
            return True
        if active is None:
            return False
        return _restart_warp_inplace(active, old_ip)
    # 落池前实测池还能不能服务：2026-10-26 就是因为池内节点全 connection reset，
    # 把流量甩到死池上（落池即断流），才把这个开关关掉的。重开之前补上这道闸：
    # 随机抽 3 条真探一遍，只有 True 才落池；None（Clash API 抖动查不到）与
    # False 都按不可用处理 —— 宁可原地重连，也不把流量甩给一个死池。
    # 只在「冷却已过、确实要落池」这一步付这次的探测成本。
    pool_ok = _proxy_pool_healthy_cached()
    if pool_ok is not True:
        log(f"⚠ 池健康探测未通过（{pool_ok}），不落池，改为原地重连")
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
    _begin_segment(landed)
    _proxy_dwell_since = landed     # 落池时刻：回切判据只统计此后的拦截
    # 清进程级出口地址：此时「当前出口」已经不是任何 WARP 线，留着旧地址会让
    # /rotate 把「还挂在被换下那条隧道上的连接」当成新出口的连接（见 _with_active_line）。
    _ACTIVE_WARP_IP = None
    # 落池对外部调用方同样是一次「旧地址不再承载流量」：拿着旧地址的 429 应该
    # 得到 already_retired 然后重连，而不是再触发一次换线（池上根本没有 WARP
    # 可换，只会浪费一次 no_warm_standby）。
    _exit_ledger_note(old_ip, None, landed)
    log("✓ 已切到轮换池 proxy（WARP 隧道全部不可用）")
    return True


def _restart_warp_inplace(member: dict, old_ip: str | None) -> bool:
    """单线兜底：原地重启当前隧道换会话地址（有几秒断流）。"""
    global _last_warp_switch_ok_ts, _ACTIVE_WARP_IP
    backoff = _warp()["retry_backoff_sec"]
    for attempt in range(1, _warp()["max_attempts"] + 1):
        log(f"  ↻ {member['tag']} 原地重连 #{attempt}")
        _invalidate_warp_line_ip(member["tag"])
        # v4 从第一次就换节点（重启同节点 = 同一个地址池），见 _refresh_idle_warp。
        ok = (_restart_warp_line(
                  member, rebind=(member.get("family") == "v4"
                                  or attempt > 1))
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
                _ACTIVE_WARP_IP = new_ip
                # 换线历史也要跟着换址：否则迟到的 429 回溯到这条线时会拿到
                # 换址前的旧地址，把已经不再承载流量的地址记成新的坏账。
                _record_warp_switch_history(member["tag"], now, new_ip)
                # 换线表：selector 没动，但出口地址换了 —— 对调用方来说同样是
                # 「旧地址不再承载流量」。漏记这一笔，拿着旧地址的 429 会被判成
                # 「还没换走」，于是为了一个已经不是出口的地址再烧一条新线。
                _exit_ledger_note(old_ip, new_ip, now, member["tag"])
                _begin_segment(now)  # 本段结束：主动轮换计时重新开始
                return True
            log(f"  ✗ {member['tag']} 出口不新鲜: {new_ip or '探测失败'}")
        else:
            log(f"  ✗ {member['tag']} {_warp()['ready_timeout_sec']}s 内未就绪")
        time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
    return False


_SWITCH_WAIT_CAP_SEC = 1.2   # 见 _warp_switch_wait_deadline


def _warp_switch_wait_deadline(now: float) -> float:
    """无备线时的等待上限。

    调用方 9router 的 ASIP_TIMEOUT_MS=2000，而 _rotate_once 全程持 _API_LOCK：
    客户端 2s 挂断后 _rotate_once 仍在锁里等，超时的代价是「客户端在旧隧道上
    重放、出口却已经换了」（asipRotate.js:17-19），不是只丢一个响应。

    实测 2026-10-31 19:31 起 159258 行日志：进入本分支 1608 次，1521 次能配上
    它自己的超时行，其中 1042 次（68.5%）整整走满 3s —— 那 1s 是在客户端已经
    挂断之后才等的，且此刻快照里本就没有备线，多等也变不出来。

    上限是常量不是配置项：它由调用方超时反推，改了客户端就得跟着改，不该被
    「调一调试试」。
    """
    return now + min(float(_warp().get("switch_wait_sec", 3)), _SWITCH_WAIT_CAP_SEC)


def _warp_exhaustion_escape(reason: str, active: dict | None,
                            old_ip: str | None, now: float) -> bool:
    """全池换不出 OK 时的最后一条路：把流量甩到机场池（**另一个 IP 空间**）。

    WHY 必须有这条路 —— 备线的判据是「120s 内出现过 OK 的样本」
    （`_warp_line_fresh_candidates`）。当整个 WARP 地址空间都被判 quota 时，
    这个前置条件**永远无法满足**：没有线 OK → 没有备线 → 不换线 → 更不会 OK。
    2026-11-09 21:00–23:00 实测就是这么空转的：

        本次不切 937 次 / 换线成功 4 次 / 惩罚期熔断 347 次
        同期 9router 报 432 条 503（当晚 598 条里的 72%）
        每个新换到的 IPv6 立刻被判 quota（Retry-After 10~11h）

    也就是说「重启换 IP」在这两小时里买不到任何配额 —— 不是换得不够多，
    而是**同一条地址空间里没有可换的东西**。能换的只有出口的地址空间本身，
    而机场池（anytls/trojan，AWS/HK/SG/JP 机房 IP）正是那个另一空间。
    2026-11-09 06:20 实测：把 `opencode-out` 切到 `proxy` 后 3 条 opencode
    请求 2×200（4.4s / 4.2s）、1×429 —— 池子能承载这类流量。

    守卫（缺一不可，缺任何一条就退回原行为「不切」，绝不盲落）：
      1. `proxy_fallback_enabled` 打开。2026-10-26 因「池内节点全 connection
         reset，落池等于断流」关掉过它，重开必须同时具备第 2、3 条。
      2. 连续 `warp_exhaust_streak_min`(3) 次走到死锁分支。单次「没备线」
         可能只是一条线打嗝，不能因为一次抖动就把流量甩到池上。
      3. `_proxy_pool_healthy()` 为 True。None（Clash API 抖动查不到）与
         False 都按不可用处理 —— 宁可原地重连，也不重演「落池即断流」。
    返回是否真的发起了切换。
    """
    global _WARP_EXHAUST_STREAK
    _WARP_EXHAUST_STREAK += 1
    streak = _WARP_EXHAUST_STREAK
    need = int(_warp().get("warp_exhaust_streak_min", 3))
    # 已经在池上时不做任何盲目动作：`_fallback_to_proxy` 的冷却分支会"盲切回
    # 其它 WARP 成员"，而此刻 WARP 恰恰就是刚被证明换不出配额的那一侧 ——
    # 那等于把正在服务的流量从池上拽回一条被 quota 的线。离开池只有一条路：
    # `_maybe_failback_to_warp`（要求池真的不可用，且 WARP 有可用地址）。
    if _proxy_dwell_since:
        log(f"  · 当前已在轮换池（连续 {streak} 次无 WARP 备线），交给回切判据")
        return False
    if not _warp().get("proxy_fallback_enabled", False):
        log(f"  ⚠ 等待超时仍无新鲜备线（连续 {streak}/{need}），本次不切"
            f"（落池开关关闭）")
        return False
    if streak < need:
        log(f"  ⚠ 等待超时仍无新鲜备线（连续 {streak}/{need}），本次不切"
            f"（先按单线抖动留观）")
        return False
    # 走到这里 = 连续多次换不出 OK，且开关允许落池。`_fallback_to_proxy`
    # 内部还会再判一次池健康（第 3 条），两道闸各自独立。
    started = _fallback_to_proxy(active, old_ip, now)
    if started:
        _WARP_EXHAUST_STREAK = 0
        log(f"✓ 全池换不出配额（连续 {streak} 次），已改由机场池承载（{reason}）")
    else:
        log(f"  ⚠ 落池未启动（连续 {streak} 次仍无备线），等下一轮证据")
    return started


def _reset_warp_exhaust_streak() -> None:
    """换线成功/重新出现备线后清空死锁计数 —— 证据变了，旧计数不再是事实。"""
    global _WARP_EXHAUST_STREAK
    _WARP_EXHAUST_STREAK = 0


def _reconnect_warp_by_table(reason: str, interrupt: bool = True,
                              out: dict | None = None) -> bool:
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
        # 空等无用：先叫醒探测器让它立刻重排一次（它可能正睡在自己的 idle
        # 等待里），再等很短一窗。旧实现只等不催，20s 到点照样放弃 ——
        # 实测 74 次换线里 73 次走这条路。
        with _WARP_LINE_CV:
            _WARP_LINE_CV.notify_all()
        deadline = _warp_switch_wait_deadline(now)
        log("  · 暂无 120s 内 OK 的备线，已催探测器，短等写入")
        cands = _warp_line_wait_fresh(
            active["tag"] if active else None, deadline)
    if not cands:
        # 原先这里直接放弃（"不重启、不落池"）。但全池惩罚期时「必须有 120s
        # 内 OK 的备线」是自锁条件：没有线 OK 就永远没有备线。2026-11-09
        # 21:00–23:00 实测 937 次换线全卡死在这里（成功 4 次），同期 432 条
        # 503。逃生路径见 `_warp_exhaustion_escape`（连续多次才落池）。
        return _warp_exhaustion_escape(reason, active, old_ip, now)
    _reset_warp_exhaust_streak()
    cands = [(m, ip) for m, ip in cands if ip != old_ip]
    if not cands:
        log("  ⚠ 新鲜备线均与当前出口同址，本次不切")
        return False
    # 与 selector 实际成员取交集：`.warp/warp-outbound.json` 落后于 members 时，
    # 这些线切过去只会拿到 400。2026-11-01 实测 CF-E/CF-F/WG-13 两小时内吃掉
    # 524 次失败尝试，换线成功率被拖到 38%，出口还在原地卡了 71 秒。
    # 快照未建立（启动早期）时不过滤，退回原行为 —— 宁可多试一次，也不能
    # 因为查不到快照就把所有备线判死。
    sel = (_clash_snapshot(_warp()["selector"]) or {}).get("all") or []
    if sel:
        sel_set = set(sel)
        cands = [(m, ip) for m, ip in cands if m["tag"] in sel_set]
        if not cands:
            log("  ⚠ 备线均不在 selector 成员里（warp-outbound 未重建），本次不切")
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
            if not _switch_warp(m["tag"], ip=ip):
                continue
            switched_at = time.time()
            _mark_warp_ip_used(ip, switched_at)
            _warp_line_selected(m["tag"], ip, switched_at)
            _ACTIVE_WARP_IP = ip
            _begin_segment(switched_at)
            _proxy_dwell_since = 0
            _from = active["tag"] if active else "proxy"
            if out is not None:
                out["tag"] = m["tag"]
                out["ip"] = ip
                out["switched_at"] = switched_at
                out["old_tag"] = active["tag"] if active else None
            log(f"✓ WARP 换线成功: {_from} -> {m['tag']} (出口 {ip})")
            _reset_warp_exhaust_streak()
            _after_warp_switch(active["tag"] if active else None, interrupt,
                               reason)
            return True
        finally:
            _release_refresh_slot(m["tag"])
    log("  ⚠ 新鲜备线切换均失败，本次不切")
    return False


def reconnect_warp(reason: str, interrupt: bool = True) -> bool:
    """把 opencode 流量换到另一条出口新鲜（窗口外）的隧道上。

    备线在窗口内用过就先重启它换会话 IP（此时它闲置，重启安全），再切过去
    （PUT selector 秒级、在途请求不断）。备线都不可用才原地重连当前线
    （有几秒断流）。

    不刷新刚换下的那条：在途请求仍走它，重启会掐断连接。
    """
    if _PROBER_STARTED:
        return _reconnect_warp_by_table(reason, interrupt)
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
                if not (_restart_warp_line(m)
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
        n_probe = sum(1 for p in probes if _warp_ip_rank(p[1], now, guard)[0] == 1)
        n_ok = sum(1 for p in probes if _warp_ip_rank(p[1], now, guard)[0] == 2)
        log(f"  ↻ 选线: 从没用过 {n_new} 条, 探针 OK {n_probe} 条, "
            f"次级优质 {n_ok} 条, "
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
        switched = _switch_warp(m["tag"], ip=ip)
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
    _ACTIVE_WARP_IP = ip
    _begin_segment(switched_at)
    _proxy_dwell_since = 0          # 回到 WARP，驻留计时清零
    _from = active["tag"] if active else "proxy"
    log(f"✓ WARP 换线成功: {_from} -> {m['tag']} (出口 {ip}, "
        f"{'新段' if rest is None else f'/64 静置 {rest/60:.0f}min'})")
    _after_warp_switch(active["tag"] if active else None, interrupt, reason)
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


def _warp_reconnect_worker(reason: str, cooldown_on_ok: float,
                           interrupt: bool = True):
    """cooldown_on_ok 由调度处算好（含退避），这里不重算，否则会覆盖掉放大的冷却。"""
    global _warp_cooldown_until
    ok = False
    try:
        ok = reconnect_warp(reason, interrupt)
    except Exception as e:
        log(f"✗ WARP 重连异常: {e}")
    finally:
        cooldown = cooldown_on_ok if ok else _warp()["cooldown_fail_sec"]
        _warp_cooldown_until = time.time() + cooldown
        _WARP_LOCK.release()



_LAST_TRANSPORT_PROBE_TS = 0.0
# (换线窗口起点, transport 计数)：同一窗口内计数没变就说明证据没变，不再重复探。
# 窗口起点一起进签名，换线后窗口重置能自动重新获得一次探测机会。
_LAST_TRANSPORT_PROBE_SIG = (0.0, 0)
# 出口正常时的僵死连接清理节流：清理本身便宜，但 503 风暴期每秒都会触发。
_LAST_STALE_SWEEP_TS = 0.0
# L7 探针的连续失败计数：单次悬挂可能只是上游抖动/慢排队，连续两次才换线，
# 避免把一条正常但偶发超时的出口切走（K8s/CF LB 的 consecutive_down 同思路）。
_LANE_FAIL_STREAK = 0
_LANE_FAIL_TS = 0.0
_LANE_LOCK = threading.Lock()
LANE_FAIL_CONFIRM = 2
LANE_FAIL_WINDOW_SEC = 120.0


def _maybe_verify_transport_failures(count: int, now: float) -> None:
    """传输层 503 出现后异步验证当前出口是否真的死了，再决定换线。

    WHY 不能见 503 就换：sidecar 全局故障（spawn 失败 / 5min backoff）时换线
    无效，只会制造 churn。区分办法是绕开 9router、直接经当前隧道查 CF trace：
    出口通 → 问题在 9router 侧（不换）；出口不通 → 换线（不标脏，503 不是
    出口级证据）。探针节流，避免 503 批量落库时每秒起一个线程。
    """
    global _LAST_TRANSPORT_PROBE_TS, _LAST_TRANSPORT_PROBE_SIG
    gap = float(_warp().get("transport_probe_gap_sec", 15))
    if now - _LAST_TRANSPORT_PROBE_TS < gap:
        return
    sig = (_last_warp_reconnect_ts, count)
    if sig == _LAST_TRANSPORT_PROBE_SIG:
        return
    tag = active_warp_tag()
    member = _warp_member(tag)
    if not member:
        return
    _LAST_TRANSPORT_PROBE_TS = now
    _LAST_TRANSPORT_PROBE_SIG = sig
    try:
        threading.Thread(target=_transport_probe_worker,
                         args=(tag, member["port"], count),
                         daemon=True, name="transport-probe").start()
    except Exception as e:  # 线程起不来时证据签名必须回滚，否则这批 503 永不再探
        _LAST_TRANSPORT_PROBE_TS = 0.0
        _LAST_TRANSPORT_PROBE_SIG = (0.0, 0)
        log(f"✗ 传输层探针线程启动失败: {e}")


OPENCODE_LANE_URL = "https://opencode.ai/inference/v1/chat/completions"


def _opencode_lane_alive(port: int, timeout_sec: float = 6.0) -> bool:
    """经指定本地代理打一条最小 free-lane 请求，判断上游是否在静默悬挂。

    `port` 应当是**9router 实际走的那个本地代理端口**（mixed_port），不是隧道
    成员端口：2026-10-27 12:20 实测，从成员 socks 直连探活返回 429（隧道通），
    而 sidecar 经 sing-box 的池化连接却 socket hang up —— 探错路径就会在真正
    挂死时判定「出口正常」，asip 于是永远不换线。

    WHY 不能只看 CF trace：2026-10-27 00:0x 的故障里出口探测 1.2s 通、
    GET https://opencode.ai/ 也 0.6s 通，但带 x-opencode-* 指纹的 chat 请求
    被挂满超时；同一端点不带指纹时 0.26s 就返回 429。CF 可达 ≠ 上游肯服务，
    只有真打一次 free lane 才能区分「这个 IP 被针对」和「sidecar 池僵死」。

    任何 HTTP 响应（含 429）都算活，只有超时/连接错才算悬挂。正常出口上这条
    探测几乎零成本（429 不消耗配额）。
    """
    payload = json.dumps({
        "model": "deepseek-v4.1-flash-free",
        "max_tokens": 1,
        "stream": True,
        "messages": [{"role": "user", "content": "1"}],
    })
    sid = "ses_asip_lane_probe"
    cmd = [
        "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
        "--max-time", str(timeout_sec),
        "--proxy", f"socks5h://{_warp()['socks_host']}:{port}",
        "-X", "POST", OPENCODE_LANE_URL,
        "-H", "content-type: application/json",
        "-H", "authorization: Bearer public",
        "-H", "user-agent: opencode/latest/2.0.26/cli",
        "-H", "x-opencode-client: cli",
        "-H", f"x-opencode-session: {sid}",
        "-H", f"x-opencode-session-id: {sid}",
        "-H", f"x-session-affinity: {sid}",
        "-H", f"x-session-id: {sid}",
        "-H", "x-opencode-project: global",
        "-H", "accept: text/event-stream",
        "--data-binary", payload,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_sec + 2)
    except subprocess.TimeoutExpired:
        return False
    code = (r.stdout or "").strip()
    return bool(code) and code.isdigit() and code != "000"


def _transport_probe_worker(tag: str, port: int, count: int) -> None:
    global _LAST_TRANSPORT_PROBE_SIG
    try:
        _verify_transport_failure(tag, port, count)
    except Exception as e:  # 探针崩了不能留下「已探过」的签名，否则同批证据永不重探
        _LAST_TRANSPORT_PROBE_SIG = (0.0, 0)
        log(f"✗ 传输层探针异常: {e}")


def _verify_transport_failure(tag: str, port: int, count: int) -> None:
    """探当前出口：不通 → 换线；通 → 清理当前链上的僵死连接。

    WHY 探通也要动作：2026-10-26 22:00 的故障里出口是好的（新连接 1~6s
    拿到 429），挂死的是 sidecar 复用的 45 分钟旧连接。只探不清理的话，
    这条僵死连接会被一直复用，503 持续到它自然断开。
    """
    global _LAST_STALE_SWEEP_TS, _LAST_TRANSPORT_PROBE_SIG
    global _LANE_FAIL_STREAK, _LANE_FAIL_TS
    ip = warp_egress_ip(port, timeout_sec=8.0)
    if not ip:
        log(f"⚠ 传输层 503 ×{count} 且 {tag} 出口探测失败 → 换线")
        # respect_evidence=False：**这条证据比 requestDetails 强得多**。
        # 上面那行 `warp_egress_ip()` 是刚打的实探，返回空就说明这个出口此刻
        # 出不去 —— 与「窗口里是哪一段的 429」完全无关。证据闸问的是「新出口
        # 说话了没有」，而这里已经有直接答案了，再拦就是拿间接信号否决直接
        # 观测。实测 2026-11-09 10:07 因此白等了两轮（94s / 109s）。
        if _reconnect_warp_now(f"transport ×{count}（出口探测失败）", _now_ts(),
                               respect_evidence=False):
            with _LANE_LOCK:  # 新出口的第一条证据不该继承旧线上的计数
                _LANE_FAIL_STREAK = 0
            return
        # 冷却/闸门拒了这次换线：旧签名会把「同一批 503」锁死成不再探，出口已死
        # 却永远等不到下一次机会。撤掉签名，下一批证据到达时再试。
        _LAST_TRANSPORT_PROBE_SIG = (0.0, 0)
        log("  · 传输层换线被冷却/闸门拒绝，已解除签名等待重试")
        return
    if not _opencode_lane_alive(MIXED_PORT):
        probe_now = time.time()
        with _LANE_LOCK:
            if probe_now - _LANE_FAIL_TS > LANE_FAIL_WINDOW_SEC:
                _LANE_FAIL_STREAK = 0
            _LANE_FAIL_STREAK += 1
            _LANE_FAIL_TS = probe_now
            streak = _LANE_FAIL_STREAK
        if streak < LANE_FAIL_CONFIRM:
            # 留观一次：清掉签名，下一批 503 到达时再探（仍受 15s 探针节流约束）。
            log(f"⚠ 传输层 503 ×{count}，{tag} 出口可达（{ip}）"
                f"但 opencode 链路悬挂（{streak}/{LANE_FAIL_CONFIRM}），留观")
            _LAST_TRANSPORT_PROBE_SIG = (0.0, 0)
            return
        log(f"⚠ 传输层 503 ×{count}，{tag} 出口可达（{ip}）"
            f"但 opencode 链路连续 {streak} 次悬挂 → 换线")
        if _reconnect_warp_now(f"transport ×{count}（opencode 链路连续悬挂）", _now_ts()):
            with _LANE_LOCK:
                _LANE_FAIL_STREAK = 0
            return
        _LAST_TRANSPORT_PROBE_SIG = (0.0, 0)
        log("  · 传输层换线被冷却/闸门拒绝，已解除签名等待重试")
        return
    with _LANE_LOCK:
        _LANE_FAIL_STREAK = 0
    now = time.time()
    if not _warp().get("cleanup_enabled", False):
        log(f"⚠ 传输层 503 ×{count}，{tag} 出口正常（{ip}）"
            f" → 连接清理已禁用（cleanup_enabled=false），仅记录")
        return
    gap = float(_warp().get("transport_stale_gap_sec", 600))
    if now - _LAST_STALE_SWEEP_TS < gap:
        log(f"⚠ 传输层 503 ×{count}，{tag} 出口正常（{ip}）"
            f" → 连接池僵死，清理节流中（<{gap:.0f}s）")
        return
    _LAST_STALE_SWEEP_TS = now
    log(f"⚠ 传输层 503 ×{count}，{tag} 出口正常（{ip}）→ 清理当前链僵死连接")
    _schedule_connection_cleanup(
        tag, float(_warp().get("transport_stale_age_sec", 300)), reason="僵死")


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

    传输层 503（9router Bun 传输悬挂）不走这条阈值路径：它不是出口级证据，
    先经当前隧道探出口，探不通才换线（见 `_maybe_verify_transport_failures`）。
    """
    global _last_warp_reconnect_ts, _warp_cooldown_until
    blocked = count_warp_blocked_since(_last_warp_reconnect_ts)
    # 传输层 503 不走上面的出口级阈值（它不是出口被限流/拉黑的证据）。
    # 达到阈值后先异步探当前出口，探不通才换线，见 _maybe_verify_transport_failures。
    transport_fail = int(blocked.get("transport", 0))
    if transport_fail >= int(_warp().get("transport_fail_threshold", 1)):
        _maybe_verify_transport_failures(transport_fail, now)
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
    # `_last_warp_switch_ok_ts > 0` 是必需的：0 表示「还没成功换过线」，而
    # `now - 0` 在启动后 120 秒内恒小于窗口 —— 不排除的话，daemon 刚起来的
    # 前两分钟会被判定成「窗口里全是上一段的证据」而完全不换线。
    stale_only = (_last_warp_switch_ok_ts > 0
                  and now - _last_warp_switch_ok_ts
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
    # 这里**不再单设 min_switch_gap_sec(5s)**：它只管「两次换线别挨太近」，
    # 而真正的判据是「新出口说话了没有」—— 统一闸门 `_switch_evidence_ready`
    # 在 `_reconnect_warp_now` 里，窗口 120s，且新出口一报错就放行。5 秒那道
    # 闸正好解释了「在位时长恒等于 5 秒」：它是唯一挡住满窗风暴的东西，闸一开
    # 就切走。配置项保留（旧部署仍读得到），但不再参与判定。
    # 触发条件已满足，委托给统一入口处理冷却、证据窗口与并发
    full_err_bypass = blocked.get("full_err", False)
    _reconnect_warp_now(
        " + ".join(f"{k} ×{blocked[k]}" for k in triggered),
        now,
        ignore_cooldown=full_err_bypass
    )


def _switch_evidence_ready(now: float) -> tuple[bool, str]:
    """换线后是否已经拿到**新出口自己的**证据。返回 (可否再换, 原因)。

    这是全模块唯一的「刚换过就别换」判据，四个错误/探测驱动的入口共用：
    探测惩罚期、探测失败、传输层 503、错误率阈值。此前只有最后一条有
    （`maybe_reconnect_warp` 的 stale_only），另外三条只受 cooldown_ok_sec(20s)
    约束 —— 而换线生效要 p50 4 秒，20 秒冷却等于没有。

    实测 2026-11-09 修复后的 17 次换线里，5 次是「探测发现当前出口处于惩罚期」
    1 秒内再次触发、6 次是 rate429 在 1~3 秒内再次触发：新出口一条请求都还没
    发出去，窗口里全是上一个出口在途请求的 429，就被判死并切走。在位时长中位
    4 秒、64% 不到 10 秒。

    「新出口自己的证据」= 生效之后出现过出口级错误（403/429）。这时窗口里至少
    混进了新出口的真实表现，换线才有依据。还没出现就等着 —— 生产流量 2~5 req/s，
    新证据通常 1 秒内就到，不是真的等满 grace 秒。
    """
    if not _SWITCHED_IN_PROCESS:
        # 启动恢复的段起点不算「刚换过线」：它是重启前那一段的开头，可能
        # 就在几秒前（重启前刚切过），也可能在两小时前。拿它当证据闸的锚点
        # 会让刚重启的进程在最需要换线的前 120 秒里换不了 —— 而那正是出口
        # 可能最脏的时刻（sing-box 刚复位到配置里的 default）。
        return True, "本进程还没换过线"
    age = now - _last_warp_switch_ok_ts
    grace = float(_warp().get("switch_stale_grace_sec", 120))
    if age >= grace:
        return True, "已过证据窗口 %.0fs" % age
    fresh = count_warp_blocked_since(_last_warp_switch_ok_ts)
    if fresh.get("abuse403", 0) or fresh.get("rate429", 0):
        return True, "新出口已报错 %d+%d 条" % (
            fresh.get("abuse403", 0), fresh.get("rate429", 0))
    return False, ("换线仅 %.0fs，新出口尚无错误证据（窗口里 %d 条都是旧出口的）"
                   % (age, fresh.get("other_err", 0)))


def _reconnect_warp_now(reason: str, now: float, ignore_cooldown: bool = False,
                        interrupt: bool = True,
                        respect_evidence: bool = True) -> bool:
    """立刻发起一次换线，绕开 403/429 阈值。返回是否真的发起了。

    WHY 需要绕开阈值：`maybe_reconnect_warp()` 的触发依赖
    `count_warp_blocked_since()` 统计到足够的下游错误。但「隧道复活」这件事
    本身不产生任何 403/429 —— 只看下游错误，daemon 会永远停在 proxy 上。

    ignore_cooldown 仅供主动轮换：计算出的最小周期可能短于 cooldown_ok_sec(20s)，
    受冷却约束就永远等不到触发。`_WARP_LOCK` 无论如何都要抢到 —— 那是并发换线
    的互斥，不是节流。错误触发路径保持受冷却约束。

    respect_evidence 是**所有错误/探测驱动入口共用**的闸门（见
    `_switch_evidence_ready`）：刚换完、新出口还没说话时不许再换。主动轮换传
    False —— 它按配额/时钟走，与「出口好不好」无关，等证据只会让周期漂移。

    interrupt 决定换线后是否清理旧链在途连接（见 `_after_warp_switch`）。
    默认 True —— 调用方几乎都是错误触发（出口已判死，留着旧连接只是在等超时）。
    只有主动轮换传 False：旧出口健康，在途请求该跑完。
    """
    global _last_warp_reconnect_ts, _warp_cooldown_until
    if not ignore_cooldown and now < _warp_cooldown_until:
        return False
    if not _WARP_LOCK.acquire(blocking=False):
        return False
    if respect_evidence:
        ready, why = _switch_evidence_ready(now)
        if not ready:
            # 锁是刚抢到的，必须还回去，否则后续换线全部被拒（本函数不靠
            # `_warp_reconnect_worker` 的 finally 释放这条路径）。
            _WARP_LOCK.release()
            log(f"  · 暂不换线（{reason}）：{why}")
            return False
    delay = _warp()["cooldown_ok_sec"]
    prev_cooldown = _warp_cooldown_until
    _last_warp_reconnect_ts = _now_ts()
    _warp_cooldown_until = now + delay  # 先占位，防 tick 重入
    try:
        threading.Thread(target=_warp_reconnect_worker,
                         args=(reason, delay, interrupt),
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
                now, ignore_cooldown=_last_warp_reconnect_ts <= _last_warp_switch_ok_ts,
                interrupt=False, respect_evidence=False)
        return

    n = count_dsv4_ok_since(start)
    target = int(_warp()["proactive_after_dsv4_ok"])
    _proactive_progress = f"requests {n}/{target}格"
    if n >= target:
        _reconnect_warp_now(
            f"主动轮换 已用配额 {n} 格 dsv4 ≥ {target}",
            now, ignore_cooldown=_last_warp_reconnect_ts <= _last_warp_switch_ok_ts,
            interrupt=False, respect_evidence=False)


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
               if _is_pool_node(n) and n != current and n not in _DEAD_NODES]
    # 仅测试 TTL 已到的死节点（允许恢复），保持指数退避语义
    now = time.time()
    for n in list(_DEAD_NODES):
        if _is_pool_node(n) and n != current and _DEAD_NODES_RELEASE_TS.get(n, 0) <= now:
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
            _NODE_DELAY_TS[node] = now
            _dead_fail_count.pop(node, None)
        else:
            dead.add(node)
            _NODE_DELAYS.pop(node, None)
            _NODE_DELAY_TS.pop(node, None)
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
    """后台异步运行健康检查，供主循环非阻塞调用。

    只测池节点、只写池状态：`nodes` 是 selector `proxy` 的成员，WARP 隧道的
    健康另有探测器（`_warp_prober_loop`，按线状态表驱动），两条路径不共享结论。
    """
    return batch_test_nodes(nodes, current)


_POOL_DELAY_CURSOR = 0
_pool_delay_last = 0.0


def _pool_delay_bootstrapping(nodes: list[str] | None) -> bool:
    """池里还有没有「缺样本或样本过期」的节点 —— 冷启动/样本失效后为真。

    为真时补刷按秒级节奏连续排空，而不是等 300s 的健康检查周期。压测实测
    单轮 40 个要 15s：142 个池子按 300s 节奏要 20 分钟才铺满，而这 20 分钟里
    选点没有延迟可用，每次轮换都可能切到死节点。

    注意这里问的是「有没有**测过**」，不是「有没有成功的样本」：探测失败的
    节点也会写时间戳（见 `_refresh_pool_delays`），否则它会永远留在 stale 里，
    让排空循环停不下来。
    """
    if not nodes:
        return False
    now = time.time()
    with _state_lock:
        return any(_is_pool_node(n) and n not in _DEAD_NODES
                   and now - _NODE_DELAY_TS.get(n, 0.0) > _NODE_DELAY_TTL
                   for n in nodes)


def _refresh_pool_delays(nodes: list[str], current: str | None) -> int:
    """补池成员的延迟样本：每轮健康检查抽 `pool_delay_refresh` 个缺样本的节点。

    WHY 不是每轮全测：142 个节点串行测一轮 93s（实测 1.55s/节点），而池的
    主动轮换是 60s 一次。全量刷新会把 _health_pool 占满，并把 sing-box 的
    延迟探测接口打成持续负载。游标轮转保证每个节点在几轮内都会拿到样本。

    只补「缺样本或样本过期」的，已经在 TTL 内的不重测 —— 机场节点延迟的
    有效窗口是分钟级，重复测只是烧配额。
    """
    global _POOL_DELAY_CURSOR
    limit = int(CFG["intervals"].get("pool_delay_refresh") or 40)
    if limit <= 0 or not nodes:
        return 0
    now = time.time()
    with _state_lock:
        stale = [n for n in nodes
                 if _is_pool_node(n) and n not in _DEAD_NODES
                 and n != current
                 and now - _NODE_DELAY_TS.get(n, 0.0) > _NODE_DELAY_TTL]
    if not stale:
        return 0
    # 游标取模：stale 列表每轮都在变长变短，用绝对游标保证不会永远只测前 40 个。
    start = _POOL_DELAY_CURSOR % len(stale)
    batch = (stale[start:] + stale[:start])[:limit]
    _POOL_DELAY_CURSOR = start + len(batch)
    results = _concurrent_test_delays(batch)
    got = 0
    failed_after_probe = 0
    with _state_lock:
        for node, delay in results.items():
            # 测过就给时间戳，包括失败：失败的样本同样是「刚测过」的证据。
            # 不给的话，探不通的节点永远留在 stale 里，冷启动排空判据永远为真
            # → 每秒派发一轮 40 个节点的探测，把 _health_pool 打满（实测会连续
            # 跑 10 轮 / 112s 才收敛）。
            _NODE_DELAY_TS[node] = now
            if delay is None:
                _NODE_DELAYS.pop(node, None)
                failed_after_probe += 1
                continue
            _NODE_DELAYS[node] = delay
            got += 1
    log(f"  池延迟样本: {got}/{len(batch)} 刷新成功"
        f"{f'，{failed_after_probe} 个探测失败' if failed_after_probe else ''}"
        f"（池内有效样本 {sum(1 for n in nodes if now - _NODE_DELAY_TS.get(n, 0.0) <= _NODE_DELAY_TTL)}/{len(nodes)}）")
    return got

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
        # 未知延迟给 0.4：比中位数以下的所有已知节点（0.5~1.0）差，但不该像
        # 0.1 那样被系统性打死 —— 新上架的节点还没有样本，0.1 意味着它只会
        # 在「两个都未知」时被抽到，于是永远拿不到样本，形成冷启动死锁。
        return 0.4

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
        if not _is_pool_node(n) or n == current:
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
        # 过期样本按「未知」处理：留着它等于用一个 15 分钟前的延迟决定此刻的
        # 出口，而机场节点的延迟按分钟级抖动。
        now = time.time()
        delays = {n: d for n, d in _NODE_DELAYS.items()
                  if now - _NODE_DELAY_TS.get(n, 0.0) <= _NODE_DELAY_TTL}
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

    # 抽查上限 3 个。压测实测：单次 strict 预检 p50 ~1.5s，3 次全失败 =
    # 4.5s 常速、30s 走满超时；而同一个 3 次预算拿去换 3 个不同候选，比
    # 反复重抽同一批更可能选到一个活的（池里 11/60 是死的）。
    for attempt in range(3):
        chosen = _p2c_select(candidates, dedup_skip, call_map)
        # 预检只在「没有新鲜延迟样本」时做。压测实测：单次 strict 预检 p50
        # 1.5s / p95 4.5s / 最坏 10s，而每次轮换都要付这笔；可延迟样本本来就
        # 已经回答「它此刻连不连得上」（3 分钟内测到的成功即证据）。有样本
        # 就直接切，把那 1.5s 从换线关键路径上拿掉。
        fresh = time.time() - _NODE_DELAY_TS.get(chosen, 0.0) <= _PROBE_SKIP_FRESH_SEC
        probe = _probe_node(chosen, 10000, strict=True) if not fresh else _NODE_DELAYS.get(chosen)
        # 注释与实现不符的老坑：原来这里调 test_node_quick()，它内部就是
        # _probe_node()，返回值被丢掉了，于是 precheck 拿到的延迟白测一次。
        if probe is not None:
            with _state_lock:
                _recent_nodes.append(chosen)
                _NODE_DELAYS[chosen] = probe
                _NODE_DELAY_TS[chosen] = time.time()
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

    blind 模式 = 9router 停用或不跑：库里没有 settings 表，写它只会每 60s 刷
    一条 "no such table: settings"。其余 9router 写路径同样受 _observe_9router() 门控。
    """
    if not _observe_9router():
        return False
    enabled_int = 1 if enabled else 0
    for attempt in range(max(1, attempts)):
        try:
            with _db_lock:
                c = _get_db()
                # 全新 9router 的 settings 表是空的（初始化时 mergeWithDefaults 走内存默认
                # 值，不落行）。纯 UPDATE 在这种情况下静默影响 0 行、json_set 也不报错，
                # 于是「已把 9router 出口指到 7890」是一句假话 —— 实测首次部署后 rowcount=0。
                c.execute("INSERT OR IGNORE INTO settings(id, data) VALUES(1, '{}')")
                cur = c.execute("""
                    UPDATE settings SET data = json_set(COALESCE(data, "{}"),
                        "$.outboundProxyUrl", ?,
                        "$.outboundProxyEnabled", ?
                    ) WHERE id=1
                """, (url, enabled_int))
                c.commit()
                if cur.rowcount == 0:
                    log("设置 outboundProxy 影响 0 行 (settings 无 id=1 行)")
            return True
        except Exception as e:
            if attempt + 1 >= max(1, attempts):
                log(f"设置 outboundProxy 失败（已重试 {attempts} 次）: {e}")
                return False
            time.sleep(0.2 * (attempt + 1))
    return False

def _singbox_pkill_argv() -> list[str]:
    """只杀主 sing-box 的 pkill 参数。纯函数，回归测试直接验它。

    WHY 不能用宽匹配 `^{bin} run`：WARP-WG 线各自跑一份 `sing-box run -c
    .../lineN/singbox.json`，宽匹配会把 17 个进程一起杀掉 —— 每条线重启就换一个
    WARP 会话、换一个出口 IP，台账里的 line→ip 绑定当场作废。
    """
    binary = CFG["singbox"]["binary"]
    cfg = CFG["singbox"]["config_file"]
    return ["pkill", "-f", f"^{_re.escape(binary)} run -c {_re.escape(cfg)}"]


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
        config_file = CFG["singbox"]["config_file"]
        # 只杀主进程。WARP-WG 线各自跑一份 `sing-box run -c .../lineN/singbox.json`，
        # 宽匹配 `^{bin} run` 会把它们一起杀掉（实测 17 个进程全中）：每条线重启
        # 就换一个 WARP 会话、换一个出口 IP，台账里的 line→ip 绑定当场作废，
        # 而且订阅每小时刷新一次就等于每小时churn 一遍全部线路。
        subprocess.run(_singbox_pkill_argv(), capture_output=True, timeout=10)
        time.sleep(2)

        _slot_ports = [s["port"] for s in consumer_slots()] or [MIXED_PORT]
        for _sp in _slot_ports:
            subprocess.run(
                ["fuser", "-k", f"{_sp}/tcp"],
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
            # 启动 slot-2..6 的独立 sing-box 进程（每台独立端口/连接池）
            for _slot in consumer_slots()[1:]:
                _slot_cfg = _slot.get("config_file") or consumer_slot_config_path(
                    config_file, consumer_slots().index(_slot))
                if not os.path.exists(_slot_cfg):
                    log("  slot 配置缺失: %s" % _slot_cfg)
                    continue
                subprocess.Popen(
                    [singbox_bin, "run", "-c", _slot_cfg],
                    stdout=log_file, stderr=subprocess.STDOUT,
                    start_new_session=True)
                log("  启动 slot sing-box: %s port=%d clash=%d"
                    % (_slot["name"], _slot["port"], _slot["clash_api_port"]))
        time.sleep(CFG["singbox"]["startup_grace_sec"])

        if is_singbox_alive():
            with _RESTART_LOCK:
                _RESTART_COUNT = 0
            log("✓ sing-box 重启成功")
            # 恢复代理
            _set_outbound_proxy(url=_expected_proxy_url(), enabled=True)
            if prev_warp and prev_warp != _warp()["members"][0]["tag"]:
                # 不传 ip：sing-box 刚重启，隧道会话已重开，`_ACTIVE_WARP_IP`
                # 是重启前那个会话的地址，对恢复后的这条线不成立。
                _switch_warp(prev_warp)
                log(f"  ↺ WARP 线路恢复为 {prev_warp}")
            elif prev_mode == "proxy" and _warp().get("proxy_fallback_enabled", False):
                # 重启前停在轮换池，重启后 selector 回到 default(WARP-CF)：
                # 不切回去等于静默改变出口模式，落池冷却也就白等了。
                _switch_warp("proxy")
                _ACTIVE_WARP_IP = None   # 会话已重开，旧 IP 不再属于任何线路
            elif prev_mode == "proxy":
                # 落池已禁用：保持 default(WARP-CF)，不把流量送回死池。
                _ACTIVE_WARP_IP = None
                log("  ⚠ 落池已禁用，重启后保持默认 WARP 出口")
            elif prev_mode == "unknown":
                log("  ⚠ 重启前查不到出口模式，selector 停在 default，交由下次换线校正")
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
def _clash_api() -> tuple[str, str] | None:
    """(base_url, secret)；配置里没开 clash_api 就返回 None。"""
    try:
        with open(CFG["singbox"]["config_file"], encoding="utf-8") as f:
            ca = (json.load(f).get("experimental") or {}).get("clash_api") or {}
    except Exception:
        return None
    addr = ca.get("external_controller") or ""
    if not addr:
        return None
    return f"http://{addr}", str(ca.get("secret") or "")


def reload_singbox_config(path: str | None = None, slot: dict | None = None) -> bool:
    """用 Clash API 热重载配置：不杀进程、不关端口、不禁用 outboundProxyUrl。

    实测 sing-box 1.13.0（本机，2026-11-09）：
      PUT /configs?force=true  → 204，耗时 0.01s，50ms 粒度探测 7890 全程 0 次失败。
    而 pkill + 重新 Popen 那条路是 sleep2 + fuser + sleep1 + startup_grace ≈ 13s，
    期间 9router 的每个请求都 ECONNREFUSED → 503。订阅刷新、节点并入这类
    「配置级」动作根本不值得拿出口可用性去换这 13 秒。

    只在**确认进程活着**时用；进程死了必须走 restart_singbox（那才是看门狗的活）。
    """
    if slot is None:
        api = _clash_api()
    else:
        api = ("http://127.0.0.1:%d" % int(slot["clash_api_port"]),
               get_singbox_secret())
    if not api:
        return False
    base, secret = api
    cfg_path = path or ((slot or {}).get("config_file")
                        or CFG["singbox"]["config_file"])
    body = json.dumps({"path": os.path.abspath(cfg_path)}).encode()
    req = urllib.request.Request(
        f"{base}/configs?force=true", data=body, method="PUT",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {secret}"})
    tag = (slot or {}).get("name") or "主配置"
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        log(f"  ✗ {tag} 热重载被拒 HTTP {e.code}；回退整进程重启")
        return False
    except Exception as e:
        log(f"  ✗ {tag} 热重载失败 {type(e).__name__}: {e}；回退整进程重启")
        return False
    if code not in (200, 204):
        log(f"  ✗ {tag} 热重载返回 {code}；回退整进程重启")
        return False
    log(f"✓ {tag} 热重载完成（{int((time.time() - t0) * 1000)}ms，端口未中断）")
    return True


def _reload_config_set(changed: list[tuple[str, str]]) -> bool:
    """只热重载真正变化的配置；任一失败则交给整进程重启兜底。"""
    slots = consumer_slots()
    changed_finals = {f for f, _t in changed}
    ok = True
    for i, slot in enumerate(slots or []):
        final = slot.get("config_file") or consumer_slot_config_path(
            CFG["singbox"]["config_file"], i)
        if final not in changed_finals:
            continue
        if not reload_singbox_config(path=final, slot=slot):
            ok = False
    if not slots and changed:
        ok = reload_singbox_config(path=changed[0][0])
    return ok


def _dormant_tags() -> set[str]:
    """休眠源里的 tag（`.dormant-outbounds.json`）。读不到就当空集。"""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "dormant-outbounds.json")
        with open(p) as f:
            d = json.load(f)
        items = d.get("outbounds") if isinstance(d, dict) else d
        return {x["tag"] for x in items if isinstance(x, dict) and x.get("tag")}
    except Exception:
        return set()


def _count_outbounds(path: str) -> int:
    """数「活订阅还能供出多少条节点」（不存在/损坏/无 selector 返回 0）。

    WHY 不数出站条数（2026-10-28 审查发现）：配置里常驻 65 条休眠出站 + 15 条
    WARP socks，全是常量。按出站条数算，下面那段骤降护栏（new_n < old_n*0.5）
    的闸门被抬到 66，而常量就有 80 条 —— 订阅只剩 1 条节点时 new_n 仍有 81，
    一路绿灯。改成数主 selector 的成员：那才是「能被切过去的东西」的个数。
    """
    try:
        with open(path) as f:
            cfg = json.load(f)
        sel = CFG["singbox"]["selector"]
        dormant = _dormant_tags()
        for o in cfg.get("outbounds", []):
            if o.get("type") == "selector" and o.get("tag") == sel:
                # 只数「来自活订阅」的成员。休眠源（okz 那 65 条）也算成员，
                # 但订阅整个挂掉时它们照样把数撑到 65，护栏就不会触发 ——
                # 那正是这个护栏最该拦住的情形。
                return len([m for m in o.get("outbounds", [])
                            if m != "direct" and m not in dormant])
        return 0
    except Exception:
        return 0

def _pool_members(path: str) -> set[str]:
    """某个配置文件里主池 selector 的节点成员（不含 direct）。"""
    try:
        with open(path) as f:
            cfg = json.load(f)
        sel = CFG["singbox"]["selector"]
        for o in cfg.get("outbounds", []):
            if o.get("type") == "selector" and o.get("tag") == sel:
                return {m for m in o.get("outbounds", []) if m != "direct"}
    except Exception:
        pass
    return set()


def _outbound_signature(path: str) -> list | None:
    """整份配置的出口指纹；读不出来返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None
    outs = cfg.get("outbounds") or []
    return sorted(
        (str(o.get("tag") or ""),
         json.dumps(o, sort_keys=True, ensure_ascii=False))
        for o in outs)


def _config_semantically_equal(a: str, b: str) -> bool:
    """两份配置的出口集合一模一样时返回 True。

    订阅刷新真正决定出口的只有 outbounds 这一块（端口、api、路由都是本地
    生成侧固定写的）。实测 09:00~14:55 的 9 次刷新里 8 次是 169 → 169 同一
    份内容，却每次都 pkill 重启 sing-box，白送 13 秒出口空窗 —— 9router 侧
    就表现为一批 ECONNREFUSED → 503。没变化就不该动进程。
    任一份读不出来一律返回 False：宁可多热载一次，也不能拿解不开的配置当
    「没变化」。
    """
    sa, sb = _outbound_signature(a), _outbound_signature(b)
    return sa is not None and sa == sb


def _log_pool_diff(old_path: str, new_path: str) -> None:
    """刷新前后池子成员的进出，写进 daemon.log。

    WHY：沉睡池让成员可以动态进出，但原来日志里只有一个总数（old_n → new_n），
    事后问「这条什么时候出的池、为什么」答不出来。这里把 tag 和证据一起落。
    """
    before, after = _pool_members(old_path), _pool_members(new_path)
    added, removed = sorted(after - before), sorted(before - after)
    if not added and not removed:
        return
    evid: dict = {}
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               ".dormant_state.json")) as f:
            evid = json.load(f).get("nodes") or {}
    except Exception:
        pass

    def why(tag: str) -> str:
        v = evid.get(tag)
        if not isinstance(v, dict):
            return "无实测记录"
        return "实测通" if v.get("alive") else "实测不通"

    log(f"  池子成员 +{len(added)} / -{len(removed)}")
    if added:
        log("    入池: " + ", ".join(f"{t}({why(t)})" for t in added[:8]))
    if removed:
        log("    出池: " + ", ".join(f"{t}({why(t)})" for t in removed[:8]))


_pool_reject_streak = 0          # 连续被骤降护栏拦住的次数（只用于喊话）


def _read_sub_stats(cfg_path: str) -> dict:
    """读生成侧写在配置文件旁边的 `<cfg>.stats.json`。

    只认"这一次真的生成出来的那份"，所以手工试跑 / pytest 都不会污染线上基线。
    读不到返回空 dict，调用方退回"数池子成员"的旧口径。
    """
    try:
        with open(str(cfg_path) + ".stats.json") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


POOL_WINDOW_SEC = 24 * 3600      # 基线窗口：只认这个时间窗内的样本
POOL_HISTORY = ".pool_history.json"


def _pool_history_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), POOL_HISTORY)


_pool_history_bad = False        # 读失败过一次就置位，护栏会喊话
_stats_missing_streak = 0        # 连续多少轮拿不到统计（护栏等于被拆）


def _read_pool_history() -> list:
    """读样本序列，只保留真实时间窗内的（按每条的 ts 判断）。

    2026-10-28 三轮审查的核心教训：原先写成"峰值 + 每次写刷新 ts"，判断恒真，基线
    变成"自上次中断以来的历史最大"，只涨不落 —— 一次合法的池子腰斩（上游永久少一半
    节点、删掉一个订阅源）就会让好配置被**无限期**拒收，且没有自愈路径。
    """
    try:
        with open(_pool_history_path()) as f:
            d = json.load(f)
        samples = d.get("samples") if isinstance(d, dict) else None
        if not isinstance(samples, list):
            return []
        now = time.time()
        out = []
        for s in samples:
            try:
                ts, pool = float(s["ts"]), int(s["pool"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= now - ts <= POOL_WINDOW_SEC:
                out.append({"ts": ts, "pool": pool})
        return out
    except FileNotFoundError:
        return []                       # 还没跑过刷新，正常
    except Exception as e:
        # 读失败 = 拿不到基线 = 骤降判定恒不成立（静默空转）。必须喊。
        global _pool_history_bad
        if not _pool_history_bad:
            _pool_history_bad = True
            log(f"⚠ 护栏基线读不出来（{e}），骤降判定本轮不生效；"
                f"修法：检查 {POOL_HISTORY} 的属主/权限，或删掉它重建")
        return []


def _pool_baseline():
    """窗口内的最大池子成员数；窗口内没有样本时返回 None（护栏不介入）。"""
    return max((s["pool"] for s in _read_pool_history()), default=None)


def _record_pool_sample(pool: int) -> None:
    """记一条样本。只在**真的 promote 之后**调用，退回全部节点那种轮次不记。"""
    samples = _read_pool_history()
    samples.append({"ts": time.time(), "pool": int(pool)})
    if _side_effects_off():
        return                      # 测试/审查/手工跑不往线上基线里塞样本
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_pool_history_path()),
                                   prefix=".pool_history.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"samples": samples[-2000:]}, f)
            os.replace(tmp, _pool_history_path())
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        log(f"⚠ 护栏样本写不进去（{e}），基线会一直用旧窗口")


# 生成侧写在配置旁边的旁路文件：统计（护栏水位）与节点→订阅源归属。
_SIDECAR_SUFFIXES = (".stats.json", ".sources.json")


def _drop_stats(path: str) -> None:
    """删掉生成侧写在配置旁边的旁路文件（`<cfg>.stats.json` 等）。

    刷新用的是 `<config>.tmp`，旁路文件落在 `<config>.tmp.<suffix>`；配置被
    rename 走之后这份就没人读了，不清会每次刷新留一份（且 sources 里带订阅源
    归属，没必要留）。判据是**父目录里的同名旁路文件**，不是只认 `.tmp` 版本。
    """
    base = str(path)
    for suffix in _SIDECAR_SUFFIXES:
        for p in (base + suffix, base + suffix + ".tmp"):
            try:
                os.unlink(p)
            except OSError:
                pass


def _promote_sidecars(tmp_path: str, final_path: str) -> None:
    """把 `<tmp>.stats.json` / `<tmp>.sources.json` 搬到 `<final>` 旁边。

    `os.replace` 只搬配置本身。不搬的话 `singbox-config.json.sources.json`
    永远停在 `.tmp` 版本上 —— 渠道报表读不到订阅源归属，只能显示 `-`，
    而它恰恰是「这个节点来自哪家机场」的唯一记录（tag 里不含来源）。
    """
    for suffix in _SIDECAR_SUFFIXES:
        try:
            os.replace(tmp_path + suffix, final_path + suffix)
        except OSError:
            # 这一轮**没生成**这份旁路（例如解析器提前退出）。必须删掉上一轮
            # 的旧文件：留着会让下游把旧归属当成当前配置的归属（sources 尤其
            # 糟，tag 相同、机器已换）。删不掉也不该让刷新失败。
            try:
                os.unlink(final_path + suffix)
            except OSError:
                pass


def _discard(path: str):
    _drop_stats(path)
    try:
        os.unlink(path)
    except OSError:
        pass


def _config_set_pairs(main_final: str) -> list[tuple[str, str]]:
    """(最终路径, 草稿路径) 列表；第 0 项永远是主配置。"""
    slots = consumer_slots()
    if not slots:
        return [(main_final, main_final + ".tmp")]
    finals = []
    for i, slot in enumerate(slots):
        final = slot.get("config_file") or consumer_slot_config_path(main_final, i)
        if i == 0:
            final = main_final
        finals.append(final)
    return [(f, f + ".tmp") for f in finals]


def _discard_config_set(main_final: str) -> None:
    for _final, tmp in _config_set_pairs(main_final):
        _discard(tmp)


def _config_set_check_error(main_final: str) -> str:
    """六份草稿全部过 sing-box check 才允许任何一份上线。"""
    for final, tmp in _config_set_pairs(main_final):
        if not os.path.exists(tmp):
            return "缺少槽位草稿: %s" % os.path.basename(tmp)
        err = _config_check_error(tmp)
        if err:
            return "%s: %s" % (os.path.basename(final), err)
    return ""


def _config_set_semantically_equal(main_final: str) -> bool:
    for final, tmp in _config_set_pairs(main_final):
        if not os.path.exists(final) or not _config_semantically_equal(final, tmp):
            return False
    return True


def _promote_config_set(main_final: str) -> list[tuple[str, str]]:
    """成组提升已校验的草稿；任一步失败则回滚已替换文件。

    返回真正发生变化的 (final,tmp) 对，供热重载只重读变化的进程。
    os.replace 跨文件无法做到真正的单事务，但六份都已先通过 check，且这里
    保留旧文件副本，异常时按相反顺序回滚，不会出现「一半新一半旧」长期劈叉。
    """
    pairs = _config_set_pairs(main_final)
    changed = [(f, t) for f, t in pairs
               if not (os.path.exists(f) and _config_semantically_equal(f, t))]
    backups = {}
    replaced = []
    try:
        for final, _tmp in changed:
            rb = final + ".rollback"
            if os.path.exists(final):
                shutil.copy2(final, rb)
                backups[final] = rb
        for final, tmp in changed:
            os.replace(tmp, final)
            replaced.append(final)
    except Exception:
        for final in reversed(replaced):
            rb = backups.get(final)
            try:
                if rb and os.path.exists(rb):
                    os.replace(rb, final)
                elif not rb:
                    os.unlink(final)
            except OSError:
                pass
        raise
    finally:
        for rb in backups.values():
            try:
                os.unlink(rb)
            except OSError:
                pass
    return changed

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
# 追加源在**每次刷新时**现读环境：`asip sub add` 写进 .env 后不必重启 daemon
# （重启会掐断在途 SSE，代价远大于一次 list 拼接）。固定源仍冻结在 import 期，
# 它们来自 config.py，改它们本来就要重启。
def _sub_urls() -> list[str]:
    from config import extra_subscription_urls
    return [u for u in _SUB_URLS + extra_subscription_urls() if u]

_REFRESH_LOCK = threading.Lock()   # 串行化订阅刷新：启动路径与主循环会撞在一起

# ── 新节点自动实测（刷新后触发）──────────────────────────────────
# 用户规则：池子里的节点要么测出 colo 进块，要么根本不该存在。块模型只认
# `.warp/block-map.json` 的 colo，而那份表是**手动跑探测**产出的 —— 实测
# 2026-11-04 订阅里 187 个节点有 112 个从未测过，其中 49 个是新加的源。
# 结果是「节点在池子里，但永远选不上」，日志里没有任何说明。
#
# 这里只做**调度**：真正的探测仍是 `warp_diag.py`（WG）与
# `tools/probe_h2_nodes.py`（H2），判据、设备分配、台账全在它们里面，不复制。
_AUTODIAG_LOCK = threading.Lock()
_AUTODIAG_LAST = 0.0
_H2DIAG_LAST = 0.0
# H2 探测结论台账。与 WG 的分开：同一节点在两条承载上的结论互不代表。
_H2_STATE = os.path.join(_PROJECT, ".warp", "h2_probe_state.json")
# 两次自动探测的间隔。一轮 20 个节点 × 2 轮 ≈ 4~8 分钟，全程占一台设备。
# 取 900s（15min）而不是 30min：49 个待测节点按 20 个/轮要 3 轮才有结论，
# 30min 一轮意味着新订阅源的节点进块要等一个多小时。探测是**串行**的，
# 两轮之间本来就不会重叠；这个间隔只是防「刷新风暴时反复起探测进程」。
_AUTODIAG_MIN_GAP = 900


def _h2_fail_fresh(rec: dict) -> bool:
    """这条失败结论是不是「最近才落的」—— 是则本轮不重测。

    `tested and not ok` 曾被当成永久属性：节点恢复后也永不复测，于是 H2 供给
    只减不增。实测反证：`ddixx-1dd1.dankenet.help:3526-any` 11-05 判失败、
    11-11 实测可用（出口 104.28.211.32），却因「永久失败」永不重测。
    TTL 来自 `warp.h2_probe_fail_ttl_sec`（默认 6h）。
    """
    ts = rec.get("ts")
    if ts is None:
        return False                     # 没有时间戳：当作从未测过，放进队列
    try:
        ttl = float(_warp().get("h2_probe_fail_ttl_sec") or 21600)
    except (TypeError, ValueError):
        ttl = 21600.0
    return (time.time() - float(ts)) < ttl


def _pending_probe_nodes(limit: int = 12,
                         state_path: str | None = None) -> list[dict]:
    """待实测的节点，按源比例取 N 个。

    两条判据**分开**：
      - WG（`state_path=None`）：`block-map` 里没有 colo，且 WG 台账没记过失败
      - H2（`state_path=.warp/h2_probe_state.json`）：同上，用 **H2 自己的**
        台账

    WHY 不能用同一份台账：`block-map` 的 `colo` 是 WG 探测写进去的，而
    `carrier=h2` 是 H2 探测写的。一个节点「有 colo」不代表它测过 H2 ——
    实测 296 个生产节点里只有 61 个有 h2 结论。共用台账会让 H2 探测永远
    认为「已测过」，`carrier=h2` 再也不会增加，H2 池子就死在老节点上。

    **按源分配名额**而不是取前 N 个：同一家机场的节点常常整批同性质，按顺序取
    会让排在前面的源每次都占满名额，新加的源永远轮不上。
    """
    try:
        import warp_blocks
        with open(CFG["singbox"]["config_file"], encoding="utf-8") as f:
            cfg = json.load(f)
        mapped = warp_blocks.load_map()
        with open(CFG["singbox"]["config_file"] + ".sources.json",
                  encoding="utf-8") as f:
            ledger = json.load(f)
    except (OSError, ValueError):
        return []
    # 该路径自己的「测过且失败」台账：失败的不重测（长期属性），成功的靠
    # colo/carrier 判据排掉。
    probed: dict = {}
    if state_path:
        try:
            with open(state_path, encoding="utf-8") as f:
                probed = (json.load(f) or {}).get("nodes") or {}
        except (OSError, ValueError):
            probed = {}
    skip = {"selector", "urltest", "direct", "block", "dns", "socks", "wireguard"}
    by_sub: dict[str, list[dict]] = {}
    for o in cfg.get("outbounds") or []:
        tag = o.get("tag")
        if (not tag or o.get("type") in skip or not o.get("server")
                or not o.get("server_port")):
            continue
        if state_path:
            st = probed.get(tag) or {}
            if st.get("tested"):
                # 测过且**失败**：不立刻重测 —— 再烧一台设备换同一个结论是
                # 浪费。但**不是永久跳过**：失败结论有 TTL（`h2_probe_fail_ttl_sec`），
                # 过期后重新测。承载能力不是节点的永久属性，机场换机、端口重开
                # 都会让昨天挂的节点今天复活；此前永久跳过让 158 个失败节点
                # （157 个超过 24h）永不复测，H2 供给只减不增。
                if not st.get("ok") and _h2_fail_fresh(st):
                    continue
                # 测过且成功：**只有已落进块映射才跳过**。
                # 原来只跳 `tested and not ok`，于是成功过但 merge 丢了的节点
                # 永远占着名额空转（实测 2026-11-08：12 个名额里 6 个是刚测
                # 通过的），而真正的新节点（JNB/IAD/IST/SYD）排不进来 ——
                # `carrier=h2` 长期不增长就是这么来的。
                # 但也不能一刀切跳过全部 tested：`ok=True` 而块映射里没有
                # carrier/h2.colo，说明**合并丢了**（build_map 重建抹字段、
                # merge 没跑完），这种必须重测，否则那个 colo 永远进不了池。
                m = mapped.get(tag) or {}
                if m.get("carrier") == "h2" or (m.get("h2") or {}).get("colo"):
                    continue
        elif (mapped.get(tag) or {}).get("colo"):
            continue                      # WG：已有 colo 就是测过了
        src = (ledger.get(tag) or {}).get("sub") or "?"
        by_sub.setdefault(src, []).append({"tag": tag, "node": o})
    if not by_sub:
        return []
    # 名额按各源**待测数成比例**分配，不是每源一个。
    # 每源一个看着公平，实则把「源的大小」当成噪声：实测 yyd2tett 49 个待测、
    # etcloud 11 个，各给 1 个名额意味着前者要 49 轮（30min 一轮 ≈ 24h）才测完，
    # 而后者 11 轮就空了。按比例分则 49:11 → 12 个名额里拿 9~10 个。
    # 每步取「已取/待测」比例最低的源，等价于最大余数法，且不需要预先算配额。
    picked: list[dict] = []
    taken: dict[str, int] = {s: 0 for s in by_sub}
    while len(picked) < limit:
        avail = [s for s, lst in by_sub.items() if taken[s] < len(lst)]
        if not avail:
            break
        src = min(avail, key=lambda s: (taken[s] / len(by_sub[s]), s))
        picked.append(by_sub[src][taken[src]])
        taken[src] += 1
    return picked


def _ensure_spare_devices(want: int = 2, max_new: int = 3) -> list[int]:
    """探测前补足空闲设备，返回当前空闲槽位。

    没有空闲设备时 `warp_diag.spare_slots()` 直接退出（它不能拿生产线的设备
    去探 —— 两条会话抢同一个 CF 设备身份，双方都掉线）。于是「设备用完」就
    等于「新节点永远测不出 colo、永远进不了块」，而这正是节点大面积不进池的
    原因之一。

    每轮最多补 `max_new` 台：CF 一个窗口约限流 9 台，`register_line` 自带
    30/90/240s 退避，撞上了就等下一轮（`_AUTODIAG_MIN_GAP`=30min），不在
    本地硬撑重试 —— 那只会把限流窗口越拉越长。
    """
    try:
        import warp_diag
        import warp_wg
        registered = warp_diag.registered_slots()
        used = warp_diag.active_slots()
    except Exception as e:
        log(f"  ⚠ 设备台账不可用: {type(e).__name__}: {e}")
        return []
    spare = [s for s in registered if s not in used]
    need = want - len(spare)
    if need <= 0:
        return spare
    start = (max(registered) if registered else 0) + 1
    for i in range(start, start + min(need, max_new)):
        try:
            warp_wg.register_line(i)
            spare.append(i)
            log(f"  ＋ 注册 WARP 设备 {i}（空闲 {len(spare)}）")
        except Exception as e:
            # 多半是 429。退避已在 register_line 里做过，这里只记账不重试。
            log(f"  ⚠ 设备 {i} 注册失败，下轮再试: {type(e).__name__}")
            break
    return spare


def _run_h2_probe(limit: int = 12) -> None:
    """对未测 H2 承载的节点跑一轮实测，并并入 block-map。

    WHY 必须单独一条路径：`warp_diag` 测的是 **WG（UDP 2408）** 承载，
    `probe_h2_nodes` 测的是 **H2（MASQUE over HTTP/2，TCP）** 承载。两条
    承载路径互不代表 —— 实测 22 条 WG 线里 13 条 STUN 不通但 WARP 正常，
    反过来也有 TCP 通而 UDP 被封的。只跑 WG 探测的话 `carrier=h2` 这个
    标记永远不会增加，H2 渠道池就永远是那一批老节点，新订阅的节点一个都
    进不来。这正是「节点只在代理池里、进不了 H2」的根因。

    判据不在这里重写：`--new-only` 的 colo 判据、设备分配、`warp=on` 的
    trace 验证全在 `tools/probe_h2_nodes.py` 里，daemon 只做调度。
    """
    global _H2DIAG_LAST
    with _AUTODIAG_LOCK:
        if time.time() - _H2DIAG_LAST < _AUTODIAG_MIN_GAP:
            return
        _H2DIAG_LAST = time.time()
    # **两边各测各的结论**：H2 与 WG 是两条独立承载，同一节点可能只通一条。
    # 但「H2 还没测过」的筛子要用 H2 自己的台账，不能用 WG 的 —— 否则
    # WG 测过的节点会被认为「测过了」，H2 永远不测它（实测 296 个节点里
    # 只有 61 个有 h2 结论，其余 235 个从没测过 H2）。
    todo = _pending_probe_nodes(limit, state_path=_H2_STATE)
    if not todo:
        return
    out = os.path.join(tempfile.gettempdir(), "asip-h2scan.json")
    log(f"🔬 自动实测 H2 承载 {len(todo)} 个节点")
    import subprocess
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__),
                                          "tools", "probe_h2_nodes.py"),
             "--tags", *[x["tag"] for x in todo], "--out", out, "--rounds", "2"],
            capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            log(f"  ✗ H2 实测退出码 {r.returncode}: "
                f"{(r.stderr or '').strip().splitlines()[-1][:120] if r.stderr else ''}")
            return
        try:
            with open(out, encoding="utf-8") as f:
                rows = json.load(f)
        except (OSError, ValueError):
            return
        ok = [x for x in rows if x.get("ok")]
        if ok:
            m = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__),
                                              "tools", "merge_h2_blockmap.py"),
                 out],
                capture_output=True, text=True, timeout=120)
            log(f"  ✓ H2 实测完成：{len(ok)}/{len(rows)} 可承载，已并入 block-map")
            if m.returncode != 0:
                log(f"  ⚠ H2 合并失败: {(m.stderr or '').strip()[-120:]}")
        else:
            log(f"  · H2 实测完成：{len(rows)} 个节点均未通过")
    except subprocess.TimeoutExpired:
        log("  ✗ H2 自动实测超时（15min），下轮再试")
    except Exception as e:
        log(f"  ✗ H2 自动实测失败: {type(e).__name__}: {e}")
    # 实测刚并入的新 colo 要马上成块成线，否则要等到下一次整点刷新。
    _submit_once("h2sync", _run_h2_sync)


_H2SYNC_LOCK = threading.Lock()
_H2SYNC_LAST = 0.0
_H2SYNC_MIN_GAP = 1800


def _run_h2_sync() -> None:
    """块模型里出现新渠道（新 colo / 同 colo 多一条）就自动补线并安装。

    WHY 要定时做：`_run_h2_probe` 只把实测结论并进 block-map，它不渲染、
    也不装单元。没有这一步的话，新测出的 BOM/FRA/GIG/LHR 永远停在
    「块有了、线没有」—— 2026-11-05 实测 29 条渠道只投产了 19 条。

    动作顺序与手工流程一致（渲染 → 校验 → 装单元 → 重启主 sing-box），
    只是全部自动：渲染器自己会在设备不足时调 h2-add-devices.sh 注册。

    安装要写 /etc/systemd/system，所以提权。**渲染必须在提权之前、以本用户
    跑完**：以 root 跑渲染会把 `.warp/h2/*` 的属主改成 root，daemon 之后
    就读不回来了。所以用 `--no-render` 让安装脚本直接用这份产物。
    """
    global _H2SYNC_LAST
    with _H2SYNC_LOCK:
        if time.time() - _H2SYNC_LAST < _H2SYNC_MIN_GAP:
            return
        _H2SYNC_LAST = time.time()
    proj = _PROJECT
    render = os.path.join(proj, "deploy", "render_h2_lines.py")
    try:
        gap = subprocess.run([sys.executable, render, "--check"],
                             cwd=proj, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"  ⚠ H2 差集检查失败: {type(e).__name__}")
        return
    if gap.returncode == 0:
        return
    if gap.returncode != 1:
        log(f"  ⚠ H2 差集检查异常 rc={gap.returncode}: "
            f"{(gap.stderr or '').strip()[-120:]}")
        return
    added = [x for x in (gap.stdout or "").split() if x]
    log(f"＋ H2 块模型新增 {len(added)} 条渠道（{', '.join(added[:6])}"
        f"{' …' if len(added) > 6 else ''}），自动补线")
    try:
        r = subprocess.run([sys.executable, render], cwd=proj,
                           capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"  ✗ H2 渲染失败: {type(e).__name__}")
        return
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        log(f"  ✗ H2 渲染失败 rc={r.returncode}: {tail[-1][:160] if tail else ''}")
        return
    install = os.path.join(proj, "deploy", "h2-install.sh")
    argv = ["bash", install, "--no-render"]
    if os.geteuid() != 0:
        # 与 `_install_wg_unit` 同一个理由：daemon 自己带着 ProtectSystem=strict，
        # sudo 不换 mount namespace，直接 sudo 写 /etc 会撞只读挂载。
        argv = ["sudo", "-n", "systemd-run", "--pipe", "--wait", "--collect",
                "--quiet", f"--unit=asip-h2-sync-{os.getpid()}-{int(time.time())}",
                *argv]
    try:
        r = subprocess.run(argv, cwd=proj, capture_output=True,
                           text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"  ✗ H2 单元安装失败: {type(e).__name__}")
        return
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        log(f"  ✗ H2 单元安装失败 rc={r.returncode}: {tail[-1][:160] if tail else ''}")
        return
    log(f"  ✓ H2 新线已安装（{len(added)} 条），并入主配置让 selector 看见")
    # 新成员要靠 config.py 在 import 时读台账才进 CFG["warp"]["members"]，
    # 再经 members.py 落进 warp-outbound.json。**必须用子进程**：daemon 里的
    # CFG 是启动时读的，不重读台账。
    # 之后走 patch-wg-outbounds 而不是整轮 refresh_subscription：后者要重拉
    # 全部订阅（几分钟 + 骤降护栏），而这里只多了几条本地出口。
    proj = _PROJECT
    try:
        m = subprocess.run([sys.executable,
                            os.path.join(proj, "deploy", "members.py"),
                            "--write-warp-outbound"],
                           cwd=proj, capture_output=True, text=True, timeout=180)
        if m.returncode != 0:
            log(f"  ✗ 重建 warp-outbound.json 失败 rc={m.returncode}")
            return
        p = subprocess.run([sys.executable,
                            os.path.join(proj, "deploy", "patch-wg-outbounds.py")],
                           cwd=proj, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"  ✗ 主配置并入失败: {type(e).__name__}")
        return
    if p.returncode != 0:
        log(f"  ⚠ 主配置并入失败 rc={p.returncode}: "
            f"{(p.stderr or p.stdout or '').strip()[-120:]}")
        return
    # 别写 `"CHANGED" in stdout`：脚本输出的是 UNCHANGED，子串匹配恒真，
    # 于是「主配置无需并入」也照样重启 sing-box —— 每次白送 13 秒出口空窗。
    words = set((p.stdout or "").split())
    if "CHANGED" in words:
        log(f"  ✓ {(p.stdout or '').strip()}；热重载 sing-box 加载")
        if not reload_singbox_config():
            restart_singbox()
    else:
        log("  · 主配置无需并入")


def _run_autodiag() -> None:
    """对未测节点跑一轮承载实测，并把结论并进 block-map。

    失败不重试、不阻塞主循环：探测是加分项，它挂了不该影响换线。唯一的
    硬要求是**不能占用生产线的设备** —— 这一点由 `warp_diag.spare_slots()`
    保证（它发现没有空闲设备会直接退出）；设备不够时先补注册。
    """
    global _AUTODIAG_LAST
    with _AUTODIAG_LOCK:
        if time.time() - _AUTODIAG_LAST < _AUTODIAG_MIN_GAP:
            return
        _AUTODIAG_LAST = time.time()
    todo = _pending_probe_nodes()
    if not todo:
        return
    if not _ensure_spare_devices():
        log("  · 无空闲设备且补注册失败，本轮不实测（下轮再试）")
        return
    log(f"🔬 自动实测 {len(todo)} 个未入块节点（"
        f"{', '.join(sorted({(x['tag'] or '').split(':')[0] for x in todo}))[:80]}）")
    import subprocess
    out = os.path.join(tempfile.gettempdir(), "asip-autodiag.jsonl")
    job = os.path.join(tempfile.gettempdir(), "asip-autodiag.job.json")
    try:
        with open(job, "w", encoding="utf-8") as f:
            f.write(json.dumps(todo, ensure_ascii=False))
        r = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "warp_diag.py"),
             "--job", job, "--out", out, "--rounds", "2", "--skip-done"],
            capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            log(f"  ✗ 自动实测退出码 {r.returncode}: "
                f"{(r.stderr or '').strip().splitlines()[-1][:120] if r.stderr else ''}")
            return
        ok = 0
        try:
            with open(out, encoding="utf-8") as f:
                for line in f:
                    if json.loads(line).get("ok"):
                        ok += 1
        except (OSError, ValueError):
            pass
        if ok:
            m = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__),
                                              "warp_blocks.py"),
                 "merge-diag", out],
                capture_output=True, text=True, timeout=120)
            log(f"  ✓ 实测完成：{ok}/{len(todo)} 可承载，已并入 block-map")
            if m.returncode != 0:
                log(f"  ⚠ 合并失败: {(m.stderr or '').strip()[-120:]}")
        else:
            log(f"  · 实测完成：{len(todo)} 个节点均未通过，不并入")
    except subprocess.TimeoutExpired:
        log("  ✗ 自动实测超时（15min），下轮再试")
    except Exception as e:
        log(f"  ✗ 自动实测失败: {type(e).__name__}: {e}")


def _rebind_wg_lines_after_refresh() -> None:
    """订阅刷新后，把绑定已失效的 WG 线换到后备节点。

    线路配置内嵌节点 outbound 的副本；订阅换 IP 或改名后旧副本永久连不上，
    而 daemon 只会不停重启一条注定失败的线。
    """
    if not ((_warp().get("wg") or {}).get("enabled")):
        return
    try:
        # 同上：换绑定这条路径在 test_night_window 里以 sys.modules 替换验证。
        import warp_wg
        moved = warp_wg.validate_bindings()
    except Exception as e:
        log(f"✗ WG 线路绑定校验失败: {e}")
        return
    if not moved:
        return
    by_tag = {m["tag"]: m for m in _warp()["members"]}
    for tag in moved:
        m = by_tag.get(tag)
        if not m:
            continue
        # 与其余三条刷新路径同源：夜间锁管的是「重启换地址」这个动作，
        # 换绑定也是一次重启。白名单外的线留到 08:00 之后再换 —— 反正它
        # 绑定的节点已经下架，多冻一夜不影响它本来就不通畅的事实。
        # 逐条取时间：这一批重启可能跨过 08:00，用循环外的一次快照会让
        # 后半段白等一夜（或反向该锁的时候没锁）。
        if _night_block_restart(tag, "订阅刷新换绑定", time.time()):
            continue
        if _restart_service(m["service"], tag):
            log(f"  ⇄ {tag} 绑定节点已下架，已换节点并重启")


def refresh_subscription(reason: str = "") -> bool:
    """拉所有订阅源 + merge 去重 + 生成 sing-box config + 重启 sing-box

    非阻塞获取锁：已有刷新在跑时直接返回 False，不再排队。两个并发刷新
    会写同一个 .tmp（交错内容让 _count_outbounds 读到 0），且第二次
    restart 会 pkill 掉第一次刚恢复的连接。

    `reason` 非空表示这是**修复动作**触发的刷新（目前只有 WARP 出口丢失
    自愈）。它会走完全相同的骤降护栏 —— 2026-11-03 实测：自愈直接跑裸的
    `parse_subscription.py`，绕过了护栏，于是护栏刚判定为可疑的那份产出
    （160 → 28）被原样推上线，池子从 94 掉到 26。
    """
    if not _REFRESH_LOCK.acquire(blocking=False):
        log("⚠ 已有订阅刷新在进行，跳过本次")
        return False
    try:
        return _refresh_subscription_locked(reason)
    finally:
        _REFRESH_LOCK.release()

def _refresh_subscription_locked(reason: str = "") -> bool:
    global _stats_missing_streak
    log(f"🔄 刷新订阅{'（' + reason + '）' if reason else ''}...")
    try:
        parse_script = os.path.join(os.path.dirname(__file__), "parse_subscription.py")
        config_file = CFG["singbox"]["config_file"]
        urls = _sub_urls()
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
            _discard_config_set(config_file)
            return False
        # 生成侧的诊断都走 stderr，以前只转发带 ⚠ 的那几行 —— 于是
        # 「WARP 配置读取失败，跳过」这种**决定性**的一行被静默丢弃，
        # 2026-11-03 远端因此丢了 2.5 小时的 WARP 出口而无任何日志。
        # 正常一轮 stderr 只有 4 行以内（实测），全量转发不会淹掉日志；
        # 生成侧写多少行就转多少行，宁可多一行噪声，不能再漏掉一行故障。
        for ln in (r.stderr or "").splitlines():
            ln = ln.strip()
            if ln:
                log(f"  {ln}")
        # 护栏口径（2026-10-28 三轮审查后定版）：
        #   比「池子实际成员数」的 **24 小时滚动窗口最大值**。
        #   - 比订阅条数会误伤：okz 那 62 条全是死节点，它挂掉时池子一个不少；
        #   - 比"上一次成员数"会随判死一路下移，护栏越守越松；
        #   - 比"峰值 + 每次刷新重写 ts"是单向棘轮：只涨不落，好配置被永久拒收。
        # 样本只在 promote 成功后追加，且按每条自己的 ts 判断是否过期。
        new_stat = _read_sub_stats(tmp_file)
        new_pool = new_stat.get("pool_nodes")
        fallback = bool(new_stat.get("fallback"))
        if isinstance(new_pool, int):
            # 统计读到了就算"这一轮有统计"，不管后面会不会被拒收 ——
            # 否则"连续 N 轮缺统计"会把其实有统计的轮次也算进去，喊话数字偏大。
            _stats_missing_streak = 0
            new_n = new_pool
            base = _pool_baseline()
            old_n = base if base is not None else new_pool
            # 数字关系（实测对得上）：订阅节点 + 仅休眠源 = 在池 + 判死摘出。
            # 19:59 那版写成"订阅节点 = 在池 + 判死 + 仅休眠源"，39+78+3=120≠114，
            # 多算了那 3 条 —— 它们本来就在订阅节点里，只是池子判死表里没有。
            sub_n = new_stat.get('sub_nodes', '?')
            dorm_n = new_stat.get('dormant_source_nodes', '?')
            dead_n = new_stat.get('dead_nodes', '?')
            log(f"  池子成员 {new_n}（订阅节点 {sub_n} + 仅休眠源 {dorm_n}"
                f" = 在池 {new_n} + 判死摘出 {dead_n}"
                f"{'，⚠ 本轮退回全部节点' if fallback else ''}）")
            unjudgeable = False
        else:
            # 统计文件缺失（生成侧没写成功 / 部署了旧版 parse / 被人手工删了）。
            # 这时只能数"出站条数"（135 里 80 条是常量出站），这个口径会被严重
            # 稀释；更危险的是 fallback 轮在这一口径下会被当成正常，把 117 记进
            # 基线 —— 第三轮辛苦修掉的毒化就从这条缝里原样复活。
            # 所以：**这一轮不判定、不记样本**，只喊一句。
            _stats_missing_streak += 1
            extra = ""
            if _stats_missing_streak >= 3:
                # 不记样本 → 24h 后窗口清空 → 基线变 None → 骤降判定恒不成立。
                # 也就是说这道理序会自己把自己拆掉，必须喊到人看见。
                extra = (f"；已连续 {_stats_missing_streak} 轮，"
                         f"**骤降护栏处于失效状态**，再这样下去 24h 后连基线都没了")
            log(f"  ⚠ 统计文件缺失，本轮不做骤降判定，也不记基线样本"
                f"（生成侧没写成功？）{extra}")
            new_n = _count_outbounds(tmp_file)
            old_n = _count_outbounds(config_file)
            fallback = False
            unjudgeable = True
        if new_n == 0:
            log("✗ 刷新产出 0 节点，保留现有配置")
            _discard_config_set(config_file)
            return False
        # 节点数骤降：old_n 为 0（配置缺失/损坏/编码异常）时护栏不可用，
        # 此时若旧配置本来存在，宁可拒绝也不要冒风险覆盖
        if not fallback and not unjudgeable and old_n == 0 and os.path.exists(config_file):
            log("✗ 现有配置不可读（0 节点），无法判断刷新是否合理，保留现有配置")
            _discard_config_set(config_file)
            return False
        if not fallback and not unjudgeable and old_n and new_n < old_n * 0.5:
            global _pool_reject_streak
            # 唯一的让路条件：线上那份配置**根本没有任何 WARP 出口**，而新产出
            # 有。护栏防的是「把好池子换成坏池子」，这里是从「opencode 静默落
            # 机场池」换成「有小池子的可用配置」，严格更好。
            # 2026-11-03 实测：修复动作直接跑裸 parse_subscription，绕过了护栏，
            # 于是 94 → 26 那份产出被原样推上线；同一时刻定时刷新正被护栏拦着。
            if (reason and _config_warp_count(config_file) == 0
                    and _config_warp_count(tmp_file) > 0):
                _pool_reject_streak = 0
                log(f"⚠ 骤降护栏让路（{reason}）：线上配置没有 "
                    f"{_warp()['selector']}，带 WARP 的 {new_n} 条仍优于无 WARP 的 "
                    f"{old_n} 条")
            else:
                _pool_reject_streak += 1
                log(f"✗ 刷新节点数骤降 ({old_n} → {new_n}，基线是 24h 滚动窗口最大值)，"
                    f"疑似订阅源故障或大批节点下线，保留现有配置")
                if _pool_reject_streak >= 4:
                    # 基线可能过时（上游真的永久缩水）。不自动放宽 —— 放宽就等于没有
                    # 护栏；但必须喊出来，人看一眼就知道要不要清掉旧样本。
                    log(f"⚠ 已连续 {_pool_reject_streak} 次被骤降护栏拦住，基线 "
                        f"{old_n} 可能已过时。确认订阅确实缩水了，就删掉 "
                        f"{POOL_HISTORY} 里旧的样本，让它按新水位重建。")
                _discard_config_set(config_file)
                return False
        _pool_reject_streak = 0
        # 最后一道防线：promote 前六份草稿全部用 sing-box 自身校验。单个坏节点
        # （非法 pbk/sid、缺 password 等）会让整个配置 FATAL；只校验主配置时，
        # 槽位 2..6 可能已经上线坏配置，出现主/槽版本与可用性劈叉。
        err = _config_set_check_error(config_file)
        if err:
            log(f"✗ 新配置未通过 sing-box check，保留现有配置: {err}")
            _discard_config_set(config_file)
            return False
        # 池子一个字都没变时**什么都不做**：不覆盖、不重载、不重启。
        # 实测 09:00~14:55 九次刷新里八次是 169 → 169 的同一份内容，却每次都
        # 走 restart_singbox（13 秒出口空窗），9router 侧就是成批 ECONNREFUSED
        # → 503。用户要的「重启换 IP」针对的是出口地址，不是把配置重读一遍。
        if _config_set_semantically_equal(config_file):
            _discard_config_set(config_file)
            if not fallback and not unjudgeable:
                # 样本照记：这份配置确实在跑，基线窗口不该因为「没变化」断档。
                _record_pool_sample(new_n)
            log(f"✓ 订阅刷新完毕（池子成员 {old_n} → {new_n}），"
                f"六份配置均无实质变化，跳过 sing-box 重载")
            _submit_once("autodiag", _run_autodiag)
            _submit_once("h2diag", _run_h2_probe)
            _submit_once("h2sync", _run_h2_sync)
            # 「配置没变」不等于「进程活着」：看门狗和启动路径都会在 sing-box
            # 不通时调本函数，这里若不接住，刷新返回成功而进程永远是死的。
            if not is_singbox_alive():
                log("  · 配置无变化但 sing-box 未运行，改为重启进程")
                return restart_singbox()
            return True
        _log_pool_diff(config_file, tmp_file)
        changed = _promote_config_set(config_file)
        _promote_sidecars(tmp_file, config_file)
        _drop_stats(tmp_file)
        if not fallback and not unjudgeable:
            # 只记"真的上线了"的样本：试跑、被拒的、口径不明的都不进窗口。
            _record_pool_sample(new_n)
        _rebind_wg_lines_after_refresh()
        log(f"✓ 订阅刷新完毕（池子成员 {old_n} → {new_n}），"
            f"成组提升 {len(changed)} 份配置，热重载 sing-box...")
        # 新节点要测出 colo 才进得了块；放在重载之后异步跑，不拖慢刷新。
        # WG 与 H2 是两条独立承载，各测各的：只跑 WG 时 carrier=h2 永远不增加，
        # H2 渠道池就停在那一批老节点上（实测 etcloud 的 18 个活节点因此一直
        # 只在代理池里躺着）。
        _submit_once("autodiag", _run_autodiag)
        _submit_once("h2diag", _run_h2_probe)
        # 实测只更新 block-map；真正把新块变成线的是这条（自限 30min）。
        _submit_once("h2sync", _run_h2_sync)
        # 只重载真正变化的进程：优先 Clash API 热载（端口不断），任一失败就
        # 回退整进程重启（六台一起重建，保证版本一致）。
        if _reload_config_set(changed):
            return True
        return restart_singbox()
    except Exception as e:
        log(f"✗ 订阅刷新失败: {e}")
        _discard_config_set(config_file)   # 超时/异常时清除全部残留（含凭据）
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


def _pool_should_rotate(on_warp: bool, pool_current_dead: bool) -> bool:
    """池该不该做主动轮换。**只表达池自己的需要**，WARP 状态只作为一个事实入参。

    三种情况：
      1. opencode 不在 WARP 上 → 它就是池的流量，必须按时换（保持原行为）
      2. 池当前节点已死 → 非 opencode 的流量（9router 其它 provider）走
         :7890 也是 100% 失败，必须换
      3. 其余情况（opencode 在 WARP、池节点健康）→ 不换：池是兜底资源，
         轮换只是白消耗节点配额

    单独成函数是为了让「池的节奏」与「WARP 的状态」在代码上分开：调用点只
    负责把 WARP 的状态喂进来，判据的演进不会牵动 WARP 换线逻辑。
    """
    return (not on_warp) or pool_current_dead


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


# 上一次「重建 WARP 出口」的时刻，以及连续失败次数。watchdog 每 30s 一轮，
# 没有它会在配置修不好时每轮都重启一次 sing-box。模块级而非闭包：
# `_singbox_watchdog` 由多个线程路径调用，状态必须共享。
_LAST_WARP_REBUILD_TS = 0.0
_WARP_REBUILD_FAILS = 0
# 重建失败后的等待时间，按连续失败次数递增：1min → 2 → 4 → 8 → 封顶 10min。
# 固定 10 分钟是错的：2026-11-03 实测，一次偶发失败（比如 members.py 撞上
# 订阅源超时）会让恢复白等 10 分钟，而它下一分钟就能成功。反过来完全不退避
# 又会每 30s 重启一次 sing-box，把在途 SSE 反复掐断。指数退避两边都照顾到。
_WARP_REBUILD_BACKOFF = (60, 120, 240, 480, 600)


def _config_warp_count(path: str) -> int:
    """一份配置里有几条 WARP 出口（socks 出站）。读不了按 0 条算。

    只用于骤降护栏的让路判据：线上 0 条 + 新产出 >0 条 → 严格更好，不该拦。
    """
    try:
        with open(path) as f:
            obs = json.load(f).get("outbounds") or []
    except (OSError, ValueError):
        return 0
    return sum(1 for o in obs if isinstance(o, dict) and o.get("type") == "socks")


def _warp_selector_missing() -> bool:
    """主配置里 `opencode-out` 是否整个消失，或成员数少得不正常。

    WHY 判这个：探活只看「Clash API 通 + 7890 端口能连」，一份**没有 WARP
    出口**的主配置完全满足这两条 —— 于是 watchdog 认为一切正常，而
    opencode 流量已经静默落到 `final: proxy` 的机场池上。2026-11-03 远端
    就是这样：`warp-outbound.json` 属主被 root 抢走，`parse_subscription.py`
    读到 PermissionError 后生成了一份不含 WARP 的合法配置并 promote 上线，
    daemon 之后 3 小时 24 分每次换线都是 `status=404`（实测 30362 条），
    而 `singbox=🟢` 一直亮着。

    成员数阈值取「有单元在跑的成员数」，不是 `len(members)`：后者含 4 条
    disabled 的线（既定事实第 26 条）。
    """
    status, data = singbox_api(f'/proxies/{_warp()["selector"]}', silent=True)
    if status == 404:
        # sing-box 活着、API 应答正常，但这份配置里没有该 selector ——
        # 正是「主配置丢了 WARP 出口」的形态。
        return True
    if status != 200 or not data:
        return False          # 其它失败（API 不通/5xx）不在这里判
    members = data.get("all") or []
    if not members:
        return True           # selector 在但空着
    # 写进 selector 的线不可能多于 members，少得离谱说明生成端丢了数据。
    # 阈值取声明成员的**半数**，不是四分之一：四分之一在 9 条声明下只有 2，
    # 「骤降到 3」判不出来（回归用例 test_warp_selector_missing_detects_lost_outbounds
    # 期望 3 条触发、50 条不触发）。注意 config 的 members 只是 usque 一类，
    # 运行时 selector 还含 H2/WG 线，实测 20 > 声明 9 —— 所以这是「相对声明」
    # 的粗判据，宁可漏报也不误报。
    return len(members) < max(1, len(_warp()["members"]) * 0.5)


def _rebuild_warp_outbounds_watched() -> bool:
    """调 `_rebuild_warp_outbounds` 并按结果维护退避计数。

    成功一次就把连续失败数清零：下次故障仍从 1 分钟起步，而不是继承上一次
    故障的 10 分钟。计数只反映「最近这串重建到底有没有成功」。
    """
    global _WARP_REBUILD_FAILS
    ok = _rebuild_warp_outbounds()
    # 判据必须是**效果**而不是「命令返回 0」：重建可能全程成功、产出也带 WARP，
    # 但 sing-box 起不来（端口被占、二进制坏了），下一轮 watchdog 照样看到
    # selector 缺失。那种情况退回 60s 起步就成了活锁 —— 每轮都「成功」、
    # 每轮都修不好，退避永远不增长。
    if ok and _warp_selector_missing():
        log("⚠ 重建命令成功，但运行中的配置仍缺 WARP 出口，按失败计")
        ok = False
    _WARP_REBUILD_FAILS = 0 if ok else _WARP_REBUILD_FAILS + 1
    return ok


def _rebuild_warp_outbounds() -> bool:
    """重建 `warp-outbound.json` 并重新生成主配置。

    这是 `_warp_selector_missing` 的修复动作。之所以要重建而不只是重读：
    它的丢失原因通常就是文件读不了（属主/权限），单靠重跑 `parse_subscription`
    只会再造一份没有 WARP 的配置。所以先把属主按当前进程对齐（只有以 root
    运行时才做得了），再依次跑 members.py 与 parse_subscription。

    重建后立刻重新加载：调用方是 watchdog，进程本身是活的，不重启就白改。
    """
    proj = os.path.dirname(os.path.abspath(__file__))
    wo = os.path.join(proj, ".warp", "warp-outbound.json")
    uid = os.getuid()
    if uid == 0:
        # 以 root 跑时才纠属主：非 root 进程 chown 必然失败，只会刷日志。
        try:
            me = pwd.getpwnam(_run_user_name())
            st = os.stat(proj)
            if os.stat(wo).st_uid != st.st_uid:
                os.chown(wo, st.st_uid, st.st_gid)
                log(f"  ⚠ {wo} 属主已按项目目录对齐")
        except (KeyError, OSError) as e:
            log(f"  ⚠ 无法对齐 {wo} 属主: {e}")
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(proj, "deploy", "members.py"),
             "--write-warp-outbound"],
            cwd=proj, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"✗ 重建 warp-outbound.json 失败: {e}")
        return False
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        log(f"✗ 重建 warp-outbound.json 失败 (rc={r.returncode}): "
            f"{tail[-1][:160] if tail else ''}")
        return False
    # 主配置必须走 `refresh_subscription`，不能直接跑 parse_subscription：
    # 后者是裸生成，会绕过骤降护栏。2026-11-03 实测：自愈重建把护栏刚判为
    # 可疑的同一份产出（160 → 28）原样推上线，池子 94 → 26，而同一时刻
    # 定时刷新正被护栏拦着。护栏只在「线上连 WARP 出口都没有」时让路。
    log("✓ warp-outbound.json 已重建，重新生成主配置")
    return refresh_subscription(reason="WARP 出口自愈")


def _run_user_name() -> str:
    """本进程该归属哪个用户：优先 systemd 的 User=，其次当前 euid。"""
    try:
        r = subprocess.run(["systemctl", "show", "auto-switch-ip.service",
                            "-p", "User", "--value"],
                           capture_output=True, text=True, timeout=5)
        name = r.stdout.strip()
        if name:
            return name
    except (OSError, subprocess.TimeoutExpired):
        pass
    return pwd.getpwuid(os.geteuid()).pw_name


def _singbox_watchdog() -> None:
    """探活 → 必要时重启。探测最慢 ~12s、重启要几十秒，只能在后台跑。"""
    global _pool_cooldown_until, _RESTART_COUNT
    if _probe_singbox_with_retry():
        with _RESTART_LOCK:
            if _RESTART_COUNT > 0:
                _RESTART_COUNT = 0
                log("✓ sing-box watchdog 恢复")
        # 进程活着不等于配置是对的：WARP 出口整段消失时，探活照样全绿。
        # 这条必须放在探活之后单独判，否则会被「sing-box 活着」直接 return 掉。
        if _warp_selector_missing():
            # 退避而不是固定冷却：失败一次等 1 分钟再来，连续失败才逐步拉长到
            # 10 分钟。既不会每 30s 重启一次 sing-box（掐断在途 SSE），也不会
            # 让一次偶发失败白等 10 分钟。
            global _LAST_WARP_REBUILD_TS, _WARP_REBUILD_FAILS
            wait = _WARP_REBUILD_BACKOFF[min(_WARP_REBUILD_FAILS,
                                             len(_WARP_REBUILD_BACKOFF) - 1)]
            left = int(_LAST_WARP_REBUILD_TS + wait - time.time())
            if left <= 0:
                # 时间戳只在**真的提交了**才推进：`_submit_once` 遇到同名任务在跑
                # 会返回 False，先记时间戳等于把这次尝试白记掉，下一轮再等满一个
                # 退避周期 —— 而重建可能要几分钟，watchdog 每 30s 一轮，这段空白
                # 里日志只会刷「仍缺失」。
                if _submit_once("rebuild-warp-outbounds",
                                _rebuild_warp_outbounds_watched):
                    _LAST_WARP_REBUILD_TS = time.time()
                    log(f"⚠ 主配置里 {_warp()['selector']} 缺失或成员异常，开始重建"
                        f"（sing-box 进程本身正常，第 {_WARP_REBUILD_FAILS + 1} 次）")
                else:
                    log(f"⚠ {_warp()['selector']} 仍缺失，上一轮重建还在跑")
            else:
                log(f"⚠ {_warp()['selector']} 仍缺失，等重建重试（{left}s）")
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
    log("  换线: 只改 selector 不动在途连接；错误换线额外清理旧链老化连接")
    log("=" * 50)



# ===== 同步轮换接口（/rotate，Unix domain socket）=====
#
# 为什么要有：9router 的两个 proxy pool 内容完全相同（都指向 127.0.0.1:7890），
# 它的 selectAlternateProxy 永远返回 null —— 撞 429 时它自己换不出任何东西，
# 只能就地合成一个 429 回去。真正能改变出口的只有本进程（它持有 selector）。
#
# 不开 TCP：WSL 镜像网络下 127.0.0.1 与 Windows 宿主共享，开端口等于把「换
# 出口」这个动作暴露给宿主上任意进程。UDS 靠文件 0600 + 目录 0700 保护。
_API_LOCK = threading.Lock()
# 换闸互斥：/rotate 的秒切、槽位后台修整、槽位补线、主循环换线可能同时动手。
# 它们改的是同一个 selector / 同一份台账，并发进来会互相覆盖（典型：两个
# 429 同时到，各自挑一条备用线，最后一刻才写回，前一条白白被烧）。
# 用法分两种：前台取失败就**不换**（回 coalesced，让调用方重连），后台取不
# 到就排队等 —— 后台不赶时间，串行反而正确。
_SWITCH_GUARD = threading.Lock()
_API_TOKEN = ""
_last_rotate_ts: float = 0.0
_api_stats = {"calls": 0, "switched": 0, "coalesced": 0, "retired": 0,
              "too_frequent": 0, "no_standby": 0, "failed": 0, "line_ok": 0,
              "divergence": 0, "borrowed": 0}

# 换线前实测台账：tag|ip -> 结论。9router 一次 429 风暴会连着打几十个 /rotate，
# 每个都实测会把换线拖到几分钟，而同一地址同一时刻的结论不会变。
_PROBE_BEFORE_ROTATE = {}

# 探测分歧台账：9router 说这条线不行、asip 实测却 OK 的每一次。这是「差异在
# 请求体/上游口径、不在出口」的直接证据，只靠日志 grep 不方便复盘。
_PROBE_DIVERGENCE = []


def _probe_line_before_rotate(tag, ip, fresh=False):
    """换线前实测「即将被换掉」的这条线还能不能服务。

    9router 认定这条线吃 429 才让切线，但那只是它的观测，可能是它判错了。
    配额一律按出口判定。切之前用 asip 自己的探测器（与生产同一份指纹、同一格
    配额）实测一次。OK 就说明这条线还能用，换掉它等于白烧一条干净线。

    返回 None 表示没有结论（未启用 / 没有当前线 / 探测器缺失）。窗口内复用。
    """
    cfg = _api_cfg()
    if not cfg.get("pre_rotate_probe", True):
        return None
    member = _warp_member(tag)
    if not member or not ip:
        return None
    ttl = float(cfg.get("pre_rotate_probe_ttl_sec", 30))
    key = tag + "|" + ip
    now = time.time()
    hit = _PROBE_BEFORE_ROTATE.get(key)
    # fresh=True：连缓存都不看，真打一次。补线/修整后要确认「这条线到底是不是
    # 真的 quota」，复用 30s 前的 OK 等于拿旧结论给新线盖章。
    if hit and not fresh and now - hit["ts"] < ttl:
        out = dict(hit)
        out["cached"] = True
        return out
    started = time.time()
    retry_after = None
    try:
        import probe_warp
        verdict, sec, detail, retry_after = probe_warp.probe(
            member["port"], float(cfg.get("pre_rotate_probe_timeout_sec", 4)))
    except Exception as e:
        verdict, sec, detail = "unreachable", 0.0, str(e)[:100]
    out = {"tag": tag, "ip": ip, "verdict": verdict, "sec": sec,
           "detail": detail, "retry_after": retry_after,
           "elapsed": round(time.time() - started, 1), "ts": time.time()}
    _PROBE_BEFORE_ROTATE[key] = out
    log("换线前实测 %s 出口=%s: %s %ss" % (tag, ip, verdict, sec))
    return out

# ── 备用线槽位 ───────────────────────────────────────────────────
# 9router 只连 7890（主出口 opencode-out）。6 条槽位是 asip 手里「已探通、
# 随时能顶上去」的备用线。出错时的全部动作在这里完成：
#
#   1. 立刻把主出口切到另一条健康槽位的线（一次 selector PUT，十几毫秒），
#      调用方马上拿到结果去重试，不等换隧道；
#   2. 记下「刚才主出口用的是哪个槽位的线」—— 那个槽位转后台**自己修自己**：
#      先复核自己的线（就是刚被换下的那条），能用就留着标回 ok；不能用就换
#      一条新线、实测、重标。可用性来自「总有一条通着的备用线」，不靠客户端干等。
#
# 关键不变量：_MAIN_SLOT_NAME 永远指向「当前把线借给主出口的那个槽位」。
# 它在被借期间不动（keeper 跳过它、_fast_switch 不会再选它）；它被换下那一刻
# 才变成「原槽位」去修自己，同时 _MAIN_SLOT_NAME 改指新借出的槽位。

_SLOT_HEALTH = {}
# 槽位状态表的读改写锁。RLock：_slot_begin_repair 持锁时还会调 _slot_mark。
_SLOT_STATE_LOCK = threading.RLock()
# daemon 启动时刻。启动时六条槽位都是 unknown，要等各自首轮探测出结论；
# 这段窗口里「没有健康备槽」不等于「闸全废」。main() 里会重新赋值为
# 真正打印启动日志的时刻，避免把模块导入耗时算进宽限期。
_SLOT_BOOT_TS = time.time()
# 当前服务主出口的槽位名。None = 主出口用的是主循环挑的线，不属于任何槽位
# （此时被换下的线按游离线处理，由 _repair_line 复核）。
_MAIN_SLOT_NAME = {"name": None}

# 每个槽位自己的出口变更台账。主路径用 _EXIT_LEDGER 判断「这条 429 追的地址
# 是否已被换走」；槽位各有独立 selector，不能共用主出口的台账，否则 A 槽换线
# 会把 B 槽同 IP 的迟到请求误判成已退休。条目与全局表同构（old/new/at）。
_SLOT_EXIT_LEDGER = {}
_SLOT_LEDGER_MAX = 32
_SLOT_LEDGER_LOCK = threading.Lock()
# 槽位重定向公平轮询。按 consumer_slots 顺序永远先选 slot-1，会把所有失败重试
# 都压到主出口（实测 176/305 涌向 slot-1），形成报告越多、越容易被再次撞坏的
# 级联。这里只记「上一次从哪个位置开始找」，不改槽位数组本身。
_SLOT_REDIRECT_RR = {"index": 0}


def _slot_by_name(name):
    for s in consumer_slots():
        if s["name"] == name:
            return s
    return None


def _slot_current(slot):
    _, data = slot_singbox_api(slot, "/proxies/" + slot["selector"], silent=True)
    tag = (data or {}).get("now")
    if not tag or not _warp_member(tag):
        return None, None
    return tag, _warp_line_ip(tag)


# ── 第七闸口（代理池兜底）──────────────────────────────────────────────
# 六条常规闸口各自挂一条 WARP 线；第七闸口的「线」是轮换池本身。严格兜底
# 的两条硬约束：
#   1. 普通候选阶段绝不参与 —— 它的 warp selector 指向池 selector，
#      _slot_current 永远返回 (None, None)，常规选槽/补闸/修整循环自然
#      跳过它；只有 _pick_healthy_slot 的常规候选集为空时才显式考虑它。
#   2. 9router 的常规端口轮询（ASIP_LOCAL_PROXY_PORT 默认 7890,7892-7896）
#      不含它的端口，流量只能由 /rotate 回包的 switch_to_url 引过来。
# 它报 429/403 的含义也与常规闸口不同：是「这个池节点被烧了」，动作是池内
# 换节点（pick_next_node 同一套判据），绝不换回 WARP 线。
_PROXY_GATE_LAST_USED = {"ts": 0.0}    # 最近一次被指为重试目标/在役的时刻
_PROXY_GATE_LAST_ROTATE = {"ts": 0.0}  # 最近一次池内换节点时刻


def _is_proxy_gate(slot) -> bool:
    return bool(slot) and slot.get("kind") == "proxy"


def _proxy_gate_slot():
    for s in consumer_slots():
        if s.get("kind") == "proxy":
            return s
    return None


def _pool_selector_name() -> str:
    return CFG["singbox"]["selector"]


def _proxy_gate_current(slot):
    """第七闸口当前挂的池节点 (tag, None)。进程挂/读不到返回 (None, None)。

    ip 一栏刻意为 None：池节点的出口地址由 node_info 台账按需实测，不像
    WARP 线那样有线级 ip 缓存可读；调用方拿 tag 已足够记账与展示。
    """
    if not _is_proxy_gate(slot):
        return None, None
    _, data = slot_singbox_api(slot, "/proxies/" + slot["pool_selector"],
                                silent=True)
    tag = (data or {}).get("now")
    if not tag or tag == "direct":
        return None, None
    return tag, None


def _proxy_gate_nodes(slot):
    """第七闸口自己的池 selector：(nodes, current)。查不到返回 ([], None)。

    注意节点名与主进程池 selector 完全同名同序（同一份订阅渲染），所以
    _DEAD_NODES / _NODE_DELAYS 等池状态对它同样成立。
    """
    _, data = slot_singbox_api(slot, "/proxies/" + slot["pool_selector"],
                                silent=True)
    if not data:
        return [], None
    nodes = [x for x in (data.get("all") or []) if _is_pool_node(x)]
    return nodes, data.get("now")


def _slot_note_switch(slot, old_tag, old_ip, new_tag, new_ip, ts):
    """记一次槽位 selector 的**真实**变更，并维护迟到请求判断所需的台账。

    同 tag 同 IP 的 PUT 不记：那只是重选当前线，没有切断任何请求；若把它当
    「换线」，之后所有比它更早发出的 429 都会被误判成 already_retired，坏线
    反而得不到修整。
    """
    name = slot["name"]
    h = _slot_health(name)
    h["tag"] = new_tag
    h["egress"] = new_ip
    h["switched_at"] = float(ts)
    if old_tag == new_tag and old_ip == new_ip:
        return
    # 排空台账（2026-11-11）：旧线从此刻起「退役」，排空后才允许回收重启；
    # 新入选的线撤销其退役时间戳；被换走即「判坏但在槽」等到的结局，清账。
    with _DRAIN_LOCK:
        if old_tag and old_tag != new_tag:
            _RETIRED_AT[old_tag] = float(ts)
            _DRAIN_PENDING.pop(old_tag, None)
            _BAD_ON_SLOT.pop(old_tag, None)
        _RETIRED_AT.pop(new_tag, None)
    with _SLOT_LEDGER_LOCK:
        rows = _SLOT_EXIT_LEDGER.setdefault(name, [])
        rows.append({"at": float(ts), "old": old_ip, "new": new_ip,
                     "tag": new_tag})
        if len(rows) > _SLOT_LEDGER_MAX:
            del rows[:-_SLOT_LEDGER_MAX]


def _slot_ip_is_retired(name, ip, request_started_at):
    """这条槽位的请求地址，是否在该请求发出之后被换走。"""
    if not ip or not request_started_at or request_started_at <= 0:
        return False
    with _SLOT_LEDGER_LOCK:
        return any(e["old"] == ip and e["at"] > request_started_at
                   for e in _SLOT_EXIT_LEDGER.get(name, ()))


def _is_main_slot(slot):
    slots = consumer_slots()
    return bool(slots) and slot.get("name") == slots[0].get("name")


def _record_main_slot_switch(slot, old_tag, old_ip, new_tag, new_ip, ts):
    """slot-1 就是主出口进程：它的 selector 变更必须补主路径的全套记账。

    绕过这一层时，`_ACTIVE_WARP_IP` / 出口台账 / `_warp_line_selected` 都停在
    旧线上，迟到 429 会认不出地址已退休，又去烧下一条。与 `_fast_switch` 保持
    同一顺序：先给主台账记旧→新，再更新进程级当前出口。
    """
    global _ACTIVE_WARP_IP, _proxy_dwell_since
    _exit_ledger_note(old_ip, new_ip, ts, new_tag)
    _mark_warp_ip_used(new_ip, ts)
    _warp_line_selected(new_tag, new_ip, ts)
    _set_warp_line_ip(new_tag, new_ip)
    _ACTIVE_WARP_IP = new_ip
    _begin_segment(ts)
    _proxy_dwell_since = 0
    # 主进程自己换了线，主出口不再借用其他槽位。
    _MAIN_SLOT_NAME["name"] = slot["name"]
    log("⚡ 主槽位换线：%s → %s（出口=%s，槽位=%s）"
        % (old_tag or "未知", new_tag, new_ip or "未知", slot["name"]))


def _slot_put(slot, tag):
    old_tag, old_ip = _slot_current(slot)
    r, _ = slot_singbox_api(slot, "/proxies/" + slot["selector"], method="PUT",
                            body={"name": tag})
    if r not in (200, 204):
        return False
    _publish_clash_snapshot(slot["selector"], {"now": tag})
    new_ip = _warp_line_ip(tag)
    switched_at = time.time()
    _slot_note_switch(slot, old_tag, old_ip, tag, new_ip, switched_at)
    if _is_main_slot(slot):
        _record_main_slot_switch(slot, old_tag, old_ip, tag, new_ip, switched_at)
    return True


def _slot_peers():
    """已被占用的线名：主出口 + 所有槽位当前指向的线。"""
    used = set()
    tag = active_warp_tag()
    if tag:
        used.add(tag)
    for s in consumer_slots():
        t, _ip = _slot_current(s)
        if t:
            used.add(t)
    return used


def _slot_line_owners():
    """tag -> [槽位名]：用于发现两个槽位压同一条线。"""
    owners = {}
    for sl in consumer_slots():
        tag, _ip = _slot_current(sl)
        if tag:
            owners.setdefault(tag, []).append(sl["name"])
    return owners


def _dedupe_slots():
    """把重复指向同一条线的槽位（保留一个）标成 bad，交给补齐流程重挑。

    重复的代价很直接：两个槽位压同一条线，等于少一个备份；那条线一坏，
    两个闸口同时失效，秒切能去的地方少一个。服务中的槽位优先保留，修整中
    的槽位不动（它正在换自己的线，动了会互相覆盖）。
    """
    owners = _slot_line_owners()
    serving = _slot_serving_name()
    dropped = []
    for tag, names in owners.items():
        if len(names) < 2:
            continue
        keep = serving if serving in names else names[0]
        for n in names:
            if n == keep:
                continue
            if _slot_health(n)["status"] == "repairing":
                continue
            _slot_mark(n, "bad", "duplicate")
            log("⚠ 槽位 %s 与 %s 重复指向 %s，已标不可用待重挑" % (n, keep, tag))
            dropped.append(n)
    return dropped


def _slot_pick(slot, avoid, now=None):
    """按主出口同一套判据挑线：新鲜、未被封、且不在 avoid 里。"""
    now = now or time.time()
    cur, _ip = _slot_current(slot)
    for m, mip in _warp_line_fresh_candidates(cur, now):
        if m["tag"] in avoid:
            continue
        if mip and _warp_ip_is_blocked(mip, now):
            continue
        return m["tag"]
    return None


# 槽位选线串行锁：多个闸口同时被 429 触发修整时，各自在切换**前**算的
# avoid 快照里看不到对方即将选中的线，会同时挑中同一条（实测 slot-3 与
# slot-6 都落到 WARP-H2-SIN-5、同一个出口 IP，6 个备份实际只剩 5 个）。
# 挑线和 PUT 必须在同一个临界区里，且每次都在锁内重算占用表。
_SLOT_PICK_LOCK = threading.Lock()


def _slot_claim(slot, extra_avoid=None):
    """在锁内挑一条「此刻没被任何其他闸口/主出口占用」的线并切过去。

    返回 (tag, ip)；没有候选或切换失败返回 (None, None)。
    """
    with _SLOT_PICK_LOCK:
        cur, _ = _slot_current(slot)
        # 只有「其他闸口占用表」里的当前线可以放行（允许原地重挑）；调用方
        # extra_avoid 里的当前线往往是刚实测失败的线，绝不能跟着一起 discard。
        # 旧写法先合并再 discard，会把 _repair_slot 已排除的失败线重新放回候选，
        # 候选生成器又持续返回它时就会形成无边界 PUT/探测循环。
        occupied = _slot_peers()
        occupied.discard(cur or "")
        avoid = set(extra_avoid or ()) | occupied
        for _ in range(6):
            pick = _slot_pick(slot, avoid)
            if not pick:
                return None, None
            if _slot_put(slot, pick):
                return pick, _warp_line_ip(pick)
            avoid.add(pick)
    return None, None


def _slot_health(name):
    # 必须返回表里的那一份，不能返回同等内容的临时 dict：调用方会往里写
    # last_rotate_ts 之类的限速事实，写到临时对象上等于静默丢弃。
    with _SLOT_STATE_LOCK:
        return _SLOT_HEALTH.setdefault(
            name, {"status": "unknown", "verdict": None, "ts": 0.0,
                   "switched_at": 0.0, "tag": None, "egress": None})


def _slot_begin_repair(name, reason=None):
    """原子地把「非 repairing」标成 repairing；True 表示本次由我认领修复。

    check-then-mark 拆成两步时，两条并发收敛（同一批重复槽位各修完一个）
    会同时看到同一个坏槽是 bad，各起一条修整线程；后进 _slot_claim 的那条
    会把前一条刚探通的线再换掉 —— 白烧一条线，还打断已就绪的连接池。
    """
    with _SLOT_STATE_LOCK:
        if _slot_health(name)["status"] == "repairing":
            return False
        _slot_mark(name, "repairing", reason)
        return True


def _slot_mark(name, status, verdict=None):
    # 必须保留 switched_at/tag/egress：它们不是状态，是「这条请求发出后是否
    # 已经换过线」的事实；每次 mark 都重建 dict 会把事实清空，于是迟到 429
    # 再次变成无法识别，刚修好的线被反复记脏。
    with _SLOT_STATE_LOCK:
        h = _SLOT_HEALTH.setdefault(name, {"status": "unknown", "verdict": None,
                                           "ts": 0.0})
        prev = h.get("status")
        h.update({"status": status, "verdict": verdict, "ts": time.time()})
    if prev != status:
        log("· 槽位 %s 状态 %s → %s%s"
            % (name, prev or "unknown", status,
               ("（%s）" % verdict) if verdict else ""))


def _slot_serving_name():
    """当前把线借给主出口的槽位名。"""
    return _MAIN_SLOT_NAME["name"]


def assign_consumer_slots(force=False):
    """给每条槽位一条独立的线；新上线的线必须实测通过才标 ok。

    跳过两类：(a) 正在服务主出口的槽位（_MAIN_SLOT_NAME）—— 它的线就是主出口
    的线，动了等于在主出口脚下抽梯子；(b) repairing 的槽位 —— 原槽位正在后台
    修自己，keeper 不该和它抢同一个 selector。
    """
    slots = consumer_slots()
    if not slots:
        return
    serving = _slot_serving_name()
    state = {s["name"]: _slot_current(s) for s in slots}
    used = set()
    if active_warp_tag():
        used.add(active_warp_tag())
    for tag, _ip in state.values():
        if tag:
            used.add(tag)
    for s in slots:
        name = s["name"]
        if _is_proxy_gate(s):
            # 第七闸口的「线」是池节点，不参与 WARP 线认领；健康与轮换由
            # _proxy_gate_keeper 维护。
            continue
        if name == serving:
            continue  # 它的线在服务主出口，别动
        tag, ip = state[name]
        h = _slot_health(name)
        if h["status"] == "repairing":
            continue  # 原槽位在后台修自己，别抢
        if (tag and ip and not force and not _warp_ip_is_blocked(ip)
                and h["status"] in ("ok", "unknown")):
            if h["status"] == "unknown" or _slot_should_probe(name):
                # 两件事一起做：确认这条线此刻真能服务，并把延迟读数写进状态表
                # （选槽排序的第二判据）。命中 30s 探测缓存则不重复烧额度。
                h["probe_try_ts"] = time.time()
                probed = _probe_line_for_slots(tag, ip)
                verdict = (probed or {}).get("verdict")
                if verdict == "OK":
                    _slot_mark(name, "ok")
                    _slot_note_probe(name, probed)
                    continue
                if verdict in ("quota", "flagged"):
                    # 上游明确拒绝：这条线此刻不可用，交给下面 claim 换一条。
                    _slot_mark(name, "bad", verdict)
                    continue
                # unreachable：12s 内没拿到结论，慢线握手超时也会这样。不足以
                # 判死，保留原状态，60s 后再试。
                if h["status"] == "unknown":
                    _slot_mark(name, "bad", verdict or "no_verdict")
                continue
            continue
        pick, ip2 = _slot_claim(s)
        if not pick:
            _slot_mark(name, "bad", "no_candidate")
            continue
        probed = _probe_line_for_slots(pick, ip2)
        verdict = (probed or {}).get("verdict")
        _slot_mark(name, "ok" if verdict == "OK" else "bad", verdict)
        if verdict == "OK":
            _slot_note_probe(name, probed)
        log("⇄ 槽位 %s → %s：%s（%ss）"
            % (name, pick, verdict, (probed or {}).get("sec")))


def _channel_verdict_snapshot(tag: str, ip: str | None) -> dict:
    """读渠道级已有结论，代替闸口切换时的一次实测。

    WHY 不在这里打请求：闸口的调用点在 `_slot_put` 之后（`_slot_claim`
    内部已完成 selector PUT），探测晚于切换生效 —— 既否决不了这次选择，也
    回滚不了，只能事后补一个状态标签。而候选在进闸口前已由
    `_warp_line_fresh_candidates` 要求「当前地址有新鲜 OK」，prober 又按同一
    判据跳过这条线（`_warp_line_needs_probe`）。再打一次，等于把两个组件
    都判定「不必再测」的那一对重测一遍，代价是一格配额。

    返回结构与 `_probe_line_before_rotate` 对齐（verdict/sec/cached），
    调用方的 `_slot_mark` / `_slot_note_probe` 无需改动。没有结论时给
    unknown，与实测超时同义：不判死，下一轮再看。
    """
    st = _warp_line_state(tag) if tag else {}
    sec = st.get("last_probe_sec")
    if not isinstance(sec, (int, float)) or sec <= 0:
        sec = None
    if ip and st.get("ip") == ip:
        last_ok = float(st.get("last_ok_ts") or 0.0)
        if (st.get("last_ok_ip") == ip
                and time.time() - last_ok <= _line_fresh_sec()):
            return {"tag": tag, "ip": ip, "verdict": "OK", "sec": sec,
                    "cached": True, "source": "channel"}
        if st.get("last_verdict_ip") == ip and st.get("last_verdict"):
            return {"tag": tag, "ip": ip, "verdict": st["last_verdict"],
                    "sec": sec, "cached": True, "source": "channel"}
    return {"tag": tag, "ip": ip, "verdict": "unknown", "sec": sec,
            "cached": True, "source": "channel"}


def _probe_line_for_slots(tag, ip, fresh=False):
    """探测一条线，并把 OK 的结论用于补坏闸 / 同步闸状态。

    探测是补闸的唯一引擎（没有轮询）：
      - OK 且这条线不在任何闸内、也不在主出口 → 马上塞进一个坏闸（除非没有坏闸）
      - OK 且在某个闸内 → 该闸状态跟着改 ok（状态变了才动）
      - 非 OK 且在某个闸内 → 该闸状态跟着改 bad
    修整中 (repairing) 和服务中 (serving) 的闸不在这改 —— 它们由 _repair_slot /
    主出口自己管。
    """
    cfg = _api_cfg()
    if cfg.get("slot_switch_probe", False):
        # 旧行为：闸口切换时实测一次。保留开关，便于对照与回退。
        probed = _probe_line_before_rotate(tag, ip, fresh)
    else:
        probed = _channel_verdict_snapshot(tag, ip)
    verdict = (probed or {}).get("verdict")
    if not tag:
        return probed
    holder = None
    for s in consumer_slots():
        st, _si = _slot_current(s)
        if st == tag:
            holder = s
            break
    serving = _slot_serving_name()
    # 状态同步：在闸内、非修整、非服务 → 跟探测结论改
    if holder is not None and holder["name"] != serving:
        h = _slot_health(holder["name"])
        if h["status"] != "repairing":
            want = "ok" if verdict == "OK" else "bad"
            if h["status"] != want:
                _slot_mark(holder["name"], want, verdict)
    # 补闸：OK、不在任何闸内、不是主出口 → 塞进一个坏闸
    if verdict == "OK" and holder is None and tag != active_warp_tag():
        for s in consumer_slots():
            if _is_proxy_gate(s):
                continue  # 池闸不收 WARP 线：塞了线它就不再是纯池闸
            if s["name"] == serving:
                continue
            if _slot_health(s["name"])["status"] != "bad":
                continue
            with _SLOT_PICK_LOCK:
                # 锁内重查：这条线可能刚被别的闸口拿走，也可能主出口正在用。
                busy = tag == active_warp_tag() or any(
                    (_slot_current(x)[0] == tag)
                    for x in consumer_slots() if x["name"] != s["name"])
                if busy:
                    break
                if not _slot_put(s, tag):
                    continue
            _slot_mark(s["name"], "ok", "OK")
            _slot_note_probe(s["name"], probed)
            log("＋ 探测补闸：%s → %s" % (tag, s["name"]))
            break
    return probed


def _converge_duplicate_slots(exclude=None):
    """一次修整结束后立刻收敛重复指向，不再依赖已删除的 15s 巡检。

    只对 **这次修整之外** 的重复槽位启动一次后台修整，避免自己修完仍是重复
    又无限自我重启；自己的重复状态留给下一轮探测或下次修整处理。
    """
    for name in _dedupe_slots():
        if name == exclude:
            continue
        slot = _slot_by_name(name)
        if slot is None or _slot_health(name)["status"] != "bad":
            continue
        if not _slot_begin_repair(name, "duplicate"):
            continue
        threading.Thread(target=_repair_slot, args=(slot, "duplicate"),
                         daemon=True, name="repair-" + name).start()


def _repair_slot(slot, reason):
    """修整包装：真正换线后做一次事件驱动的重复槽位收敛。"""
    try:
        return _repair_slot_impl(slot, reason)
    finally:
        try:
            _converge_duplicate_slots(exclude=slot.get("name"))
        except Exception as e:
            log("⚠ 槽位重复收敛失败: %s" % e)


def _repair_slot_impl(slot, reason):
    """错误轮换时原槽位修自己：换掉自己的线，探通才标 ok。

    9router 已判 429 —— 不再「探一下通就留着」。换一条新线，探通才标 ok；
    探不通就记脏这条、继续找下一条，直到探通或候选耗尽。这是错误轮换的
    一部分（顺便把自己修好），其余场景闸口不主动动自己的线。
    """
    name = slot["name"]
    tag, ip = _slot_current(slot)
    if not tag:
        _slot_mark(name, "bad", "no_line")
        return
    if reason not in ("proactive", "transport") and ip:
        _mark_warp_ip_used(ip, time.time(), blocked=True, kind=reason)
    # 错误切换必须真的换线。不能只靠候选生成器隐式排除当前线：IP 未知时旧线
    # 没被打上脏标记，生产里已经出现过「429 后错误切换 A → A」。把线名放进
    # extra_avoid，它在整个重挑周期内都不会被 _slot_claim 放行。
    avoid = {tag}
    tried = 0
    while True:
        pick, new_ip = _slot_claim(slot, avoid)
        if not pick:
            _slot_mark(name, "bad", "no_candidate" if tried == 0 else "all_tried")
            log("✗ 槽位 %s 错误切换失败（试 %d 条都不通）" % (name, tried))
            return
        probed = _probe_line_for_slots(pick, new_ip)
        verdict = (probed or {}).get("verdict")
        if verdict == "OK":
            _slot_mark(name, "ok", "OK")
            _slot_note_probe(name, probed)
            log("↻ 槽位 %s 错误切换：%s → %s，实测 OK（%ss，试 %d 条）"
                % (name, tag, pick, probed.get("sec"), tried + 1))
            return
        # 只有上游明确拒绝（quota / flagged）才把这条线记脏。unreachable 是
        # 「12 秒内没拿到结论」——慢线握手超时也会这样，记脏等于把好线也封了，
        # 候选会被快速耗光。没结论就只在本轮排除，不进全局惩罚表。
        if verdict in ("quota", "flagged") and new_ip:
            _mark_warp_ip_used(new_ip, time.time(), blocked=True, kind=reason)
        avoid.add(pick)
        tried += 1
        log("· 槽位 %s 试线 %s 不通（%s），换下一条" % (name, pick, verdict))


def _repair_line(tag, ip, reason):
    """复核游离线：主出口之前用的是主循环挑的线（_MAIN_SLOT_NAME 为 None），
    被换下后不属于任何槽位。OK 的线由 _probe_line_for_slots 自动塞进坏闸；
    被拒就记脏。这里只补「探测分歧」证据。
    """
    if not tag:
        return
    probed = _probe_line_for_slots(tag, ip)
    verdict = (probed or {}).get("verdict")
    if verdict == "OK":
        _api_stats["divergence"] += 1
        _PROBE_DIVERGENCE.append({
            "ts": time.time(), "reason": reason, "tag": tag, "ip": ip,
            "probe_sec": probed.get("sec"),
            "elapsed": probed.get("elapsed"),
            "cached": bool(probed.get("cached"))})
        del _PROBE_DIVERGENCE[:-50]
        log("⚠ 探测分歧: 9router=%s 但 asip 实测 %s 出口=%s OK（%ss），已补闸"
            % (reason, tag, ip or "未知", probed.get("sec")))
        return
    if verdict in ("quota", "flagged") and ip:
        _mark_warp_ip_used(ip, time.time(), blocked=True, kind=reason)
    log("· 坏线复核 %s 出口=%s：%s" % (tag, ip or "未知", verdict))


def _slot_snapshot():
    """槽位状态：谁在服务、指向哪条线、延迟、最近一次实测结论。"""
    now = time.time()
    active = active_warp_tag()
    serving = _slot_serving_name()
    out = []
    for s in consumer_slots():
        if _is_proxy_gate(s):
            # 池闸的「当前线」是它挂的池节点；9router 侧的 /slots 解析只读
            # name/port/tag/egress，多出来的 kind 是给人看的。
            tag, ip = _proxy_gate_current(s)
        else:
            tag, ip = _slot_current(s)
        h = _slot_health(s["name"])
        probe = _PROBE_BEFORE_ROTATE.get("%s|%s" % (tag, ip)) if (tag and ip) else None
        out.append({
            "name": s["name"],
            "selector": s["selector"],
            "kind": s.get("kind") or "warp",
            # 端口是 9router 侧唯一稳定的槽位键：它只知道 127.0.0.1:789N，
            # 不知道 slot-N。/slots 不吐端口的话，客户端只能靠数组下标猜，
            # 而 consumer_slots() 的端口跳过 7891，下标与端口不是同一套编号。
            "port": s.get("port"),
            "status": h["status"],
            "status_age_s": round(now - h["ts"], 1) if h.get("ts") else None,
            "verdict": h.get("verdict"),
            "tag": tag,
            "egress": ip,
            "serving": s["name"] == serving,
            "probe_sec": (probe or {}).get("sec"),
            "probe_verdict": (probe or {}).get("verdict"),
            "probe_age_s": round(now - probe["ts"], 1) if probe else None,
        })
    return out


def cli_slots(argv=None):
    """命令行查看槽位状态（读运行中 daemon 的 /slots）。"""
    import subprocess
    cfg = _api_cfg()
    tok_path = cfg.get("token_path")
    sock = cfg.get("socket_path")
    try:
        tok = open(tok_path, encoding="utf-8").read().strip()
    except OSError:
        print("读不到 token：%s（daemon 没跑过？）" % tok_path)
        return 1
    out = subprocess.run(
        ["curl", "-s", "--unix-socket", sock, "-H", "X-Asip-Token: " + tok,
         "http://localhost/slots"], capture_output=True, text=True).stdout
    try:
        data = json.loads(out)
    except Exception:
        print("接口无响应：%s" % (out or "empty")[:200])
        return 1
    rows = data.get("slots") or []
    print("%-8s %-9s %-8s %-22s %-42s %6s %6s %s"
          % ("槽位", "状态", "结论", "节点", "出口", "实测s", "年龄s", "服务中"))
    for r in rows:
        print("%-8s %-9s %-8s %-22s %-42s %6s %6s %s"
              % (r["name"], r["status"], r.get("verdict") or "未测",
                 r.get("tag") or "-", r.get("egress") or "-",
                 "-" if r.get("probe_sec") is None else r["probe_sec"],
                 "-" if r.get("probe_age_s") is None else r["probe_age_s"],
                 "是" if r.get("serving") else ""))
    return 0


def _kick_background_repair(tag, ip, reason):
    """把刚被报 429 的那条线所属的槽位，扔到后台做错误切换。

    由 _rotate_once 在**回包定型之后**用 finally 启动：秒切挑线与后台换线
    都要读同一份槽位/线状态表，后台线程若在秒切之前就跑起来换线、探新线、
    放回池子，秒切挑中的可能正好是后台刚标脏的那条，或者读到过时快照。
    放在 finally 里既能保证串行，又不会被 coalesced / too_frequent 这些提前
    return 漏掉；秒切失败同样走这里，回包里的 restart_required 只负责让
    9router 重建连接池，不代替修旧闸。只动「selector 当前指向 tag」的那个
    槽位（状态漂移时不认错）；已经在修整中的槽位不重复启动。
    """
    if not tag:
        return
    owner_name = _slot_serving_name()
    owner = _slot_by_name(owner_name) if owner_name else None
    if owner is not None:
        ot, _oip = _slot_current(owner)
        if ot != tag:
            owner = None
    if owner is not None:
        if not _slot_begin_repair(owner["name"], reason):
            return
        threading.Thread(target=_repair_slot, args=(owner, reason),
                         daemon=True, name="repair-" + owner["name"]).start()
    else:
        threading.Thread(target=_repair_line, args=(tag, ip, reason),
                         daemon=True, name="repair-line").start()


def _slot_ip_is_quarantined(ip: str | None,
                            now: float | None = None) -> bool:
    """这个出口地址是否已判不可用（生产标脏 / 探针 quota / flagged）。

    秒切选目标以前只看「槽位健康」，与地址额度无关 —— 21:22 撞 429 的那个
    /128 在随后 6 分 47 秒里被复用 ≥4 次，就是这条缺口。额度封禁按地址计，
    过滤也必须按地址做：生产 429 的标脏落在 _WARP_IP_USED（含 ban_until），
    探针结论落在 _WARP_PROBE_SEEN / _PROBE_BEFORE_ROTATE，两个来源都要认。
    只用于过滤，不改写探测台账，诊断字段「实测/标脏后」的语义保持不变。
    """
    if not ip:
        return False
    if now is None:
        now = time.time()
    try:
        if _warp_ip_is_blocked(ip, now):
            return True
    except Exception:
        pass
    try:
        v = _probe_verdict_for_ip(ip, now)
        return v in ("quota", "flagged")
    except Exception:
        return False


def _slot_note_probe(name, probed):
    """把这次探测的实际耗时记到槽位状态里，供下一次选槽按延迟排序。

    用 probe_warp 量到的真实请求耗时（含 TLS 握手），不是 ICMP —— 与生产走
    同一条代理路径，比 ping 更接近用户看到的延迟。
    """
    if not isinstance(probed, dict):
        return
    sec = probed.get("sec")
    if not isinstance(sec, (int, float)) or sec <= 0:
        return
    with _SLOT_STATE_LOCK:
        h = _slot_health(name)
        h["probe_sec"] = float(sec)
        h["probe_ts"] = time.time()


def _slot_should_probe(name):
    """该槽位是否值得补一次延迟读数（缺失或过期），60s 内不重复试探。

    补测频率受两个因素约束：只有「没有读数」时才补（有读数就等它过期），
    以及同一条线 60s 内最多试一次（失败也要等下轮，别把慢线打成死循环）。
    """
    h = _slot_health(name)
    if time.time() - float(h.get("probe_try_ts") or 0) < 60:
        return False
    return _slot_probe_sec(name) is None


def _slot_probe_sec(name):
    """该槽位最近一次探测的耗时（秒）；过期或没数据返回 None。

    延迟随节点负载漂：五分钟前的 200ms 不能代表此刻，所以超过
    slot_probe_ttl_sec 就当没有。没有数据的候选排在「有数据」之后，不是排第一
    —— 拿未知当最优会反复踩同一条没测过的线。
    """
    ttl = float(_api_cfg().get("slot_probe_ttl_sec", 300))
    with _SLOT_STATE_LOCK:
        h = _SLOT_HEALTH.get(name)
        if not h:
            return None
        sec = h.get("probe_sec")
        ts = float(h.get("probe_ts") or 0)
    if not isinstance(sec, (int, float)) or sec <= 0:
        return None
    if time.time() - ts > ttl:
        return None
    return float(sec)


def _slot_pick_key(slot, tag, ip, now, serving):
    """选槽排序键（越小越优先）：新鲜度是主判据，延迟是次判据。

    两层不是加权重求和，而是先分档、档内再比延迟 —— 延迟再低也不能把一条
    没有任何成功记录的地址顶到刚验过 OK 的地址前面，那是拿稳定性换速度。
    「占比」体现在层级：档位决定胜负，延迟只在同档内起作用。
    """
    rank = _warp_ip_rank(ip, now, _warp_guard_for_reason("rate429"))
    delay = _slot_probe_sec(slot["name"])
    return (
        0 if slot["name"] != serving else 1,
        rank[0],                                    # 新鲜度档位
        rank[1],                                    # 同档内避开刚烧过的 /64
        0 if delay is not None else 1,              # 有延迟数据的优先
        delay if delay is not None else 0.0,        # 延迟低者优先
        rank[2],                                    # 同档内越新越优先
    )


# ── 出口地址短冷却 ─────────────────────────────────────────────────────
# 真实请求撞墙（429，或 20s 连响应头都不回的 transport）时，探针往往在几秒
# 前刚判过 OK —— 探针是 3KB 小请求，代表不了 20KB+ 的真实流量，等它自己改口
# 太慢。这里按 **asip 自己看到的出口地址**（_slot_current，不是调用方报来的
# 那个）打一个 90 秒短冷却：这段时间不把这个地址再选给任何请求。
#
# 例外：transport 既可能是线路真坏，也可能只是大 body 首字节慢（探针随后
# 实测就是好的）。这类先异步复测再决定，见 _cool_egress_after_recheck。
#
# 与 ban_until 的分工：ban_until 是「上游判了这个地址多久配额」，按
# Retry-After 走（生产 429 兜底 12 小时）；这里只求躲开当前这一波，不写长期
# 账、不参与隔离判定、不进探针台账。
_EGRESS_COOLING: dict[str, float] = {}
_EGRESS_COOLING_LOCK = threading.Lock()


def _egress_cooling_sec() -> float:
    try:
        return float((CFG.get("api") or {}).get("egress_cooling_sec", 90.0))
    except (TypeError, ValueError):
        return 90.0


def _mark_egress_cooling(ip, now=None):
    """记一次「真实流量在这个出口上失败」。ip 必须是 asip 眼里的当前出口。"""
    if not ip:
        return
    ttl = _egress_cooling_sec()
    if ttl <= 0:
        return
    t = float(now if now is not None else time.time())
    with _EGRESS_COOLING_LOCK:
        # 顺手清过期：这张表按 /128 记，长跑里不清只增不减。
        for k in [k for k, until in _EGRESS_COOLING.items() if until <= t]:
            _EGRESS_COOLING.pop(k, None)
        _EGRESS_COOLING[ip] = t + ttl


def _egress_cooling_left(ip, now=None) -> float:
    if not ip:
        return 0.0
    t = float(now if now is not None else time.time())
    with _EGRESS_COOLING_LOCK:
        until = _EGRESS_COOLING.get(ip) or 0.0
    return max(0.0, until - t)

# 传输类失败的复测去重表：ip -> 最近一次复测的时刻。
# 同一出口在复测窗口（_EGRESS_RECHECK_WINDOW_SEC）内只复测一次：并发上报
# 不会各起一条线程，复测刚判 OK 的那条线也不会被下一发报障立刻再探一遍。
_EGRESS_RECHECK_WINDOW_SEC = 60.0
_EGRESS_RECHECK: dict[str, float] = {}
_EGRESS_RECHECK_LOCK = threading.Lock()


def _egress_recheck_run(ip, tag):
    """对报障出口真打一次探针，只有真不通才冷却。

    WHY 必须复测：固定小 payload 的探针代表不了真实流量（本段注释里那条
    「3KB 探针判 OK、20KB+ 真实请求失败」）。首字节/响应头超时既可能是
    线路真坏，也可能只是大 body 慢 —— 旧逻辑一律打 90s 冷却，几条线轮流
    被冷却后 _pick_healthy_slot 抽干，no_healthy_slot / 503 被放大。
    这里用生产同一台探测器（probe_warp，同指纹同配额）重测这个出口。
    """
    try:
        probed = _probe_line_before_rotate(tag, ip, fresh=True)
        verdict = (probed or {}).get("verdict")
        if verdict == "OK":
            log("· 出口 %s 报 transport 但复测 OK（%ss），不打冷却"
                % (ip, (probed or {}).get("sec", "?")))
            return
        _mark_egress_cooling(ip)
        log("· 出口 %s 报 transport 且复测 %s，按原逻辑冷却 %.0fs"
            % (ip, verdict or "无结论", _egress_cooling_sec()))
    except Exception as e:
        # 复测本身炸了不能当成「线路没问题」：维持原逻辑冷却。
        _mark_egress_cooling(ip)
        log("✗ 出口 %s 复测异常（%s），按原逻辑冷却" % (ip, e))
    finally:
        # 不摘条目：从结论落定起再算一个窗口，窗口内重复报障不再复测。
        with _EGRESS_RECHECK_LOCK:
            _EGRESS_RECHECK[ip] = time.time()


def _cool_egress_after_recheck(ip, tag):
    """transport 类失败：先复测再决定冷却，不阻塞 HTTP 线程。

    复测要 4~12s，不能在 _API_LOCK 内同步跑（9router 的 ASIP_TIMEOUT_MS
    只有 2s）。所以这一步只起线程；复测期间该地址暂不冷却 —— 代价是这几秒
    内它仍可能被选给别人。
    """
    if not ip or _egress_cooling_sec() <= 0:
        return
    if not tag:
        # 读不到是哪条线（_slot_current 没结论）：无从复测，保守冷却。
        _mark_egress_cooling(ip)
        return
    if _egress_cooling_left(ip) > 0:
        # 已经因复测失败冷却中：结论已定，重复报障不必再探一次。
        return
    now = time.time()
    with _EGRESS_RECHECK_LOCK:
        for k in [k for k, ts in _EGRESS_RECHECK.items()
                  if now - ts > _EGRESS_RECHECK_WINDOW_SEC]:
            _EGRESS_RECHECK.pop(k, None)
        if ip in _EGRESS_RECHECK:
            log("· 出口 %s 复测窗口内已探过，重复报障不再起线程" % ip)
            return
        _EGRESS_RECHECK[ip] = now
    threading.Thread(target=_egress_recheck_run, args=(ip, tag),
                     daemon=True, name="egress-recheck").start()


def _pick_healthy_slot(name, avoid_tags=None, avoid_ips=None):
    """挑一台当前健康、且不是刚失败那台的 sing-box，返回 (slot, tag, ip)。

    多端口架构下每台是独立进程、独立连接池，换端口不需要断连、不需要等
    重建 —— 这一下就能回给调用方，比让它在坏端口上重试强得多。

    排序依据是 _slot_pick_key：新鲜度档位第一、探测延迟第二；serving 槽永远
    排在最后（只剩它健康时仍返回它 —— 独立端口本身就是可用备份）。
    候选按轮询游标顺序收集，稳定排序下同分者仍轮换，避免永远压 slot-1。

    avoid_ips 收「调用方刚在上面吃到 429 的出口地址」：六条线共用一个 /64 池，
    单看「哪台闸健康」会挑到挂着同一个 /128 的另一台闸，重试必然还是 429。
    """
    slots = consumer_slots()
    if not slots:
        return None, None, None
    avoid = set(avoid_tags or ())
    avoid_ip_set = {x for x in (avoid_ips or ()) if x}
    serving = _slot_serving_name()
    t0 = time.time()
    n = len(slots)
    start = int(_SLOT_REDIRECT_RR.get("index", 0)) % n
    candidates = []
    for off in range(n):
        s = slots[(start + off) % n]
        if s["name"] == name:
            continue
        if _slot_health(s["name"])["status"] != "ok":
            continue
        tag, ip = _slot_current(s)
        if not tag or tag in avoid:
            continue
        if ip and ip in avoid_ip_set:
            log("· 秒切候选 %s 出口=%s 是这条请求刚撞过的地址，跳过"
                % (s["name"], ip))
            continue
        cooling_left = _egress_cooling_left(ip, t0)
        if cooling_left > 0:
            log("· 秒切候选 %s 出口=%s 冷却中（剩 %.0fs：真实请求刚在它上面失败）"
                % (s["name"], ip or "未知", cooling_left))
            continue
        if _slot_ip_is_quarantined(ip, t0):
            log("· 秒切候选 %s 出口=%s 已隔离（生产标脏/quota），跳过"
                % (s["name"], ip or "未知"))
            continue
        candidates.append((s, tag, ip))
    if not candidates:
        # 六个常规闸口一个都选不出来 —— 第七闸口（代理池兜底）是最后的
        # 选择，且**只在这里**被考虑：上面常规候选存在时它永远不出场。
        # name 是正在报障的那台闸：报障的若是池闸自己，也别原样把池闸
        # 递回去（那条路由 _rotate_proxy_gate_once 换完节点再给）。
        gate = _proxy_gate_slot()
        if (gate is not None and gate["name"] != name
                and _slot_health(gate["name"])["status"] == "ok"):
            gtag, _gip = _proxy_gate_current(gate)
            if gtag:
                _PROXY_GATE_LAST_USED["ts"] = t0
                log("· 秒切兜底：常规闸口全部不可选，第七闸口（代理池）接手"
                    "（池节点=%s）" % gtag)
                return gate, gtag, None
        return None, None, None
    # 排序而不是「跳过 serving 后取第一个」：新鲜度第一、延迟第二（见
    # _slot_pick_key）。candidates 本身是轮询顺序，稳定排序下键完全相同的
    # 候选仍按游标轮换 —— 不会永远压同一个槽位。
    candidates.sort(key=lambda c: _slot_pick_key(c[0], c[1], c[2], t0, serving))
    chosen = candidates[0]
    if len(candidates) > 1:
        def _brief(c):
            rank = _warp_ip_rank(c[2], t0, _warp_guard_for_reason("rate429"))
            delay = _slot_probe_sec(c[0]["name"])
            return "%s(档%d/%s)" % (c[0]["name"], rank[0],
                                    ("%.1fs" % delay) if delay is not None else "延迟未知")
        log("· 选槽排序：%s" % " > ".join(_brief(c) for c in candidates[:3]))
    idx = slots.index(chosen[0])
    _SLOT_REDIRECT_RR["index"] = (idx + 1) % n
    return chosen


def _fast_switch_to_proxy_gate(reason, cur):
    """主路径秒切的第七闸口分支：主出口 opencode-out 翻到轮换池 proxy。

    与槽位路径的第七闸口同一语义 —— 六个常规闸口（含主出口当前线）全都
    选不出来才走到这里。区别只是主路径没有独立端口可指，动作发生在主进程
    自己的 selector 上（opencode-out 的成员里本就有池 selector）。池内
    节点轮换由主循环既有的池轮换接管：落池后 opencode_out_on_warp() 变
    False，主动/错误轮换都会启动。

    不查落池冷却（proxy_fallback_cooldown_sec）：那条冷却防的是「落池→
    回切→又落池」的翻转循环，而这里进来的前提是常规闸口全都选不出来，
    不落池的唯一替代是 restart_required —— 冷却只会把兜底闸焊死。池还
    能不能服务由 _proxy_pool_healthy_cached 现场把一道关。
    """
    global _proxy_dwell_since, _ACTIVE_WARP_IP, _last_proxy_fallback_ts
    if not opencode_out_on_warp():
        # 已经在池上：selector 不必再翻，直接把池身份回给调用方重试。
        return {"tag": _pool_selector_name(), "egress": None}
    pool_ok = _proxy_pool_healthy_cached()
    if pool_ok is not True:
        log("· 秒切第七闸口：池健康探测未通过（%s），放弃" % pool_ok)
        return None
    old_ip = (_warp_line_ip(cur) if cur else None) or _ACTIVE_WARP_IP
    if not _switch_warp(_pool_selector_name()):
        log("· 秒切第七闸口：翻 selector 失败")
        return None
    landed = time.time()
    if cur is not None:
        # 与 _fallback_to_proxy 同一套落地记账：旧线只记「已用」不记脏，
        # 地址退休表补一笔，迟到的 429 才认得出「这条出口已经不承载流量」。
        _mark_warp_ip_used(old_ip, landed)
    _last_proxy_fallback_ts = landed
    try:
        _write_json_atomic(_proxy_fallback_path(), {"ts": landed})
    except Exception as e:
        log("⚠ 落池时刻落盘失败: %s" % e)
    _begin_segment(landed)
    _proxy_dwell_since = landed
    _ACTIVE_WARP_IP = None
    _exit_ledger_note(old_ip, None, landed)
    _MAIN_SLOT_NAME["name"] = None
    log("⚡ 秒切第七闸口：主出口翻到轮换池（原因=%s）" % reason)
    return {"tag": _pool_selector_name(), "egress": None}


def _fast_switch(reason):
    """把主出口立刻切到一条健康备用线上。只做同步的 selector 翻转 + 记账。

    不做探测、不做后台错误切换 —— 那些由 _kick_background_repair 在回包
    定型之后启动，不与秒切并发。
    启动（无论本函数成败，旧闸的错误切换都会跑）。本函数只给调用方换一条
    能用的线，十几毫秒返回。

    返回 {"tag", "egress"}；没有可用备用线时返回 None，调用方回「无可用闸口」
    并通知 9router 自重启，不再同步换线。
    调用方已用 _SWITCH_GUARD 非阻塞包住本函数 —— 拿不到锁直接回 coalesced。
    """
    cur = active_warp_tag()
    # 只尝试一次公平选出的健康槽。旧实现按数组顺序逐个试，既偏袒 slot-1，
    # 又会在多个 selector PUT 失败时把健康候选挨个烧一遍。
    s, tag, ip = _pick_healthy_slot(None, {cur} if cur else None)
    if s is not None and _is_proxy_gate(s):
        return _fast_switch_to_proxy_gate(reason, cur)
    if s is not None and _switch_warp(tag, ip=ip):
        # 与正常换线路径同一套落地记账，缺一条就会让迟到 429 认不出「这条出口
        # 已经被换走」，于是再烧一条备用线。顺序不能变：_switch_warp 内部的
        # exit_ledger 必须读到切换**前**的 _ACTIVE_WARP_IP。
        global _ACTIVE_WARP_IP, _proxy_dwell_since
        switched_at = time.time()
        _mark_warp_ip_used(ip, switched_at)
        _warp_line_selected(tag, ip, switched_at)
        _set_warp_line_ip(tag, ip)
        _ACTIVE_WARP_IP = ip
        _begin_segment(switched_at)
        _proxy_dwell_since = 0
        _MAIN_SLOT_NAME["name"] = s["name"]
        # 断掉旧线上的 sidecar→sing-box 长连接。否则 selector 虽然翻了，
        # 但 HTTP/2 连接池还连着旧 outbound，9router retry 走旧连接 → 旧线，
        # 旧线已 429，retry 也 429 —— 秒切白切，6 条线打转。
        # interrupt=True：旧线已被判 429，在途请求留着也是白等超时，断掉让
        # retry 真正走新 selector。
        if cur is not None:
            _after_warp_switch(cur, interrupt=True, reason=reason)
        log("⚡ 秒切备用线：%s → %s（出口=%s，槽位=%s，原因=%s）"
            % (cur or "未知", tag, ip or "未知", s["name"], reason))
        return {"tag": tag, "egress": ip}
    return None

# reason 白名单：调用方（9router）传的是自由文本，只认这四类；未知一律
# 按 rate429 处理 —— 宁可多记一次限流，也不要把一个真被拦过的地址当干净地址。
_REASON_WHITELIST = {"rate429": "rate429", "abuse403": "abuse403",
                     "transport": "transport", "proactive": "proactive"}


def _api_cfg() -> dict:
    return CFG.get("api") or {}


def _ensure_api_token() -> str:
    """首次启动生成 32 字节随机 token，写 0600。"""
    path = _api_cfg()["token_path"]
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok)
    return tok


def _rotate_ban_after_from_req(req) -> float | None:
    """把 9router 透传的 Retry-After 毫秒转成 ban_after 秒。

    9router 从上游 429 的响应头/正文解析（parseRetryAfterMs），有值以它为准；
    上游不给时用 api.production_429_ban_sec 兜底（默认 12h，来自探针实测
    11~13h），避免 rate429_guard_sec=60s 让刚封的地址立刻回池。
    transport / proactive 不标脏，传了也不会产生任何写入。
    """
    raw = req.get("retry_after_ms") if isinstance(req, dict) else None
    try:
        ms = float(raw)
        if ms > 0:
            return min(ms / 1000.0, 86400.0)
    except (TypeError, ValueError):
        pass
    try:
        secs = float(_api_cfg().get("production_429_ban_sec", 43200))
    except (TypeError, ValueError):
        secs = 43200.0
    return max(1.0, min(secs, 86400.0))


def _normalize_rotate_reason(raw) -> str:
    """白名单收敛：429/quota 归 rate429，403/abuse 归 abuse403。

    未知原因按 rate429 处理 —— 宁可多记一次限流，也不要把一个真的被拦过的
    地址当成干净地址放回候选池。
    """
    v = str(raw or "").strip().lower()
    if v in _REASON_WHITELIST:
        return _REASON_WHITELIST[v]
    if "403" in v or "abuse" in v:
        return "abuse403"
    if "transport" in v or "timeout" in v or "stall" in v:
        return "transport"
    if "proactive" in v:
        return "proactive"
    return "rate429"


def _rotate_gap() -> float:
    """两次换线之间的最小间隔（秒）。

    WHY 不能是固定值：429 的惩罚期是 rate429_guard_sec（60s），N 条活线意味着
    可持续换线速率就是「60/N 秒一次」——超出这个速率，换出去的地址会先被烧光，
    然后 no_warm_standby。固定 8s 在 16 条线的规模上正好卡在反方向：实测 24 次
    /rotate 里 20 次被这个闸拒掉，客户端只能拿着已经被拦的 IP 继续重试。

    config 的 min_gap_verified_sec 只当**下限**用（默认 1.5s）：线少的时候
    （比如只剩 3 条 usque）自动退回 20s，不会把三条线来回烧。
    """
    floor = float(CFG.get("api", {}).get("min_gap_verified_sec") or 1.5)
    try:
        sustain = float(_warp()["rate429_guard_sec"]) / max(1, len(_warp()["members"]))
    except Exception:
        return floor
    return max(floor, sustain)


def _rotate_ip_is_retired(ip: str | None,
                          request_started_at: float | None,
                          client_since: float | None = None) -> bool:
    """调用方吃到 429 的这个地址，在**这次请求发出之后**是否已经被换走。

    True  = 已换走 —— 重连一次就能绕开，本次不必再烧一条新线。
    False = 仍是出口（或判不出来）—— 重连无用，必须真换线。

    判据只有一条：换线表里存在「旧地址就是这个 ip、切换时刻 > 请求发出时刻」
    的记录。它说明这次切换发生在这条请求**之后**，请求打通的是一条已经下线的
    隧道，重连必然落到新出口上。

    **不能**只比「ip 是否等于当前出口」。asip 自己会主动换线（主循环、探测判死、
    定时轮换，全都不经过 9router），这时客户端缓存的地址必然与当前出口不同，
    而请求实际走的是新出口 —— 比出来就是「已换走」，于是每次都回 already_retired、
    从不换线，彻底卡死。反过来，一份过期的缓存也永远追不上当前出口。这个判据
    只用来**否定**「已换走」，从不用来肯定它。

    `client_since` 是调用方缓存里那个地址的**接入时刻**（asip 上次回的
    `active_egress_since`）。它决定这个缓存值可不可信：
      - 缓存里的接入时刻晚于本次请求发出时刻 → 调用方手里是个「未来」的地址，
        它不可能承载过这条请求，说明缓存本身就是错的。此时**不能**据此跳过
        换线 —— 那会让一条真实的 429 得不到处理。
      - 接入时刻早于请求发出 → 缓存可信，正常查表。

    判不出来（调用方没给请求时刻、或表里查不到这个地址）一律 False：多换一次
    只浪费一格配额，而误判「已换走」会让它拿着死隧道反复重试直到超时。
    """
    if not ip or not request_started_at or request_started_at <= 0:
        return False
    if client_since and client_since > request_started_at:
        # 缓存比请求还新，这个地址根本没承载过这条请求。当「判不出」处理。
        return False
    return _exit_ledger_since(ip, request_started_at) is not None


def _with_active_line(payload: dict, t0: float, tag: str | None = None,
                      egress: str | None = None) -> dict:
    """给 /rotate 的每个分支补上「此刻哪条线、哪个地址是出口」。

    线名和地址缺一不可，四种出口变更里有两项都靠它们分辨：
      - 换另一条线：selector 变、线名变、地址变。连接不断（`_switch_warp` 只
        改 selector），但仍挂在被烧的隧道上，调用方要换连接才能绕开。
      - 原地重连当前线：selector 不变、线名不变、只有地址变。这条路径重启了
        隧道，在途连接是被杀掉的 —— 只比线名完全看不见它。
      - 落池到 proxy：线名和地址都变成 None。调用方据此知道出口已不是 WARP。
      - 重启非当前线：三项都不变，与本进程无关，不该触发任何重置。

    tag 显式传入时优先用调用方给的那条，不去读快照：刚 PUT 完 selector 的那
    一刻，快照轮询（clash_poll_sec=0.5）可能还没刷新，读回来是刚被换下的旧线。
    """
    if tag is None:
        try:
            tag = active_warp_tag()
        except Exception:
            tag = None
    if egress is None:
        # 先读该线自己记的地址，再退回进程级当前出口：原地重连成功时线表里的
        # 地址已被 invalidate 成 None，而 `_ACTIVE_WARP_IP` 正是新地址；盲切到
        # 另一条线时反过来，线表才是对的那份，`_ACTIVE_WARP_IP` 还停在旧线。
        try:
            egress = _warp_line_ip(tag) if tag else None
        except Exception:
            egress = None
        if egress is None:
            egress = _ACTIVE_WARP_IP if tag else None
    payload["active_tag"] = tag
    payload["active_egress"] = egress
    # 这个地址是什么时候接上的。调用方缓存的地址只有配上这个时刻才有意义：
    # 只知道「asip 此刻的出口是 X」不够 —— 等到它下次发 429 时，X 可能已经
    # 挂了很久，而它自己无从分辨「刚切过来的新地址」和「缓存里躺了十分钟的旧值」。
    # 有 None 时调用方按「不知道」处理，不会据此做任何跳过决策。
    payload["active_egress_since"] = _exit_ledger_since_install(egress)
    payload["ms"] = int((time.time() - t0) * 1000)
    return payload


def _rotate_once(reason: str, request_started_at: float | None = None,
                 egress_ip: str | None = None,
                 egress_since: float | None = None,
                 ban_after: float | None = None) -> dict:
    """执行一次轮换。全程持锁 → 天然单飞，并发调用只会合并或被限速。

    错误切换（rate429/abuse403）只做秒切：翻到一条已探通的健康备用槽。
    秒切失败 = 没有可用闸口，直接回 restart_required 让 9router 自己重启，
    不再同步探测、不再同步切线，也不起后台线程修旧闸。主动轮换 / transport
    不是错误切换，仍复用 _reconnect_warp_by_table 正常换线。

    `egress_ip` 是调用方**吃到 429 的那个出口地址**（由上一次 /rotate 的
    `active_egress` 缓存而来，不是客户端自己探测的 —— 它探测的只会是直连地址，
    与代理链路无关）。有它时先判「这个地址是不是已经被换走了」：是的话这次
    429 对当前隧道无效，不该再烧一条新线，直接回 `switched=False, reason=
    already_retired`，让调用方重连一次即可。比时刻回溯更硬：时刻是推断，
    地址是事实。

    `request_started_at` 是旧的推断路径，两者都给时以 `egress_ip` 为准。

    已知局限：与主循环自己的换线调用没有共用一把锁。两条同时发生时结果为
    最后写入者胜，记账各自完整但可能指向不同的线。两者都罕见，代价是一次
    多余的切换。
    """
    global _last_rotate_ts
    cfg = _api_cfg()
    t0 = time.time()
    with _API_LOCK:
        _api_stats["calls"] += 1
        current_tag = active_warp_tag()
        old_ip = _warp_line_ip(current_tag) or _ACTIVE_WARP_IP
        # 后台要修的是「被 9router 判定失败的那条线」，不一定是当前线：429
        # 迟到几十秒时当前线可能已经换过两轮。默认与当前线一致，回溯命中时
        # 改成发出那一刻的线；那一刻停在轮换池则为 None（没有可修的线）。
        offending_tag = current_tag
        # 最硬的一条判据：换线表里显示这个地址在**这条请求发出之后**被换掉了。
        # 那它打通的是一条已经下线的隧道，重连就能绕开 —— 再换一次线等于用旧
        # 证据烧掉一条干净的新线（实测 19:24~19:27 连续 8 次轮换里，多数是同一批
        # 迟到 429 在追已经换掉的地址）。放到最前面：下面的回溯与记账都不该执行。
        if egress_ip and _rotate_ip_is_retired(
                egress_ip, request_started_at, egress_since):
            _api_stats["retired"] += 1
            log(f"· /rotate: 请求地址 {egress_ip} 在请求发出后被换走"
                f"（当前 {current_tag or '轮换池'} / {old_ip or '地址未知'}），不换线")
            return _with_active_line(
                {"ok": True, "switched": False, "reason": "already_retired",
                 "switched_at": _last_rotate_ts}, t0)
        # 当前出口本身，回溯**不会**改它。下面两个判据口径不同，必须分开：
        # `was_blocked` 问的是「此刻这条出口还脏不脏」—— 脏就别再等 gap，接着
        # 换下一跳；而 `old_ip` 是「这条 429 该记到谁头上」，迟到时可以指向另
        # 一条线。拿回溯后的 old_ip 判 was_blocked 是自证：下面刚把它标脏，
        # 于是恒为真，gap 限速对每一批迟到请求全部失效（实测 4 次调用 4 次换线，
        # 对照 2 次）。
        cur_ip = old_ip
        # 调用方带来的就是证据：它刚在出口上吃到 429/403。所以先记账再判限速 ——
        # 判完拒掉也不能当这条 IP 是干净的，否则同一波风暴里被拒的那些请求会在
        # 60s 后重新选中它。
        # 证据属不属于**当前**这条线由调用方给的发出时刻决定。三态见 `_warp_line_at`：
        # 不知道（None）→ 保持当前线，与旧行为一致；知道那一刻在轮换池 → 不标脏；
        # 知道是另一条线 → 用那条线当时的地址。
        #
        # 判据只取「调用方给了发出时刻」：停在轮换池时 current_tag 与 old_ip
        # 都是 None，任何一个当前值做前置都会让池上发出的 429 整个跳过回溯，
        # 而那批请求恰恰有明确的发出线（池切换之前那条）。
        if request_started_at is not None and request_started_at > 0:
            hit = _warp_line_at(request_started_at)
            if hit is not None:
                hist_tag, hist_ip = hit
                if hist_tag != current_tag:
                    # 那一刻不在当前这条线上。可能是另一条线（hist_tag 为线名），
                    # 也可能是轮换池（hist_tag 为 None）。
                    offending_tag = hist_tag
                    old_ip = hist_ip
                elif hist_ip and hist_ip != old_ip:
                    # 同一条线，但发出之后它被原地重连换过会话地址
                    # （`_restart_warp_inplace` 只重启隧道、selector 不动）。
                    # 只比 tag 会当作「还是当前线」而取新地址 —— 那正是这条
                    # 迟到的 429 唯一没被修到的分支：新地址刚换出来、干净，
                    # 却替旧地址背上 24h 惩罚期。
                    old_ip = hist_ip
                # 回溯命中时必须留痕：这段逻辑是否在生产里真的跑过，只能从
                # 日志回答。没有这行就只能靠单元测试推断「应该生效了」——
                # 而 9router 未重启的那段时间里它一次都没执行过。
                log(f"  ⟲ 迟到回溯: 发出于 {t0 - request_started_at:.1f}s 前"
                    f" → {hist_tag or '轮换池'}({hist_ip or '地址未知'})"
                    f" 当前={current_tag or '轮换池'}")
            else:
                log(f"  ⟲ 迟到回溯: 发出于 {t0 - request_started_at:.1f}s 前"
                    f" 但早于历史首条，退回当前线")
        elif request_started_at is not None:
            log("  ⟲ 迟到回溯: request_started_at 非法，退回当前线")
        was_blocked = False
        try:
            was_blocked = bool(cur_ip) and _warp_ip_is_blocked(cur_ip, t0)
        except Exception:
            was_blocked = False
        # transport 只证明这条连接/链路悬挂，不是 IP 级封禁证据：换线但不记脏。
        if reason not in ("proactive", "transport") and old_ip:
            _mark_warp_ip_used(old_ip, t0, blocked=True, kind=reason,
                               ban_after=ban_after)
        try:
            # 合并窗放在记账**之后**：迟到的 429 常常同一批涌进来（实测客户端一次
            # 并发几十条），若先合并再记账，第一条换线后其余全部命中合并窗，历史里
            # 那条真正发出请求的线一条都标不到，下一批又原样选回来。合并不换线，
            # 但证据必须照收。
            if t0 - _last_rotate_ts < cfg["coalesce_ms"] / 1000.0:
                _api_stats["coalesced"] += 1
                return _with_active_line(
                    {"ok": True, "switched": False, "reason": "coalesced",
                     "switched_at": _last_rotate_ts},
                    t0)
            gap = _rotate_gap()
            if (_last_rotate_ts and t0 - _last_rotate_ts < gap
                    and not was_blocked):
                _api_stats["too_frequent"] += 1
                return _with_active_line(
                    {"ok": False, "reason": "too_frequent",
                     "switched_at": _last_rotate_ts},
                    t0)
            # 秒切：翻到一条已探通的备用线，十几毫秒。错误切换没有备线就直接
            # 收口回「无可用闸口」，不再同步探测、不再同步切线。
            # 后台修整不再「异步先行」：它由本段末尾的 finally 在所有同步决策完成、
            # 回包定型之后启动，避免后台换线/探新线与秒切挑线并发改同一份状态表。
            quick = None
            if reason in ("rate429", "abuse403"):
                if not _SWITCH_GUARD.acquire(blocking=False):
                    # 已经有一次换闸在跑：这次的 429 多半来自同一批并发请求，
                    # 那条坏线正在被换走。不排队、不重复换，直接回 coalesced
                    # （调用方对这个 reason 的语义就是「别人刚换过，重连即可」）。
                    _api_stats["coalesced"] += 1
                    log("· /rotate 换闸占用中，本次不换线（并发合并）")
                    # 刻意**不回** active_tag / active_egress：这一瞬间出口正在被
                    # 另一次切换改写，快照读到的是切换前的旧线。把它回过去，调用方
                    # 会用旧身份覆盖自己的账 —— 下一条 429 就会带着错误出口来。
                    # line_unknown 明确告诉调用方「这次不知道」：既不算「没切」，
                    # 也不更新本地记录。旧闸的错误切换由 finally 在返回前启动。
                    return {"ok": True, "switched": False, "reason": "coalesced",
                            "switched_at": _last_rotate_ts, "line_unknown": True,
                            "ms": int((time.time() - t0) * 1000)}
                try:
                    quick = _fast_switch(reason)
                finally:
                    _SWITCH_GUARD.release()
            if quick:
                _api_stats["switched"] += 1
                _last_rotate_ts = t0
                return _with_active_line(
                    {"ok": True, "switched": True, "tag": quick["tag"],
                     "egress": quick["egress"], "closed": 0,
                     "switched_at": t0},
                    t0, tag=quick["tag"], egress=quick["egress"])
            # ── 秒切失败：错误切换主链路到此为止 ──────────────────────
            # 旧第 10 步「换线前实测 / line_ok」与第 11 步「按状态表同步换线」
            # 本质是「旧闸怎么修」，不是「本次请求怎么恢复」。两者已从
            # rate429/abuse403 主链路整段删除：不再同步探测、不再同步切线。
            #
            # 切失败 = 此刻没有任何健康闸口能立刻接管。不动主出口 selector
            # （不切线）；旧闸和其余分支一样，统一交给末尾 finally 在回包定型
            # 之后后台修整。回包把「无可用闸口，自己重启」交给 9router 重建
            # 连接池，60s 节流保证一批并发 429 不会变成重启风暴。
            # line_unknown=True 让调用方不要拿旧出口身份覆盖自己的账。
            if reason in ("rate429", "abuse403"):
                return {
                    "ok": True, "switched": False,
                    "reason": "no_healthy_slot",
                    "switched_at": _last_rotate_ts,
                    "line_unknown": True,
                    "switch_to": None,
                    "switch_to_url": None,
                    "restart_required": True,
                    "ms": int((time.time() - t0) * 1000),
                }
            # 主动轮换 / transport 不是错误切换，仍按状态表正常换线。
            # 这条路径本来就是「主循环要换线」，与旧闸处置无关。
            out: dict = {}
            ok = _reconnect_warp_by_table(reason, interrupt=False, out=out)
            if not ok:
                _api_stats["no_standby"] += 1
                return _with_active_line(
                    {"ok": False, "reason": "no_warm_standby",
                     "switched_at": _last_rotate_ts},
                    t0)
            _last_rotate_ts = float(out.get("switched_at") or t0)
            _api_stats["switched"] += 1
            new_tag = out.get("tag")
            # 同步换线后主出口落在一条非槽位的线上（来自状态表），不再借任何槽位。
            _MAIN_SLOT_NAME["name"] = None
            return _with_active_line(
                {"ok": True, "switched": True,
                 "tag": new_tag, "egress": out.get("ip"),
                 "closed": 0,
                 "switched_at": _last_rotate_ts},
                t0, tag=new_tag or None, egress=out.get("ip"))
        finally:
            # 回包定型之后才让后台线程动状态，避免后台换线/探新线与秒切挑线
            # 并发改同一份状态表。所有分支一视同仁，切成功、切失败、合并窗、
            # 限速都在这里修旧闸，不再分情况。
            if reason in ("rate429", "abuse403"):
                try:
                    _kick_background_repair(offending_tag, old_ip, reason)
                except Exception as e:
                    log(f"✗ 后台修整启动失败: {e}")


def _api_debug_state() -> dict:
    """测试与排障用的状态快照。

    fresh_candidates 是 /rotate 当下能选到的备线集合 —— 它是 no_warm_standby 的
    唯一直接解释。不给出来，供给不足就只能靠猜（第一次排查时就踩过：状态表里
    看着有 9 条新鲜线，候选列表其实是空的）。blocked_ips 给出惩罚期的 IP 数，
    用来区分「线不够」和「IP 全在罚期」。
    """
    now = time.time()
    active = active_warp_tag()
    try:
        cands = [m["tag"] for m, _ip in
                 _warp_line_fresh_candidates(active, now)]
    except Exception:
        cands = []
    blocked = 0
    try:
        with _WARP_IP_LOCK:
            ips = list(_WARP_IP_USED)
        blocked = sum(1 for ip in ips if _warp_ip_is_blocked(ip, now))
    except Exception:
        blocked = -1
    return {
        "ok": True,
        "active_tag": active,
        "fresh_candidates": cands,
        "blocked_ips": blocked,
        "probe_stats": dict(_PROBE_STATS),
        "active_ip": _ACTIVE_WARP_IP,
        "line_ip": {m["tag"]: _warp_line_ip(m["tag"])
                    for m in _warp()["members"]},
        "last_rotate_ts": _last_rotate_ts,
        "stats": dict(_api_stats),
        "slots": _slot_snapshot(),
        "probe_divergence": list(_PROBE_DIVERGENCE[-10:]),
        # 换线表是判断 already_retired 的唯一依据，而它只在进程内存里 ——
        # 不给出来就只能靠「日志里没有 already_retired」反推，分不清是
        # 「表里没这个地址」还是「根本没查」。按时间倒序，最近的在前。
        "exit_ledger": [
            {**e, "ok_src": (e.get("ok") or {}).get("src"),
             "ok_ago_s": (round(now - e["ok"]["ok_ts"], 1)
                          if (e.get("ok") or {}).get("ok_ts") else None)}
            for e in reversed(_EXIT_LEDGER[-20:])
        ],
    }


_CLIENT_GONE = {"count": 0, "last_log": 0.0, "last_switched": 0}


def _note_client_gone(result: dict) -> None:
    """换线已完成、但回传时客户端已经走了。

    这不是 /rotate 的故障，而是一次「换了但没等」：客户端 ASIP_TIMEOUT_MS
    （默认 2000ms）先到点、销毁了连接，锁内的换线照旧跑完并生效，只是没人
    接这个响应。所以这里只计数 + 限流打一行，绝不吞成异常再补写 500 ——
    补写那一次同样会炸，栈打穿之后日志里就只剩下 Broken pipe 这一种声音，
    真正的故障反而被淹掉。
    """
    _CLIENT_GONE["count"] += 1
    _CLIENT_GONE["last_switched"] = 1 if result.get("switched") else 0
    _api_stats["client_gone"] = _CLIENT_GONE["count"]
    now = time.time()
    if now - _CLIENT_GONE["last_log"] >= 60:
        _CLIENT_GONE["last_log"] = now
        log(f"· /rotate 回传超时累计 {_CLIENT_GONE['count']} 次；"
            f"本次已换线 switched={bool(result.get('switched'))} "
            f"reason={result.get('reason')} ms={result.get('ms')}"
            f"（换线生效，只是没人收响应）")

def _slot_no_usable_gate():
    """「这次确实没有可用闸口」：只在首轮探测收敛后给出重启信号。

    调用点只有在 _pick_healthy_slot 返回 None 时才进来，所以「有没有能立刻
    接管的健康备槽」已经回答过了。这里补的是时间维度的判定：

    启动窗口内，只要还有一条槽位没出首轮结论（unknown），就不算闸坏了。
    线上 19:52:19 daemon 启动，19:52:25 slot-1 才从 unknown → repairing、
    19:52:39 slot-2 同样、直到 19:52:43 才有第一条 unknown → ok；旧判据只
    排除「六条全 unknown」，于是 19:52:25 / 19:52:39 两次照发
    restart_required，9router 连重启 2 次。闸正在后台修，重启调用方重建
    连接池帮不上忙，只会打断刚接上的连接。

    宽限期结束后回到稳态判据：混态（在修 / 坏 / 仅剩一条 ok 但被 avoid
    排除）一律发信号。更早一版要求「没有一条 ok 且没有一条 unknown」，把
    18:41 / 19:02 / 19:03 三次全漏了，调用方既不切线也不重建连接池，
    请求直接吃 429 —— 不能再退回去。

    宽限期有硬上限（slot_boot_grace_sec）：超时后即便还有 unknown 也不再
    抑制，避免某条线长期探不出结果时永远不发信号。
    """
    slots = consumer_slots()
    if not slots:
        return False
    statuses = [_slot_health(s["name"])["status"] for s in slots]
    grace = float(_api_cfg().get("slot_boot_grace_sec", 60))
    if time.time() - _SLOT_BOOT_TS < grace:
        if any(st == "unknown" for st in statuses):
            return False
    return True


def _slot_redirect_payload(slot, cur_tag, cur_ip, reason, switched_at,
                           avoid_until=0.0, avoid_ips=None):
    """构造槽位轮换回包：当前槽身份 + 立即重试目标 + 原端口冷却截止。"""
    payload = {"ok": True, "switched": False, "reason": reason,
               "switched_at": switched_at, "ms": 0,
               "active_tag": cur_tag, "active_egress": cur_ip,
               "avoid_port_until": avoid_until, "switch_to_node": None}
    other, ttag, tip = _pick_healthy_slot(
        slot["name"], {cur_tag} if cur_tag else None, avoid_ips)
    if other is not None:
        payload["switched"] = True
        payload["switch_to"] = other["name"]
        payload["switch_to_url"] = "http://127.0.0.1:%d" % other["port"]
        payload["switch_to_tag"] = ttag
        payload["switch_to_egress"] = tip
        if _is_proxy_gate(other):
            # 常规闸口全不可选 → 重试目标是第七闸口。它没有 WARP 那种
            # /128 出口地址（switch_to_egress 只能是 None），把池节点身份
            # 单独给 9router，它才能认出这次换的是池里的哪条出口。
            payload["switch_to_node"] = ttag
    else:
        payload["switch_to"] = None
        payload["switch_to_url"] = None
        if reason != "already_retired":
            # 没有任何健康备槽能立刻接管这条请求。主链路不再同步探线/换线
            # 兜底：被报槽由后台线程修好，调用方重启自己重建连接池，等闸回来。
            # already_retired 不在此列 —— 线已经换走了，原地重连就够。
            payload["line_unknown"] = True
            # reason 必须改口。槽位路径传进来的 reason_out 是 ok/coalesced/
            # too_frequent，9router 的 429 早退分支把这些词当「换过线了，
            # 重连再试一次」—— 明明没有可用闸口，还会拿刚烧掉的原端口多打
            # 一发，而 1s 后本进程就要重启。统一成 no_healthy_slot：不切线、
            # 不重试，由 9router 前台重启接管。
            payload["reason"] = "no_healthy_slot"
            if _slot_no_usable_gate():
                payload["restart_required"] = True
    return payload


def _proxy_gate_rotate_node(slot, cur_node, reason):
    """第七闸口池内换节点；返回新节点名，失败返回 None。

    与主路径池轮换（_rotate_on_error / _rotate_pool_proactive）同一套判据：
    旧节点按 429 冷却（不拉黑 —— 429 恰恰说明链路通，判死交给健康检查的
    60% 保底守卫），新节点用 pick_next_node 的 P2C + 预检。绝不碰 WARP：
    第七闸口的「换线」只有池内换节点这一种。

    注意 PUT 直接打这台槽位自己的 pool selector：主循环的池轮换只翻主进程
    的 proxy selector，翻不到这台进程 —— 各自翻各自的，池状态（_DEAD_NODES
    / 延迟表）两边共用。
    """
    name = slot["name"]
    nodes, _cur = _proxy_gate_nodes(slot)
    if not nodes:
        _slot_mark(name, "bad", "unreachable")
        return None
    if cur_node and reason not in ("proactive", "dead"):
        # 报障换节点才冷却旧节点；dead 已在 _DEAD_NODES 里，再冷却无意义。
        _cool_down_node(cur_node)
    nxt = pick_next_node(cur_node or "", nodes, set(), count_call=False)
    if not nxt:
        _slot_mark(name, "bad", "no_candidate")
        log("✗ 第七闸口无可换池节点（%s，dead=%d）"
            % (reason, len(_DEAD_NODES)))
        return None
    if nxt != cur_node:
        r, _ = slot_singbox_api(slot, "/proxies/" + slot["pool_selector"],
                                method="PUT", body={"name": nxt})
        if r not in (200, 204):
            log("✗ 第七闸口切池节点失败 (%s): status=%s" % (nxt, r))
            return None
        time.sleep(0.2)
        _, verify = slot_singbox_api(slot, "/proxies/" + slot["pool_selector"],
                                     silent=True)
        if (verify or {}).get("now") != nxt:
            log("✗ 第七闸口切池节点验证失败: PUT %s 但 now=%s"
                % (nxt, (verify or {}).get("now")))
            return None
        _slot_mark(name, "ok", "OK")
        update_node_info_async(nxt)
        log("↻ 第七闸口池内轮换：%s → %s（%s）"
            % (cur_node or "未知", nxt, reason))
    _PROXY_GATE_LAST_ROTATE["ts"] = time.time()
    return nxt


def _rotate_proxy_gate_once(slot, reason, request_started_at=None,
                            egress_ip=None, req_id=None, ban_after=None):
    """第七闸口的 /rotate：常规闸口恢复了就立刻回去，否则池内换节点。

    严格兜底的另一半语义在这里：即便流量已经在第七闸口上，只要有任何常规
    闸口健康，重试目标马上指回常规闸口 —— 池闸不占坑。429/403 落在池闸上
    的含义是「这个池节点也被烧了」，动作是池内换节点（同主路径落池后的
    错误轮换），不是换 WARP 线。

    ban_after / egress_ip 只对 WARP 地址台账有意义，池节点没有那套账，
    收下参数但不使用。
    """
    t0 = time.time()
    cfg = _api_cfg()
    name = slot["name"]
    with _API_LOCK:
        _api_stats["calls"] += 1
        cur_node, _cur_ip = _proxy_gate_current(slot)
        h = _slot_health(name)
        last_attempt = float(h.get("last_rotate_ts") or 0.0)
        # 与常规闸口同一套合并窗：一批并发 429 只换一次池节点。
        if last_attempt and t0 - last_attempt < cfg.get("coalesce_ms", 800) / 1000.0:
            _api_stats["coalesced"] += 1
            reason_out = "coalesced"
        else:
            h["last_rotate_ts"] = t0
            reason_out = "ok"
        # 重试目标：常规健康闸口优先（恢复即回，不在池闸上多留一秒）。
        other, ttag, tip = _pick_healthy_slot(
            name, None, {egress_ip} if egress_ip else None)
        rotated = None
        if other is None and reason_out == "ok":
            # 没有常规闸口可回：在第七闸口自己的池里换节点。
            rotated = _proxy_gate_rotate_node(slot, cur_node, reason)
            if rotated:
                _PROXY_GATE_LAST_USED["ts"] = t0
        payload = {"ok": True, "switched": False, "reason": reason_out,
                   "switched_at": h.get("switched_at") or 0.0, "ms": 0,
                   "active_tag": cur_node, "active_egress": None,
                   "switch_to_node": None}
        if other is not None:
            payload["switched"] = True
            payload["switch_to"] = other["name"]
            payload["switch_to_url"] = "http://127.0.0.1:%d" % other["port"]
            payload["switch_to_tag"] = ttag
            payload["switch_to_egress"] = tip
        elif rotated:
            # 第七闸口自己就是重试目标：端口不变，节点已换。
            payload["switched"] = True
            payload["switch_to"] = name
            payload["switch_to_url"] = "http://127.0.0.1:%d" % slot["port"]
            payload["switch_to_tag"] = rotated
            payload["switch_to_egress"] = None
            # 池节点没有 WARP 那种 /128 出口地址，9router 拿不到
            # switch_to_egress 就会按「no new egress」拒绝这一发重放
            # （2026-11-13 实测 30min 内 83 次 refused）。switched_at 给出
            # 「请求发出后确实换过出口」的时间证据，switch_to_node 给出换到
            # 哪个池节点；两者只在真换了节点时填，coalesced 不能骗重放。
            payload["switched_at"] = t0
            payload["switch_to_node"] = rotated
        else:
            payload["switch_to"] = None
            payload["switch_to_url"] = None
            if reason_out != "coalesced":
                # 池也换不出节点：与常规闸口同一收口语义。
                payload["line_unknown"] = True
                payload["reason"] = "no_healthy_slot"
                if _slot_no_usable_gate():
                    payload["restart_required"] = True
        log("⇢ %s 报 %s（%s）[req=%s] 池节点=%s，重试改走 %s"
            % (name, reason, reason_out, req_id or "-", cur_node or "未知",
               payload.get("switch_to_url") or "无可用闸口"))
        return payload


def _rotate_slot_once(slot, reason, request_started_at=None, egress_ip=None,
                      req_id=None, ban_after=None, borrowed=False):
    """带防重的槽位 /rotate：迟到请求只重连，不为旧证据烧掉新线。

    与主路径一一对应：
      - 槽位自己的 exit ledger 判 already_retired；
      - 正在修整 = coalesced，重复 429 不重复起线程；
      - 合并窗内的事故只记证据、不重复换线；
      - 新线未进惩罚期时仍受 gap 限速，避免同一批风暴连续烧线。
    与主路径不同的只有结果：不等后台修完，直接回一条健康槽的端口让调用方
    立刻重试。
    """
    if _is_proxy_gate(slot):
        # 第七闸口不走 WARP 修整链路：报障 = 池内换节点。
        return _rotate_proxy_gate_once(
            slot, reason, request_started_at, egress_ip, req_id, ban_after)
    t0 = time.time()
    cfg = _api_cfg()
    name = slot["name"]
    with _API_LOCK:
        _api_stats["calls"] += 1
        cur_tag, cur_ip = _slot_current(slot)
        # 事发前先读一次该地址的实测结论。下面 _mark_warp_ip_used 会
        # forget_probe 清台账，回包/日志以前在标脏之后才读，必然是「未探」。
        verdict_before = (_probe_verdict_for_ip(cur_ip, t0)
                          if cur_ip else None)
        verdict_after = verdict_before
        h = _slot_health(name)
        switched_at = float(h.get("switched_at") or 0.0)
        retired = bool(request_started_at and request_started_at > 0
                       and switched_at > request_started_at)
        if not retired and _slot_ip_is_retired(name, egress_ip,
                                               request_started_at):
            retired = True
        if retired:
            _api_stats["retired"] += 1
            log("· %s: 请求地址 %s 在请求发出后被换走（当前 %s / %s），不换线"
                % (name, egress_ip or "未知", cur_tag or "未知",
                   cur_ip or "地址未知"))
            # 同一端口的新线已经就位；调用方清连接后原地重试即可。这里不再
            # 发起换线（有健康备槽时照样给出，让对端可以顺手合并连接）。
            out = _slot_redirect_payload(
                slot, cur_tag, cur_ip, "already_retired", switched_at, 0.0,
                {egress_ip} if egress_ip else None)
            out["offending_verdict"] = verdict_before
            return out

        # 证据先记账，再判合并/限速：被拒掉的一次也不能把刚吃 429 的地址
        # 当干净地址放回候选。
        was_blocked = False
        try:
            was_blocked = bool(cur_ip) and _warp_ip_is_blocked(cur_ip, t0)
        except Exception:
            was_blocked = False
        # 真实流量刚在这个地址上失败：不管上游是回 429，还是干脆 20 秒连响应头
        # 都不吐（transport），都给它打一段短冷却。判据必须用 asip 自己
        # _slot_current 读到的 cur_ip —— 调用方传来的 egress_ip 有可能是别的
        # 请求残留的（9router 侧 currentAsipEgress 是进程级全局），拿它冷却
        # 会去惩罚一条无辜的线，而真正该躲的那条继续放行。
        # 唯一例外是 transport：它同时覆盖「线路真断」和「大 body 首字节慢」，
        # 后者探针复测就是好的，直接冷却会把六条闸一条条抽干。这类先异步复测
        # 一次再决定（见 _cool_egress_after_recheck）。
        if cur_ip and reason == "transport":
            _cool_egress_after_recheck(cur_ip, cur_tag)
        elif cur_ip and reason in ("rate429", "abuse403"):
            if borrowed:
                # 借道（9router 把别的线的请求改投到本端口）失败：这一发不是
                # 本槽发起的，body/session 全是原线的，单发 429 不足以证明本槽
                # 出口额度耗尽。与 transport 同规格：先复测，复测仍 quota 才
                # 冷却，且**不写 24h 标脏**（_egress_recheck_run 只打短冷却）。
                _cool_egress_after_recheck(cur_ip, cur_tag)
            else:
                _mark_egress_cooling(cur_ip, t0)
        if (reason not in ("proactive", "transport") and cur_ip
                and not borrowed):
            _mark_warp_ip_used(cur_ip, t0, blocked=True, kind=reason,
                               ban_after=ban_after)
            # 只观测：标脏后台账里还剩什么，留在 ⇢ 那一行做运行时证据。
            verdict_after = _probe_verdict_for_ip(cur_ip, t0)

        last_attempt = float(h.get("last_rotate_ts") or 0.0)
        accepted = False
        repair_thread = None
        if borrowed:
            # 借道失败不动被借线：它没坏，换掉等于白烧一条线。仍然回下一个
            # 健康备槽，让 9router 把这条请求继续递下去。
            _api_stats["borrowed"] += 1
            reason_out = "borrowed_skip"
        elif h["status"] == "repairing":
            _api_stats["coalesced"] += 1
            reason_out = "coalesced"
        elif last_attempt and t0 - last_attempt < cfg.get("coalesce_ms", 800) / 1000.0:
            _api_stats["coalesced"] += 1
            reason_out = "coalesced"
        elif (not was_blocked and last_attempt
              and t0 - last_attempt < _rotate_gap()):
            _api_stats["too_frequent"] += 1
            reason_out = "too_frequent"
        elif _slot_begin_repair(name, reason):
            h["last_rotate_ts"] = t0
            # 只造线程、不启动：后台换线/探新线/放回池子和下面的
            # _pick_healthy_slot 读同一份状态表，先启动会让秒切挑中的端口
            # 恰好是后台刚标脏的那条，或读到过时健康快照。
            repair_thread = threading.Thread(
                target=_repair_slot, args=(slot, reason),
                daemon=True, name="repair-" + name)
            accepted = True
            reason_out = "ok"
        else:
            # 另一条线程刚好认领了这台槽位（重复收敛/秒切前置撞上）：等同合并，
            # 不重复换线，但也别把调用方打回去原地重试坏端口。
            _api_stats["coalesced"] += 1
            reason_out = "coalesced"
        avoid_until = (t0 + float(cfg.get("slot_cooldown_sec", 15))
                       if accepted or h["status"] == "repairing" else 0.0)
        # 回包定型（挑健康槽）之后再启动后台修整。用 finally 保证
        # _slot_redirect_payload 万一抛异常，认领过的槽位不会永远卡在 repairing。
        try:
            out = _slot_redirect_payload(slot, cur_tag, cur_ip, reason_out,
                                         switched_at, avoid_until,
                                         {egress_ip} if egress_ip else None)
        finally:
            if repair_thread is not None:
                try:
                    repair_thread.start()
                except Exception as e:
                    log(f"✗ {name} 后台修整启动失败: {e}")
        # 事发前读数钉进回包：_rotate_trace_fields 优先沿用，不在标脏
        # （forget_probe 已清台账）之后重算成「未探」。
        out["offending_verdict"] = verdict_before
        # borrowed 也打：借道不认领修整，accepted 恒 False，不加这一条就
        # 完全看不见「哪条健康线被谁借去打了一发」—— 正是不可观测的复发点。
        if accepted or h["status"] == "repairing" or borrowed:
            # 排查通道：把请求 id、这台闸绑定的线/IP、该地址的实测结论一起
            # 留下。少了这几项，出问题时只能拿时间戳去猜「哪条请求走了哪个
            # 出口、那个地址当时是不是已被判 quota」—— 槽位状态是内存态、
            # 事后还会被后台修整改写，无法回溯。
            # 线/出口取 _slot_current 读出来的**一对**；调用方报来的 egress_ip
            # 是全局活动出口、对独立槽位不适用，单列「报障IP」备查，不拿来
            # 冒充这台闸的地址（实测不同线的槽位会报同一个 IP）。
            # 「标脏后」只作观测：与「实测」并列即可看出台账是否被清过。
            log("⇢ %s 报 %s（%s）%s[req=%s] 线=%s 出口=%s 报障IP=%s 实测=%s "
                "标脏后=%s，重试改走 %s 目标线=%s 目标出口=%s 目标url=%s"
                % (name, reason, reason_out,
                   "（借道，不判死本线）" if borrowed else "",
                   req_id or "-",
                   cur_tag or "未知", cur_ip or "未知", egress_ip or "-",
                   verdict_before or "未探", verdict_after or "未探",
                   out.get("switch_to") or "无健康备槽",
                   out.get("switch_to_tag") or "-",
                   out.get("switch_to_egress") or "-",
                   out.get("switch_to_url") or "-"))
        return out


def _rotate_trace_fields(req: dict, payload: dict) -> dict:
    """给 /rotate 回包补排查三元组：事发出口 IP、线名、该 IP 的实测结论。

    之前 9router 侧只有 req + 端口，asip 侧只有「此刻」的线名和出口 IP，
    而 quota 实测日志是后台探测打的、没有 req 上下文 —— 想证明「这条 req
    当时那条 IP 已被封」只能拿时间戳硬凑。这里把三者钉在一次回包里。

    线/IP 必须取槽位自己的 active_tag/active_egress（_slot_current 读出来的
    一对，与请求实际打的那个端口对应）。**不能**拿调用方报来的 egress_ip
    当事发地址：那是 9router 账本里的全局活动出口，六条独立槽位各自有
    不同的线/IP，实测出现过「不同线的槽位全报同一个 IP」；主路径没有
    active_egress 时才退回它。调用方报的值另存 reported_egress 备查。
    """
    raw = req.get("egress_ip")
    reported = raw.strip() if isinstance(raw, str) and raw.strip() else None
    ip = (payload.get("active_egress") or payload.get("egress")
          or reported or None)
    tag = payload.get("active_tag") or payload.get("tag") or None
    # 槽位路径已在标脏前把结论快照进 payload（标脏会清台账，事后取必然
    # 是 None）。主路径 result 没有这个键，仍按实时台账取。
    if "offending_verdict" in payload:
        verdict = payload["offending_verdict"]
    else:
        try:
            verdict = _probe_verdict_for_ip(ip)
        except Exception:
            verdict = None
    return {"offending_tag": tag, "offending_egress": ip,
            "offending_verdict": verdict, "reported_egress": reported}


class _RotateHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "asip/1"

    def _send(self, code: int, payload: dict) -> bool:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, OSError):
            # 对端已关闭（超时销毁 / 客户端重启）。写不出去就是写不出去，
            # 交给调用方决定记账方式，这里不再重试、不再补写。
            self.close_connection = True
            return False

    def _authed(self) -> bool:
        return self.headers.get("X-Asip-Token") == _API_TOKEN

    def do_GET(self):
        if self.path == "/slots":
            if not self._authed():
                self._send(401, {"ok": False, "reason": "unauthorized"})
                return
            self._send(200, {"ok": True, "active_tag": active_warp_tag(),
                             "slots": _slot_snapshot()})
            return
        if self.path != "/debug/state":
            self._send(404, {"ok": False, "reason": "not_found"})
            return
        if not self._authed():
            self._send(401, {"ok": False, "reason": "unauthorized"})
            return
        self._send(200, _api_debug_state())

    def do_POST(self):
        if self.path == "/refresh-sub":
            # 订阅源刚被改过（`asip sub add`）时用它立刻生效。后台线程跑：
            # 刷新要拉 4~5 个源 + 重启 sing-box，十几秒起步，阻塞在 HTTP
            # 响应里会让调用方超时，看起来像失败。
            if not self._authed():
                self._send(401, {"ok": False, "reason": "unauthorized"})
                return
            # 不在这里判「是否已在刷新」：refresh_subscription 自己拿非阻塞锁，
            # 重复调用只会打一行「已有订阅刷新在进行」。预判一次反而多一个竞态。
            threading.Thread(target=refresh_subscription,
                             args=("手动插入订阅",), daemon=True,
                             name="manual-sub-refresh").start()
            self._send(200, {"ok": True, "started": True})
            return
        if self.path != "/rotate":
            self._send(404, {"ok": False, "reason": "not_found"})
            return
        if not self._authed():
            self._send(401, {"ok": False, "reason": "unauthorized"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b"{}"
            req = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            req = {}
        # 排查通道：9router 把本次请求 id 透传过来，下面所有 /rotate 日志带上
        # 它，才能把「一条失败请求」和「哪台闸/哪条线/哪个出口」串成一条链。
        _rid = req.get("req_id")
        rid = str(_rid).strip()[:64] if _rid else None
        # 生产 429 的封禁时长：9router 透传的真实 Retry-After 优先，
        # 缺失时用 api.production_429_ban_sec 兜底（见函数注释）。
        ban_after = _rotate_ban_after_from_req(req)
        # 多端口：带 egress_proxy_url 的请求按端口定位槽位，走独立 sing-box 路径。
        # 每台 sing-box 独立连接池，9router 轮询，429 时后台修那台，立即返回
        # 让 9router 重连到别的端口。不走下面的单主出口逻辑。
        slot = _slot_by_proxy_url(req.get("egress_proxy_url"))
        if slot is not None:
            try:
                payload = _rotate_slot_once(
                    slot,
                    _normalize_rotate_reason(req.get("reason")),
                    _as_ts(req.get("request_started_at")),
                    str(req.get("egress_ip")).strip()
                    if isinstance(req.get("egress_ip"), str)
                    and req.get("egress_ip").strip() else None,
                    rid, ban_after,
                    borrowed=bool(req.get("borrowed")))
            except Exception as e:
                log(f"✗ 槽位 /rotate 异常: {e}")
                self._send(500, {"ok": False, "reason": "internal_error"})
                return
            # 信号由 _slot_redirect_payload 给：无健康备槽 -> restart_required，
            # 9router 收到后自行重启重建连接池，等后台把被报槽修好。主链路
            # 不做同步兜底，也不把空出口身份写回调用方。
            if payload.get("restart_required"):
                log("⇢ %s 报 %s（%s）[req=%s] 无健康备槽，通知 9router 自重启"
                    % (slot["name"], req.get("reason"), payload.get("reason"),
                       rid or "-"))
            payload.update(_rotate_trace_fields(req, payload))
            self._send(200, payload)
            return
        try:
            ip = req.get("egress_ip")
            # 必须先拿结果再回传。写成 _send(200, _rotate_once(...)) 时，下面
            # 那次 _send(200, result) 会踩未定义的 result —— 每调一次 /rotate
            # 就抛一次 NameError，栈打满日志，client_gone 也永远记不上账。
            # 换线本身是成功的，坏的只是回传这一步。
            result = _rotate_once(
                _normalize_rotate_reason(req.get("reason")),
                _as_ts(req.get("request_started_at")),
                str(ip).strip() if isinstance(ip, str) and ip.strip() else None,
                _as_ts(req.get("egress_since")), ban_after)
        except Exception as e:
            log(f"✗ /rotate 异常: {e}")
            self._send(500, {"ok": False, "reason": "internal_error"})
            return
        # 主路径秒切失败：回包已经表达「无可用闸口，自己重启」，这里留一行
        # 生产日志，免得只能从 9router 侧反推 asip 到底有没有发出信号。
        result.update(_rotate_trace_fields(req, result))
        if result.get("restart_required"):
            log("⇢ 主路径无可用闸口[req=%s]，通知 9router 自重启" % (rid or "-"))
        # 换线与回传分成两步：换线成功但客户端已超时断开时，记账为「回传超时」
        # 而不是异常。否则 except 里再补写一次 500 必然二次 Broken pipe。
        if not self._send(200, result):
            _note_client_gone(result)

    def log_message(self, fmt, *a):
        pass


def _slot_keeper() -> None:
    """启动时补齐所有闸 —— 必须 6 条全 ok 才停。

    补闸靠探测：给每个坏闸挑候选线、PUT、探测；探测到 OK 的线由
    _probe_line_for_slots 自动塞进坏闸（或把持有闸标 ok）。挑不到/探不通
    就下一轮再来，直到全 ok 或耗尽重试。
    """
    for _ in range(40):
        time.sleep(2)
        if active_warp_tag():
            break
    else:
        log("⚠ 槽位分配放弃：sing-box 在 80s 内未就绪")
        return
    for attempt in range(60):
        _dedupe_slots()
        slots = consumer_slots()
        if not slots:
            return
        serving = _slot_serving_name()
        # 第七闸口由 _proxy_gate_keeper 维护，这里的补闸/就绪判断只看常规
        # 闸口：给它 claim WARP 线会把它从池闸变成线闸。
        bad = [s for s in slots
               if not _is_proxy_gate(s)
               and s["name"] != serving
               and _slot_health(s["name"])["status"] != "ok"]
        if not bad:
            log("✓ 所有槽位就绪")
            return
        for s in bad:
            pick, ip = _slot_claim(s)
            if not pick:
                continue
            _probe_line_for_slots(pick, ip)  # 探测 + 自动补闸/同步
        time.sleep(2)
    log("⚠ 槽位启动未全部就绪，剩余坏闸将由 /rotate 路径继续修复")


def _proxy_gate_check_sec() -> float:
    try:
        return float(CFG["intervals"].get("proxy_gate_check_sec", 60))
    except (TypeError, ValueError):
        return 60.0


def _proxy_gate_serving_window_sec() -> float:
    """第七闸口「在役」窗口：最近一次被指为重试目标之后多久算还在服务。

    9router 不会告诉 asip 它此刻是否还把流量打在 7897 上，只能按「最近
    被指过去」推断。窗口内按 proactive_rotate_sec 的节奏换节点保持地址
    新鲜；窗口外它是待命的兜底闸，不轮换（省节点配额），只维护健康结论。
    """
    try:
        return float(CFG["intervals"].get("proxy_gate_serving_window_sec", 300))
    except (TypeError, ValueError):
        return 300.0


def _proxy_gate_maintain(gate) -> None:
    """第七闸口单轮维护：死节点换掉、在役时按节奏换、健康结论刷新。

    健康结论的口径：进程活着（Clash API 应答）+ 当前节点未被判死 + 池
    采样健康（_PROXY_HEALTH_CACHE，TTL 120s，与落池判据共用）。None（查
    不到）不动旧结论 —— 抖动不是坏消息。
    """
    if not _warp().get("proxy_fallback_enabled", False):
        # 运维明确关了落池：池闸同步关掉，不跟健康探测较劲。
        _slot_mark(gate["name"], "bad", "disabled")
        return
    name = gate["name"]
    nodes, cur = _proxy_gate_nodes(gate)
    if not nodes:
        _slot_mark(name, "bad", "unreachable")
        return
    now = time.time()
    cur_dead = bool(cur) and cur in _DEAD_NODES
    serving = now - _PROXY_GATE_LAST_USED["ts"] < _proxy_gate_serving_window_sec()
    if not cur or cur == "direct" or cur_dead:
        _proxy_gate_rotate_node(gate, cur, "dead" if cur_dead else "no_node")
    elif (serving
          and now - _PROXY_GATE_LAST_ROTATE["ts"]
          >= float(CFG["intervals"]["proactive_rotate_sec"])):
        # 在役时才主动轮换；_pool_should_rotate 的池侧节奏只管主进程，
        # 池闸自己的节奏在这里。
        _proxy_gate_rotate_node(gate, cur, "proactive")
    # 健康结论（rotate 可能已改过状态，重新读）
    h = _slot_health(name)
    if h["status"] == "repairing":
        return
    nodes, cur = _proxy_gate_nodes(gate)
    if not nodes:
        _slot_mark(name, "bad", "unreachable")
        return
    if not cur or cur in _DEAD_NODES:
        _slot_mark(name, "bad", "pool_exhausted")
        return
    ok = _proxy_pool_healthy_cached(now)
    if ok is True:
        _slot_mark(name, "ok", None)
    elif ok is False:
        _slot_mark(name, "bad", "pool_dead")


def _proxy_gate_keeper() -> None:
    """第七闸口的持续维护线程：健康结论 + 池内轮换。

    常规闸口的补闸引擎是「探测 OK 自动塞线」；池闸的对应动作是池内换
    节点。轮换必须作用在它自己的 pool selector 上 —— 主循环的池轮换只
    翻主进程的 proxy selector，翻不到这台进程；不在 keeper 里做，就会出
    现「轮换的是主进程、实际流量在第七闸口」的错位。
    """
    gate = None
    for _ in range(40):
        time.sleep(2)
        gate = _proxy_gate_slot()
        if gate is None:
            return
        _, data = slot_singbox_api(gate, "/proxies/" + gate["pool_selector"],
                                   silent=True)
        if data:
            break
    else:
        if gate is not None:
            _slot_mark(gate["name"], "bad", "unreachable")
            log("⚠ 第七闸口放弃：sing-box 在 80s 内未就绪")
        return
    log("⇄ 第七闸口（代理池兜底）开始维护：%s port=%d clash=%d"
        % (gate["name"], gate["port"], gate["clash_api_port"]))
    while True:
        time.sleep(_proxy_gate_check_sec())
        gate = _proxy_gate_slot()
        if gate is None:
            return   # 配置热更后闸被关掉：线程自然退出
        try:
            _proxy_gate_maintain(gate)
        except Exception as e:
            log("⚠ 第七闸口维护异常: %s" % e)


def _start_rotate_server() -> None:
    """启动 UDS 接口线程。失败不影响主循环 —— 接口是增强，不是命脉。"""
    global _API_TOKEN
    cfg = _api_cfg()
    if not cfg.get("enabled"):
        log("⇄ rotate 接口未启用")
        return
    path = cfg["socket_path"]
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"✗ 清理残留 socket 失败: {e}")
        return
    _API_TOKEN = _ensure_api_token()

    class _Srv(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    try:
        srv = _Srv(path, _RotateHandler)
    except Exception as e:
        log(f"✗ rotate 接口启动失败: {e}")
        return
    os.chmod(path, 0o600)
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="rotate-api").start()
    log(f"⇄ rotate 接口就绪: {path}")


_TIER_STALE_KEYS = ("ip_ts", "last_ok_ts", "last_probe_ts", "quota_until",
                    "ban_noaddr_since", "next_probe_ts", "restart_ts",
                    "next_restart_ts", "rebind_ts")


def _clear_conclusions_on_tier_stop(tag: str, save: bool = True) -> None:
    """一条线被档位停用：清掉「描述上一个会话」的结论类字段。

    WHY 需要这个（对称补齐）：`_reset_stale_after_tier` 只挂在**解禁侧**
    （`_tier_reconcile` 的解禁分支）。设计者知道"停用期冻住的观测会污染
    解禁后的第一轮探测"，写了清理函数，却**只在一个方向接线**。

    停用侧不清理的直接后果（实测 2026-11-11）：30 条 WG 线的
    `last_verdict` 冻结在 27~33 小时前（blocked 9 条 / port_closed 8 条 /
    OK 6 条 / unknown 3 条），`ip` 早已是 None 而结论还指着"上一个会话
    的地址"。报表只能显示"档位停用 单元停用（线没跑）"，**把"这是档位
    切换前的旧值"显示成"运维停用"** —— 这正是"档位停用但状态台账里还有
    22 条"这条自检告警的根因。

    WHY 只清「结论类」字段、**保留 `ban_probes`**：
    - 必须清：`last_verdict` / `ip` / `last_ok_ip` / `fail_streak` /
      `next_probe_ts` / `next_restart_ts` / `last_probe_ts` —— 这些描述的
      是上一个会话的地址与结论，留着就是假信息。
    - 必须保留：`ban_probes` —— 它是**当天的历史证据**（今天这条线被判过
      多少次 quota），是渠道锁判据的原料。停用时清掉等于抹掉当天的账。
      而且它按"当天"自动过期（`_channel_ban_trim` 按 `time.localtime(ts)`
      过滤），不需要额外清理。
    - 同理保留：`ban_hold_until` / `stop_probe`（渠道锁状态，与档位无关，
      锁该到期就到期）；`penalty_restart_streak` / `futile_restart_streak`
      （重启次数是这条线的事实，不该因为档位切换被抹掉）。

    与解禁侧 `_reset_stale_after_tier` 的区别：解禁侧清得更狠（连
    `ban_probes` 一起清），因为线要回池子，旧样本会让它**回池即炸**；
    停用侧线根本不跑，没有"回池即炸"的风险，抹掉当天的账反而是丢失信息。
    """
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        st["last_verdict"] = ""
        st["fail_streak"] = 0
        st["ip"] = None
        st["last_ok_ip"] = None
        st["last_probe_ts"] = 0.0
        st["last_ok_ts"] = 0.0
        st["ip_ts"] = 0.0
        st["next_probe_ts"] = 0.0
        st["next_restart_ts"] = 0.0
        st["restart_ts"] = 0.0
        # `blocked_notes` 是"惩罚期短路"观测，与 `ban_probes` 同性质 ——
        # 保留（它是当天证据）。但如果盘上被写成脏形状，这里顺带归位。
        if not isinstance(st.get("blocked_notes"), list):
            st["blocked_notes"] = None
    # 批量清理（台账外残留可能几十条）时传 save=False，由调用方最后统一落盘，
    # 避免每条都 fsync 一次全表。
    if save:
        _save_warp_line_state(force=True)


def _reset_stale_after_tier(tag: str) -> None:
    """一条线从档位停用里被拉起：清掉停用期间冻住的观测。

    WHY 必须清：停用期间不探测，fail_streak 停在停用前那一刻（实测有上千
    次的），last_verdict / ip / quota_until 说的都是**上一个会话的地址**。
    带着这些回池子，第一轮探测就按旧结论判死 → 重启风暴 → 又被渠道锁，
    等于「解禁即炸」。
    """
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        st["last_verdict"] = ""
        st["fail_streak"] = 0
        st["ip"] = None
        st["last_ok_ip"] = None
        for k in _TIER_STALE_KEYS:
            st[k] = 0.0
        st["ban_hold_until"] = 0.0
        st["ban_probes"] = []
        st["stop_probe"] = False
        st["ban_noaddr_since"] = 0.0
    _save_warp_line_state(force=True)


def _tier_reconcile() -> None:
    """按档位对齐 WG 单元：档位外的停掉，档位内但没在跑的拉起来。

    此前只做「停」：远端从旧拓扑 pull + restart 之后，22 条 WG 单元仍在跑，
    `tiers.json` 写着 `wg: ""` 也拦不住 —— 文件对、实际不对。反过来「解禁」
    也只是改文件，单元照旧停着，整套 WG 一条线都没回来。两个方向都要对账。

    只碰**档位明确管到**的、且 members 里声明过的线：补位池（line23+）与
    档位无关。停用不 disable、不删单元，`systemctl start` 随时回来。
    """
    # 台账外单元（补位池 23+ / 缩容遗留）压根不出现在 members 里，下面的
    # 对账循环永远遍历不到它们，于是状态台账能留着 31~36 小时前的
    # OK/blocked 假结论（2026-11-11 审计实测 WARP-WG-23..30）。这些线不会
    # 被探测，结论永远冻结，报表只能把旧值显示成「运维停用」。
    # 清「结论类」字段、保留 ban_probes，与停用侧同一套口径。
    declared_wg = {m.get("tag") for m in (_warp().get("members") or [])
                   if m.get("kind") == "wg" and m.get("tag")}
    with _WARP_LINE_CV:
        orphan_wg = [t for t in list(_WARP_LINE_STATE)
                     if isinstance(t, str) and t.startswith("WARP-WG-")
                     and t not in declared_wg]
    if orphan_wg:
        for t in orphan_wg:
            _clear_conclusions_on_tier_stop(t, save=False)
        _save_warp_line_state(force=True)
        log(f"  · 档位对账：清掉 {len(orphan_wg)} 条台账外 WG 线的冻结结论")

    _, spec = warp_blocks.tier_spec()
    if spec is None:
        return                    # 没有档位文件 = 全开，不主动动单元
    stopped = started = 0
    for m in (_warp().get("members") or []):
        tag = m.get("tag")
        if m.get("kind") != "wg" or not tag:
            continue
        svc = m.get("service") or ""
        if not svc:
            continue
        try:
            # 只读查询直接 systemctl：`asip-unit` 白名单只放 start/stop/restart，
            # 走它问 is-active 会被拒，看起来像「没在跑」而漏停。
            st = subprocess.run(["systemctl", "is-active", svc],
                                capture_output=True, text=True,
                                timeout=10).stdout.strip()
        except Exception as e:
            log(f"  ⚠ 档位对账查询 {tag} 失败: {e}")
            continue
        if tag in _TIER_DISABLED:
            if st == "active":
                _unit_ctl("stop", svc)
                stopped += 1
            # 停用侧对称清理（2026-11-11 补齐）：原来只停单元、不清观测，
            # 导致状态表里留着一堆"档位切换前的旧结论"。详见
            # `_clear_conclusions_on_tier_stop` 的 WHY。
            # 注意：单元本来就没在跑时（st != "active"）也要清 —— 那正是
            # 这次问题的现场（22 条 WG 线单元全是 inactive，结论却冻了
            # 26~33 小时）。
            _clear_conclusions_on_tier_stop(tag)
            continue
        if st == "active":
            continue
        # 解禁侧：单元没在跑就拉起来。装没装过要判一下 —— members 声明了但
        # 单元文件不存在时 start 只会得到一条 systemd 报错。
        try:
            idx = int(m.get("index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if idx and _wg_line_state(idx) != "installed":
            continue
        if _unit_ctl("start", svc).returncode == 0:
            _reset_stale_after_tier(tag)
            started += 1
    if stopped:
        log(f"  · 档位对账：停用 {stopped} 条 WG 线（档位外的不该跑）")
    if started:
        log(f"  · 档位对账：拉起 {started} 条 WG 线，并清空停用期陈旧观测")


def _singbox_outbound_tags() -> set[str]:
    """当前 sing-box 配置里带 tag 的 outbound 集合（读不到就返回空集）。

    WHY 读不到时返回空集而不是报错：这份文件由订阅刷新流程原子替换，读到
    半截 JSON 是真实可能（实测刷新时先写 .tmp 再 os.replace，中间有窗口）。
    空集会被调用方当成「一个 outbound 都没有」，从而报出一堆假的「配置裂了」。
    """
    path = CFG["singbox"]["config_file"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    out = data.get("outbounds") if isinstance(data, dict) else None
    if not isinstance(out, list):
        return set()
    return {str(o.get("tag")) for o in out
            if isinstance(o, dict) and o.get("tag")}


def _scan_unit_still_on_disk(tag: str) -> bool:
    """这条线的 systemd 单元文件此刻是否还在磁盘上。

    WHY 需要查文件而不是查状态：单元**还在但已停** = 缩容残骸（渲染器
    没删文件）→ 要剪；单元**不在了** = 只剩状态表的陈旧记录 → 不算问题。

    「已停」不等于「没问题」：`h2-install.sh` 遍历的是渲染产物目录
    `.warp/h2/units/`，那里的台账外单元文件会被 `install` 回 systemd 并
    `enable` —— 单元文件还在磁盘上，就是僵尸复活的来源。

    **两种 tag 形态都要覆盖**：
      `WARP-H2-SCAN-<N>`  —— 探测时编造的假身份，编号直接对应单元编号。
      `WARP-H2-<COLO>-<N>` —— 旧编号体系的线（当前台账用别的 colo）。
                             它的单元编号是末尾的 N，但**当前台账里可能
                             已经有同编号的线**（编号是端口契约，不回收）——
                             这种情况下单元文件在磁盘上是**正常的**，不是残骸。
                             所以对普通 H2 tag 必须额外确认「这个编号在
                             当前台账里没有对应的线」。

    查 `systemctl list-unit-files` 而不是遍历目录：前者是 systemd 自己的
    账本，看到的才是真正会被加载的单元。
    """
    import re as _re
    m = _re.fullmatch(r"WARP-H2-SCAN-(\d+)", tag)
    if m:
        name = f"usque-h2-{m.group(1)}.service"
    else:
        m2 = _re.fullmatch(r"WARP-H2-(?:[A-Z]+)-(\d+)", tag)
        if not m2:
            return False
        idx = m2.group(1)
        # 当前台账里已有同编号的线 → 单元文件在磁盘上是正常的（编号不回收）
        try:
            # 用 os.path 而非 pathlib：daemon.py 顶部**没有** import pathlib，
            # 引入它就得改 import 块。跟随本文件既有惯例（全部用 os.path.join）。
            rows = json.loads(open(
                os.path.join(_PROJECT, ".warp", "h2", "channels.json"),
                encoding="utf-8").read())
            for r in rows if isinstance(rows, list) else []:
                if isinstance(r, dict) and str(r.get("index")) == idx:
                    return False
        except (OSError, ValueError):
            pass
        name = f"usque-h2-{idx}.service"
    try:
        r = subprocess.run(["systemctl", "list-unit-files", "--type=service",
                            "--no-legend", "--no-pager"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.split()[0] == name for line in (r.stdout or "").splitlines()
               if line.split())


def _config_consistency_check(now: float | None = None) -> dict:
    """跨源一致性自检的**异常隔离壳**（外层）。

    WHY 外面要包一层：这个自检是纯观测 —— 它只读五个真相源、只打日志、
    不改任何配置。按理说它是最安全的代码，但它**跑在 daemon 的启动关键
    路径上**（`main()` 里启动时调一次，之后每 30 分钟调一次）。

    实测事故（2026-11-11 17:22:31~17:27:21）：自检内部一个未定义的名字
    `PROJECT` 抛 NameError，异常一路冒泡到 `main()` → 进程退出 →
    systemd `Restart=always` + `RestartSec=10` 反复拉起 → **restart counter
    打到 23**，整个守护进程瘫了 5 分钟。

    WHY 必须隔离而不是"小心别写错"：这段代码要被反复修改（每次自检发现
    新的不一致就要加分类），而**一个观测函数不该有能力掀翻服务**。这里
    把故障模式从"崩溃循环"降级为"少一条告警"—— 告警照样写进日志，只是不
    再冒泡。异常本身要落日志，否则静默降级会变成静默失明。

    返回空报告（problems=0）表示"自检没跑成"，调用方据此能分辨
    "一切正常" 与 "我没查成"。后者在日志里是显式的。
    """
    try:
        return _config_consistency_check_impl(now)
    except Exception as e:                      # noqa: BLE001 —— 这里要兜住一切
        # 观测失败不能是致命错误，但必须留痕：否则「自检没跑成」和
        # 「自检跑成了没问题」在日志里长得一模一样。
        try:
            import traceback
            log(f"  ⚠ 配置自检本身出错（已忽略，不影响服务）：{e}")
            log("    " + traceback.format_exc().strip().replace("\n", "\n    "))
        except Exception:
            pass
        return {"problems": 0, "error": str(e), "selfcheck_failed": True}


def _config_consistency_check_impl(now: float | None = None) -> dict:
    """跨源一致性自检：把「配置分裂」当场变成一条明确的日志。

    WHY 需要这个：线路的真相分散在 5 个地方 —— `config.CFG["warp"]["members"]`
    （探测名单）、sing-box 配置的 outbounds（渲染产物）、systemd 单元（进程
    事实）、`.warp_line_state.json`（状态台账）、`.warp/tiers.json`（档位）。
    没有任何一处会校验其余几处，于是出现**半开状态**：文件里有线、daemon
    看不见。实测 2026-11-11：`.warp_line_state.json` 里 156 条 tag，只有 87
    条在 members 里；12 条 WG 线冻在 `last_verdict=blocked` 上 26 小时没动，
    因为它们的 systemd 单元是 `disabled` —— 探测循环连碰都不碰它们，
    报表上只印「档位停用 单元停用（线没跑）」，把「配置裂了」显示成「运维停用」。

    这里只**报告**，不修：改配置属于人的决定。自检的价值是让人不必等到
    25 小时后从报表里反推「为什么这条线没结论」。

    返回各类问题的条数，调用方据此决定要不要打日志。
    """
    members = {m.get("tag"): m for m in (_warp().get("members") or [])
               if m.get("tag")}
    now = time.time() if now is None else now
    declared_wg = {t for t, m in members.items() if m.get("kind") == "wg"}
    outs = _singbox_outbound_tags()
    # WHY 这里要兜底加载：`_config_consistency_check()` 是**只读**的自检，
    # 手工调用（排查、测试）时状态表可能还没加载 —— 那时 state_tags 会是空集，
    # 整个自检就退化成「什么都不���」并报 problems=0，形成**假阴性**：
    # 明明有 47 条残骸，却报「一切正常」。
    # 生产路径上启动自检在 `_load_warp_line_state()` 之后，不受此影响；
    # 这里补一手是为了让「手工调用」与「生产调用」结论一致。
    if not _WARP_LINE_STATE:
        try:
            _load_warp_line_state()
        except Exception:
            pass
    state_tags = set(_WARP_LINE_STATE.keys())
    tier_off = set(_TIER_DISABLED)

    # 分类一：状态台账里有、探测名单里没有的线。这些线 daemon 永远不会去探，
    # 状态会一直冻在最后一次观测上 —— 报表却把它们当「有结论」显示。
    orphan_state = sorted(state_tags - set(members))
    # 分类二：探测名单里有、但渲染产物里没有 outbound。探测会照探，但线
    # 根本不在 sing-box 里，换线时 selector 翻不到它。
    missing_out = sorted(t for t in members if t not in outs)
    # 分类三：探测名单里有、但档位停用的线。档位停用是对的（省资源），
    # 但它意味着这些线**不该有状态**；有状态说明档位切换时没清干净。
    # 分类三（问题）：档位停用的线，状态台账里却还留着**结论**。
    #   档位停用是对的（省资源），但它意味着这些线**不该有结论**；
    #   有结论说明档位切换时没清干净。
    #
    # WHY 判据是「有结论类字段」而不是「有台账条目」（2026-11-11 修正）：
    #   台账里**保留条目本身是对的** —— `ban_probes` 是当天的历史样本
    #   （渠道锁判据的原料），停用侧刻意不清它。所以「档位停用的线在台账
    #   里有条目」是**正常状态**，不是问题。
    #   真正的问题是「还留着结论」—— last_verdict / ip / last_probe_ts
    #   这些描述上一个会话的字段没被清掉，报表会把旧值显示成当前状态。
    #   旧判据导致每次启动都报 22 条假问题（清理已生效却仍在报警）——
    #   这类噪声会让真问题被淹没。
    tier_off_with_state = sorted(
        t for t in members
        if t in tier_off and t in state_tags
        and any(((_WARP_LINE_STATE.get(t) or {}).get(k)
                 for k in ("last_verdict", "ip", "last_probe_ts"))))
    # 分类四：状态台账里有、且是 WG、但不在 members 里的补位线（23+）。
    # 这些线是**闲置补位池**，与档位无关，本来就不该被探测 —— 单独归类，
    # 不与「真孤儿」混在一起报，否则真问题被淹没。
    spare_wg = sorted(t for t in orphan_state
                      if t.startswith("WARP-WG-") and t not in declared_wg)
    # 分类五：扫描发现的隧道线（`WARP-H2-SCAN-<N>`）。它们**没有渲染产物**
    # —— 单元在跑但 `channels.json` 里没有它的 tag。但「在跑」必须**查单元
    # 状态**，不能靠 tag 前缀推断：实测 47 条 SCAN 线里对应的单元全是
    # `inactive`（缩容时停掉的），只有曾经扫描到过而已。按前缀推断会把
    # 47 条死线报成「单元在跑却没人探」—— 那是假警报，会盖住真问题。
    #
    # 2026-11-11 补充：SCAN 残骸再分两档 ——
    #   单元还在磁盘（unit-files 里有）→ **台账外单元残骸**，是渲染器
    #     只增不减的直接证据，是真问题（可以用 reconcile 剪除）。
    #   单元文件已不在磁盘 → 只是状态表里的陈旧记录，不算问题。
    # 真的「在跑却没人探」要看 `_scan_running_tunnels` 的结果。
    scan_running = {tag for tag, _svc in _scan_running_tunnels(now).values()}
    scan_unprobed = sorted(t for t in orphan_state
                           if t.startswith("WARP-H2-SCAN-")
                           and t in scan_running)
    # 分类六：既不在渲染产物（channels.json）里、也不在补位池里的孤儿。
    # 这些是**缩容遗留** —— 单元删了、渲染产物也清了，只剩状态表里的残骸。
    # 它们不算故障，但状态表该清理，否则报表永远印着一堆「无样本」。
    # **台账外单元残骸**：状态表里有、探测名单里没有、且对应的 systemd
    # 单元文件**仍在磁盘上**。
    #
    # WHY 要按「单元文件是否还在磁盘」再分一档：状态表里的孤儿有四种截然
    # 不同的来源（实测 100 条）：
    #   47 条 `WARP-H2-SCAN-*` —— 缩容留下的单元残骸，单元文件仍在磁盘。
    #                             这才是「渲染器只增不减」的直接证据。
    #   14 条 `WARP-H2-LAX-*`  —— 旧编号体系的线（当前用 NRT/其他 colo）。
    #                             单元文件早没了，只剩状态记录。
    #   30 条 `WARP-WG-*`      —— 设计上的闲置补位池，本来就不该被探测。
    #    9 条 `WARP-CF-*`      —— CF 直连线的成员，属于另一套命名体系。
    # 后三类都不是故障。把它们混在一起报，会让「61 处不一致」这个数字
    # 既不准确、也不可信 —— 收到告警的人第一反应就是「又是老账」，然后
    # 真问题就被彻底埋掉了。**告警的精度决定它有没有人看。**
    retired = sorted(t for t in orphan_state
                     if t not in spare_wg and t not in scan_unprobed
                     and _scan_unit_still_on_disk(t))

    report = {
        "members": len(members),
        "outbounds": len(outs),
        "state": len(state_tags),
        "orphan_state": orphan_state,
        "missing_out": missing_out,
        "tier_off_with_state": tier_off_with_state,
        "spare_wg": spare_wg,
        "scan_unprobed": scan_unprobed,
        "retired": retired,
    }
    # 「问题」= 需要人来处理的不一致。三类都是：
    #   scan_unprobed  —— 单元在跑却不在探测名单（线在服务流量但没人探它）
    #   missing_out    —— 名单里有但 sing-box 没有 outbound（探测照探，selector 翻不到）
    #   tier_off_with_state —— 档位停用却留有状态（档位切换没清干净）
    #
    # **缩容遗留（`retired`）也算问题** —— 2026-11-11 修正。此前它被刻意
    # 排除在 problems 之外，理由是「61 条遗留会把真正要修的那几条淹掉」。
    # 但那个判断是错的：`retired` 不是「61 条陈旧记录」这么无害的东西，
    # 它是**单元文件仍在磁盘、状态表仍有残骸、而渲染器从不清理**的
    # 直接证据 —— 也就是下一次缩容会原样重演的根因。
    #
    # 当时实测：47 条 `WARP-H2-SCAN-*` 残骸（对应编号 3 与 16–61 的台账外
    # 单元），单元已停但状态表里永远冻结在最后一次观测值。报表把它们当作
    # 「有结论的线」显示，把「配置裂了」显示成「运维停用」。
    #
    # 「会淹掉真问题」的顾虑的正确解法是**分开报、分级报**，不是不报：
    # 遗留条数与真不一致条数分别给出，真问题在前、遗留在后，真问题不会被
    # 淹没。仍然排除的只有 `spare_wg`（WG 补位池）—— 它是设计上的闲置池，
    # 本来就不该被探测，不是问题。
    problems = (len(scan_unprobed) + len(missing_out)
                + len(tier_off_with_state) + len(retired))
    report["problems"] = problems
    if problems:
        detail = []
        if scan_unprobed:
            detail.append(f"扫描到的隧道单元在跑却不在探测名单 "
                          f"{len(scan_unprobed)} 条"
                          f"（{', '.join(scan_unprobed[:5])}）")
        if missing_out:
            detail.append(f"探测名单里有但 sing-box 没有 outbound "
                          f"{len(missing_out)} 条（{', '.join(missing_out[:5])}）")
        if tier_off_with_state:
            detail.append(f"档位停用但状态台账里还有 {len(tier_off_with_state)} 条"
                          f"（{', '.join(tier_off_with_state[:5])}）")
        if retired:
            detail.append(f"缩容遗留 {len(retired)} 条（单元已删、只剩状态残骸）")
        log(f"  ⚠ 配置自检：{problems} 处跨源不一致 —— " + "；".join(detail))
        log(f"    · 真相源：members {len(members)} 条 / sing-box outbounds "
            f"{len(outs)} 条 / 状态台账 {len(state_tags)} 条")
        if retired:
            log(f"    · 缩容遗留 {len(retired)} 条的单元文件仍在 systemd "
                f"（渲染器只增不减）：跑 deploy/reconcile_h2_units.py --apply "
                f"可剪除")
        if spare_wg:
            log(f"    · 另有 {len(spare_wg)} 条 WG 补位线（23+）不在探测名单，"
                f"属闲置池，不计入不一致")
    return report


def main():
    _acquire_single_instance_lock()
    # Log first, then fetch (avoid hanging before any output)
    log("=" * 50)
    log("auto-switch-ip daemon v2 启动 (加固版)")
    global _SLOT_BOOT_TS
    _SLOT_BOOT_TS = time.time()
    # 必须在拉起任何线之前对账：否则档位外的线会先跑起来再被停，白白换一次
    # WARP 会话（每次 start 都是一次 CF 注册动作）。
    #
    # WHY 对账要排在 `_load_warp_line_state()` **之后**（而不是像原来那样
    # 排在它前面）：`_tier_reconcile` 的解禁侧会调 `_reset_stale_after_tier`
    # → `_save_warp_line_state(force=True)`，而后者落的是**整个内存状态表**。
    # 如果此刻内存表还没加载盘上数据，落盘的就是"只剩被清理的那一条" ——
    # 实测 2026-11-11（/tmp 用 3 条假数据复现）：磁盘从 3 条被覆盖成 1 条，
    # 其余整条丢失，且**没有任何日志**。
    # 状态表加载只是读一个 JSON（156 条，微秒级），不碰任何单元，所以把
    # 对账挪到它之后，**不影响"先对账再拉线"的原意**。
    # 另有一道兜底：`_save_warp_line_state` 现在会拒绝"条数变少"的覆盖。

    all_nodes = get_all_proxy_nodes()
    _log_startup_banner(all_nodes)
    # 同步轮换接口：9router 撞 429 时会调它换出口（见 _rotate_once）。
    _start_rotate_server()

    # 并行消费者槽位：等 sing-box 起来后再分配，否则 PUT 打在还没就绪的
    # Clash API 上，六条槽位会全部记成失败。
    if consumer_slots():
        threading.Thread(target=_slot_keeper, daemon=True,
                         name="slot-keeper").start()

    # 第七闸口（代理池兜底）：与常规槽位 keeper 并行的独立维护线程 ——
    # 常规 keeper 只管 WARP 线，池闸的健康与池内轮换都归它。
    if _proxy_gate_slot() is not None:
        threading.Thread(target=_proxy_gate_keeper, daemon=True,
                         name="proxy-gate-keeper").start()

    # 初始化 node_info 表
    init_node_info_db()
    # 恢复跨重启的出口 IP 使用/污染记录与可用周期样本
    _load_warp_ip_used()
    _load_warp_ip_cycles()
    _load_warp_line_state()
    _load_node_ip_used()
    # 档位对账：放在状态表加载**之后**。
    # WHY 这个顺序（2026-11-11 修正）：档位对账的解禁侧会清观测状态并落盘，
    # 而落盘写的是整个内存状态表。状态表没加载就落盘 = 把 156 条覆盖成
    # 1 条（实测复现过）。详见上面 `_SLOT_BOOT_TS` 处的注释。
    # 对账本身仍赶在拉线之前：`_tier_reconcile` 只在拉线前做单元层面的
    # 启停，此刻状态表已就绪，对账的清理动作落在真实数据上。
    _tier_reconcile()
    # 跨源一致性自检：**不在这里跑**。
    # WHY 挪走：这段自检是纯观测，但它跑在 `main()` 的启动关键路径上。
    # 2026-11-11 17:22 实测事故 —— 自检内部一个未定义名字抛 NameError，
    # 异常冒泡到 main() → 进程退出 → systemd Restart=always 反复拉起，
    # restart counter 打到 23，服务瘫了 5 分钟。观测代码不该有能力掀翻服务。
    # 现在自检移到 `_start_warp_prober()` 之后（探测线程起得来之后），
    # 并且自身还有一层异常隔离壳（`_config_consistency_check`）。
    # 单实例探测器接管所有线路的探测/状态写入；旧守护仅在回滚开关下启动。
    # 同块轮回与换线/探测是两件事，两条路径下都要跑：它只看落地节点通不通。
    _start_node_rotation()
    prober_enabled = bool(_warp().get("prober_enabled", True))
    if prober_enabled:
        _start_warp_prober()
    else:
        # 脏线守护：常驻线程，脏 IP 一出现就重连换干净地址，不依赖主循环周期。
        if _line_worker_enabled():
            _start_line_workers()
        if not _line_worker_owns_scheduling():
            _start_dirty_line_janitor()
    # 跨源一致性自检：挪到这里（探测线程起得来之后）。
    # WHY 这个位置：① 状态表已加载（自检要对比状态台账与探测名单，状态没
    # 加载就比不出来）；② 探测线程已经在跑 —— 自检挂掉不再影响服务可用性；
    # ③ 它本身还有异常隔离壳（`_config_consistency_check`），双保险。
    # 只报告不修 —— 改配置是人的决定，daemon 擅自改会重排生产出口。
    _config_consistency_check()
    # 日志标签用的当前 WARP 出口 IP：启动时探一次，之后由换线路径更新
    global _ACTIVE_WARP_IP, _last_warp_switch_ok_ts
    _m = _warp_member(active_warp_tag())
    if _m:
        _ACTIVE_WARP_IP = warp_egress_ip(_m["port"])
        if not prober_enabled:
            _validate_startup_egress(_m)
    else:
        # 启动时就停在 proxy 上：落池时刻从此刻算，否则「落池后仍拦截」
        # 判据会拿 0 当起点，把落池前的旧 403/429 也算进去。
        global _proxy_dwell_since
        _proxy_dwell_since = time.time()
    _last_warp_switch_ok_ts = _load_segment_start(_ACTIVE_WARP_IP)

    # 状态
    cooldown_until = 0
    last_singbox_check = 0
    last_health_check = 0
    # 启动时不立即触发刷新；但配置文件缺失时必须立刻重建，否则 sing-box
    # 起不来且要等到下一个 1h 刷新周期（配置不入库，全新 clone/误删即此状态）
    # 配置存在也别等满一小时才刷：实测 18:05 重启后到 18:45 一次都没刷过，
    # 而新订阅的节点正等着进池。改为**启动后先跑一轮**（首次立即触发），
    # 之后按 1h 周期。代价是一次额外 fetch，收益是新节点最晚 1 小时内进块。
    last_sub_refresh = 0
    last_node_info_cleanup = time.time()
    last_proxy_check = 0
    last_summary_ts = 0
    last_proactive_rotate = 0
    last_config_check = 0.0

    global _RESTART_COUNT, _NODE_CALL_RESET_TS, _last_wal_checkpoint, _strategy_mode
    global _pool_delay_last
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
                # 实际托管 9router 的是系统级 `9router.service`；
                # `nine-router-src.service` 已 masked，且用户总线在此不可达。
                try:
                    # 本机托管 9router 的是 nine-router-src.service（源码构建版，20127 端口）。
                    # 上游写的 9router.service 在别的部署形态里存在，这里按实际在跑的挑，
                    # 两个都不在就直接放弃 —— 猜错单元只会白等一次重启。
                    for _unit in ("nine-router-src.service", "9router.service"):
                        if _unit_ctl("is-active", _unit).returncode == 0:
                            _unit_ctl("restart", _unit)
                            break
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
    last_watermark = -1
    next_error_eval = 0.0
    total = errors = consecutive_errors = 0
    transport_errors = 0
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
                    total, errors, details, consecutive_errors, reject_ratio, \
                        transport_errors = get_9router_error_rate()
                    # errors 只数出口级拦截，不能拿 total-errors 当成功数（402/超长
                    # 会被误算成成功）。成功数直接数 status，才是这个出口真的served 的流量。
                    ok_count = sum(1 for r in details if r.get("status") == "success")
                    _record_active_ip_success(total, total - ok_count)
            # 传输层 503 的自愈入口：窗口判据看不清（5 条 ~2s），用回看计数驱动
            # 探针 → 出口通就清僵死连接，僵死 socket 才会被强制重建。
            if observe and transport_errors > 0:
                _maybe_verify_transport_failures(transport_errors, now)
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
                    # ponytail: 6 字段（含 transport_errors），用 * 兜住尾部，
                    # 以后再加字段不会再在主循环里崩。
                    _, errors_after_switch, *_ = get_9router_error_rate(
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
            # 池轮换原本只在 opencode 落池时跑。但如果池选中的节点已经死了，
            # 非 opencode 流量（9router 的其它 provider）走 7890 就是 100% 失败 ——
            # 此时无论 opencode 在哪都必须换掉它。
            #
            # 注意这里的 `opencode_out_on_warp()` 是**外部依赖**，不是池自己的
            # 状态：它只回答「WARP 渠道此刻是否在承载 opencode」。池该不该轮换
            # 仍取决于池自己 —— 见 `_pool_should_rotate()`，两者的判据分开写，
            # 这样 WARP 侧的改变不会静默改变池的节奏。
            _pool_current_dead = bool(cached_current_proxy) and cached_current_proxy in _DEAD_NODES
            if _pool_should_rotate(opencode_out_on_warp(), _pool_current_dead) \
                    and now - last_proactive_rotate > CFG["intervals"]["proactive_rotate_sec"]:
                last_proactive_rotate = now
                if cached_all_nodes:
                    _submit_once("pool-proactive-rotate", _rotate_pool_proactive,
                                 cached_current_proxy, cached_all_nodes, now)

            # ---- 3b. 清理过期 node_info (每 10 分钟) ----
            if now - last_node_info_cleanup > 600:
                last_node_info_cleanup = now
                _submit_once("node-info-cleanup", _cleanup_node_info, cached_all_nodes)

            # ---- 3b2. 跨源一致性自检 (每 30 分钟) ----
            # 配置是运行期会变的：订阅刷新改 sing-box 配置、档位切换改
            # tiers.json、单元启停改 systemd。任一处变了都可能让「名单」与
            # 「事实」再次错位。自检只在**发现不一致时**打日志，正常时静默 ——
            # 每 30 分钟一条「一切正常」会把真正的告警淹掉。
            if now - last_config_check > 1800:
                last_config_check = now
                _submit_once("config-consistency-check", _config_consistency_check)

            # ---- 3c0. 双向兜底的反向：停在 proxy 上时池挂了才切回 ----
            # 内部要探测整池健康（最慢 15s），丢后台。
            _maybe_failback_to_warp_bg(now)

            # 任一隧道出口疑似脏就重连；掉在 proxy 池上时也要继续，
            # 否则池本身就是脏地址时永远回不来。单实例探测器启用后由它独占
            # 探测/重启，旧补刷与 line_worker 都不再参与，避免同线双刷。
            if not prober_enabled and not _line_worker_owns_scheduling():
                _prewarm_standby_warp(now)

            # ---- 5. 池节点健康检查 (每 300s) + 延迟样本补刷 ----
            # 这一整块只服务代理池，与 WARP 渠道的线探测无关：
            #   `_collect_test_nodes` / `_apply_test_results` 读写的都是池状态
            #   （_DEAD_NODES 等），入参 `cached_all_nodes` 是 selector `proxy`
            #   的成员快照。渠道侧的健康由 `_warp_prober_loop` 各自维护。
            # 延迟补刷有自己的节奏：健康检查在「首轮完成 && 有快照」之前一直不跑，
            # 而池的选点完全依赖这份延迟表。独立之后哪怕 WARP 还没起来，池也有数据。
            # 冷启动/样本集体过期时按 1s 一拍连续排空；铺满后回到 300s 节奏。
            if _pool_delay_bootstrapping(cached_all_nodes):
                _pool_delay_interval = 1.0
            else:
                _pool_delay_interval = CFG["intervals"]["node_health_sec"]
            if now - _pool_delay_last > _pool_delay_interval and \
                    cached_all_nodes and cached_current_proxy:
                _pool_delay_last = now
                _submit_once("pool-delay-refresh", _refresh_pool_delays,
                             cached_all_nodes, cached_current_proxy)
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

            # ---- 6b. 沉睡池体检 (6h；状态文件不存在则立即跑) ----
            # 与 6 同属配置刷新链路：这里生产 .dormant_state.json，下一次
            # make_config 才会读到实测判死名单。不另开 timer / cron。
            try:
                _maybe_run_dormant_probe()
            except Exception as e:
                log(f"  ⚠ 沉睡池体检调度异常：{e}")

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

            # ---- 8a2. 渠道锁到期还原（每分钟对账一次） ---
            # 锁到次日 08:00 才到期，不是每 tick 都要跑；这条兜底是为了
            # 「锁定后 daemon 重启过、没人再触发账本」的情况。
            if now - _CHANNEL_REHOME_TS[0] > 60:
                _CHANNEL_REHOME_TS[0] = now
                _submit_once("channel-rehome", _rehome_held_lines, now)

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
                       f"503={transport_errors} "
                       f"err>{(err_threshold*100):.0f}%→切"
                       f"冷却至={time.strftime('%H:%M:%S', time.localtime(cool_until)) if cool_until>now else '-'}"
                       f" dead={dead_count} cool={cool_count} 策略={_strategy_mode}"
                       f" 观测={'on' if observe else 'off(blind→时间轮换)'}"
                       f" 轮换W={_proactive_progress}轮换P={_POOL_PROACTIVE_LABEL}"
                       f" 夜锁={_night_label(now)}"
                       f" 渠锁={_channel_ban_label(now)}"
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
    elif len(sys.argv) > 1 and sys.argv[1] == "slots":
        raise SystemExit(cli_slots(sys.argv[1:]))
    elif len(sys.argv) > 1 and sys.argv[1] == "ip-history":
        cli_ip_history(sys.argv[1:])
    else:
        main()