# 全量转 H2 承载 —— 需求与方案设计（2026-11-05）

> 目标：把「WARP 出口」的承载方式从 WG(UDP) 为主转成 H2(MASQUE over HTTP/2) 为主，
> 减少常驻 sing-box 进程数。本文给出基线、耦合点、三个方案与改动清单。
> **结论先行：推荐方案 A（H2 为主 + 保留少量 WG 作补位/低延迟层），不要一步到 0 条 WG。**

---

## 1. 需求与可度量目标

| # | 目标 | 判据（迁移后 24h 实测） |
|---|---|---|
| G1 | 出口地址产量不下降 | 全池每日唯一出口 IP ≥ 迁移前 7 日均值 |
| G2 | 单线产量不下降 | H2 单线 ≥ 600 唯一 IP/日（当前 WG 636、H2 816） |
| G3 | 可用性不下降 | 模型层 OK 率 ≥ 迁移前 −0pp（修完选线偏置后可比） |
| G4 | 进程结构简化 | WG 独占 sing-box 进程数降到 ≤8 |
| G5 | 可回滚 | 任一步骤回退后 10 分钟内恢复迁移前拓扑 |

**非目标**：本方案不改 opencode 侧、不改 9router、不追求延迟下降（实测同 colo 两者无稳定差异）。

---

## 2. 现状基线（2026-11-05 实测）

| 维度 | H2 | WG | CF（直连 MASQUE） |
|---|---|---|---|
| 成员/单元 | 31 / 31 | 23 / 23 | 9 / 5 |
| 实测落地 colo | **13 个** | 4 个（NRT/PHX/SIN/SJC） | 2 个（SJC/LAX），`loc=CN` |
| 候选节点池 | 71 节点 / 13 colo | **243** 个 UDP 可用节点 | — |
| 单 colo 池深 | HKG23 SIN15 NRT8 TPE7 SLC4 ICN3 LAX3 LHR2 FRA2，SJC/BOM/GIG/ORD **各 1** | 深 | — |
| 24h 模型层 OK | 46.2k/70.7k = **65%** | 46.8k/58.0k = **81%** | 4.9k/9.3k = 53% |
| 今日唯一出口 IP | 25,295（816/线） | 14,627（636/线） | 4,195 |
| 今日重启次数 | 1,102 | 1,338 | 203 |
| 换节点成本 | `PUT` selector，**不重启** | 写配置 + 重启该线 | 无 |
| 换地址成本 | `systemctl restart usque-h2-N`（新会话） | 重启该线 sing-box | 重启 usque |
| 单进程 PSS | usque 6.3MB × 31 | sing-box 11.8MB × 23 | usque 6.3MB × 5 |

**资源账要说清**：全量替换 23 条 WG = 少 23 个 sing-box（≈271MB）+ 多 23 个 usque（≈145MB）
⇒ **净省约 125MB，进程总数不变**。目的如果只是「少部署 sing-box」，收益就是这个量级；
真正值得转的理由是 **colo 覆盖 13 vs 4** 和 **换节点零重启**。

**出口族**：H2 与 CF 实测全部 v6；本次 WG 也已全 v6（`.env` `WARP_WG_FAMILIES=v6`），
所以转 H2 **不损失 v4 能力**（v4 早已不在用：台账 3 条 v4 地址全部封禁）。

---

## 3. 为什么不能「删掉 WG 就完事」—— 五个结构性耦合点

| # | 耦合点 | 现状 | 全 H2 后的后果 |
|---|---|---|---|
| C1 | 补位线只认 WG 设备 | `_backfill_spare_indices` / `_ensure_backfill_line` 扫 `wglines/lineN` + `warp-wg-N.service`（`daemon.py:3930,4035,4086`） | 渠道被封时无补位可用；`_backfill_open` 永远返回 None |
| C2 | 线路→节点台账只有 WG | `warp_wg.line_nodes()` 只读 `warp_wg_state.json`；H2 不在其中 | `_line_node`（`daemon.py:3662`）对 H2 恒 None；夜间白名单、重定位排除集、colo 记账三处同时失明 |
| C3 | 重定位目标池是 UDP 池 | `_channel_rehome_target`（`daemon.py:3360`）用 `udp_capable()` + `wg_colo_map` 选目标 | 选出的节点不在该 H2 线 selector 候选里 ⇒ `PUT` 回 400 ⇒ `"gone"`，线卡在锁里 |
| C4 | 选线档位对 H2 结构性不利 | `_warp_ip_rank`（`daemon.py:6232`）：H2 永远档 2，且「验得越久越优先」 | H2 占多数后这不再是偏置，而是**主选线逻辑失效**（见 `analysis-20261105-h2-selection-bias.md`） |
| C5 | 共享 sing-box 单点 | 31 条线共用一个 `asip-h2-singbox`；改候选池要重启它 | 31→54 条后，单次重渲染/重启断线面扩大 1.7 倍 |

