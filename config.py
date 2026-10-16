#!/usr/bin/env python3
"""
Shared configuration for auto-switch-ip.
daemon.py imports from this module.
"""

import os
import sys
import time

# ============= 配置常量 =============
SECRET_FILE = "/home/administrator/.singbox_secret"

# Module-level cache for secret + mtime
_SINGBOX_SECRET: str = ""
_SINGBOX_SECRET_MTIME: float = 0

def get_singbox_secret() -> str:
    """Get sing-box secret with mtime-based auto-refresh."""
    global _SINGBOX_SECRET, _SINGBOX_SECRET_MTIME
    try:
        st = os.stat(SECRET_FILE)
        mtime = st.st_mtime
        if _SINGBOX_SECRET and mtime == _SINGBOX_SECRET_MTIME:
            return _SINGBOX_SECRET
        if st.st_mode & 0o077:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚠ Secret file loose perms ({oct(st.st_mode & 0o777)})")
        with open(SECRET_FILE) as f:
            _SINGBOX_SECRET = f.read().strip()
        _SINGBOX_SECRET_MTIME = mtime
        return _SINGBOX_SECRET
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✗ 读取密钥文件失败: {e}")
        return ""

# 启动时校验密钥 (活值) — 不冻结常量, 避免 secret 轮换后失效
if not get_singbox_secret():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] WARNING: sing-box secret 未设置 — 在 {SECRET_FILE} (chmod 600) 中写入 base64 secret", file=sys.stderr)

CFG = {
    "9router": {
        "db_path": "/home/administrator/.9router/db/data.sqlite",
        "base": "http://127.0.0.1:20128",
    },
    "singbox": {
        "clash_api": "http://127.0.0.1:9090",
        "secret": "",  # 运行时通过 get_singbox_secret() 刷新, 此处留空占位
        "selector": "proxy",
        "binary": "/home/administrator/dkk-projects/auto-switch-ip/sing-box",
        "config_file": "/home/administrator/dkk-projects/auto-switch-ip/singbox-config.json",
        "startup_grace_sec": 10,  # 增加到 10s，给 sing-box 更多初始化时间
        "max_restart_attempts": 3,
        "mixed_port": 7890,
    },
    "okz": {
        "proxy_url": "http://127.0.0.1:6696",
        "name": "okz",
        "sub_url": "https://s-dywrwizazu.cn-shanghai.fcapp.run/okz/sub?token=aa0f63eb1e9a65d23fcae0dcc8a867ab",
    },
    "hd23n": {
        "name": "hd.23n.club",
        "sub_url": "http://hd.23n.club/api/v1/client/subscribe?token=5b4815ade93497224153e0bb0ae60cc6",
    },
    "dasho": {
        "name": "dasho.pqjc.site",
        "sub_url": "https://dasho.pqjc.site/api/v1/pq/d6b05bf5edc2d676335d0726371868bf",
    },
    "dasho_alt": {
        "name": "dasho.xn--cp3a08l.com",
        "sub_url": "https://dasho.xn--cp3a08l.com/api/v1/pq/d6b05bf5edc2d676335d0726371868bf",
    },
    "intervals": {
        "monitor_sec": 2,
        "proactive_rotate_sec": 60,
        "cooldown_after_switch_sec": 10,
        "singbox_watchdog_sec": 30,
        "node_health_sec": 300,
        "subscription_refresh_sec": 3600,
        "proxy_check_sec": 60,
    },
    "thresholds": {
        "recent_window": 10,
        "opencode_min_success_rate": 0.9,
        "node_test_timeout_ms": 15000,  # 放宽到 15s，避免慢节点误判
        "node_test_url": "https://www.gstatic.com/generate_204",
        "pressure_error_rate": 0.3,   # 429 率超过此阈值 → 切均匀分散
        "normal_error_rate": 0.1,     # 429 率低于此阈值 → 切回延迟优先
    },
    "bad_nodes": {
        "ttl_sec": 300,       # 5 minutes - shared by proxy-global.js and daemon
        "max_ttl_sec": 604800,  # 7 days max TTL (replaces float('inf') permanent blacklist)
    },
    "log_file": "/home/administrator/dkk-projects/auto-switch-ip/daemon.log",
    "log_format": "%Y-%m-%d %H:%M:%S",
}

# ============= 便捷常量 =============
# ponytail: SINGBOX_SECRET 不在此导出为模块级常量——CFG["singbox"]["secret"] 是 import 时冻结的
# 旧快照，轮换后不会刷新。调用方必须走 get_singbox_secret()（mtime 自动刷新）。
NODE_INFO_DB = CFG["9router"]["db_path"]
SINGBOX_API = CFG["singbox"]["clash_api"]
SELECTOR = CFG["singbox"]["selector"]
MIXED_PORT = CFG["singbox"]["mixed_port"]
BAD_NODES_TTL = CFG["bad_nodes"]["ttl_sec"]
BAD_NODES_MAX_TTL = CFG["bad_nodes"]["max_ttl_sec"]
LOG_FORMAT = CFG["log_format"]
RECENT_WINDOW = CFG["thresholds"]["recent_window"]

# ============= FREE_PROVIDERS 常量 =============
# OPENCODE_FREE_PROVIDER: 9router providerStrategies 中的 key = "opencode"
# provider 值在 requestDetails 表中也是 "opencode"
OPENCODE_FREE_PROVIDER = "opencode"
# 辅助检查用的 provider 集合（仅用于 query 请求历史）
FREE_PROVIDERS = {OPENCODE_FREE_PROVIDER, "kiro", "nvidia", "ollama", "xiaomi-tokenplan"}
