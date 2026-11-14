#!/usr/bin/env python3
"""
Shared configuration for auto-switch-ip.
daemon.py imports from this module.
"""

import os
import pwd
import sys
import time

# ============= 路径解析 =============
# 项目内文件一律相对本文件定位（clone 到任何目录都能跑）；项目外的（9router、
# secret）以家目录为基准 + 环境变量覆盖。不要写死 /home/<user>：`sudo -u` 启动时
# HOME 可能仍是 /root，所以用 pwd 数据库里本用户的家目录而不是 ~。
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = pwd.getpwuid(os.getuid()).pw_dir


def _env_path(name: str, default: str) -> str:
    # 必须展开：`.env` 由 start 脚本 source（shell 展开）与 systemd 单元
    # EnvironmentFile 读取（**不展开**，实测 `$HOME/x` 原样传入）。不在这里
    # 统一展开，同一份 .env 在两条启动路径下会得到不同结果。
    return os.path.expanduser(os.path.expandvars(os.environ.get(name) or default))


def _env_bool(name: str, default: bool) -> bool:
    """环境变量布尔覆盖。未设/空串 = 用默认值；1/true/yes/on 为真，其余为假。"""
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


SECRET_FILE = _env_path("SINGBOX_SECRET_FILE_PATH", os.path.join(HOME_DIR, ".singbox_secret"))
# 局域网代理认证，内容一行 `username:password`（mode 600，gitignored）。
# 文件不存在 = 不生成 LAN 入站：无认证的 mixed 入站等于开放中继。
LAN_AUTH_FILE = _env_path("SINGBOX_LAN_AUTH_FILE_PATH", os.path.join(HOME_DIR, ".singbox_lan_auth"))

# Module-level cache for secret + mtime
_SINGBOX_SECRET: str = ""
_SINGBOX_SECRET_MTIME: float = 0


def get_lan_auth() -> tuple[str, str] | None:
    """局域网入站的 (username, password)；不可用时返回 None。

    刻意不做 mtime 缓存：调用方只有订阅刷新（每小时一次），缓存省不下什么，
    却会让改密码后要等一轮刷新才生效。缺失或格式非法一律 None —— 让调用方
    跳过整个入站，而不是退化成无认证监听。
    """
    try:
        st = os.stat(LAN_AUTH_FILE)
        if st.st_mode & 0o077:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚠ {LAN_AUTH_FILE} 权限过宽 "
                  f"({oct(st.st_mode & 0o777)})，同网段任何用户可读取", file=sys.stderr)
        with open(LAN_AUTH_FILE) as f:
            username, _, password = f.read().strip().partition(":")
        if not username or not password:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚠ {LAN_AUTH_FILE} 格式应为 "
                  f"username:password，当前内容无法解析", file=sys.stderr)
            return None
        return username, password
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✗ 读取局域网认证失败: {e}", file=sys.stderr)
        return None

def get_singbox_secret() -> str:
    """Get sing-box secret with mtime-based auto-refresh."""
    global _SINGBOX_SECRET, _SINGBOX_SECRET_MTIME
    try:
        st = os.stat(SECRET_FILE)
        mtime = st.st_mtime
        if _SINGBOX_SECRET and mtime == _SINGBOX_SECRET_MTIME:
            return _SINGBOX_SECRET
        if st.st_mode & 0o077:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚠ Secret file loose perms ({oct(st.st_mode & 0o777)})",
                  file=sys.stderr)
        with open(SECRET_FILE) as f:
            _SINGBOX_SECRET = f.read().strip()
        _SINGBOX_SECRET_MTIME = mtime
        return _SINGBOX_SECRET
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✗ 读取密钥文件失败: {e}", file=sys.stderr)
        return ""

# 启动时校验密钥 (活值) — 不冻结常量, 避免 secret 轮换后失效
if not get_singbox_secret():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] WARNING: sing-box secret 未设置 — 在 {SECRET_FILE} (chmod 600) 中写入 base64 secret", file=sys.stderr)

# WARP-over-proxy 线路拓扑。0 条 = 不启用（默认）。
#
#   WARP_WG_FAMILIES=v4,v6,v6,v6   每个落地节点上的渠道模式（长度 = 每节点渠道数）
#   WARP_WG_NODES=4                落地节点数 → 总线路数 = 4 × 4 = 16
#
# 地址族决定消耗哪个配额池，不是装饰：v6 每次重连换一个 /64（近乎无限），
# v4 由 WARP 落地 colo 决定，同 colo 必拿到同一地址（实测 aws49 与 unamecf
# 均为 104.28.243.105）。所以 v4 渠道数不应超过可用 colo 数。
# 旧变量 WARP_WG_COUNT=N 仍可用，等价于 N 条全 v6、单落地节点。
def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _wg_families() -> list[str]:
    raw = (os.environ.get("WARP_WG_FAMILIES") or "").strip()
    if raw:
        fams = [x.strip().lower() for x in raw.split(",") if x.strip()]
        return [f for f in fams if f in ("v4", "v6")]
    return ["v6"] * _env_int("WARP_WG_COUNT", 0)


_WG_FAMILIES: list[str] = _wg_families()
_WG_HOME_NODES = max(1, _env_int("WARP_WG_NODES", 1)) if _WG_FAMILIES else 0
_wg_count = len(_WG_FAMILIES) * _WG_HOME_NODES

