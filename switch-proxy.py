#!/usr/bin/env python3
"""
switch-proxy — 快速切换 sing-box 代理节点

用法:
  switch-proxy              # 查看当前状态
  switch-proxy next         # 切到下一个随机节点
  switch-proxy next 5       # 连续切换 5 次（快速洗 IP）
  switch-proxy list         # 列出所有可用节点
  switch-proxy goto <name>  # 切到指定节点（支持模糊匹配）
  switch-proxy status       # 详细状态（含 9router 代理指向）
"""
import os
import random
import sys
import time
import logging

import requests

logger = logging.getLogger(__name__)

# ============= 配置 =============
from config import CFG, get_singbox_secret

CLASH_API = CFG["singbox"]["clash_api"]
SELECTOR = CFG["singbox"]["selector"]
TIMEOUT = 3


def _headers():
    return {"Authorization": "Bearer " + get_singbox_secret(), "Content-Type": "application/json"}


def get_nodes():
    try:
        r = requests.get(
            CLASH_API + "/proxies/" + SELECTOR,
            headers={"Authorization": "Bearer " + get_singbox_secret()},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            return d.get("now", "?"), d.get("all", [])
    except Exception as e:
        logger.warning("get_nodes 失败 (sing-box 可能不可达/密钥失效/非JSON响应): %s", e)
    return None, []


def get_9router_proxy():
    try:
        r = requests.get(CFG["9router"]["base"] + "/api/settings", timeout=3)
        if r.status_code == 200:
            d = r.json()
            return d.get("outboundProxyEnabled", False), d.get("outboundProxyUrl", "N/A")
    except Exception as e:
        logger.warning("get_9router_proxy 失败 (9router 不可达?): %s", e)
    return None, "N/A"


# ============= 子命令 =============
def cmd_report(window_min: int = 60):
    """展示过去 window_min 内各出口节点的请求成功率（轮换效果）"""
    try:
        import daemon as _daemon  # noqa: E402 (late import breaks CLI↔daemon cycle)
        rates = _daemon.get_per_egress_error_rate(window_min=window_min)
    except Exception as e:
        logger.warning("per-egress 归因失败: %s", e)
        print("  (暂无归因数据)")
        return 1
    if not rates:
        print("  (暂无出口归因记录 — daemon 可能未在日志周期内切换)")
        return 0
    print("=== 轮换效果 (最近 %d 分钟 per-egress 成功率) ===" % window_min)
    rows = sorted(rates.items(), key=lambda kv: kv[1][2], reverse=True)
    for node, (total, errs, rate) in rows:
        bar = "#" * int((1 - rate) * 20)
        print("  %-40.40s %4d/%-4d ok  %5.1f%%  |%s" % (node, total - errs, total, (1 - rate) * 100, bar))
    return 0


def cmd_status():
    current, nodes = get_nodes()
    if current is None:
        print("sing-box 连接失败")
        return 1
    enabled, url = get_9router_proxy()
    label = "开启" if enabled else "关闭"
    print("=== 代理状态 ===")
    print("  节点:        " + current)
    print("  可用节点:    " + str(len(nodes)))
    print("  9router代理: " + label + " -> " + url)
    return 0


def cmd_list():
    current, nodes = get_nodes()
    if current is None:
        print("sing-box 连接失败")
        return 1
    print("=== 可用节点 (" + str(len(nodes)) + "个) ===")
    for i, node in enumerate(nodes):
        mark = " <-- 当前" if node == current else ""
        tag = " [anytls]" if node.endswith("-any") else ""
        print("  %2d. %s%s%s" % (i + 1, node, tag, mark))
    return 0


def cmd_next(count=1):
    if count < 1:
        print("count 必须是正整数")
        return 1
    for i in range(count):
        current, nodes = get_nodes()
        if current is None:
            print("第" + str(i + 1) + "次切换失败")
            return 1
        candidates = [n for n in nodes if n != current and n != "direct"]
        if not candidates:
            print("没有其他可用节点")
            return 1
        new_node = random.choice(candidates)
        try:
            r = requests.put(
                CLASH_API + "/proxies/" + SELECTOR,
                headers=_headers(),
                json={"name": new_node},
                timeout=TIMEOUT,
            )
            ok = r.status_code in (200, 204)
        except Exception as e:
            logger.warning("cmd_next PUT 失败: %s", e)
            ok = False
        if ok:
            if count == 1:
                print(current + " -> " + new_node)
            else:
                print("  [%d/%d] %s -> %s" % (i + 1, count, current, new_node))
            if i < count - 1:
                time.sleep(0.5)
        else:
            print("第" + str(i + 1) + "次切换失败")
            return 1
    return 0


def cmd_goto(name):
    current, nodes = get_nodes()
    if current is None:
        print("sing-box 连接失败")
        return 1
    if not name:
        print("节点名不能为空，用 list 查看可用节点")
        return 1
    if name not in nodes:
        matches = [n for n in nodes if name in n]
        if len(matches) == 1:
            name = matches[0]
        elif len(matches) > 1:
            print("'%s' 匹配到多个:" % name)
            for m in matches:
                print("  " + m)
            return 1
        else:
            print("节点 '%s' 不存在, 用 list 查看" % name)
            return 1
    try:
        r = requests.put(
            CLASH_API + "/proxies/" + SELECTOR,
            headers=_headers(),
            json={"name": name},
            timeout=TIMEOUT,
        )
        ok = r.status_code in (200, 204)
    except Exception as e:
        logger.warning("cmd_goto PUT 失败: %s", e)
        ok = False
    if ok:
        print(current + " -> " + name)
        return 0
    else:
        print("切换失败")
        return 1


# ============= 入口 =============
def main():
    args = sys.argv[1:]
    if not args:
        return cmd_status()
    cmd = args[0].lower()
    if cmd in ("status", "s"):
        return cmd_status()
    elif cmd in ("list", "ls", "l"):
        return cmd_list()
    elif cmd in ("next", "n", "switch", "sw"):
        try:
            count = int(args[1]) if len(args) > 1 else 1
        except ValueError:
            print("count 必须是整数")
            return 1
        return cmd_next(count)
    elif cmd in ("report", "rpt", "r"):
        try:
            w = int(args[1]) if len(args) > 1 else 60
        except ValueError:
            print("窗口分钟数必须是整数 (默认 60)")
            return 1
        return cmd_report(w)
    elif cmd in ("goto", "go", "g"):
        if len(args) < 2:
            print("用法: switch-proxy goto <节点名>")
            return 1
        return cmd_goto(args[1])
    elif cmd in ("help", "h"):
        print(__doc__)
        return 0
    else:
        print("未知指令 '%s' — 用法: switch-proxy goto <节点名>  或输入 help 查看帮助" % cmd)
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
