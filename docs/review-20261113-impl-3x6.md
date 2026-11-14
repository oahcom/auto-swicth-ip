# 实施监督报告 —— 台账生命周期 + 配置漂移（三方 × 六维）

> 版本：2026-11-13 16:5x
> 性质：**实施验收与监督**（设计见 `design-20261113-ledger-lifecycle.md` / `design-20261113-config-drift.md`）
> 施工方式：三路子代理并行/串行实施 + 主控三方×六维监督；**全部改动未提交**，等主人逐块看 diff
> 基线：commit `bc89daa`（配置漂移自愈——热载后校验成员并单槽重启进程）
> 验收终态：**全量 1319 passed / 0 failed**（189s）；`asip audit --doc-drift` **EXIT=0**

---

## 0. 施工总览

| 路 | 领地 | 交付 | 状态 |
|---|---|---|---|
| **ledger-dev** | daemon.py 台账区域 / config.py / 新模块与测试 | 台账生命周期 P0+P1：`asip/ledger_policy.py` 单一事实源、`expires_at` 双写与事件契约、载入归一化、`ledger_cleanup_mode` 开关（默认 legacy）、空表放行、影子统计 | ✅ 383 例自测全绿 |
| **drift-dev** | asip/config_projection.py / tools/asip_audit.py / 新测试 | E 项：纯比对单一事实源、`--doc-drift` 文档关卡（退出 1 阻断）、`--runtime-drift` 运行时对账、集成规格 | ✅ 37 例全绿；实跑抓到 2 处真文档漂移 |
| **integrator** | daemon.py 漂移区域 / 文档 | D-1 收口（`_slot_members_drift` 委托）、分类七「产物面 ⊄ 生效面」（异常隔离壳）、D-2 docstring 修正、T3/T8 测试、2 处文档修复 | ✅ 收口回归全绿 |
| **主控（监督）** | 验收 / 裁决 / 报告 | 独立复核、2 项裁决、桩签名对齐、全量验收、本报告 | ✅ 1319/0 |

**分工原则**：daemon.py 任一时刻只有一路在写（防整文件写回的并发丢更新）；两路并行期各自独占文件集，集成在台账路交还 daemon 施工权后进行。

---

## 1. 三方 × 六维审查矩阵（18 格，对实施结果）

### 1.1 需求方（主人要的到底是什么 → 成功判据 S1-S5 落地了吗）

| 维度 | 查了什么 | 结论 |
|---|---|---|
| D1 正确性 | S1「访问不续命」由 T1 用例锁死；S4 零事故迁移由保守口径 + INV-1 影子偏差监控背书 | ✅ |
| D2 一致性 | S3「三处口径一致」：判定/清理/报表共用 `ledger_policy`；文档面 `--doc-drift` 28 条声明 0 漂移 | ✅ |
| D3 边界与异常 | 载入归一化（缺 `expires_at` 保守补齐、坏 `ban_until` 作废重算）、空表显式放行、API 读不到=「未知」绝不当一致 | ✅ |
| D4 性能与成本 | 默认 `ledger_cleanup_mode="legacy"` **线上行为零变化**；双写仅一次 max 计算；分类七只读 API 并入 30 分钟自检 | ✅ |
| D5 可测性 | 新测试 10（ledger）+ 10（projection）+ 15（doc-drift）+ 5（inspect）+ 15（slot_drift 含他人增补 2）例，全绿 | ✅ |
| D6 兼容与演进 | 新读老（归一化）✅；老读新（JSON 多字段无害）✅；回滚=改配置 + 重启（S5）✅；**新旧实例禁止并行**已写明 | ✅ |

### 1.2 运维（线上会不会被搞坏）

| 维度 | 查了什么 | 结论 |
|---|---|---|
| D1 正确性 | 最坏情况=影子偏差出现负值 → `_LEDGER_SHADOW.violations` 计数 + `/debug/state` 暴露，可回滚；默认模式不改删除行为 | ✅ |
| D2 一致性 | `reload_singbox_config` docstring 与 INC-017 冲突处已改写（D-2）；文档 150→300 修后文内自洽 | ✅ |
| D3 边界与异常 | 分类七留在 `_config_consistency_check` 异常隔离壳内（T8 实证：观测异常不掀翻服务）；空表保护保留 | ✅ |
| D4 性能与成本 | 分类七只读、30 分钟一跑；自愈沿用 bc89daa 限速（180s/900s），本轮零新增重启路径 | ✅ |
| D5 可测性 | `/debug/state` 新增 `ledger_shadow`、`ledger_rank_dist`；自检报告新增 `selector_drift` / `selector_unverifiable` | ✅ |
| D6 兼容与演进 | 灰度 P0（影子）已随 P1 落地；P2 切判据=改 `ledger_cleanup_mode` + 重启即可回退 | ✅（runbook 待补，见 F-3） |

### 1.3 复核者（代码、注释、文档是否自洽）

