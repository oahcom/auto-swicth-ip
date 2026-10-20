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
        "cooldown_after_switch_sec": 60,
        # 单节点 429 冷却 60s：除非全体限流，否则池子里总有未冷却节点；
        # 全体限流时换节点本就无意义。
        "429_cooldown_sec": 60,
        "singbox_watchdog_sec": 30,
        "node_health_sec": 300,
        "subscription_refresh_sec": 3600,
        "proxy_check_sec": 60,
        # proxy → WARP 的三条回切依据（任一成立才回切）。WARP 恢复本身不是依据：
        # 只要代理池还能服务流量，就该让它服务完 —— 它是兜底资源，不是临时跳板。
        "proxy_fail_streak": 3,        # 连续 N 次池健康探测全失败
        "proxy_blocked_threshold": 5,  # 落池后 opencode 仍被 403/429 拦截 ≥N 条
        "proxy_dead_ratio": 0.5,       # 池黑名单节点占比 ≥N
    },
    "thresholds": {
        "recent_window": 10,
        "reject_window": 50,   # 403 客户端拒绝占比的统计窗口（10 条太小，噪声会抖动）
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
    # WARP 主备隧道：members 顺序即优先级，daemon 在被标记时切到另一条，
    # 并异步重连闲置线刷新其会话 IP（入口段无断流）。
    "warp": {
        "selector": "opencode-out",
        "socks_host": "127.0.0.1",
        "members": [
            {"tag": "WARP-CF", "port": 1080, "service": "usque-warp.service"},
            {"tag": "WARP-CF-B", "port": 1081, "service": "usque-warp-b.service"},
            {"tag": "WARP-CF-C", "port": 1082, "service": "usque-warp-c.service"},
            {"tag": "WARP-CF-D", "port": 1083, "service": "usque-warp-d.service"},
            {"tag": "WARP-CF-E", "port": 1084, "service": "usque-warp-e.service"},
        ],
        # 换线触发阈值（两类都按出口 IP 判定，换出口即拿到新配额/新信誉）：
        # 两者都是 1：任何一条出口级拦截出现即换，不再等累计。
        # 曾把 429 阈值设为 5（"429 太频繁，单条就换会让隧道反复重启"），
        # 但实测 22:35~22:38 在 429 风暴里每多等一条就是继续失败：
        # 阈值 5 期间流量全废，而冷却只有 20s，换线成本远低于等待成本。
        "block_threshold": 1,
        "rate429_threshold": 1,
        # 主动轮换：当前出口服务满 N 条请求就换线，不等 403 —— 报错后的切换
        # 已经冲断了在途对话。
        # 取 60：实测 61 段（初错后自动换线）里 30% 零错误、每段成功中位 14 条，
        # 说明多数出口在按请求数换掉之前还能继续服务；按 20 条换线等于把可用
        # 地址提前烧掉，而 IP 池只有 67 个 /64，复用过的成功率 57.7% vs 新的
        # 80.8%、最热两个前缀仅 32%/24%。60 ≈ 中位成功数的 4 倍，也让两次
        # 错误触发换线之间的正常流量能跑完。
        "proactive_after_requests": 60,
        # 备线「可服务性」探针：暂未实现。曾尝试经目标隧道的 SOCKS 直接向
        # opencode 发最小请求，但免费层的客户端指纹校验无法通过 —— 试过
        # /zen/v1 与 /inference/v1 两个端点、v1/v2 两种 UA、cli/desktop 两种
        # client、完整 read+shell 工具定义与 9router 的 applyFingerprintTools，
        # 全部返回 403 FreeTierError（真实 9router 请求里带的是 exec_command
        # 等另一套工具，指纹规则比源码注释描述的更复杂）。只有 9router 自己
        # 发出的请求能通过。在没有可靠探针之前，主动轮换仍是盲切。
        # 每隔多久预热一条闲置备线（重启它换会话 IP）。ip_reuse_guard_sec=24h，
        # 不预热的话换线时只有刚补刷的一条是新鲜的，流量会在两条线间往返。
        "prewarm_sec": 120,
        # 最小可用周期重算间隔：每 300s 采一次 24h 内 cycle>0 的样本最小值，
        # 驱动主动轮换时间判据。无样本时回退到 proactive_after_requests。
        "min_cycle_recalc_sec": 300,
        # 换线前校验目标线的出口 IP：窗口内用过的地址必须先刷新会话再切。
        # 429/403 都按出口 IP 判定，切回一个还在限流窗口里的地址等于没换。
        # 86400s（24h）：opencode 对同一个出口 IP 的 403/429 记忆很长，实测出口
        # 可用周期 p50 仅 19s，几十分钟内切回几乎必然再 403。历史教训：3600s 窗口下
        # 25.2 / 28.3 / 49.1 分钟的复用间隔真实发生过；900s 窗口下 18:58:05 用过的
        # 2a09:bac5:1efb:281e::3ff:42 在 10.6 分钟后被原样切回，53 秒后即爆发。
        "ip_reuse_guard_sec": 86400,
        # 被标记（403/429）的 IP 保留多久不出候选池。与 ip_reuse_guard_sec 同量级：
        # 一个 10 分钟前被烧的地址，不该在下次换线时因"窗口过期"又被选中。
        "blocked_ttl_sec": 86400,
        # 无可用备线时原地重连的上限次数与退避
        "max_attempts": 3,
        "retry_backoff_sec": [2, 5, 10],
        "ready_timeout_sec": 12,
        # 换线后短暂静默让在途请求落定。固定值，不随连续失败放大 ——
        # 只要目标线能拿到窗口外的新鲜 IP，换线就值得做；拉长冷却只会让
        # 错误持续积累（实测 2^n 退避曾在风暴期锁死到 600s，九分钟零换线）。
        "cooldown_ok_sec": 20,
        # 换线动作本身失败（两条线都不通）时的重试间隔
        "cooldown_fail_sec": 60,
        # 换线后多久刷新被换下的闲置线。太短会掐断仍在走它的在途请求
        # （upstream connection lost），太长则赶不上下一次换线（实测标记速度
        # 约 20~30s），届时同步守卫又要多等 7~9s。
        "refresh_delay_sec": 20,
        # 刷新前等待该线闲下来的上限，超出则等下一轮（强刷会掐断长命连接
        # → upstream connection lost）。判"闲"看字节是否还在增长，不看连接数：
        # 活跃线上恒有 7~10 条连接且从不归零，按连接数等必然超时。
        "idle_wait_sec": 40,
        # 被换下的线等不到空闲时反复重试的总时长。单次超时就放弃会让这条线
        # 永远停在旧地址上：实测 WARP-CF-C 被换下 8 次，7 次放弃，13 次换线里
        # 7 次切回同一个 10 分钟前的地址。
        "ip_refresh_retry_sec": 600,
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
REJECT_WINDOW = CFG["thresholds"]["reject_window"]

# ============= FREE_PROVIDERS 常量 =============
# OPENCODE_FREE_PROVIDER: 9router providerStrategies 中的 key = "opencode"
# provider 值在 requestDetails 表中也是 "opencode"
OPENCODE_FREE_PROVIDER = "opencode"
# 辅助检查用的 provider 集合（仅用于 query 请求历史）
FREE_PROVIDERS = {OPENCODE_FREE_PROVIDER, "kiro", "nvidia", "ollama", "xiaomi-tokenplan"}
