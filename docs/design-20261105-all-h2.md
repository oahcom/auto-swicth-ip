# 全量转 H2 承载 —— 需求与方案设计（2026-11-05 起草，2026-11-08 复核修订）

> 目标：把「WARP 出口」的承载方式从 WG(UDP) 为主转成 H2(MASQUE over HTTP/2) 为主，
> 减少常驻 sing-box 进程数。本文给出基线、耦合点、三个方案与改动清单。
> **结论先行：推荐方案 A（H2 为主 + 保留少量 WG 作补位层），不要一步到 0 条 WG。
> 但真正的瓶颈是设备额度与单 colo 池深，不是 sing-box 数量。**

**修订说明（11-08 复核）**：初稿的模型层 OK 率、单线产量、死线影响三处**统计口径错了**，
结论方向被带反。本版全部用「模型层结论行」重算，并用 block-map 实测 colo 复算池深。
下面标注 ~~删除线~~ 的是已被推翻的初稿数字，保留下来是为了不再犯同一个错。

---

## 1. 需求与可度量目标

| # | 目标 | 判据（迁移后 24h 实测） |
|---|---|---|
| G1 | 出口地址产量不下降 | 全池每日唯一出口 IP ≥ 迁移前 7 日均值（当前 24h：**全池去重 8,550**） |
| G2 | 单线产量不下降 | H2 单线 ≥ 300 唯一 IP/日（当前 H2 **205/线**、WG **135/线**） |
| G3 | 可用性不下降 | 模型层 OK 率与 WG 差距 ≤5pp（同小时对比） |
| G4 | 进程结构简化 | WG 独占 sing-box 进程数 22 → ≤6 |
| G5 | 可回滚 | 任一步骤回退后 10 分钟内恢复迁移前拓扑 |

**非目标**：不改 opencode 侧、不改 9router、不追求延迟下降（同 colo 实测无稳定差异）。

---

## 2. 现状基线（2026-11-08 实测，24h 窗口 daemon.log.2/.1/0 完整覆盖）

| 维度 | H2 | WG | CF（直连 MASQUE） |
|---|---|---|---|
| 成员 / 运行单元 | 31 / 31 | 22 / 22 | 9 / **5** |
| 实测落地 colo | **13 个** | 4 个（NRT/PHX/SIN/SJC） | 2 个（SJC/LAX），`loc=CN` |
| 候选节点池 | 71 节点 / 13 colo | **243** 个 UDP 可用节点 | — |
| 单 colo 池深 | HKG23 SIN15 NRT8 TPE7 SLC4 ICN3 LAX3 FRA2 LHR2，SJC/BOM/GIG/ORD **各 1** | 深 | — |
| 24h 模型层 OK | 665 / 6,674 = **10.0%** | 110 / 2,654 = **4.1%** | 82 / 296 = **27.7%** |
| 24h 出口获取次数 | 10,136 | 14,495 | 230 |
| 24h **唯一**出口 IP | **6,353**（205/线） | **2,978**（135/线） | 227 |
| 每次出口的**新地址率** | **62.7%** | **20.5%** | **98.7%** |
| 24h 重启尝试 | 10,862 | 15,245 | 178 |
| 换节点成本 | `PUT` selector，**不重启** | 写配置 + 重启该线 | 无 |
| 换地址成本 | `systemctl restart usque-h2-N`（新会话） | 重启该线 sing-box | 重启 usque |
| 单进程 PSS | usque 6.3MB × 31 | sing-box 11.8MB × 22 | usque 6.3MB × 5 |

**初稿错在哪**：把 `✓ <line> 重启后出口=<IP>`（拿到地址）当成了模型层 OK。
真正的结论行是 `✓/✗ <tag> 出口 <IP> 模型层: <verdict>`。重算后 ~~H2 65% / WG 81%~~
反转为 **H2 10.0% / WG 4.1%** —— H2 现在的模型层可用性是 WG 的 **2.4 倍**。
失败几乎全是 `quota`（H2 5992、WG 2509、unreachable 仅 17/35），说明差距来自
**地址新鲜度**，不是隧道质量。

