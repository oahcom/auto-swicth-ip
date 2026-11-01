# WARP 隧道分配规则（2026-11-01）

## 〇、上一版错在哪（先钉住，别再犯）

**错误一：按落地 /64 切块。** 数出 27 块，等于把「这一次会话抽到的 /64」当成了节点的属性。
同一节点连测 5 轮（`/tmp/opencode/stab_result.json`）：

| 节点 | 5 轮 v6 落地 /64 |
|---|---|
| 64.120.94.204 (SIN) | **5 个全不同** |
| 192.3.195.11 (ORD) | 5 个全不同（还漂到 PIT、SIN） |
| 1.162.142.52 (TPE) | 5 个全不同（还漂到 LAX 一次） |
| 208.87.242.97 (LAX) | 4 轮 3 个不同 |
| 134.195.101.120 (SJC) | 3 轮 3 个不同 |

16 组（节点×族）里 **7 组连 /48 都会变**。`docs/colo-segment-map-20261101.md` 第六节 a
早就写了「段不可定向选择」—— 段是**抽签结果**。按段切块，今天分的 27 块明天重连就散架。

**错误二：直接照抄库里的桶标签。** 拿实测 trace 回验，库标签和真实 colo 对不齐：

| 库里的桶 | 实测 trace 报的 colo | 结论 |
|---|---|---|
| NRT | NRT ×14 | ✅ |
| SIN | SIN ×15 | ✅ |
| SJC | SJC ×7 | ✅ |
| **CFB** | **SJC ×2**（现役 CF / CF-B 实测） | 桶名是「CF-B 专用池」，真身是 SJC |
| **PHX** | PHX ×7 / **LAX ×4** | 该段池被两家共用，标签不可靠 |
| **OLD** | **LAX ×1** | 名字误导 |
| CF | 无样本 | 未知 |

所以：**块按 colo 分，colo 取实测 trace 值，不取库标签。段只用于块内排序与避让。**

## 一、块表（9 个 colo）

`/48` `/64` 是**该 colo 段池的规模**（会话建立时从池里随机抽一个 /64），
不是可选清单 —— 客户端无法指定要哪一段，这一点决定了块不能按段切。

| colo | 机房 | 段池来源 | /48 | /64 | 当前封禁率 | 直连WARP | 稳定代理 | 不稳定代理 |
|---|---|---|---|---:|---:|---:|---:|---:|---:|
| **NRT** | 东京 | 仅代理 | 12 | 12 | **100.0%** | — | 8 | 6 |
| **PHX** | 凤凰城 | 仅代理 | 12 | 24 | 79.2% | — | 6 | 0 |
| **LAX** | 洛杉矶 | 直连WARP + 代理 | 11 | 67 | 26.6% | 2 | 0 | 3 |
| **SJC** | 圣何塞 | 直连WARP(CFB池) + 代理 | 12 | 52 | 25.0% | 2 | 3 | 1 |
| **SIN** | 新加坡 | 直连WARP + 代理 | 12 | 132 | 15.2% | 1 | 10 | 0 |
| **TPE** | 台北 | 仅代理（今天首现） | 7 | 7 | **未知** | — | 4 | 0 |
| **HKG** | 香港 | 仅代理（今天首现） | 2 | 3 | **未知** | — | 2 | 7 |
| **ORD** | 芝加哥 | 仅代理（今天首现） | 4 | 4 | **未知** | — | 0 | 1 |
| **PIT** | 匹兹堡 | 仅代理（偶发漂移） | 1 | 1 | **未知** | — | 0 | 0 |

> **术语先钉死**：本文的「直连」一律指 **本机直连 WARP**（usque / wgcf 隧道，
> 即 `WARP-CF*` 与 `WARP-WG-*`，流量经本机自己的 WireGuard 封装出去），
> **不是**裸直连。裸直连到 `162.159.192.1:2408` 的 UDP 被运营商 ACL 拦
> （ICMP type 3 code 13），根本握不上手。
> 与「直连」相对的另一种承载方式是「代理承载」：WARP 的 UDP 经机场节点转发。

三条必须记住的：

- **本机直连 WARP 只有 3 个真实 colo：SJC / LAX / SIN**，不是 8 个。现役 6 条线实测 trace：
  `CF` → SJC、`CF-B` → SJC、`CF-D/E/F` → LAX、`WG-13` → SIN。
  库里的 `CF` 桶（`bac1:19xx`，8 段）是 usque 的 bac1 池，**至今没有一次 trace 样本**，
  真实 colo 未定 —— 暂不单独立块，落在哪算哪个 colo 的块。
- **HKG / TPE / ORD / PIT 是今天才第一次出现**：它们的 /48 不在库内任何一条线上
  （`bac1:31a0`、`bac5:3981`、`bac1:7400/7420`、`bac5:58ec/58ef/5901~5903`、`bac5:a5fb~a5ff`），
  从没被踩过，封禁率完全未知 —— 这正是第一阶段要测的。
- **colo 本身也不是 100% 稳定**：ORD 的节点 5 轮里漂到 PIT 1 次、SIN 1 次；TPE 漂到 LAX 1 次。
  colo 只能当**标签**，不能当硬保证；漂了按实测归块。

## 二、三类渠道（判定规则）

**按节点实测成功率分，不按承载方式（本机直连 WARP / 代理承载）分**：

| 判定 | 条件 | 归入 |
|---|---|---|
| 稳定 | `success/attempts == 1`（无失败反例） | **常规块** |
| 不稳定 | `success > 0` 且有过失败 | **非常规块** |
| 不可用 | `success == 0` | 不入块 |

代理承载的节点只要稳定，就和本机直连 WARP 一起进常规块 —— 常规/非常规的分界是**稳定性**，不是承载方式。

| 类型 | 块 | 渠道 |
|---|---|---:|
| **常规** | 7 个：NRT / PHX / LAX / SJC / SIN / TPE / HKG | 14 |
| **非常规** | 5 个：NRT / LAX / SJC / HKG / ORD | 10 |
| **直连 WARP v4** | 1 个（LAX，CF-D / CF-E 两条 v4 保底线） | 2 |
| **代理 v4** | 1 个（全部节点 v4 合并） | 2 |
| | | **28** |

- 每块每类 **2 条**渠道
- 同一 colo 可同时有常规块和非常规块（NRT / LAX / SJC / HKG），
  因为块内是**节点清单**，常规块只放稳定的那几个
- 常规块里的节点来源：本机直连 WARP 5 条（SJC×2、LAX×2、SIN×1）+ 代理承载但稳定的 33 个
  （SIN 10、NRT 8、PHX 6、TPE 4、SJC 3、HKG 2）
- 代理 v4 是唯一合并块：v4 出口段高度收敛（13 个 /24 集中在 104.28.x.x），
  逐块开渠道只是同段重复

## 三、地址排序：只读缓存，绝不进同步路径

延迟是会变的，任何测量都有保质期；但测量**不能插在换线/刷新的同步流程里** ——
换线每多等一秒，坏出口就多服务一秒失败请求（现有 `_ERROR_PATH_BUSY_WINDOW_SEC`=2s 就是这个道理）。

| 项 | 做法 |
|---|---|
| 数据源 | `_NODE_DELAYS`（`_health_pool` 后台线程池每 300s 更新）+ Clash `/proxies/{tag}/delay` |
| 保质期 | 每条带 `ts`，TTL **300s**；过期算「未知」，不算「很快」 |
| 排序 | 纯读 `sorted(block, key=(过期?, delay, tag))`，过期值排同档末尾 |
| 未知 | 排块尾 + 投递一次后台探测；本次换线**不等** |
| 禁止 | 排序 / 选块 / 换线路径里出现任何网络调用；`delay()` 不得在 `_WARP_LOCK` 内调用 |

## 四、白天 / 夜间

### 白天
只用**常规块**（14 条）。

### 夜间（20:00 → 08:00）
夜间大批量 ban，常规节点大概率不够用，但不会全军覆没；非常规也未必可用。规则：

