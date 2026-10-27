# 事故记录

## INC-001: 重启 9router 导致服务中断

**日期**: 2026-07-02  
**等级**: P2（部分用户受影响，持续 ~10 分钟）  
**原因**: 误杀进程 + 启动参数错误

### 时间线

| 时间 | 事件 |
|------|------|
| 12:33 | 修改 proxy-global.js SSE streaming 修复 |
| 12:33 | `kill` 掉旧的 next-server 进程，期望 cli.js 自动重启 |
| 12:33 | qrouter cli.js 也随 kill 流程被清理 |
| 12:38 | 手动重启 9router，但未清理旧端口占用 |
| 12:38 | 用 `--no-browser --skip-update` 启动失败（端口被旧进程占用）|
| 12:39 | 改用 `--tray` 模式启动成功 |
| 12:40 | 用户报告 7890 端口也炸了（实际没炸，是测试请求 timeout）|

### 根因

1. **kill 范围过大** — `kill $(pgrep -f "next-server")` 只杀 next-server，期望 cli.js spawner 自动重启。但 cli.js 也可能在 clean up 流程中被连带杀掉。
2. **端口冲突** — 旧进程被杀后端口未立即释放，新进程启动失败
3. **无健康检查** — 重启后未确认服务就绪就继续操作
4. **`--no-browser` vs `--tray`** — cli.js 启动模式区分，`--no-browser` 是 TUI 模式，需 TTY 才能保持；后台进程应用 `--tray` 模式

### 修复措施

1. 停止 9router 必须用：`pkill -f "cli.js"` 或 `pkill -f "next-server"` + 等 cli.js spawner 自动重启
2. 重启后验证：`pgrep -f "next-server"` + `curl ... /_mitm_health`
3. 后台启动必须用：`--tray --skip-update`

### 经验

- 不要直接 `kill` next-server，让 cli.js 管理进程生命周期
- `--tray` 模式适合无终端后台运行，`--no-browser` 需要 TTY
- 每次 deploy 后执行健康检查，确认服务就绪再通知用户

## INC-002: opencode 免费层 403 持续限流，轮换 83 个节点无效

**日期**: 2026-10-18 ~ 2026-10-19  
**等级**: P1（opencode 全链路不可用，成功率 3–5%，持续约一天）  
**原因**: 出口 IP 全在 opencode 的滥用名单里；轮换池本身没有干净 IP

### 现象

- 9router `opencode` provider 持续返回 403，daemon 每 10–60s 切一次节点，切换后成功率仍是 3–5%。
- 近 2000 条 `requestDetails` 中 1931 条 error，文案全部是：
  `FreeTierError: ... detected abusive traffic from this IP address`。

### 根因

1. **两种 403 被混为一谈**。另一条文案 `only be used from within OpenCode` 才是客户端指纹（裸 curl 必现，换 IP 无效）；`abusive traffic from this IP` 是**IP 信誉**拦截，换干净出口可解。之前按"指纹"结论处理，方向错了。
2. **83 个代理节点全部撞名单**。轮换只在同一批脏 IP 里打转，所以切得再快也没有收益。
3. WireGuard 版 WARP 直连不通：UDP 2408/4500/500/1701 握手全部超时（同网段 UDP 19302/3478 正常）。
   **2026-10-28 更正**：UDP 出站本身可用，被拦的是到 Cloudflare 2408 的路径（traceroute 末端
   收到 ICMP type 3 code 13）。把同一条 UDP 经代理节点转发后握手成功，见 `warp_wg.py`。

### 修复

```
9router → sing-box:7890
        → route: domain_suffix ["opencode.ai"]
        → selector opencode-out（默认 WARP-CF，备选 proxy）
        → socks 127.0.0.1:1080（usque MASQUE，systemd usque-warp.service）
        → opencode.ai
```

- WARP 走 MASQUE（TCP/443），`cloudflare.com/cdn-cgi/trace` 验证 `warp=on`。
- `parse_subscription.py:make_config()` 读取 `.warp/warp-outbound.json`（600，gitignored）自动并入；文件缺失时整条链路不生成。
- `opencode-out` 不在 `proxy` 轮换池内，daemon 的轮换不再影响 opencode 出口。

### 时间线

| 时间 | 事件 |
|------|------|
| 10-18 全天 | 403 持续，轮换频率 10s/次，成功率无改善 |
| 10-19 14:29 | 提高冷却 + 错误即时触发切换，仍 403 |
| 10-19 15:00 | 从 DB 取到 403 原文，确认是 IP 信誉而非客户端指纹 |
| 10-19 15:02 | WireGuard 版 WARP 多 endpoint/端口握手失败 |
| 10-19 15:01 | 注册 usque MASQUE 账户，`warp=on` 验证通过 |
| 10-19 15:07 | 路由生效：`outbound/socks[WARP-CF] → opencode.ai:443` |
| 10-19 15:08~15:11 | 连续 9 次端到端请求全部 200，daemon `错误=0/10` |

### 经验

- **先取原文再下结论**：错误体里的 message 才是判据，按类型名（FreeTierError）归类会漏掉同一类型下的不同拦截原因。
- 路由规则两条硬约束：
  1. `domain_suffix` 写 `opencode.ai`，写 `.opencode.ai` 匹配不到裸域；
  2. 出站规则必须排在 `action: resolve` 之前，resolve 后 destination 变成 IP，domain 规则不再命中。
- 验证链路要看 sing-box 日志的出站标签，不能只看 HTTP 状态——403 的两种文案在状态码上完全一样。

### 遗留

