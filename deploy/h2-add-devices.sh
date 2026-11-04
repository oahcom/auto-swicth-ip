#!/bin/bash
# 扩容 H2 承载能力：注册 N 台新 usque 设备，供 render_h2_lines.py 渲染新线路。
#
#   ./deploy/h2-add-devices.sh 3          # 注册 3 台（编号接在现有最大号之后）
#   ./deploy/h2-add-devices.sh 3 --dry-run
#
# 渲染器（render_h2_lines.py）设备不够时会自己调这个脚本，一般不用手跑。
#
# 为什么需要单独一条命令：render_h2_lines.py 的线路数 = min(渠道数, 空闲设备数)，
# 设备用完就渲染不出新线。而扩容不能复用现成的 usque-warp*/usque-h2* 单元 ——
# 一台设备只能承载一个 CF 会话，两条 usque 抢同一个设备身份会互相踢下线。
#
# 注册会撞 Cloudflare 限流（实测一个窗口约 9 台），逐台串行 + 失败即停。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COUNT="${1:-}"
DRY=0
[ "${2:-}" = "--dry-run" ] && DRY=1
case "$COUNT" in ''|*[!0-9]*)
    sed -n '2,10p' "$0"; echo "✗ 需要设备数量（正整数）" >&2; exit 2 ;;
esac
[ "$COUNT" -gt 0 ] || { echo "✗ 数量必须 ≥1" >&2; exit 2; }

USQUE="$DIR/.warp/usque/usque"
[ -x "$USQUE" ] || { echo "✗ 缺少 $USQUE" >&2; exit 1; }

# **编号从现有最大号之后续**，不能每次从 1 开始：第一次扩 3 台得
# h2new1..3，第二次再扩 3 台若还从 1 起，前三台全部「已存在」跳过，
# 结果只新增 0 台 —— 而调用方（渲染器/人）以为要到了 3 台。
last=0
for d in "$DIR"/.warp/usque-h2new*; do
    [ -d "$d" ] || continue
    n="${d##*usque-h2new}"
    case "$n" in ''|*[!0-9]*) continue ;; esac
    [ "$n" -gt "$last" ] && last="$n"
done

made=0
for k in $(seq 1 "$COUNT"); do
    i=$((last + k))
    d="$DIR/.warp/usque-h2new$i"
    if [ -f "$d/config.json" ]; then
        echo "· usque-h2new$i 已存在，跳过"
        continue
    fi
    if [ "$DRY" = 1 ]; then
        echo "[dry-run] 将注册 usque-h2new$i"
        continue
    fi
    mkdir -p "$d"
    # 二进制共用主目录那份。通配 `.warp/usque*/` **包含主目录自身**，照抄会把
    # 真二进制换成指向自己的链接（ELOOP）—— 所以按目录名逐个建，不用通配。
    ln -sfn ../usque/usque "$d/usque"
    echo -n "注册 usque-h2new$i … "
    # 必须直连：本机若 export 了 http(s)_proxy，usque 会走代理，CF 返回
    # 非 JSON 错误页（failed to parse error response）。
    if ( cd "$d" && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
            ./usque register --accept-tos -n "asip-h2new$i" >/dev/null 2>&1 ); then
        chmod 600 "$d/config.json"
        echo "✓"
        made=$((made + 1))
    else
        echo "✗"
        # 半成品目录会让下一次渲染把它当成可用设备（config.json 不存在倒是
        # 会被 free_devices 过滤掉，但空目录会误导人）。清掉，下次能重来。
        rm -rf "$d"
        echo "✗ 注册失败（多半是 CF 限流）。已注册 $made 台，稍后重跑即可" >&2
        exit 1
    fi
done

[ "$DRY" = 1 ] && exit 0
echo "已新增 $made 台设备。下一步："
echo "  python3 deploy/render_h2_lines.py --print    # 看新线路会怎么分配"
echo "  ./deploy/h2-install.sh                        # 渲染并启用"
