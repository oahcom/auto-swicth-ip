#!/bin/bash
# 新主机前置检查（只读，不改系统）。WSL2 与通用 Linux 都适用。
#
#   ./deploy/preflight.sh              # 硬项失败退出 1；软项仅警告
#   ./deploy/preflight.sh --units-only # 只检查单元渲染所需项（跳过二进制/订阅）
#
# 硬项：Python>=3.10、requests/PyYAML、systemctl --user、curl、tar
# 软项：linger、.env/订阅、9router DB、bun、sing-box 配置、WSL mirrored、端口
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYBIN="${PYTHON:-/usr/bin/python3}"
UNITS_ONLY=0
[ "${1:-}" = "--units-only" ] && UNITS_ONLY=1

hard=0
ok()   { printf '✓ %s\n' "$*"; }
warn() { printf '⚠ %s\n' "$*"; }
fail() { printf '✗ %s\n' "$*" >&2; hard=1; }

HOST_HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6 || true)"
[ -n "$HOST_HOME" ] || HOST_HOME="$HOME"

echo "== auto-switch-ip 前置检查 =="
printf '  项目: %s\n  用户: %s  解释器: %s\n' "$DIR" "$(id -un)" "$PYBIN"

# 1) 运行环境 / 架构
if grep -qi microsoft /proc/version 2>/dev/null; then
    ok "运行环境: WSL2"
    WSL_CONF="$(ls /mnt/c/Users/*/.wslconfig 2>/dev/null | head -1)"
    if [ -n "${WSL_CONF:-}" ] && grep -Eqi '^[[:space:]]*networkingMode[[:space:]]*=[[:space:]]*mirrored' "$WSL_CONF"; then
        ok "WSL mirrored 网络已开启"
    else
        warn "未检测到 WSL mirrored 网络；Windows 侧 127.0.0.1 到不了 WSL 内的 7890/9090。"
        warn "  在 Windows 的 %USERPROFILE%\\.wslconfig 加 [wsl2] networkingMode=mirrored，然后 wsl --shutdown"
    fi
else
    ok "运行环境: 通用 Linux（非 WSL，无 mirrored 要求）"
fi
case "$(uname -m)" in
    x86_64|amd64|aarch64|arm64) ok "架构: $(uname -m)" ;;
    *) warn "架构 $(uname -m) 不在 fetch-binaries 覆盖范围（当前 amd64/arm64）" ;;
esac

# 2) Python + 依赖
if [ ! -x "$PYBIN" ]; then
    fail "解释器不存在: $PYBIN（用 PYTHON=/path/to/python3 指定）"
else
    if "$PYBIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        ok "Python $("$PYBIN" -V 2>&1 | awk '{print $2}')（>=3.10）"
    else
        fail "Python 版本过低（需 >=3.10）: $("$PYBIN" -V 2>&1)"
    fi
    if "$PYBIN" -c 'import requests, yaml' 2>/dev/null; then
        ok "Python 依赖 requests / PyYAML"
    else
        fail "缺少依赖：$PYBIN -m pip install -r $DIR/requirements.txt"
    fi
fi

# 3) systemd 用户实例 + linger（linger 未开只影响开机自启）
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    ok "systemd 用户实例可用"
    LINGER="$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo unknown)"
    if [ "$LINGER" = "yes" ]; then
        ok "linger 已开启（未登录也会拉起用户服务）"
    else
        warn "linger 未开启（$LINGER）：sudo loginctl enable-linger $(id -un)"
    fi
else
    fail "systemctl --user 不可用。WSL 需 /etc/wsl.conf 设 [boot] systemd=true 后 wsl --shutdown；通用 Linux 需 systemd 用户会话"
fi

# 4) 基础命令
for c in curl tar; do
    command -v "$c" >/dev/null 2>&1 && ok "命令: $c" || fail "缺少命令: $c"
done

# 5) 订阅 / 9router / 探测依赖（软项）
if [ -f "$DIR/.env" ]; then
    ok ".env 存在"
    SUB_COUNT="$(set -a; . "$DIR/.env" >/dev/null 2>&1; set +a; env | grep -c '^[A-Z0-9_]*_SUB_URL=' || true)"
    if [ "${SUB_COUNT:-0}" -gt 0 ]; then
        ok "订阅源: $SUB_COUNT 条"
    else
        warn ".env 里没有 *_SUB_URL；订阅刷新会明确失败（blind 模式可忽略）"
    fi
else
    warn "缺少 $DIR/.env：cp $DIR/.env.example $DIR/.env 并填入订阅源"
fi

DB="$("$PYBIN" -c 'import sys; sys.path.insert(0, sys.argv[1]); import config; print(config.CFG["9router"]["db_path"])' "$DIR" 2>/dev/null || true)"
if [ -n "$DB" ] && [ -f "$DB" ]; then
    ok "9router DB: $DB"
else
    warn "9router DB 不存在: ${DB:-<解析失败>}"
    warn "  daemon 需要本机 9router 的 SQLite；只做盲轮换时在 .env 设 ASIP_OBSERVE_9ROUTER=false"
fi

BUN="$("$PYBIN" -c 'import sys; sys.path.insert(0, sys.argv[1]); from probe_warp import _bun_bin; print(_bun_bin() or "")' "$DIR" 2>/dev/null || true)"
if [ -n "$BUN" ] && [ -x "$BUN" ]; then
    ok "模型层探测 Bun: $BUN"
else
    warn "未找到 bun：模型层探测会全部 unreachable（不会换线）"
    warn "  安装：curl -fsSL https://bun.sh/install | bash（或 npm i -g bun），再重跑本脚本"
    warn "  非标准路径用 .env 的 PROBE_BUN_BIN=/abs/path/bun 指定"
fi

if [ "$UNITS_ONLY" = 0 ]; then
    [ -x "$DIR/sing-box" ] && ok "sing-box 可执行" \
        || warn "sing-box 缺失/不可执行（install.sh 会调用 fetch-binaries.sh）"
    [ -x "$DIR/.warp/usque/usque" ] && ok "usque 可执行" \
        || warn ".warp/usque/usque 缺失（install.sh 会调用 fetch-binaries.sh）"
    [ -f "$DIR/singbox-config.json" ] && ok "singbox-config.json 已生成" \
        || warn "singbox-config.json 未生成：首次启动 daemon 会按 .env 订阅生成"
fi

# 6) 端口占用（软项）
for p in 7890 9090; do
    if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$p$"; then
        if pgrep -f "sing-box run" >/dev/null 2>&1; then
            ok "端口 $p 已由本机 sing-box 监听"
        else
            warn "端口 $p 被其他进程占用（预期 7890=sing-box mixed / 9090=Clash API）"
        fi
    fi
done

echo
if [ "$hard" = 0 ]; then
    echo "✓ 硬性前置检查通过。下一步: ./deploy/install.sh"
else
    echo "✗ 存在硬性缺失，请按上面提示修复后重试。" >&2
fi
exit "$hard"
