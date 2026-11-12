#!/usr/bin/env bash
# auto-switch-ip 日志快捷命令 —— 用法见 `asip -h`（命令清单只在 _usage 里维护一份）
#
# 本脚本不含任何站点设置：日志路径、密钥、Clash API、selector、隧道清单全部来自
# deploy/asip-cli-conf.sh 在部署时生成的设置文件。写死过一份的下场是换目录/改端口
# 后仍指向旧位置，而症状（日志不动、隧道全「不可达」）看起来像服务故障。
ASIP_CLI_ENV="${ASIP_CLI_ENV:-$HOME/.config/asip/cli.env}"
if [ ! -r "$ASIP_CLI_ENV" ]; then
  echo "✗ 缺少 asip 设置: $ASIP_CLI_ENV" >&2
  echo "  在项目目录里跑 ./deploy/install.sh（只补设置可跑 ./deploy/asip-cli-conf.sh）" >&2
  exit 1
fi
# shellcheck source=/dev/null
. "$ASIP_CLI_ENV"
# 内嵌 Python 用的是 os.environ（不解析 shell 变量），必须导出一份：
# 台账路径和日志路径都由设置文件提供，漏导会让 warp ip / cycles 直接 KeyError。
export ASIP_IP_USED_FILE ASIP_CYCLES_FILE ASIP_LOG ASIP_GATE_LOG ASIP_CLASH_API ASIP_PY
export ASIP_LINE_STATE_FILE
# 隧道探测走 xargs 起的子 shell，主机名必须导出才能在单引号脚本里取到。
export ASIP_SOCKS_HOST

LOG=$ASIP_LOG
# 第七闸口单独一份账本。旧设置文件（没跑过 asip-cli-conf.sh）按项目目录推导；
# 服务还没重启 / gate.log 还没生成时回退主日志，命令不会空转。
GATE_LOG=${ASIP_GATE_LOG:-${ASIP_DIR:-$(dirname "$LOG")}/gate.log}
[ -r "$GATE_LOG" ] || GATE_LOG=$LOG
SECRET_FILE=$ASIP_SECRET_FILE
# 隧道清单由部署期设置提供（`tag port service` 每行一条，源自 config.py 的
# warp.members），本脚本不再解析 config.py —— 那样等于在脚本里维护第二份清单。
PORTS=$(awk '{print $2}' <<<"$ASIP_WARP_MEMBERS")
UNITS=$(awk '{print $3}' <<<"$ASIP_WARP_MEMBERS")

if [ -z "$PORTS" ] || [ -z "$UNITS" ]; then
  echo "✗ asip 设置里没有隧道清单（$ASIP_CLI_ENV），重跑 ./deploy/asip-cli-conf.sh" >&2
  exit 1
fi
# 白名单：端口必须纯数字；单元名不能含 / 或 ..，否则 warp stop/start
# 会以 root 对任意路径做 ln -sf /dev/null，且 xargs -I{} 把 port 拼进
# sh -c 脚本体（字符串替换，非 argv 传参）。
# ASIP_UNIT_DIR 同样要判空：缺了它 $f 会退化成根目录下的单元名，
# 免密 sudo 下就在 / 里建软链。
[ -n "$ASIP_UNIT_DIR" ] || {
  echo "✗ 设置缺少 ASIP_UNIT_DIR（$ASIP_CLI_ENV），重跑 ./deploy/asip-cli-conf.sh" >&2; exit 1; }
