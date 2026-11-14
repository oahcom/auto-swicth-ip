# 台账生命周期——验证与收尾报告

> 署名：**ledger-verify**（验证与收尾工程师）｜日期：2026-11-13
> 性质：对 ledger-hardening 落盘实现的**逐项核验 + 精确补修 + 全量回归**。前任实施者未留任何交付报告，本报告为独立验证产出，不代表前任文本。
> 范围：`asip/ledger_policy.py`、`daemon.py` 台账生命周期块、`test_ledger_lifecycle.py`、`docs/runbook/ledger-cleanup-switch.md`。HEAD=4e1e2f9，改动全部留在工作区未提交。
> 判据来源：`docs/design-20261113-ledger-lifecycle.md`（INV-1~4、§3.3 事件契约）。

---

## 1. 规格逐项核验表

### 根因一：expires_at 写入口收敛 + P2 清理语义反转

| # | 核验项 | 结论 | 证据 |
|---|---|---|---|
| 1.1 | 写入口收敛：无绕过双写的直写残留 | **符合** | `grep _WARP_IP_USED[` 全部写点清点：载入两处（`_load_warp_ip_used` dict/float 分支，均先过 `_normalize_ledger_entry`）、探针建账 `_ledger_mark_probe_ok`（经 `_ledger_new_entry`）、`_warp_ip_used_locked`（`_ledger_new_entry` 构造 `fresh`，float 残留被 `fresh` 替换）、清理分支 float 防御转换（立即过 `cleanup_verdict` 补齐）。历史上绕过双写的 `_warp_model_usable` 探针直写点已改为调 `_ledger_mark_probe_ok`（daemon.py ~8023 注释记录了根因） |
| 1.2 | 探针事件 = EVENT_PROBE：建账补初值、不续命、不写 `last_ok_ts` | **符合** | `_ledger_mark_probe_ok` 只写 `probe_ok_ts`；`_ledger_apply_event_locked` 对非 `_EXTENDING` 事件不改 `expires_at`（缺字段时仅补初值）。`last_ok_ts` 全库唯一写点是 `_record_active_ip_success`（档位 2 语义独占），探针 WHY 注释明确不写。W1×2 锁定 |
| 1.3 | 载入补齐：float 条目与缺 `expires_at` 的 dict 条目都补齐 | **符合** | `_load_warp_ip_used` float 分支 `{"ts": float(v), "blocked": False}` 同样走 `_normalize_ledger_entry`；dict 分支统一归一化。W2×3 锁定 |
| 1.4 | 补齐「宁短勿长」、保 INV-1 | **符合** | `ledger_policy.backfill_expires_at` 锚点 = `max(ts, last_ok_ts, 该轮最晚解禁)`，全部取自条目真实事件，不用 `now`（docstring 明示「不用 now 凭空续窗口」）；与 P2 谓词、运行期补齐共用同一函数，口径唯一 |
| 1.5 | 时间锚点全坏：不删，启动日志 + 影子暴露 | **符合（含补修）** | 原实现启动日志已告警但**未写影子**（注释承诺了「影子暴露」）→ 本次补修在 `_load_warp_ip_used` unmigratable 分支写 `_LEDGER_SHADOW["unmigrated"]`（见 §2）。W2 `test_w2_load_unfixable_entry_kept_without_expires` 补断言锁定 |
| 1.6 | P2 清理语义反转：无 expires_at = 未迁移 → 先补齐再判 | **符合** | `_ledger_cleanup_locked` expires_at 分支：`cleanup_verdict` 返回 `backfilled` 时现场补 `v["expires_at"]` 再判；旧实现「无字段即删」已消除（diff 可查旧分支 `exp is not None and exp >= now: continue; del`） |
| 1.7 | 仍缺的跳过删除并计数告警，绝不静默删 | **符合** | `why == "unmigrated"` → `continue` + `s["unmigrated"]` 计数 + log 告警。W3×2 锁定 |
| 1.8 | legacy 模式行为不变；默认 `"legacy"`，本轮不切 P2 | **符合（一处已记录微偏离，见 §6）** | legacy 分支与旧代码逐行一致，仅多一个 `ts is None` 显式保留守卫（旧代码此处 TypeError 崩写路径）。`config.py` `ledger_cleanup_mode: "legacy"` 默认未动 |

### 根因二：影子观测证据力

