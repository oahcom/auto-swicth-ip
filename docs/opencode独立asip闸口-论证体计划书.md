# opencode 走独立 asip 闸口 — 论证体计划书

日期：2026-11-14　状态：**待对齐，未动手**
前置：本文件是论证体，不是任务清单。每条修改都必须写清「现状如何 → 为何影响 → 怎么改 → 为何能解决」。

---

## 〇、一句话结论

把 opencode CLI 的模型请求从「经 9router 中转」改为「原生直连 `api.opencode.ai` 的 `deepseek-v4.1-flash-free`」，
但出口仍走 **一个专属的 asip 闸口**（独立 sing-box + 独立选择器 + 独立轮换）。
当 opencode 撞到免费额度 429 时，**ccs** 监测到它 → 调 rotate 新增的「换 opencode 闸口」入口 → 闸口换 IP → ccs 唤醒该会话重发被中断的那次请求。
所有 opencode 会话共享这一个闸口。

---

## 一、实证基线（全部本轮实测，出处可溯）

| # | 事实 | 出处 |
|---|---|---|
| 1 | asip = `/home/administrator/dkk-projects/auto-switch-ip`（`daemon.py` 330KB + `sing-box` 57MB + `config.py`） | `ls` / `ps` |
| 2 | 槽位模型：`consumer_slots: 6` + `proxy_gate: True` → slot-1..6（WARP 线）+ **slot-7**（`kind="proxy"`，第七闸口/代理池兜底，端口 7897、Clash API 9097、selector `proxy`） | `config.py:160/170/1224-1290` |
| 3 | 每个槽位 = **独立 sing-box 进程**、独立端口、独立连接池、独立 selector；「9router 连不同端口 = 不同 sing-box = 不同线，天然隔离」 | `config.py:1224-1228` docstring |
| 4 | rotate = **HTTP over Unix Socket**（`run/rotate.sock` + `run/api-token`，0600）；`POST /rotate`，头 `X-Asip-Token` | `daemon.py:15245-15340` |
| 5 | `/rotate` 请求字段：`req_id` / `reason`(`rate429`\|`abuse403`\|`transport`\|`proactive`) / `retry_after_ms` / `egress_proxy_url` / `egress_ip` / `request_started_at` / `borrowed`；响应含 `switch_to_url` / `switch_to_tag` / `switched_at` / `switch_to_node` | `daemon.py:15318-15340`、`_rotate_slot_once:14992` |
| 6 | 槽位分发：`_slot_by_proxy_url(req.egress_proxy_url)` 定位槽位 → `_rotate_slot_once`；`_is_proxy_gate(slot)` 为真则转 `_rotate_proxy_gate_once` | `daemon.py:15321`、`15005` |
| 7 | **`_rotate_proxy_gate_once` 就是「闸口换节点」原型**：合并窗内一批并发 429 只换一次；有健康常规闸口则重试目标指回去，否则池内换节点 | `daemon.py:14907-14992` |
| 8 | opencode CLI（`~/.opencode/bin/opencode`，186MB）**吃 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` 环境变量**，也认 `"proxy"` 配置键 | 二进制 `strings`：`env.https_proxy \|\| env.HTTPS_PROXY`、`env.no_proxy \|\| env.NO_PROXY`、`"proxy"` |
| 9 | opencode 原生端点 `https://api.opencode.ai`（另有 `models.opencode.ai`） | 二进制 `strings` |
| 10 | opencode 现配置 `~/.config/opencode/opencode.jsonc`：provider `9router` → `http://localhost:20127/v1`，模型 `9router_hermes` | 文件直读 |
| 11 | **429 形态**（`~/.local/share/opencode/log/opencode.log`，32MB）：`level=ERROR message="stream error" providerID=opencode modelID=deepseek-v4.1-flash-free session.id=ses_… error.error="AI_APICallError: Free usage limit reached. Free models work in OpenCode (https://opencode.ai); subscribe to OpenCode Go…"` | `grep` 实测命中 |
| 12 | **只有 opencode.ai 免费接口能靠代理换 IP 绕过限额**；其它 provider（nvidia/openrouter/pollinations/alicode）有硬账号配额，换 IP 无效 | `CLAUDE.md:966`（既有事实） |
| 13 | 9router 是 Next.js（:20127），`providerStrategies.opencode.proxyPoolId` + `rotateStrategy`；`9router_hermes` → `oc/deepseek-v4-flash-free` 等 3 个免费模型，**同一 Public 账户** | `deploy/init-9router.sh:96/116`、`CLAUDE.md:964` |
| 14 | opencode 转录库 `~/.local/share/opencode/opencode.db`（1.1GB）：`session`(303 行, 含 `id`/`directory`)、`message`(34406)、`part`(134946) | `pragma table_info` 实查 |
| 15 | 现有 ccs 已有「API 错误中断后重发那次请求」的机制原型：`_RATE_LIMIT_NUDGE` | `session-pipeline/src/pipeflow/role_lifecycle.py` |

