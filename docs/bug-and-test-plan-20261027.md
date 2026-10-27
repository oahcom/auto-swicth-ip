# 503 与探测停摆：Bug 单 + 测试用例

> 日期：2026-10-27
> 前置：`docs/503-root-cause-20261027.md`（根因）、`docs/prober-gate-design-20261027.md`（闸门分析）
> 状态：待授权。本文所有代码为**待落盘**内容，尚未执行。
> **覆盖两个仓库**：
> - `~/dkk-projects/auto-switch-ip`（出口侧）→ B1–B7 + B12，测试落 `test_warp_prober_gates.py`
> - `~/.9router`（网关侧）→ B8–B11，测试落 `tests/unit/*.test.js`（vitest）

---


## 0. 现状校正（2026-10-27 12:10 UTC 实测，优先于下文一切描述）


`daemon.py` 在 **11:59 UTC** 被**另一个并行 session** 改动（工作区未提交），
本文件早前版本的 B1/B2/B4 描述已与代码不符。逐项校正：

```
$ git status --porcelain daemon.py          →  M daemon.py（未提交）
$ git diff --stat daemon.py                 →  +4 −5，两处
$ stat -c '%y' daemon.py                    →  2026-10-27 19:59:04 +0800 = 11:59 UTC
```

### 已经被改掉的两处

| 位置 | 改成了什么 | 后果 |
|---|---|---|
| `_warp_line_needs_probe`（daemon.py:2676-2678） | 三行时间门禁被**注释掉** | 停探的线仍然会被调度 |
| `_warp_line_note_ok`（daemon.py:2763） | `max(old, ts+120)` → `= 0` | **B4 已不存在** |

运行时复验（不是读代码推断）：

```
$ python3 -c "...next_probe_ts=now+13.3*3600; _warp_line_needs_probe('W2', now)"
  needs_probe = True | due - now = 0s
```

### 校正后的严重度

| ID | 原定 | **现定** | 依据 |
|---|---|---|---|
| **B1** | P0 停探即永久冻结 | **已降级**：门禁拆了，`_probe_one_line` 能跑了，`_warp_line_restart` 因此可达。只剩「unknown 不重启」这一子问题 | 上表 + 运行时复验 |
| **B2** | P0 停探 13.3h | **写侧仍在，读侧失效**：`next_probe_ts` 仍写 47880s（实测），但 `_warp_line_needs_probe` 不再读它 | 运行时复验见下 |
| **B3** | P0 6h 地板独立致停 | 同 B2 —— 地板写进去的是一个**没人读**的字段 | `probe_persistent_retry_sec` 仍 21600 |
| **B4** | P0 OK 无法恢复 | **已修复**（并行 session 改的） | `daemon.py:2763` → `st["next_probe_ts"] = 0` |
| **B5** | P1 TTL 6h | **仍存** | `_probe_ttl('quota') = 21600.0` 实测 |
| **B6** | P1 空等 20s | **仍存** | `switch_wait_sec = 20` 实测 |
| **B9** | P0 egress id 碰撞 | **仍存** | `~/.9router` 工作区源码无改动 |
| **B12** | （新）门禁拆除后无节奏 | **P0** | 实测 600s 内放行 600 次 |
| **B13** | （新）重启次数无上限 | **P0** | `restart_max_per_hour_per_line` 在 daemon.py 中 0 处引用 |

```
$ python3 -c "record_observation('W2','1.1.1.1','quota', ts=1000.0)"
  next_probe_ts - ts = 47880s (13.3h)      ← 写入侧照旧
  _probe_ttl(quota)   = 21600.0            ← B5 仍存
  switch_wait_sec     = 20                 ← B6 仍存
```

### 这个改动带出了新问题（必须记录）

门禁被注释掉后，`_warp_line_needs_probe` 只剩一个条件：「这条线是否 120s 内 OK」。
非 OK 的线**每一轮都返回 True**，只受 `probe_max_per_min=600` 与 `probe_concurrency=32` 约束。
也就是说：

1. **配额消耗变成开环**。`next_probe_ts` 不再限速，而 `_warp_model_usable` 的缓存只挡得住
   「已判脏且惩罚期未过」的地址（`daemon.py:4003` 的 `cached in PERSISTENT and _warp_ip_is_blocked`）。
   惩罚期已过但地址仍坏的情形会每轮重探。**修前最坏是停摆，修后最坏是烧配额 —— 两个极端都不对。**
2. **`next_probe_ts` 成了半死字段**：**15 处运行时写入**（2728/2742/2763/2780/2885/2892/
   2908/2923/2939/2961/2964/2967/2983/3025/3038）+ 1 处初始化（2530）仍在写，
   调度不再读它；但 **3 处读取仍在**：`_warp_line_wait_fresh`（:2835）、`_warp_line_restart`
   的 `prev_next`（:2958）、`_warp_prober_loop` 的 idle 唤醒（:3105）。语义漂移，后人会误读。
   **所以它不能删** —— 删了 `_warp_prober_loop` 就失去唯一的唤醒定时源。
3. **既有用例仍在锁这些写值**：`test_restart_same_ip_keeps_quota_park`（`test_warp_prober.py:132`）
   断言 `next_probe_ts == park`，它现在锁的是一个没人读的字段。

**改动未提交、无配套用例**。当前 `pytest test_warp_prober.py test_warp_refresh.py test_429_cooldown.py -q`
实测 **98 passed**，说明这个改动没有打破任何既有断言 —— 也就意味着**同样没有任何用例在保护它**。

> 本校正基于只读取证与运行时复验。我未参与该改动，也未修改任何代码。

---

## 第一部分：Bug 单

### 严重度与关系

| ID | 严重度 | 一句话 | 现状 |
|---|---|---|---|
| **B1** | ~~P0~~ → **P1** | 停探的线没有重启路径（门禁已拆，只剩 unknown 子问题） | 部分缓解 |
| **B2** | ~~P0~~ → **P1** | `next_probe_ts` 被惩罚期推到 13.3h（写侧仍在，读侧失效） | 写侧仍存 |
| **B3** | ~~P0~~ → **P2** | 6h 地板写进一个没人读的字段 | 写侧仍存 |
| **B4** | ~~P0~~ → **已修** | `_warp_line_note_ok` 的 `max` 已改为 `= 0` | 已修复 |
| **B5** | **P1** | 探测结论缓存 `quota` TTL 6h，构成第二道停摆 | 仍存 |
| **B6** | **P1** | 换线无候选时空等 20s（74 次 = 24.7 分钟） | 仍存 |
| **B7** | **P2** | `CLAUDE.md` 两处配置值与运行值不符 | 仍存 |
| **B8** | **P2** | 429 重试路径的失败结果丢失了归因上下文 | 仍存 |
| **B9** | **P0** | 三个活跃代理池共用同一 `proxyUrl`，egress id 碰撞 → 备用出口恒为空 | 仍存 |
| **B10** | 不改 | 传输层 `lastResetAt` 进程级全局 | 记录不改 |
| **B11** | 不改 | 非 strict 池静默降级直连 | 随 B9 消解 |
| **B12** | **P0（新）** | 门禁拆除后探测配额消耗开环，无固定节奏 | **新发现** |

两个 P0，不是 B9 一个：
- **B9** —— 两个仓库里唯一**源码一行没动**的原始 P0。
- **B12** —— 拆 B1/B2 的连带产物。**拆了结论驱动的闸，就必须补一个与结论无关的节奏**，
  否则从「停摆 13.3h」直接翻到「每轮重探、配额开环」，两个极端都不可用。

---

### B1 [~~P0~~ → P1] 被停探的线没有任何重启路径（**已被并行 session 部分拆掉**）

> **状态（12:10 UTC 实测）**：原描述的「`next_probe_ts` 一旦在未来，`_probe_one_line`
> 根本不会被调用」**已不成立** —— `_warp_line_needs_probe`（`daemon.py:2676-2678`）的
> 三行时间门禁已被注释掉。运行时复验：停探 13.3h 的线 `needs_probe = True`。
> 下面保留原始取证（它是改动前的现场事实），但**只剩「unknown 不重启」这一子问题**。

**现象（改动前）**

`prober_enabled=true`（当前运行值）时，旧守护线程全部不启动：

```python
# daemon.py:6722-6731
prober_enabled = bool(_warp().get("prober_enabled", True))
if prober_enabled:
    _start_warp_prober()
else:
    if _line_worker_enabled():
        _start_line_workers()
    if not _line_worker_owns_scheduling():
        _start_dirty_line_janitor()        # ← prober 模式下永不执行
```

全仓 `_warp_line_restart` 只有 **2 个调用点**，且都在 `_probe_one_line` 内部：

```
daemon.py:3033    _warp_line_restart(member, "出口处于惩罚期")
daemon.py:3060    _warp_line_restart(member, f"模型层 {verdict}")
```

而 `_probe_one_line` 的唯一调度入口是 `_warp_prober_loop`，它用 `_warp_line_needs_probe` 过滤。**`next_probe_ts` 一旦在未来，`_probe_one_line` 根本不会被调用 → `_warp_line_restart` 永远不会被触发 → 这条线永久冻结在旧 IP 上。**

实测印证：20 条线全部冻结 56.6 分钟，`NRestarts=0`（20/20）。

**为什么当时这是最严重的一条**

B2 把线停到次日 08:00，B1 让它真的等到次日 08:00。如果没有 B1，停探只是"暂时不探"，重启路径会把它拉回来。

**现在还剩什么**

门禁拆掉后重启路径已可达，但 `_probe_one_line` 碰到 `warp_egress_ip()` 返回 None 时
仍只记一条 `unknown` 就 `return`（`daemon.py:3020-3022`）：

```python
ip = warp_egress_ip(member["port"])
if not ip:
    _warp_line_record_observation(tag, None, "unknown", time.time(), started_at)
    return                            # ← 不回重启
```

若隧道进程半死（端口开着、上游已死），这条线会永远在「unknown → 5s 后重探 → 还是 unknown」
里循环，**无人重启它换会话**。这是 B1 唯一还成立的部分。

**验收标准（已按现状重写）**

1. ~~存在一条不依赖 `_probe_one_line` 的复活路径~~ —— **已不需要**，门禁已拆。
2. ~~仿真：20 条线全部 `verdict=quota` 且 `next_probe_ts` 在 13h 后，60 秒内至少一次调用~~
   —— **已天然成立**，实测 `needs_probe = True`。
3. **【仅剩】** 连续多次 `unknown` 必须能触发重启，不得无限重探。

**关键约束：不得推翻 `test_probe_unknown_ip_does_not_restart`**

`test_warp_prober.py:81-92` 明确锁死了「单次 unknown 不重启」：

```python
def test_probe_unknown_ip_does_not_restart(monkeypatch):
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: None)
    daemon._probe_one_line(m)
    assert st["last_verdict"] == "unknown"
    assert st["next_probe_ts"] > time.time()
    assert restarted == []          # ← 锁死「unknown 不重启」
```

**它是对的**：`unknown` 来自 `warp_egress_ip` 返回 None，可能是「端口不通」（该重启，
也可能是「探测瞬时超时」（不该重启）。代码选了保守策略，5s 后重探。

所以 B1 的修复不能直接让它变红，必须**区分两种 unknown**：

