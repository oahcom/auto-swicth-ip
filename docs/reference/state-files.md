# 状态文件参考：谁写、谁读、哪个字段会被误用

> **本文回答什么问题**：项目里那十几个 `.json` 各是谁的账本？字段是什么意思？
> 哪些字段**看起来能用来判断 A，实际说的是 B**？

除特别注明外，所有文件权限 `600` 且 gitignored —— 里面含隧道凭据、出口 IP
台账这类不该进版本库的东西。

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
| `.dormant_state.json` | `dormant_probe.py` | `parse_subscription.py` | 沉睡池实测结果 |
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

## 六、`.dormant_state.json`

`dormant_probe.py` 写，`parse_subscription.py` 读。

**它是 `proxy` 池成员数在两台机器上差 5 倍的全部原因**（2026-11-03 实测：
本地 97 条 `alive:false` → 池子 25 条；远端没这个文件 → 120 条）。

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
