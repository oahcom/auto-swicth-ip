# 项目文档

## 代理架构

```
Claude Code / AI Client
    ↓ HTTP/HTTPS
9router MITM (port 20128)
    ↓ https.request → proxy-global.js (NODE_OPTIONS --require)
    │
    ├── Layer 1: CONNECT tunnel → sing-box:7890
    │   → ProxyHttpsAgent 替换 https.globalAgent
    │   → tlsTunnel/connectThroughProxy 建 CONNECT 隧道
    │
    ├── Layer 2: 429/5xx/空响应 → 3 次重试 + 切节点
    │   → triggerNodeSwitch() → Clash API PUT /proxies/proxy
    │   → 空 body / SSE 错误 / 200 error-body 都触发
    │   → sleep(500*attempt) 指数退避
    │   → 重试失败返回 502 proxy-global_error
    │
    ├── Layer 3: BYPASS mode
    │   → sing-box 不可达（3 次 CONNECT 失败）
    │   → proxyEnabled=false，直连
    │   → 每 10s health check 恢复
    │
    └── bad-node tracking
        → .bad_nodes.json 共享文件（dict {expire, reason}）
        → daemon.py 独占写入，proxy-global.js 只读
        → _markBadEntry 统一接口（daemon 端写入）
        → per-key 80% 安全阀（nodeTag vs host:port-any 分开统计）
        → isBadNode 过滤 host:port-any 格式

sing-box (port 7890)
    ← Clash API (port 9090, Bearer auth)
    → 56 anytls + 1 trojan 节点池
    → 代理轮换：daemon / proxy-global.js 共用同一套 API

daemon.py
    ← 读 9router SQLite requestDetails（只查 provider="opencode"）
    → 切换后出现任意 1 条新错误 → 立即再切（不等 cooldown_after_switch_sec）
    → 403 与 429 同等触发轮换
    → 主动轮换：每 60s 加权选新节点
    → 自适应策略：normal=延迟优先 / pressure=均匀分散
    → 节点健康检查：每 300s 并发测试（_health_pool, _Dead_TTL_STAGES）
    → sing-box watchdog：重启检测
    → 死节点导出：cleanup_dead_ttl() 写 _DEAD_NODES 到共享文件
    → 时间戳统一：_now_ts() int 秒（与 JS 对齐）
    → 订阅刷新：每 1h 后台执行，清理旧 node_info
    → 节点调用计数重置：每 20min（pressure 模式跳过）
    → 健康检查前从 Clash API 实时刷新节点列表，避免缓存陈旧
    → Sing-box watchdog：重启计数受 _RESTART_LOCK 保护
    → _RESTART_COUNT 受 _RESTART_LOCK 保护，防多线程竞争
    → 线程池优雅关闭：wait=True + 10s timeout
    → SQLite 连接优雅关闭：atexit 关闭 WAL checkpoint
```

## WARP 专用出口（opencode）

```
9router → sing-box:7890 → route: domain_suffix ["opencode.ai"]
        → selector opencode-out
             ├── WARP-CF     socks 127.0.0.1:1080  usque-warp.service   （主, -6 → SJC）
             ├── WARP-CF-B   socks 127.0.0.1:1081  usque-warp-b.service （备, v4 → LAX）
             ├── WARP-CF-C   socks 127.0.0.1:1082  usque-warp-c.service （备, v4 → LAX）
             ├── WARP-CF-D   socks 127.0.0.1:1083  usque-warp-d.service （备, v4）
             ├── WARP-CF-E   socks 127.0.0.1:1084  usque-warp-e.service （备, v4）
             └── proxy       轮换池（隧道全不通时兜底）
        → opencode.ai
```

### 主备换线（daemon 自动）

- 触发（两类出口级拦截，阈值不同）：
  - `abuse403` ≥ `warp.block_threshold`（默认 1）—— IP 信誉拦截，出现即说明当前出口已被标记。
  - `rate429` ≥ `warp.rate429_threshold`（默认 **1**）—— **免费层按 IP 的配额限流**，同样换出口即拿到新配额。
    两者都是 1：任何一条出口级拦截出现即换。曾把 429 设为 5（担心频繁重启），
    但实测 22:35~22:38 风暴期每多等一条就是继续失败，而换线冷却只有 20s。
  - 实测 16:24 切 WARP 成员后成功率 3.5% → 9.3%（90s 窗口），429 换线确有效。
  - 两类同时达阈值只换一次线，理由串成 `"abuse403 ×N + rate429 ×M"`。
