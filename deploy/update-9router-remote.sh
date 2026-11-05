#!/bin/bash
# 在远端主机上从 git 重建 9Router（不改 unit、不改端口）。
#
#   ./deploy/update-9router-remote.sh                 # 默认 ~/9router -> /opt/9router
#   SRC=~/src/9router TARGET=/opt/9router ./deploy/update-9router-remote.sh
#   BRANCH=master-oa ./deploy/update-9router-remote.sh
#
# WHY 单独一个脚本，而不是复用 install-9router.sh：那个脚本会**重写 unit**，
# 而远端 unit 里的端口（PORT=38731）、PATH、INITIAL_PASSWORD 与本机不同。
# 重写等于把生产换个端口起不来。这里只替换代码与构建产物。
#
# WHY 必须先 npm install 再 build：.next 是编译产物，改了 open-sse/ 下的源码
# 不重建等于没改。实测「代码是新的、跑的是旧的」正是 429 轮换从没触发的原因。
set -euo pipefail

SRC="${SRC:-$HOME/9router}"
TARGET="${TARGET:-/opt/9router}"
BRANCH="${BRANCH:-master-oa}"
SVC="${SVC:-nine-router-src.service}"
log() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

[ -d "$SRC/.git" ] || die "$SRC 不是 git 仓库；先 clone"
cd "$SRC"
git fetch origin -q
git checkout -q "$BRANCH"
git reset -q --hard "origin/$BRANCH"
log "✓ 源码 $BRANCH @ $(git log --oneline -1)"

[ -f package.json ] || die "$SRC/package.json 缺失"
log "↓ npm install"
npm install --no-audit --no-fund >/tmp/nr-install.log 2>&1 || { tail -20 /tmp/nr-install.log; die "npm install 失败"; }
log "↓ npm run build"
NEXT_TELEMETRY_DISABLED=1 npm run build >/tmp/nr-build.log 2>&1 || { tail -30 /tmp/nr-build.log; die "build 失败"; }
[ -f .next/BUILD_ID ] || die "构建产物缺失（.next/BUILD_ID）"
log "✓ 构建完成 BUILD_ID=$(cat .next/BUILD_ID)"

# 备份后整体替换。数据在 ~/.9router，不在这里，可放心 --delete。
BAK="/opt/9router.bak-$(date +%s)"
sudo cp -a "$TARGET" "$BAK"
log "✓ 已备份到 $BAK"
sudo rsync -a --delete "$SRC"/ "$TARGET"/ --exclude=.git
sudo chown -R "$(id -un):$(id -gn)" "$TARGET"
sudo systemctl restart "$SVC"
log "✓ 已重启 $SVC；旧版本保留在 $BAK（确认无误后自行删除）"