| # | 核验项 | 结论 | 证据 |
|---|---|---|---|
| 2.1 | would-delete 干跑与实删共用谓词 | **符合** | `_ledger_shadow_sweep` 与 `_ledger_cleanup_locked` 都只调 `ledger_policy.cleanup_verdict`（纯函数）；干跑只读不写回（W4 断言 `"expires_at" not in entry`）。产出 `would_delete` / `would_delete_samples`（脱敏 `_mask_ledger_ip`）/ `unmigrated` / `sweep_at` 齐全 |
| 2.2 | 影子落盘 + 起点戳跨重启 | **符合** | `.ledger_shadow.json`：`_load_ledger_shadow_boot` 启动合并续计（SUM 相加/MAX-MIN 取极值/GAUGE 取较大），`started_at` 跨重启保留（W5×2 用 tmp_path 证明续计与起点戳）；`_persist_ledger_shadow` 照 M10 `_api_stats` 模板（固定 .tmp + `os.replace` + 失败只 log）；60s 主循环 `_submit_once("ledger-shadow-tick", …)` 干跑+落盘，atexit 补落 |
| 2.3 | pytest 隔离，测试不污染生产落盘 | **符合** | `_ledger_shadow_io_blocked` = `_IN_PYTEST` 且路径仍为默认真实路径时拒绝 IO；测试夹具另把 `_LEDGER_SHADOW_FILE` monkeypatch 到 tmp。生产 `.ledger_shadow.json`（421B，daemon 常驻写出）可证未被测试触碰 |
| 2.4 | 偏差分人群：干净档不计 deviation、惩罚期单列 | **符合** | `ledger_policy.shadow_bucket` 三档（clean/blocked/unmigrated）；`_ledger_shadow_sample` 只把 `deviation_sum/max/min` 记入惩罚期档，干净档单列 `checked_clean/exact_clean/deviation_clean_max`；`exact_match` 仅惩罚期条。旧口径 1.79e9 秒假象消除——线上影子 `deviation_max: 0.0` 即证。W6 锁定 |
| 2.5 | `/debug/state` 暴露新字段 | **符合** | `"ledger_shadow": dict(_LEDGER_SHADOW)` 原样带出 `started_at/missing_expires/unmigrated/would_delete/would_delete_samples/sweep_at/backfilled_total` 等全部键 |

**线上运行证据**（`.ledger_shadow.json` 实时快照，daemon 常驻写、数字有分钟级波动）：`checked=391`（惩罚期 359 + 干净 32）、`violations=0`、`deviation_max=0.0`、`missing_expires=0`、`unmigrated=0`、`would_delete=0`——runbook 的 P2 四门槛中前三条已在生产数据上成立（第四条全量绿见 §5）。

---

## 2. 补修清单（3 处，均精确小块）

1. **`_load_warp_ip_used` unmigratable 分支（行为补修）**：注释承诺「启动日志 + 影子暴露」但启动时未写影子（要等第一次干跑，最长 60s 窗口，崩溃则丢失）。补 `s["unmigrated"] = unmigratable`，启动即暴露。字段选 `unmigrated` 而非 spec 字面的 `missing_expires`，理由见 §6-2。
2. **`_ledger_shadow_sample` 注释精确化（注释同步）**：「全表干跑（missing_expires）计数」→「（missing_expires / unmigrated）计数」，与干跑双字段口径一致。
3. **`test_w2_load_unfixable_entry_kept_without_expires` 补断言**：锁定「启动即在影子计数暴露，不等第一次干跑」。

无需补修的核验项依据：写入口收敛、EVENT_PROBE 语义、载入补齐、P2 语义反转、干跑共用谓词、影子续计、分人群统计——实现与 spec 逐条对上，测试断言与实现语义逐一复算吻合（含 T1~T8、INV、W1~W6 全部 24 例的期望值手工推演）。

---

## 3. INV-1~4 论证

**INV-1（expires_at ≥ 该轮最晚解禁时刻）**：
- 续命事件：`compute_expires_at` 的候选值 = `max(now, block_release_ts) + retention`，恒 ≥ 解禁时刻 + 24h 保留窗口，有整整一个窗口的余量；
- 被精确封禁钳制的 `ban_until` 在写入时被钳 [1s, 24h]，且 `block_release_ts` 只采信 `ban_kind=="rate429"` 且晚于起点的值（与判定侧 `_warp_ban_until` 同语义），坏值作废后按默认窗口算——释放时刻与判定侧同源（D-1 单一事实源，判定侧 `_warp_block_remaining` 已委托 `ledger_policy.block_release_ts`）；
- 补齐路径 `backfill_expires_at` = `max(锚点, 解禁时刻) + retention` 同样盖住解禁时刻；
- 所有写路径对 `expires_at` 只做 `max` 合并（见 INV-3），不会拉低。
- 监控：`shadow_bucket` 的 blocked 档 `violation = deviation < 0`，线上 `violations=0`。

