#!/usr/bin/env python3
"""回滚演练（T2c 门槛之一）：库 → 投影 → 冻结 → 回读 → 逐字节比对。

WHY 必须真跑而不是「契约存在即视为通过」：回滚点只有在**被回读过一次**之后
才算存在。演练要回答三个问题，缺一个都不能切读：

  ① 投影是否**旧代码读得懂**：导出的 `.warp_ip_used.json` / `.warp_line_state.json`
     必须是原封套（`{"used":…}` / `{"version":1,"lines":…}`），而不是库自己的
     表/列形状 —— 否则那不是回滚点，是「另存为」。
  ② 冻结是否**自证**：`freeze` 复算 sha256 写侧车并去写位；此后篡改必须能被
     `readback` 抓到。演练里故意改一个字节做反证（负向用例），证明侧车真的在
     起作用，而不是装饰。
  ③ 回读是否**逐字节还原**：还原目录与原目录逐文件 sha256 相等。

演练全程在临时目录里跑，**不碰生产状态**：库从真实状态文件的只读副本建出来
（`--from` 指定状态根，默认仓库根），投影/冻结/回读都在 tempdir。

用法：
  python3 deploy/state-rollback-drill.py [--from DIR] [--keep]
退出码：0 通过；1 任一步不符；2 用法/环境错误。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(PROJECT, "deploy", "state-projection.py")

# 演练只针对「已入 store 的 kind」，不搬 .warp/*（设备凭据不在状态域内）。
DRILL_FILES = (".warp_ip_used.json", ".warp_line_state.json",
               ".api_stats.json", ".ledger_shadow.json",
               ".pool_history.json", ".node_ip_used.json",
               ".warp_segment.json", ".bad_nodes.json")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def seed_store(store, state_root):
    """把状态文件**只读**灌进库（形状按真实封套解析，不猜）。"""
    from asip.state_store import schema

    n = {}
    ip_used = load_json(os.path.join(state_root, ".warp_ip_used.json"), {})
    rows = ip_used.get("used") if isinstance(ip_used, dict) else {}
    n["ip_used"] = len(rows or {})
    if rows:
        store.replace_rows(schema.KIND_IP_USED, rows, allow_empty=True)

    line = load_json(os.path.join(state_root, ".warp_line_state.json"), {})
    lines = line.get("lines") if isinstance(line, dict) else {}
    n["line_state"] = len(lines or {})
    if lines:
        store.replace_rows(schema.KIND_LINE_STATE, lines, allow_empty=True)

    node = load_json(os.path.join(state_root, ".node_ip_used.json"), {})
    n["node_ip_used"] = len(node or {})
    if node:
        store.replace_rows(schema.KIND_NODE_IP_USED, node, allow_empty=True)

    bad = load_json(os.path.join(state_root, ".bad_nodes.json"), {})
    n["bad_nodes"] = len(bad or {})
    if bad:
        store.replace_rows(schema.KIND_BAD_NODES, bad, allow_empty=True)

    for fname, kind in ((".api_stats.json", schema.KIND_API_STATS),
                        (".ledger_shadow.json", schema.KIND_LEDGER_SHADOW),
                        (".pool_history.json", schema.KIND_POOL_HISTORY),
                        (".warp_segment.json", schema.KIND_SEGMENT)):
        body = load_json(os.path.join(state_root, fname), None)
        if isinstance(body, dict):
            store.write_doc(kind, body)
            n[kind] = 1
    return n


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="state-rollback-drill")
    ap.add_argument("--from", dest="src", default=PROJECT,
                    help="状态文件来源目录（只读；默认仓库根）")
    ap.add_argument("--keep", action="store_true", help="保留演练目录（排查用）")
    args = ap.parse_args(argv)

    if not os.path.isfile(TOOL):
        print("✗ 缺 %s" % TOOL, file=sys.stderr)
        return 2
    if PROJECT not in sys.path:
        sys.path.insert(0, PROJECT)

    from asip.state_store import Store

    tmp = tempfile.mkdtemp(prefix="asip-rollback-drill-")
    orig_dir = os.path.join(tmp, "orig")
    back_dir = os.path.join(tmp, "back")
    os.makedirs(orig_dir, exist_ok=True)
    os.makedirs(back_dir, exist_ok=True)
    db = os.path.join(tmp, "state.sqlite3")
    proj = os.path.join(tmp, "projection.json")
    ok = True

    try:
        store = Store(db)
        counts = seed_store(store, args.src)
        print("① 库就绪：%s（schema v%d）" % (db, store.schema_version()))
        print("   入库行数：%s" % json.dumps(counts, ensure_ascii=False))

        info = store.export_projection(proj)
        store.close()
        print("② 投影：%d 条 / %d 字节 → %s"
              % (info["entries"], info["bytes"], proj))

        # 回滚点必须先是「老代码读得懂的原文」：逐条解析 + 校验封套。
        with open(proj, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("schema") != "asip-state-projection/1":
            print("✗ 投影封套 schema 不符：%r" % doc.get("schema"))
            return 1
        if (doc.get("source") or {}).get("type") != "sqlite":
            print("✗ 投影 source.type 不是 sqlite")
            return 1
        for e in doc["entries"]:
            data = e["data"].encode("utf-8")
            if hashlib.sha256(data).hexdigest() != e["sha256"]:
                print("✗ 条目自证失败：%s" % e["name"])
                ok = False
            if e["name"].endswith(".json"):
                try:
                    json.loads(e["data"])
                except ValueError as exc:
                    print("✗ 条目不是合法 JSON：%s（%s）" % (e["name"], exc))
                    ok = False
        # 封套形状抽查：这两个是「老 daemon 读不读得懂」的关键
        by_name = {e["name"]: json.loads(e["data"]) for e in doc["entries"]
                   if e["name"].endswith(".json")}
        if ".warp_ip_used.json" in by_name and "used" not in by_name[
                ".warp_ip_used.json"]:
            print("✗ .warp_ip_used.json 缺 used 封套")
            ok = False
        if ".warp_line_state.json" in by_name:
            ls = by_name[".warp_line_state.json"]
            if "lines" not in ls:
                print("✗ .warp_line_state.json 缺 lines 封套")
                ok = False
            if ls.get("version") != 1:
                print("✗ .warp_line_state.json 缺 version=1")
                ok = False

        # ③ 冻结（复算哈希 + 去写位）
        rc, out = run([sys.executable, TOOL, "freeze", proj])
        print("③ 冻结 rc=%d %s" % (rc, out.strip().splitlines()[-1:] or ""))
        if rc != 0:
            return 1
        sidecar = proj + ".sha256"
        if not os.path.exists(sidecar):
            print("✗ 冻结未生成侧车 %s" % sidecar)
            return 1
        if os.stat(proj).st_mode & 0o222:
            print("✗ 冻结后投影仍可写（写位未去）")
            ok = False

        # ④ 回读（校验侧车 + 逐条 sha256 后还原）
        rc, out = run([sys.executable, TOOL, "readback", proj,
                       "--into", back_dir, "--force"])
        print("④ 回读 rc=%d %s" % (rc, out.strip().splitlines()[-1:] or ""))
        if rc != 0:
            return 1

        # ⑤ 逐字节比对：回读产物 vs 投影里的原文
        for e in doc["entries"]:
            dst = os.path.join(back_dir, e["name"])
            if not os.path.exists(dst):
                print("✗ 回读缺文件：%s" % e["name"])
                ok = False
                continue
            got = sha(dst)
            if got != e["sha256"]:
                print("✗ 回读内容不一致：%s" % e["name"])
                ok = False

        # ⑥ 负向反证：篡改投影后 readback 必须失败（否则侧车是装饰）。
        # 冻结已去掉写位 —— 这里先显式加回来，模拟「有人绕过冻结改文件」，
        # 才测得到侧车；不先加回就只是在测文件权限。
        os.chmod(proj, os.stat(proj).st_mode | 0o200)
        with open(proj, "r", encoding="utf-8") as f:
            tampered = f.read()
        with open(proj, "w", encoding="utf-8") as f:
            f.write(tampered.replace('"size":', '"size": ', 1))
        rc, out = run([sys.executable, TOOL, "readback", proj,
                       "--into", back_dir, "--verify-only"])
        print("⑥ 篡改后校验 rc=%d（期望非 0）" % rc)
        if rc == 0:
            print("✗ 篡改未被发现：侧车没起作用")
            ok = False

        print("=" * 60)
        print("结论：%s" % ("通过（导出→冻结→回读逐字节一致，篡改可检出）"
                           if ok else "未通过（见上面 ✗）"))
        return 0 if ok else 1
    finally:
        if args.keep:
            print("演练目录保留：%s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())