- **判定不受节点切换时间戳约束**：主循环的主动轮换（每 60s）会刷新 `_last_successful_switch_ts`，若拿它当窗口起点，爆发期间 403 会被整段吞掉（实测 15:47~15:52 共 252 条 403 零换线）。
- **换线前校验目标出口 IP**：`_WARP_IP_USED` 记录每个出口 IP 最后一次使用时间，窗口
  `ip_reuse_guard_sec`（默认 **3600s**）内用过的地址必须先重启目标隧道换会话 IP 再切 ——
  否则等于从一个被限流的出口切到另一个同样被限流的出口，白换一次（实测 16:53:59
  WARP-CF-B 刷出 `19c0:10::3ff:6d`，16:59:27 又切回同一地址，随之 429 爆发）。
  刷新后仍是旧地址就跳过该备线，所有备线都不新鲜才退回原地重连当前线。
  3600s 依据：同一 IP 被重新切回的实测间隔分布里，25.2 / 28.3 / 49.1 分钟都出现过，
  而 8~11 分钟就切回的那些几乎立刻再次 403（18:58:05 用过的
  `2a09:bac5:1efb:281e::3ff:42` 在 10.6 分钟后的 19:08:41 被原样切回，53 秒后爆发）。
  记录落盘 `.warp_ip_used.json`，跨重启保留。
- 动作：探测备线连通性 → `PUT /proxies/opencode-out` 切过去（`interrupt_exist_connections: false`，在途请求不断）→ 后台重连刚被换下的那条，刷新其会话 IP，下次可原样切回。
- 备线不通时先拉起；所有备线都不可用（或目标 IP 刷新不出新鲜值、备线均忙于在途
  连接）时**切轮换池 `proxy`**，而不是原地重连 —— 切 proxy 只是一次 PUT，秒级且
  不断流；原地重连要 `systemctl restart usque-*`，D-Bus 不可用时必败且流量会干等
  在坏出口上（实测 2026-10-19 19:09 三条线同时失效 → 原地重连全败 → 403-abuse
  每分钟 10~116 条持续 7 分钟）。仅当切 proxy 也失败时才退回原地重连，最多
  `max_attempts`（3）次，重试间隔 `retry_backoff_sec`（2/5/10s）。
- 冷却：`cooldown_ok_sec` 固定 20s，**不随连续失败放大**。WHY：2^n 退避曾在风暴期锁死到
  600s —— 实测 17:02~17:09 九分钟零换线、1752 条请求全废。换线只要目标出口 IP 新鲜
  就值得做，拉长冷却只会让错误持续积累。
- 换线动作本身失败（两条线都不通）：`cooldown_fail_sec` 60s 后重试。
- 换线后 `refresh_delay_sec`（20s）刷新被换下的闲置线；刷新前最多等 `idle_wait_sec`
  （40s）让该线闲下来，等不到则**补刷重试**直到 `ip_refresh_retry_sec`（600s）用完。
  WHY 必须补刷：单次超时就放弃会让这条线永远停在旧地址上 —— 实测 WARP-CF-C 被
  换下 8 次仅刷 2 次，13 次换线里 7 次切回同一个 10 分钟前的地址。
  「闲」的判据是**字节增量**（`warp_line_busy`），不是连接数：活跃线上恒有 7~10
  条连接且从不归零（SSE 长连接 + 连接复用，实测单条存活 936s），按连接数等必然
  超时。强刷会掐断长命连接 → `upstream connection lost`。
- sing-box 重启会把 selector 重置为 default（主线），`restart_singbox()` 会记住并恢复重启前的线路。

