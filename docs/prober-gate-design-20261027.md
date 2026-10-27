# 探测器闸门设计缺陷 —— 修复设计

> 日期：2026-10-27
> 前置文档：`docs/503-root-cause-20261027.md`（503 根因与取证）
> 本文只讲**探测器为什么停摆**，以及**该怎么改**
> 状态：设计稿，未执行任何代码改动

---

## 1. 问题陈述（用户原话）

> 探测逻辑不应该被任何东西限制，即使没有任何东西可以探测，即使探测的结果全都是不好的。

这条要求对应一个明确的设计原则：

**探测是观测，不是决策。观测不应因观测结果而停止。**

当前实现违反了它 —— 探测结论（`quota`）被写回成探测排程（`next_probe_ts`），于是"结果不好"直接变成了"不再观测"。这是把观测者和被观测对象混为一谈。

---

## 2. 实测停摆

```
最后一次探测:  2026-10-27 17:54:14（CF-C）
最近一次隧道启动: 2026-10-27 17:54:14（CF-C，与最后一次探测同一秒）
实测时刻:      2026-10-27 18:48:54
停摆时长:      56.6 分钟且持续（18:50:50 实测）
20 条线 last_probe_ts 全部冻结在 11:58–17:54
20 条线 next_probe_ts 全部 ≥ 23:29，其中 18 条在次日 08:00、2 条在当日 23:29/23:31
20 条隧道 ActiveEnterTimestamp 全部 ≤ 17:54:14（同期零重启）
NRestarts = 0（20/20）
```

同期：

```
换线触发   74 次（截至 18:15）
换线成功    0 次
```

74 次全部走 `_reconnect_warp_by_table` 的等待分支：

```python
cands = _warp_line_fresh_candidates(...)      # 空
if not cands:
    deadline = now + switch_wait_sec          # 20s
    cands = _warp_line_wait_fresh(...)        # 等 20s
if not cands:
    log("⚠ 等待超时仍无新鲜备线，本次不切（不重启、不落池）")
    return False                              # ← 74 次全在这
```

累计空等 `74 × 20s = 24.7 分钟`。

---

## 3. 九道闸门

探测器从"该探"到"真探"要过九道闸。任何一道关着，探测就不发生。当前**前三道**就关死了全部 20 条线。

### 闸 1 — `next_probe_ts` 时间闸（**本次根因**）

`daemon.py:2672-2678`

```python
def _warp_line_needs_probe(tag: str, now: float) -> tuple[bool, float]:
    next_ts = float(st.get("next_probe_ts") or 0.0)
    if now < next_ts:
        return False, next_ts        # ← 13.3 小时内不探
```

**`next_probe_ts` 共 21 处出现：13 处运行时赋值 + 2 处 `_warp_line_update` + 1 处初始默认值 + 5 处只读**（实测 `grep -n next_probe_ts daemon.py`）：

| 行 | 函数 | 写入逻辑 |
|---|---|---|
| 2530 | 初始化 | `"next_probe_ts": 0.0` |
| 2728 | `_warp_line_record_observation` OK 分支 | `ts + _line_fresh_sec()` = `now + 120s` |
| 2742 | `_warp_line_record_observation` 非 OK 分支 | `ts + wait`，**`wait = max(6h, ban_until-now)`** ← 病根 |
| 2763 | `_warp_line_note_ok`（生产流量 OK） | `max(old, ts + 120s)` —— **保留旧值，若旧值 > 120s 则不更新** |
| 2781 | `_warp_line_invalidate`（生产标脏） | `min(old, now + 5s)` |
| 2886 | `_warp_line_restart` 判忙而退 | `= next_restart_ts` |
| 2893 | 拿不到 refresh 槽 | `now + restart_cooldown`(5s) |
| 2909 | 前置检查异常 | `now + restart_cooldown`(5s) |
| 2924 | 拿不到重启信号量 | `= next_restart_ts` |
| 2940 | 重启后未就绪 | `= next_restart_ts`（backoff） |
| 2962 | 重启成功、`block_left > 0` | `max(prev_next, now + block_left)` ← **推到 ban_until** |
| 2965 | 重启成功、`same_ip` 且旧 verdict 为持久档 | `= prev_next` ← **沿用长停探** |
| 2968 | 重启成功、其它 | `now + 5s`（唯一健康分支） |
| 2984 | 重启异常 | `= next_restart_ts`（backoff） |
| 3026 | `_probe_one_line` 备线已新鲜且同址 | `now + 120s` |
| 3039 | `_probe_one_line` 速率被拒 | `now + 5s` |

