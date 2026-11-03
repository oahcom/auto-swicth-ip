#!/usr/bin/env python3
"""部署 N 条 Netlify 中继，并把地址登记进 asip 台账。

用法：
    NETLIFY_AUTH_TOKEN=xxx ./deploy/netlify-relay-deploy.py --count 2           # dry-run
    NETLIFY_AUTH_TOKEN=xxx ./deploy/netlify-relay-deploy.py --count 2 --apply   # 真部署

与 vercel-relay-deploy.py 是同一件事的两个平台实现。Netlify 比 Vercel 多一道手续：
**函数必须先打成 zip 上传**，站点文件（netlify.toml）单独传。调用顺序照着
netlify-cli 的 upload-files.ts / hash-fns.ts 来：先 POST 部署描述（路径 -> sha1），
再 PUT 需要的文件，最后轮询 state。

⚠️ 本脚本**没有在真账号上跑过**（机器上没有 token）。API 形状来自 netlify-cli 源码，
但字段拼错只会在真跑时暴露 —— 第一次跑请盯着输出，出错时先看返回体。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

import config  # noqa: E402
from asip import vercel_relay as vr  # noqa: E402

API = "https://api.netlify.com/api/v1"
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netlify-relay")
FUNCTION_NAME = "relay"


def load_token() -> str:
    token = (os.environ.get("NETLIFY_AUTH_TOKEN") or "").strip()
    if token:
        return token
    try:
        with open(os.path.join(config.PROJECT_DIR, ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("NETLIFY_AUTH_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def headers(token: str, content_type: str = "application/json") -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": content_type}


def function_zip() -> bytes:
    """把 netlify/functions/relay.mjs 打成 Netlify 认的函数包（入口在包根）。"""
    src = os.path.join(TEMPLATE_DIR, "netlify", "functions", "relay.mjs")
    if not os.path.exists(src):
        raise SystemExit(f"✗ 模板缺失：{src}")
    with open(src, encoding="utf-8") as f:
        body = f.read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{FUNCTION_NAME}.mjs", body)
    return buf.getvalue()


def site_files() -> dict[str, bytes]:
    files = {}
    for rel in ("netlify.toml",):
        path = os.path.join(TEMPLATE_DIR, rel)
        if not os.path.exists(path):
            raise SystemExit(f"✗ 模板缺失：{path}")
        with open(path, "rb") as f:
            files["/" + rel] = f.read()
    return files


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def default_account(token: str) -> str:
    res = requests.get(f"{API}/accounts", headers=headers(token), timeout=30)
    if res.status_code >= 300:
        raise RuntimeError(f"读账号失败 HTTP {res.status_code}: {res.text[:200]}")
    accounts = res.json()
    if not accounts:
        raise RuntimeError("这个 token 下没有账号")
    return accounts[0]["slug"]


def create_site(token: str, account: str, name: str) -> dict:
    res = requests.post(f"{API}/{account}/sites", headers=headers(token),
                        data=json.dumps({"name": name}), timeout=60)
    if res.status_code >= 300:
        # 名字撞了不是错误：重跑同一批时应该复用，而不是新建一堆。
        if res.status_code in (409, 422):
            look = requests.get(f"{API}/sites?name={name}",
                                headers=headers(token), timeout=30)
            hits = look.json() if look.status_code < 300 else []
            if hits:
                return hits[0]
        raise RuntimeError(f"建站点失败 HTTP {res.status_code}: {res.text[:300]}")
    return res.json()


def create_deploy(token: str, site_id: str, files: dict[str, bytes], fn_zip: bytes) -> dict:
    payload = {
        "files": {p: sha1(b) for p, b in files.items()},
        "functions": {FUNCTION_NAME: sha1(fn_zip)},
    }
    res = requests.post(f"{API}/sites/{site_id}/deploys", headers=headers(token),
                        data=json.dumps(payload), timeout=60)
    if res.status_code >= 300:
        raise RuntimeError(f"创建部署失败 HTTP {res.status_code}: {res.text[:300]}")
    return res.json()


def upload(token: str, deploy_id: str, files: dict[str, bytes], fn_zip: bytes) -> None:
    for path, body in files.items():
        res = requests.put(f"{API}/deploys/{deploy_id}/files{path}",
                           headers=headers(token, "application/octet-stream"),
                           data=body, timeout=120)
        if res.status_code >= 300:
            raise RuntimeError(f"上传 {path} 失败 HTTP {res.status_code}: {res.text[:200]}")
    res = requests.put(f"{API}/deploys/{deploy_id}/functions/{FUNCTION_NAME}",
                       headers=headers(token, "application/zip"),
                       data=fn_zip, timeout=120)
    if res.status_code >= 300:
        raise RuntimeError(f"上传函数失败 HTTP {res.status_code}: {res.text[:200]}")


def wait_ready(token: str, site_id: str, deploy_id: str, *, max_ms: int = 180000) -> dict:
    start = time.time()
    while (time.time() - start) * 1000 < max_ms:
        res = requests.get(f"{API}/sites/{site_id}/deploys/{deploy_id}",
                           headers=headers(token), timeout=30)
        data = res.json() if res.status_code < 300 else {}
        state = data.get("state")
        if state == "ready":
            return data
        if state in ("error", "rejected"):
            raise RuntimeError(f"部署失败：{state}")
        time.sleep(3)
    raise RuntimeError("部署超时（180s）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--prefix", default="asip-relay")
    ap.add_argument("--account", default="", help="账号 slug；留空取第一个")
    ap.add_argument("--apply", action="store_true", help="真部署（默认只打印计划）")
    ap.add_argument("--sync", action="store_true", help="部署完同步进 9router proxyPools")
    args = ap.parse_args(argv)

    fn_zip = function_zip()
    files = site_files()
    stamp = time.strftime("%m%d%H%M")
    plan = [f"{args.prefix}-{stamp}-{i + 1:02d}" for i in range(max(1, args.count))]

    print(f"计划部署 {len(plan)} 条 Netlify 中继（函数包 {len(fn_zip)} 字节）：")
    for name in plan:
        print(f"  {name}")
    if not args.apply:
        print("\ndry-run：加 --apply 才真部署")
        return 0

    token = load_token()
    if not token:
        print("✗ 没有 NETLIFY_AUTH_TOKEN（环境变量或项目 .env 里）。\n"
              "  https://app.netlify.com/user/applications#personal-access-tokens 建一个，然后：\n"
              "  NETLIFY_AUTH_TOKEN=xxx ./deploy/netlify-relay-deploy.py --count 2 --apply",
              file=sys.stderr)
        return 1

    account = args.account or default_account(token)
    ledger_path = config.CFG["vercel"]["ledger_path"]
    ledger = vr.load(ledger_path)

    ok, failed = [], []
    for name in plan:
        try:
            print(f"\n→ {name} …", flush=True)
            site = create_site(token, account, name)
            deploy = create_deploy(token, site["id"], files, fn_zip)
            upload(token, deploy["id"], files, fn_zip)
            wait_ready(token, site["id"], deploy["id"])
            url = (site.get("ssl_url") or f"https://{name}.netlify.app").rstrip("/")
            rid = vr.add_relay(ledger, url, name=name, kind="netlify")
            vr.save(ledger_path, ledger)
            ok.append((name, url))
            print(f"  ✓ {url}  (relay_id={rid})")
        except Exception as e:  # 一条失败不带走整批
            failed.append((name, str(e)))
            print(f"  ✗ {name}: {e}", file=sys.stderr)

    print(f"\n成功 {len(ok)} / 失败 {len(failed)}")
    if args.sync and ok:
        mapping = vr.sync_pools(config.CFG["9router"]["db_path"], ledger)
        vr.save(ledger_path, ledger)
        print(f"已同步 {len(mapping)} 个池进 9router（type=netlify）")
    elif ok:
        print("\n下一步：asip vercel probe   # 探出口 IP")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
