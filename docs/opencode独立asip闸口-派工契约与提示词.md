# opencode 独立 asip 闸口 — 派工契约与提示词

日期：2026-11-14　依据：`opencode独立asip闸口-论证体计划书.md`（决策 D1–D6 已定）
三方六维度审查架构：实现子代理 / 交叉盲审子代理 / 主代理终审

---

## 一、冻结契约（两个实现代理共同遵守，改契约须先报我）

### C1. rotate 新入口（asip 侧实现，ccs 侧调用）

- **通道**：与既有 `POST /rotate` 同一个 Unix Socket（`run/rotate.sock`），同一个 `X-Asip-Token` 头。
- **路径**：`POST /rotate-opencode-gate`（独立 endpoint，**不碰** `POST /rotate` 的分发逻辑）。
- **请求体**：
```json
{
  "req_id": "str, 可选，排查串链用（ccs 传 opencode 的 session.id）",
  "reason": "rate429 | abuse403 | transport | proactive",
  "egress_ip": "str|null, 出问题的那个出口地址",
  "request_started_at": "float|null, 该请求发出时刻（epoch）",
  "retry_after_ms": "float|null, 上游 Retry-After 毫秒",
  "session_id": "str|null, opencode 会话 id（记账用）"
}
```
- **响应体**：
```json
{
  "ok": true,
  "switched": true,
  "reason": "ok | coalesced | borrowed_skip | no_healthy_node",
  "switch_to_node": "str|null, 换到哪个池节点",
  "switched_at": "float, 换线时刻",
  "active_egress": "str|null",
  "ms": 0
}
```
- **语义硬约束**：
  1. **只动 opencode 闸口自己的 selector**，永不 `_pick_healthy_slot` 指回任何其它闸口，永不触发别人轮换；
  2. **换线走「不中断在途」路径**（glossary：`reconnect_warp` 型，非 `_warp_line_restart` 型）——所有 opencode 会话共享这一个闸口，掐断在途 = 全体掉线；
  3. 合并窗沿用 `coalesce_ms`（默认 800ms）：一批并发 429 只换一次；
  4. 节点池**与 slot-7 共用**（D1），但**选中态、冷却、记账独立**；
  5. 记账落 opencode 专属账本，不写公共 `node_info`。

### C2. 429 观测面（ccs 侧读取）

- **来源**：`~/.local/share/opencode/log/opencode.log`
- **判据**：行内同时含 `level=ERROR`、`providerID=opencode`、`error.error="AI_APICallError: Free usage limit reached"`
- **session 归属**：行内 `session.id=ses_…` → `opencode.db` 的 `session.id → directory` → ccs 工作区/角色
- **失败回退**：`opencode.db` 读不到时按「无法归属」处理，只换 IP 不唤醒（宁可漏唤醒，不误唤醒）

### C3. 唤醒文案（ccs 侧发）

复用 `_RATE_LIMIT_NUDGE` 语义：「上一轮 API 错误中断（0 产出）。继续：重发被中断的那次请求，不要重新规划，不要写自检报告。」

### C4. opencode provider 段（ccs 侧写）

```jsonc
"oc-native": {
  "npm": "@ai-sdk/openai-compatible",
  "name": "OpenCode Native",
  "options": { "baseURL": "https://api.opencode.ai/v1" },
  "models": { "deepseek-v4.1-flash-free": { "name": "DSv4.1 Flash Free",
      "limit": { "context": 200000, "output": 32000 } } }
}
```
- `9router` 段**原样保留**（D2），供用户手起会话与回退。
- 出口靠 ccs 启动时注入 `HTTPS_PROXY=http://127.0.0.1:<opencode 闸口端口>`（opencode 吃该 env，已实证）。

### C5. 模块边界（用户 2026-11-14 明确要求：opencode 逻辑单独开模块）

**opencode 闸口的全部逻辑放进 asip 的一个独立模块，不揉进 `daemon.py`（330KB 单体）。**

