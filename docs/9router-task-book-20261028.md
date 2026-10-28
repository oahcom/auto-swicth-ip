# 9router 侧任务书（2026-10-28）

> 本文只写「要做什么、做到什么算完成、怎么验证」，不写改法代码。
> 代码侧的原理说明在 `9router-dev-logic-20261028.md`。
> 本轮的硬约束：**文档只登记事实，代码改动逐条对应到提交**。审查后追加的提交见 §0 与 §4，
> 凡与实现不符的表述都在 §4 的对抗审查记录里列明订正。

---

## 0. 基线

| 项 | 值 | 取证 |
|---|---|---|
| 仓库 | `~/.9router` | — |
| HEAD | `55f31496 fix(transport): 响应头超时不再重试同一 URL；429 空候选告警限流` | `git log -1` |
| 构建产物 | `.next/BUILD_ID` mtime `2026-10-28_08:40:48` | `ls -l --time-style=+%F_%T .next/BUILD_ID` |
| 运行进程 | PID 2941203，启动 `Wed Oct 28 08:40:45 2026` | `ss -ltnp \| grep :20127` + `ps -o lstart=` |
| 源树 | `git status --short open-sse/ src/ tests/` 干净 | 无未提交的源码改动 |
| 单元测试 | 3 文件 55 条通过（原记 39 条，用例随修复增加） | `cd tests && npx vitest run unit/egress.test.js unit/egress-pool-identity.test.js unit/local-target-bypasses-env-proxy.test.js` → `Test Files 3 passed (3) / Tests 55 passed (55)` |
| 全量单测 | `29 failed / 3640 passed`，与改动前基线 `29 failed / 3622 passed` 失败集合一致 | `npx vitest run tests/unit --exclude "**/*.live.test.js"` |

**部署判据订正**：本表原来用「进程启动时刻晚于 BUILD_ID mtime」判定部署生效。08:40 这次实测
出现 3 秒倒挂（进程 08:40:45 起，BUILD_ID 08:40:48 落盘）而运行中的确实是新代码 —— 判据不可靠，
详见 §4 F8。可靠做法是从构建产物里 grep 本次改动独有的字符串。

本轮已落地提交（按时间顺序）：

```
0c4a2c09 fix(transport): 私有地址不交远端出口；固定出口的空候选集要记日志
7c663a2e test(transport): 私有地址绕开环境代理的 15 条契约
66c72d5f fix(transport): proxyFetch 复用 proxyResolution 的 getEnvProxyUrl
03e47dbb fix(transport): 五维度审查后全量收敛 + 运行时数据出库
02c408e7 fix(transport): socks5 出口改为结构性快速失败
42d39edc test: 补 alias 配置让三组用例真正能跑，测试对齐新契约
55f31496 fix(transport): 响应头超时不再重试同一 URL；429 空候选告警限流
```

此后 `66c72d5f` 把 `proxyFetch.js` 里那份同名的 `getEnvProxyUrl` 副本删掉、改为从
`proxyResolution.js` 导入。该改动已在当前运行的构建里（构建取自工作区），提交时刻晚于构建。

---

## 1. 缺陷清单

| 编号 | 严重度 | 一句话 | 状态 |
|---|---|---|---|
| B9 | P0 | 三个活跃池共用同一 `proxyUrl`，egress 身份碰撞，备用出口恒为空 | **已判定为设计正确，且空候选现在会被记录** |
| B-NEW | P0 | 私有地址被交给公共出口池，30 分钟 108 次失败 0 次成功 | **已修复** |
| B8 | P1 | 429 重试路径的失败结果不带「这是一次重试」标记 | 未修，按任务书结论保持 |
| B10 | P1 | 传输层 `lastResetAt` 是进程级全局 | 记录不改 |
| B11 | P2 | 非 strict 池静默降级直连 | 记录不改 |
| B-9R1 | P1 | `9router1` 连接 137 次请求 0 成功，每次约 235s | **已自行恢复**：对端 08:4x 起 200/1.75s，症状消失（§4 F6） |

---

### B9 —— 同 `proxyUrl` 的池是同一个物理出口

**现象**：三个 `isActive=1` 的代理池都指向 `http://127.0.0.1:7890`，429 之后没有任何备选出口可用。