---

## 二、现状因果链（为何现在慢/差）

```
opencode CLI
  └─ provider=9router → localhost:20127/v1（9router，Next.js）
        └─ providerStrategies.opencode → sing-box:7890 → domain_suffix[opencode.ai]
              └─ slot-1..6 / slot-7（共享闸口、共享轮换、共享记账）
                    └─ api.opencode.ai  deepseek-v4.1-flash-free
```

**三个可量化的问题**：

1. **多一跳**：opencode → 9router（Next.js 进程）→ sing-box → opencode.ai。9router 本身就是为「把 opencode 的免费模型包装给别的客户端」而存在的，opencode CLI 用它等于**自己的流量绕回自己**。原生直连去掉这一跳 = 更快。
2. **模型被降级**：9router 的 `9router_hermes` 是个 combo，实际落到 `oc/deepseek-v4-flash-free` / `oc/nemotron-3-ultra-free` / `oc/mimo-v2.5-free` 三个模型**之一**（`CLAUDE.md:964`），而非用户点名的 `deepseek-v4.1-flash-free`。原生直连才能保证用的是 dsv4.1-flash。
3. **轮换不同权**：opencode 的 429 现在混在 slot-1..7 的公共轮换与公共记账里（`CLAUDE.md:966` 印证「换 IP 能绕过」，但计划书 v3 亦实测「90% 的轮换动作是被 429 推着走的」，且 `node_info` 的 `call_count` 恒 0、地址台账错配 79%）。opencode 自己的 429 没有专属通道，换 IP 的动作与其它闸口互相干扰。

---

## 三、目标因果链

```
opencode CLI（所有会话共享一个闸口）
  ├─ provider = 原生 api.opencode.ai
  ├─ model    = deepseek-v4.1-flash-free
  └─ HTTPS_PROXY = http://127.0.0.1:<新闸口端口>
        └─ 【新】asip 独立闸口（kind="opencode"，独立 sing-box + 独立 selector + 独立轮换）
              └─ api.opencode.ai
                    ↘ 429（Free usage limit reached）
                          ↑ ccs 监测
                          → POST /rotate-opencode-gate（rotate 新增，只动本闸口）
                          → 闸口换 IP（不中断在途）
                          → ccs 唤醒该会话，重发被中断的那次请求
```

---

## 四、方案与因果（按最根本排序）

### 第 1 层：asip 新增独立闸口（根因层 —— 出口身份与轮换归属）

**现状**：槽位只有 `kind="wg"/"h2"/"proxy"` 三类；`kind="proxy"`（slot-7）虽是闸口，但它的语义是「兜底闸口」——
`_rotate_proxy_gate_once` 明写「常规闸口恢复了就立刻回去，否则池内换节点」，**它的重试目标会指回 slot-1..6**。
拿它当 opencode 专属闸口，opencode 的流量就会被拉回公共闸口，违背「不与其他闸口交互」。