**关键分类**（不然后面会把短值退避误当成病根）：

- **真病根（3 处）**：**2742** `max(6h, ban_until-now)`、**2962** `max(prev_next, now+block_left)`、**2965** `= prev_next` —— 这三处把惩罚期写进了探测排程。
- **短值退避（6 处）**：2886/2893/2909/2924/2940/2984，写 `next_restart_ts` 或 5s，只是防重启风暴，不是停摆源。
- **健康分支（3 处）**：2728（OK→120s）、2968（重启成功且非旧址→5s）、3026（备线同址且新鲜→120s）。
- **待复核（2 处）**：2781 的 `min(old, now+5s)` 会把停探**缩短**，方向正确；2763 的 `max` 是 OK 无法恢复探测的原因。

```
18 条：next_probe_ts = 2026-10-28 08:00:00（13.3h 后）
 2 条：next_probe_ts = 2026-10-27 23:29 / 23:31（4.7h 后）
 1 条（WARP-CF）：last_verdict=OK 但 next_probe_ts 仍在次日 08:00
```

原因：该线在 11:58 产生 OK 时 `next_probe_ts` 已被 quota 推到 08:00；`_warp_line_note_ok` 的 `max(old, ts+120)` 保留了更大的旧值。**OK 结论也无法让它恢复探测。**

### 闸 2 — `warming` 标志

`daemon.py:3011`（`_probe_one_line` 入口）

```python
if st.get("warming"):
    return
```

重启中/预热中的线不探。合理，但 `warming` 若因异常未被清除，会永久阻塞。

### 闸 3 — `_warp_ip_is_blocked` 台账判脏

`daemon.py:3028-3034`

```python
if _warp_ip_is_blocked(ip, now):
    _warp_line_record_observation(tag, ip, "blocked", time.time(), started_at)
    ...
    return
```

探测前查台账：该 IP 若在惩罚期内，直接记 `blocked` 并重启，**根本不做模型层探测**。`_warp_ip_is_blocked` 读 `ban_until` / 惩罚期起点 → 同 `ban_until` → 08:00。

### 闸 4 — `_warp_probe_blind` 整批失明

`daemon.py:3035-3037`

```python
if _warp_probe_blind(now):
    _warp_line_record_observation(tag, ip, "blind", time.time(), started_at)
    return
```

整批探测全 `unreachable` 时宣布失明 120s，期间跳过模型探测。合理，但若触发则是另一道停探。

### 闸 5 — 全局探测速率

`daemon.py:2993-3004`（`_probe_rate_allow`）

```python
limit = probe_max_per_min       # 运行值 600（配置默认 30，被覆盖）
if len(_PROBE_RATE_TIMES) >= limit:
    return False
```

60 秒最多 600 次模型层探测。**实际不生效**（600 远大于 20 条线 × 32 并发）。

### 闸 6 — 探测结论缓存 TTL

`daemon.py:657-678`（`_probe_ttl` + `_probe_cache_get`）

```python
def _probe_ttl(verdict):
    if verdict in ("quota", "flagged"):  return 21600    # 6h
    if verdict == "OK":                  return 600      # 10min
    return 120                             # 2min

def _probe_cache_get(tag, ip, now):
    hit = _WARP_PROBE_SEEN.get((tag, ip))
    if now - ts > _probe_ttl(verdict):
        return None
    return verdict            # ← TTL 内复用结论，不实测
```

`quota`/`flagged` 的 TTL 是 **6h**。**第二道 6 小时停摆**，隐藏在缓存层。与闸 1 叠加：即使闸 1 放开，同一个 `(tag, ip)` 在 6h 内仍不会真探。

### 闸 7 — 并发探测上限

`daemon.py:3076` + `3117`

```python
probe_concurrency = 32      # 运行值（配置默认 4）
model_probe_concurrency = 32
```

全局 32 并发。合理，但实测当前 20 条线也用不满。