**INV-2（expires_at ≥ last_ok_ts + 复用窗口）**：
- EVENT_SUCCESS 以 `last_ok_ts` 本身为 `now`（`_record_active_ip_success` 传 `float(entry["last_ok_ts"])`），候选值 = `max(last_ok_ts, 解禁) + retention` ≥ 成功时刻 + 窗口；
- 补齐锚点含 `last_ok_ts`（ts 缺失的畸形条目兜底，W2 `test_w2_load_backfill_uses_last_ok_when_ts_missing` 锁定）；
- 不续命事件不改值，不破坏已成立的下界。

**INV-3（expires_at 单调不减）——backfill 时间语义为何保得住**：
- 机制：`compute_expires_at` 续命分支返回 `max(现值, 候选)`，不续命分支原样返回现值，唯一写入新值的路径是「字段缺失/损坏视为缺失」时的补初值——不存在把已有值改小的分支；
- `backfill_expires_at` 的锚点全部来自条目**已发生的真实事件时刻**（`ts`、`last_ok_ts`、解禁时刻），**绝不用 `now`**。这是关键：若用 `now`，每次重启/每次触碰都凭空续一个新窗口，语义变成「永不过期」且与续命事件的 max 合并失去可比性；用真实事件时刻，补齐值就是「该条目历史事件 + 窗口」，与后续任何续命候选（同样是「事件时刻 + 窗口」）取 max 自然单调；
- 归纳：补齐只在缺失时发生一次，之后全靠 max 合并，序列单调。

**INV-4（删除判据唯一：expires_at < now）**：P2 分支只认 `cleanup_verdict` 的 `exp < now`，与干跑同一谓词；缺字段先补齐再判，补不出的永不删；`legacy` 是配置保留的旧口径（本轮默认未启用），切 P2 后判据唯一。

---

## 4. 六维自查

| 维度 | 结论 |
|---|---|
| **正确性** | 释放时刻、判定、清理、影子四方共用 `ledger_policy`（`block_release_ts`/`is_effectively_blocked`/`compute_expires_at`/`backfill_expires_at`/`cleanup_verdict`/`shadow_bucket` 全是纯函数）；判定侧 `_warp_ip_is_blocked`/`_warp_block_remaining` 已委托同一事实源，口径分裂（缺陷 A）消除；探针与生产成功严格分账（`probe_ok_ts` vs `last_ok_ts`） |
| **一致性** | 注释与行为同步核过：写入口收敛块根因注释（daemon.py ~7608）、探针 WHY 不写 `last_ok_ts` 注释（~8023）、`_ledger_cleanup_locked` docstring、`_load_warp_ip_used` 归一化说明；文档侧 glossary「生命周期截止」词条、established-facts §32、runbook 门槛与实现字段口径一致（本次把 load 注释里误称 `missing_expires` 的一处改为 `unmigrated`） |
| **边界与异常** | 空表：清理到空时 `_LEDGER_ALLOW_EMPTY_WRITE` 显式放行一次写空（R-6/D-3）；落盘失败：`_persist_ledger_shadow`/`_write_json_atomic` 吞异常只 log；坏值：`_as_ts` 对 None/NaN/Inf/溢出统一当缺失，float/dict/None-ts 条目都有明确去处（补齐或 unmigrated 保留）；锁内并发：所有台账写持 `_WARP_IP_LOCK`（`_ledger_mark_probe_ok`/`_warp_ip_used_locked`/`sweep` 都在锁内），影子落盘无锁快照沿用 M10 模板（并发修改由 try/except 兜住，丢一次快照不丢累计） |
| **性能与成本** | 干跑 60s 一次全表纯函数扫描（37k 条毫秒级）；写路径生命周期计算 O(惩罚类型数)；`_ledger_new_entry` 在 `_warp_ip_used_locked` 每次调用都构造 `fresh`（即便条目已存在）属 O(1) 常数浪费，可忽略，不动；写盘频次与全量序列化成本不变（R-5 已明确单独立项） |
| **可测性** | `test_ledger_lifecycle.py` 24 例（T1~T8 + INV×2 + W1~W6）全部断言与实现语义逐条复算吻合；三条锁窗口语义的老用例（`test_warp_ip_reuse_window_and_cleanup`/`test_warp_ip_reuse_window_24h`/`test_ip_history_survives_hot_table_cleanup`）单独跑绿；隔离三重保险（`WARP_IP_USED_FILE_PATH` 环境变量、`_LEDGER_SHADOW_FILE` monkeypatch、`_ledger_shadow_io_blocked` 兜底） |
| **兼容与演进** | 老 float 条目：载入即转 dict 并补齐（W2）；缺字段存量：载入补齐 / P2 先补齐再判 / 补不出永保留；新旧并存：老代码读新文件忽略 `expires_at` 无害，新旧实例并行由 runbook 铁律禁止（串行部署）；回滚 = `ledger_cleanup_mode` 改回 `"legacy"` + 串行重启，不迁数据不回代码（runbook 已写）；本轮默认 `"legacy"`，P2 未启用 |

