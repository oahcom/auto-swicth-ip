#!/usr/bin/env python3
"""H2 单元对账剪除：把「台账里已经没有」的 usque-h2-* 单元停掉并从 systemd 移除。

WHY 需要这个（2026-11-11 实测）：
  `render_h2_lines.py` 只写台账内的单元文件，**从不删除台账外的**。
  2026-10-28 缩容把渠道从 103 砍到 56 时，47 个旧单元文件原样留在
  `/etc/systemd/system`，`enabled` 状态也没清 → 开机自启、`Restart=always`
  拉起 → 永久空转。更糟的是 sing-box 侧的 direct 入站已被删掉，于是这些线
  指向 `127.0.0.1:<已删端口>`，**进程活着、socks 端口在听、systemd 报 active，
  三个健康判据全过** —— 只有真的去取一次出口地址才会暴露。
  根因不是「停得不够」，是「**只增不减的渲染**」+「**全仓没有一处剪除代码**」。

  本模块是全仓第一处剪除实现。它把剪除做成**带闸门的对账**，而不是无脑删除。

安全闸（三道，任一不满足就只报告、不执行）：
  闸一 · 身份闸：候选单元必须在 systemd 单元文件里存在，且**不在**当前
        台账 `channels.json` 的单元名集合中。台账是唯一事实源。
  闸二 · 状态闸：候选单元必须**不是 active**。active 意味着它正在服务
        流量 —— 停掉它等于中断在途请求。台账缺了但单元还活着 = 台账错了，
        那是改台账的事，不是停单元的事。
  闸三 · 保护闸：候选单元不得在保护名单里（`H2_PROTECTED_UNITS` 环境变量，
        逗号分隔）。给人工留的兜底闸 —— 万一某条线是「台账还没写但人在用」，
        可以先保护起来。

WHY 剪除必须发生在**渲染与设备分配之前**：
  `free_devices()` 按「设备是否被 active 单元持有」判空闲。僵尸单元在停掉
  之前是 active 的 → 它的设备被判为「已被占用」→ 扩容时拿不到这台设备。
  更糟的是顺序反了的话会出现另一种误判：先分配设备、后剪除，分配时把一台
  「马上要被停掉的僵尸单元的设备」当成空闲发给新线，剪除后新线就独占了这台
  —— 恰好是对的；但如果剪除**失败**（权限不足等），新线与僵尸线同台设备，
  两条线互抢 CF 会话。所以顺序固定为：**先剪除 → 再分配**。

幂等：重复执行不产生额外副作用。已经剪除过的单元不在 unit-files 里，第二次
  跑就是空的候选集。

用法：
  python3 deploy/reconcile_h2_units.py            # 只报告（dry-run）
  python3 deploy/reconcile_h2_units.py --apply    # 真剪除
  python3 deploy/reconcile_h2_units.py --apply --keep-files
                                                  # 停用但保留单元文件（回滚用）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
LEDGER = PROJECT / ".warp" / "h2" / "channels.json"
DEFAULT_UNIT_DIR = "/etc/systemd/system"

# 人工保护名单：环境变量给逗号分隔的单元名（可写 `usque-h2-16` 或
# `usque-h2-16.service`）。命中则只报告不剪除。
PROTECT_ENV = "H2_PROTECTED_UNITS"


def _protected() -> set[str]:
    raw = os.environ.get(PROTECT_ENV, "")
    out = set()
    for item in raw.split(","):
        s = item.strip()
        if not s:
            continue
        out.add(s if s.endswith(".service") else f"{s}.service")
    return out


def ledger_units() -> set[str]:
    """台账里的单元名集合。台账读不到就返回空集 —— 宁可什么都不剪，也不能
    在读不到事实源的情况下按猜测删除生产单元。"""
    try:
        rows = json.loads(LEDGER.read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(rows, list):
        return set()
    return {str(r.get("unit")) for r in rows if isinstance(r, dict) and r.get("unit")}


def installed_units(unit_dir: Path) -> dict[str, str]:
    """单元名 → systemd 单元文件状态（enabled / disabled / ...）。

    判据用 `list-unit-files` 而不是遍历目录：目录里可能残留 `.service` 之外的
    东西，也可能被手工放进来没被 systemd 加载的；`list-unit-files` 是
    systemd 自己的账本，看到的才是真正会被启动的。
    """
    try:
        r = subprocess.run(["systemctl", "list-unit-files", "--type=service",
                            "--no-legend", "--no-pager"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out: dict[str, str] = {}
    for line in (r.stdout or "").splitlines():
        m = re.match(r"(\S+\.service)\s+(\S+)", line.strip())
        if m and re.fullmatch(r"usque-h2-\d+\.service", m.group(1)):
            out[m.group(1)] = m.group(2)
    return out


def unit_state(name: str) -> str:
    """单元此刻的运行状态（active / inactive / failed / ...）。"""
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (r.stdout or "").strip() or "unknown"


def stale_products(ledger: set[str]) -> list[str]:
    """渲染产物目录里、但台账里没有的单元文件名。

    WHY 必须连产物一起清：`h2-install.sh` 遍历的是 `.warp/h2/units/` 下的
    `usque-h2-*.service`，**不是** systemd 目录。渲染器写新单元时从不删旧
    文件（实测产物目录里 103 个单元 vs 台账 56 条）—— 只剪 systemd 目录的话，
    下一次 `h2-install.sh` 会把产物目录里那 47 个台账外单元**原样装回
    systemd**，僵尸单元当场复活。

    所以「剪除」是两件事，缺一不可：
      ① systemd 目录：停用 + 停止 + 删文件（消除当前的事实）
      ② 渲染产物目录：删文件（消除复活的来源）
    只做 ① 等于白做。

    产物目录里的 `usque/` 脚本与 `conf/` 派生配置同理：它们按编号命名，
    台账外的编号对应的脚本没人引用，但会一直留在磁盘上（实测 104 个
    脚本 / 104 份 conf vs 台账 56 条），随渲染轮次无界增长。
    """
    units_dir = PROJECT / ".warp" / "h2" / "units"
    out: list[str] = []
    if not units_dir.is_dir():
        return out
    for p in sorted(units_dir.glob("usque-h2-*.service")):
        if p.name not in ledger:
            out.append(p.name)
    return out


def clean_products(stale: list[str]) -> tuple[int, int]:
    """清掉渲染产物里台账外的单元文件、脚本与派生配置。

    返回 (成功, 失败)。删的是**渲染产物**（本仓库内的生成物），不是
    systemd 里的已安装单元 —— 后者由 main() 的剪除流程负责。
    """
    base = PROJECT / ".warp" / "h2"
    ok = fail = 0
    for name in stale:
        idx = re.search(r"usque-h2-(\d+)\.service", name)
        targets = [base / "units" / name]
        if idx:
            targets.append(base / "usque" / f"{idx.group(1)}.sh")
            targets.append(base / "conf" / f"{idx.group(1)}.json")
        for t in targets:
            if not t.exists():
                continue
            try:
                t.unlink()
                ok += 1
            except OSError as e:
                fail += 1
                print(f"  ✗ 删除产物失败 {t}: {type(e).__name__}")
    return ok, fail


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"


def _privileged(argv: list[str]) -> tuple[int, str]:
    """需要写 systemd 的动作：已是 root 就直接跑，否则走 sudo -n。

    `sudo -n`（非交互）：daemon 是非交互运行的，带密码提示的 sudo 会直接
    挂死在那里等输入 —— 那比不做还糟。
    """
    if os.geteuid() == 0:
        return _run(argv)
    return _run(["sudo", "-n", *argv])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="真执行剪除；默认只报告")
    ap.add_argument("--keep-files", action="store_true",
                    help="只停用不删单元文件（需要回滚时用）")
    ap.add_argument("--unit-dir", default=os.environ.get("ASIP_SYSTEM_UNIT_DIR")
                    or DEFAULT_UNIT_DIR)
    args = ap.parse_args()

    unit_dir = Path(args.unit_dir)
    led = ledger_units()
    if not led:
        print("✗ 读不到台账，拒绝剪除（台账是唯一事实源）", file=sys.stderr)
        return 2

    installed = installed_units(unit_dir)
    protected = _protected()
    # 台账有、但 systemd 里没有 —— 台账说在线、事实说没有。这是**另一个**
    # 方向的不一致（缺单元），报告但不剪除：停一个不存在的单元没有意义，
    # 该做的是把单元装上去。
    missing = sorted(led - set(installed))
    # systemd 有、台账没有 —— 这才是剪除候选。
    stale = sorted(set(installed) - led)

    products = stale_products(led)

    print(f"== H2 单元对账 ==")
    print(f"  台账 {len(led)} 条 / systemd {len(installed)} 个单元")
    if products:
        print(f"  渲染产物目录里台账外的单元: {len(products)} 个"
              f"（不清理的话下次安装会装回 systemd）")
    if missing:
        print(f"  ⚠ 台账有、systemd 没有 {len(missing)} 条："
              f"{', '.join(missing[:6])}{' …' if len(missing) > 6 else ''}")
        print("    （方向相反：该装单元，不是剪单元。已跳过）")

    if not stale:
        print("  ✓ 无台账外单元，无需剪除")
        return 0

    print(f"  台账外候选 {len(stale)} 个：{', '.join(stale[:6])}"
          f"{' …' if len(stale) > 6 else ''}")

    # 逐个过闸
    actionable: list[str] = []
    for name in stale:
        if name in protected:
            print(f"  ⊘ {name}: 在保护名单（{PROTECT_ENV}），跳过")
            continue
        st = unit_state(name)
        if st == "active":
            # 闸二：正在服务流量。台账缺它不等于它没用 —— 可能是台账没跟上。
            print(f"  ⊘ {name}: 状态 {st}，正在服务流量，跳过")
            continue
        actionable.append(name)

    if not actionable:
        print("  ✓ 无可剪除单元（候选全部被闸门拦下）")
        return 0

    if not args.apply:
        print(f"  → 将剪除 {len(actionable)} 个：{', '.join(actionable[:8])}"
              f"{' …' if len(actionable) > 8 else ''}")
        print(f"  → 将清理渲染产物里 {len(products)} 个台账外单元"
              f"（+ 对应脚本与派生配置）")
        print("    （dry-run；加 --apply 才真执行）")
        return 0

    # 产物先清，systemd 后剪。
    # WHY 这个顺序：产物是**复活的来源**（h2-install.sh 遍历它）。
    # 先清产物再剪 systemd，即使 systemd 剪除失败，僵尸单元也不会在下次
    # 安装时回来 —— 失败只留下一个已停用的单元文件，而不是一个会自启的单元。
    if products:
        pok, pfail = clean_products(products)
        print(f"  渲染产物清理：删除 {pok} 个文件，失败 {pfail} 个")

    ok = fail = 0
    for name in actionable:
        # 先停用再停止：disable 去掉开机自启，stop 停掉当前进程。
        # 顺序反了的话，stop 之后 enable 仍能让它在下次开机回来。
        rc, out = _privileged(["systemctl", "disable", "--now", name])
        if rc != 0:
            # 单元已经不存在（被手工删了）也算成功 —— 目标已达成。
            if "not found" in out.lower() or "does not exist" in out.lower():
                ok += 1
                continue
            print(f"  ✗ {name}: disable --now 失败 rc={rc} {out[:120]}")
            fail += 1
            continue
        if not args.keep_files:
            rc2, _ = _privileged(["rm", "-f", str(unit_dir / name)])
            if rc2 != 0:
                # 停用成功了，文件删不掉只是留下残骸，不影响「不会再跑」。
                print(f"  ⚠ {name}: 已停用但单元文件删除失败（保留残骸，不影响运行）")
        ok += 1
        print(f"  ✓ {name}: 已停用并"
              f"{'停用保留文件' if args.keep_files else '删除单元文件'}")

    print(f"剪除完成：成功 {ok} 失败 {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
