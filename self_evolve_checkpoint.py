#!/usr/bin/env python3
"""
Self-Evolution Checkpoint for auto-switch-ip monitoring loop.

Meta-goal: The monitoring loop should evolve its own checks.
- Tracks what checks have run, anomalies found, blind spots
- Auto-generates new targeted checks for recurring patterns
- Lightweight, runs inline with existing cron (~100ms overhead)
"""

import json
import os
import re
import time
import subprocess
import sys
from pathlib import Path
from collections import defaultdict, Counter

STATE_FILE = Path(__file__).parent / ".monitor_state.json"
PROJECT_DIR = STATE_FILE.parent
_BAD_NODES_FILE = PROJECT_DIR / ".bad_nodes.json"
# 复用 daemon 的 bad-node 过期常量（单源真值），避免魔表。
try:
    sys.path.insert(0, str(PROJECT_DIR))
    from config import BAD_NODES_TTL as _BAD_NODES_TTL
except Exception:
    _BAD_NODES_TTL = 300


def _write_bad_nodes(bad: dict) -> None:
    """把归因出的坏出口节点写入 daemon 共享 .bad_nodes.json（原子 read-modify-write）。

    格式与 daemon 完全一致：{node: {"expire": now+TTL, "reason": "egress_error_rate"}}。
    daemon 的 cleanup_dead_ttl 已读取此文件并受 _POOL_MIN_RATIO 阀门保护，故写入即接入拉黑闭环。
    """
    now = int(time.time())
    try:
        data = json.loads(_BAD_NODES_FILE.read_text()) if _BAD_NODES_FILE.exists() else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    for node in bad:
        data[node] = {"expire": now + _BAD_NODES_TTL, "reason": "egress_error_rate"}
    tmp = _BAD_NODES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(_BAD_NODES_FILE)

# 与 daemon.py 阀门一致：允许 70% 节点拉黑（_POOL_MIN_RATIO=0.3）。
# 天花板从 Clash API 实时节点数 derivation，防止订阅变更后 65 这类魔表失效。
BLACKLIST_CEILING_RATIO = 0.7
_FALLBACK_CEILING = 65  # 当 Clash API 不可达时退避（当前 93 节点池 *0.7）


def _clash_pool_size():
    """从 sing-box Clash API 读取可用节点总数；失败退为 _FALLBACK_CEILING。"""
    try:
        sys.path.insert(0, str(PROJECT_DIR))
        import config  # 复用单源真值
        import requests
        r = requests.get(
            config.CFG["singbox"]["clash_api"] + "/proxies/" + config.CFG["singbox"]["selector"],
            headers={"Authorization": "Bearer " + config.get_singbox_secret()},
            timeout=5,
        )
        if r.status_code == 200:
            return len(r.json().get("all", []))
    except Exception:
        pass
    return _FALLBACK_CEILING

# Default check suite - evolves over time
DEFAULT_CHECKS = [
    {"name": "7890_connectivity", "enabled": True, "interval": 1},
    {"name": "dead_nodes", "enabled": True, "interval": 1},
    {"name": "bypass_events", "enabled": True, "interval": 1},
    {"name": "bounded_pool_pressure", "enabled": True, "interval": 5},
    {"name": "node_switch_distribution", "enabled": True, "interval": 5},
    {"name": "response_latency_p95", "enabled": True, "interval": 5},
    {"name": "upstream_failure_rate", "enabled": True, "interval": 5},
    {"name": "repeat_failure_nodes", "enabled": True, "interval": 5},
    {"name": "egress_error_attribution", "enabled": True, "interval": 5},
]

# Anomaly patterns that trigger new check activation.
# threshold 单位与对应 check 的 metric 一致：dead/bypass/upstream 为次数(整数)，
# skew(节点分布)为 0~1 比率，latency 为秒。
ANOMALY_PATTERNS = {
    "dead_nodes_spike": {"threshold": 30, "check": "dead_nodes"},
    "bypass_spike": {"threshold": 2, "check": "upstream_failure_rate"},
    "node_skew_spike": {"threshold": 0.4, "check": "node_switch_distribution"},
    "latency_degradation": {"threshold": 5.0, "check": "response_latency_p95"},
    "pool_pressure": {"threshold": 3, "check": "bounded_pool_pressure"},
    "egress_quality_drop": {"threshold": 0.5, "check": "egress_error_attribution"},
}


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "round": 0,
        "checks": DEFAULT_CHECKS.copy(),
        "anomalies": [],
        "history": [],
        "enabled_checks": ["7890_connectivity", "dead_nodes", "bypass_events", "bounded_pool_pressure", "node_switch_distribution", "response_latency_p95", "upstream_failure_rate", "repeat_failure_nodes"],
    }


