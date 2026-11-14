# 按出口 IP 的会话池设计（15×10，IP 变重建）

> 版本：2026-11-13 18:45（CST）
> 范围：WSL 143 `~/dkk-projects/auto-switch-ip` + `~/.9router`
> 状态：**已实施并线上验证**；9router `BUILD_ID=AWEbSHrMs3I1sQzxc1O0u`，asip active
> 关联：`docs/opencode-429-limit-model-20261112.md`、`docs/established-facts.md` 3b

---

## 1. 背景与问题

2026-11-13 白天，143 的 asip 探测出现「几乎 0 OK」。根因不是线路不通，而是
**会话身份被全局烧穿**：9router 与探针都在用同一组「每天固定 15 个」的 session，
所有出口 IP 共用这 15 个 ID。

上游 opencode 免费层的限制可以拆成两层：

1. **每出口 IP 的 distinct session 上限**（约 40 个）；
2. **每个 session 自己的请求额度**。

旧方案那 15 个固定 ID 被上游标记后，换 IP 也一起 429；而每个新 IP 又只带同一组
旧 ID，于是「换 IP 拿新配额」失效。

### 1.1 A/B 证据（2026-11-13）

- FRA-27 `2a09:bac5:2a92:246e::3a1:33`、SLC-11 `2a09:bac1:76c0:7d0::8f:5e`：
  - 常规 session → `quota`；
  - 同 IP 换成 143 `_daily_sessions()[6]` → `quota`；
  - 午夜锚 + 随机后缀 → `OK`。
- 真实 daily[6] = `ses_e2f833bffff8DTpZi3tsuig4sX`；
  前缀必须用毫秒 `int(anchor*1000)*0x1000`。
- 结论：被拒的是那 15 个精确 ID 整串，不是 IP 本身；但 IP 级 14h quota 仍然存在。

---

## 2. 目标与非目标

### 2.1 目标

- 每个出口 IP 独立一组 15 个随机会话；IP 变立即重建；绝不跨 IP 共用。
- IP 内每 10 次切下一个；15×10=150 次后**回绕到第 1 个**（均分循环）。
- 429 重试立即切下一个 session；换出口时按**目标出口**重新取池。
- 9router 与 asip 探针使用同一套语义。

### 2.2 非目标

- 不试图解决 IP 本身的 14h quota；那是换线/选线的职责。
- 不把 distinct/IP 提高到 40；15 是留余量。
- 不改每日固定池的锚点生成规则（仅作为历史对照保留）。

---

## 3. 约束与判据

| # | 约束/判据 | 落点 |
|---|---|---|
| C1 | distinct/IP ≤ 15 < 40 | `SESSION_POOL_SIZE=15` |
| C2 | 同 IP 重访复用同一组，不重新生成 | `_SESSION_POOLS[key]` / `sessionPools` |
| C3 | IP 变 → 新 15 个，与旧组不相交 | `_session_pool(ip)` / `sessionPoolKey(ip)` |
| C4 | IP 内每 10 次切下一个 | `SESSION_POOL_SWITCH_EVERY=10` |
| C5 | 150 次后回绕到第 1 个 | `idx=(idx+1)%15` |
| C6 | 429 重试立即切 | `rotate=True` / `sessionRotate:true` |
| C7 | 换出口按目标出口取池 | `attemptCredentials` / `egressIp` |
| C8 | slot-7 无 /128 时用池节点 tag | `asipEgressForPort().tag` |
| C9 | 未知出口不共用 `"?"` | `port:<port>` |
| C10 | 池有上界 | 8192；9router LRU，asip 按 `ts` 淘汰 |

---

## 4. 设计

### 4.1 asip 探针 `probe_warp.py`

- 常量：`SESSION_POOL_SIZE=15`、`SESSION_POOL_SWITCH_EVERY=10`、
  `SESSION_POOL_MAX_ENTRIES=8192`（`probe_warp.py:197-199`）。
- `_SESSION_POOLS`：按出口 IP 的 dict；`_SESSION_POOL_LOCK` 保护。
- `_mint_pool()`：生成 15 个 `ses_<12hex><14base62>`，用
  `secrets.SystemRandom().shuffle` 打乱（`:212`）。
- `_session_pool(ip)`（`:217`）：没有就新建；超过 8192 按 `ts` 淘汰最旧。
- `_pick_session(ip, rotate)`（`:232`）：
  - `key = (ip or "").strip()`；`"?"` 归一为 `"-"`；
  - `rotate=True` 或 `used >= 10` → `idx=(idx+1)%len(sessions)`，`used=0`；
  - `used += 1`；返回 `sessions[idx]`（`:250-254`）。
