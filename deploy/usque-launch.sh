#!/bin/bash
# 启动一条 WARP 隧道；标了 v6 的线在本机没有 IPv6 路由时自动降级走 v4。
#
# 用法: usque-launch.sh <端口> [族]     族 = -6 表示期望 IPv6
# 工作目录由 unit 的 WorkingDirectory 指定（.warp/usqueN，含自己的 config.json）。
#
# WHY 在单元层判而不在 daemon 里判：地址族是 usque 的启动参数，运行时改不了；
# 而 daemon 每次刷线都会 restart 单元，于是判一次就等于跟着网络变化自动纠正，
# daemon 不必知道 ipv6 字段的存在。
# WHY 判路由不判握手：没有 v6 默认路由时内核在 sendmsg 就失败
# （write udp [::]:x->[2606:4700:103::2]:443: network is unreachable），usque 却
# 每秒重试一次把日志刷满，daemon 又把一条永远不会成功的线当脏线反复重启
# （实测 5 条 v6 线 30 分钟被拉起 44~57 次，零次成功）。
# 有路由但上游不通属于另一种故障，由 usque 重连与 daemon 刷新兜住，不在这里猜。
set -euo pipefail
port=${1:?用法: usque-launch.sh <端口> [-6]}
want=${2:-}
case $want in ''|-) want=v4 ;; -6) ;; *) echo "✗ 未知地址族参数: $want" >&2; exit 2 ;; esac

fam=v4
if [ "$want" = -6 ]; then
    # 只看路由不够：残留默认路由但全局地址已失效时路由查询照样成功，随后
    # usque -6 仍然 sendmsg 失败。必须同时拿到一个真实的源地址。
    route=$(ip -6 route get 2606:4700:103::2 2>/dev/null || true)
    src=$(printf '%s\n' "$route" | sed -nE 's/.* src ([^ ]+).*/\1/p')
    [ -n "$src" ] && [ "$src" != "::" ] && fam=v6
fi
# 一行写进 journal：事后要判断"这条线当时走的是哪个族"，只看这个。
echo "usque-launch :$port 期望=$want 实走=$fam" >&2

if [ "$fam" = v6 ]; then
    exec ./usque -c config.json socks -6 -b 127.0.0.1 -p "$port"
fi
exec ./usque -c config.json socks -b 127.0.0.1 -p "$port"