valid=1; checked=0
while read -r _ p u; do
  [ -n "$p" ] || continue
  checked=$((checked + 1))
  case "$p" in *[!0-9]*)
    echo "✗ 非数字端口: $p（来自 $ASIP_CLI_ENV 的成员行）" >&2; valid=0 ;;
  esac
  case "$u" in ''|*/*|..*)
    echo "✗ 非法单元名: $u" >&2; valid=0 ;;
  esac
done <<< "$ASIP_WARP_MEMBERS"
# checked 守卫不可省：格式整体变了（如从空格改逗号）时循环一次都不进，
# valid 仍是 1，等于校验形同虚设。
[ "$checked" -gt 0 ] || { echo "✗ 成员行格式不认识（期望「tag port service」）" >&2; exit 1; }
[ "$valid" = 1 ] || exit 1

# 单元从用户级迁到系统级后，--user 会作用在一个不存在的 unit 上并"静默成功"，
# 于是 asip warp stop 报成功而隧道照跑。作用域由部署期探测结果决定，不猜。
if [ "$ASIP_SYSTEMD_SCOPE" = system ]; then
  if [ "$(id -u)" = 0 ]; then SCTL=(systemctl); else SCTL=(sudo -n systemctl); fi
  JCTL=(journalctl)
else
  SCTL=(systemctl --user)
  JCTL=(journalctl --user)
fi
# 查询类动词不需要 root：systemctl is-active 普通用户就能问，而本机 sudoers 只放行了
# asip-unit，走 sudo -n 会直接报"需要交互认证"，把状态打成空白。但 --user 不能丢：
# 用户级部署里不带它的 is-active 查的是**系统**总线，会把每条隧道都报成 inactive。
_sctl() {
  case "${1:-}" in
    is-active|is-enabled|status|show)
      if [ "$ASIP_SYSTEMD_SCOPE" = system ]; then systemctl "$@"; else systemctl --user "$@"; fi ;;
    *) "${SCTL[@]}" "$@" ;;
  esac
}
_jctl() { "${JCTL[@]}" "$@"; }
# 系统级单元目录在 /etc 下：屏蔽/还原要写文件，非 root 必须借 sudo。
# 失败必须冒泡：没有 set -e，静默失败的 sudo 会让 stop 报成功而单元原样还在。
_as_root() {
  if [ "$ASIP_SYSTEMD_SCOPE" = system ] && [ "$(id -u)" != 0 ]; then
    sudo -n "$@" || { echo "✗ 需要 root 权限：$*" >&2; return 1; }
  else
    "$@"
  fi
}

_usage() {
  cat <<'EOF'
asip — auto-switch-ip 日志与出口诊断

日志
  asip                 跟随全部日志（先回看 50 行）
  asip <N>             只看最后 N 行
  asip s               跟随换线全过程：触发/选线/兜底（回看 2000 行）
  asip e               跟随错误与异常（回看 2000 行）

第七闸口（代理池兜底 slot-7）
  asip gate            状态快照 + asip / 9router 两侧最近日志
  asip gate log [N]    跟随独立账本 gate.log（默认回看 200 行；daemon.log 不再收池闸行）
  asip gate events [N] 只看守护/秒切/池内轮换事件，滤掉高频 429 报障
  asip gate 9r [N]     9router 侧切线与重放日志（默认 30 行，-f 跟随）
  asip gate -h         本子命令帮助

诊断
  asip st              状态快照：出口模式 / 当前节点 / 近 1h 切换次数
  asip warp status     各隧道状态 + 各自出口 IP + 当前出口
  asip warp ip         出口 IP 使用记录（是否被标记、多久以前）
  asip warp cycles     24h IP 可用周期样本与生效的最小周期
  asip chan            渠道健康：成功率/ban 率/机房/colo/出口段/订阅源（-h 看参数）
  asip chan --html F   同上，输出可直接打开的 HTML
  asip blk             块视图：每个 colo 块有哪些节点、延迟、谁在用
  asip blk --blocks    块池视角：块内**全部**节点，标出哪几个排成线位、哪些是候补
  asip blk --colo NRT  只看一个块；加 --delay 现测延迟（慢，并发会打满出口）
  asip blk --channels  渠道视角：每条渠道选中哪个节点、候选池多大、和谁撞同一台
  asip blk --pools     附带打印每块的完整候选节点池

  承载口径（不再是笼统的 proxy）：
    warp-proxy   WG over UDP，经机场节点中转     h2-warp   MASQUE over HTTP/2
    warp-direct  直连 WARP，不经节点              proxy     纯代理，不承载 WARP

WARP 控制
  asip warp start      解除屏蔽并启动全部隧道
  asip warp stop       切轮换池 + 停止并屏蔽隧道
  asip warp restart    stop 后 start

隧道日志
  asip warp log        跟随 daemon 日志中与 WARP 换线相关的行
  asip warp jlog       跟随 usque 隧道 journal（默认滤噪）
  asip warp jlog all   同上，不过滤
  asip vercel status   Vercel 中继：台账 + 9router 池/策略 + 开关
  asip vercel probe    经每条中继探出口 IP（-h 看全部子命令）

订阅
  asip sub             列已配置的订阅源（固定 + 追加）
  asip sub add <url>   插入一个订阅源并立即刷新（不必重启 daemon）
  asip sub rm <url|N>  移除一个追加源（按 URL 或上表序号）并立即刷新
  asip sub refresh     只触发一次刷新（源没变、想立刻重拉时用）

收件箱（Sister Bus，各角色报给本项目的消息）
  asip inbox           本项目相关的未读消息（关键词过滤，约 4%）
  asip inbox all       不过滤，看全部未读
  asip inbox tail      只看最新 20 条（不管是否相关）
  asip inbox watch     每 5s 刷新，只为新到的消息打一行
  asip inbox cats      （全量）各分类未读计数；tail 只取最近 N 条
                       ASIP_INBOX_LIMIT=500 可减少拉取条数（默认 5000）

  -h, --help           本帮助
EOF
}

# daemon 用 RotatingFileHandler 轮转（5MB×5），tail -f 过轮转点后就停在旧 inode 无限空转，
# 必须用 -F 按文件名重开；先打一段历史，否则跟随前长时间只见空白。
_follow() {
  local re=$1 scan=${2:-50} log=${3:-$LOG}
  echo "── 回看最近 $scan 行，之后 live（Ctrl-C 退出）──" >&2
  if [ -n "$re" ]; then
    # 匹配率低（近 300 行里换线行 128、错误行个位数），至少回看 2000 行才不显空。
    # gate.log 是池闸专属文件，行行都命中，不用按主日志的稀疏比例回看。
    tail -F -n $scan "$log" 2>/dev/null | grep --line-buffered -E "$re"
  else
    tail -F -n $scan "$log" 2>/dev/null
  fi
}

# 换线全过程：换线触发/成功/失败、选线、缓存命中、脏 IP、会话刷新、补刷、轮换池兜底。
_RE_SW='切代理|切到|❄|WARP|轮换池|选线|会话已刷新|脏 ?IP|补刷|兜底|出口模式'
# 不能用裸「错误」「失败」「冷却」：健康行每 10s 一条，含「错误=N/10」「冷却至=」
# 与「探测失败」样板，三个词都会把过滤器放大成全量。只认行首错误标记与异常。
_RE_ERR='^\[[^]]*\] +(✗|⚠|⇄)|Traceback|ERROR|不可达|异常:'
# usque 每 20s 一条的固有噪声：空闲/重连心跳、TCP-only 提示、探测连接主动断开。
_RE_NOISE='Tunnel idle|Detected outbound activity|l4-socks|SOCKS client .* closed during negotiation'
# 第七闸口：全量口径含高频「slot-7 报 429 / 重试改走 slot-7」（实测约 50 行/min）。
# events 窄口径只留守护、秒切、池内轮换，用来回答「闸口本身有没有在动」。
# 与 daemon.py 的 _GATE_LOG_RE 保持同一份口径（有单测盯着，别改一边）。
_RE_GATE='第七闸口|秒切兜底|slot-7|池闸|代理池兜底|池内轮换'
_RE_GATE_EVENT='第七闸口|秒切兜底'

_singbox() {
  local method=$1 path=$2 body=${3:-}
  local secret
  secret=$(cat "$SECRET_FILE" 2>/dev/null)
  # 走 -H @file 而不是 -H "Authorization: ..."：argv 是 644 的 /proc/<pid>/cmdline，
  # 直接放命令行时同机任意用户都能在 5 秒超时窗口内读到 token。
  local hdr rc=0
  hdr=$(mktemp) || return 1
  ( umask 077; printf 'Authorization: Bearer %s\n' "$secret" > "$hdr" )
  if [ -n "$body" ]; then
    curl -s --max-time 5 -X "$method" -H @"$hdr" \
         -H 'Content-Type: application/json' -d "$body" "$ASIP_CLASH_API$path" || rc=$?
  else
    curl -s --max-time 5 -X "$method" -H @"$hdr" "$ASIP_CLASH_API$path" || rc=$?
  fi
  rm -f "$hdr"
  return $rc
}

_exit_now() {
  local now=$(_singbox GET "/proxies/$ASIP_WARP_SELECTOR" | grep -oP '"now"\s*:\s*"\K[^"]+')
  echo "${now:-查询失败}"
}

# ── 第七闸口（代理池兜底）：只读诊断 ──────────────────────────────────────
# 证据分散在三处，单看任何一处都会得出错误结论：
#   daemon.log      选它 / 报障 / 池内轮换
#   9router journal 是否真把请求重放到池闸（asip 写了 to=slot-7 ≠ 重放成功）
#   /slots          进程健康、当前池节点、常规闸口还剩几个可选
# 全部只读，不 PUT、不触发刷新。
_asip_uds_get() {  # $1=path；走 rotate.sock + token
  local tok path=$1
  tok=$(cat "$ASIP_DIR/run/api-token" 2>/dev/null) || return 1
  "$ASIP_PY" - "$ASIP_DIR" "$tok" "$path" <<'PY'
import http.client, socket, sys
repo, tok, path = sys.argv[1], sys.argv[2], sys.argv[3]
class C(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=8)
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(8)
        self.sock.connect(f"{repo}/run/rotate.sock")
try:
    c = C()
    c.request("GET", path, headers={"X-Asip-Token": tok})
    r = c.getresponse()
    sys.stdout.write(r.read().decode("utf-8", "replace"))
    sys.exit(0 if 200 <= r.status < 300 else 1)
except (OSError, ValueError) as e:
    print(f"✗ 读 {path} 失败: {e}", file=sys.stderr)
    sys.exit(1)
PY
}

_gate_usage() {
  cat <<'EOF'
asip gate — 第七闸口（代理池兜底 slot-7）日志与状态

  asip gate            状态快照 + asip / 9router 两侧最近日志
  asip gate st [N]     同上，asip 侧最近 N 行（默认 12）
  asip gate log [N]    跟随第七闸口专属日志 gate.log（默认回看 200 行）
  asip gate events [N] 只看守护/秒切/池内轮换事件（默认 30 行，不跟随）
  asip gate 9r [N]     9router 侧切线/重放日志（默认 30 行；-f 跟随）
  asip gate -h         本帮助

  ASIP_ROUTER_SERVICE 可覆盖 9router 的 systemd 单元名（默认 9router.service）。
  ASIP_GATE_LOG 可覆盖独立日志路径（由 asip-cli-conf.sh 写入设置）；文件不存在时
  自动回退 daemon.log。daemon.log 默认不再收池闸行，journalctl 仍是全量账本。
  池闸只在六个常规闸口都不可选时才会被选中；全部命令只读。
EOF
}

# 别把 abc 塞给 tail/journalctl 后让人去猜那句英文报错。
_gate_num() {
  case "${1:-}" in
    ''|*[!0-9]*) echo "✗ 行数需为正整数，收到 '${1:-}'" >&2; return 1 ;;
  esac
}

_gate_slots() {  # $1=目标文件
  _asip_uds_get /slots >"$1" 2>/dev/null && [ -s "$1" ]
}

_gate_port_from_file() {  # $1=/slots JSON；端口从运行时读，不写死
  "$ASIP_PY" - "$1" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    print(next((s.get("port") or "" for s in d.get("slots", [])
                if s.get("kind") == "proxy"), ""))
except (OSError, ValueError):
    pass
PY
}

# 9router 侧：以 slot-7 端口号动态拼正则，换端口不用改脚本。
_gate_router_log() {  # $1=行数  $2=1 跟随  $3=池闸端口
  local n=$1 follow=$2 port=$3 svc=${ASIP_ROUTER_SERVICE:-9router.service}
  local re="to=slot-7"
  [ -n "$port" ] && re="$re|:$port"
  if [ "$follow" = 1 ]; then
    echo "── 9router 侧第七闸口日志，之后 live（Ctrl-C 退出）──" >&2
    _jctl -u "$svc" -f --since '-5 min' --no-pager -q 2>/dev/null \
      | grep --line-buffered -E "$re"
  else
    _jctl -u "$svc" --since '-30 min' --no-pager -q 2>/dev/null \
      | grep -E "$re" | tail -n "$n"
  fi
}

_gate_status() {  # $1=/slots JSON  $2=asip 侧日志行数
  ASIP_LOG_PATH="$GATE_LOG" ASIP_MAIN_LOG="$LOG" \
  GATE_LOG_NAME="$(basename "$GATE_LOG")" \
  GATE_LINES="$2" GATE_HOST="${ASIP_SOCKS_HOST:-localhost}" \
  GATE_RE="$_RE_GATE" "$ASIP_PY" - "$1" "$ASIP_SERVICE" <<'PY'
import json, os, re, socket, sys, time
path, svc = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError) as e:
    raise SystemExit(f"✗ /slots 不是合法 JSON: {e}")
slots = d.get("slots") or []
gate = next((s for s in slots if s.get("kind") == "proxy"), None)
warps = [s for s in slots if s.get("kind") != "proxy"]
ok = [s for s in warps if s.get("status") == "ok"]
if gate is None:
    raise SystemExit("✗ /slots 里没有 kind=proxy 的槽位（第七闸口未启用？看 singbox.proxy_gate）")
name, port = gate.get("name", "?"), gate.get("port")
listen = "✗ 未监听"
try:
    with socket.create_connection((os.environ.get("GATE_HOST", "localhost"),
                                  int(port)), 0.4):
        listen = "✓ 监听中"
except (OSError, ValueError, TypeError):
    pass
print(f"第七闸口（代理池兜底）  {name}  port={port}  {listen}")
print(f"  健康:  {gate.get('status', '?')}  verdict={gate.get('verdict') or '-'}  "
      f"持续 {gate.get('status_age_s', '?')}s")
print(f"  当前池节点: {gate.get('tag') or '-'}")
print(f"  在役:  {'是' if gate.get('serving') else '否（待命）'}")
print(f"  常规闸口可选: {len(ok)}/{len(warps)}  → "
      + ("池闸应待命，严格兜底成立" if ok else "全部不可选，池闸是唯一兜底"))
print(f"  主出口: {d.get('active_tag') or '-'}")
print(f"  服务:  {os.environ.get('GATE_SVC_STATE', '?')}  {svc}")
# 近 1h 统计：独立账本 + 主日志的历史行。拆文件那一刻之前的池闸行还在
# daemon.log 里，只读 gate.log 会让重启后一小时内的数字凭空缩水。
# mirror 模式同一行会落两份：按整行去重，再按行首时间戳排序。
rx = re.compile(os.environ["GATE_RE"])   # 与 bash 侧 _RE_GATE 共用一份口径
logs = [os.environ.get("ASIP_LOG_PATH", ""), os.environ.get("ASIP_MAIN_LOG", "")]
n = int(os.environ.get("GATE_LINES", "12") or 12)
since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
cnt = {"rotate": 0, "fallback": 0, "redirect": 0}
seen, rows = set(), []
for path in logs:
    if not path:
        continue
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not rx.search(line):
                    continue
                row = line.rstrip()
                if row in seen:
                    continue
                seen.add(row)
                rows.append(row)
    except OSError:
        pass
rows.sort()
for line in rows:
    if line < "[" + since:      # 时间戳在行首，字典序即时间序
        continue
    if "池内轮换" in line:
        cnt["rotate"] += 1
    if "秒切兜底" in line:
        cnt["fallback"] += 1
    if "重试改走" in line and ("slot-7" in line or str(port) in line):
        cnt["redirect"] += 1
recent = rows[-n:] if n else []
print(f"  近1h: 池内轮换 {cnt['rotate']} 次 / 秒切兜底 {cnt['fallback']} 次 / "
      f"指向池闸 {cnt['redirect']} 次")
if n and recent:
    print(f"\n── 池闸日志最近 {len(recent)} 行（{os.environ.get('GATE_LOG_NAME', 'asip 侧')}"
          f"；gate log 跟随 / gate events 只看事件）──")
    for line in recent:
        print("  " + line)
PY
}

cmd_gate() {
  local sub=${1:-st} n=30 follow=0 tmp port rc
  shift || true
  case "$sub" in
    -h|--help|help) _gate_usage ;;
    log|follow|f)
      n=${1:-200}; _gate_num "$n" || return 1
      _follow "$_RE_GATE" "$n" "$GATE_LOG" ;;
    events|event|ev)
      n=${1:-30}; _gate_num "$n" || return 1
      grep -E "$_RE_GATE_EVENT" "$GATE_LOG" 2>/dev/null | tail -n "$n" ;;
    st|status|'')
      n=${1:-12}; _gate_num "$n" || return 1
      tmp=$(mktemp) || return 1
      if ! _gate_slots "$tmp"; then
        rm -f "$tmp"
        echo "✗ 读不到 /slots（daemon 未起 / rotate.sock 或 api-token 缺失）" >&2
        echo "  日志仍可查：asip gate log / asip gate 9r" >&2
        return 1
      fi
      port=$(_gate_port_from_file "$tmp")
      GATE_SVC_STATE="$(_sctl is-active "$ASIP_SERVICE" 2>/dev/null)" \
        _gate_status "$tmp" "$n"
      rc=$?
      rm -f "$tmp"
      [ $rc -eq 0 ] || return $rc
      echo
      echo "── 9router 侧最近 5 行（asip gate 9r 看全部）──"
      _gate_router_log 5 0 "$port" ;;
    9r|9router|peer)
      case "${1:-}" in
        -f|follow) follow=1 ;;
        '') ;;
        *) n=$1; _gate_num "$n" || return 1
           [ "${2:-}" = -f ] && follow=1 ;;
      esac
      tmp=$(mktemp) || return 1
      port=""
      if _gate_slots "$tmp"; then port=$(_gate_port_from_file "$tmp"); fi
      rm -f "$tmp"
      _gate_router_log "$n" "$follow" "$port" ;;
    *) echo "✗ 未知子命令: asip gate $sub" >&2; _gate_usage >&2; return 1 ;;
  esac
}

# ── inbox：读 Sister Bus 里各角色报给本项目的消息 ──
# 全量未读 4486 条（2026-10-21 实测），不滤的话几乎全是别的项目/工作流的派单。
#   asip inbox          本项目相关未读（关键词，实测留 177 条 = 4%）
#   asip inbox all      不过滤
#   asip inbox tail [N] 按时间倒序取最近 N 条（默认 20）
#   asip inbox watch    每 5s 拉一次，只打印新到的
#   asip inbox cats     各分类未读计数
#   asip inbox 427344   单条全文
#   asip inbox <正则>   自定义关键词
# 只读：从不调用 consume，因此看过不等于已消费（消费由角色自己决定）。
# watch 只关心「最近出现的」，500 条足够（新消息必在头部）；
# 仍受 ASIP_INBOX_LIMIT 约束，便于调试时缩小。
_WATCH_LIMIT=${ASIP_INBOX_LIMIT:-500}
_INBOX_RE='warp|opencode|9router|auto-switch-ip|asip|sing-?box|usque|轮换池|出口|代理池|隧道|换线'

# 60s 够全量 4486 条（实测 0.14s）；限 5000 是因为 bus 的 --limit 0 才是无限，
# 而无限会把 4 年前的归档也拉进来，与「未读」语义不符。
# 注意：bus_client 把「消息条数」当退出码（4486 条 → rc=134，256 取模截断），
# 所以 rc 非零不代表失败，只能看输出能否解析 —— 故 || true。
_inbox_json() {  # $1=limit，默认全量；watch 用 500 足够（新消息必在最近 500 条内）
  local lim=${1:-${ASIP_INBOX_LIMIT:-5000}}
  # 非数字会让 argparse 直接退出、输出为空，最终被报成「读取 Sister Bus 失败」——
  # 把配置错误说成连接故障，是最难查的一类误导。
  case "$lim" in
    ''|*[!0-9]*) echo "✗ ASIP_INBOX_LIMIT 需为正整数，当前 '$lim'" >&2; return 1 ;;
  esac
  timeout 60 "$ASIP_PY" "$ASIP_BUS_CLIENT" unread --limit "$lim" --json 2>/dev/null || true
}

# 一次 fetch 落临时文件再交给 python：各分支不必各查一次 DB，也避免把 json 的
# "id" 嵌进 sh 单引号后再也改不动。
_inbox_run() {  # $1=mode  $2=arg
  local tmp rc db=${HERMES_BLACKBOARD_DB:-$ASIP_BLACKBOARD_DB}
  # 必须在调用前查：Blackboard 以 rwc 打开，路径写错会静默新建一个空库并返回 0 条，
  # 之后无从分辨「今天没消息」和「根本没连上」。
  if [ ! -f "$db" ]; then
    echo "✗ Blackboard 库不存在: $db（bus_client 会自动建空库，故先拦下）" >&2
    return 1
  fi
  tmp=$(mktemp) || return 1
  # 先取返回值：_inbox_json 自己报错（如 limit 非法）时已经说明了原因，
  # 再补一句「读取失败」只会把配置错误伪装成连接故障。
  local jrc=0
  _inbox_json >"$tmp" || jrc=$?
  if [ $jrc -ne 0 ]; then
    rm -f "$tmp"; return $jrc
  fi
  # 空输出分两种：库不存在（路径错/未初始化）与库正常但确实 0 条未读。
  # 后者 bus_client 仍会输出 {"facts":[],"count":0}，所以「有输出但 0 条」是真的空库；
  # 「完全没输出」才是连不上。不区分的话两者都显示成「0 条」，路径错了也看不出来。
  if [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    echo "✗ 读取 Sister Bus 失败，无任何输出（检查 $db 与 bus_client.py）" >&2
    return 1
  fi
  INBOX_RE=$_INBOX_RE "$ASIP_PY" -I - "$1" "${2:-}" "$tmp" <<'PY'
import json, os, sys, time, re, collections

# `asip inbox all | head -3` 会让下游提前关闭管道，下一次 print 抛 BrokenPipeError
# 并把 traceback 甩到 stderr。SIGPIPE 恢复默认行为（Python 默认忽略它），
# 进程被信号终止即可，与 tail/grep 的行为一致。
try:
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (AttributeError, ValueError):
    pass

mode, arg, path = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    facts = json.load(open(path, encoding='utf-8'))['facts']
except Exception as e:
    sys.exit(f"✗ 解析未读 JSON 失败: {e}")

# 参数在 python 里校验：tail 的 N 走 python 切片，abc 会抛 ValueError，
# 负数切片（tail -1）会从末尾取 1 条，语义与「最近 1 条」相反。
if mode == 'tail':
    if not arg.isdigit():
        sys.exit(f"✗ tail 需要非负整数，收到 {arg!r}")
    arg = int(arg)

ts = lambda x: time.strftime('%m-%d %H:%M', time.localtime(x['timestamp']))
line = lambda x: f"[{x['id']}] {ts(x)} ({x['category']}) {x['src']} → {x['title'][:90]}"

if mode == 'cats':
    print(f"未读 {len(facts)} 条：")
    for k, v in collections.Counter(x['category'] for x in facts).most_common():
        print(f"  {v:5d}  {k}")
elif mode == 'tail':
    if arg > 0:
        for x in facts[:arg]:
            print(line(x))
elif mode == 'all':
    for x in facts:
        print(line(x))
elif mode == 'one':
    q = [x for x in facts if x['id'] == int(arg)]
    if not q:
        sys.exit(f"未读里没有 #{arg}（可能已被消费；asip inbox all 可核对）")
    x = q[0]
    print(f"#{x['id']}  [{x['category']}]  src={x['src']}  {ts(x)}")
    print(x['title'])
    print()
    print(x['evidence'])
else:                                    # list（默认）或自定义正则
    try:
        rx = re.compile(arg or os.environ.get('INBOX_RE', ''), re.I)
    except re.error as e:
        sys.exit(f"✗ 关键词不是合法正则: {e}")
    hit = [x for x in facts if rx.search(x['title'] + ' ' + x['evidence'])]
    if not facts:
        print(f"0 条未读（{path} 对应库存在但为空）")
    else:
        print(f"相关未读 {len(hit)}/{len(facts)} 条（asip inbox all 看全部，asip inbox <正则> 自定义）")
    for x in hit:
        print(line(x))
PY
  rc=$?
  rm -f "$tmp"
  return $rc
}

_inbox_watch() {
  local db=${HERMES_BLACKBOARD_DB:-$ASIP_BLACKBOARD_DB}
  if [ ! -f "$db" ]; then
    echo "✗ Blackboard 库不存在: $db（bus_client 会自动建空库，故先拦下）" >&2
    return 1
  fi
  local base; base=$(mktemp) || return 1
  local jrc=0
  _inbox_json "$_WATCH_LIMIT" >"$base.raw" || jrc=$?
  if [ $jrc -ne 0 ]; then
    rm -f "$base" "$base.raw"; return $jrc
  fi
  "$ASIP_PY" -I -c '
import json, signal, sys
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
print("\n".join(str(x["id"]) for x in json.load(sys.stdin)["facts"]))' <"$base.raw" >"$base" 2>/dev/null
  rm -f "$base.raw"
  # 空基线是合法的（今天一条未读也没有），不能当失败 —— 只有「库不存在」才是失败，
  # 那一项已在上面拦过。用 -e 而非 -s：文件存在就可能为空。
  if [ ! -e "$base" ]; then
    rm -f "$base"; echo "✗ 读取 Sister Bus 失败" >&2; return 1
  fi
  # 首轮只记基线不打印：否则一启动就把几千条积压刷满屏。
  echo "── 基线 $(wc -l <"$base") 条未读，之后每 5s 只报新到的（Ctrl-C 退出）──" >&2
  local tmp
  while sleep 5; do
    tmp=$(mktemp) || continue
    _inbox_json "$_WATCH_LIMIT" >"$tmp"
    if [ ! -s "$tmp" ]; then rm -f "$tmp"; continue; fi
    "$ASIP_PY" -I - "$base" "$tmp" <<'PY'
import json, signal, sys, time
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
known = set(open(sys.argv[1], encoding='utf-8').read().split())
try:
    facts = json.load(open(sys.argv[2], encoding='utf-8'))['facts']
except Exception:
    raise SystemExit(0)
for x in facts:
    if str(x['id']) in known:
        continue
    print(f"[{time.strftime('%H:%M:%S', time.localtime(x['timestamp']))}] "
          f"#{x['id']} ({x['category']}) {x['src']} → {x['title'][:100]}")
# 基线在同一次 fetch 里更新，期间到达的消息不会漏。
open(sys.argv[1], 'w', encoding='utf-8').write("\n".join(str(x['id']) for x in facts))
PY
    rm -f "$tmp"
  done
}

cmd_inbox() {
  case "${1:-list}" in
    watch|follow) _inbox_watch ;;
    all)          _inbox_run all ;;
    cats|cat)     _inbox_run cats ;;
    tail|new)     _inbox_run tail "${2:-20}" ;;
    ''|list)      _inbox_run list ;;
    -*[0-9]*)     echo "✗ 参数不能以 - 开头（tail 取最近 N 条，N 必须非负）" >&2; exit 1 ;;
    [0-9]*)       _inbox_run one "$1" ;;
    -h|--help)    _usage ;;
    *)            _inbox_run list "$1" ;;
  esac
}

cmd_warp() {
  case "${1:-status}" in
    stop)
      echo "→ 切 opencode 出口到轮换池 proxy"
      _singbox PUT /proxies/$ASIP_WARP_SELECTOR '{"name":"proxy"}' >/dev/null
      # Restart=always 会让 stop 后 5s 重生；systemctl mask 会因单元文件已存在而失败，
      # 直接把单元文件覆盖成 /dev/null 软链才是有效屏蔽（原文件内容见下方 start 的还原）。
      echo "→ 停止并屏蔽 WARP 隧道"
      _sctl stop $UNITS 2>/dev/null
      for u in $UNITS; do
        f="$ASIP_UNIT_DIR/$u"
        # 备份失败必须中止：原本行之后无条件 ln -sf /dev/null，cp 失败时
        # 单元内容被覆盖且没有备份 —— 20 条隧道再也起不来，start 也还原不了
        # （它靠 .asip-bak 判断），而且全程报成功。
        if [ -e "$f" ] && [ ! -L "$f" ]; then
          _as_root cp "$f" "$f.asip-bak" || { echo "✗ 备份 $u 失败，拒绝屏蔽" >&2; return 1; }
        fi
        _as_root ln -sf /dev/null "$f" || return 1
      done
      _sctl daemon-reload
      sleep 1
      echo "WARP:   $(_sctl is-active $UNITS 2>/dev/null | tr '\n' ' ')"
      echo "出口:   $(_exit_now)"
      ;;
    start)
      echo "→ 解除屏蔽并启动 WARP 隧道"
      for u in $UNITS; do
        f="$ASIP_UNIT_DIR/$u"
        if [ -L "$f" ] && [ -f "$f.asip-bak" ]; then
          _as_root rm -f "$f" || return 1
          _as_root mv "$f.asip-bak" "$f" || return 1
        fi
      done
      _sctl daemon-reload
      _sctl start $UNITS
      sleep 3
      for i in $(seq 1 10); do
        up=$(printf '%s\n' $PORTS | xargs -P10 -I{} sh -c \
          'curl -s --max-time 2 --socks5-hostname $ASIP_SOCKS_HOST:{} \
             https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -q "warp=on" && echo 1' \
          | wc -l)
        [ "$up" -gt 0 ] && break
        sleep 1
      done
      echo "WARP:   $(_sctl is-active $UNITS 2>/dev/null | tr '\n' ' ')"
      echo "可达:   ${up:-0}/$(echo $PORTS | wc -w) 条"
      # 必须自己切 selector：WARP 与池互不自动切换（daemon 已无回切逻辑），
      # stop 把出口切到 proxy 后没人切回来，只 start 隧道等于流量仍走池。
      # 挑第一条真正探得通 warp=on 的线，探不通就不切（切到死线比留在池上更糟）。
      if [ "${up:-0}" -gt 0 ]; then
        for p in $PORTS; do
          if curl -s --max-time 3 --socks5-hostname "$ASIP_SOCKS_HOST:$p" \
               https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -q "warp=on"; then
            tag=$(awk -v p="$p" '$2==p {print $1}' <<<"$ASIP_WARP_MEMBERS")
            _singbox PUT /proxies/$ASIP_WARP_SELECTOR "{\"name\":\"$tag\"}" >/dev/null
            echo "→ 出口已切到 $tag"
            break
          fi
        done
      else
        echo "⚠ 无一条隧道可达，出口保持在 $(_exit_now)"
      fi
      echo "出口:   $(_exit_now)"
      ;;
    restart)
      cmd_warp stop
      cmd_warp start
      ;;
    status|'')
      # 逐条带单元名：`is-active` 一次收 44 个参数只回一排字母，配不上号。
      # 非 active 的单独列出来并区分「未部署」（disabled，缩容遗留）与
      # 「该跑没跑」（enabled 却没起来）—— 后者才是故障。
      _down=""
      while read -r _t _p _u; do
        [ -n "$_u" ] || continue
        [ "$(_sctl is-active "$_u" 2>/dev/null)" = "active" ] && continue
        if [ "$(_sctl is-enabled "$_u" 2>/dev/null)" = "disabled" ]; then
          _down="$_down
  · $_t ($_u) 未部署（disabled）"
        else
          _down="$_down
  ✗ $_t ($_u) 该跑没跑"
        fi
      done <<<"$ASIP_WARP_MEMBERS"
      if [ -n "$_down" ]; then
        echo "WARP:   非 active 单元 —$_down"
      else
        echo "WARP:   全部 active"
      fi
      # 串行探测 10 条要 7s+，并行后受最慢那条决定：每次换线都会重启某条隧道，
      # 总有一条正好落在重连窗口。2s 下界太紧 —— 实测 WARP-CF-E 是**当前出口**
      # （daemon 报 singbox=🟢），2s 下连测两次都判「不可达」，第三次 6s 才通。
      # 6s 覆盖重连窗口又不至于让整轮等太久（-P10 与条数解耦）。
      # 打印 `tag port ip` 三元组，由 awk 并回一行：并行探测只回 ip 的话，
      # 44 个端口里哪条不通就得靠人对着端口表反查。
      while read -r _t _p _u; do
        [ -n "$_p" ] && printf '%s %s\n' "$_t" "$_p"
      done <<<"$ASIP_WARP_MEMBERS" | xargs -P10 -n2 sh -c \
        'ip=$(curl -s --max-time 6 --socks5-hostname $ASIP_SOCKS_HOST:$2 \
           https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -oP "^ip=\K.*")
         printf "%s %s %s\n" "$1" "$2" "${ip:-不可达}"' _ \
        | sort -k2 -n | "$ASIP_PY" -c '
import json, os, sys
# 端口探不通时补一句**原因**。探测本身（6s 超时）只能说明「没拿到地址」，
# 而 daemon 那边的 egress_error 分得更细：端口没监听 / DNS 失败 /
# 隧道被打断 / 到点无响应 —— 处置动作完全不同。
try:
    st = json.load(open(os.environ["ASIP_LINE_STATE_FILE"]))
except (OSError, ValueError, KeyError):
    st = {}
lines = (st.get("lines") if isinstance(st, dict) else None) or {}
for row in sys.stdin:
    parts = row.split()
    if len(parts) < 3:
        continue
    tag, port, ip = parts[0], parts[1], parts[2]
    if ip == "不可达":
        why = (lines.get(tag) or {}).get("egress_error") or ""
        if why:
            ip = f"不可达（{why}）"
    print(f"  {tag:<16} :{port:<5} {ip}")'
      echo "出口:   $(_exit_now)"
      ;;
    cycles|cycle)
      # 24h 内每个出口 IP 的「换线生效 -> 首次 403/429」样本，以及据此算出的最小周期。
      "$ASIP_PY" -I - <<'PY'
import json, os, time
p = os.environ["ASIP_CYCLES_FILE"]
try:
    d = json.load(open(p))
except FileNotFoundError:
    print("尚无周期样本（.warp_ip_cycles.json 未生成）"); raise SystemExit
except (OSError, ValueError) as e:
    raise SystemExit(f"✗ 台账格式不认识: {p}: {e}")
cyc = d.get("cycles", d) if isinstance(d, dict) else d
if not isinstance(cyc, list) or not all(isinstance(c, dict) for c in cyc):
    # 旧版本/别处带过来的台账可能是 list[number] 或 {"used": {...}} 形态。
    # 不拦就是一段 AttributeError traceback，看起来像脚本坏了。
    raise SystemExit(f"✗ 台账格式不认识: {p}（期望 list[dict]，实际 {type(cyc).__name__}）")
now = time.time()
recent = [c for c in cyc if now - float(c.get("ended_at", 0)) < 86400]
usable = [c for c in recent if float(c.get("cycle", 0)) > 0 and int(c.get("ok", 0) or 0) > 0]
drop = len(recent) - len(usable)
print(f"24h 样本: {len(recent)}  可用: {len(usable)}  首错即废(排除): {drop}")
if usable:
    vals = sorted(float(c["cycle"]) for c in usable)
    n = len(vals)
    raw = vals[0]; eff = max(raw, 5.0)
    print(f"周期: min={raw:.0f}s p25={vals[n//4]:.0f}s p50={vals[n//2]:.0f}s "
          f"p75={vals[3*n//4]:.0f}s max={vals[-1]:.0f}s")
    print(f"生效最小周期: {eff:.0f}s（下限 5s；切换本身 p50≈4s）")
    low = min(usable, key=lambda c: float(c["cycle"]))
    print(f"最短样本: {low['ip']}  {float(low['cycle']):.0f}s  ok={low.get('ok')}  "
          f"{(now-float(low['ended_at']))/60:.0f} 分钟前")
else:
    print("无可用样本 -> 主动轮换回退为按请求数（20 条）")
PY
      ;;
    log)
      _follow "$_RE_SW" 2000
      ;;
    jlog)
      # 单元名是 journalctl 的 -u 参数，不能用位置参数（会 "Failed to add match"）。
      # 不要加 -u init.scope：那等于拉全部单元（cron-worker、watchdog 全进来）。
      local args=()
      for u in $UNITS; do args+=(-u "$u"); done
      echo "── 隧道 journal：最近 5 分钟（${2:-已滤噪}），之后 live ──" >&2
      # 前缀随作用域而变，两套都要剥：
      #   系统级  Tue 2026-10-27 13:45:27 CST 主机名 init.scope[1]: 消息
      #   用户级  user@1000.service/init.scope: 消息
      # 只认用户级那套，系统级日志每行就顶着时间戳+pid，滤噪也匹配不上。
      # 不用 `.*?`：POSIX ERE 没有惰性量词，GNU sed 会把它当字面量。
      # 压缩规则必须放最后：sed 对每行按规则顺序单遍执行，先压缩只会匹配到带前缀的原文。
      _jctl -f --since '-5 min' --no-pager -q -o with-unit "${args[@]}" 2>/dev/null \
        | sed -u -E 's#^[A-Z][a-z]{2} [0-9-]+ [0-9:]+ [A-Z]+ [^ ]+ (usque-warp[-.a-z0-9]*)(\[[0-9]+\])?: ([0-9]{4}/[0-9]{2}/[0-9]{2} [0-9:]+ CST )?#[\1] #;
                     s#^[A-Z][a-z]{2} [0-9-]+ [0-9:]+ [A-Z]+ [^ ]+ [^ :]+(\[[0-9]+\])?: ##;
                     s#^.*user@[0-9]+\.service/(usque-warp[^:]*): ([0-9]{4}/[0-9]{2}/[0-9]{2} [0-9:]+ CST )?#[\1] #;
                     s#^.*user@[0-9]+\.service/[^:]*: ##;
                     s#^(Starting|Started|Stopping|Stopped|Deactivated) (usque-warp[-.a-z0-9]*)\.service.*#[\2] \1#' \
        | { [ "${2:-}" = all ] && cat || grep --line-buffered -vE "$_RE_NOISE"; }
      ;;
    ip|ips)
      "$ASIP_PY" -I - <<'PY'
import json, os, time
p = os.environ["ASIP_IP_USED_FILE"]
try:
    d = json.load(open(p))
except FileNotFoundError:
    print("尚无记录"); raise SystemExit
except (OSError, ValueError) as e:
    raise SystemExit(f"✗ 台账格式不认识: {p}: {e}")
used = d.get("used", d)
# 旧版台账把值写成裸时间戳 {ip: ts}，新版是 {ip: {ts, blocked}}。
# 混着重放会 AttributeError，统一收窄成 dict 形态，裸值当「只在此时刻用过」。
if not isinstance(used, dict):
    raise SystemExit(f"✗ 台账格式不认识: {p}（期望 dict，实际 {type(used).__name__}）")
used = {ip: (v if isinstance(v, dict) else {"ts": v}) for ip, v in used.items()}
seen = d.get("seen") or list(used)
now = time.time()
blocked = [ip for ip, v in used.items() if isinstance(v, dict) and v.get("blocked")]
clean = [ip for ip in used if ip not in blocked]
print(f"累计见过: {len(seen)} 个 IP")
print(f"  已标记: {len(blocked)}")
print(f"  未标记: {len(clean)}")
for ip, v in sorted(used.items(), key=lambda x: -x[1].get("ts", 0))[:10]:
    age = (now - v.get("ts", 0)) / 60
    mark = "blocked" if v.get("blocked") else "clean  "
    print(f"  [{mark}] {ip}  {age:.0f} 分钟前")
PY
      ;;
    -h|--help|help) _usage ;;
    *) echo "用法: asip warp [stop|start|restart|status|ip|cycles|log|jlog]  （asip -h 看全部）"; exit 1 ;;
  esac
}

cmd_sub() {
  # 订阅源清单不在这里解析：config.py 是唯一事实源（固定源 + 环境变量 +
  # 台账文件），CLI 只负责写台账、打一行、叫 daemon 刷新。
  local sub=${1:-ls}; shift || true
  local out
  out=$("$ASIP_PY" - "$ASIP_DIR" "$sub" "$@" <<'PY'
import os, sys
repo, cmd, *rest = sys.argv[1:]
sys.path.insert(0, repo)
# daemon 靠 systemd 的 EnvironmentFile 拿到 .env，CLI 子进程没有 —— 不先补上，
# 固定源会显示成 0 个（订阅其实配着，只是这个进程看不见）。
try:
    with open(os.path.join(repo, ".env"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
except OSError:
    pass
import config

urls = list(config.subscription_urls())
extra = list(config.extra_subscription_urls())

def show():
    print(f"固定源 {len(urls)} 个（config.py / .env，改后需重启 daemon）")
    for i, u in enumerate(urls, 1):
        print(f"  {i:2d}. {u.split('?')[0].rsplit('/', 1)[0]}/…")
    print(f"追加源 {len(extra)} 个（{config.EXTRA_SUBS_FILE}，改后无需重启）")
    for i, u in enumerate(extra, 1):
        print(f"  {i:2d}. {u.split('?')[0].rsplit('/', 1)[0]}/…")

if cmd in ("", "ls", "list"):
    show()
elif cmd == "add":
    if not rest:
        sys.exit("用法: asip sub add <url>")
    url = rest[0]
    if "://" not in url:
        sys.exit(f"✗ 不是合法 URL: {url[:40]}")
    if url in urls or url in extra:
        sys.exit("已存在，不重复添加")
    with open(config.EXTRA_SUBS_FILE, "a", encoding="utf-8") as f:
        f.write(url + "\n")
    import os
    os.chmod(config.EXTRA_SUBS_FILE, 0o600)
    print("✓ 已加入追加源")
elif cmd == "rm":
    if not rest:
        sys.exit("用法: asip sub rm <url|序号>")
    key = rest[0]
    target = None
    if key.isdigit():
        k = int(key) - 1
        target = extra[k] if 0 <= k < len(extra) else None
        if target is None:
            sys.exit(f"✗ 序号超出追加源范围（1~{len(extra)}）")
    else:
        target = key if key in extra else None
        if target is None:
            sys.exit("✗ 只删追加源；固定源请改 .env 后重启 daemon")
    keep = [u for u in extra if u != target]
    from asip.atomic_io import write_text
    write_text(config.EXTRA_SUBS_FILE,
               "".join(u + "\n" for u in keep), mode=0o600)
    print("✓ 已移除")
elif cmd != "refresh":
    sys.exit(f"✗ 未知子命令: {cmd}")
PY
)
  local rc=$?
  printf '%s\n' "$out"
  # add/rm 才需要刷新；ls 只读。刷新失败不该吞掉原来的成功状态码 ——
  # 台账已经写进去了，那是用户要的结果。
  case "$sub" in
    add|rm|refresh)
      [ $rc -eq 0 ] || return $rc
      local tok req
      tok=$(cat "$ASIP_DIR/run/api-token" 2>/dev/null) || {
        echo "⚠ 读不到 $ASIP_DIR/run/api-token，未触发刷新（daemon 未启用 rotate 接口？）" >&2
        return 0; }
      req=$("$ASIP_PY" - "$ASIP_DIR" "$tok" <<'PY'
import http.client, json, socket, sys
repo, tok = sys.argv[1], sys.argv[2]
class C(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=10)
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(f"{repo}/run/rotate.sock")
c = C()
body = b"{}"
c.request("POST", "/refresh-sub", body,
          {"X-Asip-Token": tok, "Content-Length": str(len(body))})
r = c.getresponse()
print(r.status, r.read().decode()[:120])
PY
)
      echo "→ $req"
      echo "  （刷新在后台跑：asip s 看换线与池子成员变化，约 10~30s）"
      if [ "$sub" = "add" ]; then
        # 入块是异步的、且要独占一个空闲 WARP 设备：放在前台会让 add 卡几分钟。
        # 但必须说清楚 —— 不测 colo，新节点只是一个普通代理，永远分不到 WARP
        # 渠道（块表里没有 colo 的节点 channels() 不会选它）。
        echo "  下一步（让新节点进 WARP 渠道，两条承载路径分开测）："
        echo "    # UDP / WireGuard："
        echo "    $ASIP_PY $ASIP_DIR/warp_diag.py --from-config --new-only \\"
        echo "        --out /tmp/asip-newdiag.jsonl --rounds 2"
        echo "    $ASIP_PY $ASIP_DIR/warp_blocks.py merge-diag /tmp/asip-newdiag.jsonl"
        echo "    # TCP / MASQUE(H2)：用独立探测设备，不碰生产线"
        echo "    $ASIP_PY $ASIP_DIR/tools/probe_h2_nodes.py --new-only \\"
        echo "        --out /tmp/asip-h2scan.json"
        echo "    $ASIP_PY -c 'import json,sys; r=[x for x in json.load(open(\"/tmp/asip-h2scan.json\")) if x[\"ok\"]]; json.dump(r, open(\"/tmp/asip-h2ok.json\",\"w\"))'"
        echo "    $ASIP_PY $ASIP_DIR/tools/merge_h2_blockmap.py /tmp/asip-h2ok.json"
      fi
      ;;
  esac
}

case "${1:-}" in
  -h|--help|help) _usage ;;
  warp) shift; cmd_warp "$@" ;;
  gate|slot7|g7) shift; cmd_gate "$@" ;;
  sub) shift; cmd_sub "$@" ;;
  inbox|inb) shift; cmd_inbox "$@" ;;
  # Vercel 中继：子命令全在 python 里（argparse 管，别在 bash 里再抄一份参数表）。
  # 用 exec 而不是调用后返回：退出码要原样透出去（2 = 有 relay 不可用）。
  vercel) shift; exec "$ASIP_PY" "$ASIP_DIR/vercel_relay_cli.py" "$@" ;;
  # 渠道健康报表：参数全在 python 里（argparse 管，别在 bash 里再抄一份）。
  chan|channel) shift; exec "$ASIP_PY" "$ASIP_DIR/tools/channel_report.py" "$@" ;;
  # 块视图：块内节点清单 / 延迟 / 当前绑定，参数全在 python 里。
  blk|block) shift; exec "$ASIP_PY" "$ASIP_DIR/tools/block_view.py" "$@" ;;
  st|status)
    out=$(_singbox GET /proxies/$ASIP_WARP_SELECTOR | grep -oP '"now"\s*:\s*"\K[^"]+')
    # 「当前」必须跟着模式走：WARP 模式下 opencode 走 opencode-out 指向的隧道，
    # 池 selector 的选点是后台噪音。恒读 proxy 会让人以为流量落在池节点上。
    if [ "$out" = "$ASIP_POOL_SELECTOR" ]; then
      now=$(_singbox GET "/proxies/$ASIP_POOL_SELECTOR" | grep -oP '"now"\s*:\s*"\K[^"]+')
    else
      now=$out
    fi
    since=$(date -d '-1 hour' '+%Y-%m-%d %H:%M:%S')
    echo "服务:  $(_sctl is-active "$ASIP_SERVICE")"
    # 出口指向 WARP-CF* 即 WARP 模式，指向池 selector 即轮换池模式。
    case "$out" in
      WARP-CF*) echo "模式:  WARP ($out)" ;;
      "$ASIP_POOL_SELECTOR") echo "模式:  PROXY 轮换池" ;;
      *)        echo "模式:  ${out:-查询失败}" ;;
    esac
    echo "当前:  ${now:-查询失败}"
    echo "近1h:  切换 $(awk -v c="$since" '$0 >= "["c && /切代理/ {n++} END{print n+0}' "$LOG") 次"
    tail -n 3 "$LOG"
    ;;
  s|switch) _follow "$_RE_SW" 2000 ;;
  e|err)    _follow "$_RE_ERR" 2000 ;;
  '')       _follow ;;
  # 裸参数只能是行数；给个别的词就掉进 tail 报 "invalid number of lines"，不如直接给清单。
  *[!0-9]*) _usage >&2; exit 1 ;;
  *)        tail -n "$1" "$LOG" ;;
esac