```
$ sqlite3 db/data.sqlite "SELECT id, isActive, json_extract(data,'\$.proxyUrl') FROM proxyPools;"
644a72f0-a6d8-41ac-bf41-6065da1381fc|1|http://127.0.0.1:7890
351035a6-0282-4cd4-8bdd-3948daa798a0|1|http://127.0.0.1:7890
24de9bf6-9377-4e4f-b2f4-c9b6cfef0baa|1|http://127.0.0.1:7890
c5a21b4e-…|0|http://127.0.0.1:7890
c2af8341-…|0|http://127.0.0.1:7890
1380ee97-…|0|http://192.168.100.104:7890
```

**判定**：原任务书写「修 B9 才能让备选非空」。本轮结论相反，并已用测试固定：
**空候选集是正确语义**。三个池背后是同一个 sing-box 监听口，换池只是换配置对象，
9router 既看不到也决定不了 sing-box 用哪个成员，所以它不构成「另一个出口」。

**机制订正（§4 F2）**：原文说「sing-box 在同一个 proxyUrl 下轮换真实出口 IP」，与配置不符。
`singbox-config.json` 里 `proxy`（28 成员）和 `opencode-out`（20 成员）都是 `selector` 而非
`urltest`，成员由外部 rotation manager 主动切换才变；同一 URL 在两次切换之间出口是**固定**的。
结论不变，但理由要换成「9router 不可见、不可控，且 URL 级冷却会活得比那次切换更久」。

**验收（已达成）**

```
$ node /tmp/b9check.mjs
三个同 url 池 id: 1 个不同 -> proxy:f1410cd719dcee161d171a3729c50bff
同 url 两候选 -> alternate = null
一个不同 url -> alternate = http://127.0.0.1:7000
```

同一脚本覆盖三条契约：同 url 三池去重为 1 个身份；同 url 候选永不被选中；
不同 url 的池仍是合法备选。

**补充验收（已达成）**：空候选不再静默。重启后的日志里已出现新告警：

```
$ grep -c "no alternate egress" logs/start-20261028-0629.log
4
$ grep -m2 "no alternate egress" logs/start-20261028-0629.log
[06:29:55] ⚠️  [EGRESS] proxy 429, no alternate egress (2 configured, all excluded as this url or cooling)
```

改动前这里一行都不会有：固定代理出口不进冷却表（`open-sse/utils/egress.js:238`），
两条分支都沉默，「没有候选」与「换了但失败」在日志里长得一模一样。

**回归用例**

- `tests/unit/egress-pool-identity.test.js`（4 条）
- `tests/unit/egress.test.js`（20 条，冷却断言改用 relay 出口）

---

### B-NEW —— 私有地址被交给公共出口池

**现象**：`9router1` 连接（`64dc47ff-dc92-4e32-ae0b-9daf1828ecb7`，baseUrl
`http://192.168.1.97:20127/v1`，`connectionProxyEnabled: false`）每一次请求都等 235 秒后失败。

**根因链**（三段，缺一段就不成立）

1. `settings.outboundProxyEnabled=1`、`outboundProxyUrl=http://127.0.0.1:7890`、
   `outboundNoProxy=''`：

   ```
   $ sqlite3 db/data.sqlite "SELECT json_extract(data,'\$.outboundProxyEnabled'), json_extract(data,'\$.outboundProxyUrl'), quote(json_extract(data,'\$.outboundNoProxy')) FROM settings;"
   1|http://127.0.0.1:7890|''
   ```

