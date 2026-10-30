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
    # 池表里有四种中继类型，不能只报 vercel —— 接 netlify 之后这句话会说谎。
    by_kind = {}
    for row in pools:
        by_kind[row.get("kind") or "?"] = by_kind.get(row.get("kind") or "?", 0) + 1
    detail = "、".join(f"{k}={v}" for k, v in sorted(by_kind.items())) or "-"
    print(f"9router 里的中继池：{len(pools)} 个（{detail}）")
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
        rid = vr.add_relay(ledger, url, name=args.name or "", kind=args.kind)
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


# 哪些 provider 的中继出口是"物理不通"的：不是配置问题，是代码里写死了拒绝。
RELAY_INCOMPATIBLE_PROVIDERS = {
    "opencode": (
        "opencodeTransport.js: if (normalizeString(proxyOptions?.vercelRelayUrl)) "
        "return noteFailure(\"vercel-relay\") —— free lane 必须由本机 Bun 直连上游，"
        "中继会让 TLS 指纹变成中继方的，实测必 429。"
    ),
}


def cmd_on(args: argparse.Namespace) -> int:
    """把 provider 的出口指到中继池。

    先 sync（保证池存在），再写策略。顺序不能反：策略指向一个不存在的池时，
    9router 会退回直连 —— 出口变成本机真实 IP，这是最坏的一种失败。
    """
    provider = _cfg()["provider"]
    # 2026-10-29 23:04 踩的坑：opencode 的 free lane 走 open-sse/utils/opencodeTransport.js，
    # 里面有一行硬闸 —— proxyOptions.vercelRelayUrl 非空就 noteFailure("vercel-relay")。
    # 原因是这条通道要求 TLS 指纹由本机 Bun 亲自跟上游握，经过中继这一跳指纹就变成
    # 中继方的，实测必定 429；所以它宁可快速失败，也不悄悄绕回直连。
    # 那天 on --apply 把出口切到 13 条中继池，所有请求立刻 503。
    # 这条闸现在挡在前面：不是"别忘了"，是"不许"。
    if provider in RELAY_INCOMPATIBLE_PROVIDERS and not getattr(args, "force", False):
        print(f"✗ 拒绝：{provider} 的 transport 会拒中继（vercelRelayUrl 非空即 503）。"
              f"\n  {RELAY_INCOMPATIBLE_PROVIDERS[provider]}"
              f"\n  真要走中继得传 --force，但那等于明知会 503。", file=sys.stderr)
        return 2

    ledger = _load()
    if not ledger["relays"]:
        print("台账是空的，先 add 或 deploy", file=sys.stderr)
        return 1
    dry = not args.apply
    mapping = vr.sync_pools(_db_path(), ledger, dry_run=dry)
    if not mapping:
        return 1
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


