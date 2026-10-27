# 三方五维复审 —— 已落地改动 vs PRD 承诺

> 审查对象：`docs/review-5d-20261027.md` v5（下称 PRD）声明的 FR / M / 风险 与**当前运行代码**的差异
> 取证方式：读代码 + 跑运行探针 + cli 计数，全部命令与输出附在正文
> 日期：2026-10-28 05:10 CST
> 状态：**只审查，未改代码**
> 立场：PRD 是 2026-10-27 13:5x 定的；此后并行 session 又改了 `config.py` 与 `daemon.py`。
> 本审查不评判那两个 session 的取舍，只回答一件事：**PRD 写的验收条件，现在的系统达成了几条。**

---

## TL;DR

**PRD 有 14 条 FR，达成 8 条（其中 3 条已随配置改动失效），3 条部分达成，3 条未达成/被推翻。**

| 判据 | 结果 |
|---|---|
| 达成 | FR-1、FR-2、FR-3、FR-4、FR-5、FR-6、FR-9、FR-10、FR-11 |
| 达成但**当前配置下形同虚设** | FR-13（cadence=0）、FR-14（cap=0）、NFR-1（rate=0） |
| 部分达成 | FR-7（判据被简化）、M7（一条不变量用例被改名） |
| 未达成 | FR-12（文档漂移仍在） |

**最严重的一处**：PRD 把 FR-14 定为「FR-13 的必要配套，不是额外要求」，并列出
「60s 节奏叠加 blocked-即-重启 = 每线每小时 60 次重启」的推导。当前生产实测
**单线 294 次/小时**（WARP-WG-13），全池 2935 次/小时 —— **是 PRD 预警值的 4.9 倍，
是 PRD 定的 M9 目标（≤20）的 14.7 倍。** FR-13 + FR-14 这一对已经一起被配置关掉了。

---

## 1. 逐条比对

### 1.1 达成

| FR | PRD 要求 | 实际 | 证据 |
|---|---|---|---|
| FR-1 | 非 OK 结论的重探间隔不读 `ban_until` | **达成** | `grep -i ban_until \| grep probe` → 无命中；`_warp_line_needs_probe` 体内无 `ban_until` |
| FR-2 | `probe_persistent_retry_sec` 不进探测间隔计算路径 | **达成** | 该键已从 `config.py` 删除，`grep` 全仓 0 处 |
| FR-3 | 生产 OK 回写直接赋值，不用 `max` 保留旧排程 | **达成** | `_warp_line_note_ok` 末行 `st["next_probe_ts"] = 0` |
| FR-4 | `quota`/`flagged` 缓存 TTL ≤ 600s | **达成** | 运行探针：`_probe_ttl(quota)=600.0`、`(flagged)=600.0` |
| FR-5 | 无候选时先催探测再短等 | **达成** | `daemon.py:4802` `notify_all()` + `:4804` 日志「已催探测器」 |
| FR-6 | `switch_wait_sec` ≤ 3s | **达成** | 运行值 `3` |
| FR-9 | 同 `proxyUrl` 的池 id 可区分 | **达成（改法不同）** | `identifyFixedEgress` 仍 `hashEgress("proxy", proxyUrl)`；PRD 的 M5 要求「去重数 = 3」**不可能达成**，见 §2.1 |
| FR-10 | 同一物理出口不互为备选 | **达成** | `selectAlternateProxy` 按 `egress.id` 排除；id 即 url 哈希 → 同 url 天然排除 |
| FR-11 | 不同 url 仍能选为备选 | **达成** | `egress.test.js` 与 `egress-pool-identity.test.js` 共 24 条 vitest 全绿 |

### 1.2 达成但当前配置下形同虚设

#### FR-13 / M8 —— 节奏机制在位，但取值为 0

PRD 的 M8 要求「同一线 600s 内被判定可探 ≤ 10 次」，FR-13 要求 `cadence ≤ 60s`。

```
$ python3 -c "import config; print(config.CFG['warp']['line_probe_cadence_sec'])"
0

$ 运行探针（last_probe_ts=1000, verdict='quota'）
needs_probe(now+1)  = (True, 1000.0)   ← 刚探完 1 秒就放行
needs_probe(now+61) = (True, 1000.0)
```

机制本身正确（`due = last_probe_ts + cadence`，不读 verdict / ban_until / next_probe_ts），
但 `cadence=0` 时 `due = last_probe_ts`，等价于**无节奏**。
配置注释给出的理由是「36 条线 × 60s 把吞吐锁死在 36 条/分钟，而单次探测只要 1.9~5.7s」。

**这不是笔误，是有理由的取舍 —— 但它把 PRD 的 M8 判据整个作废了。**
PRD §5.1 明确写过「只做 FR-13 = 把现在的随机风暴改成 60s 节拍器」，
现在连节拍器也没有了。

