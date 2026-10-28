#!/bin/bash
# WARP-over-proxy 线路初始化：一条命令从零到可用（幂等，可在远端主机复现）。
#
#   ./deploy/wg-bootstrap.sh                 # 全流程
#   ./deploy/wg-bootstrap.sh --dry-run       # 只打印计划，不动系统
#   ./deploy/wg-bootstrap.sh --verify-only   # 只验收现有部署
#   ./deploy/wg-bootstrap.sh --resurvey      # 强制重测落地 colo
#   ./deploy/wg-bootstrap.sh --count=8       # 只铺 8 条（注册被限流时降级）
#   WARP_WG_FAMILIES=v4,v6,v6,v6 WARP_WG_NODES=4 ./deploy/wg-bootstrap.sh
#
# 流程：预检 → wgcf → .env → survey → 注册+铺线+单元 → 并入主配置 → 重启 → 验收
#
# 幂等保证（远端反复执行不会自伤）：
#   · 已注册的设备不重注册 —— 重注册会换掉在用密钥，隧道当场掉线
#   · .env 只补/改本脚本管的键，其余原样保留
#   · 单元文件重写，但只有内容变化时才重启对应单元
#   · 主配置没变化就不重启 sing-box（重启是断流操作）
#
# 以 root 跑时线路单元仍是普通用户（ASIP_RUN_USER / 仓库属主），
# 因为 daemon 以该用户运行、且要读 .warp 下的线路凭据。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYBIN="${PYTHON:-/usr/bin/python3}"
# 仓库属主 = 该跑线路的用户。本机唯一提权路径是 wsl -u root，所以脚本可能以
# root 跑；不把属主传下去，单元会写成 User=root，而 singbox.json 属 root、
# daemon 以属主身份读不到（Permission denied，16 条线全部 start-limit 循环）。
OWNER="${ASIP_RUN_USER:-$(stat -c %U "$DIR/daemon.py" 2>/dev/null || id -un)}"
OWNER_GROUP="${ASIP_RUN_GROUP:-$(id -gn "$OWNER" 2>/dev/null || id -gn)}"

DRY_RUN=0; VERIFY_ONLY=0; RESURVEY=0; DO_VERIFY=1; COUNT=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --verify-only) VERIFY_ONLY=1 ;;
        --resurvey) RESURVEY=1 ;;
        --no-verify) DO_VERIFY=0 ;;
        --count=*) COUNT="${arg#*=}" ;;
        --help|-h) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "✗ 未知参数: $arg（--help 看用法）" >&2; exit 2 ;;
    esac
done

ok()   { printf '✓ %s\n' "$*"; }
warn() { printf '⚠ %s\n' "$*" >&2; }
die()  { printf '✗ %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s ==\n' "$*"; }
# 变更动作统一走 run：dry-run 下只打印，不落手。
run() {
    if [ "$DRY_RUN" = 1 ]; then printf '[dry-run] %s\n' "$*"; return 0; fi
    "$@"
}
need_root() {
    if [ "$(id -u)" = 0 ]; then "$@"; else sudo -n "$@" || die "需要 root: $*"; fi
}
py() { PYTHONPATH="$DIR" "$PYBIN" "$@"; }
# 从 config.py 取一个表达式（stderr 丢掉：import 期会打 singbox secret 告警）
cfg() { py -c "import sys; sys.path.insert(0,'$DIR'); from config import CFG; print(eval(sys.argv[1]))" "$1" 2>/dev/null; }

SINGBOX="$(cfg 'CFG["singbox"]["binary"]')"
CONFIG="$(cfg 'CFG["singbox"]["config_file"]')"
BASE_PORT="$(cfg 'CFG["warp"]["wg"]["base_port"]')"
WGCF="${WGCF_BIN:-$(cfg 'CFG["warp"]["wg"]["wgcf_bin"]')}"
API_SOCK="$(cfg 'CFG["api"]["socket_path"]')"
API_TOKEN_FILE="$(cfg 'CFG["api"]["token_path"]')"
STATE="$(py -c "import sys; sys.path.insert(0,'$DIR'); import warp_wg; print(warp_wg.state_path())" 2>/dev/null)"