- WARP 是单一出口 IP，被标记后无自动替换；回落命令 `PUT /proxies/opencode-out {"name":"proxy"}`。
- 15:10 订阅刷新出现 `刷新节点数骤降 (19 → 1)`，护栏拒绝覆盖（配置完好），但 `19` 与当时 83 个节点对不上，原因未查明。

## INC-003: WARP 单一出口被标记后无替换机制

**现象**：opencode 走 WARP 后接口恢复，但 WARP 出口是单一的。一旦这个会话地址被
Cloudflare 判定为 abusive，`opencode-out` 无处可换——只能手动重启 usque 等新地址，
期间 opencode 断流。

**根因**：出口只有一个 usque 实例（`127.0.0.1:1080`），`opencode-out` 的备选只有轮换池
`proxy`，而轮换池全在脏 IP 上（见 INC-002）。没有第二条干净的 WARP 通道可切。

**修复**（2026-10-19）：
1. 用 `usque register` 注册第二个独立设备，落在 `.warp/usque2/`，监听 `127.0.0.1:1081`，
   由 `usque-warp-b.service` 托管。
2. `.warp/warp-outbound.json` 从单对象改为数组（主 + 备），`make_config()` 据此生成含两个
   WARP 成员的 `opencode-out` selector：`[WARP-CF, WARP-CF-B, proxy]`。
3. daemon 新增换线路径：abuse 403 达阈值 → 探测备线 → `PUT /proxies/opencode-out` 切过去
   （`interrupt_exist_connections: false`，在途请求不断）→ 后台重连被换下的线刷新会话 IP，
   下次可原样切回。
4. 备线不可用时退回原地重连当前线（有几秒断流），受 `max_attempts` 与指数退避限制。
5. sing-box 重启会把 selector 重置为 default，`restart_singbox()` 记住并恢复重启前线路。

**验证**（15:32–15:34 实测）：
- 手动：`换线前 WARP-CF 出口=2a09:bac5:1ef8:372d::57f:92` → `✓ 换线成功: WARP-CF -> WARP-CF-B`
  → `↻ WARP-CF 会话已刷新`。
- 自动：`15:33:01 ↻ WARP 换线 (abusive 403 ×49)` → `15:33:08 ✓ 换线成功: WARP-CF-B -> WARP-CF
  (出口 2a09:bac1:19c0:10::4e8:25)`；随后 `requestDetails` 最近 30 条 success=20/error=10。
- 两端口出口互不相同：`1080=2a09:bac1:19c0:10::4e8:25`、`1081=2a09:bac5:1efa:1173::1bd:e7`。

**遗留**：自动换线时 `abusive 403 ×49` 的计数包含了 daemon 启动前的历史错误（窗口起点为 0），
已在启动时把窗口起点初始化为当前时刻；该修正在重启后生效，尚未在长跑中复核。

**经验**：换线优于原地重连——Cloudflare 的 WARP 会话地址重连即换，但原地重连有几秒断流，
换到备线是秒级且在途请求不断，被换下的线可在后台慢慢重连。

## INC-004: 换线逻辑形同虚设 —— 被节点切换时间戳门控吞掉 + daemon 重启连带杀 sing-box

**现象**：用户连续报告 opencode 返回 abuse 403。实测 15:47~15:52 期间 `requestDetails`
累计 252 条 abuse 403（单次爆发 15:50:30~50 共 100 条），但 `daemon.log` 里**一条换线日志都没有**。

**根因（两个，独立叠加）**：

1. **换线被错误门控吞掉**。主循环里"主动轮换"（步骤 3，每 60s）排在"错误触发"（3c）之前，
   主动轮换成功调用 `switch_proxy()` 会把 `_last_successful_switch_ts` 刷到当前时刻，
   而 3c 的判定 `errors_after_switch >= 1` 正是以该时间戳为窗口起点
   —— 每次轮换都把窗口推成 0 错误，爆发期间的 403 全部落在窗口外。
   probe 验证：`opencode_out_on_warp()=True`、`count_warp_blocked_since(0)=50`、
   `block_threshold=1`，条件全满足，但主循环从未进入换线分支。

2. **daemon 重启连带杀死 sing-box**。`restart_singbox()` 用 `Popen(start_new_session=True)`
   拉起 sing-box，但进程仍留在 `auto-switch-ip.service` 的 cgroup 内；该 unit 是默认的
   `KillMode=control-group`，于是每次 `systemctl restart auto-switch-ip.service` 都会连带
   杀掉 sing-box → daemon 启动时置 bypass 直连 → opencode 看到本地真实 IP → 直接 403。
   实测 252 条 abuse 403 中 **125 条（50%）**落在 sing-box 重启后 30s 窗口内
   （15:31/15:36/15:50 三次重启各触发 50~100 条）。前几轮"重启 daemon 加载新代码"的操作
   本身就是 403 爆发的直接推手。

**修复**（2026-10-19）：
1. 3c 拆分：`opencode_out_on_warp()` 为真时直接走 `maybe_reconnect_warp()`，不再读
   `_last_successful_switch_ts`；该门控只保留给轮换池路径（那条路径下它仍有意义）。
2. `auto-switch-ip.service` 增加 `KillMode=process`，daemon 重启不再连带杀 sing-box。
3. `warp.block_threshold` 3 → 1；`cooldown_ok_sec` 300 → 20 并新增 `cooldown_max_sec=600`
   指数退避（换线后仍被拦说明新出口也被标，冷却 20→40→80…→600，干净窗口归零）。
4. 换线后同步刷新被换下的线（原为另起线程，worker 会先释放 `_WARP_LOCK`，
   下次换线可能在刷新途中切回这条刚重启的线）。
5. `_warp_reconnect_worker` 不再用 `cooldown_ok_sec` 覆写调度处算好的退避值。