**部署约束（勿改）**：sing-box 由 daemon 用 `Popen(start_new_session=True)` 拉起，进程仍属于
`auto-switch-ip.service` 的 cgroup。该 unit 必须保持 `KillMode=process` —— 默认的
`control-group` 会让每次 `systemctl restart auto-switch-ip.service` 连带杀掉 sing-box，
daemon 随即置 bypass 直连，opencode 看到本地 IP 直接返回 abuse 403
（实测 15:31/15:36/15:50 三次重启各触发 50~100 条 403）。

**为什么换线比原地重连好**：Cloudflare 的 WARP 会话地址重连即换，但原地重连有几秒断流；换到备线是秒级且在途请求不断，被换下的线在后台慢慢重连刷新。

**双向兜底（任一炸了，另一个必须察觉并顶上）**

`opencode-out` 成员含 `proxy`（83 节点池），两个方向都是**主动探测**，不等下游报错：

| 方向 | 触发 | 实现 |
|---|---|---|
| WARP → proxy | 全部隧道不可用或出口 IP 已被标记（换线时探测） | `_fallback_to_proxy()`，一次 PUT，不掐断在途连接 |
| proxy → WARP | 三条判据任一成立（每 `proxy_check_sec`(60s) 评估）：①池健康探测**连续** `proxy_fail_streak`(3) 次全失败；②落池后 opencode 仍被 403/429 拦截 ≥ `proxy_blocked_threshold`(5) 条；③黑名单节点占比 ≥ `proxy_dead_ratio`(0.5)。另需 WARP 至少一条能出未标记 IP | `_maybe_failback_to_warp()` |

WHY 反向必须主动探：停在 proxy 上时 `opencode_out_on_warp()` 为 False，错误分支走
`_rotate_on_error()`，只在 83 个节点内部打转 —— 正是 INC-002 的失败模式（全天轮换
成功率 3~5%）。靠下游 429 累计到阈值才动，期间流量全废。

WHY 不按「WARP 探测又通了」回切：探测会抖动，实测 00:02:45 落到 proxy、00:02:52
就因一次探测通过切回（7s），83 节点池一条 opencode 请求都没服务过。代理池是兜底
资源，能服务流量就该让它服务完；只有池真的不堪用（上面三条判据）才回切。
池健康采样**随机 3 个节点并发探测**：固定取前 3 个时，这 3 个恰好挂了就把整池判死。

池健康只采样前 3 个节点：全池逐个探测代价过高，而"能连"与"全死"之间没有需要
精确刻画的中间态 —— 有一个通就还能兜底。注意 INC-002 记录过该池的失败模式，
所以它只作**最后兜底**，不作主力。

- 原理：opencode 免费层的 403 有两种文案，9router 命中的是 "abusive traffic from this IP" —— IP 信誉拦截，换干净出口可解；另一种 "only be used from within OpenCode" 是客户端指纹，换 IP 无效。
- WireGuard 版 WARP 在本机不通（UDP 2408/4500/500/1701 握手全超时），只能用 MASQUE(TCP/443)。
- 配置来源：`.warp/warp-outbound.json`（600，gitignored），单个对象或数组皆可，`make_config()` 自动并入；文件不存在则整条链路不生成，行为回到旧版。
- 设备凭据：`.warp/usque/config.json`（主）、`.warp/usque2/config.json`（备），各自独立注册的 usque 设备（600，gitignored）。
- 五个 WARP 成员都不在 `proxy` 轮换池里，daemon 的节点轮换不影响 opencode 出口。
- opencode 走 WARP 时换轮换池节点没有意义（错误由 WARP 出口决定），daemon 此时改走 WARP 换线路径（`_rotate_on_error` 仅在该路径下调用）。
- 手动回落：`PUT /proxies/opencode-out {"name":"proxy"}`；手动换线：`POST` 同名接口 `{"name":"WARP-CF-B"}`。
- **WARP 开关**：`asip warp stop`（切 proxy + 停全部隧道）/ `start` / `restart` / `status`。
  单元是 `Restart=always`，`systemctl stop` 后 5s 会重生，因此 stop 走「停 + 把单元文件
  换成 /dev/null 软链 + daemon-reload」，start 从 `.asip-bak` 还原（原文件会被覆盖，
  故停之前先备份）。stop 后 daemon 的 `_maybe_recheck_warp` 仍会尝试切回 WARP，
  但隧道不可达时它不会动作。

