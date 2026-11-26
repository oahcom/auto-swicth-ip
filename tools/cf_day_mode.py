#!/usr/bin/env python3
"""CF / H2 白天夜间切换 —— 只启停进程，不改台账、不重启 sing-box。

白天（on）：停 30 条 CF 直连，出口交给 H2/机场池；补位 H2 保持运行。
夜间（off）：启 30 条 CF 直连，出口切回 WARP-CF4；停 30 条补位 H2。

为什么只启停：H2 的 30 条补位线是常驻成员（入站/selector 一直在），
启停 usque 进程不会影响其他线路，也不需要重启 asip-h2-singbox。
删除/重建台账才会触发全量重启 —— 那条路已经验证过会打断在途流量。

用法：
  python3 tools/cf_day_mode.py on      # 白天模式
  python3 tools/cf_day_mode.py off     # 夜间模式
  python3 tools/cf_day_mode.py status  # 查看当前状态
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
LOG = PROJECT / ".warp" / "cf_day_mode.log"
CONFIG = PROJECT / "singbox-config.json"
CF_SELECTOR = "opencode-out"
DAY_DEFAULT = "proxy"          # 白天：出口交给机场/H2 池
NIGHT_DEFAULT = "WARP-CF4"     # 夜间：出口交给 CF 直连
SPARE_INDEX_MIN = 101          # index > 101 的 H2 行是补位线


def log(msg: str) -> None:
    line = f"[{subprocess.run(['date', '+%F %T'], capture_output=True, text=True).stdout.strip()}] {msg}"
    print(line)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def sctl(*args: str) -> None:
    subprocess.run(["sudo", "systemctl", *args], capture_output=True, text=True)


def cf_units() -> list[str]:
    r = subprocess.run(
        ["systemctl", "list-unit-files", "usque-warp*.service", "--no-legend", "--plain"],
        capture_output=True, text=True,
    )
    return [l.split()[0] for l in r.stdout.splitlines() if l.strip()]


def spare_units() -> list[str]:
    """补位 H2 的单元名（index > SPARE_INDEX_MIN）。"""
    p = PROJECT / ".warp" / "h2" / "channels.json"
    if not p.exists():
        return []
    led = json.loads(p.read_text())
    return sorted(r["unit"] for r in led if int(r["index"]) > SPARE_INDEX_MIN)


def active_units(pattern: str) -> set[str]:
    r = subprocess.run(
        ["systemctl", "list-units", pattern, "--state=active", "--no-legend", "--plain"],
        capture_output=True, text=True,
    )
    return {l.split()[0] for l in r.stdout.splitlines() if l.strip()}


def set_default(name: str) -> None:
    """把 opencode-out 的默认值写进配置（运行态 + 落盘）。"""
    try:
        c = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return
    changed = False
    for o in c.get("outbounds") or []:
        if o.get("tag") == CF_SELECTOR and o.get("default") != name:
            o["default"] = name
            changed = True
    if changed:
        CONFIG.write_text(json.dumps(c, ensure_ascii=False, indent=1))
    # 运行态同步（不重启进程）
    try:
        import urllib.request
        body = json.dumps({"name": name}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:9090/proxies/{CF_SELECTOR}",
            data=body, method="PUT",
            headers={"Authorization": "Bearer BWw/9meYoOsWMaVfkZuUoGPem3qcaszRUhlMFbCSnKM=",
                     "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def cmd_on() -> None:
    """白天：停 CF、留 H2。"""
    cf = cf_units()
    if cf:
        sctl("disable", "--now", *cf)
        log(f"已停 {len(cf)} 条 CF 直连")
    sp = spare_units()
    not_running = [u for u in sp if u not in active_units("usque-h2-*")]
    if not_running:
        sctl("enable", "--now", *not_running)
        log(f"已启 {len(not_running)} 条补位 H2")
    set_default(DAY_DEFAULT)
    log(f"出口 → {DAY_DEFAULT}（白天模式）")


def cmd_off() -> None:
    """夜间：启 CF、停补位 H2。"""
    cf = cf_units()
    not_running = [u for u in cf if u not in active_units("usque-warp*")]
    if not_running:
        sctl("enable", "--now", *not_running)
        log(f"已启 {len(not_running)} 条 CF 直连")
    sp = spare_units()
    running = [u for u in sp if u in active_units("usque-h2-*")]
    if running:
        sctl("disable", "--now", *running)
        log(f"已停 {len(running)} 条补位 H2")
    set_default(NIGHT_DEFAULT)
    log(f"出口 → {NIGHT_DEFAULT}（夜间模式）")


def cmd_status() -> None:
    cf = cf_units()
    sp = spare_units()
    cf_act = active_units("usque-warp*")
    h2_act = active_units("usque-h2-*")
    sp_act = [u for u in sp if u in h2_act]
    try:
        c = json.loads(CONFIG.read_text())
        dflt = next((o.get("default") for o in c["outbounds"] if o.get("tag") == CF_SELECTOR), "?")
    except Exception:
        dflt = "?"
    print(f"CF 直连:   {len(cf_act)}/{len(cf)} 在线")
    print(f"补位 H2:  {len(sp_act)}/{len(sp)} 在线")
    print(f"H2 总数:  {len(h2_act)} 在线")
    print(f"出口默认: {dflt}")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "on":
        cmd_on()
    elif cmd == "off":
        cmd_off()
    elif cmd == "status":
        cmd_status()
    else:
        print("用法: cf_day_mode.py on|off|status")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