#### FR-14 / M9 —— cap 从 20 改成 0，M9 目标被主动放弃

```
$ python3 -c "import config; print(config.CFG['warp']['restart_max_per_hour_per_line'])"
0
```

`_warp_line_restart_allowed` 的 `cap <= 0` 分支直接 `return True`。
配置注释：「0 = 不设上限（2026-10-28 按主人要求去掉）。它挡的从来不是观测，
而是"把一个确定不能用的地址换掉"这个动作本身」。

**实测代价**（近 1 小时，`journalctl -u auto-switch-ip`）：

| tag | 重启次数/h | PRD M9 目标 |
|---|---|---|
| WARP-WG-13 | **294** | ≤ 20 |
| WARP-WG-3 | 230 | ≤ 20 |
| WARP-WG-15 | 226 | ≤ 20 |
| WARP-WG-11 | 203 | ≤ 20 |
| WARP-WG-10 | 199 | ≤ 20 |
| 全池合计 | **2935** | — |

PRD §5.1 的推演是「60s 节奏 = 每线每小时最多 60 次」，那是 **cadence=60 时**的天花板。
cadence=0 时天花板由 `restart_cooldown_sec=5` 决定 = 720 次/h/线，实测 294 已达其 41%。

#### NFR-1 —— 安全网三处只剩一处

PRD NFR-1 要求「`probe_max_per_min=600`、`probe_concurrency=32`、
`model_probe_concurrency=32` 三处不变」。实际：

```
probe_max_per_min      = 0     ← PRD 要求 600
probe_concurrency      = 32    ✓
model_probe_concurrency = 32   ✓
```

配置注释：「0 = 不限。旧值 30/600 是纯自我设限」。这条改动与 ADR-6「常量节拍器是唯一节奏」
的前提冲突：节拍器取 0 之后，**PRD 设计的双保险变成了单保险**。

### 1.3 部分达成

#### FR-7 —— 判据被简化，丢掉了一个分支

PRD §5.1 FR-7 与 §5.3 R-2 给的是**双判据**：

```python
# PRD 写法
if streak >= 3 and (not _port_open(member["port"]) or streak >= 10):
```

```python
# 实际写法（daemon.py:3078）
streak = int(_warp_line_state(tag).get("fail_streak") or 0)
if (tag != active and streak >= _UNKNOWN_RESTART_STREAK      # = 10
        and not _port_open(member["port"])):
```

两处差异：

1. **阈值 3 → 10**：PRD 的「3 次且端口不通」这一支被删掉，端口不通也要等满 10 次。
2. **`or streak >= 10` 变成了 `and not _port_open`**：PRD 特意写了「这个或条件不可省，
   `_port_open` 在 usque 进程半死时可能仍返回 True（进程在监听，上游已死）。
   只看端口会永远凑不满条件，线仍然冻结」—— 实际实现**正好把这条保命分支删了**。

后果：`_port_open` 恒为 True 的半死隧道，即使 streak 涨到 100 也不重启。
这与 PRD 警告的场景完全一致。不过 PRD 也承认 `_port_open` 判据存疑（A-4），
而实现改用了 `fail_streak` 累积（NFR-5 的 `unknown_streak` 字段并未引入）——
**这是换了一套判据，不是照着 PRD 做的**。

#### M7 —— 六条不变量用例有一条被改名

PRD §9.1 列为「必须保持绿」的六条：

```
test_restart_same_ip_keeps_quota_park          ← 不存在
test_restart_to_another_banned_ip_parks_until_unban   ✓
test_blocked_ip_probe_restarts_without_burning_model_quota  ✓
test_dirty_ip_is_never_probed                  ✓
test_dirty_ip_shared_by_two_lines_is_not_probed ✓
test_probe_unknown_ip_does_not_restart         ✓
```

实际存在的是 `test_restart_same_ip_reprobes_on_cadence`（`test_warp_prober.py:124`）。
改名本身有正当理由：PRD 要求「同址重启后停在 quota 惩罚期」，而 FR-1/FR-2 之后
惩罚期不再进探测路径 —— 旧语义已被 PRD 自己推翻。**但这意味着 C-2「不得推翻 6 条带 WHY 用例」
在字面上被违反了**，PRD 没有登记这次改名。

### 1.4 未达成

#### FR-12 —— 文档漂移仍在

PRD 要求 `CLAUDE.md` 的 `rate429_guard_sec` 与 `proactive_mode` 与运行值一致。
`config.py:262` 是 `proactive_mode: "fixed"`（带注释「出现503的场景，导致requests失效，临时切到fixed」）。
需要核对 `CLAUDE.md` 是否同步 —— PRD 自己把这条列为「无测试，靠人工核对」（R-9）。

