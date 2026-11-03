# 文档构建计划

> 目标：把**看代码看不出来**的设计逻辑沉淀成文档。
> 判据：一个没参与过这个项目的人，读完能回答「为什么是这样」而不只是「是什么」。

## 一、问题诊断

现状：`docs/` 已有 20 份、约 450K，但结构是**按时间堆叠的变更记录**。
新人拿到的是一个巨大的时间线，要自己从中拼出全局。

三个具体缺口：

| 缺口 | 例子 |
|---|---|
| **没有"为什么"** | 代码里能看到 `V4_COLO = "ALL"`，看不到为什么 v4 必须合并、为什么排最前 |
| **没有全景** | 4 种隧道类型散在 5 个文件里，没人说得清它们的分工 |
| **名词不统一** | 「节点」「线路」「渠道」「块」「成员」在不同文件里指代不同东西 |

## 二、文档分层

```
L0  入口        docs/README.md                ← 地图：先读什么
L0' 名词表      docs/glossary.md              ← 所有文档的用词基准
L1  既定事实    docs/established-facts.md     ← 看起来像 bug 的设计
L2  架构        docs/architecture/*.md        ← 隧道类型 / IP 分布 / 构建 / 取舍
L3  子系统      docs/subsystems/*.md          ← 探测器 / 换线 / 轮回 / 补刷 / 锁
L4  参考        docs/reference/*.md           ← 配置项 / 状态文件 / 端口 / 单元
L5  运维        docs/runbook/*.md             ← 加隧道 / 排障 / 轮换
L6  历史        docs/incident-*.md 等         ← 已有，保持不动
```

**名词表是第一优先级**：没有统一的用词，其他文档写出来会互相打架
（现状：「节点 / 线路 / 渠道 / 成员」在不同文件里指代不同东西）。

**分层规则**：上层只引用下层，不重复内容。`established-facts.md` 里说过的，
L2/L3 只链接不重写。

## 三、批次与分工

### 批次 1：架构层（用户点名，最高优先级）

| # | 文档 | 要回答的问题 | 为什么必须写 |
|---|---|---|---|
| **A** | `architecture/tunnel-types.md` | 有哪几种隧道？各自走什么协议、经过什么、能拿到什么出口？边界在哪？ | 4 种类型散在 5 个文件，新人分不清 `WARP-CF` / `WARP-H2` / `WARP-WG` 的区别 |
| **B** | `architecture/ip-pools.md` | WARP 的 IP 从哪来？v4/v6 分布规律？为什么 v4 收敛？共享池怎么影响可用性？ | 决定所有调度策略的物理前提，代码里只有零散注释 |
| **C** | `architecture/provisioning.md` | 一条隧道是怎么建起来的？从注册设备到 systemd 单元到 sing-box 出口，每一步做了什么、为什么 | `install.sh` + `usque-launch.sh` + `members.py` + `render_h2_lines.py` 四段拼起来才看得懂 |

### 批次 2：子系统层

| # | 文档 | 要回答的问题 |
|---|---|---|
| **D** | `subsystems/prober.md` | 探测器怎么判一条线好坏？verdict 五档各是什么意思？为什么按 /128 记账？ |
| **E** | `subsystems/switching.md` | 换线怎么选目标？IP 分级三档、/64 静置、冷却线垫底分别解决什么？ |
| **F** | `subsystems/locks.md` | 夜间锁 / 渠道锁 / 额度隔离 / 小时配额，四把锁各防什么？什么时候必须让路？ |
| **G** | `subsystems/refresh.md` | 补刷、预热、脏线守护三者分工？为什么必须等线空闲？ |

### 批次 3：参考与运维

| # | 文档 | 要回答的问题 |
|---|---|---|
| **H** | `reference/config.md` | 每个配置项的**含义 + 为什么是这个值 + 改了会怎样**（不是抄 config.py） |
| **I** | `reference/state-files.md` | 每个 `.warp*.json` 谁写谁读、字段语义、哪些字段容易误用 |
| **J** | `reference/ports-units.md` | 端口分配表、单元命名规则、tag 命名规则 |
| **K** | `runbook/add-tunnel.md` | 加一条隧道要做哪几件事（漏一步就是"起着但用不上"） |
| **L** | `runbook/troubleshoot.md` | 线不通 / 全是 429 / selector 打空，各怎么查 |

