#!/bin/bash
# 生成 asip CLI 的运行期设置（可 source 的 shell 片段）。
#
#   ./deploy/asip-cli-conf.sh            # 写入 $ASIP_CLI_ENV（默认 ~/.config/asip/cli.env）
#   ./deploy/asip-cli-conf.sh --print    # 只打印，不落盘（对比/测试用）
#
# 所有取值都从 config.py 推导：日志、密钥、Clash API、selector、20 条隧道的
# 端口/单元名/tag、两个 IP 台账路径。手抄一份到脚本里的下场是改端口或换目录后
# 静默指向旧位置 —— 那正是这个脚本存在的理由。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 与 config.py 一致：从 passwd 取家目录，不信 $HOME（`sudo -u` 下 HOME 可能是 /root）。
# ASIP_HOME_DIR 只是给试跑/测试用 —— 否则 CI 里跑一次就把设置写进真人目录。
HOME_DIR="${ASIP_HOME_DIR:-$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6 || true)}"
[ -n "$HOME_DIR" ] || HOME_DIR="$HOME"
PYBIN="${PYTHON:-/usr/bin/python3}"
OUT="${ASIP_CLI_ENV:-$HOME_DIR/.config/asip/cli.env}"
PRINT_ONLY=0
[ "${1:-}" = "--print" ] && PRINT_ONLY=1

[ -x "$PYBIN" ] || { echo "✗ 解释器不存在：$PYBIN（用 PYTHON=... 指定）" >&2; exit 1; }

rendered="$("$PYBIN" - "$DIR" "$HOME_DIR" "$PYBIN" <<'PY'
import os, shlex, sys

repo, home, pybin = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, repo)
import config  # noqa: E402  缺键/取值非法时它自己 SystemExit，部署就该在这里失败

c = config.CFG
members = c["warp"]["members"]
if not members:
    sys.exit("✗ config.py 的 warp.members 为空，无法生成隧道清单")

SERVICE = "auto-switch-ip.service"
# 单元此前从用户级迁移到系统级（用户级同名单元已删除），而 install.sh 写的是
# 用户级目录 —— 两种部署并存，CLI 必须跟着实际落点走，否则 stop/start 作用在
# 一个不存在的 unit 上却报成功。
# SYSTEMD_DIR 优先：install.sh 用它试跑（写 /tmp/x 而不是真实单元），此时单元
# 就在那个目录里。不采信它的话，设置会指向 /etc 的真实单元 —— 演练完跑一次
# asip warp stop 就动了生产。ASIP_SYSTEM_UNIT_DIR 供测试覆盖探测结果。
explicit = os.environ.get("SYSTEMD_DIR")
if explicit:
    unit_dir = explicit
    scope = "system" if explicit.startswith("/etc/systemd/") else "user"
else:
    system_dir = os.environ.get("ASIP_SYSTEM_UNIT_DIR") or "/etc/systemd/system"
    user_dir = os.path.join(home, ".config/systemd/user")
    if os.path.exists(os.path.join(system_dir, SERVICE)):
        scope, unit_dir = "system", system_dir
    else:
        scope, unit_dir = "user", user_dir

def q(v):
    return shlex.quote(str(v))

# 台账路径与 daemon 的解析顺序一致：环境变量优先，其次仓库目录。
used = os.environ.get("WARP_IP_USED_FILE_PATH") or os.path.join(repo, ".warp_ip_used.json")
cycles = os.environ.get("WARP_IP_CYCLES_FILE_PATH") or os.path.join(repo, ".warp_ip_cycles.json")
blackboard = os.environ.get("HERMES_BLACKBOARD_DB") or os.path.join(
    home, ".hermes/sister_bus/blackboard.db")

print("# 由 deploy/asip-cli-conf.sh 生成 —— 不要手改，重跑该脚本即可刷新。")
print("# asip CLI 启动时 source 本文件；缺失即报错并提示部署命令。")
for key, val in (
    ("ASIP_DIR", repo),
    ("ASIP_PY", pybin),
    ("ASIP_LOG", c["log_file"]),
    ("ASIP_SECRET_FILE", config.SECRET_FILE),
    ("ASIP_CLASH_API", c["singbox"]["clash_api"]),
    ("ASIP_WARP_SELECTOR", c["warp"]["selector"]),
    ("ASIP_POOL_SELECTOR", c["singbox"]["selector"]),
    # CLI 直连隧道口探测 warp=on；主机写死成 127.0.0.1 的话，把隧道挪到
    # 别的地址后 start/status 全报「不可达」，看起来像隧道故障。
    ("ASIP_SOCKS_HOST", c["warp"]["socks_host"]),
    ("ASIP_SERVICE", SERVICE),
    ("ASIP_SYSTEMD_SCOPE", scope),
    ("ASIP_UNIT_DIR", unit_dir),
    ("ASIP_IP_USED_FILE", used),
    ("ASIP_CYCLES_FILE", cycles),
    ("ASIP_BUS_CLIENT", os.path.join(home, ".hermes/scripts/bus_client.py")),
    ("ASIP_BLACKBOARD_DB", blackboard),
    ("ASIP_WARP_COUNT", len(members)),
    # 与 deploy/members.py 同格式：tag port service。CLI 只需按端口找 tag、
    # 列单元名，不必再解析 config.py（那正是原先写死路径的同类错误）。
    ("ASIP_WARP_MEMBERS",
     "\n".join(f"{m['tag']} {m['port']} {m['service']}" for m in members)),
):
    print(f"{key}={q(val)}")
PY
)" || { echo "✗ 从 config.py 生成 CLI 设置失败" >&2; exit 1; }

if [ "$PRINT_ONLY" = 1 ]; then
    printf '%s\n' "$rendered"
    exit 0
fi

mkdir -p "$(dirname "$OUT")"
# cli.env 带端口、单元名、Blackboard 库路径、日志路径，相当于资产清单；
# 与 install.sh 生成密钥时的一致：只对本用户可见。
( umask 077; printf '%s\n' "$rendered" > "$OUT" )
echo "asip 设置: $OUT（$(grep -c '^ASIP_[A-Z_]*=' "$OUT") 项，$(sed -n 's/^ASIP_WARP_COUNT=//p' "$OUT") 条隧道）"
