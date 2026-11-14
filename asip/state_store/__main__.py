"""管理面三件套的可执行入口（CLI）。**只编排，不重写判定。**

三个动作都要求「判定在 mgmt、执行在 daemon 写口」，所以本 CLI 的职责是：
把 mgmt 生成的**计划**交给 daemon 的 UDS 管理面落地，再把结果打印出来。

  asip-state unban  <ip> [--reason ...] [--actor ...]   # 手动解封（走 daemon 写口）
  asip-state snapshot [--json]                          # 状态快照（复用 /health）
  asip-state restart --dry-run <target>[:kind] ...      # 副作用分级（不执行）

WHY 不直接写 JSON：`asip-state unban` 若自己改文件，就绕过了 daemon 的
`_WARP_IP_LOCK`、空表/缩水护栏与内存态一致性 —— 那正是「手改台账」这个
破口的另一个名字。CLI 只发请求，写只发生在 daemon 线程里。

退出码：0 成功 / 1 拒绝（计划给出「不该做」的理由）/ 2 环境错误。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from . import mgmt
from . import schema
from .store import Store


def _api(cfg: dict) -> tuple[str, str]:
    api = (cfg or {}).get("api") or {}
    sock = api.get("socket_path") or os.path.join("run", "rotate.sock")
    token = ""
    token_file = api.get("token_file") or os.path.join("run", "api-token")
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        pass
    return sock, token


def _get(cfg: dict, path: str, timeout: float = 5.0) -> dict:
    sock, token = _api(cfg)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock)
        req = ("GET %s HTTP/1.1\r\nHost: asip\r\nX-Asip-Token: %s\r\n"
               "Connection: close\r\n\r\n" % (path, token))
        s.sendall(req.encode())
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    finally:
        s.close()
    raw = b"".join(chunks)
    body = raw.split(b"\r\n\r\n", 1)[-1]
    try:
        return json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        return {"ok": False, "reason": "daemon 返回非 JSON", "raw": body[:200]}


def _post(cfg: dict, path: str, payload: dict, timeout: float = 10.0) -> dict:
    sock, token = _api(cfg)
    body = json.dumps(payload, ensure_ascii=False).encode()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock)
        head = ("POST %s HTTP/1.1\r\nHost: asip\r\nX-Asip-Token: %s\r\n"
                "Content-Type: application/json\r\nContent-Length: %d\r\n"
                "Connection: close\r\n\r\n" % (path, token, len(body)))
        s.sendall(head.encode() + body)
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    finally:
        s.close()
    raw = b"".join(chunks)
    data = raw.split(b"\r\n\r\n", 1)[-1]
    try:
        return json.loads(data.decode("utf-8") or "{}")
    except ValueError:
        return {"ok": False, "reason": "daemon 返回非 JSON"}


def cmd_unban(args, cfg: dict) -> int:
    """判定与落地都在 daemon 里（内存态是唯一权威）；CLI 只发请求 + 打印。

    WHY 不在本地先算：影子阶段库是**观察对象**，不是权威 —— 拿库里的行去算
    「这个 IP 该不该解封」，会用一份可能落后的副本做判定。判定必须对内存态
    做（`_WARP_IP_USED`），而那份内存只在 daemon 进程里。
    """
    out = _post(cfg, "/state/unban", {"ip": args.ip, "reason": args.reason,
                                      "actor": args.actor,
                                      "dry_run": bool(args.dry_run)})
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out.get("ok") else 1


def cmd_snapshot(args, cfg: dict) -> int:
    out = _get(cfg, "/health")
    sc = cfg["state_store"]
    if args.with_store:
        try:
            store = Store(sc["db_path"])
            try:
                out = {**out, "store": store.stats(),
                       "shadow": mgmt.shadow_summary(
                           store, window_sec=args.window)}
            finally:
                store.close()
        except Exception as exc:
            out = {**out, "store_error": str(exc)}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out.get("status") in ("ok", "degraded", "unknown") else 1


def cmd_restart(args, cfg: dict) -> int:
    targets = []
    for t in args.targets:
        if ":" in t:
            name, kind = t.rsplit(":", 1)
            targets.append((name, kind))
        else:
            targets.append(t)
    budget = None
    warp = cfg.get("warp") or {}
    if warp.get("restart_budget"):
        budget = warp["restart_budget"]
    out = mgmt.restart_dry_run(targets, budget=budget)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if out["requires_explicit_approval"]:
        print("⚠ 本次含换地址动作（烧额度），需要显式批准", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="asip-state",
                                description="auto-switch-ip 状态管理面")
    ap.add_argument("--config-dir", default=None,
                    help="仓库根（默认取本文件上两级）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("unban", help="手动解封出口 IP（经 daemon 写口）")
    p.add_argument("ip")
    p.add_argument("--reason", default="")
    p.add_argument("--actor", default=os.environ.get("USER", ""))
    p.add_argument("--dry-run", action="store_true", help="只出计划不改台账")
    p.set_defaults(fn=cmd_unban)

    p = sub.add_parser("snapshot", help="状态快照（/health + 库统计 + 影子摘要）")
    p.add_argument("--with-store", action="store_true")
    p.add_argument("--window", type=float, default=3600.0)
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("restart", help="restart --dry-run（副作用分级）")
    p.add_argument("targets", nargs="+")
    p.add_argument("--dry-run", action="store_true",
                   help="只出计划（本阶段唯一允许的形态：换地址是业务动作，"
                        "执行权在 W3 仲裁点）")
    p.set_defaults(fn=cmd_restart)

    args = ap.parse_args(argv)

    root = args.config_dir or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from config import CFG
    return args.fn(args, CFG)


if __name__ == "__main__":
    raise SystemExit(main())