另外两条硬约束，必须在方案里显式处理：

- **设备数 = 线路数**（一台 usque 设备一个 CF 会话，同一设备不能开两个会话）。
  当前 39 台有 config：31 H2 + 5 CF + 3 探测，**空闲 0**。加 23 条线 = 新注册 23 台，
  CF 限流约 9 台/窗口 ⇒ 约 3 个窗口。WG 设备不能复用（Curve25519 vs ECDSA 两种密钥）。
- **单 colo 池深**：SJC/BOM/GIG/ORD 各只有 1 个候选节点，这些线上的「换节点」等于没有。
  扩容应优先投给池深 ≥4 的 colo（HKG/SIN/NRT/TPE/SLC）。

---

## 4. 三个方案

### 方案 A —— H2 为主 + WG 薄补位层（**推荐**）

```
H2 31 → 44~46 条（新增 13~15，投给 HKG/SIN/NRT/TPE）
WG 23 → 6 条（保留 NRT/SIN 低延迟 + 作为补位设备源）
CF  9 → 不变（唯一不经机场的路径，保留）
```
- 新增设备 13~15 台（2 个窗口）。
- 补位继续走 WG（C1 不改），代价是保留 `wg-lines-install.sh` 与 6 条线的运维面。
- 只需修 C2/C3/C4（C1 不动），C5 靠「按 colo 分 2 个共享 sing-box」缓解（可选）。

### 方案 B —— 全 H2（0 条 WG）

```
H2 31 → 54 条；WG 0；CF 9 不变
```
- 新增设备 23 台（3 个窗口）。
- **必须新建 H2 补位机制**：渲染 N 条 `spare` 线（设备+单元+成员预先发布进
  `warp-outbound.json`），平时不启动单元、不进探测/选线；被封时 = `start` 单元即可，
  不需要重载主配置。约 60 行（ledger `spare` 字段 + 三处候选过滤 + 开/关）。
- 失去：243 节点的深轮换池、逐进程故障隔离；补位从「注册一台就能起」变成
  「必须提前预留设备」。
- 只在「机场节点 UDP 2408 被系统性封禁」时才值得做——当前 243 个节点 UDP 可用，
  这个前提不成立。

### 方案 C —— 不迁移，只修 H2 短板（**最低成本**）

- 修 6 条死线（SLC×3 + 低产组重渲染）、补测 yyd2tett 49 / dasho 46 两个源的 H2 承载、
  修 C4 档位偏置。不改拓扑。
- 成本约 1~2 天，收益可能大于直接迁移（H2 从 28/31 可用 → 更高，且选线公平）。

### 选择建议

**先 C，再 A；B 不做。**
C 的两项（补池、修档位）是 A 的前置条件——不修 C4 就扩容，只是往一个结构性偏置的
选线器里灌更多候选。B 只有在 UDP 通道被系统性封死时才成立，当前数据不支持。

---

## 5. 改动清单（方案 A 口径）

