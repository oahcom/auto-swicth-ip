# 配置参考：改了会怎样

> **本文回答什么问题**：`config.py` 里的每一项，**动它会连带改变什么**？
>
> 默认值不在这里抄 —— `grep '"键名"' config.py` 就有，抄一遍只会立刻过期
> （2026-11-03 实测：CLAUDE.md 的配置段有 8 项值与 `config.py` 不符）。
> 这里只写三件事：**它控制什么、改了会坏在哪、什么信号说明改错了**。

## 一、读这份文档前必须知道的两件事

### 1. 配置只有一份真源

`config.py` 的 `CFG` 是唯一来源。`.env` 只覆盖**拓扑类**的少数键
（`WARP_WG_FAMILIES` / `WARP_WG_NODES` / `ASIP_WARP_BLOCKS` /
`ASIP_WARP_BLOCK_CHANNELS` / `NINE_ROUTER_BASE` / 各路径），不是所有键都能从
环境变量改。改 `.env` 后**必须重启 daemon** —— `EnvironmentFile=` 只在进程
启动时读一次（2026-11-03 实测：远端 `NINE_ROUTER_BASE` 改成 38731 后不重启
不生效）。

要看当前**生效值**，不要读 `config.py` 源码猜：

```bash
python3 -c "import config,json;print(json.dumps(config.CFG['warp'],ensure_ascii=False,indent=1))"
```

### 2. 有一批值在 2026-10-28 被**故意归零**

`2cd6845`（"探测去限流"）把下面这些归零，实测吞吐 36 → 52.3 条/分钟：

| 键 | 值 | 归零的理由 |
|---|---|---|
| `line_probe_cadence_sec` | `0` | 曾有 60s 地板，36 条线 × 60s 把吞吐锁死在 36 条/分钟，而单次探测只要 1.9~5.7s —— 加渠道本来是为提速，却按「渠道数 × 60s」算轮次 |
| `probe_max_per_min` | `0` | 旧值 30 是纯自我设限：烂地址在入口就被惩罚期短路（判脏根本不探），再压频次只会让「新地址刚变可用」没人发现 |
| `restart_max_per_hour_per_line` | `0` | 它挡的不是观测，而是「把确定不能用的地址换掉」这个动作。撞上限后线路带着烂地址继续跑，比多重启几次更糟 |
| `restart_max_per_minute` | `0` | 同上，全局版 |

**不要"恢复"这些上限**。它们的副作用有实测记录：归零后 2026-10-28
19:36~20:08 出现 10028 次重启 / 125 分钟 —— 但同期吞吐是涨的。真要限制，
先读 `docs/established-facts.md` 第 11 条（一次失败不算死）与第 25 条
（`fail_streak` 语义），确认限制的是"真故障"而不是"观测频率"。

## 二、探测节奏

| 键 | 控制什么 | 改了会怎样 |
|---|---|---|
| `probe_concurrency` | 同时在飞的行级探测线程数 | **唯一的物理上限**。36 条线 × 1.9~5.7s，需要 32 槽才能把一轮压到单次探测耗时量级。调小 → 一轮变长，换线时「没有新鲜备线」 |
| `model_probe_concurrency` | 真打上游 `chat/completions` 的并发 | 与上者分开：它撞的是**上游限流**不是本地 CPU。每格探测都烧一格 opencode 免费配额 |
| `line_probe_cadence_sec` | 同一线两次观测的最小间隔 | `0` = 探完立刻重新排队。**不要调回 60**，理由见上表 |
| `probe_unknown_retry_sec` | 只对"没拿到结论"的那条线的重试间隔 | 不是轮次，不拖慢其它线。调大会让 unknown 线滞留 |
| `probe_ok_ttl_sec` | OK 结论的缓存 TTL | 见下方「三档 TTL」 |
| `probe_transient_ttl_sec` | `unreachable`/`err` 的 TTL | 同上 |
| `probe_persistent_ttl_sec` | `quota`/`flagged` 的 TTL | 同上 |

### 三档 TTL 为什么不能合并

分档依据是**结论的持久性**，不是好坏：

- **持久档**（`quota` / `flagged`，600s）：地址自身的配额/信誉结论。同 /128
  重探有 99.2% 仍判 quota，30h 内仅 1 次恢复。**不要调回 21600**（这是它的
  历史值，`git log -S` 可查）—— 那会让最该持续观测的地址 6 小时无人看。
  重复探测的代价已被入口的惩罚期短路挡住（判脏地址根本不探），不需要第二道停探闸。