**这才是转 H2 的真正理由**：不是省内存（见下），而是
**62.7% vs 20.5% 的新地址率**——同一条线每拿到一个新地址的概率，H2 是 WG 的 3 倍。

**资源账**：替换 22 条 WG = 少 22 个 sing-box（≈260MB）+ 多 22 个 usque（≈139MB）
⇒ **净省约 121MB，进程总数不变**。只为了「少部署 sing-box」的话，收益就是这个量级；
扩容到 54 条线时 usque 总数到 85 个，内存反而涨回去。**把目标写成「省 sing-box」
会导向错误方案**，正确的目标写法是「提高单位时间的新地址产出」。

**出口族**：H2 / WG / CF 实测 24h 内**全部 v6**（14,501 + 10,143 + 230，v4 = 0）。
转 H2 **不损失 v4 能力**（v4 早已不在用）。

---

## 3. 结构性耦合点（11-08 逐条对码复核）

| # | 耦合点 | 现状（已核实） | 全 H2 后的后果 |
|---|---|---|---|
| C1 | 补位线只认 WG 设备 | `_backfill_spare_indices` `daemon.py:3997` 扫 `wglines/lineN` + `_wg_line_state(i)!="installed"`；`_ensure_backfill_line:4102`；`_backfill_open:4153` | 渠道被封时无补位可用；`_backfill_open` 永远返回 None |
| C2 | 线路→节点台账只有 WG | `_line_node` `daemon.py:3729` = `return warp_wg.line_nodes().get(tag)`，一行，无 H2 分支 | H2 恒 None；夜间白名单 `_block_night_tags:4741`、重定位排除集、colo 记账三处同时失明 |
| C3 | 重定位目标池是 UDP 池 | `_channel_rehome_target` `daemon.py:3427`：`warp_wg.udp_capable()` + `wg_colo_map` 选目标，注释自己写明「候选还必须同时在 UDP 池里」 | 选出的节点不在该 H2 线 selector 候选里 ⇒ `PUT` 回 400 ⇒ `"gone"`，线卡在锁里 |
| C4 | ~~选线档位对 H2 结构性不利~~ | **已修**。`_warp_ip_rank` `daemon.py:6299` 现为档 1 = `probe_ok_ts`、**越新越优先**（`now - probe_ok`），档 2 = `last_ok_ts` 同样越新优先。commit `a376028`（11-05 20:03） | 无。初稿的「H2 永远档 2」机制已不存在 |
| C5 | 共享 sing-box 单点 | 实测只有 **1 个** `asip-h2-singbox.service`，31 条线共用；改候选池要重启它 | 扩到 54 条后单次重渲染断线面 ×1.7 |

**C4 撤销**，但 `docs/analysis-20261105-h2-selection-bias.md` 与
`docs/subsystems/switching.md` 仍写着旧机制，需要一起改（见改动清单 #10）。

另外两条硬约束：

- **设备数 = 线路数**（一台 usque 设备一个 CF 会话）。当前 39 台有 config：
  31 H2 + 5 CF + 3 探测，`free_devices()` 实测返回 `[]`，**空闲 0**。
  加线 = 新注册设备，CF 限流约 9 台/窗口。WG 设备不能复用（Curve25519 vs ECDSA）。
  **这是全方案最硬的约束**：方案 A 加 13~15 条就要 2 个窗口，方案 B 要 3 个。
  （`free_devices()` 只排除 **active** 单元，15 个 disable 的历史 `usque-warp*`
  不算占用——已经按这个口径算出 0，不是被历史单元误判的 0。）
- **单 colo 池深**：SJC/BOM/GIG/ORD 各只有 1 个候选节点，这些线的「换节点」等于没有。
  扩容只能投给池深 ≥4 的 colo（HKG/SIN/NRT/TPE/SLC）。

---

## 4. 三个方案

### 方案 A —— H2 为主 + WG 薄补位层（**推荐**）