```python
ip = warp_egress_ip(member["port"])
if not ip:
    _warp_line_record_observation(tag, None, "unknown", time.time(), started_at)
    st = _warp_line_state(tag)
    streak = int(st.get("unknown_streak") or 0) + 1
    _warp_line_update(tag, unknown_streak=streak)
    # 连续多次拿不到 IP：端口不通 → 重启；仅次数够 → 也重启（防端口半死）
    if streak >= 3 and (not _port_open(member["port"]) or streak >= 10):
        _warp_line_restart(member, f"连续 {streak} 次无法取得出口 IP")
    return
```

**为什么 `streak >= 10` 这个或条件不可省**：`_port_open` 在 usque 进程半死时
可能仍返回 True（进程在监听，但上游已死）。只看端口会永远凑不满条件，线仍然冻结。

对应新用例：

```python
def test_unknown_streak_triggers_restart(monkeypatch):
    """连续多次拿不到 IP 必须重启，不能无限重探。"""
    m = _member()
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip", lambda port: None)
    monkeypatch.setattr(daemon, "_port_open", lambda port: True)   # 端口看似正常
    restarted = []
    monkeypatch.setattr(daemon, "_warp_line_restart",
                        lambda mm, r: restarted.append(mm["tag"]) or True)

    for _ in range(10):
        daemon._probe_one_line(m)

    assert restarted == ["W2"], \
        "连续 10 次拿不到 IP 仍未重启 —— 端口半死时这条线永远冻结"
```

**回归保护**：`test_warp_prober.py:81-92` 的 `test_probe_unknown_ip_does_not_restart`
在 B1 修复后必须**仍绿**（单次 unknown 不重启）。不要重复写这条，只在新执行单里确认它。

**备注（与原方案的差异）**

我原方案里写过"删掉 `_warp_ip_is_blocked` 在探测入口的判脏跳过"，**这条撤回**。见第四部分"被推翻的方案"。

---

### B2 [~~P0~~ → P1] `next_probe_ts` 被 IP 惩罚期推到 6h~13.3h（**写侧仍在，读侧已失效**）

> **状态（12:10 UTC 实测）**：`next_probe_ts` 仍被写成 13.3h 后（实测 47880s），
> 但 `_warp_line_needs_probe` 已不读它 → **停探后果已消失**。这条现在只是
> 「写一个没人读的字段」，并且该字段仍被 `_warp_line_wait_fresh`（`:2835`）与
> `_warp_prober_loop`（`:3105`）用于计算等待超时 —— 语义漂移，见 §0 与 B12。

**代码**

```python
# daemon.py:2729-2742
else:
    if verdict == "unknown":
        wait = _line_unknown_retry()                       # 5s
    elif verdict in ("quota", "flagged", "blocked"):
        wait = max(_line_persistent_retry(),               # 21600 = 6h   ← B3
                   _line_block_remaining(st.get("ip"), ts))  # ban_until - now  ← B2
    elif verdict == "blind":
        wait = _line_transient_retry(1)
    else:
        wait = _line_transient_retry(st["fail_streak"])
    st["next_probe_ts"] = ts + wait
```

**实测**

`.warp_ip_used.json['used']`：491 条记录，`blocked=True` 421 条，其中 417 条带 `ban_until`，**417/417 全部落在 08:00**。

```
ban_until 跨度  86405.9s = 24.002h   ← 固定的对齐时刻
ts        跨度 104991.3s = 29.164h   ← 实际观测窗口
```

若 `ban_until` 是逐条相对时长，两者跨度应相近。差 5.16h，说明它是**绝对对齐时刻**（每日 08:00 配额重置）。

`.warp_line_state.json`：20 条线，18 条 `next_probe_ts = 2026-10-28 08:00`。

**根因定级**

`_line_block_remaining` 回答的是"**这个地址**什么时候解禁"，它被误用来回答"**这条线**什么时候该再看一眼"。两者不是同一个问题：

- 地址解禁 → 影响**选线**（还能不能把这个 IP 作为出口）
- 探测节奏 → 影响**观测**（还要不要看）

**验收标准**

```python
assert next_probe_ts - ts <= 60        # 无论 verdict 与 ban_until
```

---

### B3 [~~P0~~ → P2] 6h 地板 `probe_persistent_retry_sec` 单独即可致停（**同样只写进没人读的字段**）

> **状态（12:10 UTC 实测）**：`probe_persistent_retry_sec` 运行值仍为 `21600`，
> `_line_persistent_retry()` 仍返回它。但它的产物 `next_probe_ts` 已不被调度读取，
> 所以「独立致停 6 小时」的后果**当前不成立**。改不改只影响 B2/B12 的收尾方式。

**代码**

```python
# daemon.py:2640-2642
def _line_persistent_retry() -> float:
    return float(_warp().get("probe_persistent_retry_sec",
                             _warp().get("probe_persistent_ttl_sec", 21600)))
```

运行值 `probe_persistent_retry_sec = 21600`（6.0 小时）。

`wait = max(地板, block_left)` 是**两个数取大**，所以即使 `ban_until` 已经过期（`block_left <= 0`），verdict=quota 仍然停探 6 小时。

**独立影响实测**（quota/flagged/blocked 共 19 条）：

```
停探 ≤ 6h :  2 条   ← 这 2 条只受地板影响，ban_until 已过期
停探 > 6h : 17 条   ← ban_until 额外推出去的部分
```

**只修 B2 不修 B3，停摆上限只能从 13.3h 降到 6h。**

**验收标准**

`probe_persistent_retry_sec` 不再出现在探测间隔的计算路径上（移出或降到 ≤60s）。

---

### B4 [~~P0~~ → **已修复**] `_warp_line_note_ok` 用 `max` 使 OK 无法恢复探测

> **状态（12:10 UTC 实测）**：已被**并行 session** 改成 `st["next_probe_ts"] = 0`
> （`daemon.py:2763`，工作区未提交）。下面记录原始缺陷与取证，供追溯用。
> **注意**：`= 0` 比本文件原先的建议（`= ts + _line_fresh_sec()`）更激进 ——
> 它把「下次可探时刻」直接归零，配合已拆的时间门禁等于「立刻可探」。
> 这在 B12 的固定节奏补上之前，会让生产 OK 流量也立刻触发一轮重探。

**原始代码（改动前）**

```python
# daemon.py:2763-2764（_warp_line_note_ok，生产成功流量调用）
st["next_probe_ts"] = max(float(st.get("next_probe_ts") or 0.0),
                          ts + _line_fresh_sec())
```

`max` 保留两者较大值。若 `next_probe_ts` 已被 quota 推到 08:00，此后即使拿到 OK，`max(08:00, now+120)` 仍然是 08:00。

**实测反例**

```
WARP-CF:  last_verdict = OK,  last_probe_ts = 11:58:35
          next_probe_ts = 2026-10-28 08:00:00        ← OK 了仍在停探
```

**这解释了为什么"探测成功也无法自救"** —— 新的好证据被旧坏证据覆盖。

**验收标准**

```python
# OK 之后 120s 内必须复探
assert next_probe_ts - ts <= 120
```

**当前实际状态**：已由 `= 0` 满足（且远超要求）。原本对应的用例
`test_note_ok_resets_next_probe_ts_even_if_parked` **不需要再写** —— 它现在会
天然通过，写成新用例只是复述现状。真正缺的用例在 B12。

---

### B12 [P0 新] 门禁拆除后探测配额消耗开环，无固定节奏

**这是拆 B1/B2 的连带产物，不是独立缺陷。**

**机制**

```python
# daemon.py:2672-2686（改动后）
def _warp_line_needs_probe(tag: str, now: float) -> tuple[bool, float]:
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        # next_ts = float(st.get("next_probe_ts") or 0.0)     ← 已注释
        # if now < next_ts:
        #     return False, next_ts                           ← 已注释
        ip = st.get("ip")
        if not ip:
            return True, now                                  # 无 IP → 每轮都探
        last_ok = float(st.get("last_ok_ts") or 0.0)
        if (st.get("last_ok_ip") == ip
                and now - last_ok <= _line_fresh_sec()):
            return False, last_ok + _line_fresh_sec()         # 只有「120s 内 OK」才不探
        return True, now                                      # ← 其余全部每轮都探
```

非 OK 的线**每一轮循环**都返回 `True`。唯一的节流只剩两处资源闸：

```
probe_max_per_min    = 600    ← 全局速率（实际不生效：20 线 × 32 并发远达不到）
probe_concurrency    = 32     ← 每轮并发
```

而模型层探测每次**烧掉该出口一格 opencode 免费配额**。`_warp_model_usable` 的缓存
只挡得住一种情形：

```python
# daemon.py:4003
if cached in _WARP_PROBE_PERSISTENT and _warp_ip_is_blocked(ip):
```

即「结论是持久档 **且** 该地址仍在惩罚期」。**惩罚期已过但地址仍坏**时（B7 的
`rate429_guard_sec=60` 让这种情况很常见：429 的默认窗口只有 60s），缓存不生效，
于是每 5~30s 重探一次同一地址。

**后果两个方向都对不上**

| | 修前 | 修后（当前工作区） |
|---|---|---|
| 最坏情形 | 停摆 13.3 小时，零观测 | 每轮重探，配额开环消耗 |
| 可预测性 | 不可预测的停摆 | 不可预测的消耗（同上，只是换了个坏方向） |

用户的要求是「探测逻辑不应该被任何东西限制」——**但「不被结论限制」不等于「没有节奏」**。
`prober-gate-design-20261027.md` §4 已经写明：真正该保留的是**统一的、与结论无关的固定节奏**
（如每线每 60s 最多一次），而不是「结论是 quota 就停 6 小时」。当前改动把两者一起删了。

**验收标准**

三条用例（完整代码见第二部分「B12」小节，此处不重复）：
`test_probe_cadence_caps_per_line_rate`（600s 内放行 ≤10 次）、
`test_cadence_is_verdict_independent`（五种结论的首探时刻完全相同）、
`test_cadence_survives_zero_next_probe_ts`（`next_probe_ts=0` 时节奏仍生效）。

判据是**闭环**的：每次放行后回写 `last_probe_ts`。不回写的话，正确实现也会数出 600 次
—— 这条我第一版写错过，见下。

**修法提示**：节奏必须**只由 `last_probe_ts` 推出**，与 `verdict`、`ban_until`、
`next_probe_ts` 都无关：

```python
# 读取侧：下次可探时刻 = 上次探测 + 节奏，与结论无关
due = float(st.get("last_probe_ts") or 0.0) + 60.0
if now < due:
    return False, due
return True, due
```

### ⚠ 一个写出来会失效的写法（我第一版就是这么写的）

```python
# ❌ 错误：min() 会在 next_probe_ts 为 0 时坍缩
next_ts = float(st.get("next_probe_ts") or 0.0)
if now < min(next_ts, st.get("last_probe_ts", 0.0) + 60.0):
    return False, next_ts
```

`_warp_line_note_ok`（`daemon.py:2763`）与 `_warp_line_invalidate`（`:2780`）都会把
`next_probe_ts` 写成 0，`min(0, last+60) = 0`，`now < 0` 恒假 → **完全放行**。
实测（带 `last_probe_ts` 回写，600 秒窗口）：

| `next_probe_ts` | 原式放行 | 正确式放行 |
|---|---|---|
| `0`（`note_ok` 之后） | **600** | 9 |
| `last + 5`（unknown 重探） | **595** | 9 |
| `last + 47880`（quota） | 9 | 9 |

原式只在第三种情形下碰巧正确，而**前两种正是最常见的状态** —— `note_ok` 在生产
每次成功请求后都会把 `next_probe_ts` 置 0。此外它 `return False, next_ts` 返回的是
未钳制的 `next_ts`，`_warp_prober_loop` 的 idle 唤醒（`:3101-3105`）会据此睡到 13.3h 后。

