#!/bin/bash
# WSL2 启动时自动拉起 sing-box + auto-switch-ip daemon
set -euo pipefail

SINGBOX="/home/administrator/dkk-projects/auto-switch-ip/sing-box"
CONFIG="/home/administrator/dkk-projects/auto-switch-ip/singbox-config.json"
DAEMON="/home/administrator/dkk-projects/auto-switch-ip/daemon.py"
LOG_DIR="/home/administrator/dkk-projects/auto-switch-ip"

export OKZ_SUB_URL="https://s-dywrwizazu.cn-shanghai.fcapp.run/okz/sub?token=7c00e506cb827821ad76e93053737c61"

# Wait for networking
for i in $(seq 1 15); do
    if ping -c 1 -W 1 8.8.8.8 >/dev/null 2>&1; then break; fi
    sleep 2
done

# Start sing-box if not running
if ! pgrep -f "sing-box run" >/dev/null; then
    nohup "$SINGBOX" run -c "$CONFIG" > "$LOG_DIR/singbox.log" 2>&1 &
    echo "started sing-box PID $!"
fi

# Start daemon if not running
if ! pgrep -f "daemon.py" >/dev/null; then
    nohup python3 "$DAEMON" > "$LOG_DIR/daemon.out" 2>&1 &
    echo "started daemon PID $!"
fi
