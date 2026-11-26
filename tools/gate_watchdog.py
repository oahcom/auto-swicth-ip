#!/usr/bin/env python3
"""闸口自检：探测各 slot 的 egress，失败后按【故障域】分流。

影子模式（默认 SHADOW=True）：
    只读探测 + 分类 + 统计，不执行任何 PUT selector。任何自动换节点都
    必须先经方案批准并显式传 --live 才可能发生。

两轮失败后的分流（先分类、后决策）：
    ACCOUNT / MODEL / REQUEST / CONTEXT_REJECTED -> 不换节点
    UNKNOWN                                    -> 继续观测
    TRANSPORT                                  -> 检查代理链路，不换节点
    EXIT_IP + 独立证据                         -> 独立确认后才允许退役
                                                  （影子下仅记 would_rotate，不 PUT）

设计纪律：
    1. egress_ok 只回答「CF trace 通不通」，它不等于「节点坏了」，更不等于
       「账号/模型可用」。所以探测失败只产生候选事件，分类交给 failure_domain。
    2. 429 / 配额 / 上下文拒绝属于请求域，绝不进入出口退役路径。
    3. EXIT_IP 必须带独立证据（evidence_id / evidence_confirmed /
       independent_confirm）才允许进入退役路径；无证据一律继续观测。

用法：
    python3 tools/gate_watchdog.py --once            # 一轮（默认影子）
    python3 tools/gate_watchdog.py --loop 60         # 常驻（默认影子）
    python3 tools/gate_watchdog.py --shadow-report   # 只读影子统计
    python3 tools/gate_watchdog.py --once --live     # 转正式（需方案批准；非默认）
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from asip.failure_domain import FailureDomain, ProbeClass  # noqa: E402

SECRET = "BWw/9meYoOsWMaVfkZuUoGPem3qcaszRUhlMFbCSnKM="
STATE = PROJECT / ".warp" / "gate_watch.json"
LOG = PROJECT / ".warp" / "gate_watch.log"
SHADOW_STATE = PROJECT / ".warp" / "gate_watch_shadow.json"
EVIDENCE = PROJECT / ".warp" / "gate_watch_evidence.json"
TRACE = "https://www.cloudflare.com/cdn-cgi/trace"

# 默认影子：任何自动 PUT 都被禁用。转正式必须显式 --live 且由人批准。
SHADOW = True

# 禁止换节点的域：请求级 / 账号级 / 模型级 / 上下文拒绝。
NO_ROTATE_DOMAINS = frozenset({
    FailureDomain.ACCOUNT,
    FailureDomain.MODEL,
    FailureDomain.REQUEST,
    ProbeClass.CONTEXT_REJECTED,
})


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def targets() -> list[tuple[int, int, str]]:
    """[(socks_port, api_port, label)]：主代理 7890/9090 + slot2..8。"""
    out = [(7890, 9090, "main")]
    for n in (2, 3, 4, 5, 6, 7, 8):
        out.append((7890 + n, 9090 + n, f"slot{n}"))
    return out


def egress_ok(port: int, timeout: int = 8) -> bool:
    """只回答 CF trace 能否从该 socks 出口取回 colo 字段。"""
    r = subprocess.run(
        ["curl", "-s", "--max-time", str(timeout), "--socks5-hostname",
         f"127.0.0.1:{port}", TRACE],
        capture_output=True, text=True)
    return r.returncode == 0 and "colo=" in (r.stdout or "")


def api(port: int, path: str, body: dict | None = None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method="PUT" if data else "GET",
        headers={"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=6) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def alive_nodes() -> set[str]:
    """实测连通过的节点（node_delay.json 只记录 TCP 握手成功的）。"""
    try:
        return set(json.loads((PROJECT / ".warp" / "h2" / "node_delay.json").read_text())["delays"])
    except (OSError, ValueError, KeyError):
        return set()


# ---------------- 故障域：先分类，后决策 ----------------

_STRING_MAP = {
    "": FailureDomain.UNKNOWN,
    "account": FailureDomain.ACCOUNT,
    "account_quota": FailureDomain.ACCOUNT,
    "model": FailureDomain.MODEL,
    "model_quota": FailureDomain.MODEL,
    "request": FailureDomain.REQUEST,
    "rate_limited": FailureDomain.REQUEST,
    "context_rejected": ProbeClass.CONTEXT_REJECTED,
    "transport": FailureDomain.TRANSPORT,
    "transport_error": FailureDomain.TRANSPORT,
    "exit_ip": FailureDomain.EXIT_IP,
    "ip_blocked": FailureDomain.EXIT_IP,
    "unknown": FailureDomain.UNKNOWN,
}


def normalize_domain(value) -> object:
    """把枚举或字符串统一成 FailureDomain / ProbeClass；无法识别一律 UNKNOWN。"""
    if isinstance(value, (FailureDomain, ProbeClass)):
        return value
    key = str(value or "").strip().lower()
    return _STRING_MAP.get(key, FailureDomain.UNKNOWN)


def domain_label(domain) -> str:
    d = normalize_domain(domain)
    return getattr(d, "value", str(d))


def classify_failure(evidence) -> object:
    """把一次失败的观测证据映射到故障域。没有证据一律 UNKNOWN。"""
    if not isinstance(evidence, dict) or not evidence:
        return FailureDomain.UNKNOWN
    if "domain" in evidence:
        return normalize_domain(evidence.get("domain"))
    if "classification" in evidence:
        return normalize_domain(evidence.get("classification"))
    reason = str(evidence.get("reason") or "").strip().lower()
    if not reason:
        return FailureDomain.UNKNOWN
    if "403" in reason or "abuse" in reason or "context" in reason:
        return ProbeClass.CONTEXT_REJECTED
    if "transport" in reason or "timeout" in reason or "stall" in reason:
        return FailureDomain.TRANSPORT
    # model 优先：'model quota' 属模型配额，不能先被 quota 命中成账号配额。
    if "model" in reason:
        return FailureDomain.MODEL
    if ("quota" in reason or "account" in reason or "free usage" in reason
            or "daily limit" in reason):
        return FailureDomain.ACCOUNT
    if "429" in reason or "rate" in reason:
        return FailureDomain.REQUEST
    return FailureDomain.UNKNOWN


def evidence_confirmed(evidence) -> bool:
    """出口退役需要独立证据；仅有失败次数不算。"""
    if not isinstance(evidence, dict):
        return False
    return bool(evidence.get("evidence_id")
                or evidence.get("evidence_confirmed")
                or evidence.get("independent_confirm"))


def decide(domain, evidence=None, shadow=True) -> tuple[str, str]:
    """返回 (action, reason)。action ∈ hold/observe/check_transport/would_rotate/rotate。"""
    d = normalize_domain(domain)
    if d in NO_ROTATE_DOMAINS:
        return "hold", f"{domain_label(d)}: 请求/账号/模型/上下文故障，不换节点"
    if d is FailureDomain.UNKNOWN:
        return "observe", "UNKNOWN: 缺少分类证据，继续观测"
    if d is FailureDomain.TRANSPORT:
        return "check_transport", "TRANSPORT: 先检查代理链路，不换节点"
    if d is FailureDomain.EXIT_IP:
        if not evidence_confirmed(evidence):
            return "observe", "EXIT_IP: 缺独立确认证据，继续观测"
        if shadow:
            return "would_rotate", "EXIT_IP+独立证据: 影子模式，仅记录不执行 PUT"
        return "rotate", "EXIT_IP+独立证据: 允许退役换节点"
    return "observe", "未识别故障域，继续观测"


def load_evidence(port: int):
    """影子阶段从独立证据文件读取；缺文件=无证据=UNKNOWN。"""
    try:
        data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get(str(port)) if isinstance(data, dict) else None


def record_shadow(port: int, label: str, domain, action: str, reason: str) -> None:
    try:
        data = json.loads(SHADOW_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    counts = data.setdefault("counts", {})
    key = domain_label(domain)
    counts[key] = int(counts.get(key, 0)) + 1
    if action == "would_rotate":
        data["would_rotate"] = int(data.get("would_rotate", 0)) + 1
    # 影子模式下实际 PUT 恒为 0。
    data["actual_rotate"] = 0
    samples = data.setdefault("samples", [])
    if not isinstance(samples, list):
        samples = data["samples"] = []
    if len(samples) < 200:
        samples.append({"ts": time.time(), "port": port, "label": label,
                        "domain": key, "action": action, "reason": reason})
    SHADOW_STATE.parent.mkdir(parents=True, exist_ok=True)
    SHADOW_STATE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def shadow_report(path=None) -> dict:
    """只读统计入口，不写任何文件、不发任何请求。"""
    p = Path(path) if path else SHADOW_STATE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    counts = data.get("counts") or {}
    samples = data.get("samples") or []
    if not isinstance(samples, list):
        samples = []
    return {
        "shadow_state": str(p),
        "counts": counts if isinstance(counts, dict) else {},
        "would_rotate": int(data.get("would_rotate") or 0),
        "actual_rotate": int(data.get("actual_rotate") or 0),
        "samples": samples[-20:],
        "note": "影子模式：actual_rotate 恒为 0；would_rotate 只是候选决策，未执行 PUT。",
    }


def rotate(api_port: int, socks: int, alive: set[str], last_good: str | None) -> str | None:
    """把 proxy selector 推到下一个换上去真能出网节点（仅 --live 调用）。"""
    info = api(api_port, "/proxies/proxy")
    cur, cands = info.get("now"), list(info.get("all") or [])
    if not cands:
        return None
    order: list[str] = []
    if last_good and last_good in cands and last_good != cur:
        order.append(last_good)
    start = cands.index(cur) + 1 if cur in cands else 0
    for i in range(len(cands)):
        name = cands[(start + i) % len(cands)]
        if name not in order:
            order.append(name)
    tried = 0
    for name in order:
        if alive and name not in alive:
            continue
        if tried >= 6:
            break
        tried += 1
        try:
            api(api_port, "/proxies/proxy", {"name": name})
        except Exception:                     # noqa: BLE001
            continue
        if egress_ok(socks, timeout=5):
            return name
    return None


def one_round(shadow: bool = SHADOW) -> int:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    alive = alive_nodes()
    bad = 0
    for socks, apip, label in targets():
        key = str(socks)
        if egress_ok(socks):
            state[key] = 0
            continue
        state[key] = int(state.get(key, 0)) + 1
        bad += 1
        if state[key] < 2:
            log(f"{label} {socks} 不通（第 {state[key]} 轮，先观察）")
            continue
        evidence = load_evidence(socks)
        domain = classify_failure(evidence)
        action, reason = decide(domain, evidence, shadow=shadow)
        if action == "rotate" and not shadow:
            new = rotate(apip, socks, alive, (state.get("good") or {}).get(key))
            if not new:
                log(f"{label} {socks} 不通且找不到可换的活节点")
                continue
            state[key] = 0
            ok = egress_ok(socks)
            if ok:
                state.setdefault("good", {})[key] = new
            log(f"{label} {socks} EXIT_IP 确认 → 换节点 {new} {'✓ 恢复' if ok else '✗ 仍不通'}")
        else:
            record_shadow(socks, label, domain, action, reason)
            log(f"{label} {socks} 第 {state[key]} 轮失败 → {action}: {reason}（影子={shadow}）")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return bad


def main() -> int:
    if "--shadow-report" in sys.argv:
        print(json.dumps(shadow_report(), ensure_ascii=False, indent=1))
        return 0
    shadow = "--live" not in sys.argv
    if "--loop" in sys.argv:
        gap = int(sys.argv[sys.argv.index("--loop") + 1])
        while True:
            one_round(shadow=shadow)
            time.sleep(gap)
    bad = one_round(shadow=shadow)
    log(f"本轮结束：{bad} 个出口不通（影子={shadow}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