def cmd_bench(args: argparse.Namespace) -> int:
    """让每条 relay 自己报一次账：函数 -> 上游花多久。

    这是唯一一种不受本机网络抖动影响的量法 —— 本地那一段（1–2.5s 的摆动）
    根本不出现在算式里，因为它测的是函数内部 Date.now() 的差。
    """
    import statistics
    ledger = _load()
    targets = ([_resolve(ledger, args.id)] if args.id else
               [rid for rid, e in ledger["relays"].items() if e.get("enabled", True)])
    targets = [t for t in targets if t]
    if not targets:
        print("没有可测的 relay", file=sys.stderr)
        return 1
    print(f"{'id':<14}{'运行时':<9}{'区域':<10}{'fetch_ms':<10}{'首块_ms':<9}"
          f"{'总耗时_ms':<10}{'字节':<9}目标")
    bad = 0
    for rid in targets:
        e = ledger["relays"][rid]
        samples = []
        last_error = None
        for _ in range(max(1, args.samples)):
            r = vr.timing_probe(e["url"], target=args.target, timeout=args.timeout)
            if r.get("ok"):
                samples.append(r)
            elif r.get("error") == "no_timing_support":
                print(f"{rid:<14}{e.get('runtime', '-'):<9}{(e.get('region') or '-'):<10}"
                      f"（这条还是老函数，refresh 一次才会有 timing）")
                bad += 1
                break
            last_error = r.get("error")
            if last_error == "no_timing_support":
                break
        if not samples:
            if not (locals().get("last_error") == "no_timing_support"):
                print(f"{rid:<14}{e.get('runtime', '-'):<9}{(e.get('region') or '-'):<10}"
                      f"✗ 测不到（{last_error or '?'}）")
                bad += 1
            continue
        last_error = None
        fetch = int(statistics.median([x["fetch_ms"] for x in samples]))
        first = [x["first_chunk_ms"] for x in samples if x.get("first_chunk_ms")]
        total = int(statistics.median([x["total_ms"] for x in samples]))
        size = max(x["bytes"] for x in samples)
        vr.record_timing(ledger, rid, samples[-1])
        print(f"{rid:<14}{e.get('runtime', '-'):<9}{(e.get('region') or '-'):<10}"
              f"{fetch:<10}{int(statistics.median(first)) if first else '-':<9}"
              f"{total:<10}{size:<9}{args.target}")
    _save(ledger)
    print(f"\n测了 {len(targets)} 条，失败 {bad} 条（{args.samples} 次取中位）")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """一张表看全：哪条、什么运行时、哪个区、出口 IP、开没开、池子在不在。

    这张表就是「可控」的意思 —— 换 IP 之前得先知道自己手里有哪几张牌。
    """
    ledger = _load()
    if not ledger["relays"]:
        print("台账是空的", file=sys.stderr)
        return 1
    pools = {}
    if os.path.exists(_db_path()):
        pools = {p["id"]: p for p in vr.list_pools(_db_path())}
    now = time.time()
    print(f"{'id':<14}{'开':<4}{'运行时':<9}{'区域':<10}{'出口 IP':<17}{'状态':<8}{'池':<6}地址")
    for rid, e in ledger["relays"].items():
        burned = float(e.get("burned_until") or 0)
        if not e.get("enabled", True):
            state = "关"
        elif burned > now:
            state = "烧"
        elif e.get("ip"):
            state = "可用"
        else:
            state = "未测"
        pool = pools.get(e.get("pool_id") or "")
        pool_txt = "-" if pool is None else ("on" if pool["active"] else "off")
        print(f"{rid:<14}{'on' if e.get('enabled', True) else 'off':<4}"
              f"{e.get('runtime', 'edge'):<9}{(e.get('region') or '-'):<10}"
              f"{(e.get('ip') or '-'):<17}{state:<8}{pool_txt:<6}{e['url']}")
    on = sum(1 for e in ledger["relays"].values() if e.get("enabled", True))
    print(f"\n共 {len(ledger['relays'])} 条，开着 {on} 条")
    return 0


def cmd_toggle(args: argparse.Namespace, *, enable: bool) -> int:
    """开/关一条。关不等于删：地址、区、上次 IP 都留着，随时能打开。"""
    ledger = _load()
    rid = _resolve(ledger, args.id)
    if rid is None:
        print(f"✗ 找不到 {args.id}", file=sys.stderr)
        return 1
    entry = ledger["relays"][rid]
    apply_ = getattr(args, "apply", False)
    vr.set_enabled(ledger, rid, enable)
    if not apply_:
        _save(ledger)
    print(f"{'开' if enable else '关'} {rid}  {entry['url']}（台账已改）")
    # 池子跟着动：9router 只认 isActive
    pool_id = entry.get("pool_id") or ""
    if not pool_id:
        print("  （还没同步进 9router，先 asip vercel sync --apply）")
        return 0
    changed = vr.set_pool_active(_db_path(), pool_id, enable, dry_run=not apply_)
    print(f"  池 {pool_id} -> {'on' if enable else 'off'}：{changed}")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    return cmd_toggle(args, enable=True)


def cmd_disable(args: argparse.Namespace) -> int:
    return cmd_toggle(args, enable=False)


