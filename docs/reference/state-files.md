# 状态文件参考：谁写、谁读、哪个字段会被误用

> **本文回答什么问题**：项目里那十几个 `.json` 各是谁的账本？字段是什么意思？
> 哪些字段**看起来能用来判断 A，实际说的是 B**？

除特别注明外，所有文件权限 `600` 且 gitignored —— 里面含隧道凭据、出口 IP
台账这类不该进版本库的东西。**唯一例外是 `dormant-outbounds.json`（入库）**：
它是节点定义，必须跟着仓库走，否则新机器上沉睡池直接空掉。

> ⚠ **它入库的代价**：文件里有机场节点的明文 `password`（65 条 anytls 全带），
> 2026-10-28 手工固化时就没有脱敏，也已经进了 git 历史。仓库若哪天转公开，
> 要先轮换这批节点密码 —— 只从工作区删掉没用，历史里还在。

## 一、文件清单

| 文件 | 写入者 | 读取者 | 一句话 |
|---|---|---|---|
| `singbox-config.json` | `parse_subscription.py` | sing-box / daemon / Clash API | 唯一的生产配置 |
| `.warp/warp-outbound.json` | `deploy/members.py` / `warp_wg.apply` | `parse_subscription.py` | WARP 出口的素材 |
| `.warp/warp_wg_state.json` | `warp_wg.write_line` / `daemon._rehome_one` | `warp_wg` / daemon 轮回 | WG 线落地节点台账 |
| `.warp_line_state.json` | daemon | daemon | **每线状态**（最大的那份） |
| `.warp_ip_used.json` | daemon | daemon | 出口 IP 使用台账 |
| `.warp_ip_cycles.json` | daemon | 离线诊断 | 段样本（**只记错误终止的段**） |
| `.bad_nodes.json` | daemon（独占写） | daemon | 池节点黑名单 |
| `dormant-outbounds.json` | **手工固化**（非 gitignored） | `parse_subscription.py` | 沉睡池定义：okz 的历史快照，65 条 |
| `.dormant_state.json` | `dormant_probe.py` | `parse_subscription.py` | 沉睡池**实测死活**（`alive`） |
| `.pool_history.json` | daemon | daemon | 骤降护栏的基线样本 |
| `.warp_proxy_fallback.json` | daemon | daemon | 落池时刻（3h 冷却） |
| `.warp_segment.json` | daemon | daemon | 当前出口段起点 |
| `.warp/block-map.json` | 人工 / `warp_blocks.build_map` | `warp_blocks.channels()` | 块 → 节点归属 |
| `.warp/h2/channels.json` | `deploy/render_h2_lines.py` | `h2-install.sh` / `remote-deploy.sh` | H2 渠道 ↔ 设备 ↔ 端口 |
| `.warp/ipv6_segments.json` | 探测脚本 | `warp_blocks` | colo ↔ 地址段映射（实测数据） |

## 二、`singbox-config.json`

**唯一的生产配置**。`promote` 前必须过两道检查，缺一不可：

1. 骤降护栏（`new_n < 基线的一半` → 拒绝覆盖）
2. `sing-box check`（单个坏节点会让整个配置 FATAL，而一旦覆盖好池子就没了）

旁边还有一份 `<config>.stats.json`，由生成侧写，记录"这一次生成了什么"
（`sub_nodes` / `pool_nodes` / `dead_nodes` / `fallback`）。**护栏读的是这份，
不是自己数出站条数** —— 出站里有 50 条 WARP socks 是常量，按条数算闸门会被
抬到 66，订阅只剩 1 条节点时仍是一路绿灯。

> 手工跑 `parse_subscription.py --out /tmp/x.json` 不写统计（除非同时给
> `--stats`），这是故意的：试跑不该污染线上基线。

## 三、`.warp_line_state.json` —— 最容易被误读的一份

结构：`{"lines": {tag: {...}}}`，每条线一个 dict。字段语义见
`_LINE_DEFAULTS`（daemon.py:2749 附近）。

### 三个必须分清的时间戳

| 字段 | 谁写 | 含义 |
|---|---|---|
| `last_ok_ts` | 探测器 | 这条线的出口 IP **最近成功服务过**模型的时刻。`verdict=blocked/quota` 时会被**清零** |
| `ip` | 探测器 | 隧道**真的拿到了**出口地址才写。每次隧道重启会被 `_invalidate_warp_line_ip` 清空 |
| `restart_ts` | 重启路径 | 隧道最后一次重启的时刻 |
| `rebind_ts` | `_rehome_one` | **配置文件写回新落地节点**的时刻（不等于进程已重启） |

**最容易犯的错**：拿 `last_ok_ts` 判断"隧道通不通"。它说的是"这个**地址**
在模型层的表现"，不是"隧道建没建起来"。判断后者用 `ip`。