1. **全部 28 条渠道都参与**，按**实测存活度**排序，不按先验分类
2. **只要不是 100% 失败就用** —— 夜间产出率再低也比空着强
3. **加大刷新力度**：额外加隧道、缩短重启间隔，靠换会话 IP 硬抢产出
4. **本机直连 WARP 的 v4 渠道（LAX 2 条，CF-D / CF-E）按文档是夜间可用性最高的一档**，优先保
5. **本机直连 WARP 的 v6 渠道夜里基本不可用但会有零星产出**，不能剔
6. 夜间窗口内完全不看延迟排序 —— 能通就上

现有 `warp.night_window`（20:00→08:00 只允许白名单刷新）保留，
白名单改为**按块生成**（每块当前 2 条），不再硬编码 8 个 tag。

> **2026-11-01 20:33 修正：第一阶段的夜间锁是关着的。**
> `warp.night_window.enabled=False`。理由：第一阶段的目的就是「全部渠道白天
> 夜里都跑，测出每个 colo 的夜间封禁率」，锁住一部分线等于把要测的样本自己
> 删掉 —— 夜里到底哪条线能出货就没数据了。锁的实现留在 `warp_night.py`，
> 第二期按实测筛出夜间专用池时再打开（白名单那时用 `night_line_tags`）。
> 10-31 那次「17036 次重启 / 0 次刷新成功」是锁的动机，但它也说明：夜里
> 该测的是「哪条线还能出货」，而不是把所有线一起关掉。

## 五、两阶段

### 第一阶段（现在）：全量纳入，白天晚上都跑
- 全部 9 个 colo 的节点，白天和晚上都参与；**不设夜间锁**
- 目的：测出每个 colo / IP 段的**夜间封禁率**
- 现在夜间数据几乎为零：HKG / TPE / ORD / PIT 是今天才出现的池子，没经历过夜间；
  NRT 12 段当前全封；PHX 24 段封 19
- 例外：**封禁率 100% 的块不占设备**（`warp.blocks.exclude_colos`，当前只有 NRT）。
  这不是夜间策略，是「这个 colo 的段已经全封，新设备拿到的 /128 必然也在限额里」
  —— 占着设备不用等于把可测的块挤掉。第一阶段要测的是**还有余量**的块。

### 第二阶段：按夜间表现筛选
跑够若干夜晚后，把「晚上可用率高」的 colo 和 IP 段列成夜间专用池，其余白天用。
判据用实测封禁率，不用地理猜测。

## 六、当前实测底数（2026-11-01）

| 项 | 数 |
|---|---|
| 代理出口 IP | 95 |
| 能建 WARP 隧道 | 51（稳定 33 / 不稳定 18 / 死 44）|
| 双栈都通 / 只 v4 / 只 v6 | 38 / 8 / 2 |
| 协议 | vless 29/35 = 83%、anytls 19/59 = 32%、trojan 0/1 |

**44 个「死」节点的定性**（跑到 18/44 被超时打断）：TCP 活着 17/18；
UDP 转发不通 6 个；WARP 握手 3 轮全失败 18/18。
不是节点挂了，是**转发 UDP 的能力**不到。

**已证伪的探针**：往 `162.159.192.1:2408` 发裸 UDP 判连通 —— 对已知能跑 WARP 的节点
（`cn.bing.com:443~21-any` 12/12、`pq.aws54` 8/8）同样全部超时。WireGuard 对非握手包
静默丢弃，这个探针**恒为 False**，已弃用。判「能不能承载 WARP」只能靠真建隧道。

## 七、落地状态（2026-11-01 实施）

| # | 步骤 | 落点 | 状态 |
|---|---|---|---|
| 1 | `warp.blocks` 配置：9 个 colo 的静态属性（机房 / 承载 / 段封禁率）+ `channels_per_block` + `enabled` | `config.py`（`warp.blocks`）+ §启动校验（colo 重复、`channels_per_block<1` 导入即炸） | 完成，**默认 `enabled=False`** |
| 2 | 节点→块归属表由实测生成 | `warp_blocks.py build-map <probe_dir>` → `.warp/block-map.json`（gitignored，`.warp/` 已在 .gitignore） | 完成，51 节点入块（regular 33 / irregular 18）|
| 3 | 渠道 = (类型, 块, 序号) → 线路参数 | `warp_blocks.channels()` → `plan_lines()` → `warp_wg.apply`（选点分叉只在这一处，注册/写盘/发布共用） | 完成 |
| 4 | 延迟排序纯读缓存 | `warp_blocks.order_nodes(nodes, delays)`；`apply(delays=)` 由调用方传入，模型自己不测量 | 完成（接口留好，daemon 侧接 `_NODE_DELAYS` 待开 `enabled` 时接） |
| 5 | 夜间白名单按块生成 | `warp_blocks.night_line_tags()` → `daemon._block_night_tags()`（30s 缓存，异常退回配置 allow） | 完成 |
| 6 | 回归 | `test_warp_blocks.py` 22 例；`test_night_window.py` / `test_warp_prober_gates.py` / `test_daemon_unit.py` / `test_warp_refresh.py` 与改前逐例一致 | 完成 |

**配置里没有 `nodes[]`**（第 158 行原计划有）。块成员只能来自实测，配置里再存一份
等于让「上周的事实」和「今天的观测」赛跑，输的那份没有人会去删。

### 7.5 扩容执行记录（2026-11-01 20:30 → 21:55，现网）

| 项 | 之前 | 20:30 | 21:55（全量） |
|---|---|---|---|
| WARP-WG 线 | 12（4 落地 × v4,v6,v6） | 20 | **28**（9 个 colo 全开） |
| 每节点渠道 | 3（v4+v6+v6） | 1 | 1 |
| 已注册设备 | 16 | 22 | **28** |
| 夜间锁 | 开（8 条白名单） | 关 | 关 |
| `channels_per_block` | 2 | 3 | 3 |
| 排除块 | — | `["NRT"]` | **`[]`**（主人定：全部块一起测） |

块表（28 条，全 v6）：NRT 6 · HKG 5 · SJC 4 · PHX 3 · LAX 3 · SIN 3 · TPE 3 · ORD 1
（v4 合并块的 3 条渠道暂不开：成员 `family` 语义还没跟着渠道走，v4 保底先由
usque CF-D/E 承担）。

**「一个池子」的修正（2026-11-01 23:30 主人指出）**：usque 的 v6 出口来自
**同一个共享池** —— 单条 CF-B 反复重连，迟早能刷出现在 C/G/H/I 手里那些地址
（实测佐证：CF 与 CF-G 已经落在同一个 /48 `1f00` 里，只是 /64 不同）。所以
多开 usque 线**不是扩地址池**，买的是两样别的东西：

1. **并发持有数**：同一时刻握着 N 个互不相同的出口 IP，每个都有自己独立的配额桶；
2. **刷新吞吐**：N 条线并行换会话，探索池子的速度是单线的 N 倍。

真正决定夜间产出的仍然是「池子里干净地址的存量与供给速率」——20:30-21:52
这 82 分钟 2975 次 quota、只有 9 次 OK，就是 SJC 池存量见底的直接读数
（当时 usque 还只有 5 条，加线改变的是并发，不是供给）。推论：加 usque 线
解决「并发不够」；池子被烧穿时（如 10-31 夜间）加多少条都没用，只能靠
**扩 colo**（每条 WG 线背后是一个独立的机场中继池）或等 08:00 重置。

**PIT 现在有渠道了（2026-11-01 22:15）**：PIT 从没有过自己的节点 —— 唯一落到
PIT 的是 ORD 那条 `us.bing.com:443~6-any`（`colo_mix`: ORD 7 / PIT 3 / SIN 1），
按众数它归 ORD，于是 PIT 块永远空着。加 `"pin": [...]` 让它**再挂一台设备**，
就多一条「预期落 PIT」的线（第 29 条，端口 1138）。代价说清楚：这条线七成时间
其实就是一条 ORD 线 —— 块名是预期落点，实测落点以 trace 为准，`build_map` 会把
漂移写进每个节点的 `colo_mix` / `drift` 字段。首次上线实测落在 ORD（符合 7/10 的
分布），要拿到 PIT 的样本得等它重连漂过去。