def cmd_refresh(args: argparse.Namespace) -> int:
    """原地重部署一条 relay —— 换出口 IP 的正经办法。

    实测（2026-10-29）：同一个项目重部署，iad1 从 3.89.27.199 换成 54.198.100.196，
    sin1 从 18.142.186.105 换成 52.77.250.68。项目和别名都不变，只有 IP 变。
    这就是「可控换 IP」：不需要新建项目，也不需要等它自己漂。
    """
    import subprocess
    ledger = _load()
    rid = _resolve(ledger, args.id)
    if rid is None:
        print(f"✗ 找不到 {args.id}", file=sys.stderr)
        return 1
    entry = ledger["relays"][rid]
    project = entry.get("project") or ""
    if not project:
        host = (entry["url"].split("//", 1)[-1]).split("/", 1)[0]
        if host.endswith(".vercel.app"):
            project = host[: -len(".vercel.app")]
    if not project:
        print("✗ 这条没有项目名，没法原地重来（用 deploy 新建一条吧）", file=sys.stderr)
        return 1
    if (entry.get("kind") or "vercel") != "vercel":
        print(f"✗ 这条是 {entry.get('kind')}，refresh 目前只接了 Vercel。"
              "\n  Netlify 要重部署得走 CLI（cd 到站点目录 netlify deploy --prod），"
              "这条路还没包进命令里。", file=sys.stderr)
        return 1
    script = os.path.join(config.PROJECT_DIR, "deploy", "vercel-relay-deploy.py")
    cmd = [sys.executable, script, "--name", project,
           "--runtime", entry.get("runtime") or "edge"]
    if entry.get("region"):
        cmd += ["--region", entry["region"]]
    apply_ = getattr(args, "apply", False)
    if apply_:
        cmd.append("--apply")
    print(f"→ 重部署 {project}（runtime={entry.get('runtime')} region={entry.get('region') or '-'}）")
    if not apply_:
        return subprocess.call(cmd)

    # 循环挪进模块了（refresh_until_new_ip）：那里用的是现成的 probe / record_probe，
    # 以后 daemon 要复用同一套「烧了就换」的逻辑，不用再抄一遍。
    attempts = max(1, int(getattr(args, "attempts", 3)))

    def _redeploy() -> None:
        rc = subprocess.call(cmd)
        if rc != 0:
            raise RuntimeError(f"deploy failed rc={rc}")

    try:
        out = vr.refresh_until_new_ip(ledger, rid, _redeploy, attempts=attempts)
    except RuntimeError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1
    _save(ledger)
    for t in out["tries"]:
        mark = "" if t["changed"] else "（没换）"
        print(f"  第 {t['attempt']} 次：{out['old'] or '-'} -> {t['ip'] or '-'}{mark}")
    if out["ok"]:
        return 0
    print(f"  试了 {out['attempts']} 次还是 {out['old'] or '-'}。"
          "重部署换 IP 不是必然的，可以多试几次，或者换个区域新开一条。",
          file=sys.stderr)
    return 2