- `daemon.py:7800`、`daemon.py:12419` 探测时传 `ip=ip`，让探针按真实出口 IP 分池。
- `probe()` 的 `egress_ip()` 失败返回 `"?"` 时，按 `port:<port>` 分组，避免所有
  未知出口共用一组。

### 4.2 9router `opencode.js`

- 常量：`SESSION_POOL_SIZE=15`、`SESSION_POOL_SWITCH_EVERY=10`、
  `SESSION_POOL_MAX_KEYS=8192`（`opencode.js:130-132`）。
- `sessionPools`：`Map<key, {list, idx, used, lastUsed}>`。
- `mintSessionPool()`（`:144`）：15 个 `generateSessionId()`，Fisher-Yates 打乱。
- `evictSessionPool()`（`:153`）：超过 8192 删 Map 头部最冷池。
- `nextSessionInPool(key, {rotate})`（`:161`）：
  - 取池；`lastUsed=Date.now()`；
  - `sessionPools.delete(key); sessionPools.set(key, pool)` → 真正 LRU（`:171-172`）；
  - `rotate || used >= 10` → `idx=(idx+1)%15`，`used=0`（`:173-175`）；
  - `used += 1`；`OPENCODE_SESSION_TRACE=1` 时打印 `[SESSION] key= idx= used= sid=`
    （`:179-181`）。
- `sessionPoolKey(egressIp, egressId)`：`ip:<ip>` / `eg:<id>` / `eg:default`。
- `pickSessionForEgress(egressIp, egressId, opts)`（`:185`）；
  `nextSessionInPreparedPool(prepared)`（`:189`）用于同出口重试。
- 调用点：
  - 正常请求：`prepareRequestCredentials({ egressId, egressIp })`；
  - 主 429 换线循环：按目标出口重新构造
    `attemptCredentials = prepareRequestCredentials({ egressId: targetProxyUrl || egress?.id,
    egressIp: targetEgress || targetNode, sessionRotate: true })`，并写回
    `args.usedCredentials.value`；换新出口时 `requestEgressSince=null`；
  - 本地 cooldown 路径：`egressIp = rotated.switchToEgress || rotated.switchToNode ||
    rotated.activeEgress || rotated.activeTag || ""`；
  - 同出口直重试：`egressIp = retryEgress.ip ||
    (retryEgress.kind === "proxy" ? requestLine.tag : "")`。
- **slot-7**：`requestLine.egress || requestLine.tag` 作为池身份；换节点时用
  `switchToNode` / `activeTag`；没有 /128 也能重建池。

### 4.3 `opencode-zen.js`

- 引入 `asipEgressForPort`（`opencode-zen.js:4`），`execute` 里在
  `proxyUrl && !egressIp` 时补算 `egressIp/tag`。
- 池逻辑与 `opencode.js` 一致：`SESSION_POOL_SIZE=15`、`SWITCH_EVERY=10`、
  `MAX_KEYS=8192`（`:26-28`）；`nextSessionInPool`（`:48`）同样 LRU + 回绕；
  trace 前缀 `[SESSION-ZEN]`（`:67-69`）。

### 4.4 `asipRotate.js`

- `PORT_LINE_TTL_MS=30000`（`:450`）：端口 → 出口地址的缓存 TTL。
- `refreshSlotEgress()`（`:485`）：拉 `/slots`，按端口建 `byPort`；`SLOT_SNAPSHOT_TTL_MS`
  （5s）内复用快照。
- `asipEgressForPort(proxyUrl)`（`:527`）：返回 `{egress, tag, since}`；
  - warp 槽：`egress` 为真实 `/128`，`tag` 为线路 tag；
  - slot-7：`egress:null`，`tag` 为池节点（如
    `upoz.cn.greatgameupload.org:28065-any`）；
  - 查不到：`{egress:null, tag:null, since:null}`，调用方按未知出口处理。

---

## 5. 行为矩阵

| 场景 | 结果 |
|---|---|
| 同一 IP 连续 10 次 | 同一个 session |
| 第 11 次 | 切到组内下一个 |
| 第 150 次 | 组内第 15 个，第 10 次使用 |
| 第 151 次 | **回绕到组内第 1 个**（不重新生成） |
| 429 重试 | `rotate` 立即切下一个（不等 10 次） |
| IP 变化 | 新建 15 个，旧组丢弃；不跨 IP |
| slot-7 | 用池节点 tag 作池身份；tag 变即换池 |
| 未知出口 | `port:<port>`；不共用 `"?"` |
| 池超过 8192 | 9router LRU 淘汰；asip 按 `ts` 淘汰最旧 |

