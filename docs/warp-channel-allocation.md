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

## 五、两阶段

### 第一阶段（现在）：全量纳入，白天晚上都跑
- 全部 9 个 colo 的全部节点，白天和晚上都参与
- 目的：测出每个 colo / IP 段的**夜间封禁率**
- 现在夜间数据几乎为零：HKG / TPE / ORD / PIT 是今天才出现的池子，没经历过夜间；
  NRT 12 段当前全封；PHX 24 段封 19

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

**22 条渠道 vs 第 82 行表格的 28 条**：差的 6 条是**本机直连 WARP** 的
`WARP-CF/CF-B/CF-D/CF-E/CF-F/WG-13`。它们不经代理出口，而 `block-map.json`
是由「代理出口 → 真建隧道 → trace 取 colo」生成的 —— 把直连线硬塞进这张表，
表里就会出现没有观测来源的 colo 行。它们继续按 `warp.members` 的老路走
（其中 LAX 两条是 v4 保底线，夜间白名单按「colo 未知 → 保留」自动留下）。

**这一轮明确没做的两件（别以为已经生效）**：

| 计划里的项 | 现状 | 为什么没做 |
|---|---|---|
| 延迟排序接 `_NODE_DELAYS` | `order_nodes(nodes, delays)` 接口在，`plan` CLI 传 `None`，`apply(delays=)` 也还没人传 | 延迟缓存在 daemon 内存里，没有落盘快照；要做就得让 `_health_pool` 定期写一份（新 IO），或者让 apply 去查 Clash API（进同步路径）。按第三节的规矩，宁可不接，也不把测量塞进换线路径 |
| `warp.night_extra_channels`（夜里额外加隧道） | 键不存在 | 「加隧道」等于临时增开 systemd 单元 + 新注册 WARP 设备，是部署动作不是运行期动作；夜里（20:00 后）做这件事的收益/风险都没测过。先用「白名单 + 现有 12 条线」跑几个夜晚，用夜间的产出率决定要不要扩 |

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

数据留档（都在 `/tmp/opencode/`，重启即失，要长期保留就拷进 `.warp/`）：
`diag44.jsonl`（最终）、`diag44-dns-broken.jsonl`（作废探针的留档，勿引用）、
`diag_ctrl2.jsonl`（对照）。



