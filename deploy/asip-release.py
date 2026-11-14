#!/usr/bin/env python3
"""asip 版本化发布内核 —— 版本号 + 校验 + 可回滚 + 状态迁移器骨架（T5/W1 最小内核）。

WHY 本文件存在：
现状升级 = 覆盖工作区 + systemctl restart（本机「直接改文件 + restart」，远端
remote-deploy.sh `git reset --hard` + restart），无版本、无回滚。16k 行模块
拆分是全方案最大手术，无回滚 = 回归即事故；状态权威「切读」这一不可逆点
同样需要回退路径。本内核只做四件事，不做成重型发布系统：
  版本号   release   一次发布 = 一个不可变快照（manifest + 文件树 + 校验和）
  校验     verify    工作区漂移检测 + 快照自身完整性校验（双向）
  可回滚   rollback  按快照恢复版本域文件；每次发布独立可回退
  迁移器   migrate   状态迁移骨架（up/down），P3 落地 SQLite 迁移时在此登记

版本域（什么进快照）：git 跟踪的文本文件（代码 + deploy/ + tools/ + docs/）。
二进制（sing-box 等 >1MB 或非文本）只记校验和不入树（体积与部署语义都不同，
fetch-binaries.sh 负责补齐）。**不进版本域**：.env、.warp/、密钥、运行时状态
文件（.bad_nodes.json / *_ip_used 等）——回滚代码绝不连带回滚状态，状态的
回退走「库导出 JSON 投影→冻结→回读」（deploy/state-projection.py，P3 接线）。

快照存放：$ASIP_RELEASE_ROOT（默认 ~/.asip/releases）。放工作区之外是有意的：
工作区会被 git clean / reset / 重建卷掉，跟工作区同生共死的快照不叫回滚点。

失败语义（自审六问对应）：
  发布中断     <id>.partial 临时目录 + 原子 rename；半个发布不会出现在版本列表
  版本文件损坏 verify/rollback 先验 MANIFEST.sha256，损坏即拒、工作区不动
  回滚中途失败 逐文件原子替换（临时文件 + os.replace），失败即停并列出已恢复/
               未恢复清单；回滚本身幂等，可重跑同一目标

用法：
  python3 deploy/asip-release.py release [--note 文本]
  python3 deploy/asip-release.py verify [版本号]
  python3 deploy/asip-release.py rollback <版本号> [--dry-run] [--with-migrations]
  python3 deploy/asip-release.py status
  python3 deploy/asip-release.py migrate status|up|down [--to N]
退出码：0 成功；1 校验/回滚失败或有漂移；2 用法/输入错误。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MANIFEST_SCHEMA = "asip-release/1"
COPY_SIZE_LIMIT = 1_000_000  # 超过 1MB 视同二进制/大文件：只记校验和不入快照树


def project_root():
    return Path(os.environ.get("ASIP_PROJECT_DIR")
                or Path(__file__).resolve().parent.parent)


def release_root():
    root = os.environ.get("ASIP_RELEASE_ROOT")
    if root:
        return Path(root)
    return Path.home() / ".asip" / "releases"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_textish(path):
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    return b"\0" not in head


def git_info(root):
    try:
        rev = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        if rev.returncode != 0:
            return {"rev": "nogit", "short": "nogit", "dirty": None}
        short = rev.stdout.strip()[:12]
        dirty = bool(subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=60).stdout.strip())
        return {"rev": rev.stdout.strip(), "short": short, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"rev": "nogit", "short": "nogit", "dirty": None}


def tracked_files(root):
    proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                          capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise SystemExit("✗ 不是 git 仓库或 git 不可用——release 需要 git ls-files 定义版本域")
    return [p.decode("utf-8") for p in proc.stdout.split(b"\0") if p]


def untracked_code_warnings(root, tracked):
    """未纳入版本域的代码文件（git 未跟踪）——只告警不快照，防「以为已版本化」。"""
    tracked_set = set(tracked)
    warn = []
    for base in ("", "asip", "deploy", "tools"):
        d = root / base if base else root
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            rel = str(f.relative_to(root))
            if rel in tracked_set or not f.is_file():
                continue
            if f.suffix in (".py", ".sh"):
                warn.append(rel)
    return warn


def build_manifest(root):
    files = {}
    for rel in tracked_files(root):
        path = root / rel
        if not path.exists():
            files[rel] = {"sha256": None, "size": None, "stored": False, "missing": True}
            continue
        size = path.stat().st_size
        digest = sha256_file(path)
        stored = size <= COPY_SIZE_LIMIT and is_textish(path)
        files[rel] = {"sha256": digest, "size": size, "stored": stored}
    git = git_info(root)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    vid = f"{ts}-{git['short']}" + ("-dirty" if git.get("dirty") else "")
    return vid, {
        "schema": MANIFEST_SCHEMA,
        "id": vid,
        "created_at": ts,
        "git": git,
        "project_dir": str(root),
        "files": files,
        "untracked_code_warning": untracked_code_warnings(root, list(files)),
    }


# ---------------- migrate：状态迁移器骨架（P3 在此登记真实迁移） ----------------
# 迁移文件：deploy/migrations/NNN_名字.py，必须定义 up() 与 down()。
# 应用台账：$ASIP_RELEASE_ROOT/migrations.json（按序记录已应用 id）。
# 语义：up 按文件名字典序、逐个应用逐个记账（中途崩溃只记到已成功的）；
#       down 逆序回退。数据迁移本身由迁移文件负责（P3 的 SQLite schema 一等公民）。

def migrations_dir(root):
    return root / "deploy" / "migrations"


def migration_files(root):
    d = migrations_dir(root)
    if not d.is_dir():
        return []
    return sorted(f for f in d.iterdir()
                  if f.suffix == ".py" and not f.name.startswith("_"))


def applied_state():
    path = release_root() / "migrations.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("applied", [])


def record_applied(applied):
    root = release_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "migrations.json"
    fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".migrations.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"applied": applied, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_migration(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not (hasattr(mod, "up") and hasattr(mod, "down")):
        raise SystemExit(f"✗ 迁移文件缺 up()/down()：{path.name}")
    return mod


def migrate_cmd(args):
    root = project_root()
    all_names = [f.stem for f in migration_files(root)]
    applied = applied_state()
    if args.migrate_cmd == "status":
        print(f"已应用 {len(applied)} / 共 {len(all_names)}")
        for name in all_names:
            mark = "✓" if name in applied else "·"
            print(f"  {mark} {name}")
        return 0
    if args.migrate_cmd == "up":
        pending = [n for n in all_names if n not in applied]
        if not pending:
            print("无待应用迁移")
            return 0
        for name in pending:
            mod = load_migration(migrations_dir(root) / f"{name}.py")
            print(f"→ up {name}")
            mod.up()
            applied.append(name)
            record_applied(applied)  # 逐个记账：崩溃后重跑从断点续
        print(f"已应用 {len(pending)} 个迁移")
        return 0
    if args.migrate_cmd == "down":
        target = args.to if args.to is not None else len(applied) - 1
        if target < 0 or target > len(applied):
            print(f"✗ --to 超界（0..{len(applied)}）", file=sys.stderr)
            return 2
        while len(applied) > target:
            name = applied[-1]
            mod = load_migration(migrations_dir(root) / f"{name}.py")
            print(f"← down {name}")
            mod.down()
            applied.pop()
            record_applied(applied)
        print(f"回退至 {target} 个已应用迁移")
        return 0
    return 2


# ---------------- release / verify / rollback ----------------

def cmd_release(args):
    root = project_root()
    rr = release_root()
    vid, manifest = build_manifest(root)
    if args.note:
        manifest["note"] = args.note
    partial = rr / (vid + ".partial")
    final = rr / vid
    if final.exists():
        print(f"✗ 版本目录已存在：{final}", file=sys.stderr)
        return 2
    if partial.exists():
        shutil.rmtree(partial)
    (partial / "tree").mkdir(parents=True)
    # 1) 快照文件树
    for rel, meta in manifest["files"].items():
        if not meta.get("stored"):
            continue
        src = root / rel
        dst = partial / "tree" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    # 2) manifest + 总校验和
    with open(partial / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    sums = []
    for f in sorted((partial / "tree").rglob("*")):
        if f.is_file():
            rel = f.relative_to(partial / "tree")
            sums.append(f"{sha256_file(f)}  {rel}")
    (partial / "MANIFEST.sha256").write_text("\n".join(sums) + "\n", encoding="utf-8")
    # 3) 原子出台 + 拨 CURRENT
    os.replace(partial, final)
    cur = rr / "CURRENT"
    fd, tmp = tempfile.mkstemp(dir=str(rr), prefix=".CURRENT.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(vid + "\n")
    os.replace(tmp, cur)
    stored = sum(1 for m in manifest["files"].values() if m.get("stored"))
    print(f"✓ 发布 {vid}")
    print(f"  版本域 {len(manifest['files'])} 个文件（快照树 {stored} 个，"
          f"二进制/大文件只记校验和 {len(manifest['files']) - stored} 个）")
    print(f"  git {manifest['git']['short']} dirty={manifest['git']['dirty']}")
    if manifest["untracked_code_warning"]:
        print(f"  ⚠ 未跟踪代码未纳入版本域（{len(manifest['untracked_code_warning'])} 个）："
              + ", ".join(manifest["untracked_code_warning"][:8]))
    print(f"  快照: {final}")
    return 0


def load_release(rr, vid):
    d = rr / vid
    mp = d / "manifest.json"
    if not mp.exists():
        raise SystemExit(f"✗ 找不到版本 {vid}（{mp} 不存在）")
    try:
        with open(mp, encoding="utf-8") as f:
            manifest = json.load(f)
    except ValueError as e:
        raise SystemExit(f"✗ 版本 {vid} 的 manifest.json 损坏：{e}")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise SystemExit(f"✗ 版本 {vid} schema 不识别：{manifest.get('schema')}")
    return d, manifest


def verify_snapshot_integrity(d):
    """快照树自身完整性：MANIFEST.sha256 逐条复算。返回问题列表。"""
    problems = []
    sums_file = d / "MANIFEST.sha256"
    if not sums_file.exists():
        return [f"{d.name}: 缺 MANIFEST.sha256"]
    for line in sums_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, rel = line.partition("  ")
        path = d / "tree" / rel
        if not path.exists():
            problems.append(f"{d.name}: 快照缺文件 {rel}")
        elif sha256_file(path) != digest:
            problems.append(f"{d.name}: 快照文件被改 {rel}")
    return problems


def cmd_verify(args):
    rr = release_root()
    vid = args.version or (rr / "CURRENT").read_text(encoding="utf-8").strip()
    d, manifest = load_release(rr, vid)
    problems = verify_snapshot_integrity(d)
    root = Path(manifest["project_dir"])
    drift = []
    for rel, meta in sorted(manifest["files"].items()):
        path = root / rel
        if meta.get("missing"):
            continue
        if not path.exists():
            drift.append(f"工作区缺失: {rel}")
            continue
        if sha256_file(path) != meta["sha256"]:
            drift.append(f"工作区已改: {rel}")
    if problems:
        print(f"✗ 快照 {vid} 自身损坏 {len(problems)} 处：")
        for p in problems:
            print("  " + p)
    if drift:
        print(f"✗ 工作区相对 {vid} 漂移 {len(drift)} 处：")
        for line in drift[:40]:
            print("  " + line)
        if len(drift) > 40:
            print(f"  … 其余 {len(drift) - 40} 条省略")
    if not problems and not drift:
        print(f"✓ 工作区与版本 {vid} 一致，快照完整")
        return 0
    return 1


def cmd_rollback(args):
    rr = release_root()
    vid = args.version
    d, manifest = load_release(rr, vid)
    problems = verify_snapshot_integrity(d)
    if problems:
        print(f"✗ 拒绝回滚：快照 {vid} 自身损坏（工作区未动）", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 1
    root = Path(manifest["project_dir"])
    restored, failed, skipped = [], [], []
    for rel, meta in sorted(manifest["files"].items()):
        if not meta.get("stored"):
            skipped.append(rel)
            continue
        src = d / "tree" / rel
        dst = root / rel
        if args.dry_run:
            restored.append(rel)
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(dst.parent or "."), prefix="." + dst.name + ".")
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
            restored.append(rel)
        except OSError as e:
            failed.append(f"{rel}: {e}")
    print(("演练：" if args.dry_run else "") + f"回滚 {vid} → {root}")
    print(f"  恢复 {len(restored)} 个文件；二进制/大文件不在回滚域 {len(skipped)} 个（记于 manifest，可对照校验）")
    if failed:
        print(f"✗ {len(failed)} 个文件恢复失败（已恢复的保持生效，可重跑本命令续作）：")
        for line in failed:
            print("  " + line)
        return 1
    # 迁移器状态差提示（真正执行需显式 --with-migrations）
    want = manifest.get("migrations_applied")
    cur = applied_state()
    if want is not None and want != cur:
        delta = f"迁移态不同：发布时 {len(want)} 个已应用，当前 {len(cur)} 个"
        if args.with_migrations:
            print(f"  → {delta}，按 --with-migrations 回退迁移至发布时状态")
            args.migrate_cmd = "down"
            args.to = len(want)
            migrate_cmd(args)
        else:
            print(f"  ⚠ {delta}（加 --with-migrations 才回退迁移；数据迁移见 deploy/migrations/）")
    if not args.dry_run:
        # 回滚后自检：恢复结果与 manifest 复对
        recheck = []
        for rel, meta in sorted(manifest["files"].items()):
            if meta.get("stored") and (root / rel).exists():
                if sha256_file(root / rel) != meta["sha256"]:
                    recheck.append(rel)
        if recheck:
            print(f"✗ 回滚后复校失败 {len(recheck)} 个：{', '.join(recheck[:10])}")
            return 1
        print("  ✓ 回滚后复校一致")
    print("  注意：版本域只含代码/脚本；.env/.warp/运行时状态不在其中。"
          "重启服务请人工执行（线上禁止自动 restart）。")
    return 0


def cmd_status(args):
    rr = release_root()
    if not rr.exists():
        print(f"（无发布记录：{rr}）")
        return 0
    cur_file = rr / "CURRENT"
    cur = cur_file.read_text(encoding="utf-8").strip() if cur_file.exists() else "(无)"
    print(f"CURRENT = {cur}")
    print(f"快照目录: {rr}")
    for d in sorted(rr.iterdir()):
        if not d.is_dir() or d.name.endswith(".partial"):
            continue
        mp = d / "manifest.json"
        if not mp.exists():
            print(f"  {d.name}  (manifest 缺失)")
            continue
        try:
            with open(mp, encoding="utf-8") as f:
                m = json.load(f)
            n = sum(1 for v in m["files"].values() if v.get("stored"))
            print(f"  {d.name}  文件 {len(m['files'])}（快照 {n}）"
                  f"  git={m['git']['short']} note={m.get('note', '')}")
        except ValueError:
            print(f"  {d.name}  (manifest 损坏)")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("release", help="发布：打版本号+快照+校验和")
    pr.add_argument("--note", default="")
    pv = sub.add_parser("verify", help="校验：工作区漂移 + 快照完整性")
    pv.add_argument("version", nargs="?", default=None)
    pb = sub.add_parser("rollback", help="回滚到指定版本")
    pb.add_argument("version")
    pb.add_argument("--dry-run", action="store_true")
    pb.add_argument("--with-migrations", action="store_true",
                    help="连同状态迁移回退到该发布时的迁移态")
    sub.add_parser("status", help="列出版本与 CURRENT")
    pm = sub.add_parser("migrate", help="状态迁移器骨架")
    pm.add_argument("migrate_cmd", choices=["status", "up", "down"])
    pm.add_argument("--to", type=int, default=None, help="down 目标：保留前 N 个已应用")
    args = p.parse_args()
    if args.cmd == "release":
        return cmd_release(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "rollback":
        return cmd_rollback(args)
    if args.cmd == "status":
        return cmd_status(args)
    return migrate_cmd(args)


if __name__ == "__main__":
    sys.exit(main())