---

## 6. 验证

### 6.1 单测

- 9router：5 个文件 **69 passed**
  （`opencode-session` 30、`opencode-egress` 17、`opencode-429-resession-replay` 11、
  `opencode-zen-free-tier` 6、`asip-port-egress` 5）。
  - 新增「第 151 次回绕到第 1 个（15×10 均分循环）」。
- asip：`test_probe_warp.py` **14 passed**
  - `test_session_pool_15_per_ip_and_regenerate_on_ip_change`；
  - 新增 `test_session_pool_wraps_to_first_after_15_sessions`。

### 6.2 线上 trace（临时 `OPENCODE_SESSION_TRACE=1`）

- 40 picks / 9 keys；keys 均为真实 egress IP；
- **0 个 session 跨 key**；
- `used` 全部在 1..10；
- 某个 key 7 picks / 5 sessions → 429 重试的 `sessionRotate` 生效；
- trace drop-in 已删除；18:22 后 `[SESSION]` = 0。

### 6.3 线上结果

- `asipEgressForPort`：
  - `7890` → `{egress:"2a09:bac1:76c0:1a98::c:3b0", tag:"WARP-H2-IAD-90"}`
  - `7896` → `{egress:"2a09:bac5:624a:1846::26b:74", tag:"WARP-H2-LAX-129"}`
  - `7897` → `{egress:null, tag:"upoz.cn.greatgameupload.org:28065-any"}`
- 9router：
  - 18:18:42–18:22：61 success / 3 ERROR 429；
  - 18:22 起：42 success / 5 ERROR 429（约 89%）；
  - DB since 10:10Z：188 success / 14 error（约 93%）。
- asip gate：6/6 常规闸可用，slot-7 待命；daemon 12min 107 OK / 652 quota。
- 429 体是 `FreeUsageLimitError` + `Retry-After ~14h`，属于 **IP 级 quota**，
  不是 session 跨 IP 复用。

---

## 7. 已知边界

- **IP 级 quota 仍在**：会话池只解决 session 级全局烧穿，不能解决 IP 本身被上游
  14h quota；这部分交给换线/选线。
- **slot-7 的 tag**：只在池节点轮换时变；同一节点上游自己换 IP 不会触发重建
  （`/slots` 不暴露该 IP）。
- **30s TTL**：`asipEgressForPort` 的端口→出口缓存 30s；IP 切换后池键最多滞后
  约 30s；429 重试路径用 `switchToEgress/switchToNode` 兜底。
- **150 次回绕是用户确认的设计**：IP 长时间不变且请求量 >150 时，会复用已经
  用过 10 次的 session；这是均分循环，不是 bug。distinct/IP 仍恒 ≤15，不重新 mint。
- **9router 的 429 仍有约 7–11%**：主要是目标 IP 的 14h quota / 线路退役间隙，
  不是会话池回绕导致。

---

## 8. 变更与回滚

- 生产代码：
  - `~/dkk-projects/auto-switch-ip/probe_warp.py`；
  - `~/.9router/open-sse/executors/opencode.js`；
  - `~/.9router/open-sse/executors/opencode-zen.js`；
  - `~/.9router/open-sse/utils/asipRotate.js`。
- 测试/文档：`test_probe_warp.py`、`opencode-session.test.js`、
  `docs/opencode-429-limit-model-20261112.md`、`docs/established-facts.md`。
- 9router 改完必须 `npm run build` 后 restart；asip 重启加载 `probe_warp.py`。
- 备份：
  - 9router 源码：`.bak-20261113-180649`、`.bak-20261113-181137`；
  - 9router 测试：`.bak-20261113-180911`、`.bak-20261113-183627`；
  - asip 探针：`probe_warp.py.bak-20261113-180659`；
  - asip 测试：`test_probe_warp.py.bak-20261113-183627`；
  - 文档：`.bak-20261113-182422`、`.bak-20261113-183627`。
- 回滚：恢复对应备份 → 9router rebuild/restart → asip restart；
  `postdeploy_gate.sh strict` 必须 MATCH。

---

## 9. 参考

- `docs/opencode-429-limit-model-20261112.md` §五
- `docs/established-facts.md` 3b
- `docs/cf-repair-and-per-ip-usage-20261112.md`