### 闸 8 — 模型层探测并发信号量

`daemon.py:3041-3046`

```python
sem = _MODEL_PROBE_SEM      # 32
with sem:
    ok = _warp_model_usable(member, ip)
```

同闸 7，保护配额。

### 闸 9 — 探测额度本身

每次模型层探测烧掉该出口一格 opencode 免费配额。这是**唯一的物理约束**，不是代码闸门。

---

## 4. 设计原则（从这九道闸反推）

分成两类，处理方式不同：

| 类别 | 闸门 | 该不该限制 | 理由 |
|---|---|---|---|
| **调度/资源限制** | 闸 7 并发、闸 8 信号量、闸 5 速率 | **应该保留** | 保护本机与上游，不是"结果驱动" |
| **结论驱动的停探** | 闸 1 `next_probe_ts`、闸 3 台账判脏、闸 6 结论缓存 TTL、闸 4 整批失明（合理但可调）、闸 2 `warming`（需保证可清除） | **必须移除/削弱** | 观测结果决定是否继续观测 = 自我封闭 |

用户的要求精确对应第二类：**"即使探测的结果全都是不好的"** —— 结果不好只应影响「探完之后拿这个结论做什么」，不应影响「下次还要不要探」。

### 为什么这不会造成配额灾难

有人会担心：去掉停探后，20 条线 × 每分钟探 = 烧爆配额。

不会，因为：
1. **闸 5 的 `probe_max_per_min = 600` 仍在** —— 全局速率上限不变
2. **闸 7 的 `probe_concurrency = 32` 仍在** —— 并发上限不变
3. 真正的配额保护应来自**统一的、与结论无关的固定节奏**（如每线每 60s 最多一次），而不是「上次结论是 quota 就停 6 小时」

关键区别：固定节奏是**可预测的消耗**；结论驱动停探是**不可预测的停摆**。前者最坏是"多烧几格配额"，后者最坏是"整链失去自愈能力"（本次 56.6 分钟零探测、74 次换线全废）。

---

## 5. 修补点

### P0 — 移除闸 1：`next_probe_ts` 不再由结论决定

**位置**：`daemon.py:2729-2742`（探测回写） + `daemon.py:2763`（生产 OK 回写） + `daemon.py:2959-2968`（重启路径） + `daemon.py:2781`（生产标脏路径）

**现状**：

```python
# 探测回写 :2729-2742
if verdict == "OK":
    st["next_probe_ts"] = ts + _line_fresh_sec()          # 120s
else:
    if verdict == "unknown":
        wait = _line_unknown_retry()                       # 5s
    elif verdict in ("quota", "flagged", "blocked"):
        wait = max(_line_persistent_retry(),               # 6h
                   _line_block_remaining(st.get("ip"), ts)) # → 13.3h  ← 病根
    elif verdict == "blind":
        wait = _line_transient_retry(1)
    else:
        wait = _line_transient_retry(st["fail_streak"])     # 指数退避，cap 600s
    st["next_probe_ts"] = ts + wait

# 生产 OK 回写 :2763 —— max 保留旧值，导致 OK 也无法恢复
st["next_probe_ts"] = max(float(st.get("next_probe_ts") or 0.0),
                          ts + _line_fresh_sec())

# 重启路径 :2961-2968
if block_left > 0:
    st["next_probe_ts"] = max(prev_next, now + block_left)
elif (same_ip and prev_next > now and last_verdict in (...)):
    st["next_probe_ts"] = prev_next
else:
    st["last_verdict"] = "unknown"
    st["next_probe_ts"] = now + _line_unknown_retry()      # 5s
```

**改法**：所有非 OK 结论一律走同一个固定节奏，不再读 `ban_until`；OK 分支保持 `ts + 120s`，不再 `max` 保留旧值。

