#!/usr/bin/env python3
"""状态导出投影工具 —— 「导出投影 → 冻结 → 回读」三步的最小实现（T5/W1，供 P3 接线）。

WHY：阶段 2「状态权威切读」是不可逆点（停读 JSON），回滚路径 = 库导出 JSON
投影 → 冻结 → 回读。本工具先把**契约形状**钉死：投影文件的封套、校验语义、
三步的调用约定。P3 落地 SQLite 后在本工具加 `--source sqlite:<db>` 数据源，
封套形状不变——这就是「接口先行、接线后至」的预留方式。

三步语义：
  导出投影 export   把状态文件（JSON/JSONL）读出 → 单个 JSON 投影文件，
                    内含每条目的 sha256（内容自证）与封套元数据
  冻结     freeze   复算自证哈希后写 <投影>.sha256 侧车 + 去写位；
                    此后任何篡改都可被发现（回滚点必须不可变）
  回读     readback 校验侧车与逐条 sha256 后还原到目标目录（--verify-only 只验不写）

默认状态文件集（与 .gitignore「运行时状态文件」段一致，取值口径=文件实际路径）：
  .bad_nodes.json .monitor_state.json .egress_log.jsonl
  .warp_ip_used.json .warp_ip_cycles.json .warp_ip_history.jsonl
  .warp_line_state.json .warp_proxy_fallback.json .opencode_gate_ledger.jsonl
  .warp/*.json .warp/h2/*.json
glob 按**路径段**匹配（.warp/*.json 只命中直接子级）——设备凭据
（.warp/*/config.json、wglines/*/singbox.json）不在状态域，不进投影。
可用 --include 追加（可重复），--no-default 只用 --include。

用法：
  python3 deploy/state-projection.py export --state-root DIR --out FILE
      [--include GLOB ...] [--no-default]
  python3 deploy/state-projection.py freeze FILE
  python3 deploy/state-projection.py readback FILE --into DIR [--verify-only] [--force]
退出码：0 成功；1 校验失败/缺文件；2 用法错误。
"""
import argparse
import fnmatch
import hashlib
import json
import os
import stat
import sys
import tempfile
import time

SCHEMA = "asip-state-projection/1"

# 与 .gitignore 运行时状态段同口径；改这里之前先对 .gitignore 复核。
DEFAULT_INCLUDES = [
    ".bad_nodes.json",
    ".monitor_state.json",
    ".egress_log.jsonl",
    ".warp_ip_used.json",
    ".warp_ip_cycles.json",
    ".warp_ip_history.jsonl",
    ".warp_line_state.json",
    ".warp_proxy_fallback.json",
    ".opencode_gate_ledger.jsonl",
    ".warp/*.json",
    ".warp/h2/*.json",
]


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def glob_match(rel, pattern):
    """按路径段匹配：fnmatch 的 * 会跨 / 吞掉子目录，.warp/*.json 必须只命中
    直接子级——否则设备凭据（.warp/usqueN/config.json 等）会被误纳入状态投影。"""
    r_parts, p_parts = rel.split("/"), pattern.split("/")
    if len(r_parts) != len(p_parts):
        return False
    return all(fnmatch.fnmatch(r, p) for r, p in zip(r_parts, p_parts))


def collect(state_root, includes):
    """按 glob 收集状态文件；返回 [(相对名, Path)]，按名排序保证投影稳定有序。"""
    found = {}
    for pattern in includes:
        for dirpath, dirnames, filenames in os.walk(state_root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, state_root).replace(os.sep, "/")
                if glob_match(rel, pattern):
                    found[rel] = full
    return [(rel, found[rel]) for rel in sorted(found)]


def cmd_export(args):
    if not os.path.isdir(args.state_root):
        print(f"✗ 状态根不存在：{args.state_root}", file=sys.stderr)
        return 2
    includes = ([] if args.no_default else list(DEFAULT_INCLUDES)) + list(args.include or [])
    if not includes:
        print("✗ 没有任何包含模式（--no-default 且未给 --include）", file=sys.stderr)
        return 2
    entries = []
    for rel, full in collect(args.state_root, includes):
        with open(full, "rb") as f:
            data = f.read()
        entries.append({
            "name": rel,
            "sha256": sha256_bytes(data),
            "size": len(data),
            "mtime": os.stat(full).st_mtime,
            "data": data.decode("utf-8", errors="surrogateescape"),
        })
    projection = {
        "schema": SCHEMA,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {"type": "json-files", "root": os.path.abspath(args.state_root)},
        "includes": includes,
        "entries": entries,
    }
    out = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(out)) or ".",
                               prefix=".projection.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(projection, f, ensure_ascii=False)
    os.replace(tmp, out)
    total = sum(e["size"] for e in entries)
    print(f"✓ 投影已导出：{out}")
    print(f"  条目 {len(entries)} 个，原始字节 {total}")
    for e in entries:
        print(f"  {e['name']}  {e['size']}B  {e['sha256'][:12]}")
    return 0