# ---- .env 键写入：有就替换、没有就追加，绝不重复追加 ----
set_key() {
    local k="$1" v="$2" f="$DIR/.env"
    [ "$DRY_RUN" = 1 ] && { printf '[dry-run] .env %s=%s\n' "$k" "$v"; return 0; }
    "$PYBIN" - "$f" "$k" "$v" <<'PY'
import sys, re, pathlib
f, k, v = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = f.read_text().splitlines() if f.exists() else []
pat = re.compile(r'^\s*' + re.escape(k) + r'=')
hit = False
out = []
for ln in lines:
    if pat.match(ln):
        if not hit:
            out.append(f'{k}={v}'); hit = True
        continue
    out.append(ln)
if not hit:
    out.append(f'{k}={v}')
f.write_text('\n'.join(out) + '\n')
PY
}
del_key() {
    local k="$1" f="$DIR/.env"
    [ "$DRY_RUN" = 1 ] && { printf '[dry-run] .env 删 %s\n' "$k"; return 0; }
    "$PYBIN" - "$f" "$k" <<'PY'
import sys, re, pathlib
f, k = pathlib.Path(sys.argv[1]), sys.argv[2]
if not f.exists():
    raise SystemExit(0)
pat = re.compile(r'^\s*' + re.escape(k) + '=')
keep = [ln for ln in f.read_text().splitlines() if not pat.match(ln)]
f.write_text('\n'.join(keep) + '\n')
PY
}

# 线路条数（以状态文件为准：它是 apply 的产物，比 .env 更接近事实）
line_count() {
    [ -s "$STATE" ] || { echo 0; return; }
    py -c "import json,sys; print(len(json.load(open(sys.argv[1])).get('lines',{})))" "$STATE" 2>/dev/null || echo 0
}

# ============================== 1 预检 ==============================
if [ "$VERIFY_ONLY" = 0 ]; then
step "1/7 预检"
[ -x "$PYBIN" ] || die "解释器不存在：$PYBIN（用 PYTHON=/path 指定）"
"$PYBIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' \
    || die "Python 需 >= 3.10：$("$PYBIN" -V 2>&1)"
[ -x "$SINGBOX" ] || die "sing-box 不存在或不可执行：$SINGBOX（先跑 deploy/fetch-binaries.sh）"
[ -n "$BASE_PORT" ] || die "读不到 config.py 的 warp.wg.base_port（导入失败？）"
command -v curl >/dev/null 2>&1 || die "缺少 curl"
command -v systemctl >/dev/null 2>&1 || die "缺少 systemctl（WSL 需 /etc/wsl.conf 设 [boot] systemd=true）"
# wireguard 内核模块：没有它 sing-box 的 wireguard endpoint 起不来
if ! grep -qw wireguard /proc/modules 2>/dev/null; then
    if [ "$(id -u)" = 0 ] || sudo -n true 2>/dev/null; then
        need_root modprobe wireguard 2>/dev/null || true
    fi
fi
if grep -qw wireguard /proc/modules 2>/dev/null; then
    ok "wireguard 内核模块已加载"
else
    warn "wireguard 模块未加载：内核未内置时装 linux-modules-extra-\$(uname -r)；WSL 用 wsl --update"
    warn "  缺它时线路进程会反复 FATAL 退出（start-limit 循环）"
fi
# 写 /etc/systemd/system 与 systemctl enable 都要 root
if [ "$(id -u)" != 0 ]; then
    sudo -n true 2>/dev/null || die "需要免密 sudo（本机 sudoers 只放行 asip-unit），或直接以 root 跑"
fi
ok "预检通过（项目 $DIR，线路用户 $OWNER，端口基址 $BASE_PORT）"

# ============================== 2 wgcf ==============================
step "2/7 wgcf"
if [ -x "$WGCF" ] && "$WGCF" --help >/dev/null 2>&1; then
    ok "wgcf 可用：$WGCF"
