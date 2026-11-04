# 四把锁：各防什么，什么时候必须让路

> 本文回答：为什么“线坏了却不能重启”？四把锁分别拦什么，什么情况下必须绕过它们？

## 一、先讲共同点

四把锁看起来理由各不相同，但第一条是同一个：

> **它们防的都是「在同一个烂地址池里反复抽签」。**

重启一条线 = 让 Cloudflare 重新给这条线分配一个出口地址。但：

- v6 每次重连确实换新地址 → 重启有效
- v4 由落地 colo 决定 → 同 colo 重启拿到的是同一批地址 → **重启无效**
- 整个 colo 被烧穿时 → 换到同 colo 的另一个地址还是被烧的 → **重启无效**

所以这些锁不是“限制性能”，是“别做无用功还要付代价”。代价是什么：
每次重启会**掐断在这条线上传输的请求**（客户端看到 `upstream connection lost`）。

---

## 二、夜间锁

**字段**：`warp.night_window`（默认 `enabled: False`，窗口 20:00 → 08:00）

**防什么**：夜里重启刷不出新鲜地址。10-31 夜间实测 **17036 次重启、0 次刷新成功**，
全部白烧，还把每条线的在途连接掐断了一万多次。

**为什么夜里刷不出来**：WARP 出口 IP 池是**全用户共享**的。到了深夜，池子里的
干净地址基本被用完了 —— 不是我们的重启坏了，是池子干了。
（详见 [established-facts.md](../established-facts.md) 第 0 条）

**什么时候必须让路**：**换了节点之后**。锁防的是「同一个地址池反复抽签」，
而换节点已经把池子换掉了。

**注意**：第一期默认**关闭**（`enabled: False`）。第一阶段的目的是“全部渠道白天
夜里都跑，测出每个 colo 的夜间封禁率”，锁住一部分线等于把要测的样本删掉。

---

## 三、渠道锁

**字段**：`ban_hold_until`（默认锁到次日 `channel_ban_reset_hour = 8` 点）

**防什么**：某条线**当天**最近 1000 条模型层实测**全部**判 quota，说明整条
渠道（同 colo 全段）都烧穿了。这时候重启换地址不会产出。

**判据**（`_channel_ban_hold_hit`）：
1. 样本只算当天（跨过 08:00 自动作废）
2. 满 `channel_ban_min_samples = 1000` 条才判
3. 其中**一条** OK / flagged / err 就否决（只有全 quota 才算烧穿）

另一条独立判据：连续 `channel_noaddr_hold_sec = 5h` 拿不到出口地址 → 同样锁定
（隧道/节点已经死了，重启买不到任何东西）。

**锁定期间不重启、不探测**（`stop_probe=True`、`next_probe_ts=until`）：探测出来
的 quota 会进窗口，到期时互相续期 —— 实测 WG-21 因此锁了 17 小时。

**为什么锁到早八点**：opencode 免费层的配额按自然日重置，重置时刻是 **08:00**
（本地时间）。这**是上游的重置时间**，不是我们随便定的闹钟。详见
[established-facts.md](../established-facts.md) 第 1 条。

**什么时候必须让路**：换了节点之后。

### 补位渠道（`ban_backfill_*`）

**一个渠道被封，必定要有另外一个渠道补位**（主人 2026-11-04 定）。命中锁后
`_rehome_held_lines` 每分钟对账一次，缺补位的就开一条：

1. 用**已注册但没启用**的空闲设备线（`lineN/profile.conf` 存在、不在 members
   里、单元没被 mask）
2. 落地节点取 ban 率最低的 colo（`_lowest_ban_colos` → `ipv6_segments.colo_ban_rates`）
3. 写线配置 → 起单元 → 写 `.warp/backfill.json` → 重跑 `deploy/members.py`
   发布 `.warp/warp-outbound.json` → `refresh_subscription` 重建主配置
