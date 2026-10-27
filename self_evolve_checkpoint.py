#!/usr/bin/env python3
"""
Self-Evolution Checkpoint for auto-switch-ip monitoring loop.

Meta-goal: The monitoring loop should evolve its own checks.
- Tracks what checks have run, anomalies found, blind spots
- Auto-generates new targeted checks for recurring patterns
- Lightweight, runs inline with existing cron (~100ms overhead)
"""

import copy
import json
import os
import re
import sys
import time
from pathlib import Path
from collections import Counter

from asip import proc
from asip.atomic_io import update_json, write_json
from asip.clash_client import ClashClient

STATE_FILE = Path(__file__).parent / ".monitor_state.json"
PROJECT_DIR = STATE_FILE.parent
_BAD_NODES_FILE = PROJECT_DIR / ".bad_nodes.json"
# 复用 daemon 的 bad-node 过期常量（单源真值），避免魔表。
# 日志路径同样取自 config（ASIP_LOG_FILE 可覆盖），不写死目录。
_LOG_FILE = str(PROJECT_DIR / "daemon.log")
try:
    sys.path.insert(0, str(PROJECT_DIR))
    from config import BAD_NODES_TTL as _BAD_NODES_TTL
    from config import CFG as _CFG
    _LOG_FILE = _CFG["log_file"]
    _MIXED_PROXY = f"http://127.0.0.1:{_CFG['singbox']['mixed_port']}"
except Exception:
    _BAD_NODES_TTL = 300
    _MIXED_PROXY = "http://127.0.0.1:7890"


def _write_bad_nodes(bad: dict) -> None:
    """把归因出的坏出口节点写入 daemon 共享 .bad_nodes.json（原子 read-modify-write）。

    格式与 daemon 完全一致：{node: {"expire": now+TTL, "reason": "egress_error_rate"}}。
    daemon 的 cleanup_dead_ttl 已读取此文件并受 _POOL_MIN_RATIO 阀门保护，故写入即接入拉黑闭环。
    """
    now = int(time.time())
    # read-modify-write 必须串行：两个 cron 轮次同时归因时，后写者会拿旧
    # 快照覆盖前者刚写入的节点，等于漏拉黑。daemon 只读此文件，所以仅需
    # 在写入方加锁。
    lock_path = str(_BAD_NODES_FILE.with_suffix(".json.lock"))

    def _merge(data):
        if not isinstance(data, dict):
            data = {}
        for node in bad:
            data[node] = {"expire": now + _BAD_NODES_TTL,
                          "reason": "egress_error_rate"}
        return data

    update_json(str(_BAD_NODES_FILE), _merge, default={}, lock_path=lock_path)

# 与 daemon.py 阀门一致：允许 70% 节点拉黑（_POOL_MIN_RATIO=0.3）。
# 天花板从 Clash API 实时节点数 derivation，防止订阅变更后 65 这类魔表失效。
BLACKLIST_CEILING_RATIO = 0.7
_FALLBACK_CEILING = 65  # 当 Clash API 不可达时退避（当前 93 节点池 *0.7）


def _clash_pool_size():
    """从 sing-box Clash API 读取可用节点总数；失败退为 _FALLBACK_CEILING。

    失败时向 stderr 记录一次原因：静默回退会让天花板长期停在魔数上而无人
    察觉（stdout 留给 main 的机器可读输出）。
    """
    reason = None
    try:
        sys.path.insert(0, str(PROJECT_DIR))
        import config  # 复用单源真值
        client = ClashClient(
            config.CFG["singbox"]["clash_api"],
            config.get_singbox_secret,
            timeout=5,
        )
        names = client.proxy_names(config.CFG["singbox"]["selector"])
        if names is not None:
            return len(names)
        reason = "Clash API returned no proxy list"
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
    print(
        f"[self-check] _clash_pool_size fallback to {_FALLBACK_CEILING}: {reason}",
        file=sys.stderr,
    )
    return _FALLBACK_CEILING


# 节点切换分布：从日志尾部流式取样，替代旧的四段 shell 管道（D9）。
# 只读最后 2MiB，既避免把 4MB+ 日志整体载入，又能覆盖足够多的近期切换。
_SWITCH_TAIL_BYTES = 2 * 1024 * 1024
_SWITCH_SAMPLE_EVENTS = 50
_SWITCH_EXCLUDE_MARKERS = ("冷却", "fallback", "okz", "标记死节点")
_SWITCH_NODE_RE = re.compile(r"->\s+(\S+)")


