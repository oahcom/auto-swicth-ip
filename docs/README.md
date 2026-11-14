# 项目文档地图

> 第一次接手？按这个顺序读，一小时能建立完整心智模型。

## 第 0 步：先读这一篇（不读会被自己"修"坏）

**[established-facts.md](established-facts.md) —— 既定事实**

那些"看起来像 bug、其实是设计"的决定。每一条都踩过坑、定过口径。
**动代码之前必须读完**，否则你会把 `08:00 解封`、`v4 跨 colo 换节点`、
`不启动 warp-svc` 这类既有决策当成问题"修复"掉。

---

## 第 1 步：它是干什么的

一句话：**让 opencode 免费层尽可能长时间地不撞 429/403。**

手段是不断更换出口 IP —— opencode 的配额按出口 IP 计，换 IP 就拿到新配额。
`CLAUDE.md` 开头的架构图是全局视图。

## 第 2 步：三层结构

```
9router (:20127)          ← 客户端入口，按 API key 鉴权
   ↓ outboundProxyUrl
sing-box (:7890)          ← 唯一的路由与出口选择点
   ↓ opencode-out selector
   ├── WARP 隧道（usque / MASQUE / WireGuard）
   └── proxy 池（订阅节点，按实测判死动态进出，兜底）
   ↓
opencode.ai
```

**daemon.py** 是大脑：读 9router 的 SQLite 判断成功/失败，决定切哪条线。

## 第 3 步：出口分四类（这是最容易混的地方）

| 类别 | 怎么出去 | 归属模块 | 换 IP 的方式 |
|---|---|---|---|
| **直连 WARP** | usque 隧道，不经机场 | `usque-*` 单元 | 重启即换（v6）；v4 需 rebind |
| **H2（MASQUE over HTTP/2）** | TCP 经机场节点 | `deploy/render_h2_lines.py` | 重启即换 |
| **WG（WireGuard over UDP）** | UDP 经机场节点中转 | `warp_wg.py` + `warp_blocks.py` | 重启即换（v6）；v4 换 colo |
| **proxy 池** | 订阅节点轮换（成员数动态） | daemon 的换线逻辑 | 换节点 |

## 第 4 步：三个守护各管一摊

| 守护 | 管什么 | 不管什么 | 代码 |
|---|---|---|---|
| **探测器 prober** | 出口 IP 好不好（模型层实测） | 线路通不通 | `daemon._start_warp_prober` |
| **同块轮回** | 线路绑的节点还能不能跑 WARP | IP 好不好 | `daemon._node_rotation_pass` |
| **换线** | 哪条线当当前出口 | 修线 | `daemon.reconnect_warp` |

三者边界严格，混在一起就会互相打架。详见 `established-facts.md` 第 18 条。

## 第 5 步：渠道模型（块 = 机房）

`warp_blocks.py`。设计文档见 [warp-channel-allocation.md](warp-channel-allocation.md)。

- **块** = 一个 colo（机房），如 NRT / PHX / SJC
- **渠道** = (类型 v6/v4, 块, 序号)
- v4 合并成一个虚拟块 `ALL`，且**排在清单最前**（否则线路数不够时整块被截断）

看当前会开几条什么渠道，**不写盘**：

```bash
set -a; . ./.env; set +a
python3 warp_blocks.py plan
```

## 第 6 步：验证改动

```bash
python3 -m pytest test_warp_blocks.py -q   # 28 项，纯模型
python3 test_node_rotation.py               # 11 项，轮回守护（mock）
python3 -m py_compile daemon.py config.py warp_wg.py warp_blocks.py
```

---

## 文档索引（按用途）

### opencode 独立闸口需求（2026-11，需求归档）

| 文档 | 内容 |
|---|---|
| **[opencode需求全链归档-20261114.md](opencode需求全链归档-20261114.md)** | **整个 opencode 需求（7 大块）唯一入口**：全链总览 + 文档落位表 + 已知限制汇总 |
| [opencode独立asip闸口-论证体计划书.md](opencode独立asip闸口-论证体计划书.md) | slot-8 专属闸口 + 原生直连 dsv4.1-flash-free + 429 换 IP 的论证体计划 |
| [opencode独立asip闸口-派工契约与提示词.md](opencode独立asip闸口-派工契约与提示词.md) | `POST /rotate-opencode-gate` 冻结契约（C1–C7）与派工提示词 |
| [opencode独立asip闸口-六维度终审裁决.md](opencode独立asip闸口-六维度终审裁决.md) | 4 提交改动清单、四条红线实证、L1–L8 已知限制、部署记录 |