两次上线后的实测（每条约 10s 超时、串行探测）：

| 时间 | 线数 | 拿到 `warp=on` | 分布 |
|---|---:|---:|---|
| 20:47（排除 NRT） | 20 | **13** | PHX3 SJC3 SIN3 TPE4 LAX1 HKG0 ORD0 |
| 22:04（全量） | 28 | **17** | NRT3 TPE5 PHX2 LAX1 SJC2 SIN3 HKG1 ORD1 |

20:47 那批是「先排掉 NRT」的版本，22:04 是「全块一起跑」的版本 —— 后者把
NRT 又从 0 条拉到 3 条、HKG/ORD 各拉起 1 条，代价是 28 条里有 11 条暂时起不来
（HKG 5 条占多数）。两种都能给第一期的夜间样本，差别只是「覆盖率 vs 设备利用率」。

**同一个接口在两条路上都成立**：`ASIP_WARP_BLOCK_CHANNELS` 控制每块深度，
`exclude_colos` 控制排不排块 —— 改这两个值 + 重跑 `deploy/wg-lines-install.sh`
即可在「全量测」和「集中产出」之间切换，不用改代码。

复现这套拓扑只需要 `.env` 四行（`.env` gitignored，所以记在这里）：

```bash
WARP_WG_FAMILIES=v6      # 本轮不做 v4 渠道：v4 保底由 usque CF-D/E 承担
WARP_WG_NODES=28         # 线路数 = 1 × 28
ASIP_WARP_BLOCKS=1       # 块模型接管选点
ASIP_WARP_BLOCK_CHANNELS=3
```

然后 `./deploy/wg-lines-install.sh`（以 root 跑，`ASIP_RUN_USER=administrator`）——
它会 apply 写单线配置、渲染单元并重启；主 sing-box 另需
`deploy/patch-wg-outbounds.py` + 重启才看得到新出口，daemon 也要重启一次
（members 是 import 时冻结的）。

**夜间唯一真正出货的 colo（20:30–21:52 实测）**：82 分钟里 6186 次重启、
2975 次 `模型层: quota`，模型层 OK 只有 **9 次，全部来自 TPE**
（WARP-WG-7/14/15 各 3 次）。也就是说「隧道起得来」和「出口能用」是两件事：
NRT/PHX/LAX/SJC/SIN 的隧道大多能建（13~17/28），但出口 IP 几乎全在限额里。
这条数据直接指向第二期的筛选方向：**先按 colo 保 TPE，再按段测其余**。


**28 条 WG 线 = 28 个代理承载渠道**；本机直连 WARP 的 5 条（`WARP-CF/CF-B/CF-D/CF-E/CF-F`）
不经代理出口，仍按 `warp.members` 的老路走 —— `block-map.json` 是由「代理出口 →
真建隧道 → trace 取 colo」生成的，把直连线硬塞进去会出现没有观测来源的 colo 行。
v4 保底也在这 5 条里（CF-D / CF-E 是 v4 usque）。

**这一轮明确没做的两件（别以为已经生效）**：

| 计划里的项 | 现状 | 为什么没做 |
|---|---|---|
| 延迟排序接 `_NODE_DELAYS` | `order_nodes(nodes, delays)` 接口在，`plan` CLI 传 `None`，`apply(delays=)` 也还没人传 | 延迟缓存在 daemon 内存里，没有落盘快照；要做就得让 `_health_pool` 定期写一份（新 IO），或者让 apply 去查 Clash API（进同步路径）。按第三节的规矩，宁可不接，也不把测量塞进换线路径 |
| `warp.night_extra_channels`（夜里额外加隧道） | 键不存在，但**线已经加过了**：12 → 28 条常驻线，夜里全部参与 | 「夜间临时扩」是部署动作不是运行期动作。这一轮改的是「常态就多铺线」，等于把扩容前移到部署期；真要按分钟级临时加线，得先有预注册的空闲设备池，等第二期按块筛完再说 |

## 八、分层（这次的架构改动）

改造的第一原则是**不要让新的选择逻辑长进 `daemon.py`**（它已经 8270 行）。
所以拆成三个模块，各自只有一个职责，依赖方向单向：

```
config.CFG ──► warp_blocks.py   （渠道模型：块/类别/排序/夜间取舍 → 纯函数）
                 ▲   ▲
                 │   └── warp_wg.py      （线路：注册/写配置/重启/发布）
                 │            ▲
                 └──────── daemon.py     （调度：何时换线、何时重启、夜锁 IO）
                     warp_night.py ──────┘  （夜锁策略：窗口算术 + 放行判定）
```

| 模块 | 有什么 | 没有什么 |
|---|---|---|
| `warp_blocks.py` | 块表读取、`channels()`、`plan_lines()`、`night_line_tags()`、`build_map()` | 网络、文件写入（CLI 除外）、线路/端口/单元名 |
| `warp_night.py` | `window` / `active` / `restart_allowed` / `label` | 配置读取、日志、全局状态 |
| `warp_wg.py` | 注册、`build_line_config`、`apply`、`rebind`、`line_nodes()` | 「哪个块该开几条渠道」这类策略 |
| `daemon.py` | 调度与 IO；`_block_night_tags()` 只做缓存与异常兜底 | 窗口算术、白名单规则 |

**为什么把夜锁拆出去**：不是为了行数，是为了测试面。留在 daemon 里时，验一个
「窗口反了会怎样」要先 import 8270 行、再伪造 `_warp()` / `_clash_snapshot()` /
`active_warp_tag()` 三处状态；拆出来后 `test_warp_blocks.py` 里四行断言就够，
且能在任意时区跑。

**没有拆的**：IP 台账（`.warp_ip_used` / 历史 / cycle）、探测缓存、隔离台账。
它们每一个都在热路径上读写模块级全局 + 锁，拆出去要先引入状态对象和注入，
那是第二轮的事 —— 一次动太多，出问题时无法判断是新逻辑还是搬家搬错了。

## 九、44 个「死」节点的复测（2026-11-01）

### 为什么这次换探针

上一轮的结论（「18/18 握手 3 轮全失败」）出自 `/tmp` 里一份**自己拼 sing-box
配置**的脚本。它拼出来的东西和 `warp_wg.build_line_config` 不是同一份：

| 差异 | 后果 |
|---|---|
| DNS 换成 DoH `custom.juhazf.cn` + `domain_suffix` 规则 | 单线里没有 `dns-airport` 定义，节点入口解析不到 |
| 节点 tag 改成 `node` | 与 `detour` 对齐，看着没问题，但掩盖了上面那条 |
| 端口用「基准 + 偏移」而不是让内核分配 | 与残留进程撞端口，报「端口没起来」也记成握手失败 |
| `warp_rounds` 里的 `null` 不分阶段 | 「配置错」「端口没起来」「握手失败」三件事一个桶 |

结果就是 **44 个节点全灭**，其中 15 个的 UDP:53 明明是通的。这不是节点的问题，
是探针在测自己 —— 同一个坑当天上午刚在订阅解析里踩过（TEST_PLAN v1.57）。

### 探针的三条规矩（写进 `warp_diag.py`）

1. **配置只有一份**：只调 `warp_wg.build_line_config`，不复制。
2. **失败分四类**：`config`（进程没起来）/ `port`（没绑上）/ `handshake`
   （隧道起不来）/ `ok`。只有后两类能用来判节点。
3. **先跑对照组**：3 个已知可用的节点必须 `ok` 且 colo 与台账一致，否则停手。

