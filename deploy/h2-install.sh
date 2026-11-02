#!/bin/bash
# 启用 H2 线路：把 .warp/h2/units/ 下的单元装进 systemd 并启动。
#
#   ./deploy/h2-install.sh                 # 全流程（渲染 → 校验 → 安装 → 启用）
#   ./deploy/h2-install.sh --render-only   # 只重新渲染
#   ./deploy/h2-install.sh --config-only   # 安装单元但不启用
#   SYSTEMD_DIR=/tmp/x ./deploy/h2-install.sh --config-only   # 演练
#
# 渲染与启用分开，是因为 render_h2_lines.py 特意不 enable 任何单元：它只写文件。
# 这个脚本负责落进 /etc/systemd/system，并先 reload 再启动 sing-box、最后 usque。
#
# 幂等：单元内容没变就不重启；usque 各线互不影响（一条挂了不重启其余）。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYBIN="${PYTHON:-/usr/bin/python3}"
[ -x "$PYBIN" ] || { echo "✗ 解释器不存在：$PYBIN（用 PYTHON=... 指定）" >&2; exit 1; }

UNIT_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SRC="$DIR/.warp/h2/units"
RENDER_ONLY=0; CONFIG_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --render-only) RENDER_ONLY=1 ;;
        --config-only) CONFIG_ONLY=1 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "✗ 未知参数: $arg" >&2; exit 2 ;;
    esac
done

echo "== 1/3 渲染 =="
"$PYBIN" "$DIR/deploy/render_h2_lines.py" || exit 1
[ "$RENDER_ONLY" = 1 ] && exit 0

echo "== 2/3 静态校验 =="
"$PYBIN" "$DIR/deploy/verify_h2_render.py" || exit 1
# sing-box check 是唯一能抓「规则语义写错」的检查：缺 action 的规则在 1.13 被当
# reject，验证器已经查了，但 override 端口之类只有真解析才暴露。
"$DIR/sing-box" check -c "$DIR/.warp/h2/singbox.json" \
    || { echo "✗ sing-box check 失败：.warp/h2/singbox.json" >&2; exit 1; }

echo "== 3/3 安装单元 =="
# 演练目录（SYSTEMD_DIR 非 /etc）不装 sudoers/asip-unit：那是系统级的路径。
case "$UNIT_DIR" in
    /etc/systemd/*|/usr/lib/systemd/*)
        need_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo -n "$@" || { echo "✗ 需要 root: $*" >&2; exit 1; }; fi; }
        ;;
    *) need_root() { "$@"; } ;;
esac
need_root mkdir -p "$UNIT_DIR"

SERVICES="asip-h2-singbox.service"
for f in "$SRC"/usque-h2-*.service; do
    [ -f "$f" ] || continue
    svc="$(basename "$f")"
    # 内容没变就别装：install 会更新 mtime，systemd 视为改动，随后 restart 换会话。
    cmp -s "$f" "$UNIT_DIR/$svc" || need_root install -m 0644 "$f" "$UNIT_DIR/$svc"
    SERVICES="$SERVICES $svc"
done
# 上面那份 sing-box 单元也走同样的写法
cmp -s "$SRC/asip-h2-singbox.service" "$UNIT_DIR/asip-h2-singbox.service" \
    || need_root install -m 0644 "$SRC/asip-h2-singbox.service" "$UNIT_DIR/asip-h2-singbox.service"

case "$UNIT_DIR" in
    /etc/systemd/*|/usr/lib/systemd/*) need_root systemctl daemon-reload ;;
    *) systemctl --user daemon-reload 2>/dev/null || true ;;
esac

if [ "$CONFIG_ONLY" = 1 ]; then
    echo "已安装单元（未启用）：$UNIT_DIR（$(echo $SERVICES | wc -w) 个）"
    exit 0
fi

# 先 sing-box 再 usque：入站没起来时 usque 会立刻 connection refused 并重试，
# 日志噪声大且看不出真因。单元里已写 After/Wants，这里只是让首次启动更干净。
case "$UNIT_DIR" in
    /etc/systemd/*|/usr/lib/systemd/*) SCTL=(need_root systemctl) ;;
    *) SCTL=(systemctl --user) ;;
esac
"${SCTL[@]}" enable --now asip-h2-singbox.service
USQUE_UNITS="$(echo $SERVICES | sed 's/asip-h2-singbox.service//' | xargs)"
[ -z "$USQUE_UNITS" ] || "${SCTL[@]}" enable --now $USQUE_UNITS
echo "已启用 $(echo $SERVICES | wc -w) 个单元（1 sing-box + usque 线）"

# 主 sing-box 需要重载才会看到新的 socks 出站（warp-outbound.json 已含 H2 成员）。
echo "提示：主 sing-box 需重载后才把 H2 线纳入 opencode-out（见 docs/warp-channel-allocation.md）" >&2