**`next_probe_ts` 不能删**：它仍被 `_warp_line_wait_fresh`（`:2835`）与
`_warp_prober_loop`（`:3105`）读取。保留写入，只是不让它参与「探不探」的判定。

---

### B13 [P0 新] 每线每小时重启上限从未实现，实测已达 76 次/小时

**这是查 B12 修法时撞出来的：补 60s 节奏会让「blocked 即重启」以固定频率触发，
于是回头找那个本该兜住它的上限 —— 它不存在。**

**文档说它有，代码里没有**

```bash
$ grep -rn 'restart_max_per_hour' --include='*.py' .
（无输出）

$ grep -n 'restart_max_per_hour_per_line' CLAUDE.md config.py
CLAUDE.md:664: warp.restart_max_per_hour_per_line: 20   # 每线每小时重启上限
```

`CLAUDE.md:664` 是这个词在整个仓库里的**唯一**出现处，且写在「配置（config.py）」
一节的代码块里 —— 读者会以为它像上下十几个键一样真的可从 `CFG["warp"]` 取到。
`config.py:442-445` 的相邻键是：

```python
"restart_cooldown_sec": 5,
"restart_backoff_sec": [2, 5, 10, 20, 60],
"restart_backoff_max_sec": 120,
"restart_concurrency": 4,          # 全局并发信号量，不是速率
```

`restart_max_per_hour_per_line` 不在其中。`config.CFG["warp"]` 运行时实测：

```python
$ python3 -c "import config; print([k for k in config.CFG['warp'] if 'restart' in k])"
['restart_cooldown_sec', 'restart_backoff_sec', 'restart_backoff_max_sec', 'restart_concurrency']
```

**唯一在跑的三道闸都不管速率**

| 机制 | 位置 | 实际约束什么 | 为什么不构成上限 |
|---|---|---|---|
| `next_restart_ts` | `daemon.py:2873` 读 / `:2952` 写 | 距上次重启 ≥ `restart_cooldown_sec`(5s) | 重启**成功**时被写成 `0.0` —— 成功越快，下一次越快，等于零冷却 |
| `restart_backoff_sec` | `:2937` / `:2981` | 重启**失败**后的退避 | 只在失败分支写；成功分支走的是 `:2952` 的 `= 0.0` |
| `restart_concurrency` | `:3119` → `_RESTART_SEM` | 全局同时在重启几条 | 信号量，与时间无关；且等待超时 5s（`:2920`），失败即放掉 |
| `restart_count` | `:2950` 写 / `:2532` 初始化 | **没有任何读取点** | 只增不减、无人读，纯计数器 |

`restart_count` 是最刺眼的一处：它每重启一次就 +1，写进 per-line 状态表并落盘
（`_save_warp_line_state()`），却**从头到尾没有一处读它**。差一个 `if restart_count >= N` 就是完整实现。

**实测：单线 76 次/小时，是文档宣称值的 3.8 倍**

```bash
$ sudo journalctl -u auto-switch-ip --since '3 hours ago' --no-pager -o short-iso \
    | grep '↻ 重启' | ...
```

| tag | 重启次数 | 折算 /h | 文档宣称上限 20/h |
|---|---|---|---|
| WARP-CF-F | 127 | **76** | 超 3.8× |
| WARP-CF-11 | 112 | **67** | 超 3.4× |
| WARP-CF-16 | 112 | **67** | 超 3.4× |
| WARP-CF-H | 109 | **65** | 超 3.3× |
| WARP-CF-17 | 109 | **65** | 超 3.3× |
| WARP-CF-G | 107 | **64** | 超 3.2× |

窗口：`2026-10-27T19:59:31+08:00` → `2026-10-27T21:39:44+08:00`，跨度 **1.67h**，
样本 1836 条，全池合计 **1099 次/小时**。

注意 76 > 60 —— 说明当前连「每 60s 一次」的节奏都没有：真正的节流只剩
`restart_cooldown_sec=5`（重启成功后被清零）+ 一次探测往返的耗时。
理论天花板是 **720 次/小时/线**（5s 一次），实测 76 只是因为探测本身要几秒。

**为什么它是 P0 而不是 P1**

重启一条 WARP 隧道 = `systemctl restart usque-warp-*` + 换会话地址。代价有三层：

1. **在途连接被掐断**。这正是 `warp_line_busy`（`:2878`，判据见 CLAUDE.md「零连接直接判闲」）
   存在的理由 —— 但 `_probe_one_line` 的探测失败分支（`:3059`）调用 `_warp_line_restart`
   时，`busy` 判定用的窗口是 `_ERROR_PATH_BUSY_WINDOW_SEC`(2s)，很短。
2. **配额白烧**。每次重启拿到新地址，第一件事就是探它一格（`:3051`）。
   76 次/h × 20 条线 = 每小时 1500+ 格探测配额，只为确认「这条线现在还是坏的」。
3. **B12 修好后会更糟，不是更好**。当前无节奏时靠探测耗时间然撞出 ~76/h；
   一旦补上 60s 固定节奏，`blocked` 线每 60s 探一次、探完即重启 → **稳定 60 次/h/线**，
   比现在更规则、也更确定地打满。**所以 FR-13 与 FR-14 必须同批上线**，
   只上节奏 = 把随机风暴改成节拍器。

**修法**

在 per-line 状态里维护一个滑动窗口，重启前查：

```python
def _warp_line_restart_allowed(tag: str, now: float) -> bool:
    cap = int(_warp().get("restart_max_per_hour_per_line", 20))
    with _WARP_LINE_CV:
        st = _warp_line_state_locked(tag)
        hist = [t for t in (st.get("restart_hist") or []) if now - t < 3600]
        st["restart_hist"] = hist
        return len(hist) < cap
```

放行后 append `now`。**达上限只停重启，不停探测** —— 探测是观测，重启才是有副作用的决策
（`CLAUDE.md` 用户规则：「探测逻辑不应该被任何东西限制」）。

`config.py:445` 后补 `"restart_max_per_hour_per_line": 20,`，`CLAUDE.md:664` 才不再是谎话。

**验收标准**

三条用例（完整代码见第二部分「B13」小节）：
`test_restart_rate_is_capped`（模拟 1 小时，第 21 次必须被拒）、
`test_restart_cap_does_not_stop_probing`（达上限后 `_warp_line_needs_probe` 仍返回 True）、
`test_restart_cap_survives_state_reload`（`restart_hist` 必须随状态表落盘，
否则 daemon 重启一次上限就清零 —— `_load_warp_line_state:2614` 只恢复
`k in _LINE_DEFAULTS` 的键，新键不加进那张表就会被静默丢弃；实测窗口内
20:35:09 daemon 重启过一次）。

---

### B5 [P1] 探测结论缓存 `quota` TTL 6h，构成第二道停摆

**代码**

```python
# daemon.py:657-663
def _probe_ttl(verdict: str) -> float:
    w = _warp()
    if verdict in _WARP_PROBE_PERSISTENT:      # {"quota", "flagged"}
        return float(w.get("probe_persistent_ttl_sec", 21600))    # 6h
    if verdict in _WARP_PROBE_OK:              # {"OK"}
        return float(w.get("probe_ok_ttl_sec", 600))              # 10min
    return float(w.get("probe_transient_ttl_sec", 120))           # 2min
```

即使 B2/B3 全部修好，同一个 `(tag, ip)` 在 6h 内仍会被 `_probe_cache_get` 判为"TTL 内已探过"而跳过实测。

**注意：这一条的改动范围比前三条小**

缓存的用途是**避免重复烧配额**，价值真实存在（同一 `/128` 重探有 99.2% 仍 quota）。问题只在 TTL 长度，不在机制本身。**不建议删除缓存，只建议缩短 TTL。**

**与已有测试的关系**

`test_dirty_ip_is_never_probed` / `test_dirty_ip_shared_by_two_lines_is_not_probed` 锁定的是 `_warp_ip_is_blocked`（台账判脏），**不是** `_probe_ttl`。改 TTL 不会让它们变红。

**验收标准**

`_probe_ttl("quota") <= 600`。

---

### B6 [P1] 换线无候选时空等 20s

**代码**

```python
# daemon.py:4735-4742
if not cands:
    deadline = now + float(_warp().get("switch_wait_sec", 20))    # 20s
    log("  · 暂无 120s 内 OK 的备线，等待探测器写入")
    cands = _warp_line_wait_fresh(active["tag"] if active else None, deadline)
if not cands:
    log("  ⚠ 等待超时仍无新鲜备线，本次不切（不重启、不落池）")
    return False
```

被动等探测器写表。探测器停摆时必然等满 20s。

**实测**：74 次触发 × 20s = 24.7 分钟纯等待，落在 10s tick 的主循环上。

**验收标准**

1. 无候选时先主动催一次探测（把目标线 `next_probe_ts` 置 0 并 `notify_all`）。
2. `switch_wait_sec` ≤ 3。

---

### B7 [P2] `CLAUDE.md` 配置值与运行值不符

| 位置 | 文档写的 | 运行值 | 影响 |
|---|---|---|---|
| `CLAUDE.md:638` | `rate429_guard_sec: 86400`（24h） | `60` | 按文档排查会得出"429 关 24 小时"的错误结论，掩盖真凶 `ban_until` |
| `CLAUDE.md` 主动轮换段 | `proactive_mode: "requests"`（当前生效） | `fixed` | 文档说按 150 格配额轮换，实际按 60s 定时 |

**验收标准**

文档值与 `python3 -c "import config; print(config.CFG['warp'][...])"` 一致。

---

## 第一部分续：9router 侧（`~/.9router`）

> 四条里只有 **B9** 是当前直接生效的故障放大器。B8/B11 是设计缺陷但被 daemon 侧兜住，
> B10 是放大器但作用域有限。**优先级低于 B1-B4** —— 出口侧修好后 503 的主体（族 A 91.2%）
> 就会消失，但这四条决定了下次故障会不会再被放大。

### B9 [P0] 三个活跃代理池共用同一 `proxyUrl`，备用出口恒为空

**实测**

`proxyPools` 表当前 5 条，3 条 `isActive=1`：

```
id            isActive  testStatus  proxyUrl                    strictProxy
644a72f0…     1         active      http://127.0.0.1:7890       True    ← 被选中
351035a6…     1         error       http://127.0.0.1:7890       False
24de9bf6…     1         error       http://127.0.0.1:7890       False
c5a21b4e…     0         error       http://127.0.0.1:7890       False
1380ee97…     0         error       http://192.168.100.104:7890 False
```

`identifyFixedEgress`（`egress.js:65-73`）只按 `proxyUrl` 算身份：

```js
const { url: proxyUrl } = resolveEffectiveProxyUrl(targetUrl, proxyOptions);
if (proxyUrl) {
  return { id: hashEgress("proxy", proxyUrl), kind: "proxy", ... };
}
```

实算三个活跃池的 id，**完全相同**：

```
proxy:f1410cd719dcee161d171a3729c50bff   644a72f0 (picked, active)
proxy:f1410cd719dcee161d171a3729c50bff   351035a6 (active, error)
proxy:f1410cd719dcee161d171a3729c50bff   24de9bf6 (active, error)
proxy:b6b9ecfc108b24603dfd1138e4dd8450   1380ee97 (inactive, 不同 url)
```

`auth.js:63-76` 构造候选时先按池 id 排除当前池：

```js
const connectionProxyAlternates = isOpencode
  ? allPools.filter(p => p.id !== resolvedProxy.proxyPoolId)   // ← 按池 id 过滤，留下另两个
      .map(proxyConfigFromPool) ...
```