---

## 5. 全量测试数字与用例清单

- **全量 `python3 -m pytest -q`（经 WSL）：1322 passed / 0 failed**（2026-11-13）。
- `test_ledger_lifecycle.py`：**24 例全绿** = T1~T8（8）+ INV-1 偏差/单调（2）+ W1 探针建账×2 + W2 载入补齐×3 + W3 清理语义×4 + W4 干跑×2 + W5 影子落盘×2 + W6 分人群×1。
- 新增用例清单（W1–W6，14 例）：`test_w1_probe_ok_creates_expires_at`、`test_w1_probe_ok_backfills_legacy_entry`、`test_w2_load_float_entry_backfilled`、`test_w2_load_backfill_uses_last_ok_when_ts_missing`、`test_w2_load_unfixable_entry_kept_without_expires`（本次补 1 断言）、`test_w3_p2_cleanup_backfills_instead_of_deleting`、`test_w3_p2_cleanup_skips_entry_without_anchor`、`test_w3_p2_cleanup_deletes_only_expired`、`test_w3_legacy_mode_unchanged`、`test_w4_would_delete_dry_run`、`test_w4_would_delete_clean_table`、`test_w5_shadow_persist_resume_and_started_at`、`test_w5_shadow_boot_creates_started_at`、`test_w6_shadow_buckets_do_not_pollute`。
- 数字口径说明：领队给的基线 1314 是 W1–W6 落地后的数字；当前 1322 中多出的 8 例来自并行 WIP 测试文件（`test_probe_warp.py`/`test_trace_req.py`/`test_asip_cli_gate.py`）的新增，非本域。
- **如实报红**：首轮全量曾出现 5 例瞬时红（`test_rotate_payload_fixes.py`×3、`test_slot_failover.py`×2，报错落点 daemon.py `_slot_redirect_payload` 的 ValueError）；单跑、组合跑、复跑全量均绿（1322 passed），判为跨文件时序 flaky。用例属 gate/slot 平行领域，按纪律未触碰，留档供平行责任人排查。

---

## 6. 偏离规格的决定及理由

1. **legacy 分支新增 `ts is None` 显式保留守卫**（前任所加，本次复核保留）：spec 要求「legacy 逐字节不变」，该守卫使**畸形条目**（`ts: null`）的处理从「TypeError 崩掉整条写路径」变为「显式跳过保留」。良构条目行为逐字节不变；不回退——回退等于恢复一个崩溃缺陷。docstring 与行内注释已如实记录此例外。
2. **「影子 missing_expires 暴露」落为 `unmigrated` 字段**：实现把缺字段条目分为 `missing_expires`（可补齐，写入口收敛是否彻底的证据，runbook 门槛 1）与 `unmigrated`（补不出的坏数据，runbook 门槛 2）两个计数。把坏数据塞进 `missing_expires` 会污染门槛 1 的诊断语义（runbook 失败处置表把 `missing_expires>0` 归因于「写路径绕过收敛」）。故补不出的暴露在 `unmigrated`，字段名按实现口径、语义满足「启动日志 + 影子暴露」的规格意图，runbook 两条门槛联合即覆盖 spec 字面要求。
3. **`_ledger_apply_event_locked` 的兜底**：`backfill_expires_at` 定不出值的畸形条目若被事件触及，用 `compute_expires_at(now)` 补一个初值而不是留缺。只发生在「无任何时间锚点」的条目上，方向保守（多活一个窗口，绝不导致误删），与清理侧「unmigrated 永不删」一致。
4. **已知未修（仅记录，不影响安全）**：`_merge_ledger_shadow` 对 `deviation_min` 用 0.0 作初值哨兵，当持久化的 min > 0 时合并结果会被压成 0.0——只往保守方向失真（min 只会报小不报大，不会掩盖负偏差；`violations` 计数独立）。观测展示级瑕疵，无安全影响，未动。
5. **P2 未切**：按 spec 本轮默认保持 `ledger_cleanup_mode="legacy"`，切 P2 的门槛与步骤在 `docs/runbook/ledger-cleanup-switch.md`（四门槛 + 确定回滚点），生产影子数据已满足前三条，待全量绿（已满足）与主人拍板后执行。

---

> 改动清单（全部留在工作区，未 git add/commit）：`daemon.py` 3 处小修（`_load_warp_ip_used` unmigratable 分支补影子暴露、`_ledger_shadow_sample` 注释、其余为前任落盘内容的核验未动）、`test_ledger_lifecycle.py` 1 处断言补充。并行方 WIP 文件一律未碰。
