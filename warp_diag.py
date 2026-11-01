#!/usr/bin/env python3
"""单节点 WARP 隧道诊断：回答「这个代理节点能不能承载 WARP」，不回答「为什么不能」。

这里曾经有一份 200 行的临时脚本（`/tmp/opencode/udp_probe.py`）自己拼 sing-box
配置，拼出来的和 `warp_wg.build_line_config` 不是同一份：DNS 换成 DoH、节点 tag
改成 `node`、`domain_resolver` 缺失。结果 44 个节点全部报「WARP 握手失败」，
而其中 15 个的 UDP:53 是通的 —— 探针在测自己，不在测节点。所以本模块只做一件
事：把**生产同一份** `build_line_config` 拿来跑一遍，并如实记录失败发生在哪一层。

四类失败必须分开，它们对应完全不同的处置：
  config     配置/进程没起来（缺 resolver、端口占用、版本不支持）—— 与节点无关
  port       端口没监听（进程活着但没绑上，通常是并发抢占）
  handshake  端口通了、隧道起不来（节点不转发 2408，或出口被 CF 拒）—— 真结论
  ok         拿到 warp=on 的 trace

探测**不做并发**：同一台机器上每个实例都要绑端口、走同一条运营商出口，
并发只会让失败原因互相污染。44 个节点串行约 20 分钟，比读一份假的结论便宜。

用法::

    python3 warp_diag.py --job /tmp/job.json --out /tmp/diag.jsonl
    python3 warp_diag.py --job /tmp/job.json --out /tmp/diag.jsonl --only tag1,tag2
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import warp_wg
from config import PROJECT_DIR

PROJECT = Path(PROJECT_DIR)
SINGBOX = str(PROJECT / "sing-box")
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

# 探测必须用**没在跑**的设备身份。第一版拿 line1..4 的 profile 去探，那 4 份
# 是 WARP-WG-1..4 的线上设备：同一个 WireGuard 身份不能有两个会话，服务端只
# 认最后一次握手 —— 谁的包晚到谁掉线，日志里就是
# `read packet: use of closed network connection`。于是探针把线上线踢掉、
# 线上线把探针踢掉，44 个节点全判死里有一份是这么来的。
#
# 槽位不写死：扩容后 14..16 也会变成生产线，写死等于把雷埋到下一轮。
WLINES = "wglines"


# ---------------------------------------------------------------- 基础工具

def free_port() -> int:
    """让内核挑一个空闲端口。

    不用「基准端口 + 偏移」：那套算法在上一轮探测里和残留进程撞端口，
    44 个节点因此全部报「端口没起来」，看起来像节点故障。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def registered_slots() -> list[int]:
    """已注册（有 profile.conf）的设备槽位。"""
    base = PROJECT / ".warp" / WLINES
    out = []
    for d in base.glob("line*"):
        tail = d.name[4:]
        if tail.isdigit() and (d / "profile.conf").exists():
            out.append(int(tail))
    return sorted(out)


def active_slots() -> set[int]:
    """正在跑的生产线路序号：systemd 里 active 的 `warp-wg-N` + 线路台账。

    两处都看：台账可能落后于单元（刚 register 还没进 state），单元可能是
    手工起的（不在台账里）。探测拿其中任何一个当自己的设备都会打架。
    """
    used: set[int] = set()
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--plain", "--no-legend",
             "--state=active", "warp-wg-*.service"],
            capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            name = line.split()[0] if line.split() else ""
            tail = name.rsplit("-", 1)[-1].split(".")[0]
            if tail.isdigit():
                used.add(int(tail))
    except (subprocess.SubprocessError, OSError, IndexError):
        pass
    try:
        for tag in warp_wg.line_nodes():
            tail = tag.rsplit("-", 1)[-1]
            if tail.isdigit():
                used.add(int(tail))
    except (OSError, ValueError):
        pass
    return used


def spare_slots() -> list[int]:
    """已注册但没在跑的设备；一个都没有时直接停手。

    拿线上设备探测不只是「结果不准」——它会真的把在用的线踢下线。宁可
    报错让人补注册设备，也不要在诊断工具里埋一颗能打断生产的雷。
    """
    used = active_slots()
    free = [s for s in registered_slots() if s not in used]
    if not free:
        raise SystemExit(
            f"✗ 没有空闲的 WARP 设备可用来探测（已注册 {registered_slots()}，"
            f"其中 active {sorted(used)}）。先 register 一条新线再探 —— "
            f"用线上设备的身份探测会抢掉那条线的会话。")
    return free


