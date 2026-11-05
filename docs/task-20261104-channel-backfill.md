# 渠道封禁补位（2026-11-04）

> 主人原话：**「当一个渠道被封禁，必定要有另外一个渠道补位（封禁率最低的渠道其
> colo 的复制），到了八点解封后还原。严格按照此逻辑编写，不允许做更改逻辑操作。」**
>
> 本文只记「要做什么、做到什么算完成、怎么验证、哪些坑踩过」。代码里的逐行理由
> 在 `daemon.py` 注释里，这里不重复。

---

## 0. 一句话

渠道锁命中后，除了原有的「把这条线搬到低 ban 率 colo」（重定位），再**另起一条
渠道**——用已注册但没启用的空闲设备线，落到 ban 率最低的 colo 上——发布进主配置；
次日 08:00 解封时两条一起撤：补位渠道停单元、撤成员，原线换回原节点。

两者是**两个动作**，不能合并：重定位救的是「这条线自己」，补位保证的是「总有一条
渠道在服务」，因为被封的线在锁定期内不探测、不重启，可能长时间没有可用出口。

## 1. 渠道封禁判据（本次同时改的口径）

| 判据 | 条件 | 配置 |
|---|---|---|
| A | **当天**最近 `channel_ban_min_samples` 条模型层实测**全部** `quota`；出现任何一条 OK/flagged 就否决 | 1000 |
| B | 连续 `channel_noaddr_hold_sec` 探测不到出口地址（中途拿到一次就清零） | 5h |

样本只收地址级结论（OK / quota / flagged），探测自身失败（err / unreachable）不入账。

命中后：锁到次日 `channel_ban_reset_hour`（08:00，opencode 官方配额重置时刻），
`stop_probe=True`，`_warp_line_needs_probe` 与 `_probe_one_line` 两个入口都拦下。

**旧口径已废**（1h 窗口 100% 封禁 + 铺满半窗）：锁定期间还在探测、还在记账，到期
时窗口里的旧样本把它续到次日。实测 WARP-WG-21 在 11-03 08:00 被锁后，10:52~13:37
换两个全新 IP 出了 **7 次 OK**，锁依然留到次日 08:00:19 又重锁。

## 2. 补位渠道的完整流程

```
渠道锁命中（ban_hold_until 写入）
        ↓  每分钟 _rehome_held_lines
   _backfill_reconcile      ← 先对账：无主的行撤掉、行丢了清字段重开
        ↓
   _backfill_open(tag)      ← 锁定中且没有 ban_backfill_tag 的，开一条
        ├─ _channel_rehome_target(ignore_enabled=True) → (colo, node)
        │     取 _lowest_ban_colos 里第一个还有空节点的 colo
        ├─ _backfill_spare_indices() → 空闲设备线（profile.conf 在、不在 members、单元没 mask）
        ├─ 写线配置 → 起单元 → 等端口 → 写 .warp/backfill.json
        ├─ deploy/members.py 发 .warp/warp-outbound.json
        ├─ refresh_subscription → singbox-config.json → 重启 sing-box
        └─ 被封的那条正在服务流量 → selector 切到补位渠道
        ↓  次日 08:00
   _backfill_close(tag)
        ├─ selector 切回原线 → 停单元 → 撤成员 → 重发配置
        └─ 清 ban_backfill_tag / ban_backfill_node
```

### 为什么成员身份必须落盘

`deploy/members.py` 与 `parse_subscription.py` 都是**独立进程**，各自 import 一次
`config.py`。只改 daemon 内存里的 `CFG["warp"]["members"]`，它们看不见 —— 补位渠道
起来了也没有人能把流量切过去。所以写 `.warp/backfill.json`，
`config.py::_append_backfill_members` 在 import 时把它装回 `CFG`；daemon 重启后同样
能看见并撤销。

### 为什么补位不随重定位总闸关

`_backfill_open` 调 `_channel_rehome_target(..., ignore_enabled=True)`。补位是主人的
硬要求，`channel_rehome_enabled=False` 时重定位可以停，补位不能停。

### 补位渠道的两个「不参与」

- **不参与同块节点轮回**（`_node_rotation_pass` 跳过 `backfill_for` 成员）：它的落地
  节点就是补位目标本身，搬走就补错地方了。
- **不参与重定位**（`_rehome_held_lines` 里同样跳过）：同上。

### 不套娃

补位渠道自己也被封时不再开新线（`member.get("backfill_for")` 直接返回 None），
否则一条封禁能无限拉新线。

## 3. 代码位置

| 位置 | 作用 |
|---|---|
| `daemon.py:2940` `_channel_ban_trim` | 只留当天样本，最多 1000 条 |
| `daemon.py:2958` `_channel_ban_hold_hit` | 判据 A：全 quota 才锁 |
| `daemon.py:2981` `_channel_ban_note` | 记样本；命中写 `ban_hold_until` |
| `daemon.py:3018` `_channel_noaddr_note` | 判据 B：连续 5h 无出口地址 |
| `daemon.py:3060` `_channel_rehome_target` | 挑 (colo, node)；排除已占节点，含补位占的 |
| `daemon.py:3101` `_lowest_ban_colos` | ban 率从低到高的 colo（样本 ≥ 10） |
| `daemon.py:3116` `_rehome_held_lines` | 每分钟：到期解封 → 对账 → 补位 → 重定位 → 还原 |
| `daemon.py:3411` `_backfill_open` | 开补位渠道 |
| `daemon.py:3486` `_backfill_close` | 08:00 撤补位渠道并还原 selector |
| `daemon.py:3506` `_backfill_stop` | 停单元 + 摘成员（不碰台账行） |
| `daemon.py:3539` `_backfill_reconcile` | 每分钟对账台账与锁线 |
| `config.py:843` `_append_backfill_members` | 把 `.warp/backfill.json` 装回 `CFG` |