```
H2 31 → 44~46 条（新增 13~15，投给 HKG/SIN/NRT/TPE）
WG 22 → 6 条（保留 NRT/SIN + 作为补位设备源）
CF  9 → 不变（唯一不经机场的路径，保留）
```
- 新增设备 13~15 台（2 个窗口）。
- **注意天花板**：`channels_per_block=3`，13 个 colo 理论上限 39 条 H2。
  其中 SJC/BOM/GIG/ORD 池深各 1，实际能扩的目标只有 HKG(23)/SIN(15)/NRT(8)/TPE(7)/SLC(4)。
  44~46 条已接近上限，再往上必须先**补测新节点把池做深**，不是加设备就完事。
- 补位继续走 WG（C1 不改），代价是保留 `wg-lines-install.sh` 与 6 条线的运维面。
- 只需修 C2/C3（C4 已修），C5 可选分片。

### 方案 B —— 全 H2（0 条 WG）

```
H2 31 → 53 条；WG 0；CF 9 不变
```
- 新增设备 22 台（3 个窗口）。
- **必须新建 H2 补位机制**：预渲染 N 条 `spare` 线（设备+单元+成员先进
  `warp-outbound.json`），平时不启动、不进探测/选线；被封时 `start` 单元即可。
  约 60 行（ledger `spare` 字段 + 三处候选过滤 + 开/关）。
- 失去：243 节点的深轮换池、逐进程故障隔离；补位从「注册一台就能起」变成
  「必须提前预留设备」。
- **只在「机场节点 UDP 2408 被系统性封禁」时才值得**——当前 243 个 UDP 节点可用，
  这个前提不成立。

### 方案 C —— 只修 H2 短板，不改拓扑（**最低成本**）

初稿说「修 6 条死线」，复核后**死线只有 3 条且影响可忽略**（见下），
所以 C 的内容改成：

1. 补测未测节点，加深深 colo 的池（`tools/probe_h2_nodes.py` + `merge_h2_blockmap.py`）。
2. 修 C1/C2/C3 三处代码（这三处也是 A 的前置）。
3. 查 SLC 三条线为何长期零出口（当前 `fail_streak` 30~34、24h 各只被探测 1 次）。

**关于「死线」的更正**：24h 零出口的 H2 线只有 **SLC-11/12/24** 三条，
合计重启尝试 141 次 = 全池 26,285 次的 **0.54%**，~~不是拖累重启的主因~~。
重启集中的是 WG-1/2/3/5/6/7/10/11 一带（各 1,200~1,400 次，但都是
`出口处于惩罚期` 触发的空转，属另一个问题）。
另外 SLC 三条线 24h 内**各只产生 1 条模型层结论**——它们没进探测排期
（`last_probe_ts` 相隔很远），和 `established-facts` 13h 第二半的
「键全相同导致尾部新线抢不到并发槽位」是同族症状，值得单独查。

### 选择建议

**先 C，再 A；B 不做。**

C 的 #2（修 C1/C2/C3）是 A 的前置——不修就无法把 WG 降到 6 条而不失去补位能力。
C4 已经不需要做。B 只在 UDP 被系统性封死时成立，当前数据不支持。

---

## 5. 改动清单（方案 A 口径，行号按 11-08 复核）

| # | 位置 | 改动 | 规模 |
|---|---|---|---|
| 1 | `daemon.py:3729 _line_node` | 台账合并 H2：读 `selector_state("h2-sel-N")` 当前节点，失败退回 `channels.json` | S |
| 2 | `daemon.py:3427 _channel_rehome_target` | 按线型分池：H2 候选 = 该线 selector 的 `all` 列表；colo 排序用 H2 实测 colo，不用 `wg_colo_map`；排除集并入 H2 各线当前节点 | M |
| 3 | ~~`_warp_ip_rank` 档位~~ | **删除**：commit `a376028` 已修 | — |
| 4 | `daemon.py:4741 _block_night_tags` | 依赖 #1；`night_window.enabled=False` 期间无影响，开启前必须先修 | —（#1 覆盖） |
| 5 | `.env` | `WARP_WG_NODES` 22 → 6 | S |
| 6 | `deploy/render_h2_lines.py:248 free_devices` | `spare` 线设备不算空闲（方案 B 才需要；A 跳过） | S |
| 7 | `deploy/render_h2_lines.py:132-166` | **上限口径是 `channels_per_block`（=3），不是池深**。实测 NRT/SIN 各 4 条已超上限（台账沿用块模型），SJC/BOM/GIG/ORD 各 1 条则是因为池里只有 1 个节点。扩容要按 `min(池深, cap)` 定目标，否则往单节点 colo 加线只会复制死线 | M |
| 8 | `config.py` 夜间白名单 | 开夜锁前 `allow` 由 `night_line_tags` 生成，且需 #1 后 H2 才进得来 | M（仅当开夜锁） |
| 9 | `deploy/h2-install.sh`、`asip-unit` | **无需改**（白名单已含 `usque-h2-*`；端口段 1180+/22700+ 不冲突） | — |
| 10 | 文档 | `established-facts.md` 13d2、`analysis-20261105-h2-selection-bias.md`（C4 已过时）、`subsystems/switching.md`、`CLAUDE.md` 线路数、`docs/README.md` 索引 | S |