于是两个候选进入列表。但 `selectAlternateProxy`（`egress.js:301-306`）用 **egress id** 排除：

```js
const egress = identifyFixedEgress(targetUrl, merged);
if (!egress || excludeIds.has(egress.id)) continue;    // ← id 与当前池碰撞 → 全被跳过
```

**两处用的键不是同一个东西**：`auth.js` 按 `proxyPoolId` 去重，`egress.js` 按 `hash(proxyUrl)` 去重。
碰撞后候选集为空，`selectAlternateProxy` 返回 `{alternate: null}`。

**为什么之前"死池 7000 总被选中"**

同一个缺陷的另一面。`7000` 的 url 与其他池不同 → egress id 唯一 → 是唯一幸存候选。
删掉 7000 后变成"永远没有候选"。**两种状态都不正确。**

**关键更正（相对 `503-root-cause-20261027.md` §4.2）**

早前把「三个池全被排除」的原因写成「`markEgressCooling` 冷却把它们排掉了」，**错**。
`egress.js:228-238` 对 proxy 类直接返回 null：

```js
if (egress.kind === "proxy") return null;
```

proxy 出口**从不进冷却表**。排除的唯一原因是 id 碰撞。这个更正也解释了
`tests/unit/opencode-egress.test.js:113-116` 为什么断言 `getEgressCooldown(...)` 恒为 null。

**验收标准**

```js
// 三个共用同一 url 的活跃池，必须产生 3 个不同的 egress id
const ids = pools.map(p => identifyFixedEgress(TARGET, p).id);
assert(new Set(ids).size === 3);
```

`alternates` 非空时，`selectAlternateProxy` 必须返回 `alternate !== null`。

### ⚠ 改法必须连带解决「换线换到同一物理出口」

这是审查发现的**新问题**，不解决则 B9 修了等于白修。

先看一个关键事实（与 `503-root-cause-20261027.md` §4.2 的说法不同）：

```js
// egress.js:238
if (egress.kind === "proxy") return null;   // ← proxy 从不进冷却表
```

所以改 id **不会「破坏」冷却一致性** —— proxy 本来就没冷却。
但这带来另一个后果：

```
修前：三池同一 id → 候选恒空 → 不换线（当前故障）
修后：三池不同 id → 能换线，但三个池的 proxyUrl 都是 127.0.0.1:7890
     → 换到池 B 还是同一个 sing-box 出口
     → 立即再 429 → 切池 C → 仍是同一个出口
     → 三个池轮一遍，出口 IP 完全没变
```

`markEgressCooling` 又对 proxy 全部 no-op，连「这个出口刚 429 过」都不记。

**三个候选方案**（需用户定）：

| 方案 | 做法 | 代价 |
|---|---|---|
| **A（推荐）** | `selectAlternateProxy` 去重按 `proxyUrl` 而非 `egress.id` | 最小；语义上「不同池同 url」仍算同一出口，与物理事实一致 |
| B | `identifyFixedEgress` 的 id 纳入 `proxyPoolId`（`hashEgress("proxy", poolId \|\| url)`） | 会改变所有既有 egress 缓存键；且仍需防同 url 互选 |
| C | 不做 B9，让用户改配置（不重复配同 url 的池） | 用户已表态「配多少个池是用户的态度」 |

**A 的 WHY 更硬**：它把「候选去重」和「出口冷却」两件事分开了 ——
去重看物理出口（proxyUrl），冷却（若有）看逻辑池。不需要碰
`markEgressCooling` 里那段有充分 WHY 的注释（`:231-237`）。

对应的新用例：

```js
it("same_physical_egress_is_not_an_alternate", () => {
  // 三个池同 url：id 不同（修 B9 后），但物理上是同一个出口
  const a = pool("a", "http://127.0.0.1:7890", { url: "http://127.0.0.1:7890" });
  const b = pool("b", "http://127.0.0.1:7890", { url: "http://127.0.0.1:7890" });
  const proxyOptions = { ...a, connectionProxyAlternates: [b] };

  const selection = selectAlternateProxy(
    proxyOptions, TARGET, new Set([identifyFixedEgress(TARGET, a).id])
  );

  expect(selection.alternate).toBeNull();
  // 同一 proxyUrl 的池换过去等于没换 —— 出口 IP 不会变，只白耗一次请求
});
```

**这条与 `same_url_different_pools_must_not_collide` 不矛盾**：
前者要求「id 能区分」（用于标识/记账），后者要求「同 url 不互为备选」（用于选线）。
两个断言同时成立才是对的。

---

### B8 [P1] 429 重试路径丢失「这是一次 429 重试」的上下文

**代码**（`open-sse/executors/opencode.js:665-676`）

```js
const retryResult = await super.execute({ ...args, credentials: retryCredentials,
                                          proxyOptions: retryProxyOptions });

if (retryResult?.response?.status === 429) {
  const retryText = await readResponseText(retryResult.response);
  markEgressCooling(retryEgress, parseRetryAfterMs(retryResult.response.headers, retryText));
}
return retryResult;      // ← 直接返回，外层无法知道「原始是 429 + 重试也失败」
```

**实际影响比根因报告里写的窄**

`super.execute` 确实不知道外层刚拿到 429，但：

1. 重试结果若**仍是 429**，返回的就是真 429，combo 正常识别 → `checkFallbackError(429)` 走限流退避。
2. 重试结果若被 transport 层合成为 503（`fetchUpstream` 拿不到响应 → `UpstreamTransportError`），
   daemon 侧**已经**把它和真出口故障区分开了：`_classify_transport_error`（`daemon.py:1108-1115`）
   单独计数、不记惩罚台账，并触发 `_verify_transport_failure`（`daemon.py:5190-5210`）
   探出口 → 不通则换线、通则清僵死连接。

所以缺失的是**归因信息**，不是分类能力：外层看到 503 时无法回答
「这是首次尝试就 503，还是 429 之后重试才 503」。后者说明换出口有效但换到的仍是脏出口，
该优先换线而不是等 sidecar 冷却。

**验收标准**

返回结果带一个可读标记（如 `retryResult.egressRetried = true` 或
响应的 `x-egress-retried` 头），daemon 侧日志能区分两类 503。

**建议先验证再改**：改 `super.execute` 的返回契约属高风险，需先确认 `egressRetried`
不会破坏现有 `result.response` 消费方（combo / chatCore / 流式路径都在读它）。

---

### B10 [P1] 传输层 `lastResetAt` 是进程级全局，一条出口超时锁住全链 30s

**代码**（`open-sse/utils/opencodeTransport.js:18` + `:236-242` + `:260-266`）

```js
let lastResetAt = 0;            // ← 模块级单变量，不分出口

if (attempt === 0 && Date.now() - lastResetAt >= 30000) {
  lastResetAt = Date.now();
  retire();  continue;          // 允许重启 sidecar
}
console.warn("... reset throttled, fail fast");
return null;                    // ← 冷却期内直接失败
```

**它是全局的，但 sidecar 也是全局的**

`let active = null`（`:14`）—— 整个进程只有**一个** sidecar，所有出口（所有 WARP 线）
共用它。所以「按出口分键」这个改法**不成立**：不是多个 sidecar 各自冷却，是同一份
socket 池必须整体重建。

代码注释说明了保留 30s 的理由（`:232-234`）：不节流会让每条撞死出口的请求都 retire 一次 →
spawn 风暴。实测比例 1:6.3（sidecar 启动 255 次 vs timeout 1609 次），节流确实在起作用。

**真正的缺陷在别处**：冷却期内 `return null` 让上层的 `fetchUpstream` 合成 503，
而这个 503 会被计入「出口故障」的证据流。**实际上出口可能完全正常，只是 sidecar 在冷却。**

**已被兜住**：daemon 的 `_verify_transport_failure`（`daemon.py:5190`）先探出口 ——
探通就不换线、改清僵死连接。所以不会误换线，但会多一轮探测。

**验收标准**

冷却期内的 503 必须带可区分标记（现有 `UpstreamTransportError` 文案已够），
且 daemon 日志中「因 sidecar 冷却产生的 503」与「出口真故障的 503」计数可分离。

**改法**（比「按出口分键」更小）：`return null` 前不要走合成 503 路径，改为返回一个
带 `retry-after: ceil((30000 - (now - lastResetAt))/1000)` 的 429，让上层按限流退避
而非按出口故障处理。**但这会改变 503 的语义，需先确认 combo 的 `checkFallbackError`
对 429 的分支是否会导致跨模型切换** —— 那是更大的行为变更，本轮不建议动。

**结论：B10 记录，不建议改。** 现状是「放大器 + 已有正确兜底」，改动收益低于风险。

---

### B11 [P2] 非 strict 池静默降级直连，产生假 429 污染分类

**代码**（`open-sse/utils/proxyFetch.js:441-449` + `:359-379`）

```js
} catch (proxyError) {
  if (proxyOptions?.strictProxy === true) { throw ...; }
  console.warn("[ProxyFetch] Proxy failed, falling back to direct: ...");
  return fetchWithTlsFallback(url, options, null);    // ← 不带代理
}
```

```js
// replayFreeTierRejectionDirect: 403 + FreeTierError → 直接重放（不带代理）
if (!text.includes(FREE_TIER_ERROR_MARKER)) return bufferedResponse(text, proxied);
return await fetchWithTlsFallback(url, options, null);
```

**代价**：直连走 Node/undici 的 TLS 指纹，opencode 按指纹识别客户端 → 返回
**假的 `FreeUsageLimitError` 429**。这个 429 会被 `_classify_egress_error` 判成
`rate429` → 标脏当前 IP → 触发换线。**但当前 IP 是干净的，是降级造成的。**

**作用域已被 strict 限制住**

当前被选中的池 `644a72f0` 是 `strictProxy=True` → 走 `throw` 分支，不降级。
降级的另两个池（`351035a6` / `24de9bf6`）**正是因为 B9 才成为候选**。
**修好 B9 后，B11 的攻击面自然消失**（候选池若都 strict，就没有降级路径）。

**验收标准**

日志中 `falling back to direct` 与随后的 `rate429` 必须能关联到同一次请求；
若同一次请求既降级又 429，该 429 **不得**用于标脏出口 IP。

**改法**（不推荐现在动）：`proxyAwareFetch` 降级时打标记，`_classify_egress_error`
看到标记就不判 `rate429`。**跨仓库改判据，风险高于收益，且 B9 修好后自然消解。**

---

## 第二部分：测试用例

> 落盘位置：`test_warp_prober_gates.py`
> 风格对齐 `test_warp_prober.py`（monkeypatch + daemon 模块级状态隔离）
> **新用例当前应全部为红**（TDD：先红后绿）