**验证**：
- 回归单测 `test_warp_failover_not_gated_by_node_switch`（T-129）、
  `test_daemon_restart_keeps_singbox`→T-130 用 PID 比对实测：
  `重启前 sing-box PID=2395312 → 重启后 PID=2395312`（PASS）。
- `test_warp_escalates_cooldown_when_new_line_also_blocked`：20→40→80，上限 600（PASS）。

**遗留**：根治方案是让 systemd 直接托管 sing-box（独立 unit），使 daemon 重启与
sing-box 生命周期完全解耦；当前 `KillMode=process` 只是把影响面收窄到"sing-box 不再被连带杀死"，
daemon 自身的重启仍会短暂中断看护。另：剩余 50% 的 403 与重启无关，属于出口 IP 本身被标，
由换线机制处理。

**经验**：复用同一个"上次成功切换"时间戳给两条语义不同的路径做窗口起点，是会静默失效的耦合。
轮换池的窗口起点（节点切换）与隧道换线的窗口起点（隧道换线）必须是两个独立变量。

---

## INC-005 — 把 429 限流误判为"换 IP 无效"（2026-10-19）

**症状**：用户报告 opencode 持续 429 `FreeUsageLimitError: Rate limit exceeded`，
daemon 日志每 2 秒一条 `[OpenCode Free] N/10 请求错误`，`403占比=0%`。

**误判**：我依据 CLAUDE.md「provider 多样性约束」下"无法通过代理 IP 轮换绕过配额"一句，
回答"429 是账号级限额，换 IP 无效"。用户指出这是错的。

**实测证据（推翻旧结论）**：
手动 `PUT /proxies/opencode-out {"name":"WARP-CF-B"}` 切换 WARP 成员后，逐分钟统计：

| 窗口 | 请求 | 成功 | 成功率 |
|------|------|------|--------|
| 切换前 90s | 451 | 16 | 3.5% |
| 切换后 90s | 259 | 24 | 9.3% |

429 按出口 IP 计配额，换出口即拿到新配额。

**根因**：`count_warp_blocked_since()` 只匹配 `"abusive traffic"` 一个字符串，
429 完全不进入换线触发条件 —— 换线逻辑对占比 100% 的错误类型视而不见。

**修复**：
1. 计数字符串改为分类型返回 `{"abuse403": n, "rate429": m}`；
2. `maybe_reconnect_warp()` 两类各自比阈值（403=1，429=5），同时达阈值只换一次线；
3. 更正 CLAUDE.md「provider 多样性约束」中被证伪的那句结论。

**经验**：错误文案是判断"换出口有无意义"的唯一依据，不能靠"账号级/ IP 级"的先验猜测。
两类文案（abusive traffic / Rate limit exceeded）都必须实测过才有资格下结论。

## INC-006 — WARP 隧道失效致 9router 直连，出口 IP 被 opencode 判为滥用（2026-10-19）

**日期**: 2026-10-19
**等级**: P0（19:09~19:17 约 9 分钟 94~100% 请求失败）
**用户观感**: "全崩了" —— 所有 CCS 角色一起报错

### 因果方向（先立此节）

**代理链路先坏，直连是被动结果**，不是故障传播的一环。用户纠正原话：
"是因为本身出问题了我才直连的"。

秒级证据（DB `provider='opencode'`，`timestamp` 为 UTC 字符串，+8 得 CST）：

```
19:08:41  ✓ WARP 换线成功: WARP-CF-B -> WARP-CF-C (出口 2a09:bac5:1efb:281e::3ff:42)
19:09:05  最后一条 429（配额类，正常流量）
19:09:10  最后一条 OK
19:09:12  ↻ WARP 换线 (rate429 ×6)：当前 WARP-CF-C 出口=未知   ← selector 所在线已探不到出口
19:09:13  ↻ 备线 WARP-CF 不通，先拉起
19:09:14  ✗ 备线 WARP-CF 探测失败，跳过
19:09:14  ★ 第一条 403-abuse（此后每分钟 10~116 条）
19:09:14  ✗ 备线 WARP-CF-B 探测失败，跳过
19:09:16  ✗ Clash API 连接失败: http://127.0.0.1:9090/proxies/proxy
19:09:16  ✗ 重启 usque-warp-b.service 失败: Transport endpoint is not connected
19:09:16  daemon 收到 SIGTERM
```

403 首现于 19:09:14，session 停服在 19:09:16 —— **403 早 2 秒**。
若因果是"停服 → 直连 → 403"，403 不可能早于停服出现。

实际链条：**WARP-CF-C（selector 所在线）在 19:09:12 前失效 → sing-box 出口断
→ proxy-global.js CONNECT 重试失败 → BYPASS 直连 → opencode 看到本机公网 IP
→ 403 `abusive traffic from this IP`**。2 秒间隔与 CONNECT 重试节奏吻合。

### 现象

逐分钟错误类型（`data` 字段归类，provider=opencode）：

| CST 分钟 | 403-abuse | 429-usage | OK | 状态 |
|---------|-----------|-----------|-----|------|
| 19:07 | 0 | 7 | 41 | 正常 |
| 19:08 | 0 | 7 | 25 | 正常（1 条 upstream-lost） |
| **19:09** | **116** | 4 | 7 | **直连开始，403 突发** |
| 19:10~19:15 | 157 | 0 | 2 | 全程直连，无恢复 |
| 19:16 | 41 | **328** | 0 | 新会话拉起，本机 IP 配额耗尽 |
| 19:17 | 1 | 125 | 34 | 代理恢复中 |
| 19:18~19:24 | 0 | 39 | 190 | 恢复 |

### 根因