**第二个坑**：`rebind_ts` 只说明**文件写了**，不说明**进程重启了**。
`_line_restarted_after_write()` 就是补这个差的 —— 它要求 `restart_ts >=
rebind_ts`。少了它，渠道锁还原会每分钟重写一次配置，而 `rebind_ts` 一直被
顶新，同块轮回的"刚动过的线不判"就永久成立（2026-11-03 实测：1112 次
"还原重启未成功" vs 19 次成功）。

### `fail_streak` 的语义收窄过

**只数「拿不到出口地址」（`unknown`）**，不数 `quota` / `blocked`。

唯一消费点是"连续 N 次取不到出口地址 **且** 端口还开着 → 隧道半死"。
`quota` 说明隧道完全正常、只是这个地址被封，把健康线当半死线重启还有一层
后果：真故障被噪声埋掉（实测 WARP-WG-17 涨到 2457）。

### `ban_hold_*` 四个字段

`ban_hold_home` / `ban_hold_node` / `ban_hold_until` / `ban_hold_ready`，
渠道锁把线挪到低 ban 率 colo 时写。

**还原的三个结束条件**（缺一都会活锁）：

- `_line_node(tag) == home` **且** `_line_restarted_after_write(tag)` → 已达目的
- `_rehome_one` 返回 `gone`（原节点下架）→ 放弃还原，保留当前节点
- 重启成功 → 清 `ban_hold_*` **并且清 `ban_window`**

**`ban_window` 必须一起清**：不清的话，解封前那一小时攒的 31 条样本还在，
08:00 之后再记一条就直接重新锁到次日 —— "锁一天"退化成"永久锁定"（实测复现）。

## 四、`.warp_ip_used.json`

`{ip: {"ts": 最后使用时刻, "blocked": 是否被 403/429 标记}}`。

**必须落盘**：实测 22:22 重启后旧 IP 全变"干净"，22:27 立即切回已被烧的地址，
403 持续 144s。

**写入点只有两个**：换线切过去时（`blocked=False`）与触发换线时把当前出口
标 `blocked=True`。

`_warp_any_reachable()` 要求至少一条线出**未被标记**的 IP 才算可达 —— 否则
"连通探测全过、403 依旧 100%"（实测 22:36 切到某地址后仍 403 10/10）。

## 五、`.bad_nodes.json`

格式 `{"host:port|node-name": {"expire": int, "reason": str}}`。

**daemon 独占写入**（`proxy-global.js` 已不存在，无其他写入者）。
`_POOL_MIN_RATIO = 0.3` 是安全阀：允许 70% 节点被拉黑，再往下就"跳过拉黑
（保底）"—— 防止把自己清空。

## 六、`.dormant_state.json` 与 `dormant-outbounds.json`

这两个名字容易被混，它们管的是**同一批节点的两件事**：

| 文件 | 内容 | 谁写 | 谁读 |
|---|---|---|---|
| `dormant-outbounds.json` | 节点**定义**（65 条出站） | **手工固化**，2026-10-28 后再没变过 | `parse_subscription.py` / H2 渲染 / 各诊断工具 |
| `.dormant_state.json` | 这些节点**现在还活不活**（`alive: true/false`） | `dormant_probe.py`（`dormant-probe.timer`） | `parse_subscription.py` |

### 沉睡池到底是哪个订阅

**全部来自 `okz`**。2026-11-04 实测：

- 65 条 = **55 条 `bilibili-image-cdn.juhazf.cn:55001+`**（SNI `*.okzdns.com`）
  + **10 条 `*-aws-N.acczzz.com:443`**（SNI `*.hi2026.net`）—— 两个都是 okz 的域名
- tag 一律 `host:port-any` / `-tr`，**无 `~N` 后缀**（`~N` 是 `dasho` 那种
  「同一 host:port 挂多个节点」的源才需要的消歧后缀）

**它不是「已经消失的节点」，是同一批节点的旧入口**：按 (SNI, 协议) 对齐后，
okz 当前 63 条与沉睡池 **63/63 密码完全相同**。okz 只是把入口从
`bilibili-image-cdn.juhazf.cn:<5xxxx>` 换成了 `cn.bing.com:443` / `us.bing.com:443`，
后端与凭据没动。真正从订阅里消失的只有 2 条（`hk-aws-111` / `sg-aws-113`）。

所以它是一份**旧入口快照**：新入口（bing 前置）被墙时，旧的高位端口可以当备胎。
65 条里有 8~9 条与当前订阅同 tag（具体几条随 okz 当天给的入口变），
`make_config` 按 tag 去重后实际进池 **56~57** 条。

### 谁影响 `proxy` 池大小

池成员 = **当前订阅节点** + **沉睡池（去重后）** − **`alive:false` 的**。

**这是两台机器池子差近 7 倍的全部原因**（2026-11-03 实测）：

| | 订阅节点 | 沉睡池 | 判死摘出 | 在池 |
|---|---|---|---|---|
| 本地 | 62 | 57 | **97** | **25** |
| 远端 | 109 | 56 | **0** | **165** |