else
    case "$(uname -m)" in
        x86_64|amd64) ARCH=amd64 ;;
        aarch64|arm64) ARCH=arm64 ;;
        *) die "wgcf 无此架构构建：$(uname -m)" ;;
    esac
    VER="${WGCF_VERSION:-2.2.28}"
    URL="https://github.com/ViRb3/wgcf/releases/download/v$VER/wgcf_${VER}_linux_$ARCH"
    if [ "$DRY_RUN" = 1 ]; then
        printf '[dry-run] 下载 wgcf: %s\n' "$URL"
    else
        mkdir -p "$(dirname "$WGCF")"
        got=0
        # GitHub 直连在部分网络被拦（本机实测被拦），退回本机 sing-box 代理。
        if curl -fsSL --max-time 300 -o "$WGCF.part" "$URL" 2>/dev/null; then
            got=1
        else
            for p in "${https_proxy:-}" "${HTTPS_PROXY:-}" "http://127.0.0.1:7890"; do
                [ -n "$p" ] || continue
                warn "直连 GitHub 失败，改走代理 $p"
                if curl -fsSL --max-time 300 --proxy "$p" -o "$WGCF.part" "$URL" 2>/dev/null; then got=1; break; fi
            done
        fi
        [ "$got" = 1 ] && [ -s "$WGCF.part" ] \
            || die "wgcf 下载失败（直连与代理都不通）：手动放到 $WGCF 后重跑"
        chmod +x "$WGCF.part"
        "$WGCF.part" --help >/dev/null 2>&1 \
            || die "wgcf 不能执行（架构不符？）：$("$WGCF.part" --help 2>&1 | head -2)"
        mv "$WGCF.part" "$WGCF"
        chown "$OWNER:$OWNER_GROUP" "$WGCF" 2>/dev/null || true
        ok "wgcf 已安装：$WGCF"
    fi
fi

# ============================== 3 .env ==============================
step "3/7 .env"
if [ ! -f "$DIR/.env" ]; then
    if [ -f "$DIR/.env.example" ]; then
        run cp "$DIR/.env.example" "$DIR/.env"
        ok "由 .env.example 生成 .env（订阅源等按需再填）"
    else
        run touch "$DIR/.env"
        ok "新建空 .env"
    fi
fi
if [ -n "$COUNT" ]; then
    # 显式条数：与 WARP_WG_FAMILIES 互斥（后者按 每节点渠道数 × 节点数 算总数）
    [[ "$COUNT" =~ ^[0-9]+$ ]] && [ "$COUNT" -gt 0 ] || die "--count 需正整数：$COUNT"
    set_key WARP_WG_COUNT "$COUNT"
    del_key WARP_WG_FAMILIES
    del_key WARP_WG_NODES
    TOPO="WARP_WG_COUNT=$COUNT（全部 v6、单落地节点）"
else
    WARP_WG_FAMILIES="${WARP_WG_FAMILIES:-v4,v6,v6,v6}"
    WARP_WG_NODES="${WARP_WG_NODES:-4}"
    export WARP_WG_FAMILIES WARP_WG_NODES
    unset WARP_WG_COUNT
    set_key WARP_WG_FAMILIES "$WARP_WG_FAMILIES"
    set_key WARP_WG_NODES "$WARP_WG_NODES"
    del_key WARP_WG_COUNT
    PER=$(printf '%s' "$WARP_WG_FAMILIES" | awk -F, '{print NF}')
    TOPO="$WARP_WG_FAMILIES × $WARP_WG_NODES 个落地节点 = $((PER * WARP_WG_NODES)) 条"
fi
ok "拓扑：$TOPO"

# ============================== 4 survey ==============================
step "4/7 落地 colo 实测"
COLO="$DIR/.warp/wg_colo_map.json"
if [ "$RESURVEY" = 1 ] || [ ! -s "$COLO" ]; then
    warn "survey 要逐节点起 sing-box 实测（几十个节点，约 10-20 分钟）"
    if [ "$DRY_RUN" = 1 ]; then
        printf '[dry-run] %s %s survey\n' "$PYBIN" "$DIR/warp_wg.py"
    else
        py "$DIR/warp_wg.py" survey || die "survey 失败（节点池或代理链路不可用）"
    fi
else
    ok "已有 colo 地图（$COLO，$(stat -c %y "$COLO" | cut -c1-16)）；--resurvey 可重测"
fi
if [ "$DRY_RUN" = 0 ] && [ -s "$COLO" ]; then
    py -c "import json,sys,collections; d=json.load(open(sys.argv[1])); n=d.get('nodes',{}); c=collections.Counter((v.get('colo'),v.get('loc')) for v in n.values() if v.get('ok')); print('  colo 分布: ' + ', '.join(f'{k[0]}/{k[1]}×{v}' for k,v in c.most_common()) or '  colo 分布: 空')" "$COLO" 2>/dev/null || true
fi

# ============================== 5 注册与铺线 ==============================
step "5/7 注册设备 + 生成线路 + 系统级单元"
export ASIP_RUN_USER="$OWNER" ASIP_RUN_GROUP="$OWNER_GROUP" BASE_PORT="$BASE_PORT"
if [ -n "$COUNT" ]; then export WARP_WG_COUNT="$COUNT"; else export WARP_WG_FAMILIES WARP_WG_NODES; fi