2. `applyOutboundProxyEnv`（`src/lib/network/outboundProxy.js:65-67`）据此把
   `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 写进 `process.env`，是整个进程级导出。
3. `outboundNoProxy` 为空（进程里实际的 `NO_PROXY` / `no_proxy` 两个变量值不一致，见 §4 F4），
   `shouldBypassByNoProxy` 放行；`resolveEffectiveProxyUrl` 这条连接自己没有池，于是回落到
   env 代理（`source: "env"`）。sing-box 的路由表只有 4 条规则、最后 `final: proxy`，没有内网
   目的地分支，于是 `192.168.1.97` 被丢进公网选择器，当然连不到内网地址。节点数量以配置为准：
   `proxy` 28 个、`opencode-out` 20 个；原文「83 个公网节点」无从对上（§4 F3）。

**判据（同一目标，两条路径结论相反）**

```
$ curl --noproxy '*' … /v1/models         -> HTTP=401 t=0.011581s   （直连通）
$ curl -x http://127.0.0.1:7890 …          -> HTTP=502 t=0.350798s   （走公共出口）
```

**修复（已落地，`0c4a2c09`）**：`open-sse/utils/proxyResolution.js` 增加 `isPrivateHost`，
`getEnvProxyUrl` 对 loopback、RFC1918、链路本地、`fc00::/7` 直接返回 `null`。
必须落在解析层，因为 `NO_PROXY` 的匹配器只比主机名与点后缀，写不了 CIDR。

**验收（已达成）**

```
$ node /tmp/envcheck.mjs
LAN peer  -> null
公网目标   -> http://127.0.0.1:7890
resolve LAN -> {"url":null,"source":"direct"}
```

公网目标仍然走代理，这一步不能放松，否则真实出口 IP 会泄漏。

**回归用例**：`tests/unit/local-target-bypasses-env-proxy.test.js`（5 条），
其中 `it.each` 覆盖 9 个私有地址与 3 个公网地址，`172.32.0.5` 专门测 RFC1918 边界。

**观察量**：老日志里 `falling back to direct` 出现 200 次，新日志（06:29 起）出现 0 次。

```
$ grep -c "falling back to direct" logs/server.log        -> 200
$ grep -c "falling back to direct" logs/start-20261028-0629.log -> 0
```

---

### B-9R1 —— `9router1` 对端 chat 路径不返回（已恢复，历史记录）

**现象**：该连接的 137 条请求全部失败，耗时集中在 234–237 秒（截至 `2026-10-27T22:52Z`，
最后一条 `2026-10-27T22:51:12Z`）。

```
$ sqlite3 db/data.sqlite "SELECT count(*), sum(status='success'), round(avg(json_extract(data,'\$.latency.total'))/1000.0,1), min(...)/1000, max(...)/1000 FROM requestDetails WHERE connectionId='64dc47ff-…';"
137|0|235.5|234.9|237.2
$ … AND json_extract(data,'$.latency.total') >= 200000
137
```

**关键取证：修复之后依然 235 秒，而直连也叫不动**

```
$ curl --noproxy '*' -X POST http://192.168.1.97:20127/v1/chat/completions … model=min-oc
model=min-oc HTTP=000 t=35.000775s          （超时，无响应体）
```

同一台对端上的三条对照，说明不是网络也不是鉴权：

```
$ curl --noproxy '*' -H 'Authorization: Bearer <该连接自己的 key>' …/v1/models
peer models: HTTP=200 t=5.028385s           （通）
$ …/chat/completions ，model 给一个不存在的名字
peer chat(bad model): HTTP=404 t=0.018367s  （立刻返回 404）
$ …/chat/completions  ，model=remote 或 min-oc
HTTP=000，30s / 35s / 90s 三档上限全部超时   （挂住）
```

即：对端的 HTTP 栈、鉴权、路由都是活的，只有真正转发的 chat 请求不返回。
本机不做这件事 —— 排除掉本机出口之后结论不变。

**235 秒的构成**（与出口无关，与重试预算有关）

```
$ grep -n "FETCH_CONNECT_TIMEOUT_MS = " open-sse/config/runtimeConfig.js
96:export const FETCH_CONNECT_TIMEOUT_MS = envMs("FETCH_CONNECT_TIMEOUT_MS", 60 * 1000);
$ grep -n "502: { attempts" open-sse/config/runtimeConfig.js
117:  502: { attempts: 3, delayMs: 3000 },
```

首次尝试加 3 次重试，共 4 次，每次 60s 连接超时 ⇒ 预算 240s，实测 235.5s。
`rate429` 的 `attempts: 0` 说明这条路径不会因为限流而重试。

**现状（2026-10-28 08:4x，对抗审查时复测）**：对端已恢复，两条路径都通：

```
$ curl --noproxy '*' -X POST http://192.168.1.97:20127/v1/chat/completions … model=remote
code=200 t=1.746s
$ curl -X POST http://127.0.0.1:20127/v1/chat/completions … model=9router1/remote
code=200 t=1.755s   （响应 model=deepseek-v4.1-flash-free，与对端一致）
```

`requestDetails` 里该连接最近 10 条是 2.26s–14.01s，成功 7 条、失败 3 条，不再是 100% 失败。
恢复不是本机改动的结果（本机只改了超时重试预算），对端自身或它的上游恢复所致，本机无从归因。

**处置撤回**：原文建议「摘掉 `9router1` 或长期 `modelLock`」。既然对端已恢复，摘除会丢掉一条
可用线路，撤回该建议；改为保留 + 观察。若再次出现「同一批请求 100% 失败且每次都烧满预算」，
再按 §4.3 的净损失判据摘除。

**未验证**：对端当初为什么卡、又为什么恢复。可能的候选是它自己的上游再跳一跳、或它的 sidecar
冷却、或额度窗解禁；本机没有它的日志，不做结论。

**预算侧的改动（与本条根因无关，但消掉了「每次烧满预算」这一半）**：`55f31496` 起，响应头超时
不再重试同一个 URL —— 60s 拿不到响应头就直接换 fallback URL 或抛出，单次损失从 4×60s 降到 1×60s。
`tests/unit/base-header-timeout-no-retry.test.js` 用 `config.timeoutMs=120ms` 固定这条行为。

---

### B8 —— 429 重试结果不带「这是一次重试」的上下文

**现状（未修，按原任务书结论保持）**

```
$ grep -rn "egressRetried" open-sse/ src/
（无输出）
$ grep -n "return retryResult" open-sse/executors/opencode.js
721:    return retryResult;
```

重试若仍 429，返回的是真 429，combo 正常按限流退避；重试若被传输层合成 503，
外层无法区分「首次就 503」与「429 之后重试才 503」。后者说明换出口有效但换到的是脏出口，
该优先换线。缺的是归因，不是分类能力。

**为什么不现在改**：`super.execute` 的返回契约被 combo、chatCore、流式三条路径共用，
加字段需要先确认无人按 `Object.keys` 或结构相等做判断。原任务书建议先验证再改。

**验收标准**（若将来要做）：返回值带可读标记（如 `egressRetried`），且 daemon 日志能分出两类 503。

---

### B10 —— 传输层 `lastResetAt` 是进程级全局

**结论不变：记录不改。** 整个进程只有一个 sidecar（`opencodeTransport.js:14` `let active = null`），
所以 retire / `lastResetAt` 的作用域天然是进程级。

**机制订正（§4 F11）**：原文说「所有出口共用同一份 socket 池」，不准确 —— sidecar 内部
`scripts/opencode-transport.mjs:43` 是 `sessions = new Map()`，按 `proxy` 分键各建一个
`Bun.FetchSession`。所以「按出口分键」并非物理上不成立；不做的理由改为：重启与冷却节流是进程级的，
按出口分键会造出「这条出口冷却、那条不冷却，但共用同一次重启」的半真状态，收益不足以抵消
语义复杂度。
冷却期内的 503 已被 daemon 的 `_verify_transport_failure` 兜住：先探出口，
探通就不换线，只清僵死连接。

---

### B11 —— 非 strict 池静默降级直连

**结论不变：记录不改。** 被选中的池 `644a72f0` 是 `strictProxy: true`，走 `throw` 分支不降级。
会降级的那几个池只有在 B9 的碰撞视角下才会成为候选；既然同 url 的池现在明确不互为备选，
它们进不了候选，降级路径的攻击面随之消失。

---

## 2. 回归清单

跑这三条即覆盖本轮全部改动面：

```bash
cd ~/.9router/tests
npx vitest run unit/egress.test.js unit/egress-pool-identity.test.js unit/local-target-bypasses-env-proxy.test.js
# 期望 Test Files 3 passed (3) / Tests 55 passed (55)

