#!/usr/bin/env python3
"""影子对账取证（T2b）：真实台账 → 库 → diff。**只读生产文件。**

为什么要单独一个取证脚本而不是只看 daemon 日志：切读门槛是「影子差异=0 持续
一个观察窗」，这个结论必须能**重放**。本脚本把生产状态文件（只读）同时喂给
两条独立路径——

  路径 A：daemon 自己的载入器（`_load_warp_ip_used` / `_load_warp_line_state` …），
          得到「daemon 重启后会拿到的内存态」；
  路径 B：经 `asip.state_store` 入库再读回，得到「库投影后的内存态」。

两条路径都从同一批文件出发、走**两套不同代码**，diff=0 才说明投影没有语义
漂移。这与 daemon 运行中的影子 diff 是同一个判据（同一个 `shadow.diff_rows`），
区别只是前者可以随时重跑、后者要等观察窗。

用法：
  python3 deploy/state-shadow-drill.py [--state-root DIR] [--json]
退出码：0 差异=0；1 有差异（打印差异摘要）；2 环境错误。
"""
import argparse
import json
import os
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 只覆盖「daemon 单写者」的 kind；bad_nodes 是共享写者（SHARED），不进 diff。
KINDS = ("ip_used", "line_state", "node_ip_used")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="state-shadow-drill")
    ap.add_argument("--state-root", default=PROJECT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if PROJECT not in sys.path:
        sys.path.insert(0, PROJECT)
    os.environ.setdefault("ASIP_NO_SIDE_EFFECTS", "1")   # 绝不写生产文件

    root = args.state_root
    # 环境变量先把 daemon 的路径助手钉到只读来源（与 conftest 的同款机制，
    # 但这里指向生产文件本身 —— 只读，不写）。
    for env, fname in (("WARP_IP_USED_FILE_PATH", ".warp_ip_used.json"),
                       ("WARP_LINE_STATE_FILE_PATH", ".warp_line_state.json"),
                       ("NODE_IP_USED_FILE_PATH", ".node_ip_used.json")):
        os.environ[env] = os.path.join(root, fname)

    import daemon as d
    from asip.state_store import schema, shadow
    from asip.state_store.store import Store

    tmp = tempfile.mkdtemp(prefix="asip-shadow-drill-")
    db = os.path.join(tmp, "state.sqlite3")
    store = Store(db)
    try:
        # ── 路径 A：daemon 载入器（生产代码，只读） ────────────────
        d._load_warp_ip_used()
        d._load_warp_line_state()
        d._load_node_ip_used()
        mem = {
            schema.KIND_IP_USED: {str(k): dict(v) for k, v in d._WARP_IP_USED.items()},
            schema.KIND_LINE_STATE: {t: dict(s) for t, s in d._WARP_LINE_STATE.items()},
            schema.KIND_NODE_IP_USED: dict(d._NODE_IP_USED),
        }

        # ── 路径 B：入库再读回（库的读语义 = 白名单过滤后的行） ──────
        store.replace_rows(schema.KIND_IP_USED, mem[schema.KIND_IP_USED],
                           allow_empty=True)
        store.replace_rows(schema.KIND_LINE_STATE, mem[schema.KIND_LINE_STATE],
                           allow_empty=True)
        store.replace_rows(schema.KIND_NODE_IP_USED, mem[schema.KIND_NODE_IP_USED],
                           allow_empty=True)

        reports = []
        for kind in (schema.KIND_IP_USED, schema.KIND_LINE_STATE,
                     schema.KIND_NODE_IP_USED):
            rep = shadow.diff_rows(kind, mem[kind], store.read_rows(kind))
            reports.append(rep)
    finally:
        store.close()

    total = sum(r["diffs"] for r in reports)
    if args.json:
        print(json.dumps({"total_diffs": total, "kinds": reports},
                         ensure_ascii=False, indent=1))
    else:
        print("影子对账（daemon 载入器 vs 库读回，同一批真实文件）")
        for r in reports:
            print("  %-14s 行数=%-6d 差异=%-4d only_memory=%d only_db=%d changed=%d"
                  % (r["kind"], r["checked"], r["diffs"],
                     len(r["only_memory"]), len(r["only_db"]),
                     len(r["changed"])))
            for c in r["changed"][:5]:
                print("    · %s %s" % (c["key"], str(c["fields"])[:160]))
            for k in r["only_memory"][:5]:
                print("    · 仅内存有：%s" % k)
            for k in r["only_db"][:5]:
                print("    · 仅库有：%s" % k)
        print("=" * 60)
        print("结论：差异合计 %d（0 = 投影无语义漂移）" % total)
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())