def _tail_lines(path, limit_bytes=_SWITCH_TAIL_BYTES):
    """Return the last ``limit_bytes`` of ``path`` decoded into lines.

    Seeking from the end bounds memory on a multi-MB log. The seek can land
    mid-line, so the possibly truncated first line is dropped unless the
    window started at byte 0.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - limit_bytes)
            f.seek(start)
            data = f.read()
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"
    lines = data.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines, None


def _node_switch_distribution(log_file, max_events=_SWITCH_SAMPLE_EVENTS,
                              limit_bytes=_SWITCH_TAIL_BYTES):
    """Return ``(top10, sample_count, error)`` for recent node switches.

    ``top10`` is a ``Counter.most_common(10)`` list. ``sample_count`` is the
    number of node names actually extracted; callers must treat zero as
    "unavailable", not as a healthy zero-skew distribution (D9).
    """
    lines, error = _tail_lines(log_file, limit_bytes)
    if error is not None:
        return [], 0, error
    recent = []
    for line in reversed(lines):
        if "切" not in line:
            continue
        if any(marker in line for marker in _SWITCH_EXCLUDE_MARKERS):
            continue
        recent.append(line)
        if len(recent) >= max_events:
            break
    counts = Counter()
    for line in recent:
        match = _SWITCH_NODE_RE.search(line)
        if match:
            counts[match.group(1)] += 1
    return counts.most_common(10), sum(counts.values()), None

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
        # deepcopy：checks 里的 dict 会被 main 原地修改 interval，浅拷贝会
        # 把改动写回 DEFAULT_CHECKS，污染下一次 load_state。
        "checks": copy.deepcopy(DEFAULT_CHECKS),
        "anomalies": [],
        "history": [],
        "enabled_checks": ["7890_connectivity", "dead_nodes", "bypass_events", "bounded_pool_pressure", "node_switch_distribution", "response_latency_p95", "upstream_failure_rate", "repeat_failure_nodes"],
    }


def save_state(state):
    # Only keep last 100 rounds
    if len(state.get("history", [])) > 100:
        state["history"] = state["history"][-100:]
    write_json(str(STATE_FILE), state, indent=2)


def _grep_log(pattern, timeout=5):
    """grep 日志，返回 (lines, error)。

    grep 的退出码语义：0=有匹配、1=无匹配（正常）、>1=出错。把 1 当失败会
    误报，把 >1/超时当「0 条匹配」则会让检查恒真 —— 这正是 D9 的形态。
    """
    out = proc.run(["grep", "-a", pattern, _LOG_FILE], timeout=timeout)
    if out.timed_out:
        return [], "grep timeout"
    if out.error:
        return [], out.error
    if out.code not in (0, 1):
        return [], f"grep rc={out.code}"
    return out.stdout.splitlines(), None


def run_check(name, context, state=None):
    """Run a specific check and return (status, metric_value, details)."""
    try:
        today = time.strftime("%Y-%m-%d")
        if name == "7890_connectivity":
            out = proc.run(
                ["curl", "-m", "5", "-s", "-o", "/dev/null",
                 "-w", "%{http_code} %{time_total}", _MIXED_PROXY],
                timeout=10,
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
            lines, err = _grep_log("BYPASS\\|bypass")
            if err:
                return False, None, f"unavailable: {err}"
            count = len([l for l in lines if today in l and ("enabled" in l.lower() or "BYPASS" in l.upper())][-10:])
            return count == 0, count, f"bypass_count={count}"

        elif name == "bounded_pool_pressure":
            # Check if _BoundedPool ever logged queue full warning
            lines, err = _grep_log("pool.*满\\|queue.*full")
            if err:
                return False, None, f"unavailable: {err}"
            count = len([l for l in lines if today in l])
            return count == 0, count, f"pool_warnings={count}"

        elif name == "node_switch_distribution":
            # D9: 旧的四段 shell 管道只要任一环失败就返回空 stdout，
            # 调用方据此算出 skew=0 并判定“通过”——检查恒真。现在读取/取样
            # 失败或样本为 0 一律报告 unavailable（ok=False、metric=None），
            # metric=None 不会触发 node_skew_spike 异常，也不会伪装成正常分布。
            top, sampled, error = _node_switch_distribution(_LOG_FILE)
            if error is not None:
                return False, None, f"unavailable: cannot read log ({error})"
            if sampled == 0:
                return False, None, "unavailable: no switch samples in log tail"
            dominant_count = top[0][1]
            skew = dominant_count / sampled
            return skew < 0.4, skew, f"dominant_node_share={skew:.1%}"

        elif name == "response_latency_p95":
            # Quick latency check + trend detection
            out = proc.run(
                ["curl", "-m", "10", "-s", "-x", _MIXED_PROXY,
                 "https://www.gstatic.com/generate_204", "-o", "/dev/null",
                 "-w", "%{time_total}"],
                timeout=15,
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
            lines, err = _grep_log("upstream connect fail")
            if err:
                return False, None, f"unavailable: {err}"
            today_lines = [l for l in lines if today in l][-20:]
            return len(today_lines) < 10, len(today_lines), f"recent_upstream_fails={len(today_lines)}"

        elif name == "repeat_failure_nodes":
            """Detect nodes that repeatedly die (>3× in last 10 cycles)."""
            grep_lines, err = _grep_log("死")
            if err:
                return False, None, f"unavailable: {err}"
            nodes = Counter()
            for line in grep_lines:
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
            state.setdefault("checks", []).append(copy.deepcopy(c))
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