落地形态：
- 新增 `asip/opencode_gate.py`（或 `opencode_gate/` 包），**自包含**：
  - `kind="opencode"` 槽位的定义与 selector 选中态
  - `POST /rotate-opencode-gate` 的请求解析、合并窗、换节点、回填 payload
  - opencode 专属账本（独立于公共 `node_info`）
  - 429/换节点的判定与日志
- `daemon.py` 只做**接线**（wiring）：
  - `_RotateHandler.do_POST` 里把 `/rotate-opencode-gate` 路径委托给该模块的一个函数；
  - `consumer_slots()` 里把新槽位的 dict 产出（或委托该模块产出）；
  - **不写业务逻辑进 `daemon.py`**。
- `config.py` 只加**配置常量**（槽位数、端口基准、`coalesce_ms`、账本路径），不加逻辑。
- 模块对 daemon 的依赖面最小化：只通过函数调用/传参取所需状态（selector 当前项、节点池列表），
  **不 import daemon 的私有锁**；需要串行化时模块内自带锁。

**为何这样**（这是需求原话「错误轮换不与其他闸口交互」的结构表达）：
- 逻辑物理隔离 = 改 opencode 不可能波及 slot-1..7，反之亦然；比「注释里写不许改」硬得多；
- `daemon.py` 330KB 已是单体，把新逻辑塞进去会重蹈「改 A 坏 B」；
- 独立模块可独立测试（不拉起 daemon 就能跑用例）。

**接口约定**（daemon 与模块之间，两轨共同遵守）：
```python
# asip/opencode_gate.py 对外只暴露三个入口
def make_slot(base_port: int, base_clash: int, slot_index: int) -> dict
    # 产出槽位 dict（name/kind/selector/port/clash_api_port/config_file），格式与 consumer_slots() 一致
def handle_rotate(req: dict) -> dict
    # 契约 C1 的请求体 → 响应体；内部自带合并窗与锁
def snapshot() -> dict
    # 只读快照（当前节点/冷却/账本摘要），供诊断
```

---

## 二、派工安排（并发边界 = 仓库）

| 轨 | 代理 | 仓库 | 内容 | 并发 |
|---|---|---|---|---|
| A | `impl-gate` | `dkk-projects/auto-switch-ip` | 新增 `kind="opencode"` 槽位 + `POST /rotate-opencode-gate` + 独立选中态/记账 | 与 B 并发 |
| B | `impl-ccs` | `session-launcher` | provider 新增 `oc-native` + `HTTPS_PROXY` 注入 + 429 监测器 + 唤醒重发 | 与 A 并发 |

**为何不拆三个代理**：B 内部两项（provider 配置、429 监测器）同仓同 commit，拆开会抢 `git index.lock`；
A/B 跨仓无共享文件，是安全并发边界。

**监控点**（我盯这些）：契约偏离 / claude 路径被误改 / slot-1..7 行为被误改 / 换线走了「重启」路径 / 测试基线被改动 / 三次修不好。

---

## 三、提示词 · 轨 A（impl-gate）