**主因：WARP-CF-C 失效，且同期无备线可切**

19:09:12 daemon 连自己的当前线出口都探不到（`出口=未知`），19:09:13~14 两条备线
同样探测失败。三条隧道同时失效，换线无路可走。

失效形态不同（`singbox.log`）：

- **1080（WARP-CF）：`dial tcp 127.0.0.1:1080: i/o timeout` 共 79 次**。
  i/o timeout 而非 connection refused —— 端口存在、TCP 握手被接受，但应用层
  不响应，是**进程僵死态**，不是进程退出。
- **1081（WARP-CF-B）：`EOF`（连接建立后被对端关闭）**。
- 1082：3 次 `connection refused`（进程确已不在）。

**为何三条同时失效 —— 仍未查明。** 可能方向有三，均无直接证据：
usque 的 MASQUE 服务端（Cloudflare）侧波动、WSL 网络栈异常、或内存压力下的
调度饥饿。18:55~19:22 内核日志完全空白（WSL 内核日志已轮转丢失），
关键窗口无内核侧证据。会后补查：`dmesg -T` 全量仅 17:58:23 一次
`ccs@consumer.service` cgroup OOM（pid 21165 claude，total-vm 5908384kB），
与 19:09 无时间关联；`/var/log/syslog` 19:0x 段无网络事件。

**18:59:06~19:08:01 的 9 分钟换线静默 —— 已定位，不是故障。**
该窗口内 WARP 未触发换线的原因是 403/429 计数未达阈值：18:59:06 换到
WARP-CF 后 `403占比=0%`，19:00~19:07 每分钟错误 0~2 条且全是 429，
低于 `rate429_threshold=5`。19:08:00 累积到 4/10、19:08:01 达 5 条，
随即触发 `↻ WARP 换线 (rate429 ×5)`。守卫行为正确。

**根因 1（决定性）：被换下的线从不刷新，导致切回陈旧地址**

这一条在事后量化时才定位到，它是本次事故最直接的可修复原因。

`daemon.py _schedule_idle_refresh()` 的 `_run()` 在等 idle 超时后**直接 return**，
不重试、不排队：

```python
if not _wait_warp_idle(member):
    log(f"  ⚠ {member['tag']} {_warp()['idle_wait_sec']}s 内仍有连接，推迟刷新")
    return                      # ← 放弃，此线永远停在旧地址
```

量化后果（daemon.log 全量统计）：

| 线 | 换线次数 | 刷新成功次数 | 唯一出口 IP |
|---|---|---|---|
| WARP-CF | 38 | 30 | 32 |
| WARP-CF-B | 37 | 29 | 31 |
| **WARP-CF-C** | **13** | **2** | **6** |

C 线被换下 8 次，7 次直接放弃。「推迟刷新」在 daemon.log 全量 42 次，
其中 C 线 8 次。C 线 13 次换线里 **7 次切回同一个地址**
`2a09:bac5:1efb:281e::3ff:42`：

```
18:07:25  2a09:bac5:1efb:281e::3ff:42   ✗ 用了陈旧地址
18:13:57  2a09:bac5:1efb:281e::3ff:42   ✗ 用了陈旧地址
18:45:31  2a09:bac5:1efb:281e::3ff:42   ✗ 用了陈旧地址
18:51:41  2a09:bac5:1efb:281e::3ff:42   ✗ 用了陈旧地址
18:58:05  2a09:bac5:1efb:281e::3ff:42   ✗ 用了陈旧地址
19:08:41  2a09:bac5:1efb:281e::3ff:42   ✗ 用了陈旧地址   ← 53s 后爆发 403
```

**19:08:41 切回后 53 秒（19:09:14）出现本轮第一条 403-abuse。**

**根因 2：判「闲」的判据本身不成立**

上一条的补刷之所以必要，是因为 `_wait_warp_idle()` 原本比较**连接数是否为 0**。
90 秒连续采样证明这个前提在活跃线上永不成立：

```
20:29:27  WARP-CF=8 WARP-CF-C=1   总=9
20:29:37  WARP-CF=7 WARP-CF-C=1   总=8
20:29:47  WARP-CF=7 WARP-CF-C=1   总=8
...
20:30:57  WARP-CF=7 WARP-CF-C=1   总=8
```

活跃线上恒有 7~10 条连接。单条连接实测存活 936s / 748s（上传 16.8MB），
属 SSE 长连接 + 9router 连接复用。**按连接数等下去必然超时**，所以根因 1 的
放弃分支是必然触发的，不是偶发。

**根因 3：复用守卫窗口（300s）短于实测复用间隔**

同一出口 IP 的相邻使用间隔实测：6.2 / 6.4 / **10.6** / 11.6 / 17.1 / 50.1 分钟，
全部 > 300s。守卫判「已出窗口」放行，19:08:41 那次因此只用 7 秒就完成切换
（19:08:34 → 19:08:41），日志中**没有** `在 300s 内用过，先刷新会话 IP`。

原文档此处写「它重复触发的是 rate429（不是 403）」——**这个说法与逐条日志不符**。
19:08:41 切回 `…::3ff:42` 后 53 秒出现的正是 **403-abuse**（19:09:14 首现），
不是 rate429。该地址被复用时触发的就是信誉拦截。

**加剧因素 2：user@1000.service 在故障中途整体退出**

`journalctl -u user@1000.service` 原始序列：

```
19:09:16  Stopping auto-switch-ip.service ...  (全部 30+ 用户服务一起停)
19:11:18  Finished systemd-exit.service - Exit the Session
19:11:18  user@1000.service: Deactivated successfully
          (Consumed 8h 14min CPU, 8.0G memory peak, 2.1G swap peak)
19:15:00  systemd[1]: Starting user@1000.service  ← 新 manager
```

