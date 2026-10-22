#!/bin/bash
# 拉起 sing-box + auto-switch-ip daemon（WSL2 启动钩子；systemd 托管时不走这里）
# proxy_429 已由 9router 进程内重试替代（原 proxy-global.js 已移除）
set -euo pipefail

# 自身定位，不写死安装目录：脚本在项目根，clone 到哪都能用。
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SINGBOX="$DIR/sing-box"
CONFIG="${SINGBOX_CONFIG:-$DIR/singbox-config.json}"
DAEMON="$DIR/daemon.py"

# 订阅地址走环境变量（token 不入库）。批量启动时由调用方 export，
# 或放在 $DIR/.env（gitignored）里。
if [ -f "$DIR/.env" ]; then set -a; . "$DIR/.env"; set +a; fi

# Wait for networking
for i in $(seq 1 15); do
    if ping -c 1 -W 1 8.8.8.8 >/dev/null 2>&1; then break; fi
    sleep 2
done

# Start sing-box if not running
# 配置缺失时 sing-box 会立刻退出（FATAL read config），不能盲目启动后报成功 ——
# 先生成（配置不入库，全新 clone / 误删即此状态）
if [ ! -f "$CONFIG" ]; then
    echo "config missing, generating from subscription..."
    python3 "$DIR/parse_subscription.py" --out "$CONFIG" \
        || echo "WARN: config generation failed; daemon will retry on startup" >&2
fi

if ! pgrep -f "sing-box run" >/dev/null; then
    if [ ! -f "$CONFIG" ]; then
        echo "ERROR: $CONFIG absent and could not be generated; skipping sing-box" >&2
    else
        nohup "$SINGBOX" run -c "$CONFIG" > "$DIR/singbox.log" 2>&1 &
        sleep 1
        if kill -0 $! 2>/dev/null; then
            echo "started sing-box PID $!"
        else
            echo "ERROR: sing-box exited immediately; see $DIR/singbox.log" >&2
        fi
    fi
fi

# Start daemon if not running
if ! pgrep -f "daemon.py" >/dev/null; then
    nohup python3 "$DAEMON" > "$DIR/daemon.out" 2>&1 &
    echo "started daemon PID $!"
fi
