#!/bin/bash
# WSL2 启动时自动拉起 sing-box + auto-switch-ip daemon
# proxy_429 已由 proxy-global.js 替代（9router 进程内 429 重试 + BYPASS）
set -euo pipefail

SINGBOX="/home/administrator/dkk-projects/auto-switch-ip/sing-box"
CONFIG="/home/administrator/dkk-projects/auto-switch-ip/singbox-config.json"
DAEMON="/home/administrator/dkk-projects/auto-switch-ip/daemon.py"
LOG_DIR="/home/administrator/dkk-projects/auto-switch-ip"

export OKZ_SUB_URL="https://s-dywrwizazu.cn-shanghai.fcapp.run/okz/sub?token=aa0f63eb1e9a65d23fcae0dcc8a867ab"

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
    python3 "$LOG_DIR/parse_subscription.py" --out "$CONFIG" \
        || echo "WARN: config generation failed; daemon will retry on startup" >&2
fi

if ! pgrep -f "sing-box run" >/dev/null; then
    if [ ! -f "$CONFIG" ]; then
        echo "ERROR: $CONFIG absent and could not be generated; skipping sing-box" >&2
    else
        nohup "$SINGBOX" run -c "$CONFIG" > "$LOG_DIR/singbox.log" 2>&1 &
        sleep 1
        if kill -0 $! 2>/dev/null; then
            echo "started sing-box PID $!"
        else
            echo "ERROR: sing-box exited immediately; see $LOG_DIR/singbox.log" >&2
        fi
    fi
fi

# Start daemon if not running
if ! pgrep -f "daemon.py" >/dev/null; then
    nohup python3 "$DAEMON" > "$LOG_DIR/daemon.out" 2>&1 &
    echo "started daemon PID $!"
fi