# 本轮新增/改契约的两组
npx vitest run unit/base-header-timeout-no-retry.test.js unit/opencode-transport-timeout-cooldown.test.js

cd ~/.9router
node -e '…'   # 或 /tmp/b9check.mjs 与 /tmp/envcheck.mjs 两个探针
```

注意 `socks-proxy-dns.test.js` 需要 `selfsigned` 包，该依赖在本机缺失，与本轮改无关；
整套测试本身也不是全绿基线，见仓库 `CLAUDE.md` 的说明。

---

## 3. 未验证项与风险

| 项 | 说明 |
|---|---|
| `9router1` 对端卡住的原因 | 只有本机侧证据；对端日志不在管辖内，未查 |
| B9 的长期影响 | 六条池里四条同 url、两条非活跃。用户可继续按自己的意愿加池，只是它们不构成备选 |
| 条件变化后的有效性 | 「108 次失败 / 0 次成功」是修复前 30 分钟窗口的实测，来自 `0c4a2c09` 的提交说明。`requestDetails` 按 `DEFAULT_MAX_RECORDS`（2000 行）滚动裁剪（`src/lib/db/repos/requestDetailsRepo.js:128`），这批行已被挤出，无法复核（§4 F5） |
| 235s 预算的算术缺口 | 4×60s + 3×3s = 249s，实测均值 235.5s，差 13.5s，未能闭合；相关行已被挤出，无法再验（§4 F5） |
| 构建与源码一致性 | 08:40 的构建含 `55f31496`（构建产物 chunk 里能 grep 到 `suppressed`）。mtime 判据会出现倒挂，见 §4 F8 |

---

## 附录：取证命令

```bash
cd ~/.9router

