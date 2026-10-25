#!/bin/bash
# 下载本项目的两个外部二进制并按官方 sha256 校验。
#
#   ./deploy/fetch-binaries.sh            # 缺什么补什么，已存在则跳过
#   ./deploy/fetch-binaries.sh --force    # 全部重下
#   BIN_MIRROR=https://mirror.example ./deploy/fetch-binaries.sh
#
# 校验和钉死在下面的表里，来自各项目的 GitHub release digest。改版本要同时改
# 版本号与 sha256 —— 只有两者都对得上才落地。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1
# 本机到 GitHub release 常被墙（api.github.com 通、release 下载超时），可用镜像前缀。
MIRROR="${BIN_MIRROR:-https://github.com}"
PYBIN="${PYTHON:-/usr/bin/python3}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SHA256_ALL=(
  # sing-box 1.13.0 —— label|url-suffix|sha256
  "sing-box-linux-amd64|SagerNet/sing-box/releases/download/v1.13.0/sing-box-1.13.0-linux-amd64.tar.gz|86db0f9df3f822ca2adccde2f7c1c9e21d64e646a77bf258274fde3be399025b"
  "sing-box-linux-arm64|SagerNet/sing-box/releases/download/v1.13.0/sing-box-1.13.0-linux-arm64.tar.gz|57efdb8a256ae0736fa00aee5538c384ee50b40882075d2d1a05a1c3d7c4a889"
  # usque 4.2.1（MASQUE 客户端）
  "usque-linux-amd64|Diniboy1123/usque/releases/download/v4.2.1/usque_4.2.1_linux_amd64.zip|4117e20695078af9c11edecd1a826c009bbc7ea0b7f64458612b4198910bc313"
  "usque-linux-arm64|Diniboy1123/usque/releases/download/v4.2.1/usque_4.2.1_linux_arm64.zip|c88b061c2a567f30813d7505637d1fb6fe7ec5898b5b61fd05122409fa5ad925"
)

arch="$(uname -m)"
case "$arch" in
  x86_64|amd64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "✗ 不支持的架构：$arch（本脚本只准备 amd64/arm64）" >&2; exit 1 ;;
esac
echo "架构：$arch"

# 从表里取 "<tool>-<os>-<arch>" 这一行。
entry_for() {
  local want="$1" row
  for row in "${SHA256_ALL[@]}"; do
    [ "${row%%|*}" = "$want" ] && { echo "$row"; return 0; }
  done
  return 1
}

# fetch <label> <目标路径> <解出哪个文件> <体积下限>
fetch() {
  local want="$1" dest="$2" inner="$3" minsize="$4" row suffix sha want_label
  # 链接不能进跳过分支：主二进制一旦被换成自指链接（README 旧命令就能做到），
  # `-f` 仍为真，跳过就等于把坏状态固化下来，20 条隧道继续 ELOOP。
  if [ -f "$dest" ] && [ ! -L "$dest" ] && [ "$FORCE" = 0 ]; then
    echo "✓ 已存在，跳过：$dest"
    return 0
  fi
  [ -L "$dest" ] && echo "⚠ $dest 是符号链接，重新下载真实二进制"
  row="$(entry_for "$want")" || { echo "✗ 表里没有 $want" >&2; return 1; }
  suffix="$(echo "$row" | cut -d'|' -f2)"
  sha="$(echo "$row" | cut -d'|' -f3)"
  want_label="$want"
  echo "↓ $want_label"
  # --retry：GitHub 对中国大陆出口常见 connection reset，重试比让用户重跑省事。
  curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 20 --max-time 600 \
       -o "$TMP/pkg" "$MIRROR/$suffix" \
    || { echo "✗ 下载失败：$MIRROR/$suffix" >&2
         echo "  若被墙，设 BIN_MIRROR 指向可达镜像后重试" >&2
         return 1; }

  local got; got="$(sha256sum "$TMP/pkg" | cut -d' ' -f1)"
  [ "$got" = "$sha" ] \
    || { echo "✗ sha256 不符：期望 $sha 实际 $got —— 拒绝落盘" >&2; return 1; }
  echo "  sha256 ✓"

  case "$suffix" in
    *.tar.gz) tar -xzf "$TMP/pkg" -C "$TMP" ;;
    # 用 python 解 zip，不依赖 unzip：精简 Linux（容器/最小发行版）常没装它，
    # 而 python3 是本项目的硬依赖，一定在。
    *.zip)    "$PYBIN" - "$TMP/pkg" "$TMP" <<'PYZ'
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PYZ
              ;;
  esac
  # zip 不保留 unix 权限位（实测解出来是 644），所以不能按 -perm -u+x 找；
  # 找到后自己补执行位。tar 解出的通常已是 755，补一次也无害。
  # -print -quit：找到第一个就停，避免在几百个文件里扫完整棵树再被 head 截断；
  # 也避免 `find | head` 在 pipefail 下因 SIGPIPE 被判失败。
  local src; src="$(find "$TMP" -type f -name "$inner" -print -quit)"
  [ -n "$src" ] || { echo "✗ 解包后找不到 $inner（包内布局变了？）" >&2; return 1; }
  chmod 755 "$src"

  local size; size="$(stat -c%s "$src")"
  [ "$size" -ge "$minsize" ] \
    || { echo "✗ $inner 只有 $size 字节，低于下限 $minsize —— 疑似被劫持的占位文件" >&2; return 1; }

  mkdir -p "$(dirname "$dest")"
  # 先写临时名再改名：中断不会留下半个二进制让后续检查误判为"已存在"。
  cp "$src" "$dest.tmp" && chmod 755 "$dest.tmp" && mv -f "$dest.tmp" "$dest"
  echo "✓ $dest ($size 字节)"
}

fetch "sing-box-linux-$arch"  "$DIR/sing-box"           sing-box 20000000
fetch "usque-linux-$arch"     "$DIR/.warp/usque/usque"  usque    3000000

# 20 条隧道各有工作目录、共用一个二进制。通配 `.warp/usque*/` **包含主目录自身**，
# 照着做会把真二进制换成指向自己的链接 → 全部隧道 ELOOP。
for d in "$DIR"/.warp/usque*/; do
  [ "$(basename "$d")" = usque ] && continue
  ln -sfn ../usque/usque "$d/usque"
done
echo "✓ 已重建隧道符号链接"

"$DIR/.warp/usque/usque" version 2>/dev/null | head -1 || true
"$DIR/sing-box" version 2>/dev/null | head -1 || true