```python
"""探测停摆闸门的回归用例（2026-10-27）。

背景：`next_probe_ts` 被 IP 惩罚期推到次日 08:00，20 条线全部冻结
56.6 分钟，74 次换线全废。见 docs/bug-and-test-plan-20261027.md。

命名约定：`<场景>_<期望行为>`，断言信息用中文说明"为什么这是错的"。
"""

import json
import os
import time

import pytest

import daemon


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每条用例独立的状态表 + 落盘隔离，避免跨用例污染。"""
    monkeypatch.setattr(daemon, "_WARP_LINE_STATE", {})
    monkeypatch.setattr(daemon, "_WARP_PROBING", set())
    monkeypatch.setattr(daemon, "_WARP_REFRESHING", set())
    monkeypatch.setattr(daemon, "_PROBE_LAST_VERDICT", {})
    monkeypatch.setattr(daemon, "_PROBE_BLOCKED_AT", {})
    monkeypatch.setattr(daemon, "_WARP_IP_USED", {})
    monkeypatch.setattr(daemon, "_save_warp_line_state", lambda *a, **k: None)
    monkeypatch.setenv("WARP_LINE_STATE_FILE_PATH", str(tmp_path / "line_state.json"))


def _member(tag="W2", port=10002):
    return {"tag": tag, "port": port, "service": f"usque-warp-{tag.lower()}"}


# ─────────────────────────────────────────────────────────────
# B2 / B3 — 探测间隔不得被惩罚期延长
# ─────────────────────────────────────────────────────────────

def test_quota_verdict_does_not_extend_probe_beyond_cadence(monkeypatch):
    """quota 结论不得让 next_probe_ts 超过探测节奏上限。

    回归：daemon.py:2736 的 wait = max(6h, ban_until-now) 把停探推到
    次日 08:00（实测 18/20 条线）。
    """
    now = 1000.0
    # 该 IP 的 ban_until 在 13.3 小时后 —— 惩罚期不该污染观测排程
    monkeypatch.setattr(daemon, "_line_block_remaining",
                        lambda ip, ts: 13.3 * 3600)
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: False)
    daemon._warp_line_record_observation("W2", "1.1.1.1", "quota", ts=now)

    st = daemon._warp_line_state("W2")
    gap = st["next_probe_ts"] - now
    assert gap <= 60, (
        f"quota 结论让探测停摆 {gap/3600:.1f} 小时；"
        "地址被禁 ≠ 线不必再看，惩罚期只该影响选线"
    )


def test_blocked_verdict_does_not_extend_probe_beyond_cadence(monkeypatch):
    """blocked 结论同样不得延长停探。"""
    now = 1000.0
    monkeypatch.setattr(daemon, "_line_block_remaining",
                        lambda ip, ts: 16 * 3600)
    daemon._warp_line_record_observation("W2", "1.1.1.1", "blocked", ts=now)

    gap = daemon._warp_line_state("W2")["next_probe_ts"] - now
    assert gap <= 60, f"blocked 让探测停摆 {gap/3600:.1f} 小时"


def test_persistent_retry_floor_does_not_apply_to_probe_cadence(monkeypatch):
    """6h 地板 probe_persistent_retry_sec 不得进入探测间隔。

    回归 B3：ban_until 已过期（block_left<=0）时它单独仍停探 6 小时，
    实测 2/19 条线是这种情况。
    """
    now = 1000.0
    # ban_until 已过期
    monkeypatch.setattr(daemon, "_line_block_remaining", lambda ip, ts: 0.0)
    monkeypatch.setitem(daemon.CFG["warp"], "probe_persistent_retry_sec", 21600)
    daemon._warp_line_record_observation("W2", "1.1.1.1", "quota", ts=now)

    gap = daemon._warp_line_state("W2")["next_probe_ts"] - now
    assert gap <= 60, (
        f"惩罚期已过仍停探 {gap/3600:.1f} 小时 —— "
        "6h 地板是独立于 ban_until 的第二道停摆"
    )


def test_all_lines_quota_still_probed_within_cadence():
    """全部线都被判 quota 时，每条仍须在节奏内复探。

    用户指令：「即使没有任何东西可以探测，即使探测的结果全都是不好的」。
    """
    now = 1000.0
    tags = [f"W{i}" for i in range(20)]
    for t in tags:
        daemon._warp_line_record_observation(t, f"10.0.0.{t[-1]}", "quota", ts=now)

    for t in tags:
        gap = daemon._warp_line_state(t)["next_probe_ts"] - now
        assert gap <= 60, f"{t} 全部结论不好也必须继续探测，实际停 {gap/3600:.1f}h"


def test_failure_backoff_is_capped_at_60s():
    """失败退避只防风暴，上限硬钳 60s。"""
    for streak in (1, 2, 3, 5, 10, 100):
        cadence = daemon._line_probe_cadence(streak)
        assert cadence <= 60, f"fail_streak={streak} 退避 {cadence}s 超过 60s 上限"


# ─────────────────────────────────────────────────────────────
# B4 — OK 必须让探测恢复
# ─────────────────────────────────────────────────────────────

def test_note_ok_resets_next_probe_ts_even_if_parked():
    """生产成功流量（OK）必须把停探拉回，不得用 max 保留旧的惩罚期。

    回归 B4：实测 WARP-CF 在 11:58 已 OK，next_probe_ts 仍停在次日 08:00
    （daemon.py:2763 的 max(old, ts+120)）。
    """
    ts = 1000.0
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", last_verdict="quota",
            next_probe_ts=13.3 * 3600 + ts)      # 被 quota 停到很晚
    daemon._warp_line_note_ok("W2", "1.1.1.1", ts)

    st = daemon._warp_line_state("W2")
    gap = st["next_probe_ts"] - ts
    assert gap <= 120, (
        f"OK 之后仍停探 {gap/3600:.1f} 小时 —— "
        "新证据被旧证据压住，探测成功也无法自救"
    )


def test_ok_verdict_after_quota_restores_probe(monkeypatch):
    """先 quota 再 OK，最终必须在 120s 内复探。"""
    monkeypatch.setattr(daemon, "_line_block_remaining", lambda ip, ts: 13.3 * 3600)
    daemon._warp_line_record_observation("W2", "1.1.1.1", "quota", ts=1000.0)
    daemon._warp_line_record_observation("W2", "1.1.1.1", "OK", ts=2000.0)

    gap = daemon._warp_line_state("W2")["next_probe_ts"] - 2000.0
    assert gap <= 120, f"quota→OK 后仍停探 {gap/3600:.1f}h"


# ─────────────────────────────────────────────────────────────
# B5 — 结论缓存 TTL 不得长于探测节奏
# ─────────────────────────────────────────────────────────────

def test_quota_cache_ttl_is_bounded():
    """quota/flagged 的缓存 TTL 不得超过 600s。"""
    for verdict in ("quota", "flagged"):
        ttl = daemon._probe_ttl(verdict)
        assert ttl <= 600, (
            f"{verdict} 缓存 TTL={ttl}s（{ttl/3600:.1f}h），"
            "构成第二道停摆：即使探测器想探也会被判『TTL 内已探过』"
        )


def test_cached_quota_still_reprobed_after_ttl(monkeypatch):
    """缓存过期后必须重新实测，不得永久复用旧结论。"""
    monkeypatch.setattr(daemon, "_WARP_PROBE_SEEN", {})
    now = time.time()
    daemon._probe_cache_put("W2", "1.1.1.1", "quota", now=now - 601)
    assert daemon._probe_cache_get("W2", "1.1.1.1", now=now) is None, \
        "超过 TTL 的 quota 结论必须失效，否则这条线永远不再被实测"


# ─────────────────────────────────────────────────────────────
# B1 — 停探的线必须仍有复活路径
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# B12 — 拆掉结论驱动闸门后，必须补上「与结论无关」的固定节奏
# ─────────────────────────────────────────────────────────────

def test_probe_cadence_caps_per_line_rate(monkeypatch):
    """同一线连续失败时，单位时间内探测次数必须有硬上限（中文断言）。

    回归 2026-10-27：时间门禁被整段注释后，非 OK 的线每轮都返回
    needs_probe=True，配额消耗从「停摆」直接翻到「开环」。
    修法是补一个与结论无关的固定节奏，不是把门禁原样装回去。

    每次放行后回写 last_probe_ts —— 不模拟这一步，正确实现也会数出 600 次。
    """
    now = 1000.0
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", last_verdict="quota",
            last_probe_ts=now, next_probe_ts=0.0)

    allowed = 0
    for i in range(600):          # 模拟 600 秒内每秒问一次
        t = now + i
        if daemon._warp_line_needs_probe("W2", t)[0]:
            allowed += 1
            with daemon._WARP_LINE_CV:
                daemon._warp_line_state_locked("W2")["last_probe_ts"] = t

    assert allowed <= 10, (
        f"600 秒内允许探测 {allowed} 次 —— 无固定节奏，配额消耗不可预测"
    )


def test_cadence_survives_zero_next_probe_ts(monkeypatch):
    """next_probe_ts 为 0 时节奏仍必须生效 —— 这是 min() 写法最容易漏掉的分支。

    回归：`_warp_line_note_ok` / `_warp_line_invalidate` 都会把它写成 0，
    写成 `min(next_probe_ts, last+60)` 就会坍缩成 0，等于完全放行。
    """
    now = 1000.0
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", last_verdict="quota",
            last_probe_ts=now, next_probe_ts=0.0)

    assert not daemon._warp_line_needs_probe("W2", now + 1)[0], (
        "刚探完 1 秒就允许再探 —— next_probe_ts=0 把节奏吃掉了"
    )


def test_cadence_is_verdict_independent(monkeypatch):
    """节奏上限不得因结论不同而变化 —— 观测不因观测结果而改变频率。"""
    now = 1000.0
    gaps = {}
    for verdict in ("quota", "blocked", "flagged", "err", "unreachable"):
        with daemon._WARP_LINE_CV:
            daemon._warp_line_state_locked("W2").update(
                ip="1.1.1.1", last_verdict=verdict,
                last_probe_ts=now, next_probe_ts=0.0)
        first = next(i for i in range(600)
                     if daemon._warp_line_needs_probe("W2", now + i)[0])
        gaps[verdict] = first

    assert len(set(gaps.values())) == 1, (
        f"不同结论的重探间隔不一致：{gaps} —— "
        "结论只该影响「探完拿它做什么」，不该影响「下次还探不探」"
    )


def test_prober_never_freezes_every_line(monkeypatch):
    """全部 20 条线都被停探时，探测器仍须在 60s 内调度它们。"""
    now = time.time()
    members = [_member(f"W{i}", 10000 + i) for i in range(20)]
    monkeypatch.setitem(daemon.CFG["warp"], "members", members)
    for m in members:
        with daemon._WARP_LINE_CV:
            daemon._warp_line_state_locked(m["tag"]).update(
                ip=f"10.0.0.{m['tag'][1:]}", last_verdict="quota",
                next_probe_ts=now + 13.3 * 3600)

    schedulable = [m["tag"] for m in members
                   if daemon._warp_line_needs_probe(m["tag"], now)[0]]
    assert len(schedulable) == len(members), (
        f"20 条线中只有 {len(schedulable)} 条可被调度 —— 其余没有任何复活入口。"
        "这正是 2026-10-27 现场（56.6 分钟零探测、74 次换线全废）"
    )


def test_restart_clears_warming_on_every_return_path(monkeypatch):
    """所有提前 return 都必须清 warming，否则该线被永久跳过（闸 2 泄漏）。"""
    m = _member()
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_line_busy", lambda *a, **k: True)  # 判忙
    daemon._warp_line_restart(m, "test")
    assert daemon._warp_line_state("W2")["warming"] is False, \
        "判忙返回后 warming 仍为 True，这条线会被所有探测路径永久跳过"


# ─────────────────────────────────────────────────────────────
# B13 — 每线每小时重启次数必须有硬上限
# ─────────────────────────────────────────────────────────────

def test_restart_rate_is_capped(monkeypatch):
    """一小时内第 21 次重启必须被拒。

    回归 2026-10-27：`restart_max_per_hour_per_line` 只存在于 CLAUDE.md:664，
    daemon.py 0 处引用、config.py 无此键 —— 上限从未实现。
    实测 WARP-CF-F 1.67h 内重启 127 次 = 76 次/h，是宣称值的 3.8 倍。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "restart_max_per_hour_per_line", 20)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_line_busy", lambda tag, win: False)
    monkeypatch.setattr(daemon, "_acquire_refresh_slot", lambda tag: True)
    monkeypatch.setattr(daemon, "_release_refresh_slot", lambda tag: None)
    monkeypatch.setattr(daemon, "_RESTART_SEM", None)
    monkeypatch.setattr(daemon, "_restart_service", lambda svc, tag=None: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda port, t: True)
    monkeypatch.setattr(daemon, "_probe_refresh_egress", lambda port: "9.9.9.9")
    monkeypatch.setattr(daemon, "_invalidate_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(daemon, "_line_block_remaining", lambda ip, ts: 0.0)

    m = _member("W2", 10002)
    performed = 0
    for _ in range(40):
        if daemon._warp_line_restart(m, "test"):
            performed += 1
            with daemon._WARP_LINE_CV:
                daemon._warp_line_state_locked("W2")["next_restart_ts"] = 0.0

    assert performed <= 20, (
        f"同一线连跑 40 次重启请求放行了 {performed} 次 —— 每小时上限未生效。"
        "实测生产 76 次/h/线，每次掐断在途连接并白烧一格探测配额"
    )


def test_restart_cap_does_not_stop_probing(monkeypatch):
    """达重启上限后，探测必须照常放行 —— 压的是重启，不是观测。

    用户规则：「探测逻辑不应该被任何东西限制，即使没有任何东西可以探测」。
    达上限只应让它停在「反复重启」，不该让这条线从探测器视野里消失。
    """
    monkeypatch.setitem(daemon.CFG["warp"], "restart_max_per_hour_per_line", 20)
    now = 1000.0
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", last_verdict="blocked",
            last_probe_ts=now - 3600, next_probe_ts=0.0,
            restart_hist=[now - 60 * i for i in range(20)])

    assert daemon._warp_line_needs_probe("W2", now)[0], (
        "重启配额用尽后连探测也被挡了 —— 上限必须只作用于重启这一个动作"
    )


def test_restart_cap_survives_state_reload(monkeypatch):
    """restart_hist 必须随状态表落盘并被正确恢复。

    `_load_warp_line_state:2614` 只恢复 `k in _LINE_DEFAULTS` 的键 ——
    新键不加进那张表会被静默丢弃，daemon 每重启一次上限就清零。
    实测窗口内 20:35:09 daemon 重启过一次。
    """
    assert "restart_hist" in daemon._LINE_DEFAULTS, (
        "restart_hist 不在 _LINE_DEFAULTS 中 —— 落盘后重启即丢，上限形同虚设"
    )

    now = time.time()
    hist = [now - 60 * i for i in range(20)]
    path = os.environ["WARP_LINE_STATE_FILE_PATH"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1,
                   "lines": {"W2": {"ip": "1.1.1.1", "restart_hist": hist}}}, f)

    daemon._load_warp_line_state()
    restored = daemon._warp_line_state("W2").get("restart_hist") or []
    assert len(restored) == 20, (
        f"重启后只恢复了 {len(restored)} 条重启记录 —— 上限窗口被清零，"
        "下一次 daemon 重启就能再刷满 20 次"
    )


# ─────────────────────────────────────────────────────────────
# B6 — 换线不得空等
# ─────────────────────────────────────────────────────────────

def test_switch_wait_is_bounded(monkeypatch):
    """无候选时的等待上限不得超过 3s。"""
    assert float(daemon._warp().get("switch_wait_sec", 20)) <= 3, \
        "switch_wait_sec 仍 >3s：74 次触发 × 20s = 24.7 分钟纯空转"


def test_reconnect_forces_probe_before_waiting(monkeypatch):
    """无新鲜候选时必须先主动催探测，而不是被动等表。

    实现：遍历所有非当前出口的线，把 next_probe_ts 置 0 并 notify_all，
    让探测器下一轮立刻处理。**不依赖不存在的 _warp_line_force_probe_all**
    —— `grep -n _warp_line_force_probe_all daemon.py` 实测 0 处。
    """
    now = time.time()
    monkeypatch.setattr(daemon, "_warp_line_fresh_candidates",
                        lambda tag, now=None: [])
    monkeypatch.setattr(daemon, "_warp_line_wait_fresh",
                        lambda tag, deadline: [])
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "_warp_member", lambda tag: _member("W1", 10001))
    monkeypatch.setattr(daemon, "_ACTIVE_WARP_IP", None, raising=False)
    monkeypatch.setattr(daemon, "_mark_warp_ip_used", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_save_warp_line_state", lambda *a, **k: None)

    # 两条线都被停探 13.3 小时
    with daemon._WARP_LINE_CV:
        for tag, ip in (("W2", "1.1.1.1"), ("W3", "2.2.2.2")):
            daemon._warp_line_state_locked(tag).update(
                ip=ip, last_verdict="quota",
                next_probe_ts=now + 13.3 * 3600)

    daemon._reconnect_warp_by_table("test")

    # 修前：两条线 next_probe_ts 原封不动 —— 无人催，探测器要等到次日 08:00
    parked = [t for t in ("W2", "W3")
              if daemon._warp_line_state(t)["next_probe_ts"] - now > 60]
    assert parked == [], (
        f"无候选时未催探测，{parked} 仍停探 13.3 小时 —— "
        "探测器停摆时换线必然等满超时"
    )


# ─────────────────────────────────────────────────────────────
# 回归保护 — 下面这些行为不能被上面的修复破坏
# ─────────────────────────────────────────────────────────────

def test_blocked_ip_still_restarts_without_burning_quota(monkeypatch):
    """B5 改 TTL 后，台账判脏仍须直接重启换址、不打模型接口。

    这条与原 test_warp_prober.py 的同名用例互补：那边锁 _warp_ip_is_blocked，
    这边锁 _probe_ttl 改动没有把它一起改坏。
    """
    m = _member()
    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", next_probe_ts=time.time() + 5)
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_egress_ip",
                        lambda port, timeout_sec=20.0: "1.1.1.1")
    monkeypatch.setattr(daemon, "_warp_ip_is_blocked", lambda ip, now=None: True)
    calls = []
    monkeypatch.setattr(daemon, "_warp_model_usable",
                        lambda *a: calls.append(a) or True)
    restarted = []
    monkeypatch.setattr(daemon, "_warp_line_restart",
                        lambda mm, r: restarted.append(mm["tag"]) or True)

    daemon._probe_one_line(m)
    assert calls == [], "台账判脏后不得再烧模型配额"
    assert restarted == ["W2"], "判脏必须触发重启换址，否则线冻结在脏 IP 上"


def test_restart_new_ip_still_clears_park(monkeypatch):
    """修复后「重启换到新 IP 即恢复探测」的行为必须保留。"""
    monkeypatch.setattr(daemon, "active_warp_tag", lambda: "W1")
    monkeypatch.setattr(daemon, "warp_line_busy", lambda tag, win: False)
    monkeypatch.setattr(daemon, "_acquire_refresh_slot", lambda tag: True)
    monkeypatch.setattr(daemon, "_release_refresh_slot", lambda tag: None)
    monkeypatch.setattr(daemon, "_RESTART_SEM", None)
    monkeypatch.setattr(daemon, "_restart_service", lambda svc, tag=None: True)
    monkeypatch.setattr(daemon, "_wait_port_ready", lambda port, t: True)
    monkeypatch.setattr(daemon, "_probe_refresh_egress", lambda port: "9.9.9.9")
    monkeypatch.setattr(daemon, "_invalidate_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(daemon, "_line_block_remaining", lambda ip, ts: 0.0)

    with daemon._WARP_LINE_CV:
        daemon._warp_line_state_locked("W2").update(
            ip="1.1.1.1", last_verdict="quota",
            next_probe_ts=time.time() + 13.3 * 3600)

    assert daemon._warp_line_restart(_member(), "模型层 quota") is True
    st = daemon._warp_line_state("W2")
    assert st["ip"] == "9.9.9.9"
    assert st["next_probe_ts"] - time.time() <= 60, \
        "换到新地址后仍停探，等于换了地址也不看一眼"
```

