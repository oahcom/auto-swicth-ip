#!/bin/bash
# 启动一条 WARP 隧道。
#
# 用法: usque-launch.sh <端口> [传输]
#   传输 = h2      MASQUE over IPv4 TCP+TLS（最常见的可用通路）
#          h2v6    MASQUE over IPv6 TCP+TLS（v4 TCP 被封时的备份）
#          -6      MASQUE over IPv6 / QUIC（UDP 443）
#          -       或省略 = MASQUE over IPv4 / QUIC
# 工作目录由 unit 的 WorkingDirectory 指定（.warp/usqueN，含自己的 config.json）。
#
# WHY 默认是 h2 家族：本机到 CF 的 UDP 443 被运营商 ACL 拦（ICMP type 3 code 13），
# QUIC 版 v4 端点根本起不来。TCP 承载（--http2）不受该 ACL 影响 —— 实测
# 162.159.198.3~198.9 全部握手成功，v6 端点 2606:4700:103::2 也通。
# h2/h2v6 的差别只是入口地址族（TCP 443 走 v4 还是 v6），出口都是 WARP 分配的
# 2a09:bac* v6 地址，两者互为备份：一条 TCP 通路被封时另一条还能出。
#
# WHY -s ''：默认 SNI（consumer-masque.cloudflareclient.com）在部分链路上被 RST，
# 空 SNI 实测稳定。
#
# WHY 地址族/传输在单元层判而不在 daemon 里判：两者都是 usque 的启动参数，运行时
# 改不了；而 daemon 每次刷线都会 restart 单元，于是判一次就等于跟着网络变化自动纠正，
# daemon 不必知道 transport 字段的存在。
# WHY 判路由不判握手：没有 v6 源地址时内核在 sendmsg 就失败
# （write udp [::]:x->[2606:4700:103::2]:443: network is unreachable），usque 却
# 每秒重试一次把日志刷满，daemon 又把一条永远不会成功的线当脏线反复重启
# （实测 5 条 v6 线 30 分钟被拉起 44~57 次，零次成功）。
# 有路由但上游不通属于另一种故障，由 usque 重连与 daemon 刷新兜住，不在这里猜。
set -euo pipefail
port=${1:?用法: usque-launch.sh <端口> [h2|h2v6|-6|-]}
want=${2:-}
case $want in
    ''|-)  want=quic4 ;;
    -6)    want=quic6 ;;
    h2)    want=h2 ;;
    h2v6)  want=h2v6 ;;
    *) echo "✗ 未知传输参数: $want" >&2; exit 2 ;;
esac

# 需要 v6 源地址的两态：拿不到真实的 v6 源地址就降级到对应的 v4 通路。
# 只看路由不够：残留默认路由但全局地址已失效时路由查询照样成功，随后 sendmsg 仍失败。
case $want in
    quic6|h2v6)
        route=$(ip -6 route get 2606:4700:103::2 2>/dev/null || true)
        src=$(printf '%s\n' "$route" | sed -nE 's/.* src ([^ ]+).*/\1/p')
        if [ -z "$src" ] || [ "$src" = "::" ]; then
            if [ "$want" = h2v6 ]; then want=h2; else want=quic4; fi
        fi
        ;;
esac
# 一行写进 journal：事后要判断"这条线当时走的是哪种承载"，只看这个。
echo "usque-launch :$port 期望=${2:-default} 实走=$want" >&2

case $want in
    h2)    exec ./usque -c config.json socks --http2      -s '' -P 443 -b 127.0.0.1 -p "$port" ;;
    h2v6)  exec ./usque -c config.json socks --http2 -6   -s '' -P 443 -b 127.0.0.1 -p "$port" ;;
    quic6) exec ./usque -c config.json socks -6           -b 127.0.0.1 -p "$port" ;;
    *)     exec ./usque -c config.json socks              -b 127.0.0.1 -p "$port" ;;
esac