**出口 IP 污染记录**
`.warp_ip_used.json`（gitignored）：`{ip: {ts: 最后使用时刻, blocked: 是否被 403/429 标记}}`。
落盘而非仅进程内 —— 实测 22:22 重启后旧 IP 全变「干净」，22:27 立即切回已被烧的地址，
403 持续 144s。写入点：换线切过去时（`blocked=False`）和触发换线时把当前出口标
`blocked=True`。`_warp_any_reachable()` 要求至少一条线出**未被标记**的 IP 才算可达；
否则「连通探测全过、403 依旧 100%」—— 实测 22:36:28 切到 `2a09:bac1:19c0:18::1d2:c4`
后仍 403 10/10，同段换两个 IP 才恢复。
- 路由规则必须排在 `action: resolve` 之前，且域名写 `opencode.ai`（`.opencode.ai` 匹配不到裸域）。

## 检测与触发机制

```
proxy-global.js 侧（请求级，低延迟）:
  - 200 空 body → 切节点重试
  - 200 SSE error chunk → 切节点重试
  - 200 SSE 全空 → 切节点重试（_peekSseStream 超时 2s）
  - HTTP 429/500+ → 切节点重试
  - 网络错误 → 切节点重试
  - BYPASS: CONNECT 3 次失败 → 直连 10s 恢复
  - server_error / upstream / connection 等 error 体 → 重试

daemon.py 侧（聚合级，高延迟）:
  - 触发: 上次成功切换后出现任意 1 条新错误即立即再切（不受冷却限制）
  - 403 与 429 一样触发轮换；403 占比仅记录在日志中用于诊断
  - 只查 provider="opencode"（OpenCode Free）
  - 不查 kiro/nvidia/ollama 等（不触发 IP 轮换）
  - 结果: 错误模型名日志（如 "mimo-v2.5-free"）
  - 健康检查前从 Clash API 实时刷新节点列表，避免缓存陈旧
```

## 共享状态

| 资源 | 写入者 | 读取者 | 格式 |
|------|--------|--------|------|
| `.bad_nodes.json` | **daemon.py 独占写入** (dict) | proxy-global.js (只读) | `{"host:port|node-name": {"expire": int, "reason": str}}` |
| `sing-box Clash API` | daemon + proxy-global.js 切换节点 | 双方 | REST `/proxies/proxy` |
| `9router data.sqlite.requestDetails` | 9router | daemon 读 | provider="opencode" |

## 配置（config.py）

```python
OPENCODE_FREE_PROVIDER = "opencode"            # 9router provider key
opencode_min_success_rate: 0.9                 # 错误率 > 10% → 轮换
pressure_error_rate: 0.3                       # 正常→压力模式阈值
normal_error_rate: 0.1                         # 压力→正常模式阈值
proactive_rotate_sec: 60                       # 主动轮换间隔
proxy_fail_streak: 3                           # 池连续探测失败 N 次 → 回切 WARP
proxy_blocked_threshold: 5                     # 落池后仍被拦截 N 条 → 回切 WARP
proxy_dead_ratio: 0.5                          # 黑名单节点占比 ≥N → 回切 WARP
cooldown_after_switch_sec: 60                  # 切换后冷却（主动/错误触发共用）
429_cooldown_sec: 60                           # 429 节点让开时长
reject_window: 50                              # 403 客户端拒绝占比统计窗口
node_health_sec: 300                           # 健康检查间隔 (5min)
node_test_timeout_ms: 15000                    # 节点测试超时
singbox.max_restart_attempts: 3                # 最大重启次数
bad_nodes.ttl_sec: 300                         # bad node TTL (5min)
_DEAD_TTL_STAGES: [60, 120, 300, 600]          # 指数退避 TTL
_POOL_MIN_RATIO: 0.3                          # 允许 70% 节点被拉黑（config.py 同名常量）
```

## proxy-global.js 关键设计（当前迭代）

