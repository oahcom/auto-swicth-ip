#!/bin/bash
# 部署 auto-switch-ip：生成并启用 systemd 用户单元。
#
# 单元内容全部从本仓库推导（项目目录、config.py 的 warp.members），
# 所以 clone 到任何路径、任何用户都能直接跑，不必手改 20 个 unit 文件。
#
#   ./deploy/install.sh               # 全量：密钥 + 注册隧道 + 单元 + 启用
#   ./deploy/install.sh --units-only  # 只重生成单元（改 members 后）
#   SYSTEMD_DIR=/tmp/x ./deploy/install.sh --units-only   # 试跑，不碰真实单元
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 与 config.py 一致：从 passwd 取本用户家目录，不信 $HOME。sudo -u 下
# HOME 可能仍是 /root，会把密钥写到错误用户的家目录。
if [ -z ${ASIP_HOME_DIR:-} ] && [ -n ${SYSTEMD_DIR:-} ]; then
    case $SYSTEMD_DIR in
        /etc/systemd/*) ;;
        *) ASIP_HOME_DIR=$(dirname $SYSTEMD_DIR)/home ;;
    esac
fi
HOME_DIR=${ASIP_HOME_DIR:-$(getent passwd $(id -u) 2>/dev/null | cut -d: -f6 || true)}
[ -n $HOME_DIR ] || HOME_DIR=$HOME
mkdir -p $HOME_DIR
# 本机是系统级部署：默认写 /etc/systemd/system，而不是用户级目录。
UNIT_DIR=${SYSTEMD_DIR:-/etc/systemd/system}
# 写进 $UNIT_DIR 的单元会把 ExecStart 钉成**本次运行的这个 DIR**。所以拿一份副本
# 试跑（如 /tmp 下的 clone）时若忘了设 SYSTEMD_DIR，就会把现网单元悄悄改指到副本，
# 而副本随时可被清理 —— 服务下次重启即崩。真实事故：`--units-only` 在 /tmp 副本
# 里跑过一次，23 个单元全被改指 /tmp/xxx，直到有人发现。
# 首次部署时单元文件尚不存在：pipefail 下 sed 的非零退出会让 set -e 直接中止。
cur="$(sed -n 's/^WorkingDirectory=//p' "$UNIT_DIR/auto-switch-ip.service" 2>/dev/null | head -1 || true)"
if [ -z "${SYSTEMD_DIR:-}" ] && [ -n "$cur" ] && [ "$cur" != "$DIR" ] && [ -f "$cur/daemon.py" ]; then
    echo "✗ 现网单元当前指向 $cur，而本次运行的是 $DIR —— 拒绝覆盖生产单元" >&2
    echo "  试跑请设 SYSTEMD_DIR；确认要迁移到本目录请加 --adopt" >&2
    [ "${ADOPT:-0}" = 1 ] || exit 1
fi
# 单元里的解释器写绝对路径，所以必须钉死"给这份代码用的那个 python"，不能是
# 临时 shell 里碰巧在 PATH 首位的那个（conda 用户会拿到 miniforge，重启后不同）。
# 单元以哪个用户跑。必须是该仓库的属主：daemon 要读写 .warp/.env/日志，
# 而 config.py 的家目录也是按 uid 推的。
RUN_USER="${ASIP_RUN_USER:-$(id -un)}"
PYBIN="${PYTHON:-/usr/bin/python3}"
[ -x "$PYBIN" ] || { echo "✗ 解释器不存在：$PYBIN（用 PYTHON=... 指定）" >&2; exit 1; }
UNITS_ONLY=0; ADOPT=0
for arg in "$@"; do
    case "$arg" in
        --units-only) UNITS_ONLY=1 ;;
        --adopt)      ADOPT=1 ;;
    esac
done

# ---- 新主机前置检查：硬项失败即退出，软项只警告 ----
# 试跑/无 systemd 的沙箱可 SKIP_PREFLIGHT=1 跳过。
if [ "${SKIP_PREFLIGHT:-0}" != 1 ]; then
    if [ "$UNITS_ONLY" = 1 ]; then
        PYTHON="$PYBIN" bash "$DIR/deploy/preflight.sh" --units-only || exit 1
    else
        PYTHON="$PYBIN" bash "$DIR/deploy/preflight.sh" || exit 1
    fi
fi

# ---- 二进制：架构不符或缺失就由 fetch 脚本按官方 sha256 补 ----
# 仓库里提交的 sing-box 是 amd64 构建。arm64 机器 clone 下来能 `-x` 通过，但一跑
# 就是 Exec format error —— 单看文件存在性会漏，必须核对本机架构。
case "$(uname -m)" in
    aarch64|arm64) WANT_ELF=b7 ;;   # ELF e_machine @18: arm64=0xb7
    *)             WANT_ELF=3e ;;   #                    amd64=0x3e
esac

# binary_ok <路径>：存在、非链接、且 ELF 架构与本机一致
binary_ok() {
    [ -f "$1" ] && [ ! -L "$1" ] || return 1
    [ "$(od -An -tx1 -j18 -N1 "$1" 2>/dev/null | tr -d ' ')" = "$WANT_ELF" ]
}

needs_fetch=0
binary_ok "$DIR/sing-box" || needs_fetch=1
if [ "$UNITS_ONLY" = 0 ]; then
    binary_ok "$DIR/.warp/usque/usque" || needs_fetch=1
fi
if [ "$needs_fetch" = 1 ] && [ "$UNITS_ONLY" = 0 ]; then
    echo "→ 二进制缺失或架构不符，调用 fetch-binaries.sh"
    bash "$DIR/deploy/fetch-binaries.sh" || exit 1
fi

# ---- 前置检查：缺什么报什么，别等单元起不来才发现 ----
fail=0
"$PYBIN" -c "import requests, yaml" 2>/dev/null \
    || { echo "✗ 缺少 Python 依赖：$PYBIN -m pip install -r $DIR/requirements.txt" >&2; fail=1; }
[ -x "$DIR/sing-box" ] \
    || { echo "✗ $DIR/sing-box 不存在或不可执行（用 deploy/fetch-binaries.sh 获取）" >&2; fail=1; }
[ "$UNITS_ONLY" = 1 ] || [ -x "$DIR/.warp/usque/usque" ] \
    || { echo "✗ 缺少 $DIR/.warp/usque/usque（用 deploy/fetch-binaries.sh 获取）" >&2; fail=1; }
# 单元 ExecStart 指向包装脚本：它不可执行时 20 条隧道全部起不来。
[ -x "$DIR/deploy/usque-launch.sh" ] \
    || { echo "✗ $DIR/deploy/usque-launch.sh 不存在或不可执行（单元 ExecStart 依赖它）" >&2; fail=1; }
[ "$fail" = 0 ] || exit 1

# ---- bun：probe_warp 的探测器 ----
# 缺 bun 时 probe_warp 一律返回 noprobe：不是「这条线不能用」，而是「没有探测
# 能力」。daemon 会退回历史判据，段级静置就拦不住已烧毁的 /64 段，每个新 /128
# 都要白烧一格配额才被认出来（实测 94% 判 quota）。装不上不阻塞部署，只告警。
BUN_BIN=""
for c in "${PROBE_BUN_BIN:-}" "$(command -v bun 2>/dev/null)" "$HOME/.bun/bin/bun"; do
    [ -n "$c" ] && [ -x "$c" ] && { BUN_BIN="$c"; break; }
done
if [ -z "$BUN_BIN" ] && command -v npm >/dev/null 2>&1; then
    echo "→ 未找到 bun，尝试 npm i -g bun（probe_warp 的探测器）"
    npm i -g bun --no-audit --no-fund >/dev/null 2>&1 || true
    [ -x "$HOME/.bun/bin/bun" ] && BUN_BIN="$HOME/.bun/bin/bun"
    [ -z "$BUN_BIN" ] && BUN_BIN="$(command -v bun 2>/dev/null || true)"
fi
if [ -n "$BUN_BIN" ]; then
    # systemd EnvironmentFile 不做变量展开，必须写绝对路径。
    grep -q '^PROBE_BUN_BIN=' "$DIR/.env" 2>/dev/null \
        || echo "PROBE_BUN_BIN=$BUN_BIN" >> "$DIR/.env"
    echo "✓ bun: $BUN_BIN"
else
    echo "⚠ 未装 bun：probe_warp 会退化成 noprobe（只按历史判据选线，白烧配额）" >&2
    echo "  装：npm i -g bun，或 curl -fsSL https://bun.sh/install | bash" >&2
fi

# 只收 5 字段的行：config.py 在 import 期会打告警，一旦漏到 stdout（历史上确有），
# while read 会把它当节点行，凭空建出 .warp/<告警文本>/ 目录和一个非法单元名。

# ---- 9Router：asip 的观测与出口都依赖它 ----------------

# asip 有两种活法：没有 9router 时只能 blind（按时间盲换线），有它才能按，

# requestDetails 的 403/429 换线。装它不该是让人自己去翻文档的额外步骤。

# 已装好时脚本幂等跳过；ASIP_SKIP_9ROUTER=1 可跳过（只想要 sing-box + WARP 的场景）。

if [ "${ASIP_SKIP_9ROUTER:-0}" != 1 ]; then

    bash "$DIR/deploy/install-9router.sh" || {

        echo "⚠ 9router 安装失败 —— asip 仍可运行，但只能 blind 模式（按时间换线）" >&2

        echo "  装好后把 .env 里的 ASIP_OBSERVE_9ROUTER 改成 true 并重启 daemon" >&2

    }
    # 装完还得初始化：CLI token / proxyPool 绑定 / enableObservability。
    # 缺任何一样，9router 都是「在跑但 asip 什么都测不到」的空壳 —— 尤其
    # enableObservability 默认 false，requestDetails 不落行，错误率恒为 0/0。
    bash "$DIR/deploy/init-9router.sh" || {
        echo "⚠ 9router 初始化失败 —— asip 会读不到 requestDetails" >&2
        echo "  重跑：$DIR/deploy/init-9router.sh" >&2
    }

fi

MEMBERS="$("$PYBIN" "$DIR/deploy/members.py" 2>/dev/null | awk 'NF==5')"
[ -n "$MEMBERS" ] || { echo "✗ members.py 没有输出任何节点" >&2; exit 1; }

# .warp/warp-outbound.json 是 parse_subscription 生成 WARP 出口的唯一依据，且被
# gitignore。缺了它配置里不会有 opencode-out，daemon 全部换线打空 —— 从 config 重建。
[ -f "$DIR/.warp/warp-outbound.json" ] \
    || { "$PYBIN" "$DIR/deploy/members.py" --write-warp-outbound >/dev/null \
         && echo "已生成 .warp/warp-outbound.json"; }

# ---- 密钥：缺失时生成，绝不覆盖已有的（改它会让正在跑的 sing-box 掉线）----
SECRET_FILE="${SINGBOX_SECRET_FILE_PATH:-$HOME_DIR/.singbox_secret}"
if [ ! -f "$SECRET_FILE" ]; then
    (umask 077; "$PYBIN" -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())" > "$SECRET_FILE")
    echo "已生成 $SECRET_FILE"
fi

# ---- WARP 隧道：每条线一个独立注册的设备（凭据 gitignored）----
if [ "$UNITS_ONLY" = 0 ]; then
    REGISTER_FAILED=0
    while read -r tag _ _ dir _; do
        mkdir -p "$DIR/.warp/$dir"
        # 每条隧道一个工作目录（各存自己的 config.json = 自己的 WARP 身份），
        # 二进制共用主目录那份。通配 `.warp/usque*/` **包含主目录自身**，照着
        # 做会把真实二进制换成指向自己的链接 → 全部 20 条隧道 ELOOP 起不来。
        [ "$dir" = "usque" ] || ln -sfn ../usque/usque "$DIR/.warp/$dir/usque"
        if [ ! -f "$DIR/.warp/$dir/config.json" ]; then
            echo "注册隧道 $tag → .warp/$dir"
            # 注册必须直连：本机 export 了 http(s)_proxy（如 okz）时，usque 会走代理
            # 导致 Cloudflare 返回非 JSON 错误页（failed to parse error response）。
            ( cd "$DIR/.warp/$dir" && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
                "$DIR/.warp/usque/usque" register --accept-tos -n "$tag" ) \
                || { echo "⚠ $tag 注册失败，该单元启动后会立即退出" >&2; REGISTER_FAILED=$((REGISTER_FAILED + 1)); }
        fi
        chmod 600 "$DIR/.warp/$dir/config.json" 2>/dev/null || true
    done <<< "$MEMBERS"
fi

# ---- 单元文件：系统级 ----
# 写 /etc/systemd/system 需要 root；本脚本自身以 administrator 跑（家目录、
# .warp 属主都在它名下），只在写单元与 systemctl 这几步提权。
need_root() {
    if [ "$(id -u)" = 0 ]; then "$@"; else sudo -n "$@" || { echo "✗ 需要 root：$*" >&2; exit 1; }; fi
}
need_root mkdir -p "$UNIT_DIR"

# ---- 特权包装：daemon 以非 root 运行，但要重启系统级单元 ----
# 见 deploy/asip-unit 的注释：sudoers 不允许命令参数带通配符，白名单只能
# 收在脚本里；polkit 在本机 inactive，也不适合给无交互服务进程配规则。
need_root install -m 0755 "$DIR/deploy/asip-unit" /usr/local/bin/asip-unit
SUDOERS_FILE=/etc/sudoers.d/auto-switch-ip
if [ ! -f "$SUDOERS_FILE" ]; then
    printf '# 由 deploy/install.sh 生成。只放行 asip-unit 一个入口，白名单在它内部。\n%s ALL=(root) NOPASSWD: /usr/local/bin/asip-unit\n' "$RUN_USER"         | need_root tee "$SUDOERS_FILE" >/dev/null
    need_root chmod 0440 "$SUDOERS_FILE"
    need_root visudo -c -f "$SUDOERS_FILE" >/dev/null         || { echo "✗ $SUDOERS_FILE 语法错误" >&2; exit 1; }
fi
cat > "$UNIT_DIR/auto-switch-ip.service" <<EOF
[Unit]
Description=auto-switch-ip daemon — monitor 9router errors + rotate sing-box proxy nodes
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# 以 administrator 而非 root 运行：config.py 用 pwd.getpwuid(os.getuid()).pw_dir
# 取家目录，root 会得到 /root，9router 的库、.env、.warp 全部找不到。
# 代价是重启系统级单元要提权，走 /usr/local/bin/asip-unit（sudoers 白名单）。
User=$RUN_USER
Group=$RUN_USER
ExecStart=$PYBIN $DIR/daemon.py
WorkingDirectory=$DIR
Restart=always
RestartSec=10
# sing-box 由 daemon 用 Popen(start_new_session=True) 拉起，仍留在本 unit 的 cgroup 里。
# 默认 KillMode=control-group 会让每次 restart 连带杀掉 sing-box → daemon 置 bypass 直连
# → opencode 看到本地 IP 直接 403。实测三次重启各触发 ~50-100 条 403。
KillMode=process
EnvironmentFile=-$DIR/.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > "$UNIT_DIR/asi-selfcheck.service" <<EOF
[Unit]
Description=auto-switch-ip 自检引擎 — 巡检并写入 .monitor_state.json
After=multi-user.target

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
ExecStart=$PYBIN $DIR/self_evolve_checkpoint.py
WorkingDirectory=$DIR
EnvironmentFile=-$DIR/.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > "$UNIT_DIR/asi-selfcheck.timer" <<'EOF'
[Unit]
Description=auto-switch-ip 自检定时器 — 每 10 分钟触发 asi-selfcheck.service

[Timer]
OnCalendar=*:0/10
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=multi-user.target
EOF

# 单元名必须与 config.py 的 warp.members[].service 一致，否则 daemon 的
# systemctl restart 会打空。
SERVICES=""
while read -r tag port svc dir fam; do
    SERVICES="$SERVICES $svc"
    # fam 原样传给 usque-launch.sh（"-" = 只要 v4），由它在启动时按本机路由决定
    # 实际地址族。不在渲染期定死：同一份单元要在有/没有 IPv6 的网络上都能用。
    cat > "$UNIT_DIR/$svc" <<EOF
[Unit]
Description=Cloudflare WARP (MASQUE) SOCKS5 proxy $tag for auto-switch-ip
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$DIR/.warp/$dir
ExecStart=$DIR/deploy/usque-launch.sh $port $fam
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
done <<< "$MEMBERS"

# 单元写到哪就 reload 哪：本机生产单元在 /etc（系统级），对空着的用户级目录
# daemon-reload 打空；无 user bus 的容器/CI 里它还会直接失败（No such file or directory）。
case "$UNIT_DIR" in
    /etc/systemd/system|/usr/lib/systemd/system) : ;;   # 系统级：本次不 reload，避免影响在跑的服务
    *) systemctl --user daemon-reload 2>/dev/null || true ;;
esac
if [ "$UNITS_ONLY" = 0 ]; then
    need_root systemctl enable --now auto-switch-ip.service asi-selfcheck.timer $SERVICES
    echo "已启用 auto-switch-ip + asi-selfcheck.timer +$(echo $SERVICES | wc -w)条隧道单元"
    # 注册失败的隧道单元会立刻退出并无限重启：宁可让部署返回失败，也别让人以为成了。
    [ "$REGISTER_FAILED" = 0 ] || { echo "✗ $REGISTER_FAILED 条隧道注册失败，见上方 ⚠" >&2; exit 1; }
else
    echo "已重生成单元（未启用/未重启）：$UNIT_DIR"
fi

# ---- asip 运维 CLI：设置由 config.py 推导，入口落到 PATH ----
# 必须先于任何手工排障：CLI 读的是设置文件里的路径/端口，写死一份到脚本里的
# 下场是换目录或改 members 后它静默指向旧位置（症状像服务故障，不像配置错误）。
# SYSTEMD_DIR 由调用方传给本脚本，conf 脚本继承同一份环境变量，据此把设置指向
# 演练目录而非 /etc —— 否则演练完跑一次 `asip warp stop` 就动了生产单元。
bash "$DIR/deploy/asip-cli-conf.sh" || { echo "✗ 生成 asip 设置失败" >&2; exit 1; }
BIN_DIR="${ASIP_BIN_DIR:-$HOME_DIR/.local/bin}"
mkdir -p "$BIN_DIR"
ln -sfn "$DIR/deploy/asip-cli.sh" "$BIN_DIR/asip"
echo "asip CLI: $BIN_DIR/asip → $DIR/deploy/asip-cli.sh"
