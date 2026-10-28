#!/usr/bin/env python3
"""asip vercel —— Vercel 中继线路的运维入口。

四条子命令对应四件事，彼此可以单独跑：

  status   看台账 + 9router 里真实的池/策略（两边对不上就是同步漏了）
  probe    经每条 relay 打一次 ipify，记下出口 IP 与延迟
  sync     把台账写进 9router 的 proxyPools（幂等，按 URL 认领）
  on/off   把 opencode 的出口切到中继池 / 切回原来那个池
  rotate   一条 relay 被烧了之后的对账：停用烧掉的、启用健康的、保证策略

默认全部是 dry-run，要真写库加 `--apply`。这个默认不是保守，是因为它改的是
生产 9router 的出口 —— 一次误写，所有 opencode 请求都会走一个不存在的池。

退出码：0 成功；1 参数/环境错；2 有 relay 不可用（status/probe 用）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402  缺键时它自己 SystemExit，CLI 就该在这里失败
from asip import vercel_relay as vr  # noqa: E402


def _cfg() -> dict:
    return config.CFG["vercel"]


def _ledger_path() -> str:
    return _cfg()["ledger_path"]


def _db_path() -> str:
    return config.CFG["9router"]["db_path"]


def _load() -> dict:
    return vr.load(_ledger_path())


def _save(ledger: dict) -> None:
    vr.save(_ledger_path(), ledger)


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "-"
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def _probe_all(ledger: dict, *, timeout: float) -> list[tuple[str, vr.ProbeResult]]:
    target = _cfg()["probe_target"]
    out = []
    for rid, entry in ledger["relays"].items():
        result = vr.probe(entry["url"], target=target, timeout=timeout)
        vr.record_probe(ledger, rid, result)
        out.append((rid, result))
    return out


def cmd_status(args: argparse.Namespace) -> int:
    ledger = _load()
    provider = _cfg()["provider"]
    strategy = vr.read_provider_strategy(_db_path(), provider) if os.path.exists(_db_path()) else {}
    pools = vr.list_pools(_db_path()) if os.path.exists(_db_path()) else []
    now = time.time()

    print(f"台账 {_ledger_path()}：{len(ledger['relays'])} 条 relay")
    if not ledger["relays"]:
        print("  （空。先用 asip vercel add <url> 登记，或 deploy/vercel-relay-deploy.sh 部署）")
    for rid, e in ledger["relays"].items():
        burned = float(e.get("burned_until") or 0)
        state = "OK" if burned <= now and e.get("ip") else ("BURNED" if burned > now else "UNKNOWN")
        print(f"  {rid:<14} {state:<8} ip={e.get('ip') or '-':<16} "
              f"ok={_fmt_ts(float(e.get('last_ok_ts') or 0))} "
              f"probe={_fmt_ts(float(e.get('last_probe_ts') or 0))} "
              f"err={e.get('error') or '-'} pool={e.get('pool_id') or '-'}")
        print(f"                 {e['url']}")
    print(f"\n9router providerStrategies[{provider}] = {json.dumps(strategy, ensure_ascii=False)}")
    print(f"9router 里 type=vercel 的池：{len(pools)} 个")
    for p in pools:
        print(f"  {'on ' if p['active'] else 'off'} {p['id']}  {p['name']}  {p['url']}")
    cfg = _cfg()
    print(f"\n开关：enabled={cfg['enabled']} takeover={cfg['takeover']} "
          f"min_active={cfg['min_active_pools']} guard={cfg['guard_sec']}s")
    return 0 if pools or ledger["relays"] else 2


def cmd_add(args: argparse.Namespace) -> int:
    ledger = _load()
    added, known = [], []
    for url in args.urls:
        before = set(ledger["relays"])
        rid = vr.add_relay(ledger, url, name=args.name or "")
        if rid is None:
            print(f"✗ 不是合法的 relay 地址：{url}", file=sys.stderr)
            return 1
        (known if rid in before else added).append(rid)
    _save(ledger)
    print(f"新登记 {len(added)} 条：{', '.join(added) or '-'}")
    if known:
        print(f"已存在 {len(known)} 条（同 URL 复用旧 id）：{', '.join(known)}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    ledger = _load()
    if not ledger["relays"]:
        print("台账是空的，没有可探的 relay", file=sys.stderr)
        return 1
    dry = not args.apply
    results = _probe_all(ledger, timeout=args.timeout)
    if not dry:
        _save(ledger)
    bad = 0
    for rid, r in results:
        mark = "✓" if r.ok else "✗"
        detail = f"{r.ip}  {r.latency_ms}ms" if r.ok else f"{r.error} (status={r.status})"
        print(f"{mark} {rid:<14} {detail}")
        bad += 0 if r.ok else 1
    print(f"\n{len(results) - bad}/{len(results)} 可用（{'已写台账' if not dry else 'dry-run，未写台账'}）")
    return 2 if bad else 0


def cmd_sync(args: argparse.Namespace) -> int:
    ledger = _load()
    if not ledger["relays"]:
        print("台账是空的，先 add 或 deploy", file=sys.stderr)
        return 1
    dry = not args.apply
    mapping = vr.sync_pools(_db_path(), ledger, dry_run=dry)
    if not dry:
        _save(ledger)
    for rid, pool_id in mapping.items():
        print(f"{rid:<14} -> pool {pool_id}  {ledger['relays'][rid]['url']}")
    print(f"{'dry-run（加 --apply 真写）' if dry else '已写入 9router proxyPools'}：{len(mapping)} 行")
    return 0


def cmd_on(args: argparse.Namespace) -> int:
    """把 provider 的出口指到中继池。

    先 sync（保证池存在），再写策略。顺序不能反：策略指向一个不存在的池时，
    9router 会退回直连 —— 出口变成本机真实 IP，这是最坏的一种失败。
    """
    ledger = _load()
    if not ledger["relays"]:
        print("台账是空的，先 add 或 deploy", file=sys.stderr)
        return 1
    dry = not args.apply
    mapping = vr.sync_pools(_db_path(), ledger, dry_run=dry)
    if not mapping:
        return 1
    provider = _cfg()["provider"]
    before = vr.read_provider_strategy(_db_path(), provider)
    ledger["previous_strategy"] = before
    result = vr.set_provider_strategy(
        _db_path(), provider, strategy=_cfg()["strategy"],
        pool_id=args.pin_pool or next(iter(mapping.values())), dry_run=dry)
    if not dry:
        _save(ledger)
    print(f"provider={provider} 之前={json.dumps(before, ensure_ascii=False)}")
    print(f"之后={json.dumps(result['after'], ensure_ascii=False)}"
          f"{'  (dry-run)' if dry else ''}")
    return 0


def cmd_off(args: argparse.Namespace) -> int:
    ledger = _load()
    previous = ledger.get("previous_strategy") or {}
    dry = not args.apply
    provider = _cfg()["provider"]
    if not previous:
        print("台账里没有 previous_strategy，不知道切回哪里；"
              "手动改 settings.providerStrategies 或指定 --pool", file=sys.stderr)
        return 1
    result = vr.set_provider_strategy(
        _db_path(), provider,
        strategy=previous.get("rotateStrategy", "none"),
        pool_id=previous.get("proxyPoolId") or "", dry_run=dry)
    if not dry:
        ledger.pop("previous_strategy", None)
        _save(ledger)
    print(f"已切回 {json.dumps(previous, ensure_ascii=False)}"
          f"{'  (dry-run)' if dry else ''}")
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    """对账一轮：探活 → 停用烧掉的池 → 启用健康池 → 保持策略。

    这是 daemon 在 403/429 之后会调的那个动作。它不猜「哪条坏了」：403/429 是
    出口级的证据，调用方（daemon）知道是哪一条，用 --burn 指出来；没指就只按
    探测结果对账。
    """
    ledger = _load()
    if not ledger["relays"]:
        print("台账是空的，没有可轮换的 relay", file=sys.stderr)
        return 1
    now = time.time()
    dry = not args.apply

    if args.burn:
        for url_or_id in args.burn:
            rid = _resolve(ledger, url_or_id)
            if rid is None:
                print(f"✗ 台账里没有 {url_or_id}", file=sys.stderr)
                continue
            until = vr.burn(ledger, rid, args.kind, now=now)
            print(f"✗ 标脏 {rid}（{args.kind}，解禁 {_fmt_ts(until)}）")
    else:
        _probe_all(ledger, timeout=args.timeout)

    alive = vr.active_relays(ledger, now=now)
    print(f"可用 relay：{len(alive)}/{len(ledger['relays'])}")

    # 池的启停跟着台账走：烧掉的关掉，健康的打开。round-robin 只在 active 池里转，
    # 所以「停用一个池」等价于「把它从轮换里摘出去」，不需要改策略。
    for rid, entry in ledger["relays"].items():
        pool_id = entry.get("pool_id")
        if not pool_id:
            continue
        should_be_active = rid in alive
        if entry.get("_pool_active") == should_be_active:
            continue
        if not dry:
            vr.set_pool_active(_db_path(), pool_id, should_be_active)
        entry["_pool_active"] = should_be_active
        print(f"{'→' if should_be_active else '✗'} pool {pool_id} ({rid}) "
              f"{'启用' if should_be_active else '停用'}{'  (dry-run)' if dry else ''}")

    if not dry:
        _save(ledger)
    if not alive:
        print("⚠ 没有可用 relay —— 此时应 off 切回原出口，别让 provider 指向空池", file=sys.stderr)
        return 2
    return 0


def _resolve(ledger: dict, needle: str) -> str | None:
    if needle in ledger["relays"]:
        return needle
    norm = vr.normalize_relay_url(needle)
    for rid, entry in ledger["relays"].items():
        if entry.get("url") == norm or entry.get("pool_id") == needle:
            return rid
    return None


def cmd_deploy(args: argparse.Namespace) -> int:
    import subprocess
    script = os.path.join(config.PROJECT_DIR, "deploy", "vercel-relay-deploy.py")
    if not os.path.exists(script):
        print(f"✗ 找不到 {script}", file=sys.stderr)
        return 1
    cmd = [sys.executable, script, "--count", str(args.count)]
    if args.apply:
        cmd.append("--apply")
    if args.region:
        cmd += ["--region", args.region]
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="asip vercel", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="真写 9router 库（默认 dry-run）")
    parser.add_argument("--timeout", type=float, default=10.0)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("status", help="看台账与 9router 现状")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("add", help="登记一条或多条 relay 地址")
    p.add_argument("urls", nargs="+")
    p.add_argument("--name", default="")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("probe", help="经 relay 探出口 IP")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("sync", help="把台账同步进 9router proxyPools")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("on", help="把 provider 出口切到中继池")
    p.add_argument("--pin-pool", default="", help="只用一个池，不做轮换")
    p.set_defaults(func=cmd_on)

    p = sub.add_parser("off", help="切回上一个出口")
    p.set_defaults(func=cmd_off)

    p = sub.add_parser("rotate", help="对账一轮（标脏/探活/池启停）")
    p.add_argument("--burn", nargs="*", default=[], help="标脏哪条（id/url/pool_id）")
    p.add_argument("--kind", default="rate429", choices=sorted(vr._BURN_TTL))
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_rotate)

    p = sub.add_parser("deploy", help="用 Vercel API 部署 N 条 relay")
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--region", default="")
    p.set_defaults(func=cmd_deploy)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