> 你是实现子代理。任务：给 asip 新增一个**专属 opencode 闸口**与它的**独立换 IP 入口**。
>
> ## 执行环境（必须遵守）
> 代码在 WSL Ubuntu：`/home/administrator/dkk-projects/auto-switch-ip`（`daemon.py` 330KB + `config.py` + `sing-box`）。
> Bash 工具是 Windows Git Bash，**不能直接跑 WSL 内 python3**。统一做法：
> 1. 用 Write 工具把脚本写到 `C:\Users\Administrator\WorkBuddy\2026-11-13-16-01-58\.tmp_gate_<n>.sh`；
> 2. 用 PowerShell 工具执行 `wsl -d Ubuntu -- bash /mnt/c/Users/Administrator/WorkBuddy/2026-11-13-16-01-58/.tmp_gate_<n>.sh`；
> 3. 脚本内不要把 `$VAR` 内联进命令行（外层会吞）；命令串里不放中文。
> 改文件用 Edit 工具直接编辑 UNC 路径 `\\wsl.localhost\Ubuntu\home\administrator\dkk-projects\auto-switch-ip\...`。
>
> ## 先读（不读不许动手）
> - `docs/glossary.md` —— 名词表（节点/线路/出口/闸口/换线/重启/四把锁）。**「换线」不中断在途，「重启」会掐断**，本任务必须用换线。
> - `docs/plan-gate-rotation-v3.md` 第 0 节名词表 —— slot/闸口/selector/P2C/Retry-After/出口级额度的准确定义。
> - `config.py:1224-1290` `consumer_slots()` —— 现有 slot-1..6 + slot-7（`kind="proxy"`）怎么产出来。
> - `daemon.py:14907-14992` `_rotate_proxy_gate_once` —— 第七闸口换节点原型（**你要复用它的骨架**）。
> - `daemon.py:15245-15340` `_RotateHandler` —— rotate 的 UDS HTTP 服务与 `/rotate` 分发。
>
> ## 要做（三项）
> ### 0. 模块边界（**最高优先，用户明确要求**）
> opencode 闸口的全部逻辑放进 **`asip/opencode_gate.py`**（或 `opencode_gate/` 包），**自包含、不揉进 `daemon.py`**。
> `daemon.py` 只做接线（把 `/rotate-opencode-gate` 委托给 `opencode_gate.handle_rotate`、把槽位产出委托给
> `opencode_gate.make_slot`），`config.py` 只加配置常量。接口约定见契约 C5。
> 逻辑物理隔离 = 改 opencode 不可能波及 slot-1..7，反之亦然。**这是红线，不许把业务逻辑写进 `daemon.py`。**
>
> ### 1. 新增 `kind="opencode"` 槽位
> 在 `config.py` 的 `consumer_slots()` 里，照 slot-7 的写法再加一个槽位（端口/Clash API 继续按既有避让规则编号，
> **不许自己编号**，复用 `consumer_slot_config_path` 命名）。与 slot-7 的区别：
> - `kind="opencode"`（新值，不与 `"proxy"` 混用）；
> - **独立 selector**（自己的「当前选中项」，不与 slot-7 的 `proxy` selector 共用选中态）；
> - 节点池**共用**（D1 裁决：完全共享池），但**选中态、冷却、记账独立**。
> 配置渲染（`parse_subscription.py` 的 `default_to_pool`）要能服务它，照 slot-7 的 `default_to_pool` 分支复制一份语义。
>
> ### 2. 新增 `POST /rotate-opencode-gate`
> 在 `_RotateHandler.do_POST` 里加独立路径，**不改 `/rotate` 的任何分支**。契约见上文 C1（请求/响应字段、语义硬约束）。
> 实现骨架**复用 `_rotate_proxy_gate_once`**，但做三处删改：
> - **删掉「优先回常规闸口」那一支**（`_pick_healthy_slot(...)` 与它的 `switch_to` 回填）——
>   这是「错误轮换不与其他闸口交互」的核心，留着就违背需求原话；
> - 换节点用**不中断在途**的路径（glossary 的「换线」而非「重启」）；
> - 记账落 **opencode 专属账本**（新表或新文件），不写公共 `node_info`。
> 合并窗沿用 `coalesce_ms`（800ms）：一批并发 429 只换一次，重复调用回 `reason:"coalesced"`。
>
> ### 3. 不许碰的（红线）
> - 不许改 `POST /rotate` 的任何分支；
> - 不许改 slot-1..7 的行为、端口、selector、账本；
> - 不许改 `_rotate_proxy_gate_once` 本身（只在新入口里复用/裁剪它的逻辑）；
> - 不许改 `parse_subscription` 的既有分支（只加不改）；
> - 不许动生产在跑的 sing-box 进程 / 不许重启 asip 服务（验收阶段才做，且须我批准）。
>
> ## 验证（全跑，贴原始输出）
> 1. `python3 -m py_compile config.py daemon.py parse_subscription.py`；
> 2. 该仓既有测试基线：先跑 `python3 -m pytest -q` 记下基线（通过/失败数与失败名），改完后**必须仍是同一基线**，
>    新增用例另行计数；若该仓跑不起来测试，明说「未能跑」，不要编造；
> 3. 新增用例至少三条：①`/rotate-opencode-gate` 只动 opencode 闸口 selector、断言其它闸口 selector 当前项不变；
>   ②一批并发调用只产生一次换节点（合并窗）；③换节点走的是不中断路径（断言没调 `_warp_line_restart` / 等价的重启函数）；
> 4. **禁止**在验证里真的切换生产闸口或重启 sing-box——用打桩/临时实例（参考该仓 `test_asip_*` 已有的隔离手法）。
>
> ## 报告
> 写到 `\\wsl.localhost\Ubuntu\home\administrator\impl-gate-REPORT.md`，最终回复给：
> - commit sha + `git show --stat` + 完整 diff；
> - 三条新用例的原始输出；
> - 测试基线（改前/改后数字）；
> - 与契约/红线的任何偏差及原因（不隐瞒）；特别是「不中断在途」你怎么证明的。

