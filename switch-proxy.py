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

import requests

# ============= 配置 =============
from config import CFG, SINGBOX_SECRET

CLASH_API = CFG["singbox"]["clash_api"]
SELECTOR = CFG["singbox"]["selector"]
TIMEOUT = 3


def _headers():
    return {"Authorization": "Bearer " + SINGBOX_SECRET, "Content-Type": "application/json"}


def get_nodes():
    try:
        r = requests.get(
            CLASH_API + "/proxies/" + SELECTOR,
            headers={"Authorization": "Bearer " + SINGBOX_SECRET},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            return d.get("now", "?"), d.get("all", [])
    except Exception:
        pass
    return None, []


def get_9router_proxy():
    try:
        r = requests.get(CFG["9router"]["base"] + "/api/settings", timeout=3)
        if r.status_code == 200:
            d = r.json()
            return d.get("outboundProxyEnabled", False), d.get("outboundProxyUrl", "N/A")
    except Exception:
        pass
    return None, "N/A"


# ============= 子命令 =============
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
        except Exception:
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
    except Exception:
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
    elif cmd in ("goto", "go", "g"):
        if len(args) < 2:
            print("用法: switch-proxy goto <节点名>")
            return 1
        return cmd_goto(args[1])
    elif cmd in ("help", "h"):
        print(__doc__)
        return 0
    else:
        return cmd_goto(cmd)


if __name__ == "__main__":
    sys.exit(main() or 0)
