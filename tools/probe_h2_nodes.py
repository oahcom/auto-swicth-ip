#!/usr/bin/env python3
"""H2/MASQUE 承载实测：给出「这个节点能不能当 WARP 出口」，且不碰生产设备。

为什么需要它：`scan_masque_nodes.py` 会改写**生产设备**的 config.json
（`endpoint_v4 → 127.0.0.2`），而 24 台设备已被 19 条 H2 + 5 条 usque-warp
占满 —— 在生产设备上跑等于抢生产线会话。这里只用独立的 `--devices`，
默认 `.warp/usque-probe{1..3}`（`asip` 自己注册的探测设备）。

判据与生产完全同构，不是另造一套：
    usque --http2 → sing-box direct 入站 → override 到 CF MASQUE 端点
    → 节点出站。拿到 `warp=on` 的 trace 才算能承载。

用法：
    python3 tools/probe_h2_nodes.py --tags a:443-any b:20470-any
    python3 tools/probe_h2_nodes.py --new-only          # 只测块表里没 colo 的
    python3 tools/probe_h2_nodes.py --out /tmp/h2scan.json
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

# config.py 在 import 期读 .env；手跑不经过 systemd 的 EnvironmentFile。
_env = PROJECT / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import warp_wg  # noqa: E402
from config import CFG  # noqa: E402

SINGBOX = str(PROJECT / "sing-box")
H2_DST = "162.159.198.2:443"
TRACE = "https://www.cloudflare.com/cdn-cgi/trace"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port: int, deadline: float = 15.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def trace_via(port: int, timeout: int = 15) -> dict:
    """经 usque 的 socks 口取 CF trace。--noproxy 必须是空串。"""
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", str(timeout), "--noproxy", "",
             "--proxy", f"socks5h://127.0.0.1:{port}", TRACE],
            capture_output=True, text=True, timeout=timeout + 6)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)


def h2_singbox(node: dict, inbound_port: int) -> dict:
    """与 deploy/render_h2_lines.py 同构：direct 入站 → override → 节点出站。"""
    ob = warp_wg.prepare_node(node)
    ob["tag"] = "h2node"
    try:
        dns = json.loads((PROJECT / "singbox-config.json").read_text())["dns"]
    except (OSError, ValueError, KeyError):
        dns = {"servers": [
            {"type": "https", "tag": "dns-airport", "server": "custom.juhazf.cn",
             "path": "/inst", "domain_resolver": "dns-default"},
            {"type": "local", "tag": "dns-default"}]}
    return {
        "log": {"level": "error"},
        "inbounds": [{"type": "direct", "tag": "in", "listen": "127.0.0.1",
                      "listen_port": inbound_port}],
        "outbounds": [ob],
        "dns": dns,
        "route": {
            # action 必须显式写 route：缺省被当成 reject（见 render_h2_lines 注释）
            "rules": [{"inbound": ["in"], "action": "route",
                       "override_address": H2_DST.split(":")[0],
                       "override_port": int(H2_DST.split(":")[1]),
                       "outbound": "h2node"}],
            "final": "h2node",
            "default_domain_resolver": {"server": "dns-default"},
        },
    }


def probe(node: dict, device: Path, timeout: float = 25.0) -> dict:
    """起 sing-box + usque，拿一次 warp=on 的 trace。"""
    inbound_port, socks_port = free_port(), free_port()
    td = Path(tempfile.mkdtemp(prefix="h2probe-"))
    sb_cfg, sb_log = td / "sb.json", td / "sb.log"
    sb_cfg.write_text(json.dumps(h2_singbox(node, inbound_port)))

    dev_cfg = json.loads((device / "config.json").read_text())
    dev_cfg["endpoint_h2_v4"] = "127.0.0.1"
    dev_cfg["endpoint_h2_v6"] = ""
    conf = td / "dev.json"
    conf.write_text(json.dumps(dev_cfg))
    conf.chmod(0o600)

    procs = []
    try:
        p = subprocess.Popen([SINGBOX, "run", "-c", str(sb_cfg)],
                             stdout=subprocess.DEVNULL,
                             stderr=open(sb_log, "w"), start_new_session=True)
        procs.append(p)
        if not wait_port(inbound_port, 12):
            return {"ok": False, "stage": "config",
                    "err": (sb_log.read_text(errors="replace").strip().splitlines()
                            or ["sing-box 未起"])[-1][:160]}
        u = subprocess.Popen(
            [str(device / "usque"), "-c", str(conf), "socks", "--http2",
             "-s", "", "-P", str(inbound_port),
             "-b", "127.0.0.1", "-p", str(socks_port)],
            cwd=str(device), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        procs.append(u)
        if not wait_port(socks_port, 12):
            return {"ok": False, "stage": "port"}
        t0 = time.time()
        while time.time() - t0 < timeout:
            t = trace_via(socks_port, 8)
            if t.get("warp") == "on":
                return {"ok": True, "stage": "ok", "colo": t.get("colo"),
                        "loc": t.get("loc"), "ip": t.get("ip"),
                        "ms": int((time.time() - t0) * 1000)}
            time.sleep(1.0)
        t = trace_via(socks_port, 8)
        return {"ok": False, "stage": "handshake",
                "err": f"warp={t.get('warp', '?')}"}
    finally:
        for p in reversed(procs):
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except OSError:
                try:
                    p.kill()
                except OSError:
                    pass


def load_nodes(path: str | None = None) -> dict:
    cfg = json.loads(Path(path or PROJECT / "singbox-config.json").read_text())
    skip = {"selector", "urltest", "direct", "block", "dns", "socks"}
    return {o["tag"]: o for o in cfg.get("outbounds", [])
            if o.get("type") not in skip and o.get("server") and o.get("server_port")}


def main() -> int:
    ap = argparse.ArgumentParser(description="H2/MASQUE 承载实测（独立探测设备）")
    ap.add_argument("--tags", nargs="*", help="要测的节点 tag")
    ap.add_argument("--new-only", action="store_true",
                    help="只测 block-map 里还没有 colo 的节点")
    ap.add_argument("--devices", nargs="*",
                    default=[str(PROJECT / f".warp/usque-probe{i}") for i in (1, 2, 3)],
                    help="探测设备目录（不得与生产 usque-* 单元重合）")
    ap.add_argument("--out", default="/tmp/asip-h2scan.json")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--config", default=None,
                    help="节点取自哪份配置（默认生产 singbox-config.json）")
    ap.add_argument("--state", default=str(PROJECT / ".warp/h2_probe_state.json"),
                    help="测过的结论台账；--new-only 据此跳过已测节点")
    ap.add_argument("--retest", action="store_true",
                    help="忽略台账，强制重测（节点换策略或换中继后用）")
    args = ap.parse_args()

    devs = [Path(d) for d in args.devices if (Path(d) / "config.json").exists()]
    if not devs:
        raise SystemExit("✗ 没有可用的探测设备（先用 usque register 建一台）")

    nodes = load_nodes(args.config)
    # 已测台账：只跳过**测过且失败**的节点。成功的已经入块了，`new-only`
    # 的 colo 判据本来就会跳过它们；不记失败的话，82 个不能承载的节点每天
    # 都会被重测一遍（串行 + 每台 20s，等于空转半小时）。
    state = {}
    if not args.retest:
        try:
            state = json.loads(Path(args.state).read_text())
        except (OSError, ValueError):
            state = {}
    state.setdefault("nodes", {})

    if args.new_only:
        import warp_blocks
        m = warp_blocks.load_map()
        # **只跳过「测过且失败」的**（同 warp_diag）：测成功但还没合并的节点
        # 不该被当成已处理，否则它们的 colo 只留在 --out 里，永远进不了块。
        tags = [t for t in nodes
                if not (m.get(t) or {}).get("colo")
                and not ((state["nodes"].get(t) or {}).get("tested")
                         and not (state["nodes"].get(t) or {}).get("ok"))]
    else:
        tags = args.tags or list(nodes)
    missing = [t for t in tags if t not in nodes]
    if missing:
        raise SystemExit(f"✗ 不在当前配置里: {missing[:5]}")

    print(f"待测 {len(tags)} 个节点，设备 {[d.name for d in devs]}（串行复用）",
          flush=True)
    rows = []
    for i, tag in enumerate(tags):
        dev = devs[i % len(devs)]
        best = {"ok": False, "stage": "handshake"}
        for _ in range(max(1, args.rounds)):
            rec = probe(nodes[tag], dev)
            if rec["ok"]:
                best = rec
                break
            best = rec
        row = {"tag": tag, "device": dev.name, **best}
        rows.append(row)
        # 记录「测过」，成功失败都记：失败是结论，不是没跑过。
        state["nodes"][tag] = {"tested": True, "ok": bool(best["ok"]),
                               "colo": best.get("colo"),
                               "stage": best.get("stage"),
                               "ts": int(time.time())}
        mark = f"✓ warp=on colo={best.get('colo')} {best.get('ms')}ms" if best["ok"] \
            else f"✗ {best['stage']} {best.get('err', '')}"
        print(f"[{i+1}/{len(tags)}] {tag[:44]:44s} {mark}", flush=True)

    Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    if state["nodes"]:
        from asip.atomic_io import write_json
        write_json(args.state, state, mode=0o600, indent=1)
    ok = sum(1 for r in rows if r["ok"])
    print(f"\n可承载 H2 {ok}/{len(rows)} → {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