# 池身份与候选
sqlite3 db/data.sqlite "SELECT id,isActive,json_extract(data,'\$.proxyUrl') FROM proxyPools;"

# 出站代理设置
sqlite3 db/data.sqlite "SELECT json_extract(data,'\$.outboundProxyEnabled'),
  json_extract(data,'\$.outboundProxyUrl'), quote(json_extract(data,'\$.outboundNoProxy')) FROM settings;"

# 9router1 的成败与耗时
sqlite3 db/data.sqlite "SELECT count(*), sum(status='success'),
  round(avg(json_extract(data,'\$.latency.total'))/1000.0,1)
  FROM requestDetails WHERE connectionId='64dc47ff-dc92-4e32-ae0b-9daf1828ecb7';"

# 直连 vs 走 7890
curl -s -o /dev/null -w "%{http_code} %{time_total}\n" --noproxy '*' http://192.168.1.97:20127/v1/models
curl -s -o /dev/null -w "%{http_code} %{time_total}\n" -x http://127.0.0.1:7890 http://192.168.1.97:20127/v1/models

# 部署先后
ls -l --time-style=+%F_%T .next/BUILD_ID
ps -o lstart= -p $(ss -ltnp | grep ':20127' | grep -oP 'pid=\K[0-9]+')
```

---

## 4. 对抗审查记录（2026-10-28 09:00 起）

对本文与 `9router-dev-logic-20261028.md` 逐条对照实现复核。判据：文档里的每个数字、行号、
机制描述都要能在当前代码或当前运行态上复现；复现不了的，要么订正，要么标注为历史证据。

| 编号 | 文档原话 | 实测 | 处置 |
|---|---|---|---|
| F1 | HEAD `66c72d5f`、2 个提交、单测 39 条、BUILD_ID 06:36 | HEAD `55f31496`、7 个提交、单测 55 条、BUILD_ID 08:40:48 | §0 已更新 |
| F2 | 「sing-box 在同一个 proxyUrl 下轮换真实出口 IP」 | `singbox-config.json`：`proxy` 与 `opencode-out` 都是 `selector`（28 / 20 成员），无 `urltest`；成员只在外部 manager 切换时变 | 结论保留，机制与 `egress.js` 注释已改（`55f31496`） |
| F3 | 「83 个公网节点没有一台能连到内网」 | 配置里是 28 + 20 = 48 个成员；且 `domain_suffix: opencode.ai` 单独路由到 `opencode-out` | §1 已改为按配置计数 |
| F4 | 「进程里 `NO_PROXY=127.0.0.1,localhost,0.0.0.0`，启动脚本给的」 | `/proc/<pid>/environ`：`NO_PROXY=localhost,127.0.0.1,::1` 与 `no_proxy=127.0.0.1,localhost,0.0.0.0` 并存，值不同；无 `ALL_PROXY` | §1 已改为按实测描述；结论（不含内网段）不变 |
| F5 | 「235.5s = 4×60s 预算」 | 4×60s + 3×3s 退避 = 249s，差 13.5s 未闭合；137 条原始行已被 `requestDetails` 保留窗口（约 2000 行 / 35 分钟）挤出，无法复核 | §3 标注为历史证据 + 未闭合缺口 |
| F6 | B-9R1「对端 chat 恒挂，建议摘掉」 | 直连对端 `model=remote` → 200/1.746s；经本机 `9router1/remote` → 200/1.755s；最近 10 条 2.26–14.01s，7 成功 3 失败 | 状态改「已恢复」，摘除建议撤回 |
| F7 | 「空候选要说话」（新增告警） | 无节流：39 分钟 585 行，同一秒最多 4 条重复 | `55f31496` 起同一出口每分钟至多一行，实测 `(+14 suppressed)` |
| F8 | 「进程启动晚于 BUILD_ID mtime」判部署 | 本次 08:40:45 启动 vs BUILD_ID 08:40:48，倒挂 3 秒，而运行中的确是新代码 | 判据改为 grep 构建产物中的新增字符串 |
| F9 | `opencode.js:646-650`、`opencode.js:689`、`egress.js:51` | 实际 681、721、50；`auth.js` 实际路径 `src/sse/services/auth.js` | 已订正 |
| F10 | B10「所有出口共用同一份 socket 池」 | 进程单例成立（`opencodeTransport.js:14`），但 sidecar 内 `sessions` 按 proxy 分键（`scripts/opencode-transport.mjs:43`） | 结论保留，理由改写 |
| F11 | `daemon.py` 有 `_classify_transport_error` / `_verify_transport_failure` 兜底 | 实测存在：`daemon.py:1108`、`:1124` | 断言成立，无需改 |
| F12 | 私有后缀只列 `.localhost/.local/.internal/.home.arpa` | 实际代码另有 `.lan`（本轮新增）且当时无测试 | 补用例 `nas.lan → true` |
| F13 | `restart_nine_router.sh` 作为部署脚本 | `nine-router.service`、`auto-switch-ip.service` 在本机都不存在；探活打废弃端口 20128，`set -e` 下第一条 restart 即中断 | 脚本按真实运行方式重写并实跑通过 |

### 复现命令

```bash
cd ~/.9router

