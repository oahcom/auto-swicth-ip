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
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚠ Secret file loose perms ({oct(st.st_mode & 0o777)})")
        with open(SECRET_FILE) as f:
            _SINGBOX_SECRET = f.read().strip()
        _SINGBOX_SECRET_MTIME = mtime
        return _SINGBOX_SECRET
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✗ 读取密钥文件失败: {e}")
        return ""

# 启动时校验密钥 (活值) — 不冻结常量, 避免 secret 轮换后失效
if not get_singbox_secret():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] WARNING: sing-box secret 未设置 — 在 {SECRET_FILE} (chmod 600) 中写入 base64 secret", file=sys.stderr)

CFG = {
    "9router": {
        "db_path": _env_path("NINE_ROUTER_DB_PATH", os.path.join(HOME_DIR, ".9router/db/data.sqlite")),
        "base": "http://127.0.0.1:20127",
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
        "startup_grace_sec": 10,  # 增加到 10s，给 sing-box 更多初始化时间
        "max_restart_attempts": 3,
        "mixed_port": 7890,
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
        "proxy_blocked_threshold": 5,  # 落池后 opencode 仍被 403/429 拦截 ≥N 条
        "proxy_dead_ratio": 0.5,       # 池黑名单节点占比 ≥N
    },
    "thresholds": {
        "recent_window": 10,    # 策略/错误判据的滑动窗口条数（太小会被单条噪声带偏）
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
    "warp": {
        "selector": "opencode-out",
        "socks_host": "127.0.0.1",
        # ipv6=True → 隧道用 `usque socks -6`（MASQUE over IPv6，出口落在 SJC）；
        # 其余走 v4（出口 LAX）。这不是装饰：换协议版本即换出口地理位置，
        # 部署脚本据此生成 systemd ExecStart，别删。
        "members": [
            {"tag": "WARP-CF", "port": 1080, "service": "usque-warp.service", "ipv6": True},
            {"tag": "WARP-CF-B", "port": 1081, "service": "usque-warp-b.service", "ipv6": True},
            {"tag": "WARP-CF-C", "port": 1082, "service": "usque-warp-c.service", "ipv6": True},
            {"tag": "WARP-CF-D", "port": 1083, "service": "usque-warp-d.service"},
            {"tag": "WARP-CF-E", "port": 1084, "service": "usque-warp-e.service"},
            {"tag": "WARP-CF-F", "port": 1085, "service": "usque-warp-f.service"},
            {"tag": "WARP-CF-G", "port": 1086, "service": "usque-warp-g.service"},
            {"tag": "WARP-CF-H", "port": 1087, "service": "usque-warp-h.service", "ipv6": True},
            {"tag": "WARP-CF-I", "port": 1088, "service": "usque-warp-i.service", "ipv6": True},
            {"tag": "WARP-CF-J", "port": 1089, "service": "usque-warp-j.service"},
            # 2026-10-22 扩到 20 条。WHY：扩容前的实测是在排空并发上打满的 ——
            # 换线间隔 p50 8s（高频时 1.8 次/分钟），被切下的线排空 p50 68s /
            # p90 335s，任一时刻平均 4.9 条在排空（≥3 条占 84% 的时间），
            # 而池里只有 10 条、真正服务流量的只有 3 条。近 1 小时 23% 的
            # 换线时刻只剩 1 条候选。排空等的是真实在途流式响应（28 条连接
            # 180s 采样零增长占比 0.0%，不是僵尸连接），只能靠加容量吸收。
            {"tag": "WARP-CF-11", "port": 1090, "service": "usque-warp-11.service"},
            {"tag": "WARP-CF-12", "port": 1091, "service": "usque-warp-12.service"},
            {"tag": "WARP-CF-13", "port": 1092, "service": "usque-warp-13.service"},
            {"tag": "WARP-CF-14", "port": 1093, "service": "usque-warp-14.service"},
            {"tag": "WARP-CF-15", "port": 1094, "service": "usque-warp-15.service"},
            {"tag": "WARP-CF-16", "port": 1095, "service": "usque-warp-16.service"},
            {"tag": "WARP-CF-17", "port": 1096, "service": "usque-warp-17.service"},
            {"tag": "WARP-CF-18", "port": 1097, "service": "usque-warp-18.service"},
            {"tag": "WARP-CF-19", "port": 1098, "service": "usque-warp-19.service"},
            {"tag": "WARP-CF-20", "port": 1099, "service": "usque-warp-20.service"},
        ],
        # 换线触发阈值（两类都按出口 IP 判定，换出口即拿到新配额/新信誉）：
        # 两者都是 1：任何一条出口级拦截出现即换，不再等累计。
        # 曾把 429 阈值设为 5（"429 太频繁，单条就换会让隧道反复重启"），
        # 但实测 22:35~22:38 在 429 风暴里每多等一条就是继续失败：
        # 阈值 5 期间流量全废，而当时的冷却只有 20s（现为 cooldown_ok_sec=5），
        # 换线成本远低于等待成本。
        "block_threshold": 1,
        "rate429_threshold": 1,
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
        "proactive_after_dsv4_ok": 150,
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
        "refresh_model_check": True,
        "refresh_model_timeout_sec": 15,
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
        "proxy_fallback_enabled": False,
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
        # `_cached_warp_standby`），把同步探测移出关键路径。TTL 取 12s：
        # 守护节奏是 5s + 单次探测 1.2~7s，一个完整周期仍有余量。
        "probe_cache_ttl_sec": 12,
        # 模型层探测结论（`_WARP_PROBE_SEEN`）的保留时长，按结论的持久性分两档：
        #   - 持久（quota/flagged）：地址自身的配额/信誉结论。重探同一 /128 有
        #     99.2% 仍 quota（30h 内仅 1 次恢复、间隔 56min），留 6h —— 与生产
        #     403 惩罚期同量级。
        #   - 瞬时（unreachable/err）：探测侧故障，不代表地址状态（日志实测同一
        #     地址 err 后 66s 即 OK）—— 这一档只留 2min。
        #   - OK 单独一档 10min：它说的也是地址状态，而重探间隔中位 212s，套 2min
        #     等于每轮都重探已验过的地址 —— 实测 1980 次「前一次 OK」里 1831 次
        #     （92.5%）结论不变。地址真被烧时生产侧 `_probe_cache_forget` 会立即
        #     作废 OK，不靠这个 TTL 兜底。
        "probe_transient_ttl_sec": 120,
        "probe_ok_ttl_sec": 600,
        "probe_persistent_ttl_sec": 21600,
        # fixed 模式的提前量：换线从发起到达生效 p50=2s / p90=7s，等满
        # proactive_fixed_sec 再发起就赶不上节律，必须提前这个时长。
        # requests 模式不用它：配额是离散格数，没有「生效延迟」可提前。
        "switch_lead_sec": 3.0,
        # 错误换线路径上等目标备线空闲的上限。补刷路径用的是 idle_wait_sec(90s)，
        # 但错误路径上每多等一秒，坏出口就多服务一秒失败请求 —— 实测 40s 上限
        # 把「首错→换线生效」拉到 p90=25s / max=206s。
        "fallback_idle_wait_sec": 3,
        # 每条隧道一个常驻 worker（warp_line_worker.py）：「判断→执行」同循环，
        # 不再经预热/脏线抽检/补刷三个调度器排队。enabled=False 时完全走旧的
        # 三守护路径，行为与之前逐字节一致，便于回滚。
        # dry_run=True 只探出口并记录「本应换线/重启/验收」，不写台账、不动服务。
        "line_worker": {
            "enabled": False,
            "dry_run": True,
            "interval_sec": 30,
        },
    },
    "log_file": _env_path("ASIP_LOG_FILE", os.path.join(PROJECT_DIR, "daemon.log")),
    "log_format": "%Y-%m-%d %H:%M:%S",
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

# 订阅源顺序即合并优先级（okz 主力，其余补充）。值为空表示未配置。
SUBSCRIPTION_KEYS = ("okz", "hd23n", "dasho", "dasho_alt")


def subscription_urls() -> list[str]:
    """Configured subscription URLs, in merge order, empty entries dropped."""
    return [url for url in (CFG.get(k, {}).get("sub_url", "") for k in SUBSCRIPTION_KEYS) if url]

# ============= 启动校验 =============
# requests 模式整个判据就是「数哪个模型的成功调用」。键写错时 daemon 不该
# 带着一条永不触发的判据上线 —— 导入即炸，比线上静默不换线好查得多。
_w = CFG["warp"]
if _w.get("proactive_mode") not in ("fixed", "requests"):
    raise SystemExit(f"✗ warp.proactive_mode 只能是 fixed/requests，实际 {_w.get('proactive_mode')!r}")
if _w["proactive_mode"] == "requests" and not _w.get("proactive_quota_model"):
    raise SystemExit("✗ warp.proactive_quota_model 未配置，requests 模式无法计数")

# ============= provider 常量 =============
# OPENCODE_FREE_PROVIDER: 9router providerStrategies 中的 key = "opencode"
# provider 值在 requestDetails 表中也是 "opencode"
OPENCODE_FREE_PROVIDER = "opencode"