# apply 会按拓扑重写每条线的 singbox.json。配置变了而进程还跑旧的，就是
# 「配置里是 v4、线上还在 v6」这种静默不一致 —— 只有重启该线才生效。
# 用内容哈希判定，没变就不重启（重启线路 = 换 WARP 会话 = 换出口 IP）。
declare -A CFG_H0=()
snapshot_line_cfgs() {
    CFG_H0=()
    local f
    for f in "$DIR"/.warp/wglines/line*/singbox.json; do
        [ -f "$f" ] || continue
        CFG_H0["$f"]="$(sha256sum "$f" | awk '{print $1}')"
    done
}
restart_changed_lines() {
    local f h n changed=0
    for f in "$DIR"/.warp/wglines/line*/singbox.json; do
        [ -f "$f" ] || continue
        h="$(sha256sum "$f" | awk '{print $1}')"
        [ "${CFG_H0[$f]:-}" = "$h" ] && continue
        n="$(basename "$(dirname "$f")" | sed 's/^line//')"
        warn "line$n 配置已变，重启该线生效"
        need_root systemctl restart "warp-wg-$n.service" 2>/dev/null || true
        changed=$((changed + 1))
    done
    [ "$changed" = 0 ] || { ok "已重启 $changed 条配置变更的线路"; sleep 10; }
}

snapshot_line_cfgs
install_lines() { bash "$DIR/deploy/wg-lines-install.sh"; }
if [ "$DRY_RUN" = 1 ]; then
    printf '[dry-run] ASIP_RUN_USER=%s bash %s/deploy/wg-lines-install.sh\n' "$OWNER" "$DIR"
else
    if ! install_lines; then
        READY=$(ls -d "$DIR"/.warp/wglines/line*/profile.conf 2>/dev/null | wc -l)
        [ "$READY" -gt 0 ] || die "一条线都没注册成功（Cloudflare 限流或网络不通），稍后重跑本脚本"
        if [ -z "$COUNT" ] && [ "$((PER * WARP_WG_NODES))" -le "$READY" ]; then
            die "拓扑参数没传到位（已就绪 $READY 条却报错），本次不降级：见上方错误"
        fi
        # 降级必须保住渠道配比：先按整组减落地节点（v4 通道仍在），
        # 实在除不尽才退回单渠道 COUNT —— 那会丢掉 v4，必须说清楚。
        if [ -z "$COUNT" ] && [ "$((READY / PER))" -ge 1 ] && [ "$((READY / PER * PER))" -eq "$READY" ]; then
            DOWN=$((READY / PER))
            warn "注册被限流：已就绪 $READY 条，降级为 $DOWN 个落地节点（渠道配比不变）"
            warn "  窗口过去后重跑本脚本即可补齐（已注册的设备不会重注册）"
            WARP_WG_NODES="$DOWN"
            export WARP_WG_NODES
            set_key WARP_WG_NODES "$DOWN"
        else
            warn "注册被限流：已就绪 $READY 条，不是每节点 $PER 渠道的整数倍"
            warn "  退回单渠道铺开（只剩 v6，v4 通道丢失）；窗口过去后重跑可恢复"
            WARP_WG_COUNT="$READY"
            export WARP_WG_COUNT
            unset WARP_WG_FAMILIES WARP_WG_NODES
            set_key WARP_WG_COUNT "$READY"
            del_key WARP_WG_FAMILIES; del_key WARP_WG_NODES
        fi
        install_lines || die "降级铺开仍失败，见上方错误"
    fi
    # 必须先把属主交还给运行用户，再重启：顺序反了就是 permission denied 崩溃循环。
    # 以 root 跑时 apply 生成的凭据属 root，而单元以 $OWNER 运行 —— 必须交还属主。
    chown -R "$OWNER:$OWNER_GROUP" "$DIR/.warp/wglines" "$STATE" \
        "$DIR/.warp/wg_colo_map.json" "$DIR/.warp/wg_node_udp.json" \
        "$DIR/.warp/warp-outbound.json" 2>/dev/null || true
    restart_changed_lines