```python
# 新增：探测节奏函数（失败后重探间隔，退避只防风暴，cap 60s）
def _line_probe_cadence(fail_streak: int) -> float:
    """失败后的重探间隔。退避只防风暴，上限硬钳在 60s。

    WHY 不让结论延长它：把 quota/blocked 的惩罚期写进重探间隔会让整条线
    停探数小时（实测 next_probe_ts 被推到次日 08:00，20 条线全部冻结 56.6 分钟、
    74 次换线全废）。惩罚期该管的是「这个地址能不能被选为出口」
    （_warp_ip_is_blocked），不是「还要不要观测」。
    """
    base = float(_warp().get("probe_transient_retry_sec", 120))
    cap = float(_warp().get("probe_cadence_max_sec", 60))    # 新键，默认 60s
    return min(base * (2 ** max(0, fail_streak - 1)), cap)
```

探测回写路径改为：

```python
if verdict == "OK":
    st["next_probe_ts"] = ts + _line_fresh_sec()          # 120s
else:
    st["next_probe_ts"] = ts + _line_probe_cadence(st["fail_streak"])
```

生产 OK 回写改为直接赋值（不再 `max`）：

```python
st["next_probe_ts"] = ts + _line_fresh_sec()              # 120s，OK 后必复探
```

重启路径改为：删掉前两个分支，重启后一律 5s 复探（第三个分支）。

理由：重启已经换了会话地址。`same_ip` 时保留长停探是为了"同址重启会立刻再烧一次配额"，但这正是**应该**发生的观测 —— 烧一格配额换"这条线到底还能不能用"的答案，比停探 13.3 小时强。

### P0 — 移除闸 3：台账判脏不再跳过模型探测

**位置**：`daemon.py:3991-3997`（`_warp_model_usable` 入口） + `daemon.py:3028-3034`（`_probe_one_line` 入口）

**现状**：

```python
# _warp_model_usable 入口
if _warp_ip_is_blocked(ip):
    _PROBE_LAST_VERDICT[tag] = "blocked"
    return False

# _probe_one_line
if _warp_ip_is_blocked(ip, now):
    _warp_line_record_observation(tag, ip, "blocked", ...)
    ...
    return
```

**改法**：删除这两处入口判脏。**模型探测必须真实发起**，拿到真实结论（OK/quota/err），再把结论写回 `_warp_line_record_observation` 由后续逻辑处理。台账判脏只用于**选线排序**（`_warp_ip_rank`），不再用于**探测准入**。

### P0 — 移除闸 6：结论缓存 TTL 不再跳过探测

**位置**：`daemon.py:657-678` + `config.py:425-427`

**现状**：

```python
def _probe_ttl(verdict: str) -> float:
    ...
    if verdict in ("quota", "flagged"):
        return float(w.get("probe_persistent_ttl_sec", 21600))   # 6h
```

**改法**：`quota`/`flagged` 的 TTL 从 21600 降到与 OK 同档（600s），或直接让 `_probe_cache_get` 对非 OK 结论只用于**选线排序**，不再用于**跳过探测**。

最小改法（不动缓存语义，只改取值）：

```python
def _probe_ttl(verdict: str) -> float:
    ...
    if verdict in ("quota", "flagged"):
        return float(w.get("probe_persistent_ttl_sec", 600))     # 6h → 10min
```

并同步 `config.py` 的默认值。**保留缓存的用途**（选线时避免重复烧配额），但不再让一条 6 小时前的结论阻止 10 分钟后的复探。

### P1 — 换线等待改为「触发探测并短等」

**位置**：`daemon.py:4736-4742`

**现状**：

```python
if not cands:
    deadline = now + switch_wait_sec          # 20s
    log("  · 暂无 120s 内 OK 的备线，等待探测器写入")
    cands = _warp_line_wait_fresh(..., deadline)
```

被动等探测器写表。若探测器停摆（如本次），必然等满 20s 后放弃。

**改法**：等之前先**主动催一次探测**，并把等待降到 3s：

```python
if not cands:
    _warp_line_force_probe_all()      # 新增：无视 next_probe_ts，立刻排一批探测
    deadline = now + float(_warp().get("switch_wait_sec", 3))   # 20 → 3
    ...
```

`_warp_line_force_probe_all()` 实现：把目标线的 `next_probe_ts` 置 0 并 `notify_all()`，让探测器下一轮立刻处理。

这样即使闸 1 因某种原因仍然生效，换线路径也有自救手段，不再被动等死。

### P2 — 文档漂移修正

`CLAUDE.md:638`：

```
warp.rate429_guard_sec: 86400    # 429 恢复期（24h）
```