### 要理解"为什么这么设计"

| 文档 | 内容 |
|---|---|
| **[established-facts.md](established-facts.md)** | **既定事实（63 条），先读这个** |
| [glossary.md](glossary.md) | **名词表**：节点/线路/渠道/成员的确切定义 |
| [BUILD-PLAN.md](BUILD-PLAN.md) | 文档构建计划与更新规范 |

**新增的容易踩的**（都在 established-facts 里）：

- **第 25 条** `fail_streak` 只数「拿不到出口地址」，不数「地址被封」——
  看到「连续 N 次取不到出口地址」先查该线最近一次 `模型层:` 结论。
- **第 26 条** `config.py` 的 members 是**声明**，不等于单元在跑——
  disabled 的线仍会被探，`fail_streak` 能涨到 22 万。
- **第 27 条** v4 线的出口上限 = 落地 colo 数——三条 v4 全落 PHX
  就会拿到同一个地址。
- **第 28 条**「配置里没有 WARP 出口」是一份**合法**配置、探活全绿——
  2026-11-03 远端因此丢了 3.5 小时出口（30362 次 `status=404`）而日志无声。
  自愈重建必须走骤降护栏，裸跑 `parse_subscription.py` 会把可疑产出推上线。
- **第 29 条** `proxy` 池成员数是**动态**的（按实测判死摘出），两台机器同刻
  可以差近 7 倍（远端 165 / 本地 25）。不要把它当故障，也不要写进阈值。
- **第 25b 条** 重启循环会把唯一能救它的机制（同块轮回）顶在门外——
  `ever_ip` 为假的线重启没有意义，得换节点。H2 线的承载闸查的是另一个集合
  `_H2_POOL_DEAD`（它们的落地节点不在 `warp_wg` 台账里）。
- **第 13h 条** 部分 H2 线绑在死节点上是**预期状态**；2026-11-05 扩容后多出
  FRA/GIG/IST/LHR/SYD 五个 colo，当天下午补 greatgame 源后又多出
  BOM/JNB/DUB/IAD 四个（28 个节点实测 22 个可承载 H2）。但**新 colo 抬的是
  单位时间地址产出，不是单地址寿命** —— 看到「新 colo 也很快 429」不是扩容失效。
- **第 26c 条** `.warp/block-map.json` 与 `h2_probe_state.json` 是**累积知识**，
  不是配置：部署时**合并**而不是覆盖。旧版覆盖+过滤实测会删掉 174 条里的
  130 条（含 28 个 H2 落地节点），且远端事后不会自动加回。
- **第 26d 条** 部署脚本曾漏 `patch-wg-outbounds.py` 且渲染顺序反了 ——
  症状是单元全 active、单条 SOCKS 能出 `warp=on`，但主 selector 里没有新线。
- **第 13c 条** 手动跑 `h2-install.sh` **只装了一半**：它不写
  `warp-outbound.json`、不跑 `patch-wg-outbounds.py`，于是新线单元全 active、
  单条 SOCKS 直连能出 `warp=on`，但主 `opencode-out` 里一条都没有。完整顺序
  见 TEST_PLAN §20b。
- **第 13h 条（第二半）** 新线「一直没出过口」先查 `last_probe_ts`：**是 0 表示
  「没探过」，不是「探了不行」**。探测排期原先按 `now` 排序、键全相同，尾部新线
  永远抢不到并发槽位（已修，见 TEST_PLAN §20c）。