CFG = {
    "9router": {
        "db_path": _env_path("NINE_ROUTER_DB_PATH", os.path.join(HOME_DIR, ".9router/db/data.sqlite")),
        # 监听口随部署而异（本机 20127、云端 20128）。写死会让健康检查在换部署
        # 时永远失败，而症状是「9router 不可达」而不是「端口配错」。
        "base": _env_path("NINE_ROUTER_BASE", "http://127.0.0.1:20127"),
    },
    # 9router 流量观测开关（重启 daemon 生效）。false = blind 模式：
    #   换线只按时间判据 —— WARP 出口按 proactive_fixed_sec 定时轮换，
    #   落在代理池上按 proactive_rotate_sec 定时换节点；不读 requestDetails/
    #   usageHistory，错误触发换线、429 率策略自适应、落池后拦截回切、
    #   requests 配额判据（同样要查 9router 库）自动停用。
    #   适合 9router 停用或流量不经过它的场景。
    # true = 默认行为，9router 错误信号重新驱动换线。
    # 可用环境变量 ASIP_OBSERVE_9ROUTER 覆盖（true/false），改 .env 重启即可
    # 切换，不必动这份文件。
    "traffic": {
        "observe_9router": _env_bool("ASIP_OBSERVE_9ROUTER", True),
        # 按时间换线（blind 模式）的唯一反馈是时钟，没有错误信号帮它验 IP，
        # 所以候选节点必须先过脏 IP 过滤：出口 IP 只要在这个窗口内被**任何**
        # 节点用过就跳过，避免把刚烧过的地址再切回来。默认 6h（ip-api 的
        # 21600s）；0 = 不过滤。WARP 隧道不受本项控制 —— 它的复用窗口是
        # warp.ip_reuse_guard_sec（24h，更强）。
        "ip_reuse_window_sec": 21600,
    },
    "singbox": {
        "clash_api": "http://127.0.0.1:9090",
        # 刻意不在此放 secret：import 时冻结的快照在轮换后不会更新，
        # 调用方必须走 get_singbox_secret()（见文件末尾注释）。
        "selector": "proxy",
        "binary": _env_path("SINGBOX_BINARY", os.path.join(PROJECT_DIR, "sing-box")),
        "config_file": _env_path("SINGBOX_CONFIG", os.path.join(PROJECT_DIR, "singbox-config.json")),
        "startup_grace_sec": 30,  # 30s：6 台 sing-box 依次拉起，给慢启动留足余量
        "max_restart_attempts": 3,
        "mixed_port": 7890,
        # 并行消费者槽位：每个槽位一个本机端口 + 一条独立 selector，与主出口
        # 完全同一套逻辑（从同一个 WARP 池里挑 OK 的线、各自独立轮换）。
        # 相当于把「一个消费者」变成 N 个，9router 在端口之间轮询。
        # 端口从 7892 起，避开 mixed-in(7890) 与 LAN 口(7891)。
        "consumer_slots": 6,
        # 第七闸口：代理池兜底闸（slot-7）。它不是第 7 条 WARP 线，而是把
        # 池 selector 当自己的「线」—— opencode 流量落到它上面 = 走轮换池。
        # 严格兜底的两条硬保证：
        #   1. asip 侧：_pick_healthy_slot 只有在六个常规闸口全都选不出来
        #      时才会把它当重试目标（常规候选阶段它根本不进场）；
        #   2. 9router 侧：常规轮询端口 ASIP_LOCAL_PROXY_PORT 默认
        #      7890,7892-7896，不含它的端口 —— 流量只能由 /rotate 回包的
        #      switch_to_url 引到它上面。
        # 9router 侧无需任何改动；asip 关掉它也只要设 False。
        "proxy_gate": True,
        # 配置漂移自愈：热重载（PUT /configs?force=true）不会重建 selector
        # 成员，磁盘配置更新后必须整进程重启才能加载新线。默认开启：
        # 订阅刷新后校验成员（方一）+ 补闸连续被挡时 keeper 自愈坏槽（方二）。
        # 出问题时设 False 即回到「只告警」的现状行为。
        "slot_drift_heal": True,
        # 局域网入站：0 = 不生成（默认关闭）。开启后另起一个 mixed 入站监听
        # 0.0.0.0，与环回口互不影响 —— 环回口给 9router/daemon，局域网口给
        # 手机/其他机器，前者永远不需要认证，后者必须有。
        "lan_port": 7891,
        # 允许来源网段。sing-box 自身按来源 IP 拒掉网段外连接（route 层 reject），
        # 免得这个入站被当成可从任何地方拨入的开放代理。
        "lan_cidrs": ["192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"],
    },
    # 订阅地址含账户 token，**不在源码里保留默认值**：token 会被 git 历史、
    # 日志和 argv 三处带走，轮换一次就要翻整棵树。必须由 .env / EnvironmentFile
    # 注入（见 .env.example）；缺失时订阅刷新会明确报错，而不是静默用别人的 token。
    "okz": {
        "proxy_url": "http://127.0.0.1:6696",
        "sub_url": _env_path("OKZ_SUB_URL", ""),
    },
    "hd23n": {
        "sub_url": _env_path("HD23N_SUB_URL", ""),
    },
    "dasho": {
        "sub_url": _env_path("DASHO_SUB_URL", ""),
    },
    "dasho_alt": {
        "sub_url": _env_path("DASHO_ALT_SUB_URL", ""),
    },
    "intervals": {
        # 主循环轮询节奏。不是节流 —— 循环体里已经没有阻塞 I/O，错误统计又按
        # requestDetails 的 rowid 水位线做了增量判断（无新行时一次轮询 ~2µs）。
        # 留 0.05s 只是让出 CPU：零间隔空转会烧满一个核，GIL 争用反而拖慢
        # 真正干活的换线线程。
        "monitor_sec": 0.05,
        # Clash selector 快照刷新间隔。主循环不再直接请求 Clash API（单次超时
        # 3s，放在 20Hz 的循环里等于把错误检测钉死），只读这个后台线程的快照。
        "clash_poll_sec": 0.5,
        "proactive_rotate_sec": 60,
        "cooldown_after_switch_sec": 60,
        # 单节点 429 冷却 60s：除非全体限流，否则池子里总有未冷却节点；
        # 全体限流时换节点本就无意义。
        "429_cooldown_sec": 60,
        "singbox_watchdog_sec": 30,
        "node_health_sec": 300,
        "subscription_refresh_sec": 3600,
        "proxy_check_sec": 60,
        # proxy → WARP 的三条回切依据（任一成立才回切）。WARP 恢复本身不是依据：
        # 只要代理池还能服务流量，就该让它服务完 —— 它是兜底资源，不是临时跳板。
        "proxy_fail_streak": 3,        # 连续 N 次池健康探测全失败
        # 落池后 opencode 仍被 403/429 拦截 ≥N 条就回切 WARP。
        # 原值 5 是按「WARP 单出口」的尺度定的，放到 100+ 节点的池上等于
        # 立刻触发：2026-11-09 实测池上 3 条请求就有 1 条 429（机房 IP 共享，
        # 节点级配额本来就紧），5 条 429 在十几条请求内必到 —— 落池后几分钟
        # 就被拉回去，而回去的是一个还在全池惩罚期的 WARP。改成 50：要求
        # 池真的持续在拦（约 150 条请求量级），而不是正常的 429 抖动。
        "proxy_blocked_threshold": 50,
        "proxy_dead_ratio": 0.5,       # 池黑名单节点占比 ≥N
        # 第七闸口（代理池兜底）的维护节奏：_proxy_gate_keeper 每隔这么
        # 多秒检查一次池闸 —— 刷新健康结论、把死节点换掉、在役时按
        # proactive_rotate_sec 的节奏换节点。主循环的池轮换只翻主进程的
        # proxy selector，翻不到第七闸口自己的进程，轮换必须由 keeper 做。
        "proxy_gate_check_sec": 60,
        # 第七闸口「在役」判定窗口：最近一次被指为重试目标之后这么久
        # 之内，认为 7897 上还有真实流量，需要按节奏换节点保持地址新鲜；
        # 窗口外它是待命兜底闸，不轮换（省节点配额），只维护健康结论。
        "proxy_gate_serving_window_sec": 300,
    },
    "thresholds": {
        "recent_window": 5,    # 策略/错误判据的滑动窗口条数（太小会被单条噪声带偏）
        "reject_window": 50,   # 403 客户端拒绝占比的统计窗口（10 条太小，噪声会抖动）
        "opencode_min_success_rate": 0.9,
        "node_test_timeout_ms": 15000,  # 放宽到 15s，避免慢节点误判
        "node_test_url": "https://www.gstatic.com/generate_204",
        "pressure_error_rate": 0.3,   # 429 率超过此阈值 → 切均匀分散
        # 回滞下沿 0.1 → 0.15：实测 215 次 pressure→normal 恰好停在 10% 边界上，
        # 与 188 次 normal→pressure(30%) 构成 598 次翻转的抖动 —— 每次翻转都换
        # 一次选点方式。拉宽到 15% 后 10~15% 区间不再来回穿。
        "normal_error_rate": 0.15,    # 429 率低于此阈值 → 切回延迟优先
    },
    "bad_nodes": {
        "ttl_sec": 300,       # 5 minutes - daemon.py 独占读写（proxy-global.js 已移除）
        "max_ttl_sec": 604800,  # 7 days max TTL (replaces float('inf') permanent blacklist)
    },
    # WARP 主备隧道：members 顺序即优先级，daemon 在被标记时切到另一条，
    # 并异步重连闲置线刷新其会话 IP（入口段无断流）。
    # ===== 同步轮换接口（/rotate，走 Unix domain socket）=====
    # 不开 TCP：WSL 镜像网络下 127.0.0.1 与 Windows 宿主共享，开端口等于把
    # 「换出口」这个动作暴露给宿主上任何进程（实测 Windows 侧可达且返回 200）。
    # UDS 靠文件 0600 + 目录 0700 保护，且不占端口。
    "api": {
        "enabled": True,
        # socket 放项目目录而不是 /run：/run 归 root，非 root 进程建不了目录；
        # 要用 /run 得给 unit 加 RuntimeDirectory=，部署面变大。
        # 按 PROJECT_DIR 推导，不写死家目录：换用户/换机器直接失效（实测在
        # Ubuntu 上以 ubuntu 运行时报 Permission denied 于旧用户家目录，
        # daemon 启动即退，systemd 无限 auto-restart）。
        "socket_path": os.path.join(PROJECT_DIR, "run", "rotate.sock"),
        "token_path": os.path.join(PROJECT_DIR, "run", "api-token"),
        # 换线前实测：给当前这条线一次真实探测的机会。9router 报 429 只是它
        # 自己的观测，实测 OK 就不该换掉一条还能用的线。
        # 超时必须大于 probe_warp 的正常耗时（H2 线握手实测 1.5~3.1s，慢线更长），
        # 否则结论只会是「4s 到点、无结论」，那和没探一样。9router 侧的
        # ASIP_TIMEOUT_MS 要同步放宽，否则调用方先挂断。
        "pre_rotate_probe": True,
        "pre_rotate_probe_timeout_sec": 12,
        "pre_rotate_probe_ttl_sec": 30,
        # 选槽排序用的延迟数据有效期。延迟只在事件驱动探测时更新（不轮询
        # ——探测打的是 opencode 接口，轮询会烧配额），15 分钟是「读数仍可
        # 用于同档内比较」与「不拿陈旧读数当真」的折中。
        "slot_probe_ttl_sec": 900,
        # 并发去重窗口：同一次故障的多发请求只切一次。
        "coalesce_ms": 800,
        # 槽位报错后给 9router 的短冷却（秒）。后台修整通常 2~5s；这段时间
        # 不把新请求继续轮到这个端口，避免每个新请求都来再报一次 429。
        "slot_cooldown_sec": 15,
        # daemon 刚起来的首轮探测窗口（秒）。六条槽位从 unknown 陆续
        # 出结论实测要 20~45s；这期间「没有健康备槽」只是还没探完，
        # 不是六条闸全废，不能让 9router 白重启（19:52 连重启 2 次）。
        # 这是硬上限：超时后即便还有 unknown 也不再抑制，避免某条线
        # 长期探不出结果时永远不发信号。
        "slot_boot_grace_sec": 60,
        # 生产 429 未带 Retry-After 时的兜底封禁时长（秒）。探针实测上游
        # FreeUsageLimitError 的 Retry-After 为 11~13h；默认 rate429_guard_sec=60
        # 会让刚封的 /128 立刻回池并被反复复用。9router 透传真实值时以它为准。
        "production_429_ban_sec": 43200,
        # 限速按「上一次切到的线是否已验证」分档：
        #   - 已验证（缓存命中，可确信是优质出口）：8s。好线撑 8 秒绰绰有余，
        #     窗口内的 429 多半是瞬时限流而非配额耗尽，换线只会白扔一个好 IP。
        #   - 未验证（兜底路径重启后拿到的新地址，质量不可控）：1s。必须允许
        #     快速逃离，否则用户得等满 8 秒才能甩掉一条坏线。
        # 换线下限（秒）。真正的间隔由 _rotate_gap() 按「惩罚期 / 活线数」算：
        # 16 条线时是 3.75s，只剩 3 条 usque 时自动退到 20s。这里只是地板值，
        # 防止线很少时被算成「每次请求都换」。固定 8s 在 16 条线的规模上会丢掉
        # 一半换线机会（实测 24 次调用 20 次被拒）。
        "min_gap_verified_sec": 1.5,
        "min_gap_unverified_sec": 1.0,
    },

    # ===== opencode 专属闸口（kind="opencode"，业务逻辑在 asip/opencode_gate.py）=====
    # 槽位产出委托 opencode_gate.make_slot；rotate 走 /rotate-opencode-gate
    # 委托 opencode_gate.handle_rotate。端口基准不在这里另设：
    # 沿用 singbox.mixed_port / clash_api 的既有避让规则继续编号
    # （consumer_slots() 把 base_port/base_clash 传给 make_slot）。
    "opencode_gate": {
        "slots": 1,            # 槽位数；0 = 不产出该闸口
        "coalesce_ms": 800,    # 合并窗：一批并发报障只换一次（与 api.coalesce_ms 同值）
        "cooldown_sec": 300,   # 报障节点的独立冷却（不写公共冷却表）
        # 专属账本（JSONL）。只记本闸口自己的轮换归因，绝不写公共 node_info。
        "ledger_path": os.path.join(PROJECT_DIR, ".opencode_gate_ledger.jsonl"),
        # slot-8 专属流量日志（sing-box log.output）：连接日志不再混写公共
        # singbox.log，`asip oc log` 按这个文件看原生请求（出站连接粒度）。
        "log_path": os.path.join(PROJECT_DIR, "opencode-gate.log"),
        # opencode CLI 应用级日志（429/错误/语义事件，连接级流量日志的互补面）。
        # `asip oc app` / `oc st` 读它（能读就读，缺位优雅降级）；
        # env OPENCODE_LOG 可覆盖，解析在 opencode_gate.opencode_log_path()。
        "opencode_log": os.path.expanduser(
            "~/.local/share/opencode/log/opencode.log"),
    },

    "warp": {
        "selector": "opencode-out",
        "socks_host": "127.0.0.1",
        # ipv6=True → 隧道用 `usque socks -6`（MASQUE over IPv6，出口落在 SJC）；
        # 其余走 v4（出口 LAX）。这不是装饰：换协议版本即换出口地理位置，
        # 部署脚本据此生成 systemd ExecStart，别删。
        #
        # 2026-10-28 主人定：CF/usque 线从 20 条缩到 3 条 —— 1 条 v4 + 2 条 v6。
        # 旧的 20 条是为了扛排空并发（换线间隔 p50 8s、排空 p50 68s），当时
        # 池里只有 usque；现在 12 条 WARP-WG 线并入同一个 members 表分担轮换，
        # 容量不再需要靠 usque 数量堆，多留的 17 条只是多 17 份注册身份和
        # 17 个待探测端口（2026-10-28 已全部 stop + disable，单元文件留着好回滚）。
        "members": [
            # 2026-11-13 00:10（主人定）：9 条 CF 线**全部走 h2**，其中 5 条入口 v4、
            # 4 条入口 v6。理由链：
            #   1. 本机到 CF 的 UDP 443 被运营商 ACL 拦（ICMP type 3 code 13），
            #      QUIC 版 v4 端点根本起不来 → 必须走 TCP+TLS 的 --http2。
            #   2. `--http2` 与 `-6` 可以叠加，v6 侧同样能走 TCP 443（实测
            #      2606:4700:103::2 Connected 成功）。所以 h2 不等于只能用 v4。
            #   3. v4/v6 两条 TCP 通路互为备份：一条被封时另一条还能出。
            # 三个字段别混：
            #   transport = 本地到 CF 的承载（h2 = v4 TCP+TLS / h2v6 = v6 TCP+TLS /
            #               quic6 / quic4），deploy/members.py 原样渲染成单元末参。
            #   entry     = **入口**地址族，也就是 tag 里 4/6 的含义。
            #   family    = **出口**地址族。两种 h2 的出口都是 WARP 分配的
            #               2a09:bac* v6，所以这里恒为 v6 —— 别拿它当 entry 用。
            # h2_endpoint 是这条线 config.json 里实测可用的端点，daemon 不读，
            # 留着是为了换端点时不必逐个翻 .warp/usqueN/config.json。
            {"tag": "WARP-CF4", "port": 1080, "service": "usque-warp.service",
             "transport": "h2", "entry": "v4", "family": "v6",
             "h2_endpoint": "162.159.198.3"},
            {"tag": "WARP-CF4-B", "port": 1081, "service": "usque-warp-b.service",
             "transport": "h2", "entry": "v4", "family": "v6",
             "h2_endpoint": "162.159.198.4"},
            {"tag": "WARP-CF4-C", "port": 1082, "service": "usque-warp-c.service",
             "transport": "h2", "entry": "v4", "family": "v6",
             "h2_endpoint": "162.159.198.5"},
            {"tag": "WARP-CF4-D", "port": 1083, "service": "usque-warp-d.service",
             "transport": "h2", "entry": "v4", "family": "v6",
             "h2_endpoint": "162.159.198.6"},
            {"tag": "WARP-CF4-E", "port": 1084, "service": "usque-warp-e.service",
             "transport": "h2", "entry": "v4", "family": "v6",
             "h2_endpoint": "162.159.198.7"},
            {"tag": "WARP-CF6", "port": 1085, "service": "usque-warp-f.service",
             "transport": "h2v6", "entry": "v6", "family": "v6",
             "h2_endpoint": "2606:4700:103::2"},
            {"tag": "WARP-CF6-B", "port": 1086, "service": "usque-warp-g.service",
             "transport": "h2v6", "entry": "v6", "family": "v6",
             "h2_endpoint": "2606:4700:103::2"},
            {"tag": "WARP-CF6-C", "port": 1087, "service": "usque-warp-h.service",
             "transport": "h2v6", "entry": "v6", "family": "v6",
             "h2_endpoint": "2606:4700:103::2"},
            {"tag": "WARP-CF6-D", "port": 1088, "service": "usque-warp-i.service",
             "transport": "h2v6", "entry": "v6", "family": "v6",
             "h2_endpoint": "2606:4700:103::2"},
            # WARP-WG-13 不再写死在这里：块模型把渠道数从 12 扩到 20 之后，
            # 第 13 条就是生成成员里的普通一条，写死会得到两个同 tag 成员
            # （同一端口、同一单元、同一份设备），selector 里重复项直接报错。
            # 线路数不足 13 的部署仍需要它，见 `_append_wg_members` 末尾。
            #
            # 2026-11-02：H2 线路并入。与上面 9 条的区别是**落地路径**——
            # 那 114 条 H2 线由本机 usque 连 `127.0.0.1:2270N`，再经 sing-box
            # 的 direct 入站借机场节点转发到 CF；本文件的 9 条是**直连** CF 端点。
            # 对 daemon 而言两者都是普通 socks 出口，探测/换线路径无需分叉。
        ],
        # 换线触发阈值（两类都按出口 IP 判定，换出口即拿到新配额/新信誉）：
        # 两者都是 1：任何一条出口级拦截出现即换，不再等累计。
        # 曾把 429 阈值设为 5（"429 太频繁，单条就换会让隧道反复重启"），
        # 但实测 22:35~22:38 在 429 风暴里每多等一条就是继续失败：
        # 阈值 5 期间流量全废，而当时的冷却只有 20s（现为 cooldown_ok_sec=5），
        # 换线成本远低于等待成本。
        "block_threshold": 1,
        "rate429_threshold": 1,
        # 传输层 503（9router Bun 传输悬挂，无上游字节）：不是出口级拦截，
        # 不进惩罚台账，也不计入 consec/full_err。达到阈值后先经当前隧道探
        # 出口：探通判为 9router 侧故障不换线，探不通才换线。
        # gap 是探针节流——503 批量落库时每秒都会重触，不能每次起线程。
        "transport_fail_threshold": 1,
        "transport_probe_gap_sec": 15,
        # 换线后旧链上的 opencode 长连接会被 sidecar 继续复用（keep-alive），
        # 若其隧道已静默死亡，每次复用都要白等满超时。换线成功即异步清理
        # age >= 120s 的旧链连接：正常请求 p50 6.5s，120s 以上的不可能是
        # 在途请求，但 45 分钟的僵尸连接会被这里清掉。
        # 2026-10-27 12:19 实测：DELETE /connections 会把 sidecar 正在复用的连接
        # 一起删掉，客户端看到 "upstream connection lost"，sidecar 侧 socket hang
        # up → 503 → 再次触发清理，形成正反馈。默认关闭，只保留代码与配置开关。
        "cleanup_enabled": False,
        "cleanup_old_chain_age_sec": 120,
        # 出口探测正常但仍报传输层 503 → 判为连接池僵死，清理当前链上
        # age >= 300s 的连接（30 秒节流，避免 503 风暴里反复全表扫描）。
        "transport_stale_age_sec": 300,
        "transport_stale_gap_sec": 30,
        # 主动轮换判据二选一（互斥，只有选中的那个生效）：
        #   fixed    — 无论流量快慢，每 proactive_fixed_sec 秒换一次；
        #   requests — 当前出口上成功的 dsv4 调用累计满 proactive_after_dsv4_ok
        #              就换（数的是配额格数，不是请求条数）。
        # 时间判据的缺陷：实测主动段（60s 到点切走）dsv4 成功中位 113.5 /
        # p90 175 —— 60s 上限本身就在扔掉还没用完的配额，说明容量不是时间的
        # 函数。而 429 段的存活中位仅 6s，同样不是时间的函数。两个分布都不随
        # 时钟走，任何固定周期都只能在「切早了浪费地址」和「切晚了吃 429」
        # 之间瞎猜；按已用配额切才和配额扣减同源，流量大小自动适应。
        # 当前生效：requests（按成功 dsv4 调用数换线）。切回 fixed 只需改这一行。
        # 2026-10-28 主人要求切回 requests（阈值 150 格）。
        "proactive_mode": "requests",
        "proactive_fixed_sec": 60,
        # requests 模式数哪个模型的配额。配额按模型扣，别的模型成功不占这格。
        "proactive_quota_model": "deepseek-v4.1-flash-free",
        # requests 模式的切换阈值，单位是**成功的** dsv4 调用数（配额格）。
        # 2026-10-22 10:20~11:52 在 fixed 模式下回放「到达 T 格就切」，结论是
        # T=100 仅 13% 段可达、T=150 仅 3% —— **这组数字不可用来定 requests 阈值**：
        # 那些段在 60s 被主动切走（205 段里中位段长 61s），150 格根本来不及跑，
        # 是右删失而不是地址跑不动。
        # 直连实测（12:21 切到 requests 后的若干段，12:23~12:33）：
        #   12:23:05→12:25:23  138s 跑到 150 格 → 配额换线
        #   12:25:28→12:27:53   86 格处撞 rate429 ×3 → 错误换线
        #   12:27:57→12:30:44  167s 跑到 151 格 → 配额换线
        #   12:30:49→12:32:45  116s 跑到 151 格 → 配额换线
        # 5 段里 3 段跑满、1 段先被限流、1 段进行中；跑满时每段约 2~2.8 分钟。
        # 所以本模式确实会按配额触发（不是旧注释说的「多数时段不触发」），
        # 效果是把健康段拉到约 2 分钟，快限流的段仍由 429 兜底接管。
        # 若观察到 429 占比上升，把本值调小（每段随之变短）；调大更省地址，
        # 但更多段会在半途吃 429。
        # 2026-11-13 00:27 主人要求：阈值 150 → 300（每条线把一个出口用到 300 格
        # 再主动换，减少换线次数/地址消耗）。注意这是「成功的 dsv4 调用数」，
        # 不是 HTTP 请求条数；单 IP 实测容量 150+ 是**判活前的上限估计**，
        # 真按 300 走会有一部分段在半途吃 429，由错误换线兜底。
        "proactive_after_dsv4_ok": 300,
        # 备线「可服务性」探针：暂未实现。曾尝试经目标隧道的 SOCKS 直接向
        # opencode 发最小请求，但免费层的客户端指纹校验无法通过 —— 试过
        # /zen/v1 与 /inference/v1 两个端点、v1/v2 两种 UA、cli/desktop 两种
        # client、完整 read+shell 工具定义与 9router 的 applyFingerprintTools，
        # 全部返回 403 FreeTierError（真实 9router 请求里带的是 exec_command
        # 等另一套工具，指纹规则比源码注释描述的更复杂）。只有 9router 自己
        # 发出的请求能通过。在没有可靠探针之前，主动轮换仍是盲切。
        # 每隔多久预热一条闲置备线（重启它换会话 IP）。ip_reuse_guard_sec=24h，
        # 不预热的话换线时只有刚补刷的一条是新鲜的，流量会在两条线间往返。
        "prewarm_sec": 120,
        # 每次错误换线最多实测几条候选（0=全部）。测一条烧该出口一格配额，
        # 17 条全测要 15s 且把整池配额烧光，实测换线 2 分钟仍恢复不了。
        "probe_candidates_per_switch": 2,
        # 每轮预热最多排几条（0=全部）。全量 18 条/2min 会自己烧掉备线配额。
        "prewarm_max_per_round": 4,
        # 换线前校验目标线的出口 IP：窗口内用过的地址必须先刷新会话再切。
        # 429/403 都按出口 IP 判定，切回一个还在限流窗口里的地址等于没换。
        # 86400s（24h）：opencode 对同一个出口 IP 的 403/429 记忆很长，实测出口
        # 可用周期 p50 仅 19s，几十分钟内切回几乎必然再 403。历史教训：3600s 窗口下
        # 25.2 / 28.3 / 49.1 分钟的复用间隔真实发生过；900s 窗口下 18:58:05 用过的
        # 2a09:bac5:1efb:281e::3ff:42 在 10.6 分钟后被原样切回，53 秒后即爆发。
        "ip_reuse_guard_sec": 86400,
        # 同一 /64 静置多久算「刚烧过」。只在同档内影响先后（见 _warp_ip_rank）：
        # opencode 免费额度按出口段计，新 /128 落在刚用过的 /64 里首条请求照样
        # 429。9router 近 2h 失败流量分组：段静置 <5min 有成功比例 73%（n=41）、
        # 5-15min 100%（n=21）、>30min 91%（n=35）。
        "prefix_rest_sec": 300,
        # 被标记（403/429）的 IP 保留多久不出候选池。不小于较长的那个固定
        # 恢复期（403 6h 与旧记录的 24h）；解析到精确封禁的条目另有
        # ban_until > now 单独保护，不会被这个 TTL 提前清掉。
        "blocked_ttl_sec": 86400,
        # 选线的分级复用窗口（按触发原因）：
        #   未用过的地址 > 窗口外没用过的（403 为 6h、429 为默认冷却~用户
        #   解析出的精确封禁） > 全都不满足时兜底复用最久未用的。
        # 按 IP 记录被哪类错误烧掉，分别计时：
        #   abuse403 —— 出口 IP 信誉标记，六小时后刷新；
        #   rate429 —— 默认六十秒冷却；精确恢复时刻来自 429 响应的解析链
        #   （header Retry-After 秒数/HTTP 日期 → 响应体字段 → 报错文案），
        #   经 ban_until 覆盖本默认（钳制 [1s, 24h]，对齐 9router 的
        #   parseRetryAfterMs）。生产库不落响应头，解析失败是常态——
        #   60s 是兜底回池节奏，不是「429 只关六十秒」的承诺。
        # 换线对 403 与 429 都有效，两者都是出口 IP 级拦截。
        "blocked_guard_sec": 21600,   # 6h：403 恢复期
        "rate429_guard_sec": 60,      # 429 默认冷却（精确值经 ban_until 覆盖）
        # 台账生命周期（docs/design-20261113-ledger-lifecycle.md）：
        # expires_at 的保留窗口，条目到期后才允许清理。默认与
        # ip_reuse_guard_sec 同值，独立配置便于灰度期单独调。
        "ledger_retention_sec": 86400,
        # 清理判据模式："legacy" 走旧规则（ts cutoff + ban_until 豁免），
        # "expires_at" 只看 expires_at < now（INV-4）。P2 切换，回滚只需改配置。
        # expires_at 模式下「缺 expires_at」= 未迁移，先按最近事件补齐再判；
        # 补齐也定不出值的条目保留并计入 ledger_shadow.missing_expires，绝不静默删。
        "ledger_cleanup_mode": "legacy",
        # 生产侧 429 的**定性方式**：单发 429 就判死这条线，还是先复测再判死。
        #   off      —— 不复测，直接冷却 + 24h 标脏（P0-1 之前的旧行为，仅作回退）
        #   borrowed —— 只有借道来的 429 复测（当前生产行为，默认）
        #   all      —— 所有 rate429 都先异步复测，复测仍 quota 才冷却
        #              （abuse403 不延后定性：它是 6h 信誉级惩罚，复测口径
        #              不能覆盖，见 daemon 侧 defer_verdict 处注释。开关名
        #              与行为一致 —— 本开关只管 429。）
        #
        # 为什么做成开关而不是直接改成 all：**没有数据支撑「原地 429 有多少是假
        # 429」**。现有两份数据互相矛盾 ——
        #   · 原地 429 只占 5.6%（33,221 发里 1,844），远低于借道的 77.1%，
        #     看起来「是我自己撞上的，判死合理」；
        #   · 但同一批日志里 27.9% 的 429 发生在新 IP 的首发上，此时额度根本
        #     没被消耗，说明「一发 429 就判死」至少在这个子集上是错的。
        # 两个结论都来自同一份日志，只能靠实验分辨，不能靠偏好选一个。
        # 切到 all 后看「复测 OK : 复测 quota」的比例即可定论。
        # 复测自带去重（_EGRESS_RECHECK_WINDOW_SEC），不会对同一地址反复探。
        "rate429_recheck_mode": "borrowed",
        # 无可用备线时原地重连的上限次数与退避
        "max_attempts": 3,
        "retry_backoff_sec": [2, 5, 10],
        "ready_timeout_sec": 12,
        # 刷新路径（重启隧道后）单次出口探测的超时。健康探测仍用 20s：
        # 已稳定的线偶发 >8s 响应，20s 才够。但刚重启的隧道要么一两秒就通，
        # 要么根本没起来 —— 02:36 风暴里 95 次刷新失败中 65 次是探测失败，
        # 单次 32s（12s 等端口 + 20s 探测）把换线线程钉死。
        "refresh_probe_timeout_sec": 7,
        # 刷新备线时是否额外做一次模型层探测（probe_warp.py）。关掉即回到
        # 原模式：只验「这个地址没被烧过」，不验它此刻能不能出流量 ——
        # 新地址几分钟内 429 的情况真实存在，原模式会让候选池大但不保证能用。
        # 开启后每条候选多耗一格出口配额、多 1~3s（实测 B 线曾 16.8s）。
        "refresh_model_check": _env_bool("ASIP_REFRESH_MODEL_CHECK", True),
        # 2026-11-05 实测：15s 下 WARP-CF-D(1083) 连续 6 次 `bun: The operation
        # timed out` 全判 unreachable，而同一端口手工 curl `warp=on` 正常；
        # 拉到 30s 才有机会拿到真实结论（OK / quota）。原 15s 的说法是
        # 「多 1~3s」，那个数字量的是**已就绪**的线，没算链路抖动时的长尾。
        # 代价：超时的线每次多等 15s，但并发跑（`model_probe_concurrency`），
        # 且 unreachable 不再触发重启（见 `_warp_line_needs_restart`），
        # 多等的时间不进入任何决策路径。
        "refresh_model_timeout_sec": 30,
        # 换线后短暂静默让在途请求落定。固定值，不随连续失败放大 ——
        # 只要目标线能拿到窗口外的新鲜 IP，换线就值得做；拉长冷却只会让
        # 错误持续积累（实测 2^n 退避曾在风暴期锁死到 600s，九分钟零换线）。
        # 失败换线后的冷却。403 常在换线后 1~2 秒内出现（实测段周期中位 14s、
        # 最短 <1s），20s 冷却等于让坏出口空转 18s —— 改为 5s，只留出在途请求
        # 落定的时间。并发仍由 _WARP_LOCK 互斥。
        # 换线成功后至少隔这么久才允许因错误再换。WHY 1s 不够：换上去之后的
        # 新出口首条错误延迟实测 p25=0.7s / p50=1.6s / p75=4.5s（52 次样本），
        # 1s 冷却等于让 40% 的「刚换就错」在冷却后立刻再触发一次换线 —— 正是
        # 日志里「几十条好流量→两条 error→两条好流量」锯齿的来源。5s 覆盖 79%。
        # 例外：最近 10 条全错时绕过本冷却（见 maybe_reconnect_warp），风暴里
        # 停在坏出口上的代价远大于多换一次。
        "cooldown_ok_sec": 5,
        # 两次换线的**生效**间隔下限（秒）。含 full_err 风暴例外，是硬闸。
        # WHY 必须按生效时刻而非触发时刻起算：触发→生效实测 p50=4s / p90=9s，
        # 按触发起算时这段空档完全不算数，新出口刚上线就立刻满足闸门 —— 实测
        # 46 次错误触发里 39 次（85%）两条错误全部开始于上一段出口，新出口一条
        # 请求都还没发就被切走。按生效起算后同口径重放：141 次触发全部真错。
        # 取 5：等于 cooldown_ok_sec，覆盖新出口首条错误延迟的 79%（p75=4.5s）。
        "min_switch_gap_sec": 5,
        # 换线后「旧连接」豁免窗口：窗口内的 403/429 可能来自上一段的在途长连接，
        # 一律不标脏。换线门控在下方独立生效，新出口真坏了仍会切走。
        "switch_stale_grace_sec": 120,
        # 换线动作本身失败（两条线都不通）时的重试间隔
        # 失败也压到 3s：60s 的失败冷却会让一次换线失败后整分钟停在坏出口上。
        # 防雷暴由 `_WARP_LOCK` 的互斥 + `cooldown_fail_sec` 的短退避共同保证，
        # 不需要 60s 那么长（线只有 5 条，每次都换不同目标）。
        "cooldown_fail_sec": 3,
        # WARP 隧道全部不可用时切轮换池的冷却：每 3h 最多切一次。
        # 实测 2026-10-20 一天落池 80 次，多数是在两个坏出口之间来回弹；
        # 冷却期内改走原地重连（有几秒断流），不再把流量甩到池上。
        "proxy_fallback_cooldown_sec": 10800,
        # 落池总开关。2026-10-26 关闭：池内 anytls 节点全部 connection reset，
        # 落池等于断流；关闭后 WARP 全不可用时只原地重连，不再把流量甩到死池。
        #
        # 2026-11-09 重新打开，理由与配套闸门（缺一不可）：
        #   1. 池子已恢复：当晚 04:58 体检 183✓/3✗、池内有效样本 186/247；
        #      06:20 把 `opencode-out` 切到 `proxy` 实测 3 条 opencode 请求
        #      2×200（4.4s / 4.2s）、1×429 —— 池能承载 free lane 流量。
        #   2. 关掉它的代价已经量化：21:00–23:00 全池被判 quota（新换的 IPv6
        #      一律 Retry-After 10~11h），937 次换线只成功 4 次，同期 432 条
        #      503（当晚 598 条里的 72%）。同一地址空间里换 IP 买不到配额，
        #      能换的只有「地址空间」本身。
        #   3. 不再裸落：`_fallback_to_proxy` 现在落池前必须
        #      `_proxy_pool_healthy()` 为 True（随机抽 3 条实探），且
        #      `_warp_exhaustion_escape` 要求连续 3 次「等不到备线」才触发。
        "proxy_fallback_enabled": True,
        # 连续多少次「等不到新鲜备线」才允许落池。备线判据是「120s 内出现过
        # OK」，全池惩罚期里该条件永远无法满足（自锁）。单次无备线可能只是
        # 一条线打嗝，留观 3 次再换地址空间。
        "warp_exhaust_streak_min": 3,
        # 换线后多久刷新被换下的闲置线。太短会掐断仍在走它的在途请求
        # （upstream connection lost），太长则赶不上下一次换线（实测标记速度
        # 约 20~30s），届时同步守卫又要多等 7~9s。
        "refresh_delay_sec": 20,
        # 主渠道被切下后成为备线，这条线在这么多秒内不许重启。
        # WHY 需要独立于「判闲」的硬窗口：判闲只证明「过去 12s 没在传字节」，
        # 而流式响应在两次 token 之间静默 p90=9.1s、首 token 前最长 532.7s，
        # 刚被切下的线往往正处在「已建连、请求还没走完」的状态。实测 646 次
        # 换线里 63.3% 的被切下线在 120s 内就被重启（中位 73s，最快 0s）——
        # 重启掐断在途 SSE，客户端收到的就是 API Error: upstream connection
        # lost。120s 与 `_FIRST_BYTE_WAIT_MAX_SEC` 同源：离渠那一刻建立的连接，
        # 年龄超过 120s 就不在「首字节未到」窗口内了。
        # 代价是被切下的线 120s 不能当刷新候选，故池子需覆盖「换线间隔 × 2min」。
        "standby_cooldown_sec": 120,
        # 刷新前等待该线闲下来的上限，超出则等下一轮（强刷会掐断长命连接
        # → upstream connection lost）。判"闲"看字节是否还在增长，不看连接数：
        # 活跃线上恒有 7~10 条连接且从不归零，按连接数等必然超时。
        # 取 90：排空路径的观察窗是 30s（_DRAIN_BUSY_WINDOW_SEC），40s 只够
        # 判一次，判忙就直接超时放弃；90s 能判三次，给在途流真正结束的机会。
        "idle_wait_sec": 90,
        # 判"闲"的字节观察窗（秒）。实测流式 SSE 的两次 token 之间会静默
        # p50=7s / p90=9.1s / max=10.1s（N=30 连接 100s 采样），而旧值 2s
        # 让一半以上的采样落在静默期里 → 把仍在传流的线判成空闲 → 立即重启
        # → 9router 注入 upstream connection lost。取 12s 覆盖实测 max。
        # 代价：判定一次要 12s。只给后台补刷/脏线守护付，不影响换线响应 ——
        # 错误换线路径显式传 `_ERROR_PATH_BUSY_WINDOW_SEC`(2s)，见 daemon.py。
        "busy_window_sec": 12.0,
        # 被换下的线等不到空闲时反复重试的总时长。单次超时就放弃会让这条线
        # 永远停在旧地址上：实测 WARP-CF-C 被换下 8 次，7 次放弃，13 次换线里
        # 7 次切回同一个 10 分钟前的地址。
        "ip_refresh_retry_sec": 600,
        # 没有全池并发上限：20 条隧道互不依赖，各自 systemctl restart 互不影响，
        # 排队只让脏线在池里多待几十秒。唯一互斥的是**同一条线**（两条线程同时
        # 重启同一条隧道会白烧一次探测）。实际并发度由 `prewarm_sec` 与脏线
        # 巡检的 5s 节奏自然节流。
        # 换线时直接采用脏线守护最近一次探测到的出口地址（见
        # `_cached_warp_standby`），把同步探测移出关键路径。
        # TTL 必须覆盖「一整轮探测」，否则算数上就达不到：20 条隧道、单次探测
        # 1.2~7s，整轮 24~140s；取 12s 时任意时刻只有 12/轮询时长 ≈ 9%~50% 的
        # 线算「新鲜」，实测命中率 34%（最近 10 次仅 10%）。取 180s 覆盖整轮，
        # 后端确认（daemon.py `_invalidate_warp_line_ip`）三条重启路径都会在重启时
        # 立刻作废该线绑定，所以「已知 IP 变了」不靠 TTL 计时器兜，TTL 只承担
        # 「没有证据时的信任衰减」。取 1800s 与探测吞吐（每线中位 233s）匹配。
        # 代价是可能选中一条「缓存里还是旧地址、实际已刷新」的线 —— 后果是重试
        # 再吃一次 429，只重试一次即返回，失败有封顶。
        "probe_cache_ttl_sec": 1800,
        # 模型层探测结论（`_WARP_PROBE_SEEN`）的保留时长，按结论的持久性分两档：
        #   - 持久（quota/flagged）：地址自身的配额/信誉结论。重探同一 /128 有
        #     99.2% 仍 quota（30h 内仅 1 次恢复、间隔 56min），留 6h —— 与生产
        #     403 惩罚期同量级。
        #   - 瞬时（unreachable/err）：探测侧故障，不代表地址状态（日志实测同一
        #     地址 err 后 66s 即 OK）—— 这一档只留 2min。
        #   - OK 单独一档 30min：它说的也是地址状态，而重探间隔中位 212s，30min
        #     约合 8.5 轮 —— 实测 1980 次「前一次 OK」里 1831 次（92.5%）结论不变，
        #     拉长只是少探已验过的地址。地址真被烧时生产侧 `_probe_cache_forget`
        #     会立即作废 OK，不靠这个 TTL 兜底。
        "probe_transient_ttl_sec": 120,
        "probe_ok_ttl_sec": 1800,
        # 与探测节奏同量级。旧值 21600 是第二道停探闸：结论一落缓存，
        # 这条地址 6 小时内不再被观测 —— 而那正是最该持续看的对象。
        # 重复探测的代价已被入口的惩罚期短路挡住（判脏地址根本不探）。
        "probe_persistent_ttl_sec": 600,
        # **H2 承载实测的失败结论有效期**（秒）。
        # 此前失败是**永久**结论：`probe_h2_nodes.py` 与
        # `daemon._pending_probe_nodes` 都只判 `tested and not ok` 就跳过，
        # 于是 158 个失败节点（最早 11-05）永不复测 —— 节点恢复后也进不了
        # 池，H2 供给只减不增。反证：`ddixx-1dd1.dankenet.help:3526-any`
        # 11-05 判失败、11-11 实测可用（出口 104.28.211.32），却因「永久
        # 失败」永不重测。6h 与生产 403 惩罚期同量级，够挡住真死节点。
        "h2_probe_fail_ttl_sec": 21600,
        # 单实例探测器：所有线路的出口/模型探测都由唯一后台线程写入状态表，
        # 换线只读表 + 条件变量等待，不再在 reconnect_warp 里同步探测。
        # prober_enabled=False 只用于回滚到旧路径（测试默认不会启动探测器）。
        "prober_enabled": True,
        # 备线「新鲜」的定义：last_ok_ts 距今 ≤ 这个秒数才算可用备线。
        # 换线只接受新鲜备线 —— 旧 OK 说明的是「那时能用」，不是「现在能用」。
        #
        # **必须与 `probe_ok_ttl_sec` 同值（1800）**。这两个数字此前不一致
        # （120 vs 1800），产生了一个自相矛盾的状态：一条线探测上是「30 分钟内
        # 不必重测」（缓存命中直接复用），换线上却是「2 分钟前的 OK 已作废」。
        # 于是它既不会被重测、也进不了候选池 —— 夹在两套判据中间，
        # `asip chan` 显示 OK、换线却报「暂无 120s 内 OK 的备线」。
        # 实测 2026-11-08 20:00：整池 74 条结论里 59 条 quota，剩下的 OK
        # 全被 120s 判死，换线连等 3 次（6 秒）。半小时口径下候选取的是
        # 「30 分钟内验过且此后没被生产流量标脏」的地址，与探测缓存同一份证据。
        #
        # 地址真被烧时由生产侧 `_probe_cache_forget` + `_warp_line_invalidate`
        # **立即**作废（不等 TTL），所以放宽这个窗口不会切到已烧地址 ——
        # 作废是事件驱动，续期才是定时。
        "line_fresh_sec": 1800,
        # 探测节奏：无论上次结论是什么，同一线两次观测之间至少隔这么久。
        # 结论只决定「探完拿它做什么」，不决定「下次还探不探」。
        # 0 = 探完立刻重新排队。曾经是 60s：36 条线 × 60s 把吞吐锁死在
        # 36 条/分钟，而单次探测只要 1.9~5.7s —— 加渠道本来是为了提速，
        # 却按「渠道数 × 60s」算轮次，等于白加。
        "line_probe_cadence_sec": 0,
        # 探不到结论（unknown）时这条线的重试间隔。**恒为 0**：没结论就立刻
        # 重新排队，两次探测之间不插任何停顿。旧值 5 是探测等待，与主人
        # 2026-11-11「探测轮询间隔 0、禁止设置等待时间」的定标冲突，
        # 见 docs/established-facts.md 12b。禁止改回 >0、禁止做成配置项。
        "probe_unknown_retry_sec": 0,
        # 同时在飞的行级探测线程上限。探测本身不限速，并发是唯一的物理上限：
        # 36 条线 × 1.9~5.7s，需要 32 个槽才能把一轮压到单次探测耗时量级。
        "probe_concurrency": 32,
        # 模型层探测（真打上游 chat/completions）的并发闸。与上面分开是因为
        # 它撞的是上游限流而不是本地 CPU。取值同上：单条实测 WG v4 1.9s / CF 5.7s。
        "model_probe_concurrency": 32,
        # 每分钟探测次数上限。0 = 不限。旧值 30/600 是纯自我设限：
        # 烂地址在入口就被惩罚期短路（判脏根本不探），再压频次只会让
        # 「新地址刚刚变可用」这件事没人发现。
        "probe_max_per_min": 0,
        # 无 due 线时探测器的最长休眠。有 due 时条件变量会立刻唤醒，不影响时延；
        # 它只决定「全都闲着时别空转」。
        "prober_idle_wait_sec": 30,
        # 无新鲜备线时的等待上限。旧值 20s：实测 74 次换线里 73 次等满 20s
        # 后照样放弃（一共空转 24.7 分钟），因为等待期间没有任何人让「新鲜度」
        # 变好。现在等待前先催探测器，所以等的是「探测写入」，3s 够一个来回。
        "switch_wait_sec": 3,
        # 重启被「不是失败」的原因挡下时（在途连接没排空、拿不到重启槽、
        # 这条线正在被别处处理）的重排间隔：不是惩罚，只是别原地空转，
        # 所以固定 5s，不随 fail_streak 涨。
        "restart_cooldown_sec": 5,
        # 2026-10-28 删除 restart_backoff_sec / restart_backoff_max_sec：
        # 这条曲线从未真正生效 —— 重启成功会把 next_restart_ts 清零，紧接着
        # 探到 quota 又立刻重启，退避表根本没被查过（实测 168 次重启/分钟、
        # fail_streak 涨到 6586 而间隔仍是最低档）。现在重启只受
        # restart_cooldown_sec 这个固定冷却约束。
        # 每线每小时重启上限。0 = 不设上限（2026-10-28 按主人要求去掉）。
        # 它挡的从来不是观测，而是「把一个确定不能用的地址换掉」这个动作本身：
        # 撞上限后线路就原地带着烂地址继续跑，比多重启几次更糟。
        # 现在唯一的间隔来源是 restart_cooldown_sec 这个固定冷却。
        "restart_max_per_hour_per_line": 0,
        # 全局重启节流（条/分钟，0=关）。单线上限管不住 19 条线叠加：
        # 2026-10-28 19:36~20:08 实测 10028 次重启 / 125 分钟。
        "restart_max_per_minute": 0,
        # 「重启后仍处惩罚期」熔断：连续 N 次重启仍 quota 就停 hold 秒。
        # 2026-11-09 夜里配额普遍耗尽时把一条条线全熔断，池子被打空，
        # 主人定性「非常愚蠢」。设 ASIP_RESTART_IP_STUCK_FUSE=0 表示禁用
        # （代码用 max(2,...)，0 仍会变 2，故用极大值 999999 禁用）。
        "restart_ip_stuck_fuse": _env_int("ASIP_RESTART_IP_STUCK_FUSE", 3),
        "restart_ip_stuck_hold_sec": _env_int("ASIP_RESTART_IP_STUCK_HOLD_SEC", 900),
        # 「重启没换出地址」的踩刹车阈值 —— 与上面的配额熔断是**两回事**：
        # 上面判的是地址好不好（上游配额结论，2026-11-09 已按主人裁定禁用，
        # 因为夜里会把池子打空）；这里判的是**重启这个动作有没有效果**：
        # 连续 N 次重启都拿不到任何出口地址（隧道起得来、端口通、却无出口），
        # 说明问题在落地节点或链路，再重启只是每 5~15s 掐断一次在途连接。
        # 刹车只推迟下次重启，不写 ip_stuck_until、不碰 stop_probe ——
        # 线路照旧参与探测与选线。实测 2026-11-10 08:30 前后 12 分钟：263 次
        # 重启里 194 次以「到点无响应」收场，47 条扫描线 fail_streak 涨到 188。
        "restart_futile_limit": _env_int("ASIP_RESTART_FUTILE_LIMIT", 6),
        "restart_futile_hold_sec": _env_int("ASIP_RESTART_FUTILE_HOLD_SEC", 900),
        # 同时在飞的重启数。每条 quota/blocked 线都要「重启换地址 → 等就绪 →
        # 重探」，这个闸才是现在的真正吞吐瓶颈：4 槽 × 一次 3~5s ≈ 20~40 条/分钟。
        # 12 是折中：16 条同时注册会撞 Cloudflare 的 ~9 台/窗口限流，吃 429 后
        # 退避 30/90/240s，反而比排队更慢。
        # 按渠道的 ban 率锁。判据（2026-11-04 主人定）：
        #   当天最近 `channel_ban_min_samples`(1000) 条模型层实测**全部**判
        #   quota —— 出现任何一条 OK/flagged/err 就否决；或
        #   连续 `channel_noaddr_hold_sec`(5h) 探测不到出口地址。
        # 命中后锁到次日 reset_hour，并且**锁定期间不再探测、不再重启**。
        # 旧口径（1h 窗口 100% 封禁 + 铺满半窗）实测会把能出 OK 的线锁一整天：
        # WG-21 锁在 11-03 08:00 后，10:52~13:37 期间换两个新 IP 出了 7 次 OK，
        # 但锁不解除；到期时窗口里的旧样本又把它续到次日。
        "channel_ban_hold_enabled": True,
        "channel_ban_min_samples": 1000,
        # 转义给了 `ipv6_segments.colo_ban_rates`（重定位挑 colo 用），与本锁无关。
        "channel_ban_window_sec": 3600,
        # 连续拿不到出口地址多久锁渠道。与 quota 锁分开：拿不到地址 = 隧道/节点
        # 死了，重启换地址也买不到东西。期间任何一次拿到地址就清零。
        "channel_noaddr_hold_sec": 5 * 3600,
        "channel_ban_reset_hour": 8,
        # 命中后不只是停刷：把这条线换到最近 1h ban 率最低的 colo 上重定位，
        # 次日 08:00 再换回原节点（`rehome_from`）。
        "channel_rehome_enabled": True,
        # 目标 colo 至少要有这么多条样本才配当「最低 ban 率」，否则单向 3 条
        # 零封禁的 colo 会把整批线吸过去。
        "channel_rehome_min_samples": 10,
        # 重定位的目标必须落在**这个块里还有节点**的 colo 上：`wg_colo_map.json`
        # 对远端常常是空/过期（survey 只在 bootstrap 里跑一次），此时候选表
        # 只剩历史封禁率，挑出来的 colo 可能根本没节点可换。
        "channel_rehome_require_nodes": True,
        # 同块节点轮回（2026-11-02 主人定）：脏线守护每轮先看**当前绑定的落地
        # 节点**还通不通，不通就换到同一个 colo 块里的可用节点再重启。
        # 与「换线」和「探测」是两件事：换线换出口 IP、探测判 IP 质量，这里只
        # 判节点本身。WHY 必须独立成一路：`validate_bindings` 只在节点从订阅池
        # 里消失时才换（节点还在但已经不通就不管），`_restart_warp_line(rebind)`
        # 只覆盖 v4 线 —— v6 线的节点死掉后 daemon 会一直重启一条注定连不上的线。
        "node_rotation_enabled": _env_bool("ASIP_NODE_ROTATION", True),
        # 同一条线两次节点探测的最小间隔。探测 = 起一个 sing-box 进程再连一次，
        # 5s 一轮 × 29 条线会把机器打满；而节点死亡不是秒级状态。单次不通只记
        # 一笔，下一轮复查仍不通才换 —— 机场抖动与探测冷启都会让单次探测失败。
        "node_rotation_interval_sec": 60,
        # 错误换线（transport ×N / 链路悬挂类）成功后，顺手把被切下来的那条线
        # 修好：在同一个渠道块（colo）内换到延迟低且 TCP 可达的节点。H2 换
        # selector 零重启；WG 换节点后 rebound 重启一次（它刚被 demote，不在
        # 承载流量）。不修的话坏节点躺回候选池，下一轮又被选中再死一次
        # （实测 08:22–08:45 WG-1↔WG-9↔ICN-13 每 15~30s 弹跳）。
        # rate429 / 惩罚期 / 低质启动校验 / 主动轮换不触发 —— 那些是 IP 或
        # 配额级问题，节点本身是好的，换掉等于白丢一条能用的链路。
        "demoted_node_repair_enabled":
            _env_bool("ASIP_DEMOTED_NODE_REPAIR", True),
        # 同一条线两次修复的最小间隔：修复含一次节点 TCP 探测与（WG）一次
        # 重启；风暴期若每次换线都修同一条线，会复制换线弹跳。
        "demoted_node_repair_cooldown_sec":
            _env_int("ASIP_DEMOTED_NODE_REPAIR_COOLDOWN", 600),
        "restart_concurrency": 12,
        "ip_observe_sec": 120,
        # fixed 模式的提前量：换线从发起到达生效 p50=2s / p90=7s，等满
        # proactive_fixed_sec 再发起就赶不上节律，必须提前这个时长。
        # requests 模式不用它：配额是离散格数，没有「生效延迟」可提前。
        "switch_lead_sec": 3.0,
        # 错误换线路径上等目标备线空闲的上限。补刷路径用的是 idle_wait_sec(90s)，
        # 但错误路径上每多等一秒，坏出口就多服务一秒失败请求 —— 实测 40s 上限
        # 把「首错→换线生效」拉到 p90=25s / max=206s。
        "fallback_idle_wait_sec": 3,
        # 夜间刷新锁：窗口内只刷新「隔夜存活」的线，其余线的重启一律跳过。
        # WHY：上游免费层配额在次日 08:00 重置，20:00→08:00 之间被判 quota 的
        # 出口重启动也只会拿到另一个同样 quota 的新 /128 —— 10-31 夜里实测
        # 17036 次重启、0 次刷新成功，全是白烧。活下来的是那几条地址本身没
        # 被烧完的线，夜里该把新地址都花在它们身上。
        # allow 里的 tag 还会与 sing-box 实际成员取交集：不在 `opencode-out`
        # 里的线（如尚未并入 .warp/warp-outbound.json 的 CF-E/CF-F/WG-13）
        # 刷出地址也切不过去，自动被排除。
        "night_window": {
            # **第一期不做夜间锁**（2026-11-01 主人定）：第一阶段的目的就是
            # 「全部渠道白天夜里都跑，测出每个 colo 的夜间封禁率」，锁住一部分
            # 线等于把要测的样本自己删掉 —— 夜里到底哪条线能出货，就没数据了。
            # 第二期按实测筛出夜间专用池时再把这条打开，白名单改用
            # `warp_blocks.night_line_tags`（按块生成，见 docs 第四节）。
            # 锁的实现留在 warp_night.py，关掉的只是一个开关。
            "enabled": False,
            "start_hour": 20,
            "end_hour": 8,
            "allow": [
                # 10-31 20:00 → 11-01 08:00 存活榜（daemon.log 逐线 OK 计数）：
                # CF 9 / CF-B 9 / CF-F 6 / CF-E 6 / CF-D 6 / WG-13 5 / WG-5 5 / WG-6 5
                "WARP-CF", "WARP-CF-B", "WARP-CF-D", "WARP-CF-E", "WARP-CF-F",
                "WARP-WG-5", "WARP-WG-6", "WARP-WG-13",
            ],
        },
        # 每条隧道一个常驻 worker（warp_line_worker.py）：「判断→执行」同循环，
        # 不再经预热/脏线抽检/补刷三个调度器排队。enabled=False 时完全走旧的
        # 三守护路径，行为与之前逐字节一致，便于回滚。
        # dry_run=True 只探出口并记录「本应换线/重启/验收」，不写台账、不动服务。
        "line_worker": {
            "enabled": False,
            "dry_run": True,
            "interval_sec": 30,
        },
        # 原生 WireGuard 版 WARP（sing-box endpoints），UDP 经代理节点中转。
        # opt-in：enabled=False 时完全不改变现网行为，也不往 members 里加线。
        # WHY 经代理：本机到 162.159.192.1:2408 的 UDP 被运营商 ACL 拦
        # （ICMP type 3 code 13），直连握手只会超时。
        "wg": {
            # 单一开关：WARP_WG_FAMILIES 为空即关闭。不再另设 enabled —— 两个
            # 开关会漂移成「文件里有线、daemon 看不见」的半开状态。
            "enabled": _wg_count > 0,
            "count": _wg_count,
            "families": _WG_FAMILIES,
            "per_node": len(_WG_FAMILIES) or 1,
            "home_nodes": _WG_HOME_NODES,
            # 每条线可切的后备节点数（含当前节点）。v6 线基本用不到（重启就换
            # 地址），v4 线和节点故障时才走它。
            "rotation_size": _env_int("WARP_WG_ROTATION", 8) or 8,
            "udp_cache_ttl_sec": 6 * 3600,
            "base_port": 1110,
            "tag_prefix": "WARP-WG-",
            "service_prefix": "warp-wg-",
            "wgcf_bin": _env_path("WGCF_BIN", os.path.join(PROJECT_DIR, ".warp", "wgcf")),
            # 本地入站只监听环回：线路端口由主 sing-box 的 socks outbound 消费，
            # 不需要对外暴露。
            "listen_host": "127.0.0.1",
            # peer 用 IP 而非域名：线路进程不走本机解析器。
            "peer_v4": "162.159.192.1:2408",
            "peer_v6": "[2606:4700:103::2]:2408",
            "mtu": 1280,
            "keepalive": 25,
        },
        # 渠道分配模型：块 = colo，渠道 = (类型, 块, 序号)。
        # 设计依据 docs/warp-channel-allocation.md；模型在 warp_blocks.py（纯读，
        # 不测量、不写盘），这里只放**块的静态属性**。
        #
        # 块成员（哪个节点属于哪个 colo）不写在这里：它来自实测表
        # `.warp/block-map.json`（gitignored，由 `warp_blocks.py build-map` 生成）。
        # 两处各存一份节点清单必然会漂移 —— 配置里的这份永远是上个星期的事实。
        #
        # enabled=False：模型不接管现网拓扑，apply 仍走 assign_nodes 的轮转分配。
        # 打开前先跑 `python3 warp_blocks.py plan` 看渠道清单，别在夜里开。
        "blocks": {
            "enabled": _env_bool("ASIP_WARP_BLOCKS", False),
            # 每块每类几条渠道。2 是「同 colo 一主一备」：同块两条线共用一个段池，
            # 再多只会互相抢地址，不会多出可用地址。
            "channels_per_block": _env_int("ASIP_WARP_BLOCK_CHANNELS", 2) or 2,
            # 结构性不可用的块：封禁率 100% 的 colo 每条 /128 都在限额里，
            # 挂上去等于占着设备不产出。**第一阶段默认空**（2026-11-01 主人定：
            # 全部块都开、一起测），只在明确要放弃某个块时才往里加。
            # 当天实测参考：NRT 4 条线夜里 0 次模型层 OK，PHX/SJC/SIN/TPE 有。
            "exclude_colos": [],
            "table": [
                # ban_rate 是 2026-11-01 实测的**段封禁率**，只用于夜间取舍：
                # 1.0 的块夜里重启动只会换到另一个同样被封的 /128。
                # None = 没测过（HKG/TPE/ORD/PIT 当天才首现），夜间必须保留。
                # `channels` 覆盖全局 `channels_per_block`，按**该块的池深**给条数。
                # 不写就回落到全局值。池深见 `.warp/block-map.json` 实测：
                # HKG 24 / SIN 15 / NRT 9 / TPE 7 / SLC 4 / ICN 3 / LAX 3，
                # LHR 2 / FRA 2，ORD/BOM/SJC/GIG 各 1。
                #
                # WHY 不能一刀切：实测 2026-11-08 的 /48 重叠显示，NRT 4 条线
                # 共用同一批 12 个 /48（第 4 条在抢地址），而 ICN 3 条线各拿 12 个
                # 不重叠的段、单线唯一 IP 576/天 —— 池子质量差 10 倍。
                # 缺设备由 render_h2_lines.py 自动注册（用户规则），不要靠砍条数。
                {"colo": "NRT", "loc": "东京",     "carrier": "proxy",  "ban_rate": 1.0,
                 "channels": 13},
                {"colo": "PHX", "loc": "凤凰城",   "carrier": "proxy",  "ban_rate": 0.792,
                 "channels": 6},
                {"colo": "LAX", "loc": "洛杉矶",   "carrier": "both",   "ban_rate": 0.266,
                 "channels": 8},
                {"colo": "SJC", "loc": "圣何塞",   "carrier": "both",   "ban_rate": 0.25,
                 "channels": 3},
                {"colo": "SIN", "loc": "新加坡",   "carrier": "both",   "ban_rate": 0.152,
                 "channels": 21},
                {"colo": "TPE", "loc": "台北",     "carrier": "proxy",  "ban_rate": None,
                 "channels": 8},
                {"colo": "HKG", "loc": "香港",     "carrier": "proxy",  "ban_rate": None,
                 "channels": 18},
                {"colo": "ORD", "loc": "芝加哥",   "carrier": "proxy",  "ban_rate": None,
                 "channels": 1},
                # 2026-11-02 H2 实测新增（dormant 65 节点里首次出现）。
                # carrier=proxy：H2 与 WG-over-UDP 同属「代理承载」，区别只在
                # 传输层（TCP/HTTP2 vs UDP/WireGuard），不改变块的归类。
                # ban_rate=None：段池从没在生产台账里出现过，夜间必须保留。
                {"colo": "ICN", "loc": "首尔",     "carrier": "proxy",  "ban_rate": None,
                 "channels": 4},
                {"colo": "SLC", "loc": "盐湖城",   "carrier": "proxy",  "ban_rate": None,
                 "channels": 3},
                {"colo": "PIT", "loc": "匹兹堡",   "carrier": "proxy",  "ban_rate": None,
                 # PIT 没有自己的节点：唯一落到过 PIT 的是 ORD 那条
                 # `us.bing.com:443~6-any`（5 轮里 ORD 7 / PIT 3 / SIN 1）。
                 # 按众数它归 ORD，于是 PIT 块永远空着。想拿到 PIT 的样本只能
                 # 把这条节点**再挂一台设备**（pin），让它在漂到 PIT 的轮次里
                 # 被记成 PIT —— 代价是七成时间它其实就是一条 ORD 线。
                 # 这是「测量优先」的取舍，第二期有实测数据后可以撤掉。
                 "pin": ["us.bing.com:443~6-any"]},
            ],
        },
    },
    # Vercel 中继线路 —— 第三条地址空间（见 asip/vercel_relay.py 的模块注释）。
    #
    # 为什么放在 warp 之后单独一块：它不是一条隧道，也不是一个 sing-box 节点。
    # 它改的是 9router 自己的出口选择（providerStrategies + proxyPools），
    # 走的是 9router 的 relay 协议，sing-box 完全不参与。把它硬塞进 warp.members
    # 会得到一个「有 service、有端口」的假成员，所有探测路径都会去连一个不存在的
    # socks 口。
    #
    # 默认 enabled=False / takeover=False：打开之前不改变现网任何一个字节。
    # takeover=True 才让 daemon 在 403/429 之后改用中继轮换而不是 WARP 换线 ——
    # 两者同时动会互相抵消（一个换隧道、一个换池），日志里看不出是谁在动。
    "vercel": {
        "enabled": _env_bool("ASIP_VERCEL_ENABLED", False),
        "takeover": _env_bool("ASIP_VERCEL_TAKEOVER", False),
        # 只接管这一个 provider。opencode 是 9router 里的 noAuth free provider，
        # 它的出口就是 providerStrategies.opencode（auth.js 里写死的判据）。
        "provider": os.environ.get("ASIP_VERCEL_PROVIDER") or "opencode",
        # round-robin：9router 每请求现算（pickProxyPoolId），写库即生效，不用重启。
        # 单条 relay 的出口 IP 本来就常变，轮换的意义是分散到多个部署/区域。
        "strategy": os.environ.get("ASIP_VERCEL_STRATEGY") or "round-robin",
        "ledger_path": _env_path(
            "ASIP_VERCEL_LEDGER", os.path.join(PROJECT_DIR, ".vercel_relay.json")),
        "probe_target": os.environ.get("ASIP_VERCEL_PROBE_TARGET") or "https://api.ipify.org",
        "probe_timeout_sec": 10.0,
        # 同一个出口地址在这个窗口内不再选（与 warp.ip_reuse_guard_sec 同量级）。
        "guard_sec": _env_int("ASIP_VERCEL_GUARD_SEC", 86400),
        # 至少要留几个 active 池。低于它就别 off —— 一个空池等于直连真实 IP。
        "min_active_pools": _env_int("ASIP_VERCEL_MIN_ACTIVE", 2),
        # daemon 侧两次中继轮换的最小间隔，防 429 风暴里连环写库。
        "rotate_cooldown_sec": _env_int("ASIP_VERCEL_ROTATE_COOLDOWN", 5),
        # 部署（asip vercel deploy）：Vercel token 只从环境变量/.env 读，不落台账。
        "deploy": {
            "count": _env_int("ASIP_VERCEL_DEPLOY_COUNT", 3),
            "region": os.environ.get("ASIP_VERCEL_REGION") or "",
            "project_prefix": os.environ.get("ASIP_VERCEL_PROJECT_PREFIX") or "asip-relay",
        },
    },
    "log_file": _env_path("ASIP_LOG_FILE", os.path.join(PROJECT_DIR, "daemon.log")),
    # 第七闸口（代理池兜底）独立账本。slot-7 报 429 实测约 50 行/min，混在
    # daemon.log 里会把换线/告警冲走；默认独占：主文件不再收池闸行，
    # stdout/journal 仍全量（journalctl 是统一排障账本，不能因为拆文件就断）。
    # ASIP_GATE_LOG_MIRROR=1 恢复双写，兼容按 daemon.log grep 的旧习惯。
    "gate_log_file": _env_path("ASIP_GATE_LOG_FILE",
                               os.path.join(PROJECT_DIR, "gate.log")),
    "gate_log_mirror": _env_bool("ASIP_GATE_LOG_MIRROR", False),
    # 4a 告警事件流水：独立 JSONL（P3 状态权威落 SQLite 事件表前的过渡落点）。
    # 为什么不复用 daemon.log：5MB×5 轮转，磁盘满/告警风暴时最先滚掉的恰恰
    # 是因果；滚动日志可以丢，事件流水不能。
    "alert_event_file": _env_path("ASIP_ALERT_EVENT_FILE",
                                  os.path.join(PROJECT_DIR, ".asip_events.jsonl")),
    # 外发通道（可选）。留空=只落流水 + daemon.log；webhook 是参考实现，
    # 邮件/IM 走 asip.alerting.AlertSink 后插拔，不改配置结构。
    "alert_webhook_url": os.environ.get("ASIP_ALERT_WEBHOOK_URL") or "",
    "log_format": "%Y-%m-%d %H:%M:%S",
}