> **池子成员数 ≠ 不同后端数**。2026-11-04 实测本地池 123 条，按
> (TLS SNI, 协议, 密码) 归并只有 **62 组** —— 32 组是同一条后端挂了多个入口，
> 最多的一组（`i0.hdslb.com`）有 16 个入口（`upoz.` / `normal.` 等不同前置域名
> 与端口，密码相同）。
>
> 入口不同，只影响「连不连得上」（bing 前置 vs 直连域名 vs 高位端口），
> 不改变它是哪台机场后端。所以池子大小该读作「有多少条路」，不是
> 「有多少个可用出口」。（出口 IP 是否也相同未单独实测，但同后端同理。）

本地有 `.dormant_state.json`（97 条 `alive:false` 摘出池子），远端**根本没有这个
文件**（timer 装了但没产出），于是全量留在池里。远端订阅节点还多 47 条，那是
`dasho` 源的可及性差异，与沉睡池无关。

> **`stats.json` 的 `dead_nodes` 是「判死集合的大小」，不是「实际摘出条数」**，
> 所以减法**可能**对不上：11-03 那份是 62 + 57 − 97 = 22，而 `pool_nodes` 报 25；
> 11-04 那轮则刚好全对上（138 + 56 − 71 = 123 = `pool_nodes`）。两种都可能。
>
> 原因：`dead_nodes` 直接取 `len(sleeping)`，而 `_load_dormant_state()` 只按 `fp`
> 挡「tag 被别的节点占了」，**不要求 tag 当前还在**。节点从订阅和沉睡池里都
> 消失后，它的 `alive:false` 记录照样算进 `sleeping`，但摘它不改变池子大小。
> **要池子大小就看 `pool_nodes`，不要自己减。**
>
> 另一头：`dormant_source_nodes` 是「去重后**新增**的沉睡节点数」，也已经是
> `make_config` 算过的数，不是 `dormant-outbounds.json` 的行数（文件 65 行，
> 每次只有 56~57 条能进来 —— 差的是与当前订阅重名的那些）。

`alive:false` 的节点**出站仍保留**，只是不进 selector 成员。想让某条回到池子，
改状态而不是删文件 —— 删文件等于"把判死的节点又放回来"，且没有任何记录。

## 七、`.pool_history.json`

`{"samples": [{"ts", "pool"}]}`，24h 滚动窗口。

**只在 promote 成功后追加**，被拒的、试跑的都**不记**。判据是窗口内最大值，
不是上一次的值 —— 比"上一次"会随判死一路下移、护栏越守越松。

**连续 4 次被拦会喊话**：基线可能已过时。确认订阅真的永久缩水了，才删旧样本。
不自动放宽 —— 放宽就等于没有护栏。

## 八、`.warp/warp_wg_state.json`

`{"lines": {tag: {"node": 落地节点, "rebind_ts": ..., ...}}}`。

**会被 `warp_wg.apply` 与订阅换绑定整表重写**，把 `node` 抹回 home（实测
21:40 一次抹掉 10 条）。所以 `_rehome_held_lines` 每分钟对账一次。

## 九、临时与测试隔离

这些环境变量只为**测试隔离**存在，生产别设：

| 变量 | 隔离的文件 |
|---|---|
| `WARP_LINE_STATE_FILE_PATH` | `.warp_line_state.json` |
| `WARP_IP_USED_FILE_PATH` | `.warp_ip_used.json` |
| `WARP_IP_CYCLES_FILE_PATH` | `.warp_ip_cycles.json` |
| `WARP_PROXY_FALLBACK_FILE_PATH` | `.warp_proxy_fallback.json` |
| `SINGBOX_CONFIG` | `singbox-config.json` |
| `ASIP_LOG_FILE` | `daemon.log` |

**为什么必须有**：漏一个，测试就会把假事件写进生产账本。实测同一目录连跑
两次测试，第二次"通过"是靠着上一次落池的残留状态，不是真通过。

还有一道更硬的闸：`_side_effects_off()` —— 只有 systemd 拉起的进程
（有 `INVOCATION_ID`）才写文件，pytest / 审查脚本 / 手工跑一律只写 stdout。
**顺序有讲究**：先认"这是测试"，再认 `ASIP_ALLOW_FILE_LOG` 后门。反过来
的话，只要有人把后门写进 `.env`，pytest 就会重新往生产日志写假事件。

---

## 相关文档

- [config.md](config.md) —— 这些文件背后的配置项
- [../established-facts.md](../established-facts.md) —— 第 21~23 条（状态字段语义）、第 29 条（池子动态）
- [../subsystems/locks.md](../subsystems/locks.md) —— `ban_hold_*` 的完整流程
- [../subsystems/refresh.md](../subsystems/refresh.md) —— `.pool_history.json` 怎么用