- **瞬时档**（`unreachable`/`err`，120s）：这是**探测侧故障**，不是地址状态。
  实测同一地址 `err` 后 66s 即 OK。长 TTL 会让健康线在 6h 内被跳过。
- **OK 档**（1800s）：OK 说的同样是地址状态，但同 /128 重探间隔中位 212s，
  2 分钟档位命中不到自己。600s 时可省 33.8% 探测量。

**真正的安全网不是 TTL，是「标脏即作废」**：`_mark_warp_ip_used(blocked=True)`
会立刻 `_probe_cache_forget(ip)` 清掉该 IP 的全部结论。47 例 OK→quota 翻脸里
45 例期间发生过生产侧 429/403 换线，走的正是这条路径。

## 三、换线

| 键 | 控制什么 | 改了会怎样 |
|---|---|---|
| `cooldown_ok_sec` | 换线**成功后**的冷却 | 固定 5s，**不随连续失败放大**。旧的 2^n 退避曾在风暴期锁死到 600s（实测 17:02~17:09 九分钟零换线、1752 条全废）。5s 的依据：新出口首条错误延迟 p25=0.7s / p50=1.6s / p75=4.5s，1s 会让 40% 的"刚换就错"冷却后立刻再触发 |
| `cooldown_fail_sec` | 换线**失败后**的冷却 | 3s。60s 会让一次失败后整分钟停在坏出口 |
| `switch_wait_sec` | 没有新鲜 OK 时的等待上限 | 3s，且**等之前先催探测器**。旧值 20s：74 次换线里 73 次等满后照样放弃，空转 24.7 分钟 —— 等待期间没有任何人能让"新鲜度"变好 |
| `line_fresh_sec` | 备线的 OK 要多新才算数 | 120s。旧 OK 说明的是"那时能用" |
| `ip_reuse_guard_sec` | 出口 IP 复用窗口 | 24h。实测出口 IP 可用周期极短，8~11 分钟复用的几乎立刻爆发 |
| `prefix_rest_sec` | /64 段的静置时长 | 只用于**同档内排序**，不能升级成准入或跨档比较（见既定事实） |

### `proactive_mode` 是互斥的

`requests`（当前）与 `fixed` 二选一，由分支末的 `return` 保证。叠加时
fixed 到点那次换线会把 requests 的配额计数一起清零，150 格永远跑不到。
导入时校验取值，`lifetime` 等遗留值直接 `SystemExit`。

## 四、重启

| 键 | 控制什么 | 改了会怎样 |
|---|---|---|
| `restart_cooldown_sec` | 重启被**非失败原因**挡下后的重排间隔 | 固定 5s，不随 `fail_streak` 涨。它防的是"原地空转"，不是惩罚 |
| `restart_concurrency` | 全局并发重启上限 | 12。46 条线互不依赖，`systemctl restart` 各自独立，排队换不来东西 |
| `restart_max_per_hour_per_line` | 每线每小时重启上限 | `0` = 不设。理由见第一节 |
| ~~`restart_backoff_sec`~~ | **已删除** | 这条曲线从未真正生效：重启成功会清零 `next_restart_ts`，紧接着探到 quota 又立刻重启，退避表根本没被查过（实测 168 次重启/分钟、`fail_streak` 涨到 6586 而间隔仍是最低档） |
| ~~`refresh_concurrency`~~ | **已删除**（T-223） | 46 条线互不依赖，唯一需要互斥的是**同一条线**。旧上限实测 134 次"名额已满"里 **128 次（95.5%）是同一个 tag 自己撞自己** |

## 五、四道锁