# F2/F3 出口拓扑
python3 -c "import json;d=json.load(open('/home/administrator/dkk-projects/auto-switch-ip/singbox-config.json'));print([(o['tag'],len(o['outbounds'])) for o in d['outbounds'] if o['type']=='selector']);print(d['route']['rules'])"

# F4 进程级代理变量
tr '\0' '\n' < /proc/$(ss -ltnp | grep ':20127' | grep -oP 'pid=\K[0-9]+' | head -1)/environ | grep -i proxy

# F5 证据是否还在
sqlite3 db/data.sqlite "SELECT count(*),min(timestamp),max(timestamp) FROM requestDetails WHERE connectionId='64dc47ff-dc92-4e32-ae0b-9daf1828ecb7';"

# F6 对端现状
curl -s -o /dev/null -w "%{http_code} %{time_total}\n" --noproxy '*' -X POST http://192.168.1.97:20127/v1/chat/completions -H 'Content-Type: application/json' -H 'Authorization: Bearer <该连接 key>' -d '{"model":"remote","messages":[{"role":"user","content":"hi"}]}'

# F7 告警节流
grep "no alternate egress" logs/start-20261028-0841.log

# F8 构建产物内容判据
grep -l suppressed .next/standalone/.next/server/chunks/*.js
```

### 审查后的代码改动（与任务书条目无关但必须记录）

- `base.js`：响应头超时不再重试同一 URL（B-9R1 的「烧满预算」半边），带 120ms 单测。
- `opencode.js`：空候选告警限流。
- `proxyResolution.js`：`.lan` 后缀 + 用例。
- `restart_nine_router.sh`：按真实进程管理重写。
- `vitest.config.mjs`（`42d39edc`）：补 `@/*` 别名，三组原本在 import 阶段就失败的用例（36 条）才真正跑起来。