| 修复 | 说明 |
|------|------|
| `_nodeCount` 显式声明 | `let _nodeCount = 56`，启动时从 Clash API 初始化 |
| `_markBadEntry` 共享函数 | 统一 bad node 标记逻辑，per-key 80% 安全阀 |
| `triggerNodeSwitch` 并发保护 | `_switching` Promise chain，并发调用者等待切换完成 |
| PUT 响应校验 | `putRes.ok` 检查，失败返回 false |
| 重试退避 | `sleep(500*attempt)` 指数退避 |
| URL 校验 | `new URL(proxyUrl)` try/catch fallback |
| `fakeReq.write` guard | `writableEnded` 检查 |
| TOCTOU 修复 | `loadBadNodes` 先 stat 再读，读完二次确认 |
| TTL/路径统一 | 支持 `BAD_NODES_TTL` / `BAD_NODES_FILE_PATH` 环境变量 |
| `tlsTunnel` 记录成功 | CONNECT 成功调 `recordProxyConnectResult(true)` |
| `makeRetryableRequest` 429/5xx | else 分支加 status 检查 + 重试 |
| **server_error 识别** | `_shouldRetryFetch` regex 加 `server_error\|upstream\|connection` |
| **SSE error 同步** | `_isSseChunkError` 同步加 `server_error\|upstream` |
| **锁竞争修复** | `triggerNodeSwitch` 返回 false → 直接返回 502 |
| **`res.headers` scope 修复** | error handler 用 `_headersOnError \|\| {}` 而非 `res.headers` |
| **过度匹配修复** | `_shouldRetryFetch` 只匹配 `error.message` 而非整个对象 |
| **console.log 数组修复** | 去掉数组括号 |
| `_markBadEntry` 只读 | `proxy-global.js` 只读，daemon.py 独占写入 |
| `secret` 权限检查 | `_readSingboxSecret` 加 `stat.mode & 0o077` 警告 |
| `secret` 自动刷新 | mtime 检测自动刷新 |
| `triggerNodeSwitch` 并发等待 | Promise chain，并发调用者等待切换完成 |
| Socket body 消费 | `res.resume()` 防止泄漏 |
| `markBadNode/Singbox` 写 dict | 统一格式 `{expire, reason}` + 清理过期 |
| `loadBadNodes` TOCTOU | 先 stat 命中缓存，读完二次确认 mtime |
| `triggerNodeSwitch` PUT 校验 | `putRes.ok` 检查 |
| `_shouldRetryFetch` regex | 同步追加 `streaming response` |
| SSE `_isSseChunkError` regex | 同步追加 `server_error\|upstream` |
| `res.headers` scope 修复 | error handler 用 `_headersOnError \|\| {}` |
| 过度匹配修复 | 改 `String(error.message)` 仅匹配 message |
| `console.log` 数组修复 | 去掉数组括号 |
| `_markBadEntry` 只读 | `proxy-global.js` 只读，daemon.py 独占写入 |

## daemon.py 关键设计（当前迭代）

| 修复 | 说明 |
|------|------|
| 错误检测只查 opencode | 排除 kiro/nvidia/ollama 干扰 |
| 连续失败检测 | `consecutive_errors ≥ 2` 立即触发轮换 |
| 时间戳统一 | `_now_ts()` int 秒（与 proxy-global.js 对齐）|
| 死节点导出 | `cleanup_dead_ttl()` 导出到 `.bad_nodes.json` |
| 死变量清理 | `_last_switch_country` / `last_switch_ts` 删除 |
| 静默吞异常修复 | `test_node_delay` / `_load_shared_bad_nodes` 加 log |
| 缓存时序修复 | `_shared_bad_check_ts` 移到缓存成功填充后 |
| NULL guard | `json_set(COALESCE(data, '{}'), ...)` |
| 自适应策略 | `_p2c_select` 支持 normal/pressure 双模式 |
| 正常执行无需 sql 注释 | 原本代码中的sql注释是代码注释而不是sql注释 |
| CLI 入口重复 | 清理死代码 `cli_rotate` 重复调用 |
| 优雅关闭 | SIGTERM handler + SystemExit |
| 线程池优雅关闭 | `wait=True` + 10s timeout |
| SQLite 连接关闭 | atexit 关闭 WAL checkpoint |
| 订阅刷新清理旧节点 | 每 10min 清理 `node_info` 过期记录 |
| 健康检查前刷新节点列表 | 直接从 Clash API 刷新，避免缓存陈旧 |
| `_RESTART_COUNT` 线程安全 | `_RESTART_LOCK` 保护多线程读写 |