## 三之二、进度

更新于 2026-11-03。

| 批次 | 文档 | 状态 |
|---|---|---|
| 1 | A `architecture/tunnel-types.md` | ✅ 已完成 |
| 1 | B `architecture/ip-pools.md` | ✅ 已完成 |
| 1 | C `architecture/provisioning.md` | ✅ 已完成 |
| 2 | D `subsystems/prober.md` | ✅ 已完成 |
| 2 | E `subsystems/switching.md` | ✅ 已完成 |
| 2 | F `subsystems/locks.md` | ✅ 已完成 |
| 2 | G `subsystems/refresh.md` | ✅ 已完成 |
| 3 | H `reference/config.md` | ✅ 已完成（2026-11-03） |
| 3 | I `reference/state-files.md` | ✅ 已完成（2026-11-03） |
| 3 | J `reference/ports-units.md` | ✅ 已完成（2026-11-03） |
| 3 | K `runbook/add-tunnel.md` | ✅ 已完成（2026-11-03，只留清单 + 症状对照） |
| 3 | L `runbook/troubleshoot.md` | ✅ 已完成（2026-11-03，7 条排查树） |

批次 3 的写法：**写「改了会怎样」，不写「默认值是多少」**——后者 `grep` 就有，
抄一遍会立刻过期（2026-11-03 实测：CLAUDE.md 配置段有 8 项值与 `config.py` 不符）。

## 四、子代理分工原则

每个子代理的 brief 必须包含：

1. **输出文件路径**（唯一，不重叠）
2. **必须回答的问题清单**（不给自由发挥空间）
3. **必须读的代码/文档**（指路，不重述内容）
4. **禁止事项**：不改任何生产代码、不改 config、不重启服务、不写 `.warp/` 下任何文件
5. **格式要求**：中文、无代码块堆砌、每个结论必须有实测依据或代码依据

**关键约束**：子代理写的是文档，**不是重构建议**。发现代码有问题 → 记在
`docs/findings.md` 待人工确认，不要顺手改。

## 五、更新规范（写进 docs/README.md 末尾）

### 什么时候必须更新

| 触发 | 必须更新 |
|---|---|
| 改了 `config.py` 的默认值 | `reference/config.md` + `CLAUDE.md` 配置段 |
| 改了某个判据（阈值、轮数、窗口） | 对应 `subsystems/*.md` 的"为什么是这个值" |
| 新增一种隧道/承载方式 | `architecture/tunnel-types.md` |
| 修掉一个"看起来像 bug 的设计" | 从 `established-facts.md` 删除或改写 |
| 新增一个"看起来像 bug 的设计" | 加进 `established-facts.md` |
| 出了事故 | `incident-YYYYMMDD-*.md`，并在相关文档加链接 |

### 硬规则

1. **不写"因为代码这么写的"** —— 要写实测依据或上游硬约束
2. **不写没有日期的实测数字** —— 一律 `YYYY-MM-DD 实测`
3. **CLAUDE.md 不再堆实测数据** —— 它已约 900 行，实测进 `docs/`
4. **每份文档开头写"本文回答什么问题"** —— 一句话，写不出来说明不该有这份文档
5. **删除优于新增** —— 文档过期比没有更糟，过期的直接删

### 交叉引用

- L1（既定事实）是**唯一**的"为什么"权威源，L2/L3 遇到冲突以 L1 为准
- 每份文档末尾列"相关文档"，方便顺着读

## 六、验收标准

一份文档合格，必须满足：

- [ ] 开头有一句话说明本文回答什么问题
- [ ] 每个"为什么"都有实测日期或代码依据
- [ ] 没有大段代码粘贴（逻辑用文字讲）
- [ ] 名词与 `docs/README.md` 的名词表一致
- [ ] 末尾有"相关文档"链接
- [ ] 新人读完能复述核心结论