| 键 | 控制什么 | 改了会怎样 |
|---|---|---|
| `channel_ban_reset_hour` | 渠道锁解封时刻 | 8（早八点）。**这是 opencode 官方的配额重置时刻，不是我们定的闹钟**。改了锁会提前或滞后于真实恢复 |
| `channel_ban_min_samples` | 样本条数下限（**达到**才判，取当天最近 N 条） | 1000。判据是这 N 条**全部** quota 才锁；出现任何一条 OK/flagged 就否决。旧口径「1h 窗口 100% + 铺满半窗」已废（能出 OK 的线被锁一整天，实测 WG-21） |
| `channel_noaddr_hold_sec` | 连续拿不到出口地址多久锁渠道 | 5h。与 quota 判据独立：拿不到地址 = 隧道/节点死了，重启换地址也买不到东西 |
| `channel_ban_window_sec` | **不再是锁的窗口**，只给 `ipv6_segments.colo_ban_rates` 算 colo ban 率用（重定位/补位挑 colo） | 3600。改它只影响挑 colo 的统计口径，不影响锁 |
| ~~`channel_ban_min_cover_ratio`~~ | **已删除** | 旧口径「样本要铺满半窗」的产物，随判据一起废弃 |
| `channel_ban_hold_enabled` | 渠道锁总闸 | 关掉后已写入的锁立刻失效，并触发一次"还原回原节点"；补位渠道一并撤除（关闸是回滚） |
| `channel_rehome_enabled` | 重定位总闸 | 关掉后锁线不再搬 colo，但**补位照开**（`_backfill_open` 传 `ignore_enabled=True`，主人的硬要求） |
| `channel_rehome_min_samples` | 目标 colo 的样本下限 | 10。低于它的 colo 不配当"ban 率最低" —— 3 条零封禁会把整批线吸过去 |
| `night_window.enabled` | 夜间锁 | 当前 `false`。它防的是"深夜重启刷不出地址"（10-31 实测 17036 次重启、**0 次刷新成功** —— 不是重启坏了，是池子干了） |
| ~~`line_quota_quarantine_*`~~ | ~~撞长 Retry-After 就隔离该线~~ | **2026-11-05 已删除**：blocked 之后的 1800s 冷却不再存在。立论「重启买不到配额」与实测不符，冷却只会让线在池子最脏时退出战场 |

**四把锁在"刚换过节点"时必须让路**（`_warp_line_restart(..., rebound=True)`）。
它们防的是"在同一个烂地址池里反复抽签"，换了节点池子就变了。

## 六、落池开关

| 键 | 值 | 说明 |
|---|---|---|
| `proxy_fallback_enabled` | `false` | 2026-10-26 关闭。理由：池内节点全挂，落池 = 断流。WARP 全不可用时改**原地重连** |
| `proxy_fallback_cooldown_sec` | 10800 | 仅在开关为 `true` 时生效。冷却期内改为**盲切另一条 WARP 成员** |
| `cleanup_enabled` | `false` | 传输层 503 时只记录不清理连接 |

恢复方式：`proxy_fallback_enabled: true`。恢复前先确认池子真的能用
（`/proxies/proxy` 的成员数与实测成功率），否则等于把流量交给 3~5% 成功率的资源。

## 七、`warp.wg` 子表

`enabled: false` 时 WG 线整段不生成。开它要注意：

- `families` 必须非空，否则 `_wg_count = 0`，`members.py` 只写出 28 条而不是 50 条
- `base_port` = 1110，`WARP-WG-N` 占 `1110+N-1`
- `udp_cache_ttl_sec` = 21600：`udp_capable()` 的缓存。**这个函数只做原样透传**，
  不要给它加回任何过滤逻辑（见既定事实第 8 条：STUN 探不通 ≠ WARP 跑不了）

## 八、改配置后的验证顺序

```bash
# 1. 生效值（不是源码里的值）
python3 -c "import config,json;print(json.dumps(config.CFG['warp'],ensure_ascii=False,indent=1))"

# 2. 单元与渠道没被打散
set -a; . ./.env; set +a
python3 warp_blocks.py plan

# 3. 纯模型单测
python3 -m pytest test_warp_blocks.py -q

# 4. 语法
python3 -m py_compile daemon.py config.py warp_wg.py warp_blocks.py

# 5. 必须重启才生效
sudo systemctl restart auto-switch-ip.service
```

**判据**：如果某个改动让 `plan` 的渠道数变少、或让任何一条已知能用的线不再
被选中，先怀疑是倒退而不是修复。

---

## 相关文档

- [../established-facts.md](../established-facts.md) —— 每个"为什么"的权威源
- [../subsystems/prober.md](../subsystems/prober.md) —— 探测节奏怎么用这些值
- [../subsystems/switching.md](../subsystems/switching.md) —— 换线怎么用这些值
- [../subsystems/locks.md](../subsystems/locks.md) —— 四把锁的完整语义
- [state-files.md](state-files.md) —— 这些配置写进了哪些状态文件
