#!/usr/bin/env python3
"""可信子集运行通道：按分组标记、每组一个独立 pytest 进程跑测试。

用法（仓库根执行）：
    python3 tests/run_groups.py              # 全部组，各组独立进程
    python3 tests/run_groups.py --repeat 2   # 全量重复 2 遍（重复绿验收）
    python3 tests/run_groups.py --group warp --group slot
    python3 tests/run_groups.py --list

原作用（为什么存在）：「单独绿、全量红」的成因之一是全量互踩 —— 用例在
模块级往 daemon 打 monkey、进程级全局残留、sys.path 污染，跨文件传染。
pytest 单进程跑全量时这些污染源互相叠加（实证：test_rotate.py 被收集导入
即向 daemon 注入 monkey，43 个后续用例全红）。本入口把每个分组放进干净的
独立 python 进程（组间零共享），同一组可重复运行得到稳定红绿信号。

分组规则与 conftest.py 的 _GROUP_PREFIXES 一致（文件名前缀 test_<组名>_*，
标记由 conftest 自动打点），这里通过收集结果的模块名机械推导，无手工清单
漂移问题。默认开写入护栏（ASIP_TEST_WRITE_GUARD=block）：跑测试不许写
生产状态目录 —— 与 conftest 的写入审计钩子配套。
"""
import argparse
import os
import re
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 与 conftest._GROUP_PREFIXES 同规则（改一处须同步另一处）
GROUP_PREFIXES = {
    "warp": "warp", "slot": "slot", "rotate": "rotate", "gate": "gate",
    "config": "config", "daemon": "daemon", "asip": "asip",
    "dormant": "dormant", "tier": "tier", "h2": "h2", "egress": "egress",
    "probe": "probe", "channel": "channel", "api": "api", "audit": "audit",
    "state": "state", "ledger": "ledger", "self": "self", "node": "node",
    "subscription": "subscription", "reconcile": "reconcile",
    "rebind": "rebind", "demoted": "demoted", "trace": "trace",
    "portability": "portability", "vercel": "vercel", "night": "night",
    "opencode": "opencode", "proxy": "proxy", "auto": "auto", "block": "block",
    "429": "cooldown", "verify": "verify", "tools": "tools",
    "alerting": "alerting",
}

SUMMARY_RE = re.compile(r"(\d+) (passed|failed|error|skipped)")
FAILED_RE = re.compile(r"^FAILED (\S+)", re.MULTILINE)

# 预期红登记（③组「修前红」漂移验收，有意设计：三方裁决落地、文案对齐后转绿）。
# 与 conftest.EXPECTED_RED 同步维护。通道输出把「预期红」与「意外红」分开标注，
# 预期红不判通道失败——但绝不为了让数字好看去改测试或跳过（登记只做标注）。
# 2026-11-14：漂移修复已落地，登记项转绿后清空——回归必须以意外红亮出。
EXPECTED_RED = set()


def _group_of(mod_path: str) -> str:
    """mod_path 形如 'tests/contracts/test_x' 或 'test_y'（已去 .py）。

    与 conftest._group_of 同规则：tests/contracts/ 整树归 contracts 组，
    其余按文件名前缀。
    """
    if "contracts" in mod_path.replace("\\", "/").split("/"):
        return "contracts"
    base = mod_path.split("/")[-1]
    if not base.startswith("test_"):
        return "misc"
    return GROUP_PREFIXES.get(base[len("test_"):].split("_")[0], "misc")


def _collect_groups():
    """跑一次 --collect-only，从收集结果推导 组名 -> 文件集合。"""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=300)
    groups = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        mod = line.split("::", 1)[0]
        if not mod.endswith(".py"):
            continue
        mod = mod[:-3]
        groups.setdefault(_group_of(mod), set()).add(mod)
    if not groups:
        sys.exit("收集失败或无用例：\n" + r.stdout[-2000:] + r.stderr[-2000:])
    return groups


def _run_group(group, env, extra):
    cmd = [sys.executable, "-m", "pytest", "-m", group,
           "-p", "no:cacheprovider", "-q"] + extra
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True,
                       cwd=REPO_ROOT, env=env, timeout=1800)
    dt = time.time() - t0
    out = r.stdout or ""
    tail = out[-800:]
    counts = {k: int(n) for n, k in SUMMARY_RE.findall(tail)}
    failed_ids = set(FAILED_RE.findall(out))
    return r.returncode, counts, dt, tail, failed_ids


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--group", action="append", default=None,
                    help="只跑指定组（可重复），默认全部组")
    ap.add_argument("--repeat", type=int, default=1,
                    help="整套分组序列重复 N 遍（默认 1）")
    ap.add_argument("--list", action="store_true", help="只列出分组与规模")
    ap.add_argument("--no-write-guard", action="store_true",
                    help="关闭生产状态写入护栏（默认 block）")
    ap.add_argument("extra", nargs="*", help="透传给 pytest 的额外参数")
    args = ap.parse_args()

    groups = _collect_groups()
    order = sorted(groups, key=lambda g: (-len(groups[g]), g))
    if args.group:
        missing = [g for g in args.group if g not in groups]
        if missing:
            sys.exit("未知分组: %s（--list 查看）" % missing)
        order = [g for g in order if g in args.group]

    if args.list:
        for g in order:
            print("%-14s %3d 个文件" % (g, len(groups[g])))
        return

    env = dict(os.environ)
    if not args.no_write_guard:
        env["ASIP_TEST_WRITE_GUARD"] = "block"

    failed = []          # 意外红/异常（判通道失败）
    expected_seen = []   # 预期红（③组修前红，只标注不判失败）
    for rep in range(1, args.repeat + 1):
        if args.repeat > 1:
            print("════ 第 %d/%d 遍 ════" % (rep, args.repeat))
        for g in order:
            rc, counts, dt, tail, failed_ids = _run_group(g, env, args.extra)
            stat = " ".join("%s %s" % (n, k) for k, n in
                            sorted(counts.items())) or "无结果"
            unexpected = sorted(failed_ids - EXPECTED_RED)
            expected = sorted(failed_ids & EXPECTED_RED)
            if unexpected or (rc != 0 and not failed_ids):
                flag = "FAIL"      # 意外红，或无 FAILED 明细的异常（收集错等）
            elif expected:
                flag = "EXP-RED"   # 预期红：③组修前红，漂移修复后转绿
            elif rc != 0:
                flag = "FAIL"
            else:
                flag = "OK"
            print("[%s] %-14s %-38s %6.1fs" % (flag, g, stat, dt))
            for tid in expected:
                expected_seen.append((rep, tid))
                print("       预期红（③组修前红）: %s" % tid)
            if unexpected:
                failed.append((rep, g, unexpected))
                print(tail)
    if expected_seen:
        print("\n预期红 %d 例次（③组漂移验收，有意设计，非故障）：%s"
              % (len(expected_seen), sorted({t for _, t in expected_seen})))
    if failed:
        print("意外红/异常: %s" % failed)
        sys.exit(1)
    print("\n全部分组通过（%d 组 × %d 遍，进程级隔离；预期红已单列）"
          % (len(order), args.repeat))


if __name__ == "__main__":
    main()