def _append_wg_members(cfg: dict) -> None:
    """WARP-over-proxy 线路并入 members（仅 enabled 时）。

    对 daemon 来说它就是一条「有 service、有 socks 端口」的出口，探测/预热/
    换线路径无需分叉。安装单元由 deploy/wg-lines-install.sh 负责，这里只声明。
    """
    wg = cfg["warp"]["wg"]
    if not wg["enabled"] or wg["count"] <= 0 or not wg["families"]:
        return
    per = len(wg["families"])
    for i in range(1, wg["count"] + 1):
        cfg["warp"]["members"].append({
            "tag": f"{wg['tag_prefix']}{i}",
            "port": wg["base_port"] + i - 1,
            "service": f"{wg['service_prefix']}{i}.service",
            "kind": "wg",
            "index": i,
            # 第 i 条线属于第几组落地节点，以及该组内的渠道位置。
            "home_group": (i - 1) // per,
            "family": wg["families"][(i - 1) % per],
        })
    # 历史遗留的第 13 条线：它原本是「WG-5 同配置 ×1」的额外线（独立注册在
    # line13）。线路数 ≥13 时生成成员已经覆盖同一个端口/单元/设备，再补一条
    # 就是重复 tag；只有 12 线这种老配置才需要把它单独补回来。
    # 注意这里刻意不带 kind/index/family —— 历史行为里 daemon 不把它当
    # 「可换节点的 v4 线」，补字段会改变它的重启语义。
    if wg["count"] < 13:
        cfg["warp"]["members"].append({
            "tag": "WARP-WG-13",
            "port": wg["base_port"] + 12,
            "service": "warp-wg-13.service",
        })