## 关键文件

| 文件 | 行数 | 说明 |
|------|------|------|
| `~/.9router/proxy-global.js` | ~790 | 三层代理补丁（CONNECT + 重试 + BYPASS）|
| `~/.9router/bootstrap-undici.js` | ~50 | NODE_OPTIONS 入口 |
| `daemon.py` | ~1200 | 节点管理 + 错误检测 + 轮换 |
| `config.py` | ~90 | 共享配置常量 |
| `singbox-config.json` | ~600 | sing-box 代理节点配置 |
| `.bad_nodes.json` | 动态 | 坏节点共享文件 |
| `self_evolve_checkpoint.py` | ~260 | 自进化巡检引擎（每 10min 由 asi-selfcheck.timer 触发，写 .monitor_state.json）|
| `.egress_log.jsonl` | 动态 | 出口节点归属日志（每次成功切换 append (ts,node)，供 per-egress 错误率归因）|
| `asi-selfcheck.timer` | — | systemd 用户定时器：每 10min 触发 asi-selfcheck.service 跑巡检引擎 |
| `test_daemon_unit.py` | ~240 | 确定性单测（无网络，覆盖错误率/策略/评分/坏节点/看门狗）|
| `test_subscription_parse.py` | ~150 | 订阅解析回归单测（无网络，覆盖 vless/trojan transport/单源失败/rc 检查）|
| `verify_rotation.py` | ~230 | 轮换健康验证（7890 端口 / IP 切换 / 切换合理性），`--quick` 约 20s |

## provider 多样性约束（2026-07-08 确认）

**根因**：目前 `9router_hermes` → `op` combo 仅包含 3 个 opencode.ai 免费模型（`oc/deepseek-v4-flash-free`、`oc/nemotron-3-ultra-free`、`oc/mimo-v2.5-free`），全走同一 Public 账户。

**限制**：其他 provider（nvidia、openrouter、pollinations、alicode 等）虽配置了 API key，但有硬配额限制，**无法通过代理 IP 轮换绕过配额**。只有 opencode.ai 的免费接口能通过 sing-box 代理轮换 IP 来绕过限额。

> **更正（2026-10-19 实测）**：上文"换 IP 对配额无效"的说法对 **429 限流不成立**。
> opencode 免费层的 `FreeUsageLimitError`（429 "Rate limit exceeded"）**按出口 IP 判定**，
> 换出口即拿到新配额。实测 16:24 手动切 WARP 成员后成功率 3.5% → 9.3%（90s 窗口）。
> 因此"配额耗尽就无解"的判断被推翻：根本解法是**出口级换线**，不只是多 key。
> 现在 daemon 对 abuse403（阈值 1）与 rate429（阈值 5）都自动换线。
> 只有 abuse403 中"客户端指纹"那一类（"only be used from within OpenCode"）换 IP 真无效。

**结论**：不能简单在 combo 添加其他 provider 作为 fallback，因为一旦 opencode 配额耗尽，fallback 到的 provider 同样会立即触发配额限制。根本解决路径是：
1. 维护 sing-box 节点池健康（确保 IP 轮换有效）
2. 出口级换线已覆盖 abuse403 与 rate429，是目前对免费层限额唯一有效的手段
3. 如需 provider 多样性，必须获取可付费/可续费的 API key（alicode renew、openrouter 付费等）

## 测试工作流要求

**所有代码变更必须经过 Codex CLI 审查流程：**

1. **代码修改后** → 运行 `codex review` 对变更文件进行审查
2. **测试文档更新** → TEST_PLAN.md 同步更新测试点
3. **语法验证** → `node --check proxy-global.js` && `python3 -m py_compile daemon.py`
4. **Codex CLI 审查通过** → 输出 `PASS` 或 `PASS_WITH_NOTES` 才可合并
5. **测试执行** → 按 TEST_PLAN.md 执行 P0/P1 测试点

**禁止**：
- 直接修改代码跳过 Codex CLI 审查
- 直接修改 TEST_PLAN.md 而未同步代码变更
- 跳过语法验证直接提交
