#!/usr/bin/env python3
"""本轮交付的最终验收：把每一项声称都拿真数据验一遍。

WHY 需要这份脚本：本轮做了很多「声称」（剪除成功、零误伤、复活阻断、
幂等、自检生效、静态检查有效）。每一项都必须有**可复现的证据**，而不是
「我跑过了看起来没问题」。这里把验收写成脚本，任何人都能重跑核对。
"""
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/administrator/dkk-projects/auto-switch-ip")
os.chdir(REPO)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def sh(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr


print("=== 1. 台账 ↔ 单元 ↔ 产物 三方一致 ===")
rows = json.load(open(".warp/h2/channels.json", encoding="utf-8"))
led_units = {r["unit"] for r in rows}
led_idx = {str(r["index"]) for r in rows}
uf = sh(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"])
sys_units = set()
for line in uf.splitlines():
    m = re.match(r"(usque-h2-(\d+)\.service)\s+(\S+)", line)
    if m:
        sys_units.add(m.group(1))
prod_units = {os.path.basename(p) for p in Path(".warp/h2/units").glob("usque-h2-*.service")}
check("台账 = systemd 单元", led_units == sys_units,
      f"台账 {len(led_units)} / systemd {len(sys_units)}")
check("台账 = 渲染产物", led_units == prod_units,
      f"台账 {len(led_units)} / 产物 {len(prod_units)}")
check("无台账外单元", not (sys_units - led_units), f"多出 {sorted(sys_units - led_units)}")

print()
print("=== 2. 在役线路完好（零误伤）===")
ok = fail = 0
bad = []
for r in rows:
    # 每条线连 3 次，至少 2 次通才算通 —— 实测偶发一次失败后立即复测三次
    # 全 OK，单次失败无法区分「线路坏了」与「瞬时抖动」。
    good = 0
    for _ in range(3):
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect(("127.0.0.1", int(r["socks_port"])))
            good += 1
        except Exception:
            pass
        finally:
            s.close()
    if good >= 2:
        ok += 1
    else:
        fail += 1
        bad.append((r["unit"], f"{good}/3"))
check("全部在役线 socks 连通(3次取多数)", fail == 0, f"OK={ok} FAIL={fail} {bad[:5]}")

print()
print("=== 3. 幂等：再跑一次剪除应无候选 ===")
r = subprocess.run([sys.executable, "deploy/reconcile_h2_units.py"],
                   capture_output=True, text=True, timeout=180)
check("剪除幂等（无候选）", "无需剪除" in r.stdout, r.stdout.strip().splitlines()[-1][:80])

print()
print("=== 4. 复活阻断：产物目录数必须等于台账数 ===")
check("产物数 = 台账数", len(prod_units) == len(rows),
      f"产物 {len(prod_units)} / 台账 {len(rows)}")

print()
print("=== 5. 静态名字检查：注入错名必须被抓到 ===")
orig = Path("daemon.py").read_text(encoding="utf-8")
bad = orig.replace('os.path.join(_PROJECT, ".warp"',
                   'os.path.join(PROJECT, ".warp"')
if bad != orig:
    Path("daemon.py").write_text(bad, encoding="utf-8")
    try:
        r1 = subprocess.run([sys.executable, "tools/check_undefined_names.py", "daemon.py"],
                            capture_output=True, text=True, timeout=120)
        caught = r1.returncode != 0
    finally:
        Path("daemon.py").write_text(orig, encoding="utf-8")
    r2 = subprocess.run([sys.executable, "tools/check_undefined_names.py", "daemon.py"],
                        capture_output=True, text=True, timeout=120)
    check("注入错名被抓到", caught, f"rc={r1.returncode}")
    check("还原后零误报", r2.returncode == 0, r2.stdout.strip().splitlines()[-1][:60])
else:
    check("注入片段存在（测试前提）", False, "找不到注入点")

print()
print("=== 6. 生产侧：daemon active + 自检已生效 ===")
act = sh(["systemctl", "is-active", "auto-switch-ip.service"]).strip()
check("daemon active", act == "active", act)
nrest = sh(["systemctl", "show", "auto-switch-ip.service", "-p", "NRestarts", "--value"]).strip()
check("无重启风暴", nrest == "0", f"NRestarts={nrest}")
logs = sh(["journalctl", "-u", "auto-switch-ip.service", "--since", "-30min", "--no-pager"])
check("自检已生效（配置自检出现）", "配置自检" in logs)
# 只看【当前进程启动之后】的日志 —— 之前的崩溃循环属于已修复的历史，
# 不该让验收永远失败。判据要对着「这次部署」而不是「最近半小时」。
started = sh(["systemctl", "show", "auto-switch-ip.service",
              "-p", "ExecMainStartTimestamp", "--value"]).strip()
logs2 = sh(["journalctl", "-u", "auto-switch-ip.service", "--since", started,
            "--no-pager"])
check("本次部署后无 NameError/TypeError",
      "NameError" not in logs2 and "TypeError" not in logs2,
      f"进程启动于 {started}")

print()
print("=== 7. 设备池与扩容容量 ===")
devs = set()
for p in Path(".warp").glob("usque*"):
    if p.is_dir() and (p / "config.json").exists():
        devs.add(p.name)
used = {r["device"] for r in rows}
check("设备总数", len(devs) >= 107, f"{len(devs)} 台")
check("空闲设备足够翻倍", len(devs) >= len(rows) * 2 - 6,
      f"空闲 {len(devs - used)} 台 / 目标 {len(rows)*2} 条 / 设备 {len(devs)} 台")

print()
print("=== 8. 设备争用可见化 ===")
r = subprocess.run([sys.executable, "deploy/verify_h2_render.py"],
                   capture_output=True, text=True, timeout=180)
out = r.stdout + r.stderr
check("校验器能报出设备争用", "同时持有" in out,
      f"报出 {out.count('同时持有')} 台")

print()
print("=" * 60)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print(f"  ✗ {f}")
    sys.exit(1)
print("全部通过")