---

## 第二部分续：9router 侧测试用例

> 落盘位置：
> - `tests/unit/egress-pool-identity.test.js`（B9，纯函数，无 mock）
> - `tests/unit/opencode-egress-retry-context.test.js`（B8，需 mock fetch）
>
> 运行：`cd ~/.9router/tests && npx vitest run unit/egress-pool-identity.test.js`
> **B10/B11 不写用例** —— 结论是「记录不改」，写用例会把一个刻意保留的设计锁死。

```js
// tests/unit/egress-pool-identity.test.js
//
// 回归 2026-10-27：三个 isActive=1 的池共用 http://127.0.0.1:7890，
// identifyFixedEgress 只按 proxyUrl 算 id，三者算出同一 id；
// auth.js 按 proxyPoolId 去重留下两个候选，selectAlternateProxy 按 egress id
// 去重把它们全跳掉 → 备用出口恒为空 → 429 无法换出口。
//
// 命名约定：<场景>_<期望行为>，断言信息用中文说明为什么这是错的。

import { describe, it, expect } from "vitest";
import {
  identifyFixedEgress,
  selectAlternateProxy,
} from "../../open-sse/utils/egress.js";

const TARGET = "https://opencode.ai/zen/v1/chat/completions";

// 与生产 proxyPools 同形：url 相同，池 id 不同
const pool = (id, url, strictProxy = false) => ({
  proxyPoolId: id,
  connectionProxyEnabled: true,
  connectionProxyUrl: url,
  connectionNoProxy: "",
  vercelRelayUrl: "",
  strictProxy,
});

describe("池身份（B9）", () => {
  it("same_url_different_pools_must_not_collide", () => {
    const a = pool("644a72f0", "http://127.0.0.1:7890", true);
    const b = pool("351035a6", "http://127.0.0.1:7890");
    const c = pool("24de9bf6", "http://127.0.0.1:7890");

    const ids = [a, b, c].map((p) => identifyFixedEgress(TARGET, p).id);

    expect(new Set(ids).size).toBe(3);
    // 碰撞后果：备用出口列表非空，但 selectAlternateProxy 一个都选不出来
    // → 429 之后无路可走，只能等 sidecar 冷却。
  });

  it("collision_makes_alternates_unselectable", () => {
    const primary = pool("644a72f0", "http://127.0.0.1:7890", true);
    const alt1 = pool("351035a6", "http://127.0.0.1:7890");
    const alt2 = pool("24de9bf6", "http://127.0.0.1:7890");

    // auth.js:63-76 的产物：按池 id 排掉当前池，留下另两个
    const proxyOptions = { ...primary, connectionProxyAlternates: [alt1, alt2] };
    const currentId = identifyFixedEgress(TARGET, primary).id;

    const selection = selectAlternateProxy(
      proxyOptions, TARGET, new Set([currentId])
    );

    expect(selection.alternate).not.toBeNull();
    // 修前为 null：两个候选的 egress id 与 currentId 相同，被 excludeIds 全跳过
  });

  it("different_url_still_yields_alternate (回归保护)", () => {
    const primary = pool("644a72f0", "http://127.0.0.1:7890", true);
    const other = pool("dead", "http://127.0.0.1:7000");
    const proxyOptions = { ...primary, connectionProxyAlternates: [other] };
    const currentId = identifyFixedEgress(TARGET, primary).id;

    const selection = selectAlternateProxy(
      proxyOptions, TARGET, new Set([currentId])
    );

    // 这条现在就是绿的 —— 说明缺陷只在「同 url 多池」时出现，
    // 不是 selectAlternateProxy 整体失效。修复不能改变它。
    expect(selection.alternate?.egress.proxyUrl).toBe("http://127.0.0.1:7000");
  });
});
```