工具本身不认识「哪个节点能跑 WARP」，它只负责把 `trace` 的 `warp=on` 和失败
阶段如实记下来；判据留给 `warp_blocks.build_map`（按出口 IP 聚合、只数握手层轮次）。

### 复测暴露的真问题：单线配置没有机场 DNS

第一轮复测（18:44）拿到的失败长这样：

```
ERROR endpoint/wireguard[warp]: connect to server: failed to create session:
  lookup hk-aws-122.acczzz.com: (exchange6: NXDOMAIN)
```

`build_line_config` 的 DNS 段只有 `dns-default`（本机解析器），而
`hk-aws-*.acczzz.com` / `cn.bing.com` / `us.bing.com` 这些**机场拿别人的域名当
跳板**的入口，公网 DNS 上是 NXDOMAIN 或被解析到真 Bing。主配置由
`parse_subscription` 补 `domain_resolver: dns-airport`，单线配置没有 —— 于是
「实测能承载 WARP 的节点，一写成线就连不上」。

修法：
- `AIRPORT_DNS_SUFFIXES / _URL / _TAG` 从 `parse_subscription.py` 上移到
  `config.py`（两边都要用，不能让 `warp_wg` 反向依赖一个订阅解析脚本），
  `parse_subscription` 保留同名别名，既有用例零改动；
- `build_line_config` 的 DNS 段加 `dns-airport`（DoH，`domain_resolver:
  dns-default`，不带 `detour` —— 带上会 `detour to an empty direct outbound
  makes no sense`，实测过）+ 一条 `domain_suffix → dns-airport` 的规则；
- 节点出站的 `domain_resolver` 由 `prepare_node` 统一决定（机场域名 → 机场 DoH，
  其余 → 本机解析器）。探针也调它，不再自己写一份。

这条修复对**块模型**是前置条件：`block-map` 里排在前面的节点大量是机场域名
（`cn.bing.com:443~16-any` 就是 NRT 常规 0 号），补 DNS 之前一旦 `enabled=True`，
写出来的线会整批 NXDOMAIN。

### 复测结果（2026-11-01 19:57，44/44 完成）

**结论维持：44 个节点全部不能承载 WARP —— 但这次是有效结论。**

| 项 | 数 |
|---|---|
| config / port（探针自身故障） | **0 / 0** |
| handshake（隧道起不来） | **44 / 44**，每个 3 轮全灭 |
| 对照组（3 个已知可用节点） | 2 个 `ok`（colo 与台账一致：LAX / NRT），1 个（`cn.bing.com:443~7-any`）复测也失败 → 从候选剔除 |

| 协议 | 数 | TCP 活 | UDP:53 可转发 |
|---|---:|---:|---:|
| anytls | 38 | 35 | 9 |
| vless | 5 | 5 | 5 |
| trojan | 1 | 1 | 1 |

三个值得钉住的点：

1. **「UDP:53 能转发」推不出「能承载 WARP」**。44 个里 15 个 DNS UDP 是通的，
   WARP 握手照样 3 轮全灭。机场普遍只放行 53 端口的 UDP，2408 出站被单独掐。
   旧探针「裸 UDP 打 2408」恒 False 的教训同理：判承载能力只能真建隧道。
2. **5 条 vless 也死**，与「vless 原生 UDP 转发所以能跑 WARP」的先验不符。
   实测过换 WARP 端点（162.159.192.1 的 2408 / 1701 / 500 与 162.159.193.10:2408）
   全部不通 —— 机场掐的是 2408 这类非常规出站端口，不是 Cloudflare 拒绝。
   选落地节点时「vless」仍是必要条件，但不再充分。
3. **处置**：44 个不进块（0 次成功不入块），`block-map` 维持 51 节点 / 22 渠道
   （regular 33 / irregular 18）。这个判定已可复现：
   `warp_diag.py --job … --out diag44.jsonl --rounds 3` → `warp_blocks.py build-map`。

**第三次复测（21:00-21:52，用空闲设备 21/22）**：44/44 仍然全是 `handshake`，
0 个 config、0 个 port。这一轮的意义是排除了「设备身份打架」这个混杂因素 ——
前两次跑的时候探针借的是 line1..4 的线上设备（同一 WireGuard 身份不能两会话，
探针和生产线互踢）。换成两个从没在跑的槽位之后结论不变，说明这 44 个节点
确实不能承载 WARP，不是被线上线挤掉的。

以后跑这个诊断只需要一条命令，槽位自动挑（active 单元 + 线路台账之外的那些）：

```bash
python3 warp_diag.py --job /tmp/opencode/dead44_inputs.json --out /tmp/opencode/diag44c.jsonl --rounds 2
```

### 问题点定位（2026-11-01 22:30，最低延迟节点逐层拆）

挑延迟最低的两个（`jp-aws-117` 358ms、`cn.bing.com:443~18-any` 468ms），一层层
往里剥。表里是「经这个节点能不能拿到回包」：

