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
# 与 config.py 一致：从 passwd 取本用户家目录，不信 $HOME。`sudo -u` 下
# HOME 可能仍是 /root，会把单元/密钥写到错误用户的家目录。
# ASIP_HOME_DIR 供试跑/测试隔离，避免一次演练把设置与入口装进真人目录。
# SYSTEMD_DIR 指到 /etc/systemd 之外即视为演练：此时家目录不跟过去的话，演练
# 会顺手改写生产的 cli.env 与 ~/.local/bin/asip（实测真的发生过，md5 变了）。
# 这三样（设置/入口/密钥）全由 HOME_DIR 推导，隔离住它一处就够。
if [ -z "${ASIP_HOME_DIR:-}" ] && [ -n "${SYSTEMD_DIR:-}" ]; then
    case "$SYSTEMD_DIR" in
        /etc/systemd/*) ;;
        *) ASIP_HOME_DIR="$(dirname "$SYSTEMD_DIR")/home" ;;
    esac
fi
HOME_DIR="${ASIP_HOME_DIR:-$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6 || true)}"
[ -n "$HOME_DIR" ] || HOME_DIR="$HOME"
mkdir -p "$HOME_DIR"
UNIT_DIR="${SYSTEMD_DIR:-$HOME_DIR/.config/systemd/user}"
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

MEMBERS="$("$PYBIN" "$DIR/deploy/members.py")"

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
            ( cd "$DIR/.warp/$dir" && "$DIR/.warp/usque/usque" register --accept-tos -n "$tag" ) \
                || { echo "⚠ $tag 注册失败，该单元启动后会立即退出" >&2; REGISTER_FAILED=$((REGISTER_FAILED + 1)); }
        fi
        chmod 600 "$DIR/.warp/$dir/config.json" 2>/dev/null || true
    done <<< "$MEMBERS"
fi

# ---- 单元文件：一律绝对路径 + 当前用户 ----
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/auto-switch-ip.service" <<EOF
[Unit]
Description=auto-switch-ip daemon — monitor 9router errors + rotate sing-box proxy nodes
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
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
WantedBy=default.target
EOF

cat > "$UNIT_DIR/asi-selfcheck.service" <<EOF
[Unit]
Description=auto-switch-ip 自检引擎 — 巡检并写入 .monitor_state.json
After=default.target

[Service]
Type=oneshot
ExecStart=$PYBIN $DIR/self_evolve_checkpoint.py
WorkingDirectory=$DIR
EnvironmentFile=-$DIR/.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/asi-selfcheck.timer" <<'EOF'
[Unit]
Description=auto-switch-ip 自检定时器 — 每 10 分钟触发 asi-selfcheck.service

[Timer]
OnCalendar=*:0/10
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=default.target
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
WorkingDirectory=$DIR/.warp/$dir
ExecStart=$DIR/deploy/usque-launch.sh $port $fam
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
done <<< "$MEMBERS"

# 单元写到哪就 reload 哪：本机生产单元在 /etc（系统级），对空着的用户级目录
# daemon-reload 打空；无 user bus 的容器/CI 里它还会直接失败（No such file or directory）。
case "$UNIT_DIR" in
    /etc/systemd/system|/usr/lib/systemd/system) : ;;   # 系统级：本次不 reload，避免影响在跑的服务
    *) systemctl --user daemon-reload 2>/dev/null || true ;;
esac
if [ "$UNITS_ONLY" = 0 ]; then
    systemctl --user enable --now auto-switch-ip.service asi-selfcheck.timer $SERVICES
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