_append_wg_members(CFG)

def _append_h2_members(cfg: dict) -> None:
    """H2 线路并入 members（仅渲染产物存在时）。

    成员的唯一事实来源是 `.warp/h2/channels.json` —— 它是
    `deploy/render_h2_lines.py` 按块模型 + 设备占用算出来的台账。手抄进
    `config.py` 必然与渲染产物漂移：改渠道数/换设备后，配置里的端口会指向
    已经不存在的线，daemon 探测全失败却看不出原因。

    文件不存在（未渲染过）时什么都不做，行为回到旧版。
    """
    import json
    p = _env_path("ASIP_H2_CHANNELS_PATH",
                  os.path.join(PROJECT_DIR, ".warp", "h2", "channels.json"))
    try:
        with open(p, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(rows, list):
        return
    for r in rows:
        try:
            cfg["warp"]["members"].append({
                "tag": r["member_tag"],
                "port": int(r["socks_port"]),
                "service": r["unit"],
                "kind": "h2",
                "index": int(r["index"]),
                "colo": r["colo"],
            })
        except (KeyError, TypeError, ValueError):
            continue

_append_h2_members(CFG)

def _append_backfill_members(cfg: dict) -> None:
    """补位渠道并入 members（`.warp/backfill.json`，daemon 开/撤时写）。

    必须在这里读、而不是只改 daemon 内存里的 members：`deploy/members.py`
    与 `parse_subscription.py` 都是**独立进程**，各自 import 一次 config。
    只在 daemon 里 append，warp-outbound.json 与主配置都看不见它 —— 补位
    渠道起来了却没人能切过去。

    文件里是 daemon 自己的台账，读不到就当没有（补位只在锁定期存在）。
    """
    import json
    p = _env_path("ASIP_BACKFILL_FILE_PATH",
                  os.path.join(PROJECT_DIR, ".warp", "backfill.json"))
    try:
        with open(p, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(rows, list):
        return
    tags = {m.get("tag") for m in cfg["warp"]["members"]}
    for r in rows:
        # 同 tag 重复进 outbounds，sing-box check 直接 FATAL（同 members.py 的注释）。
        if not isinstance(r, dict) or not r.get("tag") or r["tag"] in tags:
            continue
        try:
            cfg["warp"]["members"].append({
                "tag": r["tag"], "port": int(r["port"]),
                "service": r["service"], "kind": "wg",
                "index": int(r["index"]), "family": r.get("family") or "v6",
                "backfill_for": r.get("backfill_for"),
            })
            tags.add(r["tag"])
        except (KeyError, TypeError, ValueError):
            continue

_append_backfill_members(CFG)

# ============= 状态权威（P3） =============
# 库必须落 Linux 本地盘：WAL 的共享内存与文件锁在 9p（/mnt/c）/网络挂载上
# 不可靠，写进去等于把回滚点建在流沙上。state_store 打开时会自查 /proc/mounts
# 并在远端文件系统上直接拒开（见 asip/state_store/store.py 的 StoreError）。
# 路径按 PROJECT_DIR 推导、不写死家目录：换用户/换机器直接失效（与
# api.socket_path 踩过的同一个坑同名同因）。
CFG["state_store"] = {
    "db_path": _env_path("ASIP_STATE_DB",
                         os.path.join(PROJECT_DIR, "run", "state.sqlite3")),
    # 影子对账（双写 + 周期 diff + 差异告警）。默认关闭：上线即零行为变化，
    # 要开必须显式打开并观察一个窗口 —— 差异=0 是切读门槛（T2c），不是默认态。
    "shadow": _env_bool("ASIP_STATE_SHADOW", False),
    "shadow_interval_sec": 300,
    # 事件流水进库（alerting.configure(journal=<Store>)）。关闭时仍走 JSONL，
    # 即 p6a 已交付的 EventJournal —— 关这个键不会丢告警。
    "journal_enabled": _env_bool("ASIP_STATE_JOURNAL", True),
    # 台账保留上限（健康面 ledger_headroom 判据）。超限报 degraded 而不自动删：
    # 清理走后台低优先车道，且不得与 /rotate 抢 IO（阶段 2 硬约束）。
    "ledger_headroom_cap_bytes": 32 * 1024 * 1024,
}

# ============= 便捷常量 =============
# ponytail: SINGBOX_SECRET 不在此导出为模块级常量——CFG["singbox"]["secret"] 是 import 时冻结的
# 旧快照，轮换后不会刷新。调用方必须走 get_singbox_secret()（mtime 自动刷新）。
NODE_INFO_DB = CFG["9router"]["db_path"]
MIXED_PORT = CFG["singbox"]["mixed_port"]
BAD_NODES_TTL = CFG["bad_nodes"]["ttl_sec"]
BAD_NODES_MAX_TTL = CFG["bad_nodes"]["max_ttl_sec"]
LOG_FORMAT = CFG["log_format"]
RECENT_WINDOW = CFG["thresholds"]["recent_window"]
REJECT_WINDOW = CFG["thresholds"]["reject_window"]
# 传输层错误的回看秒数。不能用 RECENT_WINDOW 条（默认 5）：高流量下 ~2s 就滚出
# 窗口，503 在 10s tick 前早被挤出，检测不到 → 僵死连接永远不清。
TRANSPORT_LOOKBACK_SEC = 180

# 订阅源顺序即合并优先级（okz 主力，其余补充）。值为空表示未配置。
SUBSCRIPTION_KEYS = ("okz", "hd23n", "dasho", "dasho_alt")

# 机场自有域名体系 —— 这些域名在公网 DNS 上是 NXDOMAIN，或被解析到**真实**的
# bing.com（202.89.233.x），而节点要连的是机场自己的前置 IP。
#
# 放在 config 而不是 parse_subscription：**主配置和单线配置都要解析它们**。
# 只写在 parse_subscription 里时，`warp_wg.build_line_config` 造出来的线只有
# 本机 DNS —— 节点入口 `hk-aws-54.acczzz.com` 直接 NXDOMAIN，隧道永远起不来，
# 而报错是 `failed to create session: lookup …`，看起来像节点挂了。
# 2026-11-01 用探针复现：44 个「不能承载 WARP」的节点里，凡是入口是机场域名的
# 都是这个死法（`us.bing.com`、`hk-aws-*.acczzz.com`、`unamecf*`）。
AIRPORT_DNS_SUFFIXES = ("okzdns.com", "juhazf.cn", "hi2026.net", "acczzz.com",
                        "bing.com")
AIRPORT_DNS_URL = "https://custom.juhazf.cn/inst"
AIRPORT_DNS_TAG = "dns-airport"
AIRPORT_DNS_HOST = "custom.juhazf.cn"
AIRPORT_DNS_PATH = "/inst"


def needs_airport_dns(server: str | None) -> bool:
    """该节点入口是否必须走机场 DoH 才能解析出前置 IP。"""
    s = str(server or "")
    return any(s.endswith(suffix) for suffix in AIRPORT_DNS_SUFFIXES)



def subscription_urls() -> list[str]:
    """Configured subscription URLs, in merge order, empty entries dropped."""
    return [url for url in (CFG.get(k, {}).get("sub_url", "") for k in SUBSCRIPTION_KEYS) if url]


EXTRA_SUBS_FILE = _env_path("ASIP_EXTRA_SUBS_FILE",
                            os.path.join(PROJECT_DIR, ".extra_subscriptions"))


def extra_subscription_urls() -> list[str]:
    """追加订阅源：环境变量 ASIP_EXTRA_SUB_URLS + `asip sub add` 写的台账文件。

    为什么用文件而不是只写 .env：daemon 的 EnvironmentFile 只在启动时读一次，
    写 .env 必须重启才生效 —— 而重启会掐断在途 SSE。台账文件在每次刷新的
    `_sub_urls()` 里现读，插一个源不用动进程。环境变量仍优先（临时覆盖/试跑）。

    顺序排在固定源之后：固定源是主力，追加源是补充。
    """
    raw = os.environ.get("ASIP_EXTRA_SUB_URLS") or ""
    urls = [p for p in raw.replace(",", " ").split() if "://" in p]
    try:
        with open(EXTRA_SUBS_FILE, encoding="utf-8") as f:
            urls += [ln.strip() for ln in f
                     if "://" in ln and not ln.lstrip().startswith("#")]
    except OSError:
        pass
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

# ============= 启动校验 =============
# requests 模式整个判据就是「数哪个模型的成功调用」。键写错时 daemon 不该
# 带着一条永不触发的判据上线 —— 导入即炸，比线上静默不换线好查得多。
_w = CFG["warp"]
if _w.get("proactive_mode") not in ("fixed", "requests"):
    raise SystemExit(f"✗ warp.proactive_mode 只能是 fixed/requests，实际 {_w.get('proactive_mode')!r}")
if _w["proactive_mode"] == "requests" and not _w.get("proactive_quota_model"):
    raise SystemExit("✗ warp.proactive_quota_model 未配置，requests 模式无法计数")

# 夜间锁：窗口反了就是「全天锁死」或「永不生效」，两者都只在夜里才被发现 ——
# 那时已经没人看日志了。allow 为空同理：一条都不许重启 = 夜里彻底停止刷新。
_nw = _w.get("night_window") or {}
if _nw.get("enabled"):
    _sh, _eh = _nw.get("start_hour"), _nw.get("end_hour")
    for _k, _v in (("start_hour", _sh), ("end_hour", _eh)):
        # bool 是 int 子类，`isinstance(True, int)` 为真 —— 写成 True 能悄悄
        # 通过并变成「1 点」，必须单独挡掉。
        if isinstance(_v, bool) or not isinstance(_v, int) or not 0 <= _v <= 23:
            raise SystemExit(f"✗ warp.night_window.{_k} 必须是 0~23 的整数，实际 {_v!r}")
    if _sh == _eh:
        raise SystemExit(
            f"✗ warp.night_window 起止小时相同（{_sh}）＝全天生效。"
            "要关就用 enabled: False")
    _allow = _nw.get("allow")
    if not isinstance(_allow, list) or not _allow:
        raise SystemExit("✗ warp.night_window.allow 为空 —— 窗口内将没有任何线可刷新")
    # 非字符串元素会被 daemon._night_allow_tags 的 isinstance 过滤悄悄丢掉，
    # 全丢光就变成「夜里一条都不许重启」—— 正好是设计要避免的那一侧。
    if not all(isinstance(t, str) and t.strip() for t in _allow):
        raise SystemExit(f"✗ warp.night_window.allow 含非字符串/空串: {_allow!r}")
    # 不在这里校验 tag 是否存在：成员集合随 WARP_WG_* 环境变量变（没 source
    # `.env` 时 WG 线根本不在 CFG 里），拿它当导入期断言会让「配置没问题、
    # 只是环境没加载」变成启动失败。交集在 daemon._night_allow_tags 里做。

# 渠道锁解封时刻同样只在「写到锁时」生效一次，写错了要等第二天才发现。
# `True` 是 int 子类会变成 01:00；`"08:00"` 让每次探测记账都抛 ValueError；
# 25 会被 mktime 归一化成次日 01:00 —— 三种都不报错，必须挡在导入期。
_rh = _w.get("channel_ban_reset_hour", 8)
if isinstance(_rh, bool) or not isinstance(_rh, int) or not 0 <= _rh <= 23:
    raise SystemExit(f"✗ warp.channel_ban_reset_hour 必须是 0~23 的整数，实际 {_rh!r}")

# 块表：colo 是主键，重复会让 channels() 给同一个块开两倍渠道、白名单算重。
# channels_per_block < 1 展开出 0 条渠道，而 apply 会因此报「渠道不足」——
# 报在 apply 时读起来像是实测表的问题，其实是一条配置写错了。
_blk = _w.get("blocks") or {}
if _blk:
    _tbl = _blk.get("table")
    if not isinstance(_tbl, list):
        raise SystemExit(f"✗ warp.blocks.table 必须是列表，实际 {type(_tbl).__name__}")
    _colos = [b.get("colo") for b in _tbl if isinstance(b, dict)]
    if len(_colos) != len(_tbl) or any(not isinstance(c, str) or not c for c in _colos):
        raise SystemExit("✗ warp.blocks.table 每项必须是带非空 colo 的字典")
    if len(set(_colos)) != len(_colos):
        raise SystemExit(f"✗ warp.blocks.table 里 colo 重复: {_colos}")
    if int(_blk.get("channels_per_block") or 0) < 1:
        raise SystemExit(
            f"✗ warp.blocks.channels_per_block 必须 ≥1，实际 {_blk.get('channels_per_block')!r}")

# ============= provider 常量 =============
# OPENCODE_FREE_PROVIDER: 9router providerStrategies 中的 key = "opencode"
# provider 值在 requestDetails 表中也是 "opencode"
OPENCODE_FREE_PROVIDER = "opencode"



def consumer_slots() -> list[dict]:
    """每槽位一台独立 sing-box 进程：独立端口、独立连接池。0 个时返回空表。

    9router 代理池放所有端口 round-robin。9router 连不同端口=不同 sing-box=
    不同连接池=不同线，天然隔离，秒切靠轮询不需要翻转 selector。
    """
    sb = CFG.get("singbox") or {}
    n = int(sb.get("consumer_slots", 0) or 0)
    sel = (CFG.get("warp") or {}).get("selector") or "opencode-out"
    base_port = int(sb.get("mixed_port", 7890))
    clash = sb.get("clash_api", "http://127.0.0.1:9090")
    base_clash = int(clash.rsplit(":", 1)[-1]) if ":" in clash else 9090
    out = []
    for i in range(n):
        clash_p = base_clash + i
        # 9091 被 h2 线的 Clash API 占用，从这里起整体后移 1
        if clash_p >= 9091:
            clash_p += 1
        port_p = base_port + i
        # 7891 被 LAN 入站占用，从这里起整体后移 1
        if port_p >= 7891:
            port_p += 1
        out.append({
            "name": "slot-%d" % (i + 1),
            "selector": sel,
            "port": port_p,
            "clash_api_port": clash_p,
            # 配置路径由同一处命名规则产出。生产代码禁止再从 slot-1 的
            # config_file 拼 "-slotN.json"：解析、启动、热重载三处各拼一次，
            # 任一处改名就会静默拉起旧配置。
            "config_file": consumer_slot_config_path(str(sb.get("config_file")
                                                       or ""), i),
        })
    if n and sb.get("proxy_gate", True):
        # 第七闸口（代理池兜底）：端口/Clash 端口沿用同一套避让规则继续
        # 编号（6 槽时是 7897 / 9097，均未被占用）。kind=proxy 是它和常规
        # 闸口的分界：daemon 所有 WARP 线逻辑（claim/put/repair/补闸）都
        # 跳过它，池内轮换只碰它的 pool_selector。
        # 它的 config_file 仍由同一命名规则产出（<base>-slot7.json），
        # 解析器随订阅刷新一起渲染它，promote 成组校验也自动覆盖。
        clash_p = base_clash + n
        if clash_p >= 9091:
            clash_p += 1
        port_p = base_port + n
        if port_p >= 7891:
            port_p += 1
        out.append({
            "name": "slot-%d" % (n + 1),
            "kind": "proxy",
            # selector 与常规闸口同名（opencode-out），但它的 default 在
            # 配置渲染时指到池 selector（parse_subscription 的
            # default_to_pool），且 daemon 永远不会往里 PUT WARP 线。
            "selector": sel,
            # 它的「线」是池节点：轮换/读当前节点都作用在这个 selector 上。
            "pool_selector": sb.get("selector") or "proxy",
            "port": port_p,
            "clash_api_port": clash_p,
            "config_file": consumer_slot_config_path(str(sb.get("config_file")
                                                       or ""), n),
        })
    # opencode 专属闸口（kind="opencode"）：槽位产出委托
    # asip/opencode_gate.make_slot —— 模块边界红线，这里只接线不写业务逻辑。
    # 端口/Clash 端口沿用上面同一套避让规则继续编号（slot_index 接着排），
    # config_file 仍走 consumer_slot_config_path 命名，不自己编号。
    # 函数内 import：config 是被 opencode_gate 延迟引用的底座，顶层互引会造环。
    from asip import opencode_gate
    _og = CFG.get("opencode_gate") or {}
    try:
        _og_n = int(_og.get("slots", 0) or 0)
    except (TypeError, ValueError):
        _og_n = 0
    for _j in range(max(0, _og_n)):
        out.append(opencode_gate.make_slot(base_port, base_clash, n + 1 + _j))
    return out


def consumer_slot_config_path(base_path: str, slot_index: int) -> str:
    """第 slot_index 个槽位（0 基）的 sing-box 配置文件路径。

    解析器可能从 `foo.json.tmp` 起草，此时槽位文件必须以去掉 `.tmp` 的最终
    路径为基准，否则会写出 `foo.json.tmp-slot2.json` 这种既不被校验、也不会
    被 promote 的孤儿文件。
    """
    base = str(base_path or "")
    if base.endswith(".tmp"):
        base = base[:-4]
    if slot_index == 0:
        return base
    return "%s-slot%d.json" % (base, slot_index + 1)