**不改**：`warp_wg.py`（方案 A 保留 6 条 WG）、`warp_blocks.py`、
`verify_h2_render.py`。

---

## 6. 迁移步骤（方案 A，可回滚）

| 阶段 | 动作 | 影响 | 回滚 |
|---|---|---|---|
| P0 | 补测 H2 源 → `merge_h2_blockmap.py`；查 SLC 零出口 | 无 | 丢弃 block-map 增量 |
| P1 | 修 #1/#2，跑 `test_h2_render_guard.py` + 新增 2 条单测 | 无（纯代码） | revert |
| P2 | `h2-add-devices.sh` 分批注册（按 9 台/窗口） | 烧 CF 注册额度 | 删设备目录 |
| P3 | 低峰期 `h2-install.sh` 渲染+安装（**共享 sing-box 重启，31 条断数秒**） | 断流数秒 | 回退 `.warp/h2` 备份 |
| P4 | `.env` 降 `WARP_WG_NODES=6` → `wg-lines-install.sh` 停多余线 → `members.py --write-warp-outbound` → 刷新主配置 | 主 sing-box 重载（断流） | 恢复 `.env` 并重跑 |
| P5 | 24h 观测 G1~G3 | — | 回滚 P4 |

**新增风险（复核发现）**：`_run_h2_sync` `daemon.py:9529` 会在块模型出现新渠道时
**自动**渲染 + 装单元 + 重启共享 sing-box，且渲染器在设备不足时会自己调
`h2-add-devices.sh` 注册设备。也就是说 P0 补测一旦让块模型长大，**扩容会
在无人值守时自己发生**，越过 P2/P3 的人工窗口控制。P0 之后要盯
`.warp/h2/channels.json` 的条数与 CF 注册额度，或临时把 `_run_h2_sync` 的间隔调大。

---

## 7. 验收标准

1. `opencode-out` 成员数 = 实际 active 单元数（当前 62 = 31 H2 + 22 WG + 9 CF，无幽灵）。
2. 每条 H2 线 24h 内至少出过一次口；零出口线 ≤1 条且原因可解释。
3. 全池唯一出口 IP/日 ≥ 迁移前 7 日均值（当前基线 8,550）。
4. H2 模型层 OK 率不低于 WG（当前 H2 10.0% > WG 4.1%，**不要写成"差距≤5pp"**——
   这个判据在初稿里隐含「H2 更差」，方向反了）。
5. 手动演练一次「渠道被封 → 补位」，10 分钟内上线。
6. 回滚演练：P4 回退后 10 分钟内 WG 线全部 active 且 `opencode-out` 可见。

---

## 8. 待定（需要主人拍板）

1. **是否接受保留 6 条 WG**（方案 A）？全 H2（方案 B）需额外 22 台设备 + 新建 spare 机制。
2. ~~档位修法~~ **已作废**（C4 已修）。
3. **共享 sing-box 是否按 colo 分片**，把单次重渲染的断线面从 31/N 降到一半？
4. **夜间锁**将来是否要开？要开则 #1 必须先修。

---

相关：[analysis-20261105-h2-selection-bias.md](analysis-20261105-h2-selection-bias.md) ·
[established-facts.md](established-facts.md) 13d/13d2/13h ·
[warp-channel-allocation.md](warp-channel-allocation.md) ·
[ban-survey-20261103.md](ban-survey-20261103.md)