---

## 2. 三方视角

### 2.1 架构视角（D1 正确性 / D5 一致性）

**A-1｜PRD 的 M5 判据与 ADR-2 自相矛盾（PRD 侧缺陷）**

M5 写「三个同 url 活跃池的 egress id 去重数：基线 1 → 目标 3」，但 ADR-2 选的是方案 A
（`selectAlternateProxy` 按 `proxyUrl` 去重，id 组成不变）。两者不可能同时成立：

- 方案 A 下 id 仍是 `hash(proxyUrl)` → 三个同 url 池去重后**恒为 1**，M5 永远达不成。
- 若 M5 要达成（id 去重数 = 3），就必须选方案 B —— 而 PRD 明确否决了 B（R-8 缓存键全失效）。

**实际落地走的是 A，所以 M5 是 PRD 写错的一条指标，不是实现漏做。**
PRD §5.4 的结论（「修后能换线但换到同一物理出口」）也支持 A：
真正的修法是「同 url 不互为备选」（FR-10），而不是「id 要不同」。

**A-2｜cadence=0 与 ADR-6 的决策前提冲突**

ADR-6 整篇的论证是「拆掉结论驱动的闸，**装回一个与结论无关的闸**」，
并明确否决选项 B（「完全无节奏，每轮都探，靠 `probe_max_per_min` 兜底」）。
当前配置 `cadence=0` + `probe_max_per_min=0` **恰好是 ADR-6 否决的那个组合**，
且比它更激进（连 600/min 的兜底也没有了）。

配置注释给的理由（吞吐锁死 36 条/分钟）是**新的证据**，PRD 定稿时没有。
但这属于**推翻 ADR-6，需要新的 ADR 登记**，不能靠改一行配置静默生效。

**A-3｜FR-7 的实现与 PRD 给的代码不同构**

PRD 的 `unknown_streak` 字段没有引入（`grep unknown_streak daemon.py` → 0 处），
实现复用了 `fail_streak`。两者语义不同：`fail_streak` 在**任何**非 OK 结论时递增
（`_warp_line_record_observation` 的 else 分支），不只在 unknown 时。
所以 `streak >= 10` 实际是「连续 10 次任何失败」，比 PRD 的「连续 10 次拿不到 IP」宽得多 ——
一条 quota 线也会累积到 10。这与 R-5（「`unknown_streak` 计数不归零」）是同类问题，
只是换了个字段名，风险没消失。

### 2.2 开发视角（D3 可维护性）

**D-1｜`_line_probe_cadence` 被定义了两次**

```
$ grep -n "^def _line_probe_cadence" daemon.py
2672:def _line_probe_cadence() -> float:
2703:def _line_probe_cadence() -> float:
```

两处函数体**完全相同**（`return float(_warp().get("line_probe_cadence_sec", 60))`）。
这是并行 session 合并时的重复落笔。虽然 Python 后定义覆盖前定义、行为无差别，
但它制造了一个未来陷阱：**改其中一个不会生效**，而后来者不知道改哪个。

**D-2｜`next_probe_ts` 的语义降级没有在状态表里注明**

PRD ADR-6 §「`next_probe_ts` 保留，但降级为记录字段」说得清楚，
但 `_LINE_DEFAULTS`（`daemon.py:2565`）对它没有任何注释，而它仍被 15 处写入。
新读者看到 15 个写入点会自然地以为它还在起调度作用 —— 这正是 PRD
自己点出的「最容易被后来者弄反的地方」。

**D-3｜测试契约已被改成「跟着配置走」**

`test_warp_prober_gates.py` 现在用 `_cadence()` / 临时改 `CFG` 的方式，
在 0 和 60 两种取值下验证语义。这个做法本身是对的（配置是运维旋钮，不该让契约用例变红），
但它掩盖了一件事：**M8「≤10 次/600s」这条验收指标现在没有任何用例在守。**
PRD 把 M8 列为成功指标，实际生产值 600 次/600s（cadence=0），无人报警。

### 2.3 QA 视角（D6 可测试性 / D1 测试层）

**Q-1｜M 指标清单与实际验证能力脱节**

PRD §3 列了 M1–M9 + O1，其中：

| 指标 | 现在能否自动判定 | 说明 |
|---|---|---|
| M1（停探超 600s 的线数 = 0） | **能** | 实测 0/36，`.warp_line_state.json` 可读 |
| M2（换线不再空等） | 能 | journalctl 计数 |
| M3（switch_wait ≤3） | 能 | 运行值 3 |
| M4（复检间隔 ≤60s） | **不能**，cadence=0 下无意义 | — |
| M5（id 去重数 = 3） | **不可能达成**（见 A-1） | PRD 写错 |
| M6（TTL ≤600） | 能 | 600 |
| M7（六条不变量绿） | **部分**，一条已改名 | 见 1.3 |
| M8（600s 内 ≤10 次） | **不能**，cadence=0 下必然失败 | 实测 600 |
| M9（单线 ≤20 次/h） | **不能**，实测 294 | 见 1.2 |

