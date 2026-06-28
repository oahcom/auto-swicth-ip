#!/usr/bin/env python3
"""
Shared configuration for auto-switch-ip.
Both daemon.py and proxy_pool.py import from this module.
"""

import os
import time

# ============= 配置常量 =============
SECRET_FILE = "/home/administrator/.singbox_secret"
def _read_secret():
    try:
        # 检查权限：必须是 0o600 或更严格
        st = os.stat(SECRET_FILE)
        if st.st_mode & 0o077:
            log_msg = f"⚠ 密钥文件 {SECRET_FILE} 权限过宽 ({oct(st.st_mode & 0o777)})，建议 chmod 600"
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {log_msg}")
        with open(SECRET_FILE) as f:
            return f.read().strip()
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✗ 读取密钥文件失败: {e}")
        return None

SINGBOX_SECRET = _read_secret()
if SINGBOX_SECRET is None:
    raise RuntimeError(f"SINGBOX_SECRET not set: create {SECRET_FILE} with a base64 secret")

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
        "startup_grace_sec": 5,
        "max_restart_attempts": 3,
    },
    "okz": {
        "proxy_url": "http://127.0.0.1:6696",
        "name": "okz",
    },
    "intervals": {
        "monitor_sec": 2,
        "proactive_rotate_sec": 30,
        "cooldown_after_switch_sec": 4,
        "singbox_watchdog_sec": 30,
        "node_health_sec": 300,
        "subscription_refresh_sec": 3600,
        "proxy_check_sec": 60,
    },
    "thresholds": {
        "recent_window": 5,
        "opencode_min_success_rate": 0.9,
        "node_test_timeout_ms": 10000,
        "node_test_url": "https://www.gstatic.com/generate_204",
    },
    "log_file": "/home/administrator/dkk-projects/auto-switch-ip/daemon.log",
}

# ============= 便捷常量 =============
NODE_INFO_DB = CFG["9router"]["db_path"]
SINGBOX_API = CFG["singbox"]["clash_api"]
SINGBOX_SECRET = CFG["singbox"]["secret"]
SELECTOR = CFG["singbox"]["selector"]
STALE_THRESHOLD_SEC = 300

# ============= FREE_PROVIDERS 常量 =============
FREE_PROVIDERS = {"opencode", "kiro", "nvidia", "ollama"}