**改法**：新增 `kind="opencode"` 槽位，走 `consumer_slots()` 同一套命名/端口避让/配置渲染规则，
但：
- **独立 selector**（自己的「当前节点」，不与 `proxy` 共用选中态）；
- **独立轮换入口**（见第 3 层），换节点时**只动自己**，永不 `_pick_healthy_slot` 指回别人，也永不触发别人的轮换；
- **换线走「不中断在途」路径**（glossary：`reconnect_warp` 不中断，`_warp_line_restart` 会掐断）。这是硬约束——
  所有 opencode 会话共享一个闸口，一旦换 IP 掐断在途，全部会话同时掉线。

**为何能解决**：①②的「多一跳 + 模型被换」由第 2 层解决，本层解决③「轮换不同权」。
闸口独立 = 429 事件的作用域被限制在 opencode 自己的出口上，其它闸口的记账、冷却、候选集不被污染。

**待对齐 D1**：节点池是**独立池**（opencode 闸口有自己的候选节点集合）还是**共享池、独立选中态**？
- 共享池独立选中态：实现最小，但「哪个出口被烧了」的记账仍是公共的（现状记账本就失真，见 v3 计划书 79% 错配）。
- 独立池：记账干净，但可用节点减半，深夜池子干涸时 opencode 会比别人先饿。
- **我的建议**：先做「共享池 + 独立选中态 + 独立冷却表」，把 opencode 的 429 记账单独落一份（新账本），
  不动公共账本。等有数据再决定是否拆池。

### 第 2 层：opencode CLI 改原生直连（性能与模型正确性）

**现状**：`opencode.jsonc` 的 provider 是 `9router`，走 `localhost:20127/v1`。
**改法**：provider 改为 opencode 原生（`api.opencode.ai`），model 显式钉死 `deepseek-v4.1-flash-free`；
出口由 ccs 在启动 opencode 时注入 `HTTPS_PROXY=http://127.0.0.1:<新闸口端口>`（opencode 吃这个 env，已实证）。

**为何用 env 而非 `opencode.jsonc` 的 `"proxy"` 键**：
- env 由 ccs 在 `_opencode_inner_cmd` 里注入，**与「所有会话共享一个闸口」天然一致**（一份启动配方，全部会话同闸口）；
- 改 `opencode.jsonc` 是全局配置，会影响非 ccs 起的 opencode（用户手起的会话），超出本任务边界；
- env 是进程级，撤闸口只需改启动配方，不污染用户配置。

**为何能解决**：去掉 9router 一跳（快）、钉死模型（质量）、出口归闸口（可轮换）。

**待对齐 D2**：`opencode.jsonc` 的 provider 段是**新增一个 provider**（保留 `9router` 段不动，opencode 会话用新 provider），
还是**改写**现有 `9router` 段？
- 我倾向**新增**（如 `provider.oc-native`），`9router` 段留给用户手起的会话与回退。理由：不动既有配置 = 零回归面。

### 第 3 层：rotate 新增「换 opencode 闸口」入口（换 IP 的因果落点）

**现状**：`POST /rotate` 只有两条分发路径——常规槽位（WARP 修整）与 `_is_proxy_gate`（第七闸口）。
后者会 `_pick_healthy_slot` 把重试目标指回常规闸口，**与「不与其他闸口交互」直接冲突**。

**改法**：rotate 新增专属入口（形如 `POST /rotate-opencode-gate`，或 `POST /rotate` 加 `gate:"opencode"` 字段，
待对齐 D3），语义为：
- 只对 `kind="opencode"` 那台 sing-box 换节点；
- **不查、不选、不指回任何其它闸口**；
- 合并窗沿用（一批并发 429 只换一次，`coalesce_ms` 800ms）；
- 换线走不中断路径（第 1 层硬约束）；
- 记账落 opencode 专属账本。

