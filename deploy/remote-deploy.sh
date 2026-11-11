#!/bin/bash
# 把本机拓扑复刻到远端主机（代码 + 状态表 + 设备 + 单元 + 服务）。
#
#   ./deploy/remote-deploy.sh user@host [--port 22] [--dry-run] [--skip-register]
#
# 设计前提：**拓扑以本机为准**。远端不参与决策，只复刻：
#   直连 usque 数、H2 渠道数与设备分配、WG 线路数、块模型开关、selector 成员。
# 所以脚本读的是本机的 `.env` 与 `.warp/*.json`，不是远端现有配置。
#
# WHY 用 tar 而不是 git bundle：bundle 会带上 `refs/stash` 与 worktree 的
# 悬空 commit，远端 `git fetch` 会在 revision walk 时报
# "did not send all necessary objects"（实测）。`git archive HEAD` 只含
# 工作树内容，没有这个坑，也不会把 .env / .warp 这些 gitignored 的东西漏出去
# —— 它们由后面的显式步骤单独传。
#
# WHY 全程显式传 HOME / 环境变量给 sudo：`sudo` 会清掉环境（`-E` 在现代
# sudo 上被忽略），于是 `WARP_WG_FAMILIES` 丢失、`HOME` 变成 /root，
# 表现为「需要 WARP_WG_FAMILIES」和「读不到 .singbox_secret」两个假故障。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

TARGET=""; PORT=22; DRY=0; SKIP_REGISTER=0
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        --skip-register) SKIP_REGISTER=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        -*) echo "✗ 未知参数: $1" >&2; exit 2 ;;
        *) TARGET="$1"; shift ;;
    esac
done
[ -n "$TARGET" ] || { echo "用法: $0 user@host [--port N] [--dry-run]" >&2; exit 2; }

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -p "$PORT")

# 部署前必须已推送：远端是从 origin 拉代码的，本地没 push 就等于部署了
# 一份仓库里不存在的东西。2026-11-03 实测过这个坑。
if [ "$DRY" != 1 ]; then
    UP=$(git rev-list --count '@{upstream}..HEAD' 2>/dev/null || echo "")
    if [ -z "$UP" ]; then
        echo "  ⚠ 没有 upstream 或无 origin，跳过「未推送提交」检查" >&2
    elif [ "$UP" != "0" ]; then
        echo "✗ 本地有 $UP 个提交未推送 —— 先 git push。" >&2
        echo "  远端从 origin 拉代码，不 push 意味着部署出去的代码不在仓库里。" >&2
        exit 1
    fi
fi
# --dry-run 必须真的不执行：远端命令里全是 restart / install / register，
# 只解析参数不短路等于「看起来演练过、实际动了生产」。
if [ "$DRY" = 1 ]; then
    # 打到 stderr：调用方普遍用 `$(run ...)` 取输出，写 stdout 会把命令文本
    # 当成结果（实测 9router 端口探测因此拿到一整段 shell）。
    run() { printf '  [dry] ssh %s %q\n' "$TARGET" "$1" >&2; }
    put() { printf '  [dry] scp %s -> %s:%s\n' "$1" "$TARGET" "$2" >&2; }
else
    run() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }
    put() { scp -O -o StrictHostKeyChecking=accept-new -P "$PORT" "$1" "$TARGET:$2"; }
fi
say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# 远端仓库路径：优先沿用已存在的，否则 ~/auto-swicth-ip
REMOTE_USER="${TARGET%@*}"
if [ "$DRY" = 1 ]; then
    # 演练不连远端，用默认值把流程走完即可（这些值只影响打印的命令文本）
    REMOTE_DIR="~/auto-swicth-ip"; REMOTE_HOME="~"
else
    REMOTE_DIR="$(run 'for d in ~/auto-swicth-ip ~/asip; do [ -d "$d" ] && { echo "$d"; exit; }; done; echo ~/auto-swicth-ip')"
    REMOTE_HOME="$(run 'getent passwd "$(id -un)" | cut -d: -f6')"
fi
echo "远端: $TARGET:$REMOTE_DIR  (user=$REMOTE_USER home=$REMOTE_HOME)"

# ---------------------------------------------------------------- 0. 预检
say "0/7 预检"
run 'command -v python3 >/dev/null || { echo "✗ 远端缺 python3"; exit 1; }
     command -v systemctl >/dev/null || { echo "✗ 远端缺 systemd"; exit 1; }
     sudo -n true 2>/dev/null || { echo "✗ 远端需要免密 sudo"; exit 1; }
     echo "  python3 $(python3 -V 2>&1 | cut -d" " -f2) / sudo 免密 OK"'