实际 `config.py:325` / 运行值是 `60`。按文档排查会得出 24 小时的错误结论，而真凶是 `ban_until`。修正为：

```
warp.rate429_guard_sec: 60       # 429 兜底冷却（精确值经 ban_until 覆盖）
```

---

## 6. 改动清单汇总

| 优先级 | 位置 | 改动 | 消除的闸门 |
|---|---|---|---|
| **P0** | `daemon.py:2729-2742` + `:2763` | 非 OK 结论不再读 `ban_until`，统一固定节奏（cap 60s）；OK 分支直接赋值不再 `max` | 闸 1 |
| **P0** | `daemon.py:3028-3034` + `:3991-3997` | 删掉 `_warp_ip_is_blocked` 在探测入口的判脏跳过 | 闸 3 |
| **P0** | `daemon.py:657-663` + `config.py:425-427` | `probe_persistent_ttl_sec` 21600 → 600 | 闸 6 |
| **P0** | `config.py` | `probe_persistent_retry_sec` 21600 → 60（**6h 地板，独立于 ban_until**） | 闸 1 另一半 |
| **P1** | `daemon.py:2959-2968` | 重启后一律 5s 复探，删除 `block_left`/`same_ip` 分支 | 闸 1 连带 |
| **P1** | `daemon.py:4736` + 新增 `_warp_line_force_probe_all` | 换线前主动催探测；`switch_wait_sec` 20 → 3 | 被动等待 |
| **P2** | `CLAUDE.md:638` | `rate429_guard_sec` 86400 → 60 | 文档漂移 |

### ⚠ 容易被漏掉的一半：`probe_persistent_retry_sec` 的 6 小时地板

`wait = max(_line_persistent_retry(), ...)` 是**两个数取大**，只改一边不够：

```
probe_persistent_retry_sec = 21600 秒 = 6.0 小时   ← 地板（与 IP 无关）
_line_block_remaining(ip)  = ban_until - now      ← 精确封禁（19 条中 17 条 > 6h）
wait = max(两者)
```

**即使 `ban_until` 已过期（`block_left <= 0`），verdict=quota 仍停探 6 小时。** 只改 `daemon.py:2735-2742` 而不改 `probe_persistent_retry_sec`，停摆上限只从 13.3h 降到 6h —— 仍然远超可接受范围。

实测分布（quota/flagged/blocked 共 19 条）：

```
停探 ≤ 6h : 2 条   ← 这 2 条只受地板影响
停探 > 6h : 17 条  ← ban_until 额外推出去的部分
```

两者必须同时改：地板降到 60s，`ban_until` 不再进探测排程。

**保留不动**：闸 7（并发 32）、闸 8（信号量 32）、闸 5（速率 600/分钟）、闸 4（整批失明保护）、闸 2（`warming` 但需确保异常路径清除）。这些是资源保护，不是结论驱动。

---

## 7. 验证方法（改完后怎么证明有效）

### 7.1 直接观测探测节奏

```bash
# 每 10s 采样 last_probe_ts，应持续推进
for i in $(seq 5); do
  python3 -c "
import json,time
d=json.load(open('.warp_line_state.json'))['lines']
lp=max(v['last_probe_ts'] for v in d.values() if v['last_probe_ts'])
print(time.strftime('%H:%M:%S'), '最后探测距今 %.0fs' % (time.time()-lp))
"
  sleep 10
done
```

**通过判据**：`最后探测距今` 应始终 < 300s，不出现单调递增。

### 7.2 停摆检测（应长期为 0）

```bash
# next_probe_ts 超过 10 分钟的线数
python3 -c "
import json,time
d=json.load(open('.warp_line_state.json'))['lines']
now=time.time()
bad=[(t,v['next_probe_ts']-now) for t,v in d.items() if v['next_probe_ts']-now > 600]
print('停探超 10 分钟的线:', len(bad), '/', len(d))
for t,s in bad: print(' ', t, '%.1fh' % (s/3600))
"
```

**通过判据**：恒为 `0 / 20`。

### 7.3 换线不再空等

```bash
journalctl -u auto-switch-ip --since "30 min ago" -o cat | grep -c '等待超时仍无新鲜备线'
```