def cmd_import(args: argparse.Namespace) -> int:
    """把账号里已有的 relay 项目扫进台账（名字里带区域的那种）。

    名字约定：asip-nr-<region>-<stamp> = Node 运行时、钉在 region；
    其他 asip-* = Edge。约定是给 import 用的，不是给 9router 用的。
    """
    import re as _re
    import requests
    token = (os.environ.get("VERCEL_TOKEN") or "").strip()
    if not token:
        env_file = os.path.join(config.PROJECT_DIR, ".env")
        try:
            with open(env_file, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VERCEL_TOKEN="):
                        token = line.split("=", 1)[1].strip()
        except FileNotFoundError:
            pass
    if not token:
        print("✗ 没有 VERCEL_TOKEN", file=sys.stderr)
        return 1
    head = {"Authorization": "Bearer " + token}
    projects, until = [], None
    for _ in range(6):
        url = "https://api.vercel.com/v9/projects?limit=100" + (f"&until={until}" if until else "")
        data = requests.get(url, headers=head, timeout=40).json()
        projects += data.get("projects", [])
        until = (data.get("pagination") or {}).get("next")
        if not until:
            break
    ledger = _load()
    added, probed = [], []
    for proj in projects:
        name = proj.get("name") or ""
        if not name.startswith(args.prefix):
            continue
        m = _re.match(r"^asip-nr-([a-z]{3}\d)-", name)
        runtime, region = ("node", m.group(1)) if m else ("edge", "")
        rid = vr.add_relay(ledger, f"https://{name}.vercel.app", name=name,
                           runtime=runtime, region=region, project=name)
        if rid:
            added.append(rid)
            probed.append(rid)
    for rid in probed:
        result = vr.probe(ledger["relays"][rid]["url"], timeout=args.timeout)
        vr.record_probe(ledger, rid, result)
    _save(ledger)
    print(f"扫到 {len(added)} 条（前缀 {args.prefix}），已探活")
    for rid in added:
        e = ledger["relays"][rid]
        print(f"  {rid:<14}{e.get('runtime'):<6}{(e.get('region') or '-'):<7}"
              f"{(e.get('ip') or '-'):<17}{e['url']}")
    return 0


def _resolve(ledger: dict, needle: str) -> str | None:
    if needle in ledger["relays"]:
        return needle
    norm = vr.normalize_relay_url(needle)
    for rid, entry in ledger["relays"].items():
        if entry.get("url") == norm or entry.get("pool_id") == needle:
            return rid
    return None


_DEPLOY_SCRIPTS = {
    "vercel": "vercel-relay-deploy.py",
    "netlify": "netlify-relay-deploy.py",
}


def cmd_deploy(args: argparse.Namespace) -> int:
    """按平台转发给各自的部署脚本。

    两家脚本都自己 dry-run、自己读 token、自己写台账 —— 这里只做分发，
    不复制一份参数表（复制的那份一定会跟脚本漂移）。
    """
    import subprocess
    script = os.path.join(config.PROJECT_DIR, "deploy",
                          _DEPLOY_SCRIPTS.get(args.provider, ""))
    if not os.path.exists(script):
        print(f"✗ 找不到 {script}（provider={args.provider}）", file=sys.stderr)
        return 1
    cmd = [sys.executable, script, "--count", str(args.count)]
    if args.apply:
        cmd.append("--apply")
    if args.sync:
        cmd.append("--sync")
    if args.provider == "vercel" and getattr(args, "region", ""):
        cmd += ["--region", args.region]
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="asip vercel", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="真写 9router 库（默认 dry-run）")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--proxy", default="",
                        help="本机代理，如 http://127.0.0.1:7897（不给就读环境变量）")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("status", help="看台账与 9router 现状")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("add", help="登记一条或多条 relay 地址")
    p.add_argument("urls", nargs="+")
    p.add_argument("--name", default="")
    # kind 决定同步时写进 proxyPools.data.type；9router 只认这四个
    p.add_argument("--kind", default=vr.RELAY_TYPE, choices=list(vr.RELAY_KINDS))
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("bench", help="让 relay 自己报耗时（函数 -> 上游，不含本地网络）")
    p.add_argument("id", nargs="?", default="", help="只测这一条（默认全部开着的）")
    p.add_argument("--target", default="https://api.ipify.org")
    p.add_argument("--samples", type=int, default=3)
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("list", help="看台账全表（运行时/区域/IP/开关/池）")
    p.set_defaults(func=cmd_list)

    for cmd_name, fn, desc in (("enable", cmd_enable, "打开一条 relay"),
                               ("disable", cmd_disable, "关掉一条 relay（不删）")):
        p = sub.add_parser(cmd_name, help=desc)
        p.add_argument("id", help="relay id / url / pool_id")
        p.add_argument("--apply", action="store_true", default=argparse.SUPPRESS,
                       help="同步到 9router 池的 isActive")
        p.set_defaults(func=fn)

    p = sub.add_parser("refresh", help="原地重部署一条 relay（换出口 IP，没换会再试）")
    p.add_argument("id", help="relay id / url / pool_id")
    p.add_argument("--attempts", type=int, default=3, help="最多重部署几次（默认 3）")
    p.add_argument("--apply", action="store_true", default=argparse.SUPPRESS,
                   help="真部署（默认只打印计划）")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("import", help="把账号里已部署的 relay 扫进台账")
    p.add_argument("--prefix", default="asip-")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("probe", help="经 relay 探出口 IP")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("sync", help="把台账同步进 9router proxyPools")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("on", help="把 provider 出口切到中继池")
    p.add_argument("--pin-pool", default="", help="只用一个池，不做轮换")
    p.add_argument("--force", action="store_true",
                   help="明知这个 provider 的 transport 会拒中继，仍要切（会 503）")
    p.set_defaults(func=cmd_on)

    p = sub.add_parser("off", help="切回上一个出口")
    p.set_defaults(func=cmd_off)

    p = sub.add_parser("rotate", help="对账一轮（标脏/探活/池启停）")
    p.add_argument("--burn", nargs="*", default=[], help="标脏哪条（id/url/pool_id）")
    p.add_argument("--kind", default="rate429", choices=sorted(vr._BURN_TTL))
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_rotate)

    p = sub.add_parser("deploy", help="部署 N 条 relay（vercel / netlify）")
    p.add_argument("--provider", default="vercel", choices=sorted(_DEPLOY_SCRIPTS))
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--region", default="", help="仅 vercel：如 iad1")
    p.add_argument("--sync", action="store_true", help="部署完顺带同步池")
    p.set_defaults(func=cmd_deploy)

    args = parser.parse_args(argv)
    if getattr(args, "proxy", ""):
        # 直连境外会被 TLS 重置（实测 curl exit 35），这不是网络坏了，
        # 是流量没走代理。这里显式设上，免得又一次「一声不吭地直连」。
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            os.environ[key] = args.proxy
        print(f"（走代理 {args.proxy}）")
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