- **第 34 条** 写入口缺口是**动态再生**的（探针建账曾绕过 `expires_at` 双写）；P2 清理把「缺字段」当**未迁移**（先补齐再判、补不出跳过告警），不当「已过期」。
- **第 35 条** 影子计数必须落盘带起点戳，否则「violations==0 连续 ≥24h」永远无法证明；偏差必须分人群（干净条目会把时间戳计成偏差）；P2 门槛靠 would-delete 干跑排演误删。
- **第 36 条** 引入 import 的提交必须把被引文件带进同一笔——F-7 断头（c819e4a 漏 add）期间 stash / 新检出即 ImportError，而工作区正常会掩盖问题。

**架构层**（看代码看不出来的设计）

| 文档 | 内容 |
|---|---|
| [architecture/tunnel-types.md](architecture/tunnel-types.md) | 四种隧道类型：协议、路径、能拿到什么出口 |
| [architecture/ip-pools.md](architecture/ip-pools.md) | WARP IP 从哪来、v4/v6 分布、共享池的影响 |
| [architecture/provisioning.md](architecture/provisioning.md) | 一条隧道从零建起来：每一步做什么、漏了会怎样 |
| [design-20261113-per-egress-session-pool.md](design-20261113-per-egress-session-pool.md) | 按出口 IP 的会话池：15×10、IP 变重建、150 回绕、slot-7 tag |
| [design-20261113-ledger-lifecycle.md](design-20261113-ledger-lifecycle.md) | 台账生命周期：`expires_at` 单一事实源、P0–P3 灰度、INV-1~4、P2 切换与回滚 |
| [design-20261113-config-drift.md](design-20261113-config-drift.md) | 配置漂移复发治理：六面对账（含生效面）、自检分类七、doc-drift 关卡 |

**子系统层**

| 文档 | 内容 |
|---|---|
| [subsystems/prober.md](subsystems/prober.md) | 探测器：五档结论、按 /128 记账、失明保护 |
| [subsystems/switching.md](subsystems/switching.md) | 换线：选线四档、/64 静置、冷却（含已知的 H2 档位偏置） |
| [subsystems/locks.md](subsystems/locks.md) | 四把锁：各防什么、什么时候必须让路 |
| [subsystems/refresh.md](subsystems/refresh.md) | 补刷 / 预热 / 脏线守护：被换下的线谁来修 |
| [warp-node-rotation.md](warp-node-rotation.md) | 同块轮回：节点坏了怎么换 |

**历史调查记录**

| 文档 | 内容 |
|---|---|
| [warp-channel-allocation.md](warp-channel-allocation.md) | 渠道分配模型 + H2 复活全过程（68K，最详细） |
| [dev-design-20261028-warp-rotation.md](dev-design-20261028-warp-rotation.md) | 轮换体系改造的完整设计书 |
| [warp-wg-over-proxy.md](warp-wg-over-proxy.md) | WG over UDP 经机场中转的原理 |
| [prober-gate-design-20261027.md](prober-gate-design-20261027.md) | 探测器判据设计 |

### 要查实测数据

| 文档 | 内容 |
|---|---|
| [colo-segment-map-20261101.md](colo-segment-map-20261101.md) | colo ↔ 地址段映射 |
| [warp-colo-survey-20261101.md](warp-colo-survey-20261101.md) | 节点 ↔ colo 普查 |
| [ban-survey-20261103.md](ban-survey-20261103.md) | 18:00 后各渠道/colo ban 率与报错指标 |
| [analysis-20261105-h2-selection-bias.md](analysis-20261105-h2-selection-bias.md) | **H2 为何几乎不被选**：档 1 无时效 + 档内「验得久优先」；含供给侧的 /64 与低产线测量 |
| [review-5d-20261027.md](review-5d-20261027.md) / [review-5d-20261028-audit.md](review-5d-20261028-audit.md) | 五日实测复盘与审计 |
| [report-20261113-ledger-lifecycle-verify.md](report-20261113-ledger-lifecycle-verify.md) | 台账生命周期实施验证报告（逐项核验 + 3 处补修 + 六维自查） |
| [review-20261113-impl-3x6.md](review-20261113-impl-3x6.md) | 三方×六维实施监督报告（台账 + 漂移两路并行实施） |

### 出了事要查