**通过判据**：应显著下降；且出现 `↻ 重启 <tag>` 行（说明进入了重启分支而非纯等待）。

### 7.4 回归测试

新增用例（`test_warp_refresh.py`）：

```python
def test_quota_verdict_does_not_extend_next_probe_beyond_cadence():
    """quota 结论不得让 next_probe_ts 超过探测节奏上限（中文断言）。

    回归 2026-10-27：20 条线的 next_probe_ts 被 ban_until 推到次日 08:00，
    探测器停摆 56.6 分钟，74 次换线全部空等 20s 后放弃。
    """
    # 构造：ip 有 ban_until = now + 13h，探测结论 quota
    # 断言：next_probe_ts - now <= 60
    assert next_probe_ts - now <= 60, "quota 结论不应让探测停摆超过节奏上限"


def test_all_lines_quota_still_probed_within_cadence():
    """全部线都被判 quota 时，探测器仍须按节奏复探（中文断言）。"""
    assert max(next_probe_ts - now for _ in all_lines) <= 60, \
        "全部线结论不好也必须继续探测"


def test_ok_verdict_does_not_preserve_old_next_probe_ts():
    """_warp_line_note_ok 不得保留旧的长停探（中文断言）。

    回归：某线 quota 推到 08:00，11:58 产生 OK，next_probe_ts 应回落至 12:00 而非保留 08:00。
    """
    assert next_probe_ts - ts <= 120, "OK 结论必须让探测在 120s 内复探"
```

---

## 8. 这条设计原则的一般化

本次缺陷的形态可以复用到其它地方检查：

| 反模式 | 本次实例 | 一般判据 |
|---|---|---|
| **观测结果污染观测排程** | `quota` → `next_probe_ts = 13.3h` | 一个"下次什么时候再看"的字段，是否被"上次看到了什么"影响？ |
| **状态与决策耦合** | `ban_until` 同时决定「能不能选为出口」和「要不要观测」 | 同一份状态是否驱动了两个不同的问题？ |
| **被动等待代替主动触发** | 换线只等探测器写表，不催 | 依赖外部生产者的路径，是否有超时自救？ |
| **缓存保留旧证据** | `next_probe_ts = max(old, ts+120)` 导致 OK 也无法恢复 | 缓存/最大值合并是否把新证据压在旧证据之下？ |

第四条尤其值得记：**OK 结论本应让探测恢复，但因为 `max` 保留了旧的 quota 排程，反而把新证据覆盖了。** 这等于说"上次看见坏的，这次看见好的，但仍按坏的排程"。

第三条：**74 次换线全在等一个不会来的事件**。等待方和被等待方之间没有任何健康检查 —— 探测器是活的（线程在、状态文件在写），但生产是停的。"进程活着"与"在工作"是两件事，靠日志空转无法区分。

---

## 9. 未解决

1. **`ban_until` 的 08:00 来源未直接取证**。我确认了它是绝对对齐时刻（417 条记录跨度恰好 24h，而标记时刻跨度 28.9h），推断来自上游 `Retry-After` 的 HTTP date（指向次日 08:00 配额重置）。生产库不落响应头，只有探测路径能看到，本轮未抓包验证。

2. **`_warp_ip_is_blocked` 在选线路径仍生效**。本文建议只移除探测入口的判脏；选线路径 `_warp_ip_rank` / `_ip_disqualified` 仍用它做排序/否决。这是否会导致"探测通过但选线拒选"的矛盾，需要实测观察。

3. **固定节奏的配额成本未量化**。20 条线 × 每分钟 = 1200 次/小时模型层探测，受 `probe_max_per_min=600` 压制到 600/分钟。这意味着 20 条线轮一遍约需 20 分钟 —— 是否够快，取决于故障恢复的时间要求，本轮未评估。

4. **`_warp_probe_blind` 的 120s 失明窗口**。若探测通道真故障（整批 unreachable），120s 内不探是合理的保护；但若是误判，会是另一道停探。建议改为配置化（`probe_blind_window_sec`）并默认 30s。

5. **`warming` 标志是否会泄漏**。重启路径里多处异常捕获都会清 `warming=False`，但若线程在 `got_slot` 后崩溃未进入 finally，可能残留。需审计所有 `return False` 路径是否都清了它。