判据：`Exit the Session` 是 `systemd-exit.service` 的
`SuccessAction=exit-force` 触发，属**会话正常终结**，不是 OOM。用户
`Linger=yes`，但 linger 只保证"无登录会话时 manager 不被销毁"，
**挡不住 exit-force**。

后果：19:09:16~19:15:00 期间用户级 D-Bus 不存在，daemon 连重启隧道的能力
都丧失 —— 19:13:03 起日志为：

```
✗ 重启 usque-warp-b.service 失败: Failed to connect to bus: Connection refused
```

即：**首家失效（隧道）→ 直连 → 403 已经发生；6 分钟后 manager 退出又切断了
唯一的自救路径（重启隧道）**。两层故障叠加，形成 19:09~19:15 的连续 100% 失败。
manager 退出**不是** 403 的起因，是**放大器**。

**同期存在但非本因**：WSL swap 100% 满（4096/4096 MB）；
`ccs@consumer.service` 在 19:24 被 cgroup OOM 杀（`MemoryMax=2G`，
`constraint=CONSTRAINT_MEMCG`）；内核 OOM 日志首现于 17:58:23。

**双 daemon 实例并存**：19:11:42 有人从终端手动起 daemon（PID 2908280），
19:15:00 systemd 起权威实例（PID 3149171）。并存至 19:19，违反架构红线
"Daemon 必须 PID 锁单实例"。已收敛。

### 修复

1. 杀掉重复 daemon（PID 2908280），收敛为 systemd 单实例。
2. `daemon.py reconnect_warp()` 同步换线路径补 idle 守卫 —— `_wait_warp_idle()`
   返回值原本被丢弃，40s 超时后照样强刷目标隧道，掐断在途连接致 9router 报
   `upstream connection lost`。现改为：有在途连接先等 → 等不到改刷闲置备线 →
   都没有则切轮换池 `proxy` 兜底（见下条）。
3. **判闲判据改为字节增量**：`warp_active_conns()`（连接数）→ `warp_line_busy()`，
   比较两次 Clash `/connections` 快照的 upload+download 增量：增长=忙、
   不变/回落=闲、查询失败=None（保守当忙）。活跃线连接数从不归零，旧判据必然超时。
4. **延迟刷新改为补刷重试**：`_schedule_idle_refresh()` 的 `run()` 单次超时不再
   `return`，改为 `_retry_refresh_idle_warp()`，重试到 `ip_refresh_retry_sec`
   （600s）用完。这是根因 1 的正解。
5. **复用窗口 300 → 900s**：覆盖实测最长复用间隔 10.6 分钟。
6. **三条隧道全不可用时切轮换池 `proxy`**（`_fallback_to_proxy()`）：新增路径。
   切 selector 只是一次 PUT，秒级且不掐断在途连接；原地重连要
   `systemctl restart usque-*`，D-Bus 不可用时必败（本次事故正是如此）。
   停在 proxy 后，下次换线时 `active_warp_tag()` 返回 None，`reconnect_warp`
   把全部 WARP 成员作为候选自动切回。
7. 澄清 `upstream connection lost` 不是 error 状态：它是 9router SSE **末帧注入值**
   （`~/.9router/.next/server/chunks/8895.js` 中 `m` 的初始值），对应 DB 记录
   `status=success`，按 status 过滤永远查不到。

### INC-006 后续发现（codex review 2026-10-19 21:47）

**DNS 规则域名形式错误：专用 DNS 与本地 resolve 双双落空**

`parse_subscription.py` 生成配置时，`dns.rules` 与 `route` 的 `resolve` 动作都写
`domain_suffix: [".opencode.ai"]`，而 `route` 的出站规则写 `["opencode.ai"]`。

`.opencode.ai` 是 domain_suffix 的「以 `.opencode.ai` 结尾」形式，**匹配不到裸域**
`opencode.ai`。CLAUDE.md 与 INCIDENTS.md 都明确记载了这条约束（"写 `.opencode.ai`
匹配不到裸域"），但生成器自己违反了它。

实测流量形态：`singbox.log` 中 `opencode.ai:443` 出现 **12086 次**，
`api.opencode.ai:443` 仅 2 次。即 **99.98% 是裸域**，写错形式意味着这两条规则
实际只覆盖了极少数流量，裸域回落到 `dns-default`（`local` 类型 → 系统解析器
`/etc/resolv.conf` 的 8.8.8.8）。

设计意图（见 `parse_subscription.py` 注释）是「WSL2 下 8.8.8.8 不可达，走阿里 DoH
本地解析后把 IP 交给节点」。域名写错使这个意图**完全失效**，opencode 流量实际依赖
系统解析器 —— 一旦 8.8.8.8 真的不可达（WSL2 的已知情形），解析会失败。

**修复**：`parse_subscription.py` 两处域名改为 `["opencode.ai", ".opencode.ai"]`
（同时覆盖裸域与子域），与出站规则保持同一写法。验证：

```
dns.rules : [{"domain_suffix": ["opencode.ai", ".opencode.ai"], "server": "dns-opencode"}]
route     : [{"domain_suffix": ["opencode.ai"], "outbound": "opencode-out"},
             {"action": "resolve", "domain_suffix": ["opencode.ai", ".opencode.ai"]}]
```

**生效时机**：无需手动干预。daemon 的订阅刷新（每 3600s）会调
`parse_subscription.py` 重新生成配置，`sing-box check` 通过后 `os.replace` 覆盖并
`restart_singbox()`。

