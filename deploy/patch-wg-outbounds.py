#!/usr/bin/env python3
"""把 .warp/warp-outbound.json 里的 WARP-WG-* socks 出口并入现有 singbox-config.json。

订阅源未配置（OKZ_SUB_URL 等为空）时 parse_subscription 无法重生成配置，
而 16 条 WG 线已经起来了 —— 只能就地把出口补进 selector。
幂等：已存在同名 tag 就跳过。
"""
import json, sys
from pathlib import Path

PROJECT = Path("/home/administrator/auto-swicth-ip")
cfg_p = PROJECT / "singbox-config.json"
warp_p = PROJECT / ".warp" / "warp-outbound.json"

cfg = json.loads(cfg_p.read_text())
warps = json.loads(warp_p.read_text())
want = [w for w in warps if w["tag"].startswith("WARP-WG-")]
have = {o.get("tag") for o in cfg["outbounds"]}
added = [w for w in want if w["tag"] not in have]
cfg["outbounds"].extend(added)

sel = next((o for o in cfg["outbounds"] if o.get("tag") == "opencode-out"), None)
if sel is None:
    sys.exit("✗ 找不到 opencode-out selector")
cur = sel.get("outbounds") or []
new = [t for t in (w["tag"] for w in want) if t not in cur]
# proxy 垫底：WARP 全挂时仍有直连兜底
if "proxy" in cur:
    cur = [t for t in cur if t != "proxy"] + new + ["proxy"]
else:
    cur = cur + new
sel["outbounds"] = cur

cfg_p.write_text(json.dumps(cfg, indent=2))
print(f"新增出口 {len(added)} 条，selector 现有 {len(cur)} 项")