def save_state(state):
    # Only keep last 100 rounds
    if len(state.get("history", [])) > 100:
        state["history"] = state["history"][-100:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def run_check(name, context, state=None):
    """Run a specific check and return (status, metric_value, details)."""
    try:
        today = time.strftime("%Y-%m-%d")
        if name == "7890_connectivity":
            out = subprocess.run(
                ["curl", "-m", "5", "-s", "-o", "/dev/null",
                 "-w", "%{http_code} %{time_total}", "http://127.0.0.1:7890"],
                capture_output=True, text=True, timeout=10
            )
            code = out.stdout.split()[0] if out.stdout.strip() else ""
            ok = code in ("400", "204")  # 代理端口裸 GET 返回 400/204 即连通
            lat = float(out.stdout.split()[1]) if out.stdout.split()[1:] else 999
            return ok, lat, out.stdout.strip()
        elif name == "dead_nodes":
            # 权威来源是 daemon 独占写入的 .bad_nodes.json（未过期条目），
            # 而非 daemon.log 全量历史里的最后一行 dead=（滞后且含陈旧值）。
            bn = STATE_FILE.parent / ".bad_nodes.json"
            now_ts = int(time.time())
            dead = 0
            if bn.exists():
                try:
                    data = json.loads(bn.read_text())
                    dead = sum(1 for v in data.values()
                               if isinstance(v, dict) and v.get("expire", 0) > now_ts)
                except Exception:
                    dead = 0
            # 与 daemon.py 阀门一致：允许 70% 节点被拉黑（_POOL_MIN_RATIO=0.3）。
            # 天花板从 Clash API 实时节点数推导，防止订阅变更后魔表失效。
            pool = _clash_pool_size()
            ceiling = int(pool * BLACKLIST_CEILING_RATIO)
            return dead <= ceiling, dead, f"dead={dead}/{ceiling}"

        elif name == "bypass_events":
            out = subprocess.run(
                ["grep", "-a", "BYPASS\\|bypass", "/home/administrator/dkk-projects/auto-switch-ip/daemon.log"],
                capture_output=True, text=True, timeout=5
            )
            count = len([l for l in out.stdout.splitlines() if today in l and ("enabled" in l.lower() or "BYPASS" in l.upper())][-10:])
            return count == 0, count, f"bypass_count={count}"

        elif name == "bounded_pool_pressure":
            # Check if _BoundedPool ever logged queue full warning
            out = subprocess.run(
                ["grep", "-a", "pool.*满\\|queue.*full", "/home/administrator/dkk-projects/auto-switch-ip/daemon.log"],
                capture_output=True, text=True, timeout=5
            )
            count = len([l for l in out.stdout.splitlines() if today in l])
            return count == 0, count, f"pool_warnings={count}"

        elif name == "node_switch_distribution":
            out = subprocess.run(
                ["bash", "-c", "grep -a '切' /home/administrator/dkk-projects/auto-switch-ip/daemon.log 2>/dev/null | grep -v '冷却\\|fallback\\|okz\\|标记死节点' | tail -50 | grep -oP '(?<=-> ).*?(?= |$)' | sort | uniq -c | sort -rn | head -10"],
                capture_output=True, text=True, timeout=10
            )
            dist = out.stdout.strip()
            # Check if any single node dominates (>40% of switches)
            lines = [l for l in dist.strip().splitlines() if l.split() and l.split()[0].isdigit()]
            total = sum(int(l.split()[0]) for l in lines)
            dominant = max((int(l.split()[0]) for l in lines), default=0)
            skew = dominant / total if total > 0 else 0
            return skew < 0.4, skew, f"dominant_node_share={skew:.1%}"

        elif name == "response_latency_p95":
            # Quick latency check + trend detection
            out = subprocess.run(
                ["curl", "-m", "10", "-s", "-x", "http://127.0.0.1:7890",
                 "https://www.gstatic.com/generate_204", "-o", "/dev/null",
                 "-w", "%{time_total}"],
                capture_output=True, text=True, timeout=15
            )
            lat = float(out.stdout.strip()) if out.stdout.strip() else 999
            # Trend detection: compare with 5-round moving average trend
            trend_lat = "stable"
            hist = [h["results"].get("response_latency_p95", {}).get("metric", 0)
                    for h in state.get("history", []) if "response_latency_p95" in h.get("results", {})]
            if len(hist) >= 3:
                recent_avg = sum(hist[-3:]) / 3
                if lat > recent_avg * 2 and recent_avg > 0.5:
                    trend_lat = f"rising (recent_avg={recent_avg:.2f}s, now={lat:.2f}s)"
            ok = lat < 3.0 and trend_lat != "rising"
            return ok, lat, f"latency={lat:.2f}s trend={trend_lat}"

        elif name == "upstream_failure_rate":
            # Check recent upstream connect failures in upstream
            out = subprocess.run(
                ["grep", "-a", "upstream connect fail", "/home/administrator/dkk-projects/auto-switch-ip/daemon.log"],
                capture_output=True, text=True, timeout=5
            )
            lines = [l for l in out.stdout.splitlines() if today in l][-20:]  # last 20 today
            return len(lines) < 10, len(lines), f"recent_upstream_fails={len(lines)}"

        elif name == "repeat_failure_nodes":
            """Detect nodes that repeatedly die (>3× in last 10 cycles)."""
            out = subprocess.run(
                ["grep", "-a", "死", "/home/administrator/dkk-projects/auto-switch-ip/daemon.log"],
                capture_output=True, text=True, timeout=5
            )
            nodes = Counter()
            for line in out.stdout.splitlines():
                if today not in line:
                    continue
                m = re.search(r"✗\s+(.+?)\s+死", line)
                if m:
                    nodes[m.group(1)] += 1
            repeat = {n: c for n, c in nodes.items() if c >= 3}
            return len(repeat) == 0, len(repeat), f"repeat_failures={dict(repeat)}"

        elif name == "egress_error_attribution":
            # 复用 daemon 的 per-egress 归因函数（单源真理）。
            sys.path.insert(0, str(PROJECT_DIR))
            import daemon as _daemon
            rates = _daemon.get_per_egress_error_rate(window_min=30)
            bad = {n: r for n, (t, e, r) in rates.items() if r >= 0.5 and t >= 3}
            # 价值闭环：坏出口节点写入 daemon 共享 .bad_nodes.json（格式与 daemon 一致），
            # daemon 的 _export_dead_nodes_locked / cleanup_dead_ttl 已读取该文件并受阀门保护，
            # 无需 daemon 改代码即可触发拉黑 + 自愈。
            if bad:
                _write_bad_nodes(bad)
            return len(bad) == 0, len(bad), f"bad_egress={dict({n: round(r,3) for n,(_,_,r) in bad.items()})}"

    except Exception as e:
        return False, None, f"check_error: {e}"

    return False, None, "unknown_check"


def main():
    state = load_state()
    state["round"] += 1
    round_num = state["round"]

    print(f"=== Self-Evolution Checkpoint Round {round_num} ===")

    # 幂等回填：确保 DEFAULT_CHECKS 全部存在于 state["checks"] 且启用，
    # 避免新增检查因旧 state 缺少条目/未启用而永不运行。
    state.setdefault("enabled_checks", [])
    existing = {c["name"] for c in state.get("checks", [])}
    for c in DEFAULT_CHECKS:
        if c["name"] not in existing:
            state.setdefault("checks", []).append(c)
        if c["name"] not in state["enabled_checks"]:
            state["enabled_checks"].append(c["name"])

    # Run enabled checks
    results = {}
    for check in state["checks"]:
        name = check["name"]
        if name not in state.get("enabled_checks", []):
            continue
        if check.get("interval", 1) > 1 and round_num % check["interval"] != 0:
            continue

        ok, metric, detail = run_check(name, {}, state=state)
        results[name] = {"ok": ok, "metric": metric, "detail": detail}
        status = "✅" if ok else "❌"
        print(f"  {status} {name}: {detail}")

        # Anomaly detection: 当某 check 的 metric 超过其对应 pattern 阈值时记录异常，
        # 并确保该 pattern 推荐的检查始终启用（自进化杠杆：异常→激活检查）。
        for pattern, config in ANOMALY_PATTERNS.items():
            if name != config.get("check"):
                continue
            if metric is not None and metric >= config["threshold"]:
                anomaly = {
                    "round": round_num,
                    "pattern": pattern,
                    "check": name,
                    "metric": metric,
                    "threshold": config["threshold"]
                }
                state["anomalies"].append(anomaly)
                print(f"  ⚠️ ANOMALY: {pattern} (metric={metric}, threshold={config['threshold']})")
                rec_check = config.get("check")
                if rec_check and rec_check not in state.get("enabled_checks", []):
                    state["enabled_checks"].append(rec_check)
                    print(f"  🔄 Auto-activated check: {rec_check}")

    # Record history
    state["history"].append({
        "round": round_num,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": {k: {"ok": v["ok"], "metric": v["metric"]} for k, v in results.items()},
        "enabled": list(state.get("enabled_checks", []))
    })

    # Prune: if a check has been ok for 50 consecutive rounds, consider reducing frequency
    for check in state["checks"]:
        name = check["name"]
        if name in state.get("enabled_checks", []) and check.get("interval", 1) == 1:
            consecutive_ok = 0
            for h in reversed(state["history"]):
                if h["results"].get(name, {}).get("ok"):
                    consecutive_ok += 1
                else:
                    break
            if consecutive_ok >= 50 and check["interval"] < 5:
                check["interval"] = min(check["interval"] * 2, 10)
                print(f"  📉 Reduced {name} interval to every {check['interval']} rounds (stable for {consecutive_ok} rounds)")

    save_state(state)
    print(f"=== Round {round_num} complete. Active checks: {len(state.get('enabled_checks', set()))} ===")


if __name__ == "__main__":
    main()