fi
N="$(line_count)"
[ "$DRY_RUN" = 1 ] || [ "$N" -gt 0 ] || die "状态文件里没有线路：$STATE"
ok "线路 $N 条，端口 $BASE_PORT-$((BASE_PORT + N - 1))"
# ============================== 6 主配置 ==============================
step "6/7 并入主配置（无订阅期的必要步骤）"
# 订阅源为空时 refresh_subscription 直接返回「无可用订阅源」，singbox-config.json
# 不再重生成 —— 新铺的出口进不了 opencode-out selector，换线全部 status=400。
# 订阅配回来后 parse_subscription 会接管，本步自动退化为 no-op。
restart_main_singbox() {
    # 只杀主进程：WG 线各跑一份 run -c .../lineN/singbox.json，宽匹配会把 16 条
    # 一起杀掉，每条线换一次 WARP 会话就换一个出口 IP，台账绑定当场作废。
    need_root pkill -f "run -c $CONFIG" 2>/dev/null || true
    for _ in $(seq 1 24); do
        sleep 5
        if pgrep -f "run -c $CONFIG" >/dev/null 2>&1; then
            ok "主 sing-box 已由 daemon 拉起"
            return 0
        fi
    done
    warn "daemon 未在 120s 内拉起主 sing-box，手动启动"
    if [ "$(id -un)" = "$OWNER" ]; then
        ( cd "$DIR" && setsid "$SINGBOX" run -c "$CONFIG" >>"$DIR/singbox.log" 2>&1 & )
    else
        ( cd "$DIR" && setsid runuser -u "$OWNER" -- "$SINGBOX" run -c "$CONFIG" >>"$DIR/singbox.log" 2>&1 & )
    fi
}

if [ "$DRY_RUN" = 1 ]; then
    printf '[dry-run] %s %s/deploy/patch-wg-outbounds.py\n' "$PYBIN" "$DIR"
else
    PATCH_OUT="$(py "$DIR/deploy/patch-wg-outbounds.py")" || die "并入失败：$PATCH_OUT"
    printf '  %s\n' "$PATCH_OUT"
    case "$PATCH_OUT" in
        CHANGED*)
            "$SINGBOX" check -c "$CONFIG" >/dev/null 2>&1 \
                || die "并入后 sing-box check 失败，主配置可能已损坏（有 .bak 备份）"
            ok "配置校验通过"
            restart_main_singbox
            ;;
        *) ok "主配置已含全部出口，跳过重启（重启是断流操作）" ;;
    esac
fi

# daemon 的成员表是启动时算的：拓扑变了（条数/渠道）必须重启才认得新线。
MEMBERS_NOW="$(curl -s --max-time 5 --unix-socket "$API_SOCK" \
    -H "X-Asip-Token: $(cat "$API_TOKEN_FILE" 2>/dev/null)" \
    http://localhost/debug/state 2>/dev/null \
    | py -c "import json,sys; d=json.load(sys.stdin); print(sum(1 for k in (d.get('line_ip') or {}) if k.startswith('WARP-WG-')))" 2>/dev/null || echo 0)"
if [ "$DRY_RUN" = 0 ]; then
    if [ "${MEMBERS_NOW:-0}" != "$N" ]; then
        warn "daemon 认得 ${MEMBERS_NOW:-0} 条线，实际 $N 条 —— 重启 daemon 让成员表跟上"
        need_root systemctl restart auto-switch-ip.service
        for _ in $(seq 1 12); do
            sleep 5
            [ "$(systemctl is-active auto-switch-ip.service 2>/dev/null)" = active ] && break
        done
        [ "$(systemctl is-active auto-switch-ip.service 2>/dev/null)" = active ] \
            || die "daemon 重启后不是 active，看 journalctl -u auto-switch-ip -n 50"
        ok "daemon 已重启"
    else
        ok "daemon 成员表已是 $N 条，无需重启"
    fi
fi

fi   # VERIFY_ONLY=0 的初始化段到此为止

# 验收段 --verify-only 也要跑：它读的是现状，不改任何东西。
N="$(line_count)"

# ============================== 7 验收 ==============================
if [ "$DO_VERIFY" = 1 ]; then
# 取一条线的出口：ip / colo / warp 三个字段
probe_line() {
    local port="$1" raw
    raw="$(curl -s --max-time 15 --socks5-hostname "127.0.0.1:$port" \
        https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | tr '\n' ' ' || true)"
    IP="$(printf '%s' "$raw" | tr ' ' '\n' | sed -n 's/^ip=//p')"
    CO="$(printf '%s' "$raw" | tr ' ' '\n' | sed -n 's/^colo=//p')"
    WR="$(printf '%s' "$raw" | tr ' ' '\n' | sed -n 's/^warp=//p')"
}