**为何能解决**：这是「ccs 调 rotate 换 IP」这一因果链的落点。入口独立 = 错误轮换不与其它闸口交互（用户原话）。

**为何复用 `_rotate_proxy_gate_once` 的骨架而非新写**：它已经解决了并发 429 合并、迟到请求防重、
`switch_to_node`/`switched_at` 回填这些坑（`D3`/`D14` 都是它的实测教训）。新写一套必然重踩。
只需把「优先回常规闸口」那一支删掉、把账本换掉。

### 第 4 层：ccs 监测 429 + 调 rotate + 重试（闭环）

**现状**：ccs 的 watchdog/意图分类器只判「忙/闲/卡死」，不认识 429；
`role_lifecycle._RATE_LIMIT_NUDGE` 已有「API 错误中断后重发那次请求」的成熟范式，但那是 pipeline 侧、面向 claude 转录。

**改法（三步）**：
1. **监测**：tail `~/.local/share/opencode/log/opencode.log`，命中
   `level=ERROR … providerID=opencode … error.error="AI_APICallError: Free usage limit reached"`
   （实证第 11 条）。该行带 `session.id=ses_…`，可经 `opencode.db` 的 `session.id → directory` 映射到 ccs 角色/工作区。
2. **换 IP**：调 rotate 的第 3 层新入口，传 `reason:"rate429"` 与本次的 `session.id` 作 `req_id`（串链排查）。
3. **重试**：对命中会话发唤醒（复用 `_RATE_LIMIT_NUDGE` 范式：「上一轮 API 错误中断（0 产出）。继续：重发被中断的那次请求…」），
   让角色自己重发，而不是 ccs 代发 prompt。

**为何选「唤醒让角色重发」而非「ccs 代发 prompt」**：
- 429 是 `stream error`，**发生在回合中间**，ccs 不知道原 prompt 是什么、进行到哪一步；
- 代发 prompt 会把一个未完成的回合重做一遍，可能重复副作用（文件写了一半又写一遍）；
- `_RATE_LIMIT_NUDGE` 在 pipeline 已实证可用，语义就是「重发被中断的那次请求」，正对本场景。

**为何选 log 而非 pane 扫描**：pane 文本会滚出视口（glossary 与 `role_lifecycle` 多处实测：24h 1342 条错误轮 pane 只认出 9 次 = 0.67%）；
log 是持久、结构化、带 `session.id` 的唯一可靠面。pane 扫描可作兜底，不作主判据。

**为何能解决**：429 → 换 IP → 重发，三步因果闭合，且每步的作用域都被限制在 opencode 闸口与该会话内。

**待对齐 D4**：429 监测器放哪？
- (a) `session-launcher` 新模块（如 `ops/opencode_quota.py`）+ watchdog 周期调用 —— 与「ccs」语义最贴（用户原话「需要我们 ccs 去拦截」）；
- (b) `session-pipeline` 的 `role_lifecycle`（那里已有转录读取与 `_RATE_LIMIT_NUDGE`）。
- **我倾向 (a)**：换 IP 是基础设施动作（调 rotate、动 asip），属 launcher 职责；pipeline 是路由/评估层，不该碰 asip。
  唤醒动作可复用 launcher 已有的 `send`/`wake` 通道。

---

## 五、明确的非目标（边界）

1. **不动 slot-1..7 的任何行为**。新闸口是增量，不重构现有轮换/记账。
2. **不动 9router**。它继续服务自己的消费方；opencode 会话只是不再经它。
3. **不深接 asip**（用户原话「其实也不算真接入」）：asip 只是被指向的出口 + 一个 rotate 入口，不做双向耦合。
4. **不做 claude 侧改动**。claude 仍走 9router（`ANTHROPIC_*` env 注入），P4 零回归。
5. **不拆节点池**（除非 D1 选独立池）。

---

## 六、风险与前置验证

