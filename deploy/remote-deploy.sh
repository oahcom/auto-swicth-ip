#!/bin/bash
# 把本机拓扑复刻到远端主机（代码 + 状态表 + 设备 + 单元 + 服务）。
#
#   ./deploy/remote-deploy.sh user@host [--port 22] [--dry-run] [--skip-register]
#
# 设计前提：**拓扑以本机为准**。远端不参与决策，只复刻：
#   直连 usque 数、H2 渠道数与设备分配、WG 线路数、块模型开关、selector 成员。
# 所以脚本读的是本机的 `.env` 与 `.warp/*.json`，不是远端现有配置。
#
# WHY 用 tar 而不是 git bundle：bundle 会带上 `refs/stash` 与 worktree 的
# 悬空 commit，远端 `git fetch` 会在 revision walk 时报
# "did not send all necessary objects"（实测）。`git archive HEAD` 只含
# 工作树内容，没有这个坑，也不会把 .env / .warp 这些 gitignored 的东西漏出去
# —— 它们由后面的显式步骤单独传。
#
# WHY 全程显式传 HOME / 环境变量给 sudo：`sudo` 会清掉环境（`-E` 在现代
# sudo 上被忽略），于是 `WARP_WG_FAMILIES` 丢失、`HOME` 变成 /root，
# 表现为「需要 WARP_WG_FAMILIES」和「读不到 .singbox_secret」两个假故障。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

TARGET=""; PORT=22; DRY=0; SKIP_REGISTER=0
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        --skip-register) SKIP_REGISTER=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        -*) echo "✗ 未知参数: $1" >&2; exit 2 ;;
        *) TARGET="$1"; shift ;;
    esac
done
[ -n "$TARGET" ] || { echo "用法: $0 user@host [--port N] [--dry-run]" >&2; exit 2; }

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -p "$PORT")
# --dry-run 必须真的不执行：远端命令里全是 restart / install / register，
# 只解析参数不短路等于「看起来演练过、实际动了生产」。
if [ "$DRY" = 1 ]; then
    # 打到 stderr：调用方普遍用 `$(run ...)` 取输出，写 stdout 会把命令文本
    # 当成结果（实测 9router 端口探测因此拿到一整段 shell）。
    run() { printf '  [dry] ssh %s %q\n' "$TARGET" "$1" >&2; }
    put() { printf '  [dry] scp %s -> %s:%s\n' "$1" "$TARGET" "$2" >&2; }
else
    run() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }
    put() { scp -O -o StrictHostKeyChecking=accept-new -P "$PORT" "$1" "$TARGET:$2"; }
fi
say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# 远端仓库路径：优先沿用已存在的，否则 ~/auto-swicth-ip
REMOTE_USER="${TARGET%@*}"
if [ "$DRY" = 1 ]; then
    # 演练不连远端，用默认值把流程走完即可（这些值只影响打印的命令文本）
    REMOTE_DIR="~/auto-swicth-ip"; REMOTE_HOME="~"
else
    REMOTE_DIR="$(run 'for d in ~/auto-swicth-ip ~/asip; do [ -d "$d" ] && { echo "$d"; exit; }; done; echo ~/auto-swicth-ip')"
    REMOTE_HOME="$(run 'getent passwd "$(id -un)" | cut -d: -f6')"
fi
echo "远端: $TARGET:$REMOTE_DIR  (user=$REMOTE_USER home=$REMOTE_HOME)"

# ---------------------------------------------------------------- 0. 预检
say "0/7 预检"
run 'command -v python3 >/dev/null || { echo "✗ 远端缺 python3"; exit 1; }
     command -v systemctl >/dev/null || { echo "✗ 远端缺 systemd"; exit 1; }
     sudo -n true 2>/dev/null || { echo "✗ 远端需要免密 sudo"; exit 1; }
     echo "  python3 $(python3 -V 2>&1 | cut -d" " -f2) / sudo 免密 OK"'

