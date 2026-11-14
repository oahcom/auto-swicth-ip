# patch-20261113：配置漂移 daemon 集成规格（C 项分类七 / D-1 收口 / D-2 docstring）

状态：规格（供 daemon 集成实施方照做；本文档不含大段代码，只到函数级）。
配套已落地：`asip/config_projection.py`（单一事实源）、`tools/asip_audit.py`
`--doc-drift` / `--runtime-drift`（E 项）、`test_config_projection.py`、
`test_asip_audit_doc_drift.py`。设计蓝本：`docs/design-20261113-config-drift.md`。

**边界**：本规格涉及的 `daemon.py` 改动由集成实施方执行；E 项实施方
（本文档作者）只读 daemon.py。`config.py`、现有测试文件不在本规格改动范围。
行号为 2026-11-13 快照（daemon.py 并行改动频繁，以符号名定位为准）。

---

## 1. 背景（为什么必须集成）

- 热重载 ≠ 生效（INC-017）：`PUT /configs?force=true` 返回 204 但
  **不重建 outbound**，进程内 selector 成员仍是旧的。2026-11-13 实测：
  磁盘 7 份配置各 147 条成员，运行时只有 108 条，10 条新鲜 OK 线一条都
  塞不进 selector（PUT 得 400 `Selector update error: not found`）。
- 六面对账里「生效面」（进程内 selector 成员）是唯一盲区：声明面 /
  产物面 / 单元面 / 台账面 / 档位面都有人查，生效面此前没人查。
- bc89daa 已落 A/B/D 三项：`_slot_members_drift`（统一判据）、
  `_slot_drift_after_reload`（方一，源头校验）、`_slot_drift_maybe_heal`
  （方二，keeper 兜底自愈，`singbox.slot_drift_heal` 开关）——
  **不要重做**。缺的是：巡检分类七（C 项，报告面）、docstring 收口
  （D-2）、比较逻辑收口到纯函数（D-1）。

## 2. 改动一（D-1 收口）：`_slot_members_drift` 委托 `asip.config_projection`

现状（`_slot_members_drift`，约 daemon.py:12906）：自己算
`missing = disk - rt` 并过 `_warp_member`。改为委托，**行为逐条不变**：

| 返回 | 映射 |
|---|---|
| `_slot_selector_members(slot)` 为 None | `compare_members(..., runtime=None, ...)` → verdict=unknown → 返回 `None` |
| 否则 | 返回 `set(res.missing)`（空集=无漂移，非空=缺失的真实线） |

- known_line_tags 传 `{m["tag"] for m in _warp()["members"]}`（`_warp_member`
  的等价集合）；expected 传 `_slot_disk_selector_members(slot)` 的返回
  （保持「读不到=空集」的旧约定，**不**传 None —— 行为不变）。
- 调用 `asip.config_projection.compare_members` 一处，**禁止**在 daemon
  里再写第二份集合比较/过滤逻辑（缺失、多余、休眠 tag 过滤、unknown
  判定全部以纯函数为准，对照表见 §5）。
- 多余集（`res.extra`）此处**丢弃**：现自愈语义只按缺失集触发（行为不变）。
  多余集由分类七（§3）与 `asip_audit --runtime-drift` 报告。

## 3. 改动二（C 项）：`_config_consistency_check_impl` 新增分类七「产物面 ⊄ 生效面」

位置：分类六（`retired`，约 :15382）之后、`report` 组装（:15386）之前。

### 3.1 输入装配（复用现有读函数，不自己读文件）

对**每个 selector 面**（主配置 + 每个消费槽位）：

- 产物面 expected：`_slot_disk_selector_members(slot)`（:12893）。
  主配置可构造 pseudo-slot（`config_file=CFG["singbox"]["config_file"]`、
  `selector=CFG["warp"]["selector"]`）复用同一函数；建议顺手抽
  `_disk_selector_members(config_file, selector)` 通用读取再让两者委托。
- 生效面 runtime：`_slot_selector_members(slot)`（:12850，None=读不到/空
  all）。主面用 `singbox_api("/proxies/" + selector)` 取 `all`，同语义
  （非 list 或空 → None）。
- 台账已知线 known：`{m["tag"] for m in _warp()["members"]}`。
- 比对：`asip.config_projection.compare_members(expected, runtime, known)`。

### 3.2 输出与计数（报告为主）

- `report` 新增 `"selector_drift"`：`[{face, missing, extra, verdict}...]`，
  verdict ∈ consistent / drift / unknown。
- `problems` 计数只加 verdict=drift 的 `len(missing) + len(extra)`；
  **verdict=unknown 不计 problems 但绝不当一致**：单列
  `report["selector_unverifiable"]`（面名列表）+ detail 文案
  「N 个面无法判定（Clash API 读不到）」。
- `detail` 增加分类七文案：「产物面 ⊄ 生效面：X 个面漂移（缺失 M 条 /
  多余 K 条）」，tag 取前 5 个与既有 detail 风格一致。

### 3.3 纪律（两条硬约束）

1. **异常隔离壳（事故先例）**：分类七代码必须写在
   `_config_consistency_check_impl` 内部，自动被
   `_config_consistency_check`（:15250）的 try/except 壳兜住 ——
   2026-11-11 17:22:31~17:27:21 自检内部 NameError 冒泡到 `main()`，
   systemd `Restart=always` 崩溃循环 23 次、服务瘫 5 分钟。观测函数
   失效 = 少一条告警，不许掀翻服务；**不得**另起线程或绕过外壳。
