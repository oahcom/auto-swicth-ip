# 台账清理判据切换（ledger_cleanup_mode）部署 Runbook

适用范围：把 `.warp_ip_used.json` 的清理判据从 `legacy`（`ts` + 24h 窗口）切到 `expires_at`（生命周期字段），即 `docs/design-20261113-ledger-lifecycle.md` 的 **P2 阶段**。P0 影子计算与 P1 双写随代码上线，无需本 runbook。

---

## 铁律：新旧实例禁止并行（设计 R-4）

新代码按 `expires_at` 删条目，老代码按 `ts` + 窗口删条目——**两套删除口径**。两个实例同时在跑（哪怕只是忘了停旧的）会互相删对方要留的条目：

- 老实例按 `ts` 窗口删掉新实例刚建、尚未到期的条目 → 惩罚中的地址被当「从未出现过」（档 0 最优）重新进池，直接引发 403/429 爆发；
- 新实例按 `expires_at` 删掉老实例眼里的活跃条目 → 复用冷却失效，脏 IP 被快速复用。

**因此部署必须串行：先停旧实例，确认退出后，再起新实例。禁止热切换、禁止滚动并行、禁止新旧版本同机同时跑。**

---

## 前置条件（进入 P2 的门槛，缺一不可）

`/debug/state` 的 `ledger_shadow` 必须同时满足下列四条（须带 `X-Asip-Token` 头鉴权读取）：

1. **`missing_expires == 0`**：全表没有「缺 `expires_at`」的条目。这是写入口收敛是否彻底的直接证据——只要探针建账等旁路还在直写台账，这个数就会随探测动态再生（实测 18:53→18:56 从 20 涨到 27）。非 0 时切 P2 的后果是「刚建的干净条目被当未迁移处理」（本版已改为先补齐再判，但缺口本身说明还有写路径没收敛）。
2. **`would_delete` 试算干净且可解释**：该值由修正后的 P2 谓词对全表干跑得出（与实删共用 `ledger_policy.cleanup_verdict`），即「若此刻切 P2 会删多少条」。要求：与只读模拟的预期条数同量级、且 `would_delete_samples` 里不出现 `blocked=true` 或 `rank_probe/rank_ok=true` 的条目特征；`unmigrated == 0`（无法补齐、清理会跳过的条目数为零）。
3. **`violations == 0` 且起点可证**：从 `started_at`（影子落盘自带，跨重启保留）起**连续 ≥ 24h**。`violations` 是分人群口径：惩罚期档校验 INV-1（锚点 ≥ 判定侧释放时刻），干净档校验 INV-2（锚点 ≥ 最近成功时刻）。`checked` 须覆盖数千次以上写入。`exact_match` 仍只作观察参考——它现在仅统计惩罚期条的精确命中，干净条目天然不计入，**不作门槛**。
4. **全量测试绿**：至少含 `test_ledger_lifecycle.py`（T1–T8 + INV + W1–W6 + 不变量）与三条锁窗口语义的用例（`test_warp_ip_reuse_window_and_cleanup` / `test_warp_ip_reuse_window_24h` / `test_ip_history_survives_hot_table_cleanup`）；
5. **确定的回滚点**：代码已提交或有明确快照（新旧代码都能读带 `expires_at` 的文件，回滚只回配置不回数据）。

---

## 切换步骤（串行）

1. 改配置：`ledger_cleanup_mode` 由 `"legacy"` 改为 `"expires_at"`（config.py 单点改）；
2. **停**旧实例（确认进程真退出、无残留实例——`ps` 查不到 daemon 进程为准）；
3. **起**新实例；
4. 验证四件事：
   - 启动日志有载入归一化统计（补齐 `expires_at` 的条数、坏 `ban_until` 清洗条数）；若出现「无可用时间锚点」告警，说明存量里有坏条目，先查清再切；
   - `/debug/state` 的 `ledger_shadow`：`violations == 0`、`missing_expires == 0`、`unmigrated == 0`，且 `started_at` 已是 24h 前的时刻（续计未被重置）；
   - `python3 tools/channel_report.py --blocked-ips` 的「生效」条数与切换前同量级（不应骤变）；
   - 台账条目数变化在模拟预期之内（保守口径下首批只删 1~3 条冷数据是常态；24h 后开始按生命周期正常回收）。

---

## 回滚（改配置 + 串行重启，不用回滚代码、不用迁数据）

1. 改回 `ledger_cleanup_mode: "legacy"`；
2. 串行重启（先停后起，同上）；
3. 老代码读新文件无害：JSON 里多出的 `expires_at` 字段会被忽略，清理回到 `ts` + 窗口口径。

---

## 失败处置

| 症状 | 处置 |
|---|---|
| 启动日志报归一化异常，或 `/debug/state` 缺 `ledger_shadow` | 代码版本不对（跑了未含 P0/P1 的版本）→ 停机换版本后重来 |
| `missing_expires > 0`（且在涨） | 有写路径绕过收敛入口（探针建账/新补建账）→ 切 P2 前必须先修写入口，否则每条都靠「先补齐再判」兜底 |
| `would_delete` 里出现 `blocked=true` 样例 | INV-1 疑似破防 → 不切 P2，取证看 `would_delete_samples` 与 `.warp_ip_history.jsonl` 时间线 |
| 台账条目数骤降超过模拟预期（>5%） | 立即回滚；保留 `.warp_ip_used.json` 现场副本，按设计 R-3（迁移误删）排查 `compute_expires_at` 的锚点 |
| 出现「惩罚中地址被选中」引发的 403/429 爆发 | INV-1 破防 → 立即回滚到 `legacy`；取证看 `_LEDGER_SHADOW` 统计与 `.warp_ip_history.jsonl` 的删除/写入时间线 |

---

相关文档：[design-20261113-ledger-lifecycle.md](../design-20261113-ledger-lifecycle.md)（§3.6 兼容矩阵、§3.7 灰度四阶段、§4 风险登记册）· [glossary.md](../glossary.md) 六之三