# 节点出站的加工也走 `warp_wg.prepare_node` —— 探针如果自己写一份
# `domain_resolver` 规则，就会和生产配置分叉，而分叉的探针得出的结论
# 不适合用来判节点。这里只保留函数名，实现完全委托。
def normalize_node(node: dict) -> dict:
    return warp_wg.prepare_node(node)


def _profile(slot: int) -> dict:
    return warp_wg.read_profile(warp_wg.line_dir(slot) / "profile.conf")


def _trace(port: int, timeout: float) -> dict | None:
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", str(int(timeout)),
             "--proxy", f"socks5h://127.0.0.1:{port}", TRACE_URL],
            capture_output=True, text=True, timeout=timeout + 4)
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode:
        return None
    return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)


def _start(cfg_path: Path, log_path: Path) -> subprocess.Popen:
    log = open(log_path, "wb")
    return subprocess.Popen([SINGBOX, "run", "-c", str(cfg_path)],
                            stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)


def _stop(p: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except OSError:
        try:
            p.kill()
        except OSError:
            pass
    try:
        p.wait(timeout=5)
    except subprocess.SubprocessError:
        pass


def _wait_port(port: int, proc: subprocess.Popen, timeout: float) -> tuple[str, float]:
    """等端口监听。返回 (状态, 用时)：`open` / `exited` / `timeout`。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return "exited", time.time() - t0
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return "open", time.time() - t0
        except OSError:
            time.sleep(0.2)
    return "timeout", time.time() - t0


def _log_tail(path: Path, n: int = 6) -> str:
    try:
        lines = path.read_text(errors="replace").strip().splitlines()
    except OSError:
        return ""
    return " | ".join(lines[-n:])


# ---------------------------------------------------------------- 单轮 / 单节点

def run_round(node: dict, family: str, slot: int,
              wait_ready: float = 12.0, wait_warp: float = 20.0) -> dict:
    """起一条隧道，等 `warp=on` 的 trace。一轮 = 一份 WARP 设备一次会话。"""
    profile = _profile(slot)
    port = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="warpdiag-"))
    cfg_path, log_path = tmp / "cfg.json", tmp / "sb.log"
    cfg = warp_wg.build_line_config(profile, port, normalize_node(node),
                                    warp_wg._wg()["peer_v4"], family)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False))
    out = {"ok": False, "stage": "config", "port": port}
    t0 = time.time()
    proc = _start(cfg_path, log_path)
    try:
        state, _ = _wait_port(port, proc, wait_ready)
        if state != "open":
            out["stage"] = "config" if state == "exited" else "port"
            out["err"] = _log_tail(log_path)
            return out
        deadline = time.time() + wait_warp
        trace = None
        while time.time() < deadline:
            trace = _trace(port, 5)
            if trace and trace.get("warp") == "on":
                break
            trace = None
            time.sleep(0.3)
        if not trace:
            out["stage"] = "handshake"
            out["err"] = _log_tail(log_path)
            return out
        out.update(ok=True, stage="ok", ip=trace.get("ip"),
                   colo=trace.get("colo"), loc=trace.get("loc"),
                   ms=int((time.time() - t0) * 1000))
    finally:
        _stop(proc)
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def diagnose(item: dict, rounds: int, family: str = "v6",
             stagger: float = 0.4, slots: list[int] | None = None) -> dict:
    """多轮判定。单轮失败不足以定论：同一节点时通时不通（实测首轮 58/190，
    补测后 86/190），这是 UDP 转发的随机性，不是我们的判据不稳。

    每轮换一个**备用**设备槽位：同一份 profile 连测多轮会连续复用同一个
    WireGuard 会话，第一轮失败后服务端还记着旧会话，第二轮容易延续同一个结果。
    """
    slots = slots or spare_slots()
    rec = {"tag": item["tag"], "egress_ip": item.get("egress_ip"),
           "country": item.get("country"), "family": family, "rounds": []}
    for r in range(rounds):
        res = run_round(item["node"], family, slots[r % len(slots)])
        rec["rounds"].append(res)
        if res["ok"]:
            break  # 一次建成即证明能承载，不必继续烧设备会话
        time.sleep(stagger)
    rec["ok"] = any(x["ok"] for x in rec["rounds"])
    rec["stage"] = ("ok" if rec["ok"]
                    else max((x["stage"] for x in rec["rounds"]),
                             key=lambda s: ("config", "port", "handshake").index(s)))
    return rec


# ---------------------------------------------------------------- CLI

def _load_job(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise SystemExit("--job 必须是 [{tag, node, egress_ip}, …] 的 JSON 数组")
    return data


def summarize(path: str) -> None:
    """把 JSONL 汇总成一张表：每个终态各多少、哪些能建隧道、落在哪个 colo。

    分成 **config/port** 与 **handshake** 两组报数：前者是工具自己的故障，
    出现就得先修工具再看结论；把两者混成一个「失败」数字，就是上一轮误判
    44 个节点的直接原因。
    """
    rows = []
    for line in Path(path).read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    by_stage: dict[str, list[dict]] = {}
    for r in rows:
        by_stage.setdefault(r.get("stage"), []).append(r)
    total = len(rows)
    print(f"共 {total} 个节点")
    for stage in ("ok", "handshake", "config", "port"):
        if stage in by_stage:
            print(f"  {stage:9s} {len(by_stage[stage]):3d}")
    bad_tool = len(by_stage.get("config", [])) + len(by_stage.get("port", []))
    if bad_tool:
        print(f"  ⚠ 其中 {bad_tool} 个是探针自身故障（config/port），不计入节点结论")
    oks = by_stage.get("ok", [])
    if oks:
        colos: dict[str, int] = {}
        for r in oks:
            colo = next((x.get("colo") for x in r["rounds"] if x.get("ok")), "?")
            colos[colo] = colos.get(colo, 0) + 1
        print(f"  可承载 WARP：{len(oks)} 个  colo={colos}")
        for r in oks:
            hit = next(x for x in r["rounds"] if x.get("ok"))
            print(f"    {r['tag'][:46]:46s} {hit.get('colo')} {hit.get('ip')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="单节点 WARP 承载能力诊断")
    ap.add_argument("--job", help="{tag, node, egress_ip} 列表 JSON")
    ap.add_argument("--out", help="结果 JSONL（逐条追加，可中断续跑）")
    ap.add_argument("--rounds", type=int, default=3, help="每节点最多几轮（默认 3）")
    ap.add_argument("--family", default="v6", choices=("v6", "v4"))
    ap.add_argument("--only", help="逗号分隔的 tag 子集")
    ap.add_argument("--skip-done", action="store_true",
                    help="跳过 --out 里已有的 tag（中断后续跑）")
    ap.add_argument("--summarize", help="只汇总已有的 JSONL，不跑探测")
    ap.add_argument("--slots", help="逗号分隔的备用设备槽位（默认 14,15,16）")
    args = ap.parse_args()

    if args.summarize:
        summarize(args.summarize)
        return
    if not args.job or not args.out:
        raise SystemExit("需要 --job 与 --out（或 --summarize）")
    slots = ([int(s) for s in args.slots.split(",") if s.strip()]
             if args.slots else spare_slots())
    print(f"设备槽位 {slots}（active 线路 {sorted(active_slots())} 已排除）",
          file=sys.stderr)

    todo = _load_job(args.job)
    if args.only:
        want = {t.strip() for t in args.only.split(",") if t.strip()}
        todo = [i for i in todo if i["tag"] in want]
    done: set[str] = set()
    if args.skip_done and Path(args.out).exists():
        for line in Path(args.out).read_text().splitlines():
            try:
                done.add(json.loads(line)["tag"])
            except (ValueError, KeyError):
                continue
        todo = [i for i in todo if i["tag"] not in done]
        print(f"跳过已完成 {len(done)} 个，剩 {len(todo)}", file=sys.stderr)

    with open(args.out, "a", encoding="utf-8") as fh:
        for k, item in enumerate(todo, 1):
            rec = diagnose(item, args.rounds, args.family, slots=slots)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            detail = [(x["stage"], x.get("colo") or x.get("ip")) for x in rec["rounds"]]
            print(f"[{k}/{len(todo)}] {rec['tag'][:44]:44s} {rec['stage']:9s} {detail}",
                  flush=True)


if __name__ == "__main__":
    main()