| 维度 | 查了什么 | 结论 |
|---|---|---|
| D1 正确性 | **D-1 单一事实源两处均落实**：释放时刻算法收口 `asip/ledger_policy.py`（判定/写入/影子/报表共用）；成员比对收口 `asip/config_projection.py`（自愈/巡检/审计共用）——口径分裂的根治而不是搬家 | ✅ |
| D2 一致性 | `--doc-drift` 比对 28 条活文档声明全一致（含修复的 2 处）；上一轮已对齐的 7 处 blocked 注释未回退 | ✅ |
| D3 边界与异常 | `blocked` 单向置真语义在 docstring 注明；坏值路径（`ban_kind` 非法、NaN/无穷 `ban_until`）归一化作废 | ✅ |
| D4 性能与成本 | 锁内全表遍历（`_warp_prefix_rest`）**仍在**——设计明说单独立项（D-5），本轮只让表长可控 | ⚠️ 已知遗留 |
| D5 可测性 | 三条锁窗口语义的用例（`test_warp_ip_reuse_window_and_cleanup` / `test_warp_ip_reuse_window_24h` / `test_ip_history_survives_hot_table_cleanup`）**零改动仍绿** | ✅ |
| D6 兼容与演进 | 老条目无 `expires_at` → 载入归一化补齐；测试桩改动只对齐签名不动断言 | ✅（glossary 登记缺口见 F-4） |

**红项：0。黄项：4（见下）。**

---

## 2. 监督发现清单

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| **F-1** | 🟡 外部 | **probe-ip 并行 WIP 漏改测试桩**：工作树里另一路未提交 WIP（`probe_warp.probe` 加 `ip=` 参数 + `asip.gate_rotation_policy` import，hunk 在 daemon.py:7801/12431，与三路施工区域完全不相交）更新了 `test_probe_warp.py` 但漏了 10 处旧签名桩（`test_daemon_unit.py` 1 处、`test_warp_refresh.py` 9 处）→ 全量 6 红 + 1 红 | 主控裁决 A：机械对齐桩签名（`lambda port, timeout` → `+ ip=None`），断言语义零改动，**归属记在 probe-ip WIP 头上**。修后 7 红清零 |
| **F-2** | 🟡 残留 | 同款旧桩还剩 2 处（`test_daemon_unit.py:4663/4681`），当前用例不经过它们（一直绿） | 按最小干预不动；**建议 probe-ip WIP 责任人整体清一遍测试桩** |
| **F-3** | 🟡 计划外 | integrator 为 `test_config_consistency.py` 加了 autouse fixture（隔离分类七的输入面，防真实 API/磁盘漂移混进 problems 精确计数断言；0.22s→稳定 0.29s） | **接受**：理由正当（该文件 12 例对 problems 计数做精确断言，不隔离必 flaky），且不在禁改清单 |
| **F-4** | 🟡 缺口 | `expires_at` 与「六面」模型尚未进 `docs/glossary.md` / `docs/established-facts.md`（设计 D-8）；runbook 串行部署条款待补 | 留待 P2 切判据前补（一行级文档活） |
| F-5 | 🟢 记录 | `test_slot_drift.py` 从 13 例变 15 例（probe-ip WIP 责任人增补），全绿，无冲突 | 无需动作 |

---

## 3. 关键验收证据

| 证据 | 实测 |
|---|---|
| 全量测试 | **1319 passed, 0 failed**（189.49s；含此前 4 个存量失败的 test_trace_req——probe-ip WIP 责任人已同步修好） |
| 三条窗口语义用例 | 专项复跑 3 passed，且 `git diff` 证明 test_daemon_unit.py 仅 1 行桩签名变动 |
| `--doc-drift` | EXIT=0：28 条声明全一致（修复的 2 处：`proactive_after_dsv4_ok` 150→300 于 CLAUDE.md:36 与 docs/subsystems/switching.md:25） |
| `--runtime-drift` | 实跑 8 面（main + slot-1..7）产物 147 / 生效 147 全一致——上午 147/108 的漂移已被 bc89daa 自愈抹平 |
| 分类七实跑形态 | `selector_drift` 每面 verdict/missing/extra + `selector_unverifiable` 单列；休眠/占位 tag 被 known 集过滤不误杀（实证） |
| 影子统计 | `_LEDGER_SHADOW` 暴露于 `/debug/state`（checked/violations/deviation 极值）；INV-1 违例计数恒监控 |
| 行为不变性 | `ledger_policy` 纯函数委托前后：test_daemon_unit 254 例 + test_channel_report 23 例全绿；`_slot_members_drift` 委托后 test_slot_drift 15 例全绿 |

---

## 4. 安全不变量与回滚（运维向）

| 不变量 | 机制 | 状态 |
|---|---|---|
| INV-1 `expires_at` ≥ 该轮最晚解禁时刻 | `compute_expires_at` 公式结构性保证（retention ≥ 0）+ 影子偏差监控 | ✅ 机制 + 监控双保险 |
| INV-2 `expires_at` ≥ last_ok_ts + 窗口 | EVENT_SUCCESS 锚点 = last_ok_ts | ✅ |
| INV-3 单调不减 | 所有续命路径取 max(current, candidate)；不续命路径不动 | ✅ T 用例锁死 |
| INV-4 删除判据唯一 | `ledger_cleanup_mode="expires_at"` 时唯一判据；**当前默认 legacy = 线上行为零变化** | ✅ |
| 回滚 | P1↔P2 之间：改 `ledger_cleanup_mode` + 重启即可，不需回滚代码 | ✅ |