4. 被封的那条正在服务流量时，selector 切到补位渠道
5. 次日 08:00 解封：selector 切回原线 → 停单元 → 撤成员 → 重发配置

**每分钟对账一次**（`_backfill_reconcile`），修两种半途状态：台账行没有锁线认领
→ 停单元、删行；锁线记着 `ban_backfill_tag` 但行丢了 → 停残留单元、清字段，当轮
重开。没有它的话，开/撤过程中一次写盘异常就能让「被封必有补位」永久失效（锁线
以为有补位，开补位循环永远跳过它）。

补位是**硬要求**，`channel_rehome_enabled` 关掉时重定位可以停，补位照开
（`_backfill_open` 传 `ignore_enabled=True`）。补位渠道自己也被封时不再套娃开新线。

成员身份必须落 `.warp/backfill.json` 而不是只在 daemon 内存里：`deploy/members.py`
与 `parse_subscription.py` 都是独立进程，只改 `CFG["warp"]["members"]` 它们看不见，
补位渠道起来了也没人能切过去。`config.py::_append_backfill_members` 读这个文件把
成员装回 `CFG`，daemon 重启后同样能看见并撤销。

补位渠道不参与同块轮回、也不参与 `_rehome_held_lines` 的重定位 —— 它的落地节点
就是补位目标本身，搬走就补错地方了。

---

## 四、额度隔离

**字段**：`quota_until`

**防什么**：这个出口地址的额度已经耗尽，重启只会拿到另一个同样耗尽的地址。
（`quota_hold_enabled` 默认 **False** —— 2026-10-28 按主人要求关闭，即便撞到长
Retry-After 也照常换线。）

**什么时候必须让路**：换了节点之后（新节点 = 新地址池）。

---

## 五、小时配额

**字段**：`restart_hist`（默认 `restart_max_per_hour_per_line = 20`）

**防什么**：同一条线无限重启。重启成功时 `next_restart_ts` 会被清零，重启失败
时的退避也管不住成功路径，所以频率必须单独记账 —— 否则一次成功就换一次新地址，
最快 5s 一轮。实测无上限时单线 **76 次/h**。

**计数位置**：`systemctl` 调用**之前**。失败的重启同样掐断过在途连接、同样烧掉
一格，只统计成功会让“重启后未就绪”这条路径绕过上限。

**达上限只停重启、不停探测** —— 压的是有副作用的决策，不是观测。

**什么时候必须让路**：换了节点之后。这个上限本意是“同一条线别无限重启”，
而换节点已经把“重启同一个坏节点”这个前提换掉了。不清零的话，触发轮回的那条线
（重启次数正是打满的）会被直接拒掉 —— 配置换了、进程没重启，等于什么都没做。

---

## 六、让路的实现

`_warp_line_restart(member, reason, rebound=False)`。

同块轮回守护换完节点后调用时传 `rebound=True`，四把锁全部跳过。
另外 `restart_hist` 和 `next_restart_ts` 会被显式清零。

**实测教训**：一开始只在调用点删掉了 `_night_block_restart` 的判断，但
`_warp_line_restart` **内部还有一道**，加上渠道锁也在内部，结果 WG-22 换到新节点后
一直没重启 —— 配置改了、进程还在跑旧节点，守护却以为自己救过它了。

---

## 七、四把锁速查表

| 锁 | 字段 | 默认 | 防什么 | 让路条件 |
|---|---|---|---|---|
| 夜间锁 | `night_window` | 关 | 夜间池子干了，重启白烧 | 换了节点 |
| 渠道锁 | `ban_hold_until` | 开 | 整条渠道烧穿 | 换了节点 |
| 额度隔离 | `quota_until` | 关 | 出口额度耗尽 | 换了节点 |
| 小时配额 | `restart_hist` | 20/h | 同线无限重启 | 换了节点 |

---

相关文档：[glossary.md](../glossary.md) · [warp-node-rotation.md](../warp-node-rotation.md) · [established-facts.md](../established-facts.md)
