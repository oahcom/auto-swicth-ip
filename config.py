#!/usr/bin/env python3
"""
Shared configuration for auto-switch-ip.
Both daemon.py and proxy_pool.py import from this module.
"""

import os
import sys
import time

# ============= 配置常量 =============
SECRET_FILE = "/home/administrator/.singbox_secret"
def _read_secret():
    try:
        st = os.stat(SECRET_FILE)
        if st.st_mode & 0o077:
            raise PermissionError(
                f"密钥文件 {SECRET_FILE} 权限过宽 ({oct(st.st_mode & 0o777)})，"
                f"请执行 chmod 600 {SECRET_FILE}")
        with open(SECRET_FILE) as f:
            return f.read().strip()
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✗ 读取密钥文件失败: {e}")
        return None

SINGBOX_SECRET = _read_secret()
if SINGBOX_SECRET is None or SINGBOX_SECRET == "":
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] WARNING: SINGBOX_SECRET not set — create {SECRET_FILE} (chmod 600) with a base64 secret", file=sys.stderr)
    SINGBOX_SECRET = ""

CFG = {
    "9router": {
        "db_path": "/home/administrator/.9router/db/data.sqlite",
        "base": "http://127.0.0.1:20128",
    },
    "singbox": {
        "clash_api": "http://127.0.0.1:9090",
        "secret": SINGBOX_SECRET,
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
        "ttl_sec": 300,  # 5 minutes - shared by proxy-global.js and daemon
    },
    "log_file": "/home/administrator/dkk-projects/auto-switch-ip/daemon.log",
    "log_format": "%Y-%m-%d %H:%M:%S",
}

# ============= 便捷常量 =============
NODE_INFO_DB = CFG["9router"]["db_path"]
SINGBOX_API = CFG["singbox"]["clash_api"]
SINGBOX_SECRET = CFG["singbox"]["secret"]
SELECTOR = CFG["singbox"]["selector"]
MIXED_PORT = CFG["singbox"]["mixed_port"]
BAD_NODES_TTL = CFG["bad_nodes"]["ttl_sec"]
LOG_FORMAT = CFG["log_format"]
RECENT_WINDOW = CFG["thresholds"]["recent_window"]

# ============= FREE_PROVIDERS 常量 =============
# OPENCODE_FREE_PROVIDER: 9router providerStrategies 中的 key = "opencode"
# provider 值在 requestDetails 表中也是 "opencode"
OPENCODE_FREE_PROVIDER = "opencode"
# 辅助检查用的 provider 集合（仅用于 query 请求历史）
FREE_PROVIDERS = {OPENCODE_FREE_PROVIDER, "kiro", "nvidia", "ollama", "xiaomi-tokenplan"}