**未验证**：当前 8.8.8.8 实测可达（`/dev/tcp/8.8.8.8/53` 成功），所以这个 bug
**没有造成实际故障** —— 它是个**潜伏缺陷**：一旦 WSL2 网络栈回到「8.8.8.8 不可达」
状态，opencode 解析会直接失败。同理未验证的是：修复后阿里 DoH 路径是否真能替代
系统解析器。

### 未修复（需人工决策）

- **三条隧道为何同时失效未查明**，这是本次的真正起点。内核日志缺失使得
  WSL 侧因素无法排除。可考虑加 usque 存活探针（进程在但端口不响应 = 僵死）
  以区分"进程死"与"进程僵死"，后者当前只有抓包或 i/o timeout 才看得出。
- **swap 100% 未缓解**。28 个 claude 进程合计 6.0 GB RSS，另有 GPTSoVits
  （1.5 GB）、node（1.5 GB）。
- `earlyoom` 的 `--avoid hermes|mimo|claude|python3|node|...` 把最大内存消费者
  全部排除，压力只能由内核 OOM 处理，OOM 命中 `ccs@consumer`（`MemoryMax=2G`），
  systemd `Restart=on-failure` 再拉起，形成消耗循环。属编排决策，本文件不擅改。
- **`Linger=yes` 挡不住 session exit** —— 若需用户级服务在会话终结后存活，
  需另寻机制（本项目未采用，仅记录）。
- 停服窗口内的人工启动应与 systemd 协调（`systemctl --user restart`），
  不要裸跑 `python3 daemon.py`。

### 经验

- **先定因果方向，再查环节**。本次我最初把"直连"写成故障链的一环，方向错了：
  直连是代理失效的**结果**。判据很简单 —— 比时间戳：结果不可能早于原因。
- 用户级服务"同时全崩"查 `journalctl -u user@1000.service` 的
  `Exit the Session` / `ActiveExitTimestamp`：本次 manager 确实重启了，
  但它晚于首家故障 2 秒，只是放大器不是起因。**伴随现象不等于因果。**
- 区分 `Exit the Session`（会话结束）与 OOM（内存不足）：处置完全不同。
- `i/o timeout` 与 `connection refused` 意义不同：前者是僵死（端口活着，
  应用不响应），后者是进程不在。本次 1080 是前者，1082 是后者。
- DB 里查错误要同时看 `status` 和 `data`：部分语义错误（SSE 末帧注入）落在
  `status=success` 的行上。
- `timeout` 缺失时 `awk`/`journalctl` 可静默返回空，看似"无事件"实为查询本身
  失败。关键窗口的"日志空白"要先确认日志系统本身是否健康（本次 user journal
  最早条目仅到 19:29:55，之前的已丢失）。

## INC-007 — 落池兜底把流量交给全挂的付费池（2026-10-26）

**日期**: 2026-10-26
**等级**: P1（WARP 全不可用时落池，池内节点全部 connection reset → 断流）
**现象**: 02:57:13 daemon 日志 `出口=PROXY:pq.aws67.yydjc.top:443-vl`，
`.egress_log.jsonl` 最后一条 ts=1792954624 停在该节点；05:32 部署重启后 selector
回到 WARP，但 `proxy` 仍在 `opencode-out` 成员里，随时可能再落池。

### 根因

1. 付费 anytls 节点（`*.juhazf.cn` / `pq.aws67.yydjc.top`）全部 connection reset，
   落池后没有可用出口。
2. `_fallback_to_proxy` 的设计前提是「池比原地重连可靠」（2026-10-19 INC-006 的
   反向教训），池整体失效后该前提不成立。
3. 代码没有关闭开关，`deploy/install.sh` 每次部署 `enable --now`，看起来像
   「自动切换被切断」，实际一直在跑。

### 修复

- `config.py` 新增 `warp.proxy_fallback_enabled: False`。
- `_fallback_to_proxy` 入口守卫：禁用时只 `_restart_warp_inplace` 当前线；
  `restart_singbox` 恢复路径也不再切回 proxy。
- 保留 WARP 轮换（当前 9router 200 靠它换到未烧出口）。
- 验证：`pytest -q` 351 passed；daemon 06:02:39 重启（PID 844647），
  selector WARP-CF-C，`/v1/chat/completions` 200。

### 教训

- 兜底路径也要有「兜底资源是否还活着」的开关；共享池全挂时，原地重连优于落池。
- 「服务被停用」要看 journal 的 `Stopped/Started` 和 `is-enabled`，不要从日志
  空窗推断。

## INC-008 — 禁令：429 不得再解释为"账号级配额"（2026-10-26）

**日期**: 2026-10-26
**等级**: P0（认知红线，反复违反）
**现象**: 主人已无数次强调，模型仍在分析/方案中把 opencode 429
（`FreeUsageLimitError` / `Rate limit exceeded`）解释为账号级/账户级/key 级
配额上限，并据此提出"全池冷却""暂停轮换""等配额重置""短封禁等待退出"等
错误方案。

### 红线条文

1. **禁止**把 opencode 429 判定为账号级/账户级/key 级配额。
2. **禁止**提出依赖该前提的衍生策略：全池冷却、暂停轮换、等待配额重置、
   短封禁等待退出。
3. 429 的唯一判定框架：**按出口 IP（egress）判定**。治理手段只有两个：
   出口级换线 + IP 池治理（池规模、-6 隧道扩容、出口质量）。
4. 依据：INC-005 实测——同一 key、仅切换出口，成功率 3.5% → 9.3%；
   若为账号级配额，换 IP 不可能提升成功率。主人 2026-10-26 原话：
   "账号级配额是错误的，不允许再次提前（出现）。"
   对短封禁等待退出的否决："提示非常小，大不了下次刷到再用。"