---

## 四、提示词 · 轨 B（impl-ccs）

> 你是实现子代理。任务：让 ccs 起的 opencode 会话**原生直连** `deepseek-v4.1-flash-free`、出口走专属闸口，
> 并在撞 429 时**监测 → 调 rotate 换 IP → 唤醒角色重发**。
>
> ## 执行环境（必须遵守）
> 代码在 WSL Ubuntu：`/home/administrator/session-launcher`。
> Bash 工具是 Windows Git Bash，**不能直接跑 WSL 内 python3**。统一做法：
> 1. 用 Write 工具把脚本写到 `C:\Users\Administrator\WorkBuddy\2026-11-13-16-01-58\.tmp_ccs_<n>.sh`；
> 2. 用 PowerShell 工具执行 `wsl -d Ubuntu -- bash /mnt/c/Users/Administrator/WorkBuddy/2026-11-13-16-01-58/.tmp_ccs_<n>.sh`；
> 3. 脚本内不要把 `$VAR` 内联进命令行；命令串里不放中文。
> 改文件用 Edit 工具直接编辑 UNC 路径 `\\wsl.localhost\Ubuntu\home\administrator\session-launcher\...`。
>
> ## 先读（不读不许动手）
> - `src/core.py` 的 `_ENGINE_REGISTRY`（约 790 行）与 `_opencode_inner_cmd`（约 781 行）—— 启动配方按引擎查表，opencode 行已存在。
> - `src/core.py` 的 `_write_opencode_mcp`（约 373 行）—— 上一轮刚加的 opencode 侧落盘，**风格照它**。
> - `src/ops/watchdog.py` —— 周期健康检查的挂载点。
> - `session-pipeline/src/pipeflow/role_lifecycle.py` 的 `_RATE_LIMIT_NUDGE` —— 「重发被中断请求」的文案范式（**只参考，不改 pipeline**）。
>
> ## 要做（三项）
> ### 1. opencode provider 原生直连
> `~/.config/opencode/opencode.jsonc` **新增** provider 段 `oc-native`（契约 C4），指向 `https://api.opencode.ai/v1`，
> model 钉死 `deepseek-v4.1-flash-free`。**`9router` 段原样保留**（D2 裁决），不许改写、不许删。
> 写入逻辑加到 `src/core.py`（风格照 `_write_opencode_mcp`：已存在就合并该键、保留其它键；JSON 坏了不覆盖只告警）。
>
> ### 2. 出口注入 `HTTPS_PROXY`
> 在 `_opencode_inner_cmd`（或它的调用点）给 opencode 进程注入 `HTTPS_PROXY=http://127.0.0.1:<opencode 闸口端口>`、
> `HTTP_PROXY` 同值、`NO_PROXY=127.0.0.1,localhost`。
> **端口从哪来**：闸口端口由 asip 侧 `consumer_slots()` 决定（继续按 789x 避让编号）。
> 你要**可配置**（env 或 config 读），不要硬编码；读不到时**显式降级为不注入**并告警（不许静默指向一个不存在的端口）。
> **不许动 claude 行**的 env 注入（`ANTHROPIC_*` 那套逐字不动，P4 零回归红线）。
>
> ### 3. 429 监测器 + 唤醒重发
> 新模块 `src/ops/opencode_quota.py`，watchdog 周期调用。契约 C2/C3：
> - tail `~/.local/share/opencode/log/opencode.log`，命中 `level=ERROR` + `providerID=opencode` +
>   `error.error="AI_APICallError: Free usage limit reached"`；
> - 行内 `session.id=ses_…` → `opencode.db`（`~/.local/share/opencode/opencode.db`，只读）的 `session.id → directory`
>   → ccs 角色/工作区；
> - 命中即调 rotate 的 `POST /rotate-opencode-gate`（Unix Socket `run/rotate.sock` + `X-Asip-Token` 头，
>   token 读 `run/api-token`；**契约 C1 的请求体**，`req_id` 传 session.id）；
> - 换 IP 成功后对该会话发唤醒（复用 launcher 已有的 `send`/`wake` 通道），文案见契约 C3；
> - **归属失败时只换 IP 不唤醒**（宁可漏唤醒，不误唤醒）。
> **重复抑制**：同一 `session.id` 在冷却窗内（建议 60s）不重复换 IP + 不重复唤醒。
>
> ### 4. 不许碰的（红线）
> - 不许改 claude 的启动配方与 `ANTHROPIC_*` env 注入（P4 零回归）；
> - 不许改 `_ENGINE_REGISTRY` / `_ENGINE_SPECS` 的既有数据（opencode 行的 `inner_builder` 可以改实现，表数据不动）；
> - 不许改 session-pipeline（那是路由/评估层，不该碰 asip）；
> - 不许重启生产 tmux 会话（`ccs-consumer`/`ccs-engineer`/`ccs-scout`）；
> - 不许把 rotate 的 socket 路径/token 硬编码进代码（读 config，缺了显式降级）。
>
> ## 验证（全跑，贴原始输出）
> 1. `python3 -m py_compile src/core.py src/ops/opencode_quota.py`；
> 2. `python3 -m pytest tests -q` 全量 —— 基线是 **518 passed**，改后必须仍 518（你新增用例则 518+N），
>    **不许新增失败**；若失败名变化，逐个说明；
> 3. 新增用例至少四条：①provider 写入保留 `9router` 段与用户手写键；②`HTTPS_PROXY` 注入只对 opencode 行、
>   claude 行零改动；③429 判据命中（喂一行真实形态的日志）；④归属失败时不唤醒但换 IP；
> 4. 端到端可选（若起得了临时 opencode）：注入 env 后 `opencode` 请求走的是指定代理端口。
>    起不了就明说「未做端到端」，不要编造。
>
> ## 报告
> 写到 `\\wsl.localhost\Ubuntu\home\administrator\impl-ccs-REPORT.md`，最终回复给：
> - commit sha + `git show --stat` + 完整 diff；
> - 四条新用例的原始输出；
> - `pytest tests -q` 改前/改后数字；
> - 与契约/红线的任何偏差及原因（不隐瞒）；特别是 `HTTPS_PROXY` 端口怎么解析的、读不到时怎么降级的。

---

## 五、交叉盲审提示词（实现完成后派，模板）

> 你是独立交叉盲审代理。只做审查与取证，**禁止修改业务代码、禁止 git 写操作**。
> 六维度：正确性 / 完整性 / 一致性 / 影响面与零回归 / 实证性 / 防退化。
> 重点盯四条红线：①claude 路径是否被误改 ②slot-1..7 行为是否被误改 ③换线是否走了「重启」路径（会掐断在途）
> ④rotate 新入口是否真的与其它闸口隔离（有没有 `_pick_healthy_slot` 回指）。
> 逐条给结论 + 证据（文件:行号 / 测试原始输出 / grep 命中）。裁决：通过 / 有条件通过 / 不通过。
> 阻断项列文件:行号 + 为何阻断。不确定就说「未能验证」，不要猜。
