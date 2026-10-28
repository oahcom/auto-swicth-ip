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
export ASIP_IP_USED_FILE ASIP_CYCLES_FILE ASIP_LOG ASIP_CLASH_API ASIP_PY
# 隧道探测走 xargs 起的子 shell，主机名必须导出才能在单引号脚本里取到。
export ASIP_SOCKS_HOST

LOG=$ASIP_LOG
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
# asip-unit，走 sudo -n 会直接报"需要交互认证"，把状态打成空白。
_sctl() {
  case "${1:-}" in
    is-active|is-enabled|status|show) systemctl "$@" ;;
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

诊断
  asip st              状态快照：出口模式 / 当前节点 / 近 1h 切换次数
  asip warp status     各隧道状态 + 各自出口 IP + 当前出口
  asip warp ip         出口 IP 使用记录（是否被标记、多久以前）
  asip warp cycles     24h IP 可用周期样本与生效的最小周期

WARP 控制
  asip warp start      解除屏蔽并启动全部隧道
  asip warp stop       切轮换池 + 停止并屏蔽隧道
  asip warp restart    stop 后 start

隧道日志
  asip warp log        跟随 daemon 日志中与 WARP 换线相关的行
  asip warp jlog       跟随 usque 隧道 journal（默认滤噪）
  asip warp jlog all   同上，不过滤

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
  local re=$1 scan=${2:-50}
  echo "── 回看最近 $scan 行，之后 live（Ctrl-C 退出）──" >&2
  if [ -n "$re" ]; then
    # 匹配率低（近 300 行里换线行 128、错误行个位数），至少回看 2000 行才不显空。
    tail -F -n $scan "$LOG" 2>/dev/null | grep --line-buffered -E "$re"
  else
    tail -F -n $scan "$LOG" 2>/dev/null
  fi
}

# 换线全过程：换线触发/成功/失败、选线、缓存命中、脏 IP、会话刷新、补刷、轮换池兜底。
_RE_SW='切代理|切到|❄|WARP|轮换池|选线|会话已刷新|脏 ?IP|补刷|兜底|出口模式'
# 不能用裸「错误」「失败」「冷却」：健康行每 10s 一条，含「错误=N/10」「冷却至=」
# 与「探测失败」样板，三个词都会把过滤器放大成全量。只认行首错误标记与异常。
_RE_ERR='^\[[^]]*\] +(✗|⚠|⇄)|Traceback|ERROR|不可达|异常:'
# usque 每 20s 一条的固有噪声：空闲/重连心跳、TCP-only 提示、探测连接主动断开。
_RE_NOISE='Tunnel idle|Detected outbound activity|l4-socks|SOCKS client .* closed during negotiation'

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
      echo "WARP:   $(_sctl is-active $UNITS 2>/dev/null | tr '\n' ' ')"
      # 串行探测 10 条要 7s+，并行后受最慢那条决定：--max-time 3 实测 3.0s，
      # 因为每次换线都会重启某条隧道，总有一条正好落在重连窗口。2s 下界足够
      # （正常隧道 RTT < 300ms，2s 还不通就是真不通），-P10 与条数解耦。
      printf '%s\n' $PORTS | xargs -P10 -I{} sh -c \
        'ip=$(curl -s --max-time 2 --socks5-hostname $ASIP_SOCKS_HOST:{} \
           https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -oP "^ip=\K.*")
         case "$ip" in
           "") printf "  :%s  %s\n" {} "不可达（也可能在重连窗口，等下再测）" ;;
           *)  printf "  :%s  %s\n" {} "$ip" ;;
         esac' | sort -t: -k2 -n
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

case "${1:-}" in
  -h|--help|help) _usage ;;
  warp) shift; cmd_warp "$@" ;;
  inbox|inb) shift; cmd_inbox "$@" ;;
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