| 探测 | 目标 | 结果 |
|---|---|---|
| DNS UDP/53 | 8.8.8.8 | ✅ 71B |
| STUN UDP/3478 | stun.l.google.com | ✅ 42B |
| STUN UDP/3478 | stun.cloudflare.com | ✅ 42B（**Cloudflare 自己的服务通**） |
| STUN UDP/**19302** | stun.l.google.com | ✅（端口不是 53/3478 也行） |
| STUN 包长 40→1400B | stun.l.google.com:19302 | ✅ 全通过 |
| 同一关联连发 5 个 | 同上 | ✅ OOOOO（关联不会被关） |
| WG 握手 148B | 162.159.192.1:**2408** | ❌ 一个回包都没有 |

再用 debug 日志看 sing-box 到底做了没有：

```
DEBUG endpoint/wireguard[warp]: peer(bmXO…fgyo) - sending handshake initiation
DEBUG ...: handshake did not complete after 5 seconds, retrying (try 2/3/4)
```

**发得出去，一个包都没回来。** 于是把「封端口」和「封网段」分开验，7 个端点全试：

| 端点 | 结果 |
|---|---|
| 162.159.192.1 : 2408 / 500 / 1701 / 4500 | ❌ 全不通 |
| 162.159.193.10:2408 | ❌ |
| 188.114.96.1:2408、188.114.97.1:2408 | ❌ |
| [2606:4700:103::2]:2408、[2606:4700:100::2]:2408（v6） | ❌ |

结论（也是这批节点的**共性**）：

1. 中继本身没问题 —— 任意 UDP 端口、任意长度、连续多包都通；
2. Cloudflare 也没问题 —— `stun.cloudflare.com` 走同一个节点是通的；
3. 唯一不通的是 **Cloudflare WARP 专属网段**（`162.159.192.0/23`、`188.114.96.0/23`）
   上的 2408/500/1701/4500，v4 v6 都封。

所以不是延迟、不是设备、不是配置，是**机场在这批出口上封了 WARP 端点**。机场
普遍这么干（防止用户拿它跑 WARP 白嫖带宽），客户端侧没有绕法 —— WARP 只有
UDP 这一条路，TCP 443 的 MASQUE（usque）又不支持指定上游代理。

**共性还表现在出口 IP 上**：95 个出口 IP 里，失败的 44 个 IP **没有一个**同时挂着
「能承载」的节点（混挂 0 个）。说明这是**中继主机级的策略**，不是协议或入口域名：
`cn.bing.com` 既有能跑的（~21/22/23，SIN 块）也有不能跑的（~18 等）——差别在它
当时被分到哪台中继，而不在节点配置。

**能做的只有两件**：① 继续用它们当普通代理（现状，已在 159 节点池里）；
② 定期重测——机场换中继/改策略时它们会自己活过来。两条命令都在工具里：

```bash
# 单个节点逐层拆解（UDP 层 + 3 个 WARP 端点，约 1 分钟）
python3 warp_diag.py --job /tmp/opencode/dead44_inputs.json --explain <tag>

# 全量重测（自动挑空闲设备，没空闲会拒绝运行）
python3 warp_diag.py --job /tmp/opencode/dead44_inputs.json \
    --out /tmp/opencode/diag44-$(date +%m%d).jsonl --rounds 2
```

### 端点全扫描（2026-11-01 23:05，网上案例验证）

社区案例（samnet.dev 的 WARP 指南）给了另一条路：Cloudflare 在 **5 个 /24** 上
开了 **50 个 UDP 端口**都接受 WireGuard 握手（854、878、891、894、908、928、939、
946、955、968、988、1010、1018、1074、1180、1387、1843、2371、2506、3138、3476、
3581、3854、4177、4198、4233、5279、5956、7103、7152、7281、7559、8319、8742、
8854、8886…）。如果机场只是按端口拦，换冷门端口就能救回来 —— 于是把
3 个网段 × 40 个端口 = **120 个组合**全扫了一遍（`jp-aws-117`，每组合一次真握手）：

**120/120 全部静默。**

连「换端口」「换网段」这两条社区公认的路都走不通，说明这批中继不是端口白名单，
而是把 Cloudflare WARP 的**整个地址空间**拉黑了。到这一步，客户端侧能试的路
（换端口、换 IP、换协议族 v4/v6、换包长、换设备）全部穷尽，剩下的只有：
① 当普通代理用（现状）；② 等机场换中继后重测；③ 换一家不封 WARP 的机场。

### MASQUE-over-TCP：差一步的路（2026-11-02 00:10）

44 个节点被封的只是 UDP，它们的 **TCP 443 到 CF 的 MASQUE-over-H2 端点
（162.159.198.2）完全畅通**（经节点 TLS 握手成功、HTTP/2 打开）。
如果 MASQUE 能走 TCP，44 个节点立刻变成 8 个地区的 WARP 承载，UDP 封锁
形同虚设 —— 这是它们「翻盘」的唯一通道，为此做了全套实验：

| 步骤 | 结果 |
|---|---|
| usque main 分支自编译（--http2） | ✅ 跑起来 |
| tollfree fork（独立 H2 实现，Go 1.25.5 自编译） | ✅ 跑起来 |
| fork 重新注册新设备 | ✅ 注册成功 |
| CONNECT-IP over H2 | ❌ **CF 一律回 400**（直连与经节点、新旧实现、新旧设备全一样） |

usque issue #32 里有对照：**官方客户端** `warp-cli tunnel masque-options set
h2-only` 在 WSL 里跑通了（warp=on，HTTP/2，走到维也纳边缘）——说明 CF 的
H2 MASQUE 服务是真的，只是 usque 两个实现的 H2 请求都被 CF 拒（400），
属于 usque 与 CF 协议实现之间的差距（connect-ip-go 原作者也明说没有计划
支持 HTTP/2，除非有人赞助）。

**后续若要继续**：把官方 WARP 客户端（warp-svc）装进 WSL，`warp-cli tunnel
masque-options set h2-only` 强制 TCP 模式，再放进网络命名空间逐节点路由
（命名空间方案已验证可行）。这是「44 个节点翻盘」的最后一公里，也是
一个独立的小项目，不是今晚一行配置能收尾的。

工具与脚本已留档：`/tmp/usque-h2b`（fork 自编译二进制）、
`/tmp/relay44.py`（443 经节点转发器）、`/tmp/usque_h2_test.sh`（实验脚本）、
`/tmp/ns44_setup.sh`（命名空间方案，未用上因 WSL 无 iptables）。

### 突破：44 个「死」节点改用 MASQUE 就活了（2026-11-02 00:30-01:00）

44 个节点被误判的全过程与翻盘方案，这一节是本日最重要的修正：

**误判链**：
1. 第一版探针拿 line1..4 的线上设备探测 → 设备互踢，结论全废
2. WG 握手 3 轮全失败 → 判「节点死」。真因：**中继识别并丢弃 WireGuard
   协议特征**（WG 的 UDP 流量特征极好认），不是不转 UDP —— DNS/STUN 都能过
3. **MASQUE（HTTP/3/QUIC）长得和普通 QUIC 流量一样，中继不拦**

**翻盘架构**（已验证，2026-11-02 00:28 首例 warp=on colo=NRT）：

```
usque socks（QUIC 模式, endpoint=127.0.0.2:444xx）
  → UDP 桥（127.0.0.2:444xx → 节点 SOCKS5-UDP）
    → 机场节点 → CF MASQUE 端点(162.159.198.2:443) → WARP 出口
```

全部是 TCP/QUIC 常规流量，机场的 WG 特征过滤形同虚设。usque 不支持上游
代理，所以桥（`/tmp/bridge44.py`，约 80 行）负责把 usque 的 QUIC 包经节点
的 SOCKS5-UDP 转发，回包从 127.0.0.2:444xx 原路送回（QUIC 要求源稳定）。

**抽样验证（8 节点，各配一台 usque 设备 usque10-17）**：

| 结果 | 数量 | 明细 |
|---|---:|---|
| ✅ warp=on | **2** | cn.bing.com~18（NRT）、us.bing.com~5（LAX）|
| ⏳ QUIC 无回包（中继连 QUIC/443 也拦或节点抖动） | 4 | cn.bing.com~25/28/30、hk-aws-54 |
| ❌ 测试实例端口冲突 | 1 | usque12（11452 被旧实例占用，测试事故）|
| ❌ 节点本身 TCP 断 | 1 | us.bing.com~7（poolcfg/46 探测 000）|

对比：同批节点用 WG 测是 **0/8**。关键坑（都是亲手踩的）：
- usque 的 `-P` 是**远端端点端口**，不是本地端口；endpoint 要改
  `endpoint_v4`（QUIC 模式读它，不是 endpoint_h2_v4）
- usque 的 SOCKS 在隧道断开时**回落直连**（trace 显示 warp=off + 本机 IP，
  别被它骗成「通了但没 WARP」）
- 闲置一个月的设备要重新 `enroll`（token 过期，CF 静默丢包）
- curl 的 `--noproxy "*"` 会把 `--proxy` 一起禁掉（测试脚本连环踩坑的根源）

**生产化路径**：每节点 3 进程（节点socks + 桥 + usque），44 节点全量约 130
进程，端口规划 200xx/444xx/114xx；出口即 socks 端口，直接并入 selector。
抽样通率 2/8 是首测（含测试事故），重试与逐节点调优后预计更高 —— 且这
只是 8 个节点的样本，44 个里 anytls 的 38 个都可能这样救活。

### 自我纠错：「WG 特征被拦」只对一半的节点成立（2026-11-02 01:50）

上一节把「改用 MASQUE 就活了」当成了 44 个节点的普遍规律 —— **这个外推是
错的**，被下一轮全量实测推翻。主机指出后做了分层探测，真相是两层：

| 节点类型 | UDP 转发 | WG 真握手 | MASQUE | 占比 |
|---|---|---|---|---|
| **A 类**（中继转发 UDP）| ✅ | ❌（jp-aws-117 上 120 个端口组合全静默）| ✅ | 少数 |
| **B 类**（中继不转发 UDP）| ❌ | ❌ | ❌ | 多数 |

判定手段（`tools/probe_udp_quic.py` + `/tmp/udp_cmp.py`）：对同一节点的
SOCKS5-UDP 打四个不同目标（DNS 8.8.8.8/1.1.1.1、STUN google:19302、STUN CF:3478），
避免「只放行自家 DNS」造成误判：

```
失败节点 cn.bing.com:443~2   DNS✗ DNS✗ STUN✗ STUN✗   → 根本不转 UDP
成功节点 cn.bing.com:443~18  DNS✓ DNS✓ STUN✓ STUN✗   → 转 UDP
```

于是三个结论要改：

1. **「WG 特征被识别拦截」只在 A 类节点上成立**。它的证据是硬的：jp-aws-117
   上 3 网段 × 40 端口 = 120 个组合真握手全静默，但同一节点 MASQUE 通（NRT）。
   把这个现象外推到全部 44 个节点是错的 —— B 类连 UDP 都不转，换什么协议都救不回。
2. **真正的必要判据是「节点转不转 UDP」**，比「WG 通不通」更靠前、也更好测
   （一次 UDP 探测几秒，真建隧道要 25 秒）。先筛 UDP，再上 MASQUE，能把
   无效探测从 34 次压到个位数。
3. **单次判定有假阴性**：hk-aws-53 第一轮判失败、全量轮判成功（→HKG）。
   失败样本里大概率还藏着能用的节点，值得第二轮重试。

**全量扫描结果（44 节点，串行，每节点一次）**：

| 结果 | 数量 |
|---|---:|
| ✅ warp=on | **4** |
| ✗ MASQUE 未建立 | 34 |
| ✗ 节点 TCP 已死（跳过探测）| 6 |

```
✓ hk-aws-53.acczzz.com:443-any    HK  colo=HKG
✓ jp-aws-117.acczzz.com:443-tr    JP  colo=NRT
✓ cn.bing.com:443~18-any          JP  colo=NRT
✓ cn.bing.com:443~19-any          JP  colo=NRT
```

对照：同批节点用 WG 测是 **0/44**。所以 MASQUE 确实把「WG 必死的节点」救回来
了 4 个（含一个 HKG，填补了之前 colo 覆盖的空白），但它**不是** 44 个全救的
万能钥匙 —— 收益是 4 条新出口，不是 44 条。

工具已入库：`tools/masque_bridge.py`（UDP 桥）、`tools/scan_masque_nodes.py`
（全量扫描）、`tools/probe_udp_quic.py`（UDP/QUIC 分层探测，用于先筛 A 类）。

### A 类筛选后复测：最终 6/44（2026-11-02 02:30）

按「先筛 UDP、再上 MASQUE」的两步法重跑（`/tmp/screen_udp.py` 并发筛 + 串行
真建隧道，失败者隔一轮重试并重新 enroll）：

| 步骤 | 结果 |
|---|---|
| UDP 分层筛选（44 节点，并发 8，~2 分钟） | **A 类 12/44** |
| A 类 MASQUE 第一轮 | 5/12 |
| A 类失败者第二轮（带 enroll） | 0/7 |

**最终清单（6/44，全部实测 warp=on）**：

```
✓ hk-aws-53.acczzz.com:443-any     HK  colo=HKG   （间歇可用：一轮成二轮败）
✓ jp-aws-110.acczzz.com:443-any    JP  colo=NRT   （此前被「TCP死」误判，实为 root 假死）
✓ jp-aws-117.acczzz.com:443-tr     JP  colo=NRT
✓ cn.bing.com:443~18-any           JP  colo=NRT
✓ cn.bing.com:443~19-any           JP  colo=NRT
✓ unamecf3.xn--ghqu5fm27b67w.com:443~8-vl  US  colo=LAX
```

两个教训钉死：

1. **root 跑 sing-box 假死**把 jp-aws-110 冤判了整整一轮 —— 测试基建的
   环境问题会伪装成节点问题，先验证基建再怀疑节点。
2. hk-aws-53 一轮成、一轮败：**中继对 CF UDP 的放行是间歇的**，单轮判定
   的置信度有限；但这批「转 UDP 却建不起 MASQUE」的（cn.bing.com~11/~17、
   downloadcfpro 系列 5 个）两轮都失败，更像是中继对 UDP/443→CF 有针对性
   处理（与 STUN CF:3478 在 A 类节点上也普遍不通相印证）。

净收益：44 个判死节点里救回 **6 条真实出口**（HKG×1 / NRT×3 / LAX×1，
比之前 51 节点的 colo 版图多出 HKG），成本是每节点 3 个常驻进程。

### SNI 改写实验：原理性不可行 + 黑名单范围（2026-11-02 03:00）

主机追问「社区有没有办法」后补的三块拼图：

**1. 黑名单只匹配一个域名**（SNI 对照，全部经节点到 162.159.192.1）：

| SNI | 结果 |
|---|---|
| consumer-masque-proxy.cloudflareclient.com | ❌ 掐断（唯一被黑） |
| **engage.cloudflareclient.com**（解析到 162.159.192.1，WARP API 域名） | ✅ 真 CF 证书 |
| api.cloudflareclient.com | ✅ 真 CF 证书 |
| *.pages.dev / *.workers.dev / *.cfargotunnel.com | ❌ 掐断（CF 自家服务被针对） |
| discord.com / www.apple.com | ✅ 真站点证书 |

**2. SNI 改写转发器（tools/sni_relay.py）做到字节级正确**
（解析 ClientHello、改 SNI、修 5 层长度字段，单测回读一致），
端到端结果是 `bad record mac` —— **TLS 1.3 的 Finished 校验覆盖
ClientHello 本身**，改写后客户端与服务器 transcript 不一致、密钥派生
失败。这是原理性障碍：**端到端 TLS 下，中间人改 SNI 必然破坏握手**。
社区常用的 domain fronting 之所以曾经可行，是因为 SNI 与 HTTP Host
分离（HTTP 层不受 transcript 保护）；而 MASQUE 的 :authority 与 SNI
绑定由客户端决定，客户端不给口子。

**3.5 中继层已突破，死结收窄到 usque 的 H2 协议（2026-11-02 03:55）**

主机点破「usque 的 SNI 本来就能改」后补测了从未测过的组合：
**usque(fork) +  + 纯 TCP 转发器经节点**——
结果错误从 （中继掐断）变成 ：**TLS 握手成功、中继层完全通过**
（客户端自己改 SNI，transcript 一致，这是合法改法，与 sni_relay 的中间人
改写有本质区别）。至此死结精确表述为：

* 中继层：✅ 已解决（SNI=engage 放行，且 engage 解析到 MASQUE 端点本尊）
* warp-svc：协议 ✅ / SNI ❌（硬编码被黑，无口子）
* usque：SNI ✅（-s）/ **协议 ❌（CONNECT-IP over H2 被 CF 400，与
  SNI 无关——同 SNI 下 QUIC 模式 warp=on，H2 就 400）**

两个客户端各缺半边。让 TCP 路线复活只剩一条技术路径：**修 usque fork 的
H2 CONNECT-IP 构造**（对照 warp-svc 的抓包逐字段对齐 :authority/:protocol/
capsule 等），工程量中等；或等上游（issue #32）。

**3. 死结的完整表述**：

* warp-svc：协议正确，但 SNI 硬编码且无配置口子 → 被 SNI 路由器黑
* usque：有 `-s` 改 SNI（客户端自己改，transcript 一致，TLS 能过），
  但 H2 的 CONNECT-IP 被 CF 400（协议实现差距，等上游）
* 所以 TCP 路线在「SNI 路由器 + 这个黑名单」组合下无解，除非：
  a) usque 修好 H2（跟踪 issue #32）；
  b) 出现「解析到 WARP 端点、且不被黑」的第三方域名（SNI 路由按域名
     解析连接真实 IP —— engage 就是现成例子，但它属于 CF 官方域名，
     若 CF 未来给 WARP 端点挂多个别名，可直接用 sni_relay.py 接上）。

**社区对「SNI 路由器」的成熟绕法**（评估记录，待主机拍板）：
vless+ws+tls over CF Workers（BPB Panel 等 10K+ stars 项目）—— 客户端
SNI 用自己的 Worker 域名（正常 CF 站点，路由器放行），Worker 在 CF
内部接流量并从 CF 的出口 IP 出。44 个节点的 TCP 全部可用作入口线
（SNI 路由器对正常站点是放行的），出口换成一族全新 IP 池；需要一个
CF 账号。自有 VPS + vless/wstunnel 同理（SNI=自己域名，中继按域名
送到真实站点 = 自己的 VPS）。

### 定性：这家机场的 TCP 中继是 SNI 路由器，TCP 路线客户端侧无解（2026-11-02 02:45）

对 44 个节点全量做「SNI×IP」矩阵测试（SNI 取 CF 真域名 / 机场白名单域名 /
知名域名 / 未知域名，IP 取 162.159.192.1，全部经节点），证据链：

| SNI | 结果 | 说明 |
|---|---|---|
| consumer-masque-proxy.cloudflareclient.com | ❌ decode error / eof | **全局黑名单**，44/44 一致 |
| www.apple.com / www.twitch.tv | TLS 通，但回**真 Apple / 真 Twitch 证书** | 连接被导向真实站点，目的 IP 被无视 |
| cn.bing.com（机场白名单）| TLS 通，回 Microsoft 的 bing 证书 | 中继伪造/劫持 |
| 不存在的域名 | ❌ 掐断 | |
| 无 SNI（IP 直连）| 192.1 回 handshake failure | |

即：**中继按 ClientHello 的 SNI 决定去向，目的 IP 完全不参与路由**。
全量判定分布：SNI路由 39 / TCP死 4 / 异常 1 → **透明转发 0/44**。

这一定性同时解释了此前所有现象：

* **为什么 UDP 桥能通**：UDP 没有 ClientHello，中继对 UDP 只能按目的 IP
  透明转发 → usque 的 QUIC 直达 162.159.198.2 的 MASQUE 服务。
* **为什么 warp-svc 的 H2 必死**：它的 SNI 是硬编码的
  `consumer-masque-proxy.cloudflareclient.com`，正好踩黑名单，且客户端
  不给改 SNI 的口子；转发器做 SNI 改写也没用 —— 改成任何域名，中继都
  不会把连接送到 162.159.192.1（SNI 路由不看 IP），而是导向那个域名的
  真实站点，那里没有 MASQUE 服务。
* **为什么 usque 的 H2 直连是 400**：直连不经过中继，SNI 直达 CF，
  TLS 与 HTTP 层都正常到达 —— 400 是 CF 对 usque 两实现 CONNECT-IP
  构造的拒绝，与中继无关（故 usque 侧无解，等上游）。

TCP 路线的剩余可能性只有一条：找到一个「解析到 WARP 端点 IP、且不在
黑名单」的域名当 SNI —— 中继会按域名解析连到真端点。但这要求该域名
恰好解析到 162.159.192.0/24 且中继黑名单只匹配 cloudflareclient.com
后缀，基本不存在；若未来发现此类域名，用 `tools/tcp_relay.py` 加
SNI 改写即可接上。

**结论：这个机场上，WARP 借道的唯一通道是 UDP（QUIC 桥），上限 6/44。**

### 走 HTTP/2 over TCP 的那条路：通到最后一公里被 SNI 拦（2026-11-02 02:10）

主机一句「不是走的 http 吗」点破了绕远的地方：MASQUE 本来就是 HTTP，
**HTTP/3 跑在 UDP 上，但 HTTP/2 跑在 TCP 上** —— 而这批节点的 TCP 是全通的。
于是把官方客户端的 H2/TCP 路线走了一遍，全程记录如下（含可复用的接口）：

| 步骤 | 结果 |
|---|---|
| `warp-cli tunnel masque-options set h2-only` + connect | ✅ `warp=on`、`http=http/2`（纯 TCP，UDP 没参与）|
| 它连的端点 | TCP **162.159.192.1:443** |
| `warp-cli tunnel endpoint set <IP:PORT>` | ✅ 存在，可强制指定端点（无需 iptables）|
| 转发器收到连接 | ✅ 3-5 条/次（说明 warp-svc 确实经节点发出去了）|
| MASQUE 建隧道 | ❌ `tls handshake eof` |

**关键坑（都是实测撞出来的，写下来省得再撞）**：

1. **endpoint override 会忽略自定义端口**。日志里写着
   `Warp endpoint override ports ports=[443, 500, 1701, 4500, 4443, 8443, 8095]`，
   无论 `set` 里写什么端口，它只用这份列表去连给定 IP。所以转发器**必须监听 443**，
   写在 9450 上不会有连接进来（第一次试就是这个原因全空）。
2. **WSL 的 iptables nat REDIRECT 是摆设**：规则能加进去、`-L` 能看到，
   但实际不生效（转发器零连接）。想改向别用 iptables，用 `tunnel endpoint set`。
3. **root 跑 sing-box 会让节点全部假死**：`/root/.singbox_secret` 缺失导致机场
   DoH 失败，同一个节点 root 跑是 000、原用户跑是 204。测试脚本里必须用
   `sudo -u administrator` 起 sing-box，否则会把「root 环境问题」误判成「节点死」。

**失败原因定性**：转发器侧有连接、warp-svc 侧 `tls handshake eof` —— 与
之前观察到的一致：**机场中继按 TLS SNI 拦 MASQUE**（`consumer-masque-proxy.cloudflareclient.com` 被 `decode error` 掐断，IP-SNI 则畅通）。官方客户端
不给改 SNI 的口子，而 TLS 是端到端的、转发器无法代改（改了就破证书固定）。
抽 6 个节点（含 B 类）全灭：**TCP 路线 0/6**。

所以 MASQUE 借道的天花板由中继策略决定：

* **放行 UDP 的中继** → QUIC 路线可行，全量实测 **4/44**（HKG / NRT×3）
* **按 SNI 拦 MASQUE 的中继** → TCP 路线也走不通（本次抽样 0/6）
* 剩下的绝大多数中继：UDP 不转、SNI 也拦 → 两条路都堵

工具：`tools/tcp_relay.py`（转发器）、`/tmp/h2_batch.py`（串行换节点验证）。

### 连通性 ≠ 承载能力（2026-11-01 22:20 补测）
主人问的一句话点到了要害：「如果有联通的，代表一定有能够连接的，只不过延迟大」。
于是补了两组测量，把两个口径分开摆：

| 口径 | 方法 | 结果 |
|---|---|---|
| 普通连通性 | Clash API `GET /proxies/{tag}/delay`（8s 超时，gstatic 204） | **39/44 可连**；延迟 min 593 / 中位 1584 / p90 4393 / max 6851 ms |
| 承载 WARP | `warp_diag.run_round(..., wait_warp=120)`，单轮窗口从 20s 拉到 120s | **0/44**；三个代表节点（快/中/慢）全部停在 `read packet: use of closed network connection` |

结论站得住，而且不是「慢」：

- 窗口从 20s 拉到 **120s**（足够 12 次以上握手重传），失败点一模一样 ——
  WARP 端点的 UDP socket 每 8~10s 被关一次，**一个包都没回来**。
  纯延迟问题会在窗口内完成握手（WireGuard 只要 1~2 个 RTT，就算 7s RTT 也在 20s 内）。
- 这 44 个里 15 个的 **UDP:53 是通的**（能转发 DNS），说明节点不是不转 UDP，
  而是**不转非 53 的 UDP**；换 WARP 端点（500 / 1701 / 另一台 2408）同样不通。
- 所以「连通」只证明它是个合格**代理**，不证明它能当 WARP **承载**。这两件事
  在生产里也一直分开记录：`proxy` 池 159 个节点里就有它们，而 WARP 线只挑
  能建隧道的 51 个。

**这 44 个现在怎么用**：作为普通代理继续留在池里（延迟中位 1.6s，本来就参与
`proxy` 兜底与 9router 的出站轮换），只是不进 `block-map`、不占 WARP 设备。

数据留档（都在 `/tmp/opencode/`，重启即失，要长期保留就拷进 `.warp/`）：
`diag44.jsonl`（第二次，借产线设备）、`diag44c.jsonl`（第三次，空闲设备，最新结论）、
`diag44-dns-broken.jsonl`（作废探针的留档，勿引用）、`diag_ctrl2.jsonl`（对照）。

### TCP 路线反转：中继可承载 MASQUE，usque H2 修复已生效（2026-11-02 03:30）

之前那句「TCP 路线 0/44 死」是**错的**，错在把「SNI 路由器」当成了按 TLS SNI 拦截。
这一轮把三层都打通了，结论是：**这条机场的中继能承载 WARP over TCP，usque 的 H2
实现只差最后一点对齐**。

**1. usque H2 的真 bug（已在 fork 修掉）**

之前 usque H2 走 CF 一直 400，根因不在 SNI 而在协议：connect-ip-go 的 `DialH2`
用 `http.MethodConnect` 发的是**普通 CONNECT**（RFC 7540），没有 `:protocol` 伪头，
CF 的 MASQUE handler 直接 400。但 Go 的 `golang.org/x/net/http2` 客户端在
`req.Header` 含有 `:protocol` 时会发 **RFC 9298 扩展 CONNECT**（transport.go:1451
`if req.Method=="CONNECT" && req.Header.Get(":protocol")!=""`）。

两处修复（fork `Diniboy1123/usque`，提交 `4d47f9e`）：

* `api/masque.go` H2 分支给 `h2Headers` 加 `:protocol: connect-ip`；
* `internal/consts.go` 的 `ConnectURI` 从 `cloudflareaccess.com` 改为
  `consumer-masque-proxy.cloudflareclient.com/`（正确的 `:authority` + 非空 `:path`）。

**2. 中继黑名单的真相（关键反转）**

机场节点是 SNI 路由器，但黑名单是按 **SOCKS 目标主机名** 匹配的，**不是按 TLS SNI**。

| 方式 | SOCKS 目标 | TLS SNI | 结果 |
|---|---|---|---|
| `curl --proxy socks5h://`（目标=主机名）| `consumer-masque-proxy.cloudflareclient.com` | 同 | ❌ 掐断 |
| `curl --proxy socks5:// --resolve ...:162.159.192.1`（目标=IP）| `162.159.192.1` | `consumer-masque-proxy.cloudflareclient.com` | ✅ TLS 成功，拿到 MASQUE 证书 |

即：中继转发器只要把 SOCKS 目标设成 **IP**（`tools/tcp_relay.py` 的 `--dst` 本就是
IP），就不触发黑名单，真正的 MASQUE SNI 能原样送达 CF。之前所有「TCP 全灭」的测试
都误用了 `socks5h`（目标=主机名），自己踩了黑名单。

**3. 验证链（经节点，SOCKS 目标=IP）**

* `SNI=engage` + `:protocol` → `extended connect not supported by peer`
  （engage 是 WARP **API** vhost，不做 MASQUE，证据铁证）；
* `SNI=consumer-masque-proxy` + `:protocol` + SOCKS目标=IP → **不再报 not supported**
  （已进入 CF 的 MASQUE vhost，它确实支持扩展 CONNECT），但 CF 把 CONNECT-IP 请求
  以连接层 `EOF` 关闭。

**4. 剩余 gap（很窄，不再是死结）**

CF 的 MASQUE vhost 已可达且支持扩展 CONNECT，CONNECT-IP 请求被 `EOF` 拒。
设备用的是 `usque10`（属 `DEVICES=[10..20]` 池，同池设备 QUIC 已实测 `warp=on`），
所以**不是设备鉴权问题**，而是 usque H2 的 CONNECT-IP 请求格式/会话细节与 QUIC 路径
有差异。最可疑的一点：QUIC 的 `connectip.Dial(..., "cf-connect-ip", ...)` 传了协议串
`"cf-connect-ip"`，而 H2 的 `DialH2` 无对应参数（H2 用 `:protocol: connect-ip` 等价，
但 CF 在 H2 上可能还要别的字段，比如 WARP 会话 token 的承载方式）。

**结论**：TCP over 中继承载 WARP **是可行的**，usque 修复已生效（顺带让 usque 在
「MASQUE SNI 不被拦」的网络上 H2 直接可用）。最后这一步是把 usque H2 的 CONNECT-IP
请求逐字段对齐 QUIC / 官方 warp-svc 抓包，工程量中等、方向明确。

> 注：usque 是上游第三方 fork，修复在 `/tmp/usque-fork`（本地提交 `4d47f9e`），
> 真正接入产线还需把它 vendored/替换为带补丁的构建。

### H2 路线终局：CF 的 MASQUE 只在 HTTP/3，H2 是私有实现（2026-11-02 03:50）

上节「只差最后一点对齐」的乐观判断被推翻。这一轮把 usque 的参数搞对（`-P` 是
**endpoint 端口**、`-p` 是 SOCKS 端口，之前混用了）后重测，拿到确定结论：

**1. usque H2 直连 CF（不经任何中继）也失败**

```
$ usque-h2c socks --http2 --insecure --always-reconnect \
    -s consumer-masque-proxy.cloudflareclient.com -c usque10/config.json -P 443 -p 11601
Failed to connect tunnel: failed dial connect-ip over HTTP/2:
  connect-ip: failed to send request:
  Connect "https://consumer-masque-proxy.cloudflareclient.com/":
  net/http: extended connect not supported by peer
```

注意 `--always-reconnect` 是必需的：SOCKS 模式下 TUN 设备不会有流量，不开这个
flag 隧道永远停在 `Tunnel idle`（这是 usque 的行为，不是 bug）。

**关键**：这是**直连 162.159.198.2:443**，完全绕开了机场中继。所以
`extended connect not supported by peer` **与中继无关**——是 CF 这个 vhost
在 H2 上**根本不发 `SETTINGS_ENABLE_CONNECT_PROTOCOL`**。

**2. 中继侧用 curl 做的 SNI 矩阵，印证同一结论**

| SNI（SOCKS 目标=162.159.192.1，绕过主机名黑名单）| curl 结果 | 含义 |
|---|---|---|
| `consumer-masque-proxy.cloudflareclient.com` | **530 / error 1016** | TLS 通，CF 报「源站 DNS 失败」 |
| `consumer-masque.cloudflareclient.com` | reset | 被掐 |
| `consumer.cloudflareclient.com` | reset | 被掐 |
| `cloudflareclient.com` | **522** | TLS 通，CF 报「源站超时」 |
| `engage.cloudflareclient.com` | **403** | TLS 通，正常 API 响应 |

`error 1016` 是 Cloudflare 的「源站 DNS 解析失败」。也就是说：`consumer-masque-proxy`
这个 vhost 在 CF 的边缘**没有配 MASQUE 源站**，它只是个占位/内部域名。真正的
MASQUE 服务不在 H2 上。

**3. Go 的 ClientHello 被单独拦（次要但真实的坑）**

同一节点、同一目标 IP、同一时刻：

| 客户端 | SNI=consumer-masque-proxy | SNI=engage / discord / api |
|---|---|---|
| curl（ClientHello 1597 字节，带 X25519MLKEM768）| ✅ TLS 1.3 通过 | ✅ |
| Go（ClientHello ~300 字节）| ❌ `EOF` | ✅ |

中继对「小 ClientHello + 敏感 SNI」有指纹规则。但这已不重要——即使把 ClientHello
伪装过，CF 那边也没有 H2 MASQUE 服务。

**4. 最终定性**

```
QUIC / HTTP3：  ✅ CF 唯一真实提供的 MASQUE 通道 → 6/44 节点实测 warp=on
H2 / TCP：      ❌ CF 侧无服务（不是中继问题，不是 usque 问题）
```

usque 的 H2 实现本身没写错（我们补的 `:protocol` 是必要且正确的），但它**服务
的对象在 CF 侧不存在**。官方 warp-cli 的 `h2` 模式能跑，是因为它连的是另一个
内部端点（`consumer-masque.cloudflareclient.com`，且需要 WARP 会话 token 走
私有 header），公开的 `:protocol: connect-ip` 路径拿不到。

**所以「44 个节点的 TCP 全部变成 WARP 承载」这个设想不成立**——卡点不在机场，
在 Cloudflare。除非 CF 未来公开 H2 MASQUE 端点，否则这条路彻底封死。

**净结论不变**：这个机场上 WARP 借道的唯一通道仍是 UDP（QUIC 桥），上限 6/44。