| 风险 | 为何重要 | 前置验证 |
|---|---|---|
| 「Free usage limit reached」是否真按 IP 计？ | 若是账号级，换 IP 无效，整条链白做 | 既有事实 `CLAUDE.md:966` 说可以；但**动手前必须实测**：同一账号、换 IP 前后各发一发，看 429 是否解除 |
| 共享一个闸口 → 换 IP 会否掐断全部在途会话 | 用户明说共享，掉线面很大 | 必须走 `reconnect_warp` 型「不中断」路径；实测换线时并发 N 个流式请求不掉 |
| opencode `HTTPS_PROXY` 对**流式**响应是否透传 | 429 出现在 `stream error`，代理必须支持 SSE/chunked | 起闸口后发一发流式请求实测 |
| `session.id → 角色` 映射是否稳定 | 监测器靠它定位要唤醒谁 | 查 `opencode.db.session` 的 `directory` 与 ccs 工作区路径的对应 |
| 新闸口端口/Clash API 端口是否会撞 | 现有避让规则是 `789x`/`909x` 递增 + 7891/9091 跳过 | 复用 `consumer_slots()` 的避让逻辑，不自己编号 |

---

## 七、决策已定（2026-11-14 用户裁决）

| # | 决策 | 裁决 | 落地口径 |
|---|---|---|---|
| D1 | 闸口节点池 | **完全共享**（池子共用） | 节点池与 slot-7 共用全部 203 个池节点；**但当前选中态与 rotate 入口必须独立**。理由：若选中态也共享，换 IP 就等于直接改 slot-7 的当前节点，与需求原话「错误轮换不与其他闸口交互」正面冲突。故落地 = 共享池 + 独立选中态 + 独立 rotate 入口（两个选项的交集，也正好是需求要的入口隔离）。 |
| D2 | opencode provider 配置 | 按建议：**新增 provider 段** | 新增 `oc-native` 段指向 `api.opencode.ai`，model 钉死 `deepseek-v4.1-flash-free`；`9router` 段原样保留供用户手起会话与回退。零回归面。 |
| D3 | rotate 入口形态 | **新增独立 endpoint** | `POST /rotate-opencode-gate`，完全不碰既有 `POST /rotate` 分发逻辑。 |
| D4 | 429 监测器归属 | 按建议：**session-launcher** | 新模块 `src/ops/opencode_quota.py`，watchdog 周期调用。理由：换 IP 是基础设施动作（调 rotate、动 asip），属 launcher 职责；pipeline 是路由/评估层，不该碰 asip。 |
| D5 | 重试语义 | **唤醒角色重发** | 复用 `_RATE_LIMIT_NUDGE` 范式（「上一轮 API 错误中断（0 产出）。继续：重发被中断的那次请求」），ccs 不代发 prompt。 |
| D6 | 前置实测 | **不用测** | 接受 `CLAUDE.md:966` 既有结论（换 IP 能绕过 opencode 免费额度），直接开工，实测并入验收阶段。 |

**D1 的张力记录**：需求原话「它的错误轮换不与其他闸口交互」与「完全共享」不能同时取到最大——
若连当前选中态都共享，换 IP 必然波及 slot-7。裁决取交集（池共享、选中态与入口隔离），
这是「完全共享」约束下能满足「不与其它闸口交互」的唯一解。若你本意是连选中态也共享（换 IP 即动 slot-7），
请推翻本条。

---

## 八、验收口径（预告，非承诺）

- **P1 归属**：opencode 会话的 429 事件只导致本闸口换节点，slot-1..7 的当前节点、账本、冷却一律不动。
- **P2 原生**：opencode 会话的模型请求直达 `api.opencode.ai`，无 `localhost:20127` 一跳；模型名就是 `deepseek-v4.1-flash-free`。
- **P3 闭环**：真造一次 429 → 监测命中 → rotate 换 IP → 会话重发成功，四步留证。
- **P4 零回归**：claude 会话行为逐字不变；slot-1..7 行为逐字不变。
- **P5 不中断**：换 IP 时并发在途流式请求不掉线。