| # | 位置 | 改动 | 规模 |
|---|---|---|---|
| 1 | `daemon.py:3662 _line_node` | 台账合并 H2：读 `_h2_clash().selector_state("h2-sel-N")` 当前节点（失败退回 `channels.json`） | S |
| 2 | `daemon.py:3375,3388 _channel_rehome_target` | 按线型分池：H2 的候选 = 该线 selector 的 `all` 列表；colo 排序用 H2 实测 colo，不用 `wg_colo_map`；排除集并入 H2 各线当前节点 | M |
| 3 | `daemon.py:6232 _warp_ip_rank` | 档 1 加时效（`last_ok_ts` 超 30min 降档）**或**档内按「最久未被选中」轮转（`st["sel"]`，一行）。前者改语义，需确认 | S |
| 4 | `daemon.py:4674,4691 夜间白名单` | 依赖 #1；`night_window.enabled=False` 期间无影响，开启前必须先修 | —（由 #1 覆盖） |
| 5 | `.env:13-14` | `WARP_WG_NODES` 22 → 6 | S |
| 6 | `deploy/render_h2_lines.py:248 free_devices` | `spare` 线占用的设备不能被算作空闲（方案 B 才需要；A 可跳过） | S |
| 7 | `deploy/render_h2_lines.py:463` | 扩容渲染：目标线数改为「按池深加权」而非等比，避免往 SJC/BOM/GIG 各 1 节点的 colo 加线 | S |
| 8 | `config.py:634-646` | 夜间白名单如果开，`allow` 由块生成（已有 `night_line_tags`），但需 #1 后 H2 才进得来 | M（仅当开夜锁） |
| 9 | `deploy/h2-install.sh`、`asip-unit` | 无需改（白名单已含 `usque-h2-*`；端口段 1180+/22700+ 不冲突） | — |
| 10 | 文档 | `established-facts.md` 13d2（补位方向）、`CLAUDE.md` 线路数、`docs/README.md` 索引 | S |

**不改**：`warp_wg.py`（方案 A 保留 6 条 WG，整套 rebind/register 继续用）、
`warp_blocks.py`（渠道模型不变）、`verify_h2_render.py`（端口/selector 校验已覆盖）。

---

## 6. 迁移步骤（方案 A，可回滚）

| 阶段 | 动作 | 影响 | 回滚 |
|---|---|---|---|
| P0 | 补测 H2 源（`tools/probe_h2_nodes.py` 测 yyd2tett/dasho 未测节点）→ `merge_h2_blockmap.py` | 无 | 丢弃 block-map 增量 |
| P1 | 修 #1/#2/#3，跑 `test_h2_render_guard.py` + 新增 2 条单测（H2 台账、重定位候选池） | 无（纯代码） | revert |
| P2 | `h2-add-devices.sh 14`（分 2 窗口） | 烧 CF 注册额度 | 删设备目录 |
| P3 | 低峰期 `h2-install.sh` 渲染+安装（**共享 sing-box 重启，31 条线断数秒**） | 断流数秒 | 回退 `.warp/h2` 备份 |
| P4 | `.env` 降 `WARP_WG_NODES=6` → `wg-lines-install.sh` 停多余线 → `members.py --write-warp-outbound` → 刷新主配置 | 主 sing-box 重载（断流） | 恢复 `.env` 并重跑 |
| P5 | 24h 观测 G1~G3 | — | 回滚 P4 |

**不要**在 H2 池未补深前做 P3/P4：31 条里已有 3 条（SLC）整日零出口，扩容只会复制同类死线。

---

## 7. 验收标准

1. `opencode-out` 成员数 = 实际 active 单元数（无幽灵成员）。
2. 每条 H2 线 24h 内至少出过一次口；零出口线 ≤1 条且原因可解释。
3. 全池唯一出口 IP/日 ≥ 迁移前 7 日均值。
4. H2 模型层 OK 率与 WG 差距 ≤5pp（同小时对比，不用全天口径）。
5. 手动触发一次「渠道被封 → 补位」演练，补位线 10 分钟内上线。
6. 回滚演练：P4 回退后 10 分钟内 WG 线全部 active 且 `opencode-out` 可见。

---

## 8. 待定（需要主人拍板）

1. **是否接受保留 6 条 WG**（方案 A）？全 H2（方案 B）需要额外 8 台设备 + 新建 spare 补位机制。
2. **档位修法**：#3 用「档 1 加 30min 时效」（改既有语义）还是「档内轮转」（保守，一行）？
3. **共享 sing-box 是否分片**（按 colo 分 2 个），把单次重渲染的断线面从 31/54 条降到一半？
4. **夜间锁**将来是否要开？要开则 #1 必须先修，否则 H2 全被锁在名单外。

---

相关：[analysis-20261105-h2-selection-bias.md](analysis-20261105-h2-selection-bias.md) ·
[established-facts.md](established-facts.md) 13d/13d2/13h ·
[warp-channel-allocation.md](warp-channel-allocation.md) ·
[ban-survey-20261103.md](ban-survey-20261103.md)
