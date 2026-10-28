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
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vercel-relay")


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


def read_template() -> dict:
    files = {}
    for rel in ("api/relay.js", "vercel.json", "package.json"):
        path = os.path.join(TEMPLATE_DIR, rel)
        if not os.path.exists(path):
            raise SystemExit(f"✗ 模板缺失：{path}")
        with open(path, encoding="utf-8") as f:
            files[rel] = f.read()
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
    ap.add_argument("--apply", action="store_true", help="真部署（默认只打印计划）")
    ap.add_argument("--sync", action="store_true", help="部署完同步进 9router proxyPools")
    args = ap.parse_args(argv)

    token = load_token()
    files = read_template()
    ledger_path = config.CFG["vercel"]["ledger_path"]
    ledger = vr.load(ledger_path)
    stamp = time.strftime("%m%d%H%M")

    plan = [f"{args.prefix}-{stamp}-{i + 1:02d}" for i in range(max(1, args.count))]
    print(f"计划部署 {len(plan)} 条（region={args.region or '账号默认'}）：")
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
            url = "https://" + str(ready.get("url") or dep.get("url")).replace("https://", "")
            rid = vr.add_relay(ledger, url, name=name)
            vr.save(ledger_path, ledger)
            ok.append((name, url))
            print(f"  ✓ {url}  (relay_id={rid})")
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
