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
import logging
import random
import sys
import time
from urllib.parse import quote

from asip.clash_client import ClashClient
from asip.http import request_json
from config import CFG, get_singbox_secret

logger = logging.getLogger(__name__)

CLASH_API = CFG["singbox"]["clash_api"].rstrip("/")
SELECTOR = CFG["singbox"]["selector"]
TIMEOUT = 3
_clash = ClashClient(CLASH_API, get_singbox_secret, timeout=TIMEOUT)


def get_nodes() -> tuple[str | None, list[str]]:
    """返回 (当前节点, 全部节点)；sing-box 不可达时返回 (None, [])。"""
    status, data = _clash.get_json(f"proxies/{quote(SELECTOR, safe='')}")
    if status is None:
        logger.warning("get_nodes 失败: sing-box 不可达")
    elif status != 200:
        logger.warning("get_nodes 失败: HTTP %s (密钥失效?)", status)
    elif not isinstance(data, dict):
        logger.warning("get_nodes 失败 (非 JSON 响应)")
    else:
        return data.get("now", "?"), data.get("all", [])
    return None, []


def get_9router_proxy() -> tuple[bool | None, str]:
    """返回 (outboundProxyEnabled, outboundProxyUrl)；9router 不可达时 (None, "N/A")。"""
    result = request_json(
        "GET",
        f"{CFG['9router']['base'].rstrip('/')}/api/settings",
        timeout=TIMEOUT,
    )
    if result.ok and isinstance(result.data, dict):
        data = result.data
        return data.get("outboundProxyEnabled", False), data.get("outboundProxyUrl", "N/A")
    if result.error:
        reason = result.error
    elif result.status is not None:
        reason = f"HTTP {result.status}"
    else:
        reason = "空响应"
    logger.warning("get_9router_proxy 失败 (9router 不可达?): %s", reason)
    return None, "N/A"


def _switch_to(node: str) -> bool:
    """PUT selector；成功返回 True，网络/HTTP 失败返回 False。"""
    if _clash.select(SELECTOR, node):
        return True
    logger.warning("切换到 %s 失败", node)
    return False


# ============= 子命令 =============
def cmd_report(window_min: int = 60) -> int:
    """展示过去 window_min 内各出口节点的请求成功率（轮换效果）。"""
    try:
        import daemon as _daemon  # noqa: E402 (late import breaks CLI↔daemon cycle)
        rates = _daemon.get_per_egress_error_rate(window_min=window_min)
    except Exception as e:
        logger.warning("per-egress 归因失败: %s", e)
        print("  (暂无归因数据)")
        return 1
    if not rates:
        # 归因失败时 daemon 内部吞异常并返回 {}，与"确实没数据"无法区分。
        # 读它记录的失败原因，把二者分开报告 —— 避免把查询故障误读成"daemon 没切换"。
        err = _daemon.get_last_egress_error()
        if err:
            print(f"  (归因查询失败: {err})")
            return 2          # 数据不可信，与"确实无数据"区分
        print("  (暂无出口归因记录 — daemon 可能未在日志周期内切换)")
        return 0
    print(f"=== 轮换效果 (最近 {window_min} 分钟 per-egress 成功率) ===")
    # rate 是错误率；表头/柱子讲的是成功率，所以按 rate 升序（成功率降序）
    for node, (total, errs, rate) in sorted(rates.items(), key=lambda kv: kv[1][2]):
        ok_pct = (1 - rate) * 100
        bar = "#" * round((1 - rate) * 20)
        print(f"  {node:<38} {total - errs:>4}/{total:<4} ok  {ok_pct:>5.1f}%  |{bar}")
    return 0


def cmd_status() -> int:
    current, nodes = get_nodes()
    if current is None:
        print("sing-box 连接失败")
        return 1
    enabled, url = get_9router_proxy()
    label = "开启" if enabled else "关闭"
    print("=== 代理状态 ===")
    print(f"  节点:        {current}")
    print(f"  可用节点:    {len(nodes)}")
    print(f"  9router代理: {label} -> {url}")
    return 0


def cmd_list() -> int:
    current, nodes = get_nodes()
    if current is None:
        print("sing-box 连接失败")
        return 1
    print(f"=== 可用节点 ({len(nodes)}个) ===")
    for i, node in enumerate(nodes):
        mark = " <-- 当前" if node == current else ""
        tag = " [anytls]" if node.endswith("-any") else ""
        print(f"  {i + 1:>2}. {node}{tag}{mark}")
    return 0


def cmd_next(count: int = 1) -> int:
    if count < 1:
        print("count 必须是正整数")
        return 1
    for i in range(count):
        current, nodes = get_nodes()
        if current is None:
            print(f"第{i + 1}次切换失败")
            return 1
        candidates = [n for n in nodes if n != current and n != "direct"]
        if not candidates:
            print("没有其他可用节点")
            return 1
        new_node = random.choice(candidates)
        if not _switch_to(new_node):
            print(f"第{i + 1}次切换失败")
            return 1
        if count == 1:
            print(f"{current} -> {new_node}")
        else:
            print(f"  [{i + 1}/{count}] {current} -> {new_node}")
        if i < count - 1:
            time.sleep(0.5)
    return 0


def cmd_goto(name: str) -> int:
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
            print(f"'{name}' 匹配到多个:")
            for m in matches:
                print(f"  {m}")
            return 1
        else:
            print(f"节点 '{name}' 不存在, 用 list 查看")
            return 1
    if not _switch_to(name):
        print("切换失败")
        return 1
    print(f"{current} -> {name}")
    return 0


# ============= 入口 =============
def main() -> int:
    args = sys.argv[1:]
    if not args:
        return cmd_status()
    cmd = args[0].lower()
    if cmd in ("status", "s"):
        return cmd_status()
    if cmd in ("list", "ls", "l"):
        return cmd_list()
    if cmd in ("next", "n", "switch", "sw"):
        try:
            count = int(args[1]) if len(args) > 1 else 1
        except ValueError:
            print("count 必须是整数")
            return 1
        return cmd_next(count)
    if cmd in ("report", "rpt", "r"):
        try:
            window = int(args[1]) if len(args) > 1 else 60
        except ValueError:
            print("窗口分钟数必须是整数 (默认 60)")
            return 1
        if window < 1:
            # 负数会让 cutoff 落到未来 → 空结果，被误读成"daemon 没切换"
            print(f"窗口分钟数必须 ≥ 1 (收到 {window})")
            return 1
        return cmd_report(window)
    if cmd in ("goto", "go", "g"):
        if len(args) < 2:
            print("用法: switch-proxy goto <节点名>")
            return 1
        return cmd_goto(args[1])
    if cmd in ("help", "h"):
        print(__doc__)
        return 0
    print(f"未知指令 '{cmd}' — 用法: switch-proxy goto <节点名>  或输入 help 查看帮助")
    return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