**上线提示**：本轮改动只有注释、双写字段、影子统计、只读巡检——运行中的 daemon 仍加载旧代码，**重启与否都不影响当前行为**；真正的行为切换（P2）在 `ledger_cleanup_mode` 从 legacy 切到 expires_at 时发生，届时按设计观察表长/命中率/档位分布。

---

## 5. 遗留与建议下一步

1. **P2 切判据**（`ledger_cleanup_mode="expires_at"`）：观察满 24h 影子一致率 ≥99.9% 后再切；切后重点看表长收敛与 403/429 命中率。
2. **probe-ip WIP 责任人**：整体清理测试桩签名（F-2 的 2 处残留 + 防再漏），并考虑给 `probe_warp.probe` 的调用点测试加签名契约检查。
3. **D-8 文档登记**：`expires_at` 语义进 glossary、六面模型进 established-facts、runbook 补「新旧实例禁止并行 + 串行部署」。
4. **单独立项**（设计明确不混做）：落盘合并/延迟写（62 次/分钟全量序列化仍在锁内）；`blocked` 字段可复位；`_warp_prefix_rest` 锁内全表遍历。
5. **提交建议**：本次改动跨 10+ 文件且与 probe-ip WIP 混在工作区，**逐块看 diff 后分开提交**（台账生命周期 / 配置漂移集成 / 测试桩对齐 三笔），避免混为一笔难回溯。

## 6. 黄项清账（2026-11-13 17:5x 追记）

| # | 处置结果 |
|---|---|
| **F-2** 残留桩 | ✅ 已清：`test_daemon_unit.py` 两处旧签名桩按裁决 A 机械对齐（补 `ip=None`，断言语义零改动），`test_daemon_unit` 254 例全绿 |
| **F-4** 文档登记 | ✅ 已补：`docs/glossary.md`「三、状态与判定」表加 `expires_at` 行 + 新增「六之三、配置六面（对账模型）」；`docs/established-facts.md` 新增第 32/33 条（`blocked` 永不复位与 `expires_at` 生命周期、写盘≠生效与六面模型）。`--doc-drift` 改后仍 EXIT=0 |
| **F-4** runbook | ✅ 已补：`docs/runbook/ledger-cleanup-switch.md`——铁律（新旧实例禁止并行 + 两套删除口径的危害）、P2 前置门槛（影子 ≥24h / 一致率 ≥99.9% / violations=0）、串行切换四步、回滚只回配置不迁数据、失败处置三行 |
| F-1 / F-3 / F-5 | 维持原处置（归属记账 / 接受 / 无需动作）；锁内全表遍历仍为设计明确的另立项遗留 |

**运维事实更正（18:2x，重要）**：上一条「运行中 daemon 无 `ledger_shadow`」是**误读**——`/debug/state` 需要 `X-Asip-Token` 鉴权，先前读到的 `{"ok":false,"reason":"unauthorized"}` 被误当成键缺失。带鉴权重查（18:2x）：`ledger_shadow = {checked: 193, violations: 0, exact_match: 188, ...}`——**P0 影子统计自代码上线起一直在积累**。台账的 daemon.py 改动在 15:5x 前已被并行提交（566ba06 起）整包收编进 HEAD，故 16:02 起的 daemon 即携带台账代码。连续观察时钟以 18:19 重启起算最保守（计数器随重启清零），最早 2026-11-14 18:19 评估 P2。读此接口必须带 token，勿再拿 401 当「键不存在」。

**发现 F-6（🟡 指标口径）**：P2 门槛「影子一致率 ≥ 99.9%」与实现口径不匹配。`exact_match` 记的是「锚点恰等于释放时刻」（偏差 = 0），而干净条目（无惩罚锚点）天然不精确命中——实测 188/193 ≈ 97.4%，被约 2.6% 的干净条目写入稀释，**该比率永远够不到 99.9%**。P2 硬门槛应取 `violations == 0`（INV-1 本身）持续 ≥ 24h 且 `checked` 覆盖数千次写入；`exact_match` 率仅作观察参考。runbook 门槛措辞已同步修正；设计文档 §3.7 的「99.9%」措辞待主人确认口径后一并改。

**发现 F-7（🔴 入库一致性）**：`asip/ledger_policy.py` / `test_ledger_lifecycle.py` 仍是 **untracked**，而 HEAD 的 `daemon.py` 自 566ba06 起就 `from asip import ledger_policy`——HEAD 的自洽性靠工作区未跟踪文件维持，任何 `git stash` / 新检出 / CI 拉取都会 **ImportError**。须尽快补入库（见 `docs/commit-plan-20261113.md` 第一笔）。

---

> 本报告由监督路独立取证：测试结论均来自主控复跑（非转述子代理汇报），代码事实以 `git diff` 与实跑输出为准。数字采集时刻 2026-11-13 16:3x–16:5x，守护进程常驻、工作区有并行 WIP，复查时以当时 `git status` 为准。