2. **自动触发不重复**：分类七只报告，**不**调 `_restart_slot_process`、
   不 `_slot_drift_maybe_heal`。自动自愈沿用 bc89daa 既有机制
   （方一 `_reload_config_set` 后置校验 → `_slot_drift_after_reload`；
   方二 keeper 兜底 `_slot_drift_maybe_heal`，含 `_SLOT_DRIFT_MIN_GAP=180`
   与 `_SLOT_DRIFT_COOLDOWN=900` 限速）——巡检再触发一遍会打穿限速。

## 4. 改动三（D-2）：`reload_singbox_config` docstring 修正

现状确认（2026-11-13 读取，:10718-10728）：docstring 仍写

> 实测 sing-box 1.13.0（本机，2026-11-09）：PUT /configs?force=true → 204，
> 耗时 0.01s，50ms 粒度探测 7890 全程 0 次失败。

bc89daa **未**修正此口径（`_reload_config_set` 的后置校验是代码层补的），
docstring 的「204=完成」暗示与 INC-017「204≠已生效」直接冲突 → **需改动**。

应改要点（保留「热重载不断流」的实测证据，去掉「=已生效」暗示）：

1. 204 只代表「配置文件被接受并加载」，**不代表 selector 成员已更新**
   ——热重载不重建 outbound，进程内 `all` 仍是旧成员（INC-017）。
2. 指向两级校验：`_reload_config_set` 后置 `_slot_drift_after_reload`
   （3 次 × 1s）→ 发现漂移滚动单槽重启；keeper 兜底
   `_slot_drift_maybe_heal`。
3. 只有整进程重启（`_restart_slot_process` / `restart_singbox`）才会
   重建 selector 成员；「7890 全程 0 失败」只证明**热载不断流**。
4. docstring 结尾补一句指向 `asip.config_projection`（判据唯一）。

## 5. 语义对照表（`_slot_members_drift` ↔ `asip.config_projection`）

| 语义点 | `_slot_members_drift` 现行为 | `asip.config_projection.compare_members` | 关系 |
|---|---|---|---|
| 运行时读不到/空 all | 返回 None（调用方按无法判定处理，不得当漂移重启） | runtime=None → verdict=unknown，**绝不判一致**（T5 假阴性防护） | 一致 |
| 缺失集 | `disk - rt` 过 `_warp_member`（只留台账真实 WARP 线；休眠/占位 tag 不构成漂移，防误杀） | `expected - runtime` 过 `known_line_tags` | 一致（known 传 members tag 集即与 `_warp_member` 等价） |
| 多余集 | 不计算（返回值只有缺失集） | `runtime - expected`，同样过 `known_line_tags` | **扩展点**（新输出；daemon 委托丢弃，行为不变） |
| 「一致」判据 | 返回空集 | missing 与 extra 均空 | 缺失口径一致；多余非空判 drift，委托方只取 missing 时旧口径不变 |
| 产物面读不到 | `_slot_disk_selector_members` 返回空集 → 按无漂移（历史行为） | expected=None → unknown | **扩展点**（daemon 委托传空集保持旧行为；审计传 None 防假阴性） |

## 6. 配套测试断言要点（新增文件，不改现有测试）

### T3：巡检主动发现分类七（新文件 `test_config_consistency_selector_drift.py` 或并入集成方新测试）

1. 构造：磁盘 selector 成员含台账真实线 tag X，运行时 `all` 不含 X
   （monkeypatch `_slot_selector_members` / 主面 API 读取）。
2. 调 `_config_consistency_check()`：断言 `report["selector_drift"]` 对应
   面 `missing` 含 X，`problems` 计数增加，detail 出现「产物面 ⊄ 生效面」。
3. 运行时读不到（返回 None）：断言该面 verdict=unknown、进
   `selector_unverifiable`、**不计** problems 且**不报一致**
   ——「无法判定」绝不洗成通过。
4. 休眠/占位 tag 不算漂移：disk 里多一个非台账 tag、运行时缺它 →
   不进 missing（防误杀回归）。

### T8：异常隔离不掀翻服务

1. monkeypatch `asip.config_projection.compare_members` 抛
   RuntimeError（或让输入装配的读函数抛异常）。
2. 调 `_config_consistency_check()`：**不得向外抛**（pytest.raises 反向
   断言）；返回 dict 含 `selfcheck_failed: True` 与非空 `error`；
   日志含「配置自检本身出错（已忽略，不影响服务）」。
3. 对照事故先例（2026-11-11 17:22，restart counter 23，瘫 5 分钟）锁死
   「观测失败 = 少一条告警」的故障模型。

## 7. 验收清单

- [ ] `_slot_members_drift` 委托 `compare_members`，`test_slot_drift.py`
      13 例仍全绿（行为不变的回归证据）。
- [ ] `_config_consistency_check_impl` 有分类七，T3 断言全过。
- [ ] T8 断言全过（异常隔离壳未被绕过）。
- [ ] `reload_singbox_config` docstring 不再含「204=完成/已生效」口径。
- [ ] 全量 `pytest` 无回归；daemon 无新写路径（分类七只读）。