```js
// tests/unit/opencode-egress-retry-context.test.js
//
// 回归 2026-10-27：429 重试后的结果直接 return，外层无法区分
// 「首次 503」与「429 后重试仍失败」。后者说明换出口有效但换到了脏出口，
// 应该优先换线而不是等 sidecar 冷却。
//
// 注意：这条用例锁的是「可观测」不是「具体字段名」。实现可以改
// egressRetried 标志 / 响应头 / 其它标记，断言只要求两者可区分。

import { describe, it, expect, vi, beforeEach } from "vitest";
import { getExecutor } from "../../open-sse/executors/index.js";

const TARGET = "https://opencode.ai/zen/v1/chat/completions";
const pool = (id, url) => ({
  connectionProxyEnabled: true,
  connectionProxyUrl: url,
  connectionNoProxy: "",
  proxyPoolId: id,
});

const makeBody = () => ({ model: "oc/deepseek-v4.1-flash-free",
                          messages: [{ role: "user", content: "hi" }] });
const okResponse = () => new Response(JSON.stringify({ ok: true }),
                                      { status: 200 });
const rateLimitedResponse = () => new Response(
  JSON.stringify({ type: "error",
                   error: { type: "FreeUsageLimitError",
                            message: "Free usage limit reached." } }),
  { status: 429 });

describe("429 重试上下文（B8）", () => {
  let fetchMock;

  beforeEach(() => {
    fetchMock = vi.fn();
    globalThis.fetch = fetchMock;
  });

  it("first_attempt_503_differs_from_retry_after_429", async () => {
    const executor = getExecutor("opencode");
    const primary = pool("a", "http://proxy-a.example:8080");
    const alt = pool("b", "http://proxy-b.example:8080");
    const proxyOptions = { ...primary, connectionProxyAlternates: [alt] };

    // 首次 429 → 换出口重试 → 仍 429
    fetchMock
      .mockResolvedValueOnce(rateLimitedResponse())
      .mockResolvedValueOnce(rateLimitedResponse());

    const retried = await executor.execute({
      body: makeBody(), proxyOptions, targetUrl: TARGET,
    });

    fetchMock.mockReset();
    // 首次就 429，且没有候选出口 → 不重试
    fetchMock.mockResolvedValueOnce(rateLimitedResponse());
    const plain = await executor.execute({
      body: makeBody(),
      proxyOptions: { ...primary, connectionProxyAlternates: [] },
      targetUrl: TARGET,
    });

    const sawRetry = readRetriedFlag(retried);
    const plainRetry = readRetriedFlag(plain);

    expect(sawRetry).not.toBe(plainRetry);
    // 修前两者都是 undefined / false —— 外层无法判断该换线还是该等冷却
  });
});

// 兼容三种可能实现：返回对象字段 / 响应头 / 无标记
function readRetriedFlag(result) {
  return result?.egressRetried
      ?? result?.response?.headers?.get?.("x-egress-retried")
      ?? null;
}
```

---

## 第三部分：用例与 Bug 映射

| Bug | 用例 | 修前 | **现况（12:10 UTC 实测）** |
|---|---|---|---|
| B1 | `test_unknown_streak_triggers_restart` | 红 | **仍红**（unknown 仍不重启） |
| B1 主症状 | `test_prober_never_freezes_every_line` | 红 | **已天然绿** —— 门禁已拆，20/20 可调度 |
| B2 | `test_quota_verdict_does_not_extend_probe_beyond_cadence`、`test_blocked_verdict_...` | 红 | **仍红**（写入侧未改，实测仍写 47880s） |
| B3 | `test_persistent_retry_floor_does_not_apply_to_probe_cadence` | 红 | **仍红**（地板仍 21600） |
| B4 | `test_note_ok_resets_next_probe_ts_even_if_parked`、`test_ok_verdict_after_quota_restores_probe` | 红 | **已天然绿** —— `note_ok` 已改为 `= 0`；**不必再写** |
| **B12** | `test_probe_cadence_caps_per_line_rate`、`test_cadence_is_verdict_independent`、`test_cadence_survives_zero_next_probe_ts` | — | **新用例，当前必红**（无节奏） |
| **B13** | `test_restart_rate_is_capped`、`test_restart_cap_does_not_stop_probing`、`test_restart_cap_survives_state_reload` | — | **新用例，当前必红**（0 处引用，无上限） |
| B5 | `test_quota_cache_ttl_is_bounded`、`test_cached_quota_still_reprobed_after_ttl` | 红 | **仍红**（TTL 仍 21600） |
| B6 | `test_switch_wait_is_bounded`、`test_reconnect_forces_probe_before_waiting` | 红 | **仍红**（`switch_wait_sec` 仍 20） |
| **B9** | `same_url_different_pools_must_not_collide`、`collision_makes_alternates_unselectable`、`same_physical_egress_is_not_an_alternate` | 红 | **仍红**（9router 源码未动） |
| **B8** | `first_attempt_503_differs_from_retry_after_429` | 红 | 红（降 P2 后可缓） |
| **B10/B11** | 不写用例 | — | — |
| 闸 2 泄漏 | `test_restart_clears_warming_on_every_return_path` | 待验 | 待验 |
| 回归 | `test_blocked_ip_still_restarts_without_burning_quota`、`test_restart_new_ip_still_clears_park`、`different_url_still_yields_alternate` | 绿 | **绿（98 passed）** |

**"已天然绿" ≠ 有保护**：B1 主症状与 B4 现在之所以通过，是因为并行 session 改了代码，
**不是因为有用例在守着**。当前 `pytest test_warp_prober.py test_warp_refresh.py
test_429_cooldown.py -q` → **98 passed**，其中没有一条会因为把门禁装回去而转红。
这正是 B12 用例要补的缺口。

**两个仓库的基线命令**

```bash
# 出口侧
cd ~/dkk-projects/auto-switch-ip
pytest test_warp_prober.py test_warp_refresh.py test_429_cooldown.py -q

# 网关侧（vitest；~64 例已知红，必须与 known-fails.txt 比对而非看总数）
cd ~/.9router/tests
npx vitest run unit/egress.test.js unit/opencode-egress.test.js
node ../tests/__baseline__/verify-no-regression.mjs
```

**必须保持绿的既有用例**：

```bash
pytest test_warp_prober.py test_warp_refresh.py test_429_cooldown.py -q
```

特别注意这 **6 条**带 WHY 的用例，它们锁定了**不该动**的语义：

```
test_restart_same_ip_keeps_quota_park
test_restart_to_another_banned_ip_parks_until_unban
test_blocked_ip_probe_restarts_without_burning_model_quota
test_dirty_ip_is_never_probed
test_dirty_ip_shared_by_two_lines_is_not_probed
test_probe_unknown_ip_does_not_restart      ← 审查新增：B1 修复不得推翻它
```

---

## 第四部分：被推翻的方案（必须记录，防止后人重蹈）

我在 `docs/prober-gate-design-20261027.md` 初版里把这三条列为 P0「移除」：

| 原方案 | 状态 | 推翻依据 |
|---|---|---|
| 删掉 `_warp_ip_is_blocked` 在 `_probe_one_line` 入口的判脏跳过（`daemon.py:3028`） | **撤回** | `test_blocked_ip_probe_restarts_without_burning_model_quota` 明确要求"blocked 判定后不得再烧模型配额"，并有 WHY |
| 删掉 `_warp_ip_is_blocked` 在 `_warp_model_usable` 入口的判脏跳过（`daemon.py:3991`） | **撤回** | `test_dirty_ip_is_never_probed` / `test_dirty_ip_shared_by_two_lines_is_not_probed` 明确要求同一脏 IP 不二次探测 |
| 删除 `_probe_ttl` 对 quota 的长 TTL | **降级为缩短 TTL** | 缓存的价值真实（同 `/128` 重探 99.2% 仍 quota），问题是长度不是机制 |

### 为什么撤回是对的（不只是"测试不让改"）

`_warp_ip_is_blocked` 判脏后的动作是：

```python
_warp_line_record_observation(tag, ip, "blocked", ...)   # 记结论
_warp_line_restart(member, "出口处于惩罚期")              # ← 重启换址
```

**这不是"限制探测"，这是"换掉一个已知坏的地址"。** 重启后拿到新 IP，下次探测照常。链条本身是通的。

真正断的是 **B1**：重启要从 `_probe_one_line` 里发起，而停探让 `_probe_one_line` 根本不执行。

所以修复重心应该从"拆掉闸 3"移到"保证闸 1 不永久冻结 + 保证停探线仍有调度入口"。这更小、更准、不和已有 WHY 冲突。

**这是我上一轮分析的主要错误：把"预期行为"当成了"待移除的闸门"。**

---

## 第五部分：执行顺序建议

**分两个仓库、两条独立线。出口侧先修 —— 它决定 91.2% 的 503，网关侧修的是"下次能不能少放大"。**

> **顺序已按 12:10 UTC 的现状重排**：B4 已修、B1 主症状已被并行改动消解，
> 现在真正的头号项是 **B12（配额开环）** —— 它是那两个改动的直接副作用，不补就会从
> "停摆 13.3h" 翻成"每轮重探"。而 B13（重启无上限）必须与 B12 同批落地。

### 出口侧（`auto-switch-ip`）

```
1. 跑基线：pytest test_warp_prober.py test_warp_refresh.py test_429_cooldown.py -q
           预期 98 passed（已实测）
2. 落盘 test_warp_prober_gates.py，确认红的是预期的那些（不是 import 错误）
3. **修 B12（补固定节奏）**     → 验证 test_probe_cadence_caps_per_line_rate
   **修 B13（重启上限）**        → 验证 test_restart_rate_is_capped
   ⚠ 第 3 步两件事必须同批：只补节奏 = 把随机重启风暴改成 60s 节拍器
4. 修 B2+B3（不再把 ban_until 写进排程，地板 ≤60s）→ 验证 test_quota_verdict_... / test_persistent_retry_floor_...
5. 修 B5（缩短 TTL）            → 验证 test_quota_cache_ttl_...
6. 修 B6（催探测 + 缩短等待）   → 验证 test_switch_wait_... / test_reconnect_...
7. 修 B1 残部（区分 unknown）   → 验证 test_unknown_streak_triggers_restart
                                    + 确认 test_probe_unknown_ip_does_not_restart 仍绿
8. 回归：全部既有用例必须仍绿（尤其 6 条带 WHY 的）
9. 现场验证：M1 判据见 docs/review-5d-20261027.md §9.4
```

**为什么 B12 排第一而不是最后**

拆门禁的改动已经在工作区里了（未提交）。在补上固定节奏之前：
- 任何一条非 OK 的线都会每轮被调度；
- `probe_max_per_min=600` 对 20 条线形同虚设；
- 每次模型层探测烧一格配额。

也就是说，**其余各条的"修好"都会在开环消耗下失去意义** —— 探得再勤，配额烧完也是 quota。

**为什么 B13 必须与 B12 同批，不能等下一批**

B12 修好之后，`blocked` 线每 60s 被探一次，探完立刻触发重启
（`_probe_one_line:3059`）→ **稳定 60 次/h/线**。当前实测 76 次/h 是「无节奏时靠
探测往返耗时撞出来的」，装上节拍器后会变成**可预测的 60 次/h**，比现在更确定地打满。
`restart_max_per_hour_per_line` 是唯一能兜住它的东西，而它至今不存在。

