#!/usr/bin/env python3
"""把 .warp/warp-outbound.json 里的 WARP-WG-* 出口并入现有 singbox-config.json。

为什么需要它：``refresh_subscription`` 是配置的唯一重生成入口，而它在订阅源
（OKZ_SUB_URL 等）为空时直接返回「无可用订阅源」。此时新铺的 WG 线只存在于
``.warp/warp-outbound.json`` 里，主进程的 ``opencode-out`` selector 永远看不到
它们 —— 换线全部 ``status=400``，日志刷屏，现象像服务故障。

订阅源一旦配回来，``parse_subscription.py`` 会重生成整份配置并自动带上这些
出口，本脚本随之退化成 no-op。所以它只覆盖「无订阅期」，不是长期路径。

用法：
    python3 deploy/patch-wg-outbounds.py           # 并入（幂等）
    python3 deploy/patch-wg-outbounds.py --check   # 只检查：需并入时退出 1

输出：CHANGED / UNCHANGED —— 调用方据此决定要不要重启 sing-box。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
CFG = PROJECT / "singbox-config.json"
WARP = PROJECT / ".warp" / "warp-outbound.json"
PREFIX = "WARP-WG-"


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    if not CFG.exists():
        print(f"✗ 主配置不存在: {CFG}（先让 daemon 生成一次，或配好订阅源）",
              file=sys.stderr)
        return 2
    if not WARP.exists():
        print(f"✗ 出口清单不存在: {WARP}（先跑 warp_wg.py apply）", file=sys.stderr)
        return 2

    cfg = json.loads(CFG.read_text())
    want = [w for w in json.loads(WARP.read_text()) if w["tag"].startswith(PREFIX)]
    if not want:
        print("UNCHANGED")
        return 0

    have = {o.get("tag") for o in cfg.get("outbounds", [])}
    added = [w for w in want if w["tag"] not in have]

    sel = next((o for o in cfg.get("outbounds", [])
                if o.get("tag") == "opencode-out"), None)
    if sel is None:
        # 没有 selector 时并入出口毫无意义：daemon 全部换线都打空。
        print("✗ 主配置里没有 opencode-out selector，拒绝并入", file=sys.stderr)
        return 2

    cur = list(sel.get("outbounds") or [])
    new = [w["tag"] for w in want if w["tag"] not in cur]
    if not added and not new:
        print("UNCHANGED")
        return 0

    if check_only:
        print(f"CHANGED 待并入出口 {len(added)} 条、selector 缺 {len(new)} 项")
        return 1

    cfg["outbounds"].extend(added)
    # proxy 垫底：WARP 全挂时仍有直连兜底，这是原配置的既有约定。
    if "proxy" in cur:
        cur = [t for t in cur if t != "proxy"] + new + ["proxy"]
    else:
        cur = cur + new
    sel["outbounds"] = cur

    CFG.write_text(json.dumps(cfg, indent=2))
    print(f"CHANGED 新增出口 {len(added)} 条，selector 现有 {len(cur)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