### 教训

- 错误文案分析要先"看证据"（响应体类型、status、出口台账），再谈配额语义。
- 涉及 429 的任何方案，先过本条红线的 4 条禁令，再动笔。

## INC-009 — 60 秒"挂起错误"实为传输层合成 503（sidecar 文案误导，2026-10-26）

**日期**: 2026-10-26
**等级**: P2（可观测性/文案误导；触发窗口内 300 条 error 一次性灌入）
**现象**: 21:07–21:08 一批 opencode 请求记录 `status=error`、`ttft=0`、
`total≈60004`、`tokens=0`，早期分类器把这类行标成 "other"，
被误读成"60 秒超时"。实际响应体为：
`503 {"type":"error","error":{"type":"UpstreamTransportError",
"message":"OpenCode free lane requires the Bun transport; sidecar unavailable."}}`

### 根因链（代码 + journal 证据）

1. 21:04:30–32 全部 20 条 usque-warp 隧道被一次性重启（本轮 -6 改单的批量
   重启窗口），所有出口同时断线重连。
2. 窗口内 Bun sidecar 的 forward 请求上游零字节返回（`ttft=0`）。
3. transport 自身无请求超时：`readyTimeoutMs`=8s、`failureBackoffMs`=5min、
   `STREAM_STALL_TIMEOUT_MS`=360s，均非此 60s（open-sse/config/runtimeConfig.js:71-81）。
4. 约 60s 无首字节，发起方中止 → `forward()` AbortError →
   `fetchViaTransport` 返回 null（open-sse/utils/opencodeTransport.js:179-186）。
5. executor 将 null 统一合成为上述 503（open-sse/executors/opencode.js:660-682，
   带 `retry-after: 1`）。**"sidecar unavailable" 同时覆盖了"forward 卡死"，
   文案误导。**
6. 并发请求同一窗口卡住、同时到 60s 被掐、同时记错 → 短时 error 爆发。

### 定性（与 429 红线的关系）

- 非 429/403，非账号级配额，亦非出口封禁：上游零字节，纯传输悬挂。
- ASIP 未换线是正确行为：503 不落 egress 标记；换 IP 救不了本地传输卡死
  （opencode.js:654-658 `computeRetryDelay` 明确禁止进程内重试）。

### 处置与待办

- 批量隧道重启必须滚动/错峰，禁止把 20 条线同时停掉。
- `[OpenCodeTransport]` 的 warn 只写 9router stdout（fd 为 socket，不落盘）
  → 需要落盘日志/告警，否则同类故障不可观测。
- 报错文案建议拆分 "forward hang" 与 "sidecar unavailable"（改动需 Codex 复审）。


## INC-010 — 传输 503 真因：换线后僵尸 CONNECT + 9router 生产构建未重建（2026-10-26）

**日期**: 2026-10-26
**等级**: P1（21:51–22:12 期间 opencode free 100% 失败，每条挂满 60s）
**现象**: 21:51 起 `transport503` 连续出现，`ttft=0`、`total≈60-65s`、
`tokens=0`；重启 9router 后立刻恢复 37/39。

### 真因链（证据）

1. 9router sidecar（bun pid 215307）自 21:07 起持有 3 条到 `opencode.ai` 的
   HTTP CONNECT 长连接（CF-C 出口，累计 up 54.8/38.6/29.4MB）。
2. 21:46:13 ASIP 把出口 WARP-CF-C 换到 WARP-CF-16；这些连接每个请求仍走
   `chains[0]=WARP-CF-C` 的僵尸隧道——本地 socket 仍 ESTAB，但黑洞无 RST。
3. 同一时刻经 7890 的新连接 1.0–6.3s 就拿到 429 FreeUsageLimitError，
   证明**出口是好的**，坏的是被复用的旧连接。
4. 旧构建无请求超时（readyTimeout=8s / stall=360s 都不是 60s），故每条复用
   请求挂满调用方 60s 上限 → 合成 503。
5. 另发现: `npm run start` 跑的是 `.next` 生产构建（06:08），而 21:28/22:28
   的 JS 改动从未 build；`.next` 内搜不到 `OPENCODE_TRANSPORT_HEADER_TIMEOUT_MS`。
   即「20s 超时 + alternate 换线」此前只在源码/测试里，线上未生效。

### 处置

- ASIP：换线成功后异步清理旧链 `age>=120s` 的 opencode 连接；传输 503 且出口
  探测正常时，清理当前链 `age>=300s` 的连接（10 分钟节流）。实测 23:31:15
  换线后输出「✓ 清理 6 条旧链连接（WARP-CF，≥120s）」。
- 9router：`headerTimeoutMs=20s`（`OPENCODE_TRANSPORT_HEADER_TIMEOUT_MS`）、
  503+UpstreamTransportError 换 alternate 重试一次；`oc/deepseek-v4.1-flash-free`
  上限 330k。**必须 `npm run build` 后重启才生效**。
- 构建离线化：`next/font/google` 改为 `next/font/local`（自托管 Inter latin），
  否则本机出口到 fonts.googleapis.com 不可达，`next build` 直接失败。

### 教训

- 重启「止血」不等于修复上线：先确认运行产物（`.next` BUILD_ID/mtime）是否
  包含改动，再看源码。
- 换线只切换 selector 出口，不会迁移已建立的长连接；长连接池必须按链清理。
- 清理必须带 age 过滤，禁止 kill-all（在途流仍可能是好请求）。

## INC-011 — 出口被上游静默悬挂 + 守护进程权限/托管缺失（2026-10-27）