def load_projection(path):
    try:
        with open(path, encoding="utf-8") as f:
            p = json.load(f)
    except (OSError, ValueError) as e:
        print(f"✗ 投影读取失败：{e}", file=sys.stderr)
        return None
    if p.get("schema") != SCHEMA:
        print(f"✗ 投影 schema 不识别：{p.get('schema')}", file=sys.stderr)
        return None
    return p


def verify_projection(path, p):
    """封套自证：逐条 data 的 sha256 与记录一致。返回问题列表。"""
    problems = []
    for e in p.get("entries", []):
        data = e.get("data", "").encode("utf-8", errors="surrogateescape")
        if sha256_bytes(data) != e.get("sha256"):
            problems.append(f"条目哈希不符: {e.get('name')}")
        if len(data) != e.get("size"):
            problems.append(f"条目字节数不符: {e.get('name')}")
    return problems


def cmd_freeze(args):
    p = load_projection(args.file)
    if p is None:
        return 1
    problems = verify_projection(args.file, p)
    if problems:
        print("✗ 投影自身不一致，拒绝冻结：", file=sys.stderr)
        for line in problems:
            print("  " + line, file=sys.stderr)
        return 1
    digest = sha256_bytes(open(args.file, "rb").read())
    sidecar = args.file + ".sha256"
    with open(sidecar, "w", encoding="utf-8") as f:
        f.write(f"{digest}  {os.path.basename(args.file)}\n")
    os.chmod(args.file, os.stat(args.file).st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
    os.chmod(sidecar, os.stat(sidecar).st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
    print(f"✓ 已冻结：{args.file}")
    print(f"  侧车 {sidecar}  sha256={digest[:12]}…  文件已去写位")
    return 0


def cmd_readback(args):
    p = load_projection(args.file)
    if p is None:
        return 1
    problems = verify_projection(args.file, p)
    sidecar = args.file + ".sha256"
    if os.path.exists(sidecar):
        want = sidecar and open(sidecar, encoding="utf-8").read().split()[0]
        have = sha256_bytes(open(args.file, "rb").read())
        if want != have:
            problems.append(f"侧车哈希不符（投影文件在冻结后被改）：{sidecar}")
    else:
        print("  ⚠ 无 .sha256 侧车（未 freeze），只做内容自证")
    if problems:
        print("✗ 回读前校验失败（目标目录未动）：", file=sys.stderr)
        for line in problems:
            print("  " + line, file=sys.stderr)
        return 1
    if args.verify_only:
        print(f"✓ 回读校验通过（{len(p['entries'])} 条目），未落盘")
        return 0
    os.makedirs(args.into, exist_ok=True)
    restored = 0
    for e in p["entries"]:
        rel = e["name"]
        if rel.startswith("/") or ".." in rel.split("/"):
            print(f"✗ 条目名不安全，拒绝回读：{rel}", file=sys.stderr)
            return 1
        dst = os.path.join(args.into, rel.replace("/", os.sep))
        if os.path.exists(dst) and not args.force:
            print(f"✗ 目标已存在（--force 才覆盖）：{dst}", file=sys.stderr)
            return 1
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        data = e["data"].encode("utf-8", errors="surrogateescape")
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst) or ".", prefix="." + os.path.basename(dst) + ".")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
        restored += 1
    print(f"✓ 回读完成：{restored} 个条目 → {args.into}")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("export", help="导出投影（只读状态根）")
    pe.add_argument("--state-root", required=True)
    pe.add_argument("--out", required=True)
    pe.add_argument("--include", action="append", default=[])
    pe.add_argument("--no-default", action="store_true")
    pf = sub.add_parser("freeze", help="冻结投影（自证+侧车+去写位）")
    pf.add_argument("file")
    pr = sub.add_parser("readback", help="回读投影到目标目录")
    pr.add_argument("file")
    pr.add_argument("--into", default=None, help="目标目录（--verify-only 时可省）")
    pr.add_argument("--verify-only", action="store_true")
    pr.add_argument("--force", action="store_true")
    args = p.parse_args()
    if args.cmd == "export":
        return cmd_export(args)
    if args.cmd == "freeze":
        return cmd_freeze(args)
    if not args.verify_only and not args.into:
        p.error("readback 落盘需要 --into（--verify-only 可不带）")
    return cmd_readback(args)


if __name__ == "__main__":
    sys.exit(main())