step "7/7 验收"
FAIL=0
ACTIVE=0
REPAIRED=0
for i in $(seq 1 "${N:-0}"); do
    st=""
    for _ in 1 2 3; do
        st="$(systemctl is-active "warp-wg-$i.service" 2>/dev/null)"
        [ "$st" = active ] && break
        sleep 2
    done
    if [ "$st" = active ]; then
        ACTIVE=$((ACTIVE + 1))
    else
        warn "warp-wg-$i.service 不是 active：journalctl -u warp-wg-$i -n 20"
        FAIL=$((FAIL + 1))
    fi
done
ok "单元 active $ACTIVE/$N"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
for i in $(seq 1 "${N:-0}"); do
    PORT=$((BASE_PORT + i - 1))
    probe_line "$PORT"
    # 进程活着不等于隧道活着：WARP 会话静默失效时 sing-box 仍在监听，
    # 只有真发一个请求才看得出来。重启该线一次是唯一有效的自愈手段。
    if { [ -z "$IP" ] || [ "$WR" != "on" ]; } && [ "$DRY_RUN" = 0 ]; then
        warn "line$i 无出口，重启该线后重测"
        need_root systemctl restart "warp-wg-$i.service" 2>/dev/null || true
        sleep 10
        probe_line "$PORT"
        [ -n "$IP" ] && [ "$WR" = on ] && REPAIRED=$((REPAIRED + 1))
    fi
    if [ -z "$IP" ]; then
        printf '  line%-2s :%s  ✗ 无出口（进程在但不通）\n' "$i" "$PORT"
        FAIL=$((FAIL + 1))
    elif [ "$WR" != "on" ]; then
        printf '  line%-2s :%s  ✗ warp=%s（出口没走 WARP）\n' "$i" "$PORT" "${WR:-?}"
        FAIL=$((FAIL + 1))
    else
        printf '  line%-2s :%s  %-8s %s\n' "$i" "$PORT" "${CO:-?}" "$IP"
    fi
    printf '%s\n' "$IP" >> "$TMP"
done
DISTINCT="$(grep -v '^$' "$TMP" 2>/dev/null | sort -u | wc -l)"
ok "出口去重 $DISTINCT/$N（同 colo 共享地址池，去重数 = v4 渠道多样性上限）"

# /rotate 探活：proactive 不拉黑旧出口，适合当冒烟测试
if [ "$DRY_RUN" = 1 ]; then
    printf "[dry-run] 跳过 /rotate 探活（会真的切一次出口）\n"
elif [ -S "$API_SOCK" ] && [ -f "$API_TOKEN_FILE" ]; then
    ROT="$(curl -s --max-time 40 --unix-socket "$API_SOCK" \
        -H "X-Asip-Token: $(cat "$API_TOKEN_FILE")" \
        -X POST http://localhost/rotate -d '{"reason":"proactive"}' 2>/dev/null || true)"
    case "$ROT" in
        *'"ok": true'*) ok "/rotate 探活通过：$ROT" ;;
        *too_frequent*) warn "/rotate 限速中（正常，刚切过）：$ROT" ;;
        '') warn "/rotate 无响应（daemon 的接口未启用？）" ; FAIL=$((FAIL + 1)) ;;
        *) warn "/rotate 返回异常：$ROT" ;;
    esac
else
    warn "rotate 接口未就绪（$API_SOCK），跳过探活"
fi

printf '\n'
[ "$REPAIRED" = 0 ] || ok "自愈 $REPAIRED 条（重启后恢复）"
if [ "$FAIL" = 0 ]; then
    ok "验收通过：$N 条线全部 warp=on，出口 $DISTINCT 个"
else
    warn "验收有 $FAIL 项不达标（见上）；修完重跑 --verify-only 复查"
fi
fi

# ============================== 收尾 ==============================
printf '\n== 完成 ==\n'
printf '  线路 %s 条  端口 %s-%s  用户 %s\n' "${N:-0}" "$BASE_PORT" "$((BASE_PORT + ${N:-0} - 1))" "$OWNER"
printf '  下一步：\n'
printf '    asip warp status                     # 线路总览\n'
printf '    asip warp rotate                     # 手动换一次出口\n'
printf '    9router 侧：429/403 时 POST /rotate（接口 %s）\n' "$API_SOCK"
printf '  重跑本脚本是幂等的：补线、补配置、不重注册已有设备。\n'
[ "${FAIL:-0}" = 0 ] || exit 1
exit 0