| 文档 | 内容 |
|---|---|
| [incident-20261102-9router-network-blackhole.md](incident-20261102-9router-network-blackhole.md) | 9router 网络黑洞 |
| [incident-20261028-9router-db-ssrf-secrets.md](incident-20261028-9router-db-ssrf-secrets.md) | DB / SSRF / 密钥事件 |
| [503-root-cause-20261027.md](503-root-cause-20261027.md) | 503 根因分析 |
| [bug-and-test-plan-20261027.md](bug-and-test-plan-20261027.md) | 缺陷与测试计划（85K） |

### 参考层（查表用）

| 文档 | 内容 |
|---|---|
| [reference/config.md](reference/config.md) | 每个配置项**改了会怎样**（不抄默认值） |
| [reference/state-files.md](reference/state-files.md) | 每个 `.json` 谁写谁读、哪些字段容易误用 |
| [reference/ports-units.md](reference/ports-units.md) | 端口分配表、单元/tag 命名规则、**20128 是 relay-guard 不是 9router** |

### 运维流程

| 文档 | 内容 |
|---|---|
| [runbook/add-tunnel.md](runbook/add-tunnel.md) | 加隧道速查清单 + 症状对照表 |
| [runbook/troubleshoot.md](runbook/troubleshoot.md) | 7 条排查树：429 风暴 / selector 打空 / 线不通 / 单元假活 / 9router 假健康 / 两台机器不一致 / 日志无线索 |
| [runbook/ledger-cleanup-switch.md](runbook/ledger-cleanup-switch.md) | P2 清理判据切换：四门槛、观测口径、失败处置表、回滚（改配置即回滚） |
| `deploy/remote-deploy.sh` | **一键复刻本机拓扑到远端**（代码/状态表/设备/单元/验收） |
| [ops-20261028-security-changes.md](ops-20261028-security-changes.md) | 安全变更操作记录 |
| [9router-task-book-20261028.md](9router-task-book-20261028.md) | 9router 任务书 |
| [task-20261104-channel-backfill.md](task-20261104-channel-backfill.md) | 渠道封禁补位：任务、实现、验证、踩过的坑 |
| [9router-dev-logic-20261028.md](9router-dev-logic-20261028.md) | 9router 开发逻辑（已不存在于本项目的 proxy-global.js 那一层） |

---

## 关键文件速查

| 文件 | 作用 |
|---|---|
| `CLAUDE.md` | 全局架构 + 配置项 + 坑（约 900 行） |
| `config.py` | 所有可调参数，**唯一的配置来源** |
| `daemon.py` | 大脑：探测器、换线、轮回、补刷（7000+ 行） |
| `warp_blocks.py` | 渠道模型（纯函数，可单测） |
| `warp_wg.py` | WG 线路的生成、探测、apply |
| `warp_diag.py` | 单节点 WARP 承载诊断（**判断节点能不能用只信它**） |
| `tools/channel_report.py` | 渠道健康报表（`asip chan`）：成功率 / 出口段四级 / 每 colo 的池与指纹落点，纯只读 |
| `deploy/render_h2_lines.py` | H2 线路渲染 |
| `deploy/install.sh` | 单元生成与安装 |

## 常用命令

```bash
# 看当前拓扑
asip warp status

# 看渠道健康与出口段（纯读；-h 看 --hours/--html/--json/--by）
asip chan

# 看渠道清单（纯读）
set -a; . ./.env; set +a; python3 warp_blocks.py plan

# 诊断某个节点能不能承载 WARP（会占用空闲设备槽位）
python3 warp_diag.py --job job.json --out result.jsonl --rounds 2 --family v6

# 重新布线
python3 -c "import warp_wg; warp_wg.apply(count=22, publish=True)"

# 把本机拓扑复刻到远端（先 --dry-run 看会执行什么）
./deploy/remote-deploy.sh user@host --dry-run
./deploy/remote-deploy.sh user@host --port 22

# 重启 daemon
sudo systemctl restart auto-switch-ip.service
```

---

## 维护规则

1. **新增"看起来像 bug 的设计"** → 加进 `established-facts.md`
2. **新增实测数据** → 写进对应的 `*-YYYYMMDD.md`，不塞进 CLAUDE.md
3. **改配置项** → 同步 CLAUDE.md 的配置段
4. **不要在 CLAUDE.md 里堆实测数据** —— 它已经约 900 行，再堆就没人读了