**状态字段**（`.warp_line_state.json`，记在被封的那条线上）：`ban_backfill_tag`、
`ban_backfill_node`。**台账文件**：`.warp/backfill.json`（daemon 写，`config.py` 读）。

## 4. 空闲设备线（本机实测 2026-11-04）

| 槽位 | profile.conf | 单元 | 可用 |
|---|---|---|---|
| line23–26、28、29 | 有 | `warp-wg-N.service` 存在、inactive | ✅ 6 条 |
| line27 | 有 | 指向 `/dev/null`（masked） | ❌ 永远起不来，已剔 |
| line30 | 有 | **单元不存在** | ❌ 起不来，靠 `_unit_ctl` 失败后换下一条 |

端口 = `wg.base_port(1110) + 序号 - 1`，即 1132–1138。

## 5. 失败与半途状态怎么收场

| 情形 | 处置 |
|---|---|
| 空闲线起不来 | 换下一条；全起不来 → 记 `_BACKFILL_TRY_TS`，10 分钟后重试 |
| 端口未就绪 | 仍然发布（prober 会继续探），只记日志 |
| 台账写入抛异常 | `finally` 仍记 `ban_backfill_tag`，避免下一轮再开一条；行丢了由对账重开 |
| 锁线有 tag、台账无行 | 对账：停残留单元、清字段，当轮重开 |
| 台账有行、没有锁线认领 | 对账：停单元、删行、重发配置 |
| daemon 重启 | `config.py` 读 `.warp/backfill.json`，成员仍在；daemon 内存重建后照样能撤 |
| 补位渠道自己也被封 | 不套娃，不开新线 |

## 6. 验证

```bash
cd /home/administrator/auto-swicth-ip
python3 -I test_channel_ban_hold.py       # 55 ok / all passed
python3 -m pytest test_channel_ban_hold.py -q   # 55 passed
```

11 条补位用例：

| 用例 | 覆盖 |
|---|---|
| `test_backfill_open_brings_up_replica_in_lowest_ban_colo` | 开补位 + 落最低 ban 率 colo + 切 selector |
| `test_backfill_skipped_when_unit_will_not_start` | 起不来换下一条 |
| `test_backfill_never_nests` | 不套娃 |
| `test_backfill_closed_at_8am_restores_original_arrangement` | 08:00 还原 |
| `test_backfill_open_runs_from_rehome_pass_when_held` | 由每分钟的 rehome 轮次驱动，不依赖探测线程 |
| `test_backfill_node_excluded_from_next_rehome_target` | 补位占的节点不再分给别人 |
| `test_backfill_target_ignores_rehome_switch` | 总闸关掉时补位仍开 |
| `test_backfill_tag_recorded_even_if_publish_raises` | 发布失败也记账 |
| `test_backfill_reconcile_removes_unowned_row` | 对账撤孤儿行 |
| `test_backfill_reconcile_reopens_when_row_lost` | 对账重开丢行的锁线 |
| `test_backfill_survives_restart_via_config` | 重启后 `config.py` 仍能装回成员 |

**线上状态**：daemon `auto-switch-ip.service` active，启动时刻晚于 `daemon.py` mtime；
`.warp/backfill.json` 不存在（无锁定期），`.warp/warp-outbound.json` 50 条。

**全量回归**：改动前后失败集合一致（`49 failed / 752 passed` vs 基线 `52 failed /
728 passed`）；`test_p2c_select_normal_picks_lowest_delay`、`test_daemon_sidecars.py`
三例、`tests/test_probe_cache.py`（collection）是既有 flaky/失败，与本次无关。

## 7. 审查发现（已处置 / 保留）

已修：

- 排除补位节点原来读台账行里不存在的键 → 改读锁线的 `ban_backfill_node`，台账行也带 `node`。
- 同一次 pass 里补位与重定位可能撞同一节点 → 共用 `used` 集合。
- `_atomic_write_json` 抛异常会让锁线「以为有补位」→ `try/except/finally` + 每分钟对账。

保留（已知、非缺陷）：

- 补位后没有调 `_after_warp_switch(interrupt=True)` 掐断在途请求 —— 换线语义，未按主人要求加。
- 「封禁率最低」是 **colo 级**统计（`ipv6_segments.colo_ban_rates`），不是逐渠道 —— 与主人原话「其 colo 的复制」一致。

## 8. 不要动

`channel_ban_reset_hour`（08:00 是上游真实重置时刻）与 `channel_ban_hold_enabled`
（渠道锁总闸）。改之前先确认 opencode 的配额重置时间变了 —— 见
[established-facts.md](established-facts.md) 第 1、2 条。
