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
import time
import subprocess
import sys
from pathlib import Path
from collections import defaultdict

STATE_FILE = Path(__file__).parent / ".monitor_state.json"

# Default check suite - evolves over time
DEFAULT_CHECKS = [
    {"name": "7890_connectivity", "enabled": true, "interval": 1},
    {"name": "7890_connectivity", "enabled": True, "interval": 1},
    {"name": "dead_nodes", "enabled": True, "interval": 1},
    {"name": "bypass_events", "enabled": True, "interval": 1},
    {"name": "bounded_pool_pressure", "enabled": True, "interval": 5},
    {"name": "node_switch_distribution", "enabled": True, "interval": 5},
    {"name": "response_latency_p95", "enabled": True, "interval": 5},
    {"name": "upstream_failure_rate", "enabled": True, "interval": 5},
    {"name": "repeat_failure_nodes", "enabled": True, "interval": 5},
]

# Anomaly patterns that trigger new check activation
ANOMALY_PATTERNS = {
    "dead_nodes_spike": {"threshold": 5, "check": "node_switch_distribution"},
    "bypass_spike": {"threshold": 2, "check": "upstream_failure_rate"},
    "latency_degradation": {"threshold": 5.0, "check": "response_latency_p95"},
    "pool_pressure": {"threshold": 0.5, "check": "bounded_pool_pressure"},
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
        if name == "7890_connectivity":
            out = subprocess.run(
                ["curl", "-m", "5", "-s", "-o", "/dev/null",
                 "-w", "%{http_code} %{time_total}", "http://127.0.0.1:7890"],
                capture_output=True, text=True, timeout=10
            )
            ok = out.stdout.strip() in ("400", "204")
            return ok, float(out.stdout.split()[1]) if out.stdout else 999, out.stdout.strip()
        elif name == "dead_nodes":
            out = subprocess.run(
                ["grep", "-a", "dead=", "/home/administrator/dkk-projects/auto-switch-ip/daemon.log"],
                capture_output=True, text=True, timeout=5
            )
            # Extract latest dead count
            import re
            matches = re.findall(r"dead=(\d+)", out.stdout)
            dead = int(matches[-1]) if matches else 0
            return dead <= 5, dead, f"dead={dead}"

        elif name == "bypass_events":
            out = subprocess.run(
                ["grep", "-a", "BYPASS\\|bypass", "/home/administrator/dkk-projects/auto-switch-ip/daemon.log"],
                capture_output=True, text=True, timeout=5
            )
            count = len([l for l in out.stdout.splitlines() if "enabled" in l.lower() or "BYPASS" in l.upper()][-10:])
            return count == 0, count, f"bypass_count={count}"

        elif name == "bounded_pool_pressure":
            # Check if _BoundedPool ever logged queue full warning
            out = subprocess.run(
                ["grep", "-a", "pool.*满\\|queue.*full", "/home/administrator/dkk-projects/auto-switch-ip/daemon.log"],
                capture_output=True, text=True, timeout=5
            )
            count = len(out.stdout.strip().splitlines())
            return count == 0, count, f"pool_warnings={count}"

        elif name == "node_switch_distribution":
            out = subprocess.run(
                ["bash", "-c", "grep -a '→ 切' /home/administrator/dkk-projects/auto-switch-ip/daemon.log 2>/dev/null | grep -v '冷却\\|fallback\\|okz' | tail -50 | grep -oP '(?<=-> ).*?(?= |$)' | sort | uniq -c | sort -rn | head -10"],
                capture_output=True, text=True, timeout=10
            )
            dist = out.stdout.strip()
            # Check if any single node dominates (>40% of switches)
            lines = dist.strip().splitlines()
            total = sum(int(l.split()[0]) for l in lines if l.split()[0].isdigit())
            dominant = max((int(l.split()[0]) for l in lines if l.split()[0].isdigit()), default=0)
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
            lines = [l for l in out.stdout.splitlines() if "2026-06-30" in l][-20:]  # last 20 today
            return len(lines) < 10, len(lines), f"recent_upstream_fails={len(lines)}"

        elif name == "repeat_failure_nodes":
            """Detect nodes that repeatedly die (>3× in last 10 cycles)."""
            out = subprocess.run(
                ["grep", "-a", "死", "/home/administrator/dkk-projects/auto-switch-ip/daemon.log"],
                capture_output=True, text=True, timeout=5
            )
            from collections import Counter
            nodes = Counter()
            for line in out.stdout.splitlines():
                if "2026-06-30" not in line:
                    continue
                import re
                m = re.search(r"✗\s+(.+?)\s+死", line)
                if m:
                    nodes[m.group(1)] += 1
            repeat = {n: c for n, c in nodes.items() if c >= 3}
            return len(repeat) == 0, len(repeat), f"repeat_failures={dict(repeat)}"

    except Exception as e:
        return False, None, f"check_error: {e}"

    return False, None, "unknown_check"


def main():
    state = load_state()
    state["round"] += 1
    round_num = state["round"]

    print(f"=== Self-Evolution Checkpoint Round {round_num} ===")

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

        # Anomaly detection
        for pattern, config in ANOMALY_PATTERNS.items():
            if name == config.get("check") or pattern in name:
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

                    # Activate recommended check if not already
                    rec_check = config.get("check")
                    if rec_check and rec_check not in state.get("enabled_checks", []):
                        state["enabled_checks"].add(rec_check)
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