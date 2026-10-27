#!/bin/bash
# 安装 9Router（oahcom fork，带 free-lane 提交）并注册 systemd 用户单元。
#
#   ./deploy/install-9router.sh          # 幂等：已装好就跳过，缺什么补什么
#   ./deploy/install-9router.sh --force  # 强制重新 build
#   NODE_VERSION=22.23.4 ./deploy/install-9router.sh
#   NINE_ROUTER_SRC=/path/to/checkout ./deploy/install-9router.sh
#
# 为什么是 fork 不是 npm 上的 9router：上游 master 没有 free-lane 那 6 个提交
# （<env> 块、每出口一个会话、429 轮换、Bun 转发），asip 的 probe_warp 依赖它们。
# 装上游会让模型层探测恒 403。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${NINE_ROUTER_SRC:-$HOME/9router}"
REPO="${NINE_ROUTER_REPO:-https://github.com/oahcom/9router.git}"
UNIT_DIR="${SYSTEMD_DIR:-$HOME/.config/systemd/user}"
SVC=nine-router-src.service
PORT="${NINE_ROUTER_PORT:-20127}"
# 与 fork 的 .nvmrc 一致。只在系统 node 太旧或缺失时下载。
NODE_VERSION="${NODE_VERSION:-22.23.4}"
NODE_DIR="${NODE_DIR:-$HOME/.local/node-v$NODE_VERSION}"

FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    esac
done

log() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

# ---- 1. Node：系统有 >=18 且架构对就用它，否则下到 ~/.local ----
node_ok() { [ -x "$1" ] && "$1" -e 'process.exit(+process.versions.node.split(".")[0] >= 18 ? 0 : 1)' 2>/dev/null; }

NODE_BIN=""
# 探测顺序：PATH → 本脚本以前的下载目录 → 手工装到 /opt 的 → nvm。
# 只看 PATH 会让「已经装好的 node」被当成没装，白下一次 30MB（实测卡在
# nodejs.org 直到超时）。
for cand in     "$(command -v node 2>/dev/null)"     "$NODE_DIR/bin/node"     /opt/node-*/bin/node     "$HOME"/.nvm/versions/node/*/bin/node     "$HOME"/.local/node-*/bin/node
do
    [ -n "$cand" ] && node_ok "$cand" && { NODE_BIN="$cand"; break; }
done
if [ -n "$NODE_BIN" ]; then
    log "✓ 用已有 node $("$NODE_BIN" -v)（$NODE_BIN）"
else
    case "$(uname -m)" in
        x86_64|amd64) NARCH=x64 ;;
        aarch64|arm64) NARCH=arm64 ;;
        *) die "不支持的架构：$(uname -m)" ;;
    esac
    case "$(uname -s)" in
        Linux) PLAT=linux ;;
        *) die "本脚本只处理 Linux（WSL2 可用）；$(uname -s) 请自行安装 node >= 18" ;;
    esac
    TARBALL="node-v$NODE_VERSION-$PLAT-$NARCH.tar.xz"
    # nodejs.org 在国内常被墙；与 fetch-binaries.sh 同样的镜像前缀思路。
    MIRROR="${NODE_MIRROR:-https://nodejs.org/dist}"
    log "↓ node $NODE_VERSION（$NODE_BIN 缺失或 <18）"
    TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
    curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 20 --max-time 600 \
        -o "$TMP/$TARBALL" "$MIRROR/v$NODE_VERSION/$TARBALL" \
        || die "下载失败：$MIRROR/v$NODE_VERSION/$TARBALL（用 NODE_MIRROR= 指镜像）"
    mkdir -p "$(dirname "$NODE_DIR")"
    rm -rf "$NODE_DIR"
    tar -xJf "$TMP/$TARBALL" -C "$TMP"
    mv "$TMP/node-v$NODE_VERSION-$PLAT-$NARCH" "$NODE_DIR"
    NODE_BIN="$NODE_DIR/bin/node"
    node_ok "$NODE_BIN" || die "$NODE_BIN 不可用"
    log "✓ 已安装 node $("$NODE_BIN" -v) -> $NODE_DIR"
fi
NPM_BIN="$(dirname "$NODE_BIN")/npm"
[ -x "$NPM_BIN" ] || NPM_BIN="$(dirname "$NODE_BIN")/npx"   # corepack 环境兜底
[ -x "$NPM_BIN" ] || die "找不到同目录的 npm（$NODE_BIN 旁边）"

