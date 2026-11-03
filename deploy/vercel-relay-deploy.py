#!/usr/bin/env python3
"""部署 N 条 Vercel 中继，并把地址登记进 asip 台账。

用法：
    VERCEL_TOKEN=xxx ./deploy/vercel-relay-deploy.py --count 3            # dry-run，只打印
    VERCEL_TOKEN=xxx ./deploy/vercel-relay-deploy.py --count 3 --apply    # 真部署
    ... --apply --sync    # 部署完顺手同步进 9router 的 proxyPools

为什么不用 9router 自带的 /api/proxy-pools/vercel-deploy：
那条路由一次只建一条、且要求已登录的浏览器会话；部署是运维动作，不该依赖 GUI。
这里直接打 Vercel 的部署 API，内联同一份 relay 代码（deploy/vercel-relay/api/relay.js）。

token 只从环境变量或 .env 读，不写台账、不打印、不进日志。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

import config  # noqa: E402
from asip import vercel_relay as vr  # noqa: E402

API = "https://api.vercel.com"
HERE = os.path.dirname(os.path.abspath(__file__))
# 两份模板：edge 不能选区域、但能长流；node 能钉区域、但有 maxDuration。
TEMPLATE_DIRS = {
    "edge": os.path.join(HERE, "vercel-relay"),
    "node": os.path.join(HERE, "vercel-relay-node"),
}


def load_token() -> str:
    token = (os.environ.get("VERCEL_TOKEN") or "").strip()
    if token:
        return token
    env_file = os.path.join(config.PROJECT_DIR, ".env")
    try:
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("VERCEL_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def read_template(runtime: str = "edge", region: str = "", max_duration: int = 60) -> dict:
    """读模板；node 运行时把区域和时长写进 vercel.json。

    区域必须写在 functions.regions 里 —— 实测顶层 regions 对 Edge 无效，
    而对 Node 函数走 vercel.json 是稳的那条路。
    """
    base = TEMPLATE_DIRS.get(runtime)
    if not base:
        raise SystemExit(f"✗ 未知运行时：{runtime}")
    files = {}
    for rel in ("api/relay.js", "vercel.json", "package.json"):
        path = os.path.join(base, rel)
        if not os.path.exists(path):
            raise SystemExit(f"✗ 模板缺失：{path}")
        with open(path, encoding="utf-8") as f:
            files[rel] = f.read()
    if runtime == "node":
        cfg = json.loads(files["vercel.json"])
        fn = {"maxDuration": max_duration}
        if region:
            fn["regions"] = [region]
        cfg.setdefault("functions", {})["api/relay.js"] = fn
        files["vercel.json"] = json.dumps(cfg, indent=2)
    return files


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def create_deployment(token: str, name: str, files: dict, region: str) -> dict:
    payload = {
        "name": name,
        "files": [{"file": k, "data": v} for k, v in files.items()],
        "projectSettings": {"framework": None},
        "target": "production",
    }
    if region:
        payload["regions"] = [region]
    res = requests.post(f"{API}/v13/deployments", headers=headers(token),
                        data=json.dumps(payload), timeout=60)
    if res.status_code >= 300:
        raise RuntimeError(f"创建部署失败 HTTP {res.status_code}: {res.text[:300]}")
    return res.json()


def poll_ready(token: str, deploy_id: str, *, max_ms: int = 180000) -> dict:
    start = time.time()
    while (time.time() - start) * 1000 < max_ms:
        res = requests.get(f"{API}/v13/deployments/{deploy_id}",
                           headers=headers(token), timeout=30)
        data = res.json() if res.status_code < 300 else {}
        state = data.get("readyState") or data.get("status")
        if state == "READY":
            return data
        if state in ("ERROR", "CANCELED"):
            raise RuntimeError(f"部署失败：{state}")
        time.sleep(3)
    raise RuntimeError("部署超时（180s）")


def disable_protection(token: str, project_id: str) -> None:
    # Vercel 默认给预览/生产开 SSO 保护，开了以后 relay 会回 401 ——
    # 这是最容易漏的一步（9router 那条路由也做了同样的事）。
    try:
        requests.patch(f"{API}/v9/projects/{project_id}", headers=headers(token),
                       data=json.dumps({"ssoProtection": None}), timeout=30)
    except requests.RequestException as e:
        print(f"  ⚠ 关 ssoProtection 失败（可能已关）：{e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=config.CFG["vercel"]["deploy"]["count"])
    ap.add_argument("--region", default=config.CFG["vercel"]["deploy"]["region"],
                    help="如 iad1 / sfo1；留空用账号默认")
    ap.add_argument("--prefix", default=config.CFG["vercel"]["deploy"]["project_prefix"])
    ap.add_argument("--runtime", default="edge", choices=sorted(TEMPLATE_DIRS),
                    help="edge=可长流但不能选区域；node=能钉区域但有 maxDuration")
    ap.add_argument("--max-duration", type=int, default=60, help="仅 node：函数时长上限（秒）")
    ap.add_argument("--name", default="", help="指定项目名（原地重部署用，换 IP 靠它）")
    ap.add_argument("--apply", action="store_true", help="真部署（默认只打印计划）")
    ap.add_argument("--sync", action="store_true", help="部署完同步进 9router proxyPools")
    args = ap.parse_args(argv)

    token = load_token()
    files = read_template(args.runtime, args.region, args.max_duration)
    ledger_path = config.CFG["vercel"]["ledger_path"]
    ledger = vr.load(ledger_path)
    stamp = time.strftime("%m%d%H%M")

    if args.name:
        plan = [args.name]
    else:
        plan = [f"{args.prefix}-{stamp}-{i + 1:02d}" for i in range(max(1, args.count))]
    where = args.region or "账号默认"
    if args.region and args.runtime == "edge":
        where += "（⚠ Edge 不认 region，出口仍跟边缘节点走）"
    print(f"计划部署 {len(plan)} 条（runtime={args.runtime} region={where}）：")
    for name in plan:
        print(f"  {name}")
    if not args.apply:
        print("\ndry-run：加 --apply 才真部署")
        return 0
    if not token:
        print("✗ 没有 VERCEL_TOKEN（环境变量或项目 .env 里）。\n"
              "  去 https://vercel.com/account/tokens 建一个，然后：\n"
              "  VERCEL_TOKEN=xxx ./deploy/vercel-relay-deploy.py --count 3 --apply",
              file=sys.stderr)
        return 1

    ok, failed = [], []
    for name in plan:
        try:
            print(f"\n→ {name} …", flush=True)
            dep = create_deployment(token, name, files, args.region)
            dep_id = dep.get("id") or dep.get("uid")
            project_id = dep.get("projectId") or name
            ready = poll_ready(token, dep_id)
            disable_protection(token, project_id)
            deploy_url = "https://" + str(ready.get("url") or dep.get("url")).replace("https://", "")
            # 台账里存**项目别名**而不是部署专用 URL：别名在重部署后依然有效，
            # 而部署 URL 会随下一次部署变成 404 —— 换 IP 的前提是地址能一直用。
            # 登记的一律是别名：部署专用 URL 带 hash，下次部署就 404，
            # 而且它和别名对不上 —— 以前探失败就退回去登部署 URL，结果是
            # 同一个项目在台账里变成两条（2026-10-29 真踩过）。
            alias = f"https://{name}.vercel.app"
            timeout = float(config.CFG["vercel"]["probe_timeout_sec"])
            probed = vr.probe(alias, timeout=timeout)
            if not probed.ok:
                probed = vr.probe(deploy_url, timeout=timeout)  # 只用来探，不登记
            url = alias
            rid = vr.add_relay(ledger, url, name=name, runtime=args.runtime,
                               region=args.region, project=name)
            if rid:
                vr.record_probe(ledger, rid, probed)
            vr.save(ledger_path, ledger)
            ok.append((name, url))
            print(f"  ✓ {url}  (relay_id={rid}, 出口 {probed.ip or '-'})")
        except Exception as e:  # 一条失败不该带走整批
            failed.append((name, str(e)))
            print(f"  ✗ {name}: {e}", file=sys.stderr)

    print(f"\n成功 {len(ok)} / 失败 {len(failed)}")
    for name, url in ok:
        print(f"  {url}")

    if args.sync and ok:
        mapping = vr.sync_pools(config.CFG["9router"]["db_path"], ledger)
        vr.save(ledger_path, ledger)
        print(f"\n已同步 {len(mapping)} 个池进 9router")
    elif ok:
        print("\n下一步：asip vercel probe   # 探出口 IP（E1）")

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