**B1 仍放最后**：它必须区分两种 unknown，不得推翻
`test_probe_unknown_ip_does_not_restart`。且 B12/B2/B3 修好后，
停探不再出现，B1 的触发窗口自然变窄。

### 网关侧（`~/.9router`）

```
1. 跑基线：npx vitest run unit/egress.test.js unit/opencode-egress.test.js
           node tests/__baseline__/verify-no-regression.mjs
2. 落盘 tests/unit/egress-pool-identity.test.js
3. **先定 B9 方案（A/B/C）** —— 方案 A：selectAlternateProxy 按 proxyUrl 去重
4. 修 B9 → 验证 same_url_different_pools_must_not_collide
          + collision_makes_alternates_unselectable
          + same_physical_egress_is_not_an_alternate
5. 回归：different_url_still_yields_alternate 仍绿
          既有 opencode-egress.test.js 仍绿（它断言 cooldown 恒 null）
6. B10/B11 不改
```

**B9 是当前唯一的真 P0**：`~/.9router` 源码工作区无改动（`git status --porcelain` 只有
数据库与构建产物），B9 一个字都没被动过。

**B9 必须排在 B8 之前**：B8 的「429 后重试仍失败」在当前配置下根本走不到 ——
候选集恒为空，`selectAlternateProxy` 返回 null，代码在 `opencode.js:631` 就 `return result` 了，
不会进入重试分支。**先修 B9 让重试路径真的可达，B8 才有观测价值。**

**B10/B11 明确不改的理由**已写在各条 bug 单里：根因是共享 sidecar / 跨仓库判据，
改动收益低于风险，且 B9 修好后 B11 的攻击面自然消失。

---

## 第六部分：跨仓库的两个同构缺陷

这两条是本次两个仓库里最值得记住的模式，形态相同、位置不同：

| | 出口侧 B1 | 网关侧 B9 |
|---|---|---|
| **表现** | 20 条线全停探，74 次换线全废 | 3 个池共用 url，备用出口恒为空 |
| **失效的层** | 调度队列（`next_probe_ts` 在未来 → 不调度） | 候选集（egress id 碰撞 → 全被排除） |
| **共同点** | **「换一个出口重试」这条唯一自愈路径被自己堵死** | 同左 |
| **判别信号** | `↻ 换线 74 / ✓ 成功 0` | `connectionProxyAlternates` 非空但 `alternate === null` |
| **一般化** | 一个「重试/换路」机制的候选集或调度条件，是否会因为自身的健康判定而恒空？ |

第二列的数字都是可观测的：74 和 0 都在日志里，`alternates` 长度和 `alternate === null`
也能从 `EGRESS` 日志读出来。**这两个信号同时出现时，不要再查上游参数** ——
问题在本地把候选集/队列排空的那个判据上。

---

## 自检

我声称完成的任务: 基于已核实的代码证据，产出跨两仓库的 Bug 单（出口侧 B1-B7 + B12 + B13、
网关侧 B8-B11）与测试用例，并在发现工作区被并行 session 改动后重做现状校正。

我实际运行的验证命令（本轮新增，全部有输出）:

  ── 工作区状态（发现分叉的关键）──
  - `git status --porcelain daemon.py` → ` M daemon.py`（未提交）
  - `git diff --stat daemon.py` → `+4 −5`
  - `stat -c '%y' daemon.py` → 2026-10-27 19:59:04 +0800（= 11:59 UTC）
  - `git status --porcelain`（~/.9router）→ 源码无改动，仅 db/与构建产物

  ── 运行时复验（不是读代码推断）──
  - `_warp_line_needs_probe('W2', now)`，next_probe_ts=now+13.3h
    → `needs_probe = True | due - now = 0s` ← 门禁已失效
  - `_warp_line_record_observation('W2','1.1.1.1','quota', ts=1000.0)`
    → `next_probe_ts - ts = 47880s (13.3h)` ← 写入侧未改
  - `_probe_ttl('quota')` → `21600.0`（B5 仍存）
  - `config.CFG['warp']` → `switch_wait_sec=20`、`probe_persistent_retry_sec=21600`、
    `probe_persistent_ttl_sec=21600`、`rate429_guard_sec=60`、`proactive_mode='fixed'`
    （B7 的两处不符实测确认）
  - `ls test_warp_prober_gates.py` → No such file（用例仍未落盘）

  ── 基线测试（地面真值）──
  - `pytest test_warp_prober.py test_warp_refresh.py test_429_cooldown.py -q`
    → **98 passed in 0.21s**

  ── B12 修法的离线验证（本轮新增，在 /tmp 内跑，未触碰仓库）──
  - 按 FR-13 的正确实现（`due = last_probe_ts + 60s`）模拟三条新用例
    → `PASS test_caps_per_line_rate` / `PASS test_cadence_is_verdict_independent`
      / `PASS test_cadence_survives_zero_next_probe_ts`
  - 反证：同一模拟改用我第一版的 `min(next_probe_ts, last+60)` 写法
    → `600 秒内放行 600 次 -> 如预期失败`
  - `probe_transient_retry_sec = 120`、`probe_transient_retry_max_sec = 600`
    → 若照抄 `min(base * 2^(streak-1), cap60)`，七个档位全是 60.0s（无退避效果），
      故 ADR-6 改为常量 cadence，不再写成假的指数退避

  ── B13 取证与离线验证（本轮新增）──
  - `grep -rn 'restart_max_per_hour' --include='*.py' .` → **无输出**（0 处引用）
  - `grep -n 'restart_max_per_hour_per_line' CLAUDE.md config.py`
    → 只有 `CLAUDE.md:664`；`config.py` 无此键
  - `python3 -c "import config; [k for k in CFG['warp'] if 'restart' in k]"`
    → `['restart_cooldown_sec','restart_backoff_sec','restart_backoff_max_sec','restart_concurrency']`
      四个都不管速率
  - `restart_count`：`:2532` 初始化 / `:2950` 自增 —— **0 处读取**
  - `sudo journalctl -u auto-switch-ip --since '3 hours ago' -o short-iso | grep '↻ 重启'`
    → 1836 条，窗口 `19:59:31` → `21:39:44` = **1.67h**
  - 按 tag 计数 → WARP-CF-F 127 次 = **76/h**（宣称上限 20/h，超 3.8×）；
    全池合计 **1099 次/h**
  - 离线模拟三条 B13 用例（`/tmp/b13_check/probe.py`，不 import daemon）
    → 上限生效时 40 次请求放行 20 次；1 小时后窗口滚动重新可用；无上限则 40 次全放行

  ── 前几轮已完成的取证（仍然有效）──
  - `grep -c _warp_line_force_probe_all daemon.py` → **0**（该函数不存在）
  - `grep -n _warp_line_needs_probe daemon.py` → 仅 2 处（2672 定义 + 3072 唯一调用点）
  - `Read egress.js:228-238` → `markEgressCooling` 对 `kind==="proxy"` 返回 null
  - `Read opencodeTransport.js:14` → `let active = null`（单 sidecar）
  - `node -e "hashEgress(...)"` → 三活跃池 id 均为 `proxy:f1410cd719dcee161d171a3729c50bff`
  - `Read test_warp_prober.py:81-92` → 锁死「单次 unknown 不重启」

未验证的断言:
  - 新增的 B12 / B13 六条用例在**真实 daemon 上未运行**（文件尚未落盘）。只验证了
    「按 FR-13 / FR-14 的正确实现，这些用例会通过；按第一版的 `min()` 写法会失败」——
    这是对修法逻辑的验证，不是对 daemon 现状的验证
  - `test_restart_cap_survives_state_reload` 依赖 `_load_warp_line_state` 的
    `k in _LINE_DEFAULTS` 过滤行为（`daemon.py:2614` 已读源码确认），但未实跑
  - B8 的 `first_attempt_503_differs_from_retry_after_429` 依赖 `executor.execute` 的入参形状，
    未与 `tests/unit/opencode-egress.test.js` 的 `run()` helper 逐字对齐
  - 「修 B9 后 selectAlternateProxy 就能选出候选」是静态推断，未实跑

已知风险:
  1. 断言里的 `<=60` / `<=120` / `<=10` 具体数值来自设计推导，未经现场调优
  2. `test_prober_never_freezes_every_line` 依赖 `CFG["warp"]["members"]` 结构
  3. B9 改 egress id 会改变所有既有缓存键 —— 本轮未评估（ADR-2 选 A 已回避）
  4. **并行 session 正在改同一个 daemon.py**：本轮取证基于 11:59 UTC 的快照，
     若它在之后又改，§0 的校正需要重跑
  5. 全部改动仍待授权；我本轮未写任何生产代码，只写了两份 docs

### 相对前几版文档的更正

| 旧结论 | 更正 | 依据 |
|---|---|---|
| 「三池冷却把它们排除掉」 | 排除原因是 **egress id 碰撞**；proxy 类出口从不进冷却表 | `egress.js:238` `if (egress.kind === "proxy") return null;` |
| 「`markEgressCooling` 冷却锁死链路」 | 锁链的是 **transport 层 `lastResetAt`**，且它是共享 sidecar 的合理节流 | `opencodeTransport.js:14,18` 单变量 + 注释 `:232-234` |
| 「B10 应按出口分键」 | **不成立** —— 全进程只有一个 sidecar，没有"每个出口各自的冷却" | `opencodeTransport.js:14` `let active = null` |
| 「B8 会让 combo 按出口故障处理真 429」 | 真 429 正常返回时 combo 识别正确；缺失的只是「这是重试后失败」的**归因**，且 daemon 侧已单独分类 | `daemon.py:1108-1115` `_classify_transport_error` |
| 「B1 = 停探即永久冻结」 | **门禁已被并行 session 注释掉**，冻结后果不成立；只剩 unknown 子问题 | §0 运行时复验 `needs_probe = True` |
| 「B4 待修」 | **已修**，`note_ok` 改为 `= 0` | `daemon.py:2763` |
| 「修完 B1-B6 就没事了」 | **错**。拆门禁引入了 B12（配额开环），它现在是头号项 | §B12；`pytest` 98 passed 但无一条守住节奏 |
| B12 修法写 `min(next_probe_ts, last+60)` | **错**。`note_ok` / `invalidate` 会把 `next_probe_ts` 写 0，`min` 坍缩成 0 → 600s 放行 600 次（已模拟复现） | §B12「一个写出来会失效的写法」 |
| 节奏写成 `min(base * 2^(streak-1), 60)` | **名不副实**。`base=120` 已 > `cap=60`，七档全是 60.0s，退避效果为零 | 实测 `probe_transient_retry_sec=120` |
| 「每线每小时重启上限 20（`CLAUDE.md:664`）」 | **该上限从未实现**。`restart_max_per_hour_per_line` 在 daemon.py 中 0 处引用、config.py 无此键。实测 **76 次/h/线**，超宣称值 3.8× | `grep -rn 'restart_max_per_hour' --include='*.py' .` → 无输出；journalctl 1.67h 窗口 1836 条 |
| 「重启成功有 `next_restart_ts` 兜底」 | **不成立**。成功分支写 `st["next_restart_ts"] = 0.0`（`:2952`），退避只在失败分支 | `daemon.py:2937` vs `:2952` |
| 「`restart_count` 是一个在用的计数器」 | **只写不读**。`:2532` 初始化、`:2950` 自增，0 处读取 —— 差一个 `if restart_count >= N` | 全文 grep |