# ---- 2. 源码 ----
# 只用 shallow clone：完整历史 132MB，安装只需要一个工作树。
if [ -d "$SRC/.git" ]; then
    log "✓ 源码已存在：$SRC"
    if [ "$FORCE" = 1 ] || [ ! -f "$SRC/package.json" ]; then
        git -C "$SRC" fetch --depth 1 origin 2>/dev/null || log "  ⚠ fetch 失败，用现有工作树"
    fi
else
    log "↓ clone $REPO -> $SRC"
    rm -rf "$SRC"
    git clone --depth 1 "$REPO" "$SRC" || die "clone 失败（GitHub 不可达时先设 https_proxy）"
fi
[ -f "$SRC/package.json" ] || die "$SRC 不像 9router 仓库"

# free-lane 提交是本项目的前提，装到上游会让模型层探测恒 403。
if ! grep -q 'OPENCODE_ENV_MARKER' "$SRC/open-sse/utils/opencodeFingerprint.js" 2>/dev/null; then
    die "$SRC 缺 free-lane 提交（没有 OPENCODE_ENV_MARKER）—— 请用 oahcom/9router，不要用上游"
fi

# ---- 3. 依赖 + 构建 ----
# npm install 5 分钟、build 3~5 分钟，都不需要每次跑。用产物时间戳判断。
BUILT="$SRC/.next/standalone/server.js"
if [ "$FORCE" = 1 ] || [ ! -f "$BUILT" ] || [ "$SRC/package.json" -nt "$BUILT" ]; then
    command -v make >/dev/null || log "  ⚠ 没装 make/g++ 时 better-sqlite3 可能退化成预编译包"
    log "↓ npm install（约 5 分钟）"
    ( cd "$SRC" && PATH="$(dirname "$NODE_BIN"):$PATH" "$NPM_BIN" install --no-audit --no-fund ) \
        || die "npm install 失败"
    log "↓ npm run build（约 3-5 分钟）"
    ( cd "$SRC" && PATH="$(dirname "$NODE_BIN"):$PATH" NEXT_TELEMETRY_DISABLED=1 "$NPM_BIN" run build ) \
        || die "npm run build 失败"
else
    log "✓ 构建产物已是最新（--force 可强制重建）"
fi

# ---- 4. unit ----
# DATA_DIR 不设 = 9router 自己取 ~/.9router，正好是 asip 的默认 db 路径。
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$SVC" <<EOF
[Unit]
Description=9Router (source build) — AI router / token saver
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SRC
# bun 必须在 PATH 里：[OpenCodeTransport] 用 subprocess 直接 spawn bun，
# ENOENT 时它退回直连 —— 流量绕过 sing-box，出口是真实 IP，必定 429。
Environment=PATH=$HOME/.bun/bin:$(dirname "$NODE_BIN"):/usr/local/bin:/usr/bin:/bin
Environment=NODE_ENV=production
Environment=NEXT_TELEMETRY_DISABLED=1
# 冷却按 proxy URL 记账（egress.js 的 hashEgress("proxy", proxyUrl)），而 asip
# 换的是同一个 URL 后面的 WARP 出口。默认上限 24h 会把 asip 的换线整段罩死：
# 请求根本不出门，9router 本地合成 429，asip 读到 429 再换线 —— 死循环。
# 压到 2s，让每个请求都真的发往 asip 当下指的那个出口。
Environment=EGRESS_MAX_COOLDOWN_MS=2000
ExecStart=$NODE_BIN $SRC/custom-server.js --port $PORT
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$SVC"

# ---- 5. 等端口起来 ----
# 只认 HTTP 200/307：进程活着不等于 router 就绪（Next 编译首帧要几百 ms）。
for i in $(seq 1 30); do
    code="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$PORT/login" || true)"
    case "$code" in
        200|30*) log "✓ 9router 就绪：http://127.0.0.1:$PORT  (unit=$SVC)"; exit 0 ;;
    esac
    sleep 1
done
systemctl --user --no-pager status "$SVC" | head -15 >&2 || true
die "9router 在 30s 内没起来（journalctl --user -u $SVC -n 50）"