**九条指标里 4 条（M4/M5/M8/M9）在当前配置下已失效或不可能达成**，
但没有任何机制会因此报警 —— 因为 M4/M5/M8/M9 都只是文档里的数字，没有对应的看板或断言。

**Q-2｜单元测试全绿不等于整链可用（PRD O-5 承认的缺口仍在）**

`pytest` 372 条全绿、`vitest` 24 条全绿。但生产侧 2935 次重启/h、
1454 次模型层探测/h，说明**整链在跑，但结果不健康**。
PRD O-5 说「无集成测试」是缺口，本轮没有补。

---

## 3. 与 PRD 承诺的直接冲突清单

按「谁会因此踩坑」排序，不按严重度。

| # | PRD 承诺 | 现状 | 冲突性质 |
|---|---|---|---|
| 1 | FR-14「FR-13 的必要配套，不是额外要求」；M9 单线 ≤20 次/h | 单线 294 次/h，配置 cap=0 | **配置推翻了需求**，无 ADR 登记 |
| 2 | ADR-6「必须装回一个与结论无关的闸」；否决「完全无节奏 + 靠 probe_max_per_min 兜底」 | cadence=0 且 probe_max_per_min=0 = 被否决的选项 B 加强版 | **决策被静默推翻** |
| 3 | C-2「不得推翻 6 条带 WHY 的既有用例」 | `test_restart_same_ip_keeps_quota_park` → `..._reprobes_on_cadence` | 改名有正当理由，但未登记 |
| 4 | FR-7 双判据，且「`or streak >= 10` 不可省」 | 正好删掉了这一支 | 保命分支缺失 |
| 5 | FR-12 文档与运行值一致 | `proactive_mode` 已改，`CLAUDE.md` 未核对 | 未验证 |
| 6 | M5「三池 id 去重数 = 3」 | 不可能达成（方案 A 下 id 恒同） | **PRD 指标写错** |

---

## 4. 建议（不是要求，按可执行性排序）

1. **给 cadence=0 / cap=0 / rate=0 补一份 ADR-7。** 它们推翻了 ADR-6，
   而 ADR-6 是 PRD 里论证最完整的一篇。现在只剩 config 注释里的两行理由，
   后人无法判断这是「新证据推翻旧决策」还是「有人顺手改了」。
2. **把 M4/M5/M8/M9 从「成功指标」移出或重写。** 留着不改，
   下次审查还会拿它们当验收条件，然后得出「全部未达成」的假结论。
3. **FR-7 的 `or streak >= 10` 分支要么补回，要么在注释里写明为什么不要它。**
   PRD 特意论证过「只看端口会永远凑不满条件」，删掉它需要给出反驳。
4. **`_line_probe_cadence` 删一处。** 两处定义相同，删哪个都行，但必须删一个。
5. **FR-12 核对一次 `CLAUDE.md`。** 这是 PRD 唯一承认「无测试，接受」的风险（R-9）。

---

## 附录 — 取证命令

```bash
# 配置运行值
cd ~/dkk-projects/auto-switch-ip
python3 -c "import config; w=config.CFG['warp']; print({k:w.get(k) for k in ['switch_wait_sec','line_probe_cadence_sec','probe_persistent_ttl_sec','restart_max_per_hour_per_line','probe_max_per_min']})"

# FR-4 / FR-13 运行探针
python3 -c "import daemon; print([(v, daemon._probe_ttl(v)) for v in ['quota','flagged','blocked']])"

# M9 — 单线每小时重启次数
sudo journalctl -u auto-switch-ip --since '1 hour ago' -o cat \
  | grep '↻ 重启' | sed -E 's/.*重启 ([A-Za-z0-9-]+)（.*/\1/' \
  | sort | uniq -c | sort -rn | head

# M7 — 六条不变量用例是否存在
for t in test_restart_same_ip_keeps_quota_park test_restart_to_another_banned_ip_parks_until_unban \
         test_blocked_ip_probe_restarts_without_burning_model_quota test_dirty_ip_is_never_probed \
         test_dirty_ip_shared_by_two_lines_is_not_probed test_probe_unknown_ip_does_not_restart; do
  grep -rq "def $t" test_*.py && echo "存在: $t" || echo "不存在: $t"
done

# D-1 — 重复定义
grep -n "^def _line_probe_cadence" daemon.py

# FR-7 — 实际判据
sed -n '3070,3085p' daemon.py
```
