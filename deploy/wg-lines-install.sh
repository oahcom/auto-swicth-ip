#!/bin/bash
# 部署 WARP-over-proxy 线路（native WireGuard，UDP 经代理节点中转）。
#
#   WARP_WG_FAMILIES=v4,v6,v6,v6 WARP_WG_NODES=4 ./deploy/wg-lines-install.sh
#       → 4 个落地节点 × 每节点 4 条渠道 = 16 条线（1 条 v4 + 3 条 v6）
#   ... --config-only                       # 只生成配置与单元，不启用
#   SYSTEMD_DIR=/tmp/x ... ./deploy/...     # 演练
#
# 先跑 `python3 warp_wg.py survey` 生成落地 colo 地图：v4 渠道的上限是不同
# colo 的个数，不实测就会被同 colo 的多个节点骗成多个出口。
#
# 与 install.sh 解耦：usque 线是现网主力，WG 线是追加能力，互不改写对方的单元。
# 关掉只需把 WARP_WG_COUNT 设回 0（daemon 与主配置同时看不到这些线）。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYBIN="${PYTHON:-/usr/bin/python3}"
[ -x "$PYBIN" ] || { echo "✗ 解释器不存在：$PYBIN（用 PYTHON=... 指定）" >&2; exit 1; }

# 与 install.sh 一致：家目录从 passwd 取，不信 $HOME（sudo -u 下可能是 /root）。
if [ -z "${ASIP_HOME_DIR:-}" ] && [ -n "${SYSTEMD_DIR:-}" ]; then
    case "$SYSTEMD_DIR" in
        /etc/systemd/*) ;;
        *) ASIP_HOME_DIR="$(dirname "$SYSTEMD_DIR")/home" ;;
    esac
fi
HOME_DIR="${ASIP_HOME_DIR:-$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6 || true)}"
[ -n "$HOME_DIR" ] || HOME_DIR="$HOME"
# WG 线与 usque-warp 一样装在**系统**总线：daemon 自己是系统级 unit，
# 用户级单元在它那里会撞 polkit 交互认证（`Interactive authentication
# required`），换线时只能看着「重启后未就绪」。
UNIT_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
RUN_USER="${ASIP_RUN_USER:-$(id -un)}"
RUN_GROUP="${ASIP_RUN_GROUP:-$(id -gn "$RUN_USER" 2>/dev/null || id -gn)}"
mkdir -p "$UNIT_DIR"

# 单元以哪个用户跑。默认本用户；以 root 部署时必须显式给 ASIP_RUN_USER ——
# 否则单元会写成 User=root，而 .warp 下的线路凭据仍属普通用户，或反过来：
# root 生成的 singbox.json 属 root，单元以 administrator 跑就读不到（Permission
# denied），现象是 16 条线全部 start-limit 循环重启。本机唯一提权路径是
# `wsl -u root`，所以这条不是假想。

# 单元把 ExecStart 钉死在本次 DIR。拿副本试跑却没设 SYSTEMD_DIR 会把生产单元
# 改指到副本（install.sh 里真实发生过），这里同样拦一道。
cur="$(sed -n 's/^WorkingDirectory=//p' "$UNIT_DIR/warp-wg-1.service" 2>/dev/null | head -1 || true)"
if [ -z "${SYSTEMD_DIR:-}" ] && [ -n "$cur" ] && [ "$cur" != "$DIR" ] && [ -f "$cur/warp_wg.py" ]; then
    echo "✗ 现网单元当前指向 $cur，而本次运行的是 $DIR —— 拒绝覆盖生产单元" >&2
    echo "  试跑请设 SYSTEMD_DIR" >&2
    exit 1
fi

CONFIG_ONLY=0
[ "${1:-}" = "--config-only" ] && CONFIG_ONLY=1

if [ -n "${WARP_WG_FAMILIES:-}" ]; then
    export WARP_WG_FAMILIES
    export WARP_WG_NODES="${WARP_WG_NODES:-1}"
    PER=$(printf '%s' "$WARP_WG_FAMILIES" | tr ',' '\n' | grep -cE '^(v4|v6)$' || true)
    COUNT=$((PER * WARP_WG_NODES))
else
    COUNT="${WARP_WG_COUNT:-0}"
    export WARP_WG_COUNT="$COUNT"
fi
if ! [[ "$COUNT" =~ ^[0-9]+$ ]] || [ "$COUNT" -le 0 ]; then
    echo "✗ 需要 WARP_WG_FAMILIES（如 v4,v6,v6,v6）或 WARP_WG_COUNT=N（N>0）。" >&2
    exit 1
fi

# ---- 1. 注册设备 + 生成每条线的 sing-box 配置 + 刷新主配置出站清单 ----
# --probe 会先逐节点验证 UDP 转发能力；默认关，因为多测一轮会多烧几秒，
# 首次部署建议开着确认节点可用性。
APPLY_ARGS=(apply)
[ "${WARP_WG_PROBE:-0}" = "1" ] && APPLY_ARGS+=(--probe)
# apply 会写 .warp/wglines/lineN/{account.toml,profile.conf,singbox.json}（0600）。
# 以 root 部署时若以 root 跑，文件属 root，而单元以 $RUN_USER 运行 —— 读不到配置，
# 16 条线全在 permission denied 里崩溃循环（实测过一次）。从源头给对属主。
if [ "$(id -u)" = 0 ] && [ "$RUN_USER" != root ]; then
    runuser -u "$RUN_USER" -- env PYTHONPATH="$DIR" "$PYBIN" "$DIR/warp_wg.py" "${APPLY_ARGS[@]}"
else
    PYTHONPATH="$DIR" "$PYBIN" "$DIR/warp_wg.py" "${APPLY_ARGS[@]}"
fi

# ---- 2. 单元：每条线一个进程，重启只影响这条线 ----
# 名字必须与 config.py 里生成的 member.service 一致，否则 daemon 的
# systemctl restart 会打空。
SERVICES=""
for i in $(seq 1 "$COUNT"); do
    svc="warp-wg-$i.service"
    SERVICES="$SERVICES $svc"
    cat > "$UNIT_DIR/$svc" <<EOF
[Unit]
Description=Cloudflare WARP (WireGuard) line $i for auto-switch-ip
After=network-online.target
Wants=network-online.target
# daemon 判脏就重启，实测峰值 228 次/分钟；默认 start-limit（10s 内 5 次）
# 会把单元锁成 start-limit-hit，之后所有重启都失败。
StartLimitIntervalSec=0

[Service]
User=$RUN_USER
Group=$RUN_GROUP
Environment=HOME=$HOME_DIR
Type=simple
# 上一实例异常退出可能仍占着端口，先清持有者再 bind。
ExecStartPre=-/usr/bin/fuser -k $((${BASE_PORT:-1110}+$i-1))/tcp
WorkingDirectory=$DIR/.warp/wglines/line$i
ExecStart=$DIR/sing-box run -c $DIR/.warp/wglines/line$i/singbox.json
Restart=always
RestartSec=2
# 每条线一条独立的 WARP 会话，文件描述符需求与 usque 同量级。
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
done

case "$UNIT_DIR" in
    /etc/systemd/system|/usr/lib/systemd/system)
        SUDO=""; [ "$(id -u)" = "0" ] || SUDO="sudo"
        $SUDO systemctl daemon-reload ;;
    *) systemctl --user daemon-reload 2>/dev/null || true ;;
esac

if [ "$CONFIG_ONLY" = 1 ]; then
    echo "已生成配置与单元（未启用/未重启）：$UNIT_DIR"
    exit 0
fi

# ---- 3. 启用 ----
# 首次启用会立刻起隧道；已启用的会被 restart，换掉当前 WARP 会话。
if [ "$UNIT_DIR" = "/etc/systemd/system" ] || [ "$UNIT_DIR" = "/usr/lib/systemd/system" ]; then
    SUDO=""; [ "$(id -u)" = "0" ] || SUDO="sudo"
    $SUDO systemctl enable --now $SERVICES
else
    systemctl --user enable --now $SERVICES
fi
echo "已启用 $(echo $SERVICES | wc -w) 条 WARP-WG 线"

# 主 sing-box 需要重启才会读到新的 socks 出站（warp-outbound.json 已更新）。
echo "提示：主 sing-box 需重载配置后才会把新线纳入 opencode-out（见 docs/warp-wg-over-proxy.md）" >&2