# ---------------------------------------------------------------- 1. 代码
say "1/7 同步代码（远端 git pull，与 origin 同源）"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
# **不再用 scp 覆盖。** 旧的 `git archive + tar xzf` 有两处硬伤：
#   · 远端仓库是独立历史，和 GitHub 毫无关系，出问题回不到任何一次真提交；
#   · 覆盖时静默吞掉远端的工作区改动，且「部署成功」不代表「代码进了仓库」。
# 现在远端有 origin，部署就是一次 pull。前提（2026-11-03 已配好）：
# 远端的 ~/.ssh/id_ed25519 能访问 github.com:oahcom/auto-swicth-ip。
run "set -e
cd $REMOTE_DIR 2>/dev/null || { echo '✗ 远端没有 $REMOTE_DIR'; exit 1; }
if ! git remote get-url origin >/dev/null 2>&1; then
    echo '✗ 远端仓库没有 origin —— 先接上 GitHub（见 docs/runbook/troubleshoot.md）' >&2
    exit 1
fi
if [ -n \"\$(git status --porcelain)\" ]; then
    echo '  ⚠ 远端工作区有未提交改动，先备份再 pull：' >&2
    git status --short | sed 's/^/    /' >&2
    git stash push -u -m \"deploy-autostash-\$(date +%s)\" >/dev/null
    echo '  （已 stash；git stash list 可查）' >&2
fi
git fetch --depth=1 origin main
git reset --hard origin/main
chmod +x deploy/*.sh tools/*.sh 2>/dev/null || true
python3 -m py_compile config.py daemon.py warp_wg.py warp_blocks.py
# **未定义名字检查**：`py_compile` 只查**语法**，不查名字。`PROJECT` 写成
# `_PROJECT` 是语法完全合法的 Python，但运行时才抛 NameError —— 而远端
# daemon 是被 systemd `Restart=always` 托管的，那会变成「启动 → 崩溃 →
# 重启 → 崩溃」的重启风暴。
# 实测 2026-11-11 17:22:31~17:27:21：本机因此连崩 5 分钟，restart counter
# 打到 23，服务瘫了 5 分钟。
# WHY 放在这里：`git reset --hard` 之后（第 103 行）—— 检查的是**远端实际
# 将要运行的代码**；且必须在第 431 行 `systemctl restart` **之前**。
if [ ! -f tools/check_undefined_names.py ]; then
    echo \"✗ 远端缺少静态名字关卡脚本，拒绝部署\" >&2; exit 1
fi
python3 tools/check_undefined_names.py daemon.py dormant_probe.py config.py warp_wg.py warp_blocks.py parse_subscription.py \\
    || { echo \"✗ 远端核心模块有未定义名字，拒绝部署\" >&2; exit 1; }
# 关卡 drop-in 随代码下发：旧版只在本机手工放过，远端「改文件 + restart」
# 这条最常见的路径没有 ExecStartPre。只有单元自带 WorkingDirectory 才能下，
# 因为关卡用的是相对路径 + WorkingDirectory（见 conf 里的 WSL 命名空间注释）。
if systemctl cat auto-switch-ip.service 2>/dev/null | grep -q '^WorkingDirectory='; then
    sudo mkdir -p /etc/systemd/system/auto-switch-ip.service.d
    sudo cp deploy/auto-switch-ip-precheck.conf /etc/systemd/system/auto-switch-ip.service.d/10-precheck.conf
    sudo systemctl daemon-reload
else
    echo \"⚠ 远端单元没有 WorkingDirectory，跳过 drop-in\" >&2
fi
echo \"  已同步到 \$(git rev-parse --short HEAD) \$(git log -1 --format=%s)\""

# ---------------------------------------------------------------- 2. 拓扑参数
say "2/7 对齐 .env 拓扑参数（以本机为准）"
# 只覆盖拓扑相关的键，其余（订阅 token、relay token 等）保留远端原值 ——
# 那些是每台机器自己的凭据，不该被本机覆盖。
ENV_KEYS=(WARP_WG_FAMILIES WARP_WG_NODES ASIP_WARP_BLOCKS ASIP_WARP_BLOCK_CHANNELS NINE_ROUTER_BASE)
ENV_PAYLOAD=""
for k in "${ENV_KEYS[@]}"; do
    v=$(grep -E "^${k}=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true)
    [ -n "$v" ] && ENV_PAYLOAD+="$k=$v"$'\n'
done
printf '%s' "$ENV_PAYLOAD" > "$TMP/env.patch"
put "$TMP/env.patch" /tmp/asip-env.patch
run "cd $REMOTE_DIR
touch .env
while IFS='=' read -r k v; do
    [ -n \"\$k\" ] || continue
    if grep -qE \"^\$k=\" .env; then
        sed -i \"s#^\$k=.*#\$k=\$v#\" .env
    else
        echo \"\$k=\$v\" >> .env
    fi
done < /tmp/asip-env.patch
grep -E '^(WARP_WG_|ASIP_WARP|NINE_ROUTER_BASE)' .env | sed 's/^/  /'"

# 远端 9router 端口可能与本机不同，必须以**单元自己声明的端口**为准。
#
# WHY 不再按「哪个端口有人应答」探：127.0.0.1:20128 跑的是 relay-guard，
# 它对 /dashboard 返回 307，于是探测把 20128 当成 9router 写进 .env。
# 后果是 check_9router_health() 对着 relay-guard 永远返回健康 —— 9router
# 真死了这层检查也不会说话。2026-11-03 远端实测就是这个状态（20127 根本
# 没人监听，daemon 却一直读 20128）。
#
# 端口在单元里有两处写法，取并集：
#   · 本机 install-9router.sh 生成的是 `custom-server.js --port 20127`
#   · 远端那份把端口写成 `Environment=PORT=38731`（ExecStart 不带 --port）
DETECTED=$(run 'U=nine-router-src.service
    a=$(systemctl show $U -p ExecStart --value 2>/dev/null \
        | grep -o -- "--port [0-9]*" | grep -o "[0-9]*" | head -1)
    b=$(systemctl show $U -p Environment --value 2>/dev/null \
        | tr " " "\n" | sed -n "s/^PORT=//p" | head -1)
    echo "${a:-$b}"')
if [ -n "$DETECTED" ]; then
    echo "  远端 9router 在 :$DETECTED（取自 nine-router-src.service 声明的端口）"
    run "cd $REMOTE_DIR
    sed -i 's#^NINE_ROUTER_BASE=.*#NINE_ROUTER_BASE=http://127.0.0.1:$DETECTED#' .env"
    # 写进去还不够：daemon 的 EnvironmentFile 只在启动时读一次，不重启就还是旧值。
    run "sudo systemctl restart auto-switch-ip.service"
else
    echo "  ⚠ 从 nine-router-src.service 读不到端口，保留 .env 现值"
fi

# ---------------------------------------------------------------- 3. 实测表
say "3/7 同步实测表与 H2 台账（gitignored，但拓扑必需）"
# block-map.json：块的成员归属，没有它 warp_blocks.channels() 返回空，
# `plan` 直接报「块模型未启用」，一条渠道都开不出来。
# h2/channels.json：已投产的渠道↔设备↔端口分配，render_h2_lines 在块模型
# 筛不出 h2 时靠它兜底（见该文件里的注释）。
# tiers.json：当前档位（哪些 WG 线该跑）。**不传的后果是静默的**：文件缺失时
# `warp_blocks.tier_spec()` 返回 None → 全开，远端会跑满 22 条 WG 线，与本机
# `h2-only` 不一致，而验收里只看「单元 active 数」是看不出来的（多跑不报错）。
# 它必须跟 block-map 一起传，否则「push 完重启就一致」不成立。
for f in .warp/tiers.json .warp/block-map.json .warp/h2/channels.json; do
    [ -f "$f" ] || { echo "  ⚠ 本机缺 $f，跳过"; continue; }
    put "$f" "/tmp/$(basename "$f")"
    echo "  $f"
done
# **block-map 只增不删**（与 tools/merge_h2_blockmap.py 同一语义）。
#
# 旧版这里是「整表覆盖 + 按远端出站表过滤」，两处都会丢数据：
#   · 覆盖本身就把远端自己测出来的条目抹掉；
#   · 过滤用的白名单是**上一次部署**的 singbox-config.json（全新远端根本没有
#     这个文件 → `live` 为空 → 只剩入库的 65 条 dormant 快照当白名单）。
#     实测本机 174 条里只保住 44 条，**删掉 130 条、其中 28 个 H2 落地节点**。
#     而被删的节点远端此后不会自动加回：`warp_blocks.merge_diag()` 只给探测
#     成功的节点建档，`h2_probe_state.json` 又没传（见下一步）。
# 过滤的原始理由（远端没有 greatgame/dankenet 这两个域名）在补源后已不成立；
# 即便成立，`render_h2_lines.node_outbound()` 本来就会跳过「不在主配置/沉睡池」
# 的节点并打一行 warning —— 带着它们走不会渲染出坏线，删掉却不可逆。
#
# channels.json 反过来：它是**本机产物台账**（哪台设备跑哪条线），不是配置。
# 远端已有一份时不许覆盖 —— 本机 31 条、远端 37 条，覆盖等于把远端砍到 31 条，
# 而 members.py 随后会按这份台账写出 31 个成员。只有远端从没渲染过才拿它兜底。
run "cd $REMOTE_DIR && mkdir -p .warp/h2
# 档位是**配置**不是台账，直接以本机为准覆盖 —— 不做合并。
# 合并会留下远端上一轮的档位（例如还写着 wg-on），两台机器跑不同拓扑。
if [ -s /tmp/tiers.json ]; then
    cp /tmp/tiers.json .warp/tiers.json && chmod 600 .warp/tiers.json
    python3 -c 'import json,sys; d=json.load(open(\".warp/tiers.json\")); print(\"  档位 \" + str(d.get(\"current\")) + \" wg=\" + repr((d.get(\"tiers\") or {}).get(d.get(\"current\") or \"\",{}).get(\"wg\")))'
fi
python3 - <<'PY'
import json
from pathlib import Path
proj = Path('.')
p = proj / '.warp' / 'block-map.json'
try:
    mine = json.loads(Path('/tmp/block-map.json').read_text())
except (OSError, ValueError):
    mine = {}
mn = mine.get('nodes') if isinstance(mine.get('nodes'), dict) else mine
if not isinstance(mn, dict):
    mn = {}
try:
    cur = json.loads(p.read_text())
except (OSError, ValueError):
    cur = {}
cn = cur.get('nodes') if isinstance(cur.get('nodes'), dict) else cur
if not isinstance(cn, dict):
    cn = {}
merged = dict(mn)
merged.update(cn)          # 远端自己的观测优先：同一节点以远端为准
if isinstance(cur.get('nodes'), dict):
    cur['nodes'] = merged
else:
    cur = merged
p.write_text(json.dumps(cur, ensure_ascii=False, separators=(',', ':')))
p.chmod(0o600)
print(f'  block-map 合并：本机 {len(mn)} + 远端 {len(cn)} → {len(merged)}'
      f'（新增 {len(set(mn) - set(cn))}，保留远端独有 {len(set(cn) - set(mn))}）')
PY
mkdir -p .warp/h2
if [ -s .warp/h2/channels.json ]; then
    echo '  channels.json 已存在，保留远端产物台账（不覆盖）'
else
    cp /tmp/channels.json .warp/h2/channels.json && echo '  channels.json 远端没有 → 用本机台账兜底'
fi"

# 探测结论与订阅快照：都不是凭据，是**两台机器各自攒出来的能力**，必须同步。
#   h2_probe_state.json —— 「测过且失败」的节点据此跳过。不传的话远端会把 82 个
#                          已知不可承载的节点重测一遍，而探测设备是稀缺资源
#                          （每台都要向 CF 注册一次）。
#   .sub_cache/         —— 一次性订阅链接失效后的唯一退路。没有它，
#                          `_fetch_or_cache` 只能 `raise`，该源整批节点消失。
#   .pool_history.json  —— 骤降护栏的水位基线，全新远端为空时第一次刷新没有保护。
say "3b/7 同步探测台账与订阅快照"
if [ -d .sub_cache ]; then
    tar -C "$DIR" -czf "$TMP/subcache.tgz" .sub_cache 2>/dev/null || true
    put "$TMP/subcache.tgz" /tmp/asip-subcache.tgz
    run "cd $REMOTE_DIR && mkdir -p .sub_cache && tar -xzf /tmp/asip-subcache.tgz -C . \
         && chmod 600 .sub_cache/*.json 2>/dev/null || true
         echo \"  .sub_cache/ \$(ls .sub_cache/*.json 2>/dev/null | wc -l) 个快照（只补不删）\""
fi
for f in .warp/h2_probe_state.json .pool_history.json; do
    [ -f "$f" ] || continue
    put "$f" "/tmp/asip-$(basename "$f")"
    run "cd $REMOTE_DIR && python3 - $(basename "$f") <<'PY'
import json, sys, shutil
from pathlib import Path
name = sys.argv[1]
src = Path(f'/tmp/asip-{name}')
dst = Path('.warp/h2_probe_state.json') if 'probe_state' in name else Path('.pool_history.json')
dst.parent.mkdir(parents=True, exist_ok=True)
if not dst.exists():
    shutil.copyfile(src, dst)
    print(f'  {dst}: 远端没有 → 直接采用本机')
else:
    # 两边都是「结论累积」，合并策略是远端优先、本机补缺。
    try:
        a = json.loads(src.read_text()); b = json.loads(dst.read_text())
    except (OSError, ValueError):
        print(f'  {dst}: 解析失败，保留远端'); raise SystemExit(0)
    if isinstance(a, dict) and isinstance(b, dict) and 'nodes' in a and 'nodes' in b:
        n = dict(a['nodes']); n.update(b['nodes'])     # 远端优先
        b['nodes'] = n
        dst.write_text(json.dumps(b, ensure_ascii=False))
        print(f'  {dst}: 合并 → {len(n)} 条')
    else:
        print(f'  {dst}: 结构不认识，保留远端')
PY
         chmod 600 .warp/h2_probe_state.json .pool_history.json 2>/dev/null || true"
done

# 追加订阅源**并集合并**，不是覆盖：远端有 `jiashule` 是本机没有的，
# 整文件覆盖会把它删掉 —— 而它决定远端能拉到哪些落点节点，是拓扑输入
# 不是凭据（脚本上方那条「token 保留远端原值」的原则不适用于它）。
if [ -f .extra_subscriptions ]; then
    put .extra_subscriptions /tmp/asip-extra-subs
    run "cd $REMOTE_DIR && touch .extra_subscriptions
python3 - <<'PY'
from pathlib import Path
def read(p):
    try:
        return [l.strip() for l in Path(p).read_text().splitlines()
                if l.strip() and '://' in l and not l.startswith('#')]
    except OSError:
        return []
mine, cur = read('/tmp/asip-extra-subs'), read('.extra_subscriptions')
merged = list(dict.fromkeys(cur + mine))          # 远端在前，顺序稳定
Path('.extra_subscriptions').write_text(''.join(u + '\n' for u in merged))
Path('.extra_subscriptions').chmod(0o600)
print(f'  追加源合并：本机 {len(mine)} + 远端 {len(cur)} → {len(merged)}')
PY"
fi

# ---------------------------------------------------------------- 4. 设备
say "4/7 补齐 usque 设备（每条 H2 线一台，CF 侧同一设备只能一个会话）"
# 本机 H2 用了几台设备，远端就得有几台空闲的。设备名必须与台账一致，
# 否则 `WARP-H2-<colo>-<i>` 成员整体平移，daemon 按 tag 记账全错位。
if [ "$SKIP_REGISTER" = 1 ]; then
    echo "  --skip-register：跳过"
else
    NEEDED=$(python3 - <<'PY'
import json, os
p = ".warp/h2/channels.json"
print(len({r["device"] for r in json.load(open(p))}) if os.path.exists(p) else 0)
PY
)
    echo "  本机 H2 需要 $NEEDED 台设备"
    run "cd $REMOTE_DIR
BIN=\$PWD/.warp/usque/usque
[ -x \$BIN ] || { echo '  ⚠ 缺 .warp/usque/usque，跳过注册'; exit 0; }
# 台账里的设备名逐个确认；缺的补注册。注册必须在该设备目录里执行 ——
# 二进制是绝对路径，cwd 不对会把 config.json 写到仓库根，下一轮又提示
# 'You already have a config'（实测踩过，误判成注册失败）。
python3 - <<'PY' > /tmp/asip-devices.txt
import json
for r in json.load(open('.warp/h2/channels.json')):
    print(r['device'])
PY
sort -u /tmp/asip-devices.txt | while read -r dev; do
    d=\$PWD/.warp/\$dev
    mkdir -p \"\$d\"
    [ -f \"\$d/config.json\" ] && continue
    ( cd \"\$d\" && timeout 90 env -u http_proxy -u https_proxy \
        -u HTTP_PROXY -u HTTPS_PROXY \"\$BIN\" register --accept-tos -n \"asip-\$dev\" ) \
        >/dev/null 2>&1 && chmod 600 \"\$d/config.json\" && echo \"  + \$dev\" || echo \"  ✗ \$dev 注册失败\"
    sleep 6
done
echo '  设备就绪'" || true
fi

# ---------------------------------------------------------------- 5. 单元
say "5/7 生成并安装单元"
# **顺序与 daemon 的 `_run_h2_sync` 对齐**（daemon.py:9098 那条链是唯一
# 被生产验证过的编排）。旧版有两个硬伤：
#   · `members.py` 跑在 `h2-install` 之前，而 h2-install 会重新渲染并改写
#     `channels.json` —— `warp-outbound.json` 反映的是「上一轮的台账」；
#   · 没有 `patch-wg-outbounds.py`，新写进 warp-outbound 的成员**没有任何人**
#     把它们并进 `singbox-config.json` 的 `opencode-out`。只能指望重启后那次
#     隐式订阅刷新补上；刷新一失败（订阅拉不到 / 骤降护栏拦下），远端就停在
#     「单元全 active、h2-sel-N 全在、单条 SOCKS 能出 warp=on，但主 selector
#     里一条新线都没有」—— TEST_PLAN §20b 记的就是这个症状。
# 每一步都必须判返回码：旧版用 `| tail -3` 收尾，**管道返回的是 tail 的码**，
# H2 渲染拒绝执行时脚本照样往下走，只在验收里显示 0 条。
run "cd $REMOTE_DIR
set -a; . ./.env; set +a
export HOME=$REMOTE_HOME
set -e
# 权限：warp-outbound.json 历史上被 root 写过，members.py 随后写不进去。
sudo chown -R $REMOTE_USER:$REMOTE_USER .warp 2>/dev/null || true

echo '  · 主配置'
python3 parse_subscription.py >/dev/null 2>&1 \
    || echo '    ⚠ parse_subscription 失败（订阅 token 可能未配）'

echo '  · WG 线'
# ASIP_RUN_USER 必须显式传：wg-lines-install.sh 的默认值是 \$(id -un)，在
# sudo 下就是 root，于是 apply → members.py 以 root 写出 600 属主的
# warp-outbound.json，daemon（User=$REMOTE_USER）随后读不到 —— 生成的配置
# 没有 WARP 出口，opencode 静默落到机场池。2026-11-03 远端因此丢了
# 2 小时 24 分的 WARP 出口（30362 次 status=404），而日志里一条都没有。
sudo env HOME=$REMOTE_HOME ASIP_RUN_USER=$REMOTE_USER \
     ASIP_RUN_GROUP=$REMOTE_USER \
     WARP_WG_FAMILIES=\"\$WARP_WG_FAMILIES\" \
     WARP_WG_NODES=\"\$WARP_WG_NODES\" ASIP_WARP_BLOCKS=\"\$ASIP_WARP_BLOCKS\" \
     ASIP_WARP_BLOCK_CHANNELS=\"\$ASIP_WARP_BLOCK_CHANNELS\" PYTHON=/usr/bin/python3 \
     ./deploy/wg-lines-install.sh > /tmp/asip-wg.log 2>&1 \
     || { echo '    ✗ WG 线安装失败：'; tail -5 /tmp/asip-wg.log | sed 's/^/      /'; }
tail -3 /tmp/asip-wg.log | sed 's/^/    /'

echo '  · H2 渲染（**本用户**，不是 root）'
# 以 root 渲染会把 .warp/h2/* 的属主改成 root，daemon 之后读不回来
# （h2-install.sh:29-31 与 daemon.py:9109 都写了这条规则）。而且
# get_singbox_secret() 按 **当前 uid 的家目录** 找 .singbox_secret ——
# 实测 root 下读 /root/.singbox_secret 直接失败、返回空串，渲染出的 Clash
# secret 为空，daemon 每次 PUT 都 401。
python3 deploy/render_h2_lines.py > /tmp/asip-h2render.log 2>&1 \
    || { echo '    ✗ H2 渲染失败：'; tail -8 /tmp/asip-h2render.log | sed 's/^/      /'; exit 1; }
tail -4 /tmp/asip-h2render.log | sed 's/^/    /'

echo '  · H2 线（--no-render，用上面刚渲染的产物）'
sudo env HOME=$REMOTE_HOME ASIP_RUN_USER=$REMOTE_USER \
     ASIP_WARP_BLOCKS=\"\$ASIP_WARP_BLOCKS\" \
     ASIP_WARP_BLOCK_CHANNELS=\"\$ASIP_WARP_BLOCK_CHANNELS\" PYTHON=/usr/bin/python3 \
     ./deploy/h2-install.sh --no-render > /tmp/asip-h2.log 2>&1 \
     || { echo '    ✗ H2 安装失败：'; tail -8 /tmp/asip-h2.log | sed 's/^/      /'; exit 1; }
tail -4 /tmp/asip-h2.log | sed 's/^/    /'

# 渲染完之后才重建 warp-outbound：channels.json 此刻才是最终产物。
# 顺序反了（旧版）会把上一轮的台账写进主配置。
echo '  · warp-outbound'
python3 deploy/members.py --write-warp-outbound >/dev/null
echo '  · 并入主配置 selector'
python3 deploy/patch-wg-outbounds.py | sed 's/^/    /'

# cli.env 是**部署期快照**，不是每次运行现算的：里面写着隧道清单、端口、
# 单元名。渲染出新线之后不重生成，asip chan / asip warp status 会一直
# 少那几条（实测本机 44 → 50 条隧道后 asip chan 仍报旧清单）。它只读 .warp
# 和 .env，不碰网络，几毫秒的事，每次部署都重来一遍。
# 注意：本块整体在双引号里，反引号与 \$() 会被**本机**展开，不要出现。
echo '  · asip 设置快照'
./deploy/asip-cli-conf.sh >/dev/null 2>&1 \
    && echo '    已重生成 ~/.config/asip/cli.env' \
    || echo '    ⚠ asip-cli-conf.sh 失败（asip 命令会读到旧清单）'

# 兜底：上面任一步以 root 写出文件，这里统一交还。缺了它，一次 sudo 就能让
# 下一轮订阅刷新产出没有 WARP 的配置。
sudo chown -R $REMOTE_USER:$REMOTE_USER .warp 2>/dev/null || true

echo '  · asip-unit 白名单'
sudo install -m 0755 deploy/asip-unit /usr/local/bin/asip-unit
for u in usque-h2-0.service warp-wg-1.service usque-warp.service; do
    sudo /usr/local/bin/asip-unit is-active \$u >/dev/null 2>&1 \
        && echo \"    ✓ \$u 在白名单\" || echo \"    ✗ \$u 被拒\"
done"

# ---------------------------------------------------------------- 6. 重启
say "6/7 重启服务（主 sing-box 由 daemon 拉起，必须重启 daemon 才重载配置）"
# `patch-wg-outbounds.py` 只改了配置文件，**进程不会自己重载** —— sing-box 是
# daemon 用 Popen 拉的，改文件对它无效。重启 daemon 也不会重拉 sing-box（它
# 发现 9090 还活着就直接进主循环），所以这里要显式杀掉主进程、让 watchdog
# （30s 一轮）按新配置拉起来。不杀的话 `opencode-out` 里没有新成员。
run "set -e
sudo systemctl daemon-reload
sudo systemctl restart auto-switch-ip.service
sleep 10
# 只杀主配置那一份；.warp/h2 与 .warp/wglines 各跑自己的 sing-box，
# 宽匹配会把它们一起杀掉（每条线重启就换一个 WARP 会话）。
P=\$(pgrep -f \"sing-box run -c \$HOME/auto-swicth-ip/singbox-config.json\" || true)
if [ -n \"\$P\" ]; then
    sudo kill \$P 2>/dev/null || true
    echo \"  已停主 sing-box（pid \$P），等 watchdog 重拉\"
fi
for i in \$(seq 1 12); do
    sleep 5
    c=\$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:9090/version || true)
    [ \"\$c\" = 200 ] || [ \"\$c\" = 401 ] && { echo '  主 sing-box 已就绪'; break; }
    [ \"\$i\" = 12 ] && echo '  ⚠ 主 sing-box 60s 内未就绪'
done
systemctl is-active auto-switch-ip.service"

# ---------------------------------------------------------------- 7. 验收
say "7/7 验收"
run "cd $REMOTE_DIR
echo '--- 单元 ---'
for pat in 'usque-warp*.service' 'usque-h2-*.service' 'warp-wg-*.service'; do
    printf '  %-24s %s active\n' \"\$pat\" \"\$(systemctl list-units --no-legend \"\$pat\" --state=active | wc -l)\"
done
printf '  %-24s %s\n' asip-h2-singbox \"\$(systemctl is-active asip-h2-singbox.service)\"
printf '  %-24s %s\n' nine-router-src \"\$(systemctl is-active nine-router-src.service 2>/dev/null)\"
echo '--- 档位 ---'
# 必须比对**实际在跑的 WG 单元数**与档位声明，不能只印档位名：
# 档位文件写对了但 daemon 没重载，或者单元是手动 start 的，都会让两者不一致，
# 而「多跑了 22 条线」不会报任何错，只会在内存和 CF 配额上悄悄多花。
python3 - <<'PY'
import json, subprocess
from pathlib import Path
try:
    d = json.loads(Path('.warp/tiers.json').read_text())
except (OSError, ValueError):
    print('  ⚠ 远端没有 .warp/tiers.json → WG 全开（与本机档位无关）')
    raise SystemExit(0)
cur = d.get('current') or ''
spec = str((d.get('tiers') or {}).get(cur, {}).get('wg') or '')
keep = set()
for part in spec.replace(' ', '').split(','):
    if not part:
        continue
    try:
        if '-' in part:
            a, b = part.split('-', 1)
            keep.update('WARP-WG-%d' % i for i in range(int(a), int(b) + 1))
        else:
            keep.add('WARP-WG-%d' % int(part))
    except ValueError:
        pass
out = subprocess.run(['systemctl', 'list-units', '--no-legend', '--plain',
                      'warp-wg-*.service', '--state=active'],
                     capture_output=True, text=True).stdout
running = {l.split()[0].replace('warp-wg-', 'WARP-WG-').replace('.service', '')
           for l in out.splitlines() if l.strip()}
print('  档位 %s  声明保留 %d 条  实际在跑 %d 条' % (cur or '(无)', len(keep), len(running)))
extra = running - keep
missing = keep - running
if extra:
    print('  ✗ 多跑 %d 条不在档位内（%s）—— daemon 没重载档位？'
          % (len(extra), ', '.join(sorted(extra)[:5])))
if missing:
    print('  ⚠ 档位要求但没在跑 %d 条（%s）'
          % (len(missing), ', '.join(sorted(missing)[:5])))
if not extra and not missing:
    print('  ✓ 实际在跑的 WG 单元与档位一致')
PY
echo '--- 硬约束 ---'
printf '  warp-svc: %s / %s\n' \"\$(systemctl is-active warp-svc 2>&1)\" \"\$(systemctl is-enabled warp-svc 2>&1)\"
echo '--- selector ---'
S=\$(cat $REMOTE_HOME/.singbox_secret 2>/dev/null)
python3 - \"\$S\" <<'PY'
import json, sys, urllib.request
sec = sys.argv[1] if len(sys.argv) > 1 else ''
def get(path):
    r = urllib.request.Request('http://127.0.0.1:9090/proxies/' + path,
                               headers={'Authorization': 'Bearer ' + sec})
    return json.load(urllib.request.urlopen(r, timeout=5))
try:
    d = get('opencode-out')
except Exception as e:
    print('  读不到 selector:', e); raise SystemExit(0)
run = set(d.get('all') or [])
print('  成员 %d 条，当前 %s' % (len(run), d.get('now')))
# **必须比对，不能只报数字**。旧版只印条数，「成员少一半」永远看不出问题。
# 期望集 = 盘上配置里的 selector 成员；运行中少一条就是「改了文件没重载」。
try:
    cfg = json.load(open('singbox-config.json'))
    want = set(next(o for o in cfg['outbounds']
                    if o.get('tag') == 'opencode-out').get('outbounds') or [])
except Exception as e:
    print('  读不到盘上配置，跳过比对:', e); raise SystemExit(0)
missing = want - run
stale = run - want
print('  盘上 %d 条 / 运行中 %d 条' % (len(want), len(run)))
if missing:
    print('  ✗ 运行中缺 %d 条（%s）—— 主 sing-box 没重载？'
          % (len(missing), ', '.join(sorted(missing)[:5])))
if stale:
    print('  ⚠ 运行中多 %d 条已不在盘上（%s）'
          % (len(stale), ', '.join(sorted(stale)[:3])))
if not missing and not stale:
    print('  ✓ 运行中 selector 与盘上配置一致')
PY
echo '--- 出口抽样（WG + 每条 H2）---'
for p in \$(seq 1110 1112); do
    r=\$(timeout 8 curl -s --socks5-hostname 127.0.0.1:\$p https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -E '^(ip|warp|colo)=' | tr '\\n' ' ')
    echo \"  \$p \${r:-fail}\"
done
# H2 的端口来自台账（socks 1180+N），不是写死的区间。
python3 - <<'PY' > /tmp/asip-h2ports.txt 2>/dev/null
import json
for r in json.load(open('.warp/h2/channels.json')):
    print(r['socks_port'])
PY
h2ok=0; h2n=0
for p in \$(cat /tmp/asip-h2ports.txt 2>/dev/null); do
    h2n=\$((h2n+1))
    r=\$(timeout 10 curl -s --socks5-hostname 127.0.0.1:\$p https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -c '^warp=on' || true)
    [ \"\$r\" = 1 ] && h2ok=\$((h2ok+1))
done
echo \"  H2 \$h2ok/\$h2n 条 warp=on\"
echo '--- daemon 告警 ---'
sudo journalctl -u auto-switch-ip.service --since '90 seconds ago' --no-pager 2>/dev/null \
  | grep -E '⚠|不在白名单|不可达|400|404' | grep -v 'sudo\\[' | tail -5 || true"

say "完成"
echo "远端: $TARGET:$REMOTE_DIR"
echo "提示：共享池的可用率随时段变化，深夜验收数字偏低是正常现象"
echo "      （见 docs/established-facts.md 第 0 条与第 24 条）。"