# ---------------------------------------------------------------- 1. 代码
say "1/7 同步代码（git archive，不含 gitignored 文件）"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
git archive --format=tar.gz -o "$TMP/src.tgz" HEAD
put "$TMP/src.tgz" /tmp/asip-src.tgz
# 先在远端建 baseline：远端可能根本不是 git 仓库（旧部署是直接拷过去的）。
# 有 baseline 才能一眼看出「本次部署改了什么」，也才能在出问题时回滚。
run "set -e
cd $REMOTE_DIR 2>/dev/null || { mkdir -p $REMOTE_DIR; cd $REMOTE_DIR; }
if [ ! -d .git ]; then
    git init -q && git config user.email deploy@asip.local && git config user.name 'asip deploy'
    git add -A 2>/dev/null || true
    git commit -q -m 'baseline: 部署前远端状态' 2>/dev/null || true
    echo '  (远端原本不是 git 仓库，已 init + baseline)'
fi
tar xzf /tmp/asip-src.tgz
chmod +x deploy/*.sh tools/*.sh 2>/dev/null || true
python3 -m py_compile config.py daemon.py warp_wg.py warp_blocks.py
echo '  代码已解包，py_compile OK'"

# ---------------------------------------------------------------- 2. 拓扑参数
say "2/7 对齐 .env 拓扑参数（以本机为准）"
# 只覆盖拓扑相关的键，其余（订阅 token、relay token 等）保留远端原值 ——
# 那些是每台机器自己的凭据，不该被本机覆盖。
ENV_KEYS=(WARP_WG_FAMILIES WARP_WG_NODES ASIP_WARP_BLOCKS ASIP_WARP_BLOCK_CHANNELS NINE_ROUTER_BASE)
ENV_PAYLOAD=""
for k in "${ENV_KEYS[@]}"; do
    v=$(grep -E "^${k}=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true)
    [ -n "$v" ] && ENV_PAYLOAD+="$k=$v"$'\n'
done
printf '%s' "$ENV_PAYLOAD" > "$TMP/env.patch"
put "$TMP/env.patch" /tmp/asip-env.patch
run "cd $REMOTE_DIR
touch .env
while IFS='=' read -r k v; do
    [ -n \"\$k\" ] || continue
    if grep -qE \"^\$k=\" .env; then
        sed -i \"s#^\$k=.*#\$k=\$v#\" .env
    else
        echo \"\$k=\$v\" >> .env
    fi
done < /tmp/asip-env.patch
grep -E '^(WARP_WG_|ASIP_WARP|NINE_ROUTER_BASE)' .env | sed 's/^/  /'"

# 远端 9router 端口可能与本机不同：探测出来覆盖掉，别让健康检查假失败。
say "2b/7 探测远端 9router 端口"
# 探 `/dashboard` 而不是 `/v1/models`：后者冷启动实测约 10s，短超时必然
# 误判成「端口上没有服务」。dashboard 只做跳转，毫秒级返回。
DETECTED=$(run 'for p in 20127 20128; do
    code=$(curl -s -o /dev/null -m 5 -w "%{http_code}" "http://127.0.0.1:$p/dashboard" || true)
    case "$code" in 2*|3*|401|403) echo "$p"; break ;; esac
done')
if [ -n "$DETECTED" ]; then
    echo "  远端 9router 在 :$DETECTED"
    run "cd $REMOTE_DIR
    sed -i 's#^NINE_ROUTER_BASE=.*#NINE_ROUTER_BASE=http://127.0.0.1:$DETECTED#' .env"
else
    echo "  ⚠ 未探到 9router（20127/20128 都不通），保留 .env 现值"
fi

# ---------------------------------------------------------------- 3. 实测表
say "3/7 同步实测表与 H2 台账（gitignored，但拓扑必需）"
# block-map.json：块的成员归属，没有它 warp_blocks.channels() 返回空，
# `plan` 直接报「块模型未启用」，一条渠道都开不出来。
# h2/channels.json：已投产的渠道↔设备↔端口分配，render_h2_lines 在块模型
# 筛不出 h2 时靠它兜底（见该文件里的注释）。
for f in .warp/block-map.json .warp/h2/channels.json; do
    [ -f "$f" ] || { echo "  ⚠ 本机缺 $f，跳过"; continue; }
    put "$f" "/tmp/$(basename "$f")"
    echo "  $f"
done
run "cd $REMOTE_DIR && mkdir -p .warp/h2
cp /tmp/block-map.json .warp/block-map.json 2>/dev/null || true
cp /tmp/channels.json .warp/h2/channels.json 2>/dev/null || true
chmod 600 .warp/block-map.json 2>/dev/null || true
echo '  已落盘'"

# ---------------------------------------------------------------- 4. 设备
say "4/7 补齐 usque 设备（每条 H2 线一台，CF 侧同一设备只能一个会话）"
# 本机 H2 用了几台设备，远端就得有几台空闲的。设备名必须与台账一致，
# 否则 `WARP-H2-<colo>-<i>` 成员整体平移，daemon 按 tag 记账全错位。
if [ "$SKIP_REGISTER" = 1 ]; then
    echo "  --skip-register：跳过"
else
    NEEDED=$(python3 - <<'PY'
import json, os
p = ".warp/h2/channels.json"
print(len({r["device"] for r in json.load(open(p))}) if os.path.exists(p) else 0)
PY
)
    echo "  本机 H2 需要 $NEEDED 台设备"
    run "cd $REMOTE_DIR
BIN=\$PWD/.warp/usque/usque
[ -x \$BIN ] || { echo '  ⚠ 缺 .warp/usque/usque，跳过注册'; exit 0; }
# 台账里的设备名逐个确认；缺的补注册。注册必须在该设备目录里执行 ——
# 二进制是绝对路径，cwd 不对会把 config.json 写到仓库根，下一轮又提示
# 'You already have a config'（实测踩过，误判成注册失败）。
python3 - <<'PY' > /tmp/asip-devices.txt
import json
for r in json.load(open('.warp/h2/channels.json')):
    print(r['device'])
PY
sort -u /tmp/asip-devices.txt | while read -r dev; do
    d=\$PWD/.warp/\$dev
    mkdir -p \"\$d\"
    [ -f \"\$d/config.json\" ] && continue
    ( cd \"\$d\" && timeout 90 env -u http_proxy -u https_proxy \
        -u HTTP_PROXY -u HTTPS_PROXY \"\$BIN\" register --accept-tos -n \"asip-\$dev\" ) \
        >/dev/null 2>&1 && chmod 600 \"\$d/config.json\" && echo \"  + \$dev\" || echo \"  ✗ \$dev 注册失败\"
    sleep 6
done
echo '  设备就绪'" || true
fi

# ---------------------------------------------------------------- 5. 单元
say "5/7 生成并安装单元"
# 三个渲染器各管一半，顺序不能换：
#   parse_subscription → 主配置（含 dns-airport，H2 出站解析机场域名要用）
#   wg-lines-install   → WG 单元
#   h2-install         → H2 单元（会先跑 verify + sing-box check）
run "cd $REMOTE_DIR
set -a; . ./.env; set +a
export HOME=$REMOTE_HOME
# 权限：warp-outbound.json 历史上被 root 写过，members.py 随后写不进去。
sudo chown -R $REMOTE_USER:$REMOTE_USER .warp 2>/dev/null || true

echo '  · 主配置'
python3 parse_subscription.py >/dev/null 2>&1 || echo '    ⚠ parse_subscription 失败（订阅 token 可能未配）'
echo '  · warp-outbound'
python3 deploy/members.py --write-warp-outbound >/dev/null

echo '  · WG 线'
sudo env HOME=$REMOTE_HOME WARP_WG_FAMILIES=\"\$WARP_WG_FAMILIES\" \
     WARP_WG_NODES=\"\$WARP_WG_NODES\" ASIP_WARP_BLOCKS=\"\$ASIP_WARP_BLOCKS\" \
     ASIP_WARP_BLOCK_CHANNELS=\"\$ASIP_WARP_BLOCK_CHANNELS\" PYTHON=/usr/bin/python3 \
     ./deploy/wg-lines-install.sh 2>&1 | tail -3

echo '  · H2 线'
sudo env HOME=$REMOTE_HOME ASIP_WARP_BLOCKS=\"\$ASIP_WARP_BLOCKS\" \
     ASIP_WARP_BLOCK_CHANNELS=\"\$ASIP_WARP_BLOCK_CHANNELS\" PYTHON=/usr/bin/python3 \
     ./deploy/h2-install.sh 2>&1 | tail -4

echo '  · asip-unit 白名单'
sudo install -m 0755 deploy/asip-unit /usr/local/bin/asip-unit
for u in usque-h2-0.service warp-wg-1.service usque-warp.service; do
    sudo /usr/local/bin/asip-unit is-active \$u >/dev/null 2>&1 \
        && echo \"    ✓ \$u 在白名单\" || echo \"    ✗ \$u 被拒\"
done"

# ---------------------------------------------------------------- 6. 重启
say "6/7 重启服务（主 sing-box 由 daemon 拉起，必须重启 daemon 才重载配置）"
run "sudo systemctl daemon-reload
sudo systemctl restart auto-switch-ip.service
sleep 12
systemctl is-active auto-switch-ip.service"

# ---------------------------------------------------------------- 7. 验收
say "7/7 验收"
run "cd $REMOTE_DIR
echo '--- 单元 ---'
for pat in 'usque-warp*.service' 'usque-h2-*.service' 'warp-wg-*.service'; do
    printf '  %-24s %s active\n' \"\$pat\" \"\$(systemctl list-units --no-legend \"\$pat\" --state=active | wc -l)\"
done
printf '  %-24s %s\n' asip-h2-singbox \"\$(systemctl is-active asip-h2-singbox.service)\"
printf '  %-24s %s\n' nine-router-src \"\$(systemctl is-active nine-router-src.service 2>/dev/null)\"
echo '--- 硬约束 ---'
printf '  warp-svc: %s / %s\n' \"\$(systemctl is-active warp-svc 2>&1)\" \"\$(systemctl is-enabled warp-svc 2>&1)\"
echo '--- selector ---'
S=\$(cat $REMOTE_HOME/.singbox_secret 2>/dev/null)
curl -s http://127.0.0.1:9090/proxies/opencode-out -H \"Authorization: Bearer \$S\" \
  | python3 -c 'import json,sys
d = json.load(sys.stdin)
print(\"  成员 %d 条，当前 %s\" % (len(d.get(\"all\") or []), d.get(\"now\")))' 2>/dev/null \
  || echo '  读不到 selector'
echo '--- 出口抽样 ---'
for p in \$(seq 1110 1112); do
    r=\$(timeout 8 curl -s --socks5-hostname 127.0.0.1:\$p https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -E '^(ip|warp)=' | tr '\\n' ' ')
    echo \"  \$p \${r:-fail}\"
done
echo '--- daemon 告警 ---'
sudo journalctl -u auto-switch-ip.service --since '60 seconds ago' --no-pager 2>/dev/null \
  | grep -E '⚠|不在白名单|不可达' | grep -v 'sudo\\[' | tail -5 || true"

say "完成"
echo "远端: $TARGET:$REMOTE_DIR"
echo "提示：共享池的可用率随时段变化，深夜验收数字偏低是正常现象"
echo "      （见 docs/established-facts.md 第 0 条与第 24 条）。"