**日期**: 2026-10-27
**等级**: P0（00:0x 起 opencode free 大面积失败；9router 01:49 被杀后停机 ~10h）
**现象**: `transport503` 每 2 分钟 159 条、`ttft=0`、`total≈20s`；`/v1/models`
每次 10s；面板/会话大量超时。ASIP 健康行长期 `503=0`，从不换线。

### 真因链（证据）

1. **出口被上游静默悬挂，而非出口不通**：故障链上 `warp_egress_ip` 1.2s 通、
   `GET https://opencode.ai/` 0.6s 200，但不带指纹的 chat 探测 0.26s 返回 429，
   带 `Bearer public` + `x-opencode-*` 指纹的同一请求挂满 30s 无响应头。
   换到 WARP-CF-D 后同请求 0.25s 429、真实会话 5/5 成功 —— 结论：该 WARP 出口
   被上游针对，CF 可达 ≠ 上游肯服务。
2. **检测失明**：`get_9router_error_rate` 的 transport 计数只看最近
   `RECENT_WINDOW=5` 条（高流量下 ~2s 就滚出窗口），10s tick 到来时恒为 0；
   清理又被 `transport_stale_gap_sec` 节流，故障期间从未触发。
3. **9router 无托管**：`9router.service` 处于 disabled，进程靠手工 `npm start`；
   01:49 被 SIGKILL 后无人拉起，停机约 10 小时（`logs/server.log` 停在 23:31，
   journal 末条 01:49:35）。
4. **sidecar 重启风暴泄漏**：`stop()` 只杀 `active.child`，spawn 等待期间再次
   `stop()` 会把旧 promise 的 child 复活成孤儿；实测 `bun` 进程 52→65 个、
   合计 1.3GB RSS。
5. **ASIP systemd 单元以 root 运行**：`/root/.singbox_secret` 缺失、
   `NODE_INFO_DB` 打不开（`unable to open database file`），单元每 10s
   `auto-restart` 空转；真实实例一直是手工跑的 administrator 进程。

### 修复

- `daemon.py`：transport 计数改为 **180s 时间回看**（`TRANSPORT_LOOKBACK_SEC`），
  不再受 5 行窗口限制；新增 `_opencode_lane_alive()`（curl + socks5h 打一条
  最小 free-lane 请求，任何 HTTP 状态含 429 都算活），出口可达但链路悬挂时
  **换线**而不是清连接；主循环在 transport>0 时直接驱动探针。
- `config.py`：新增 `TRANSPORT_LOOKBACK_SEC=180`。
- 9router：`opencodeTransport.js` 增加 `startGen` 代号守卫（作废的 start 不得
  写回 `active`）+ 传输超时重启 sidecar 的 30s 冷却。
- 运维：`systemctl enable --now 9router.service`（Restart=on-failure）；
  ASIP 单元加 drop-in `User=administrator` + `HOME=/home/administrator`。
- 验证：全量 `test_daemon_unit.py` 230 passed；重建后 rebuild 后 5/5 会话
  0.8–1.4s，3 分钟内 36 success / 0 error。

### 教训

- 「出口探通」不等于「上游可用」：健康探针必须打真实业务端点，并带上线上
  同款指纹头；只探 CF trace 会把「IP 被针对」误判成「连接池僵死」。
- 计数窗口必须按时间而不是条数：高流量下条数窗口只覆盖数秒。
- 任何常驻服务都必须有 supervisor；`Restart=always` + 正确的 User/HOME 是
  最低要求，缺一条就会静默停机或空转重启。

## INC-012 — 新 WG 线「已启用但没人用」+ 模型层 quota 被当出口故障（2026-10-28）

**等级**: P3（无用户可见故障，但换线算力被白白消耗）
**现象**: 16 条 `warp-wg-*.service` 全部 active、主 sing-box 的 Clash API 里
16 个 `WARP-WG-*` 出站可见，openCode 也验证过可切；但 `daemon.log` 里
`WARP-WG` 出现 0 次，轮换全部落在 `WARP-CF-*`（usque）。

### 根因

1. **daemon 的成员表是 config.py 导入时按环境变量展开的**。systemd 单元里只有
   `SINGBOX_SECRET` / `NINE_ROUTER_PASSWORD` / `OKZ_SUB_URL`，没有
   `WARP_WG_FAMILIES` / `WARP_WG_NODES` → `CFG["warp"]["members"]` 里没有 WG
   成员。sing-box 有出站 ≠ daemon 会用它。
2. **模型层 quota 被归为出口级错误**。日志里 `[OpenCode Free] 5/5 出口级错误 |
   403占比=0%` 配合 `模型层: quota（Retry-After 3.9h）`，说明上游是额度耗尽，
   与 IP 无关；daemon 仍按 IP 故障处理，重启同一条线 33–35 次，且「暂无 120s
   内 OK 的备线」导致换线判断连续空转。

### 结论

- 部署新线必须同时改 **部署脚本** 与 **systemd 单元的 daemon 环境**，两处
  不一致就会出现「线在跑、没人选」。
- 403=0% 且带 `Retry-After` 的失败不能算 IP 问题，换线无效；这类错误应直接
  上报冷却，不重启隧道。

### 处理

- systemd 单元补 `WARP_WG_FAMILIES=v4,v6,v6,v6` + `WARP_WG_NODES=4`，重启后
  daemon 状态表从 20 条变 36 条（16 条 WG 全部纳入探测/换线）。
- 新增全局额度闸 `_QUOTA_HOLD`：Retry-After ≥ 30 分钟的 quota 开闸，闸内不重启
  「quota/惩罚期/429」类线，短 Retry-After 与隧道半死照常处理。
- 效果：改动前 30 分钟重启 253 次，改动后同口径 2 次。

