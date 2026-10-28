# 9router 开发逻辑书（2026-10-28）

> 与 `9router-task-book-20261028.md` 配套。任务书说「做什么」，本文说「为什么这么判」。
> 每条结论都从机制推到现象，不用现象反推机制。

---

## 1. 出口身份：一个 `proxyUrl` 就是一个物理出口

### 1.1 事实

`identifyFixedEgress`（`open-sse/utils/egress.js:51`）对固定代理出口只做一件事：

```js
const { url: proxyUrl } = resolveEffectiveProxyUrl(targetUrl, proxyOptions);
if (proxyUrl) return { id: hashEgress("proxy", proxyUrl), kind: "proxy", ... };
```

身份 = `hash(proxyUrl)`。池 id、账号、池名都不参与。这看上去像缺陷 ——
毕竟三个池是三个独立配置对象 —— 但物理层面它们是同一个东西。

### 1.2 为什么这是对的

`http://127.0.0.1:7890` 是 sing-box 的 mixed 入站，路由最终落到 `selector` 出站。
**订正（任务书 §4 F2）**：原来说「真实出口 IP 在它下面轮换」，与配置不符。`singbox-config.json`
里 `proxy`（28 成员）与 `opencode-out`（20 成员）都是 `selector` 而非 `urltest`，成员只在外部
rotation manager 主动切换时才变 —— 两次切换之间，同一个 `proxyUrl` 的出口是固定的。

9router 全程看不到这个选择，也无法通过「换池」改变它：换池只改变 9router 认为自己在用哪个配置
对象，不改变任何一个发往 opencode.ai 的数据包从哪个 IP 出去。这才是「同 url 的池不构成备选」的
真正理由 —— 不是「换了也是同一个 IP」，而是「换不换与出口无关，且 9router 无从观测」。

`markEgressCooling` 里那段注释把同一件事说透了（`egress.js:228-238`）：

```js
// ... the fixed proxy URL is an opaque downstream for an external rotation
// manager (sing-box), whose upstream IP rotates underneath without changing
// the proxyUrl — the egress-or-id key does not follow that IP. Cooling the same
// 7890 URL for hours after a 429 deadlocks the lane even though the real exit
// IP is brand-new. ...
if (egress.kind === "proxy") return null;
```

所以 `kind === "proxy"` 的出口**从不进冷却表**。这不是遗漏，是刻意的：冷却按 URL 记账，
而 URL 背后的 IP 一直在变，冷却它等于把一条换了新 IP 的通道一起锁死。

### 1.3 结论与它推翻的旧判断

原任务书写「修 B9 才能让备选非空」，隐含前提是「备选为空是故障」。这个前提错了。

真正的故障有两段，发生在不同时间：

- 有第四个池 `127.0.0.1:7000` 时，它是唯一 id 不碰撞的候选，于是 15:28–17:27 之间
  **1618 次换线全换到这个死端口**。那时「备选不为空」恰恰是故障的样子。
- 删掉 7000 之后，候选恒空。这是**正确结果**，但日志里一行都没有，读起来和
  「换了但失败」一样。这才是需要修的部分。

于是修法不是「让 id 不同」，而是「空候选要说话」。`opencode.js:646-650` 补的就是这句话：

```
[EGRESS] proxy 429, no alternate egress (2 configured, all excluded as this url or cooling)
```

而 `tests/unit/egress-pool-identity.test.js` 把三条契约固定下来：
同 url 去重为 1、同 url 永不互为备选、不同 url 仍是备选。
第二条与「id 能区分」不冲突 —— 前者管选线，后者管记账。

### 1.4 一个必须记住的推论

`auth.js:63-66` 按**池 id** 排除当前池，`egress.js:310` 按 **url 哈希**排除。
两个键不是同一个东西，所以「候选列表非空」与「有真实可选出口」是两件事。
任何后来者在 `auth.js` 侧加去重都不会改变 `selectAlternateProxy` 的结论；
要判断到底有没有备选，只能看 `selectAlternateProxy` 的返回。

---

## 2. 环境代理：一层进程级的网，罩住所有没有自己池的请求

### 2.1 事实链

`settings.outboundProxyEnabled=1` 不是「给某个连接配代理」，它是**进程级导出**。
`applyOutboundProxyEnv`（`src/lib/network/outboundProxy.js:65-67`）把三个大写变量写进 `process.env`：

```js
process.env.HTTP_PROXY = validated;
process.env.HTTPS_PROXY = validated;
process.env.ALL_PROXY = validated;
```

`NO_PROXY` 只在 `outboundNoProxy` 非空时才写（`:74`）。本机该值为空串，进程里实际有**两个**
变量且值不一致（`/proc/<pid>/environ` 实测，任务书 §4 F4）：

```
NO_PROXY=localhost,127.0.0.1,::1        # 来自启动脚本
no_proxy=127.0.0.1,localhost,0.0.0.0    # 来自另一处，值不同
```

两者都不含内网段，所以结论不变；但「只有一个 `NO_PROXY=127.0.0.1,localhost,0.0.0.0`」这句话
不成立，写文档时不要照抄。

解析顺序在 `resolveEffectiveProxyUrl`：连接自己的池 → 环境代理 → 直连。
`9router1` 连接 `connectionProxyEnabled: false`，落到第二档。

### 2.2 为什么 `NO_PROXY` 解决不了

`shouldBypassByNoProxy` 的匹配器只有三种形态：`*`、点前缀后缀、精确/点后缀。
写 `192.168.0.0/16` 不会命中任何地址，因为代码里根本没有 CIDR 分支。
这不是配置没调对，是表达能力不够。

### 2.3 现象怎么对上机制

`192.168.1.97` 是本机所在网段的另一台机器。请求被交给 `127.0.0.1:7890`，sing-box 的路由表
只有 4 条规则、`final: proxy`，没有任何内网目的地分支，于是它按公网选择器处理 —— 无论选择器里
有多少成员都连不到内网地址。

**订正（任务书 §4 F3）**：原文「83 个公网节点」与配置对不上，实际是 `proxy` 28 个 +
`opencode-out` 20 个；且 `domain_suffix: opencode.ai` 的流量单独走 `opencode-out`。
同一目标两条路径的对照把这一点钉死：

| 路径 | 结果 |
|---|---|
| `--noproxy '*'` 直连 | `401`，0.0116s（未带 key 是正确的 401，说明到达了服务） |
| `-x http://127.0.0.1:7890` | `502`，0.35s（公共出口连不到内网） |

### 2.4 修法为什么放在解析层

停在决策阶梯的第 4 级：平台/地址语义本身就规定了私网不可路由到公网出口，
不需要每个调用方各写一遍。放在 `getEnvProxyUrl` 里，所有走
`resolveEffectiveProxyUrl` 的地方一次生效 —— `proxyFetch.js`、`opencodeTransport.js`、
`egress.js` 的身份计算。

**scope 必须只覆盖私有地址**。测试里专门有一条 `still proxies public targets`：
如果为了修内网而把 env 代理整个关掉，公网流量会走真实出口 IP，
那是比原来更严重的问题。

### 2.5 一个副本，两种命运

`proxyFetch.js` 里原本还有一份**同名同义**的 `getEnvProxyUrl` 私有副本。
修复只加在 `proxyResolution.js` 上时，`proxyFetch` 仍走自己那份，于是内网地址继续被代理。
`66c72d5f` 删掉副本、改为 import，两份合成一份。

这类「同一函数两份实现」的缺陷模式在本仓库出现过（`_line_probe_cadence` 在
`daemon.py` 里定义两次），判据一样：改一处、另一处不动，行为看起来只修了一半。

---

## 3. 235 秒是怎么算出来的（已被 55f31496 取代，留作历史）

> **2026-10-28 09:00 起**：`base.js` 的响应头超时不再重试同一个 URL，单次损失从 4×60s 降到
> 1×60s。本节只解释那批历史记录是怎么来的，不代表当前行为。
> `tests/unit/base-header-timeout-no-retry.test.js` 固定新行为（`config.timeoutMs=120ms` 下
> `calls=1`、耗时 <1s）。

`9router1` 的 137 条请求耗时全部落在 234.9–237.2 秒。这个数字不是网络超时，是重试预算。

```
FETCH_CONNECT_TIMEOUT_MS = 60_000        (runtimeConfig.js:96)
DEFAULT_RETRY_CONFIG[502] = { attempts: 3, delayMs: 3000 }   (runtimeConfig.js:117)
```

`base.js:143` 每次尝试挂一个 60s 的连接计时器，超时后 `AbortError` 被
`isConnectTimeout` 识别（`:173`），转成可重试的网络错误，接着 `tryRetry(502)` 放行 3 次重试。

**算术缺口（任务书 §4 F5）**：4 次尝试 × 60s **加上 3 次 3s 退避** = 249s，而实测均值 235.5s，
差 13.5s。原文「240s 预算，实测 235.5s，每次略早于硬超时返回」把这个缺口抹掉了 —— 退避是加在
预算之外的，不可能让总数变小。缺口未解释（候选：并非每条都跑满 4 次、或调用方更早中止），
且 137 条原始行已被 `requestDetails` 的保留窗口挤出，无法再验。写「机制推到现象」时，这种
闭合不了的差额必须写出来，不能取整。

`429: { attempts: 0 }` 说明限流类错误不重试，所以 235s 这个量级只能来自连接超时路径。

**推论**：看到「请求很慢」先算重试预算，别先怀疑带宽。这条同时解释了为什么
`9router1` 一个连接能在两小时内拖出 137 条 235s 的记录 —— 每条都是完整预算。

---

## 4. 对端卡住：能说什么、不能说什么

### 4.1 已证实的

对端 `192.168.1.97:20127` 的 HTTP 栈是活的：

| 请求 | 结果 |
|---|---|
| `GET /v1/models` 带 key | `200`，5.03s（77 个模型，含 `remote`） |
| `POST /v1/chat/completions`，model 不存在 | `404`，0.018s |
| `POST /v1/chat/completions`，model=`remote` | 无响应，30/35/90s 三档上限全部到点 |
| `POST /v1/chat/completions`，model=`min-oc` | 无响应，35s 到点 |

鉴权、路由、错误返回都正常，只有真正转发的 chat 请求不返回。本机侧已经把出口排除掉
（直连同样卡），所以问题在对端或对端的上游。

### 4.2 不能说的

对端为什么卡，本机没有证据。可能的候选：它自己也有一层 upstream 转发（`remote`
这个名字暗示再一跳）、它自己的 sidecar 在冷却、它的上游账号在退避。
这些都需要对端日志，本机不猜。

### 4.3 这条缺陷的处置原则

「同一批请求 100% 失败、每次都烧满预算」是明确的净损失：既拿不到结果，又占住调用槽 235s。
在根因定位前，正确动作是**把它从 combo 里摘掉或长期 modelLock**，而不是等它自己好。
这与 `oc/longcat-2.5-preview-free` 的处理同构：当时它 0/180 成功、每条白试一次，
处置就是移除而不是继续容忍。

---

## 5. 三个「记录不改」的判据

这三条容易被后来者当成遗漏再修一遍，所以把不修的理由写死。

### 5.1 B8 —— 归因缺失，不是分类能力缺失

重试结果若是 429，返回的是真 429，combo 的 `checkFallbackError(429)` 正常退避；
若是传输层合成的 503，daemon 的 `_classify_transport_error` 已经把它与真出口故障分开计数，
并触发 `_verify_transport_failure` 先探出口。缺的只是「这是 429 之后重试的 503」这条归因。

改它要动 `super.execute` 的返回契约，而 combo、chatCore、流式三条路径都在读这个返回值。
收益是一条日志字段，风险是三条路径的行为回归。**性价比不成立。**

### 5.2 B10 —— 按出口分键这个改法在物理上不成立

`opencodeTransport.js:14` 的 `let active = null` 是单例：整个进程只有一个 sidecar 进程。
所以 retire 与 `lastResetAt` 的作用域是进程级 —— 这一半成立。

**订正（任务书 §4 F10）**：原文接着说「一份 socket 池」，不准确。sidecar 内部
`scripts/opencode-transport.mjs:43` 是 `sessions = new Map()`，按 `proxy` 分键各建一个
`Bun.FetchSession`，出口之间并不共享 socket 池。因此「按出口分键」不是物理上不成立，而是：
重启与冷却节流是进程级的，按出口分键会造出「这条冷却、那条不冷却，但共用同一次重启」的
半真状态。不做的理由是可读性/收益比，不是不可能。
分键会造出「这条出口冷却、那条不冷却，但共用同一个 sidecar」的假象。

真正的缺陷是冷却期返回 `null` 让上层合成 503，而出口可能完全健康。
但改成带 `Retry-After` 的 429 会改变 503 语义、可能触发跨模型切换 ——
是更大的行为变更。现状已被 `_verify_transport_failure` 兜住（探通就不换线）。

### 5.3 B11 —— 修好 B9 之后攻击面自己消失

非 strict 池降级直连会走 Node/undici 的 TLS 指纹，opencode 按指纹返回**假的**
`FreeUsageLimitError` 429，这个 429 又会被 daemon 判成 `rate429` 去标脏 IP。
但降级的只有 `351035a6` / `24de9bf6` 这类非 strict 池，它们能成为候选**正是因为**
同 url 池的碰撞视角。既然同 url 不互为备选，它们进不了候选，路径不可达。
跨仓库改分类判据的风险高于收益。

---

## 6. 判断这类问题的方法

### 6.1 先问「换过去真的会变吗」

任何「切换」「重试」「轮换」类修复，第一步是确认切换前后**外部可观测状态**是否真的不同。
同 `proxyUrl` 换池、同 IP 重连隧道、同账号换 key，都属于「看起来换了、实际没换」。
这一条能挡掉大半无效修复。

### 6.2 沉默的分支要能被读出来

本轮两个缺陷的共同形态是：代码走了一条什么都不做的分支，日志里没有痕迹。
「没有候选」与「换了但失败」在日志里一样，「私有地址被代理了」与「对端慢」也一样。
修法不一定是改行为，有时只是让分支说话。

### 6.3 耗时异常先算预算

235s = 4 × 60s。任何整秒级的异常耗时，先找常量：连接超时、重试次数、退避数组、
冷却窗口。找不到常量再去怀疑网络。

### 6.4 同名函数两份实现是缺陷源

`getEnvProxyUrl` 的两份副本让修复看起来只生效一半。发现同名函数时，
第一件事是确认调用方导入的是哪一份。

### 6.5 部署判据：grep 构建产物里的新增字符串（mtime 不可靠）

`.next` 是构建产物，改源码不生效。原先的判据是「进程启动时刻晚于 `BUILD_ID` 的 mtime」，
**2026-10-28 08:40 实测不可用**（任务书 §4 F8）。证据不是「倒挂」，而是时间戳互相矛盾：

```
同一 pid 的启动时刻：08:40:45（第一次读） / 08:39:31（第二次读，ps 与 /proc 一致）
该进程 stdout 日志文件 birth：08:41:13
BUILD_ID mtime：08:40:48
```

一个进程不可能在 08:39:31 启动却持有 08:41:13 创建的日志文件；四个读数无法同时成立。
原因未查明，这里不编机制（上一版写「构建原地写产物导致 mtime 混淆」是猜测，已删）。
运行中的确实是新代码，这一点由内容与行为证实：构建 chunk 里有本次新增的 `suppressed`，
运行时出现 `(+N suppressed)`，且 6 次 429 探针新增告警行数为 0（被限流）。

可靠判据按强度排序：

1. 从构建产物里 grep 本次改动独有的字符串：`grep -l suppressed .next/standalone/.next/server/chunks/*.js`
2. 运行态行为验证（如日志里出现了只有新代码才会打印的字段）
3. 需要看时间顺序时，先 `rm -rf .next` 再构建，避免原地写导致的 mtime 混淆

教训与 06:36 那次同类：判据本身也会错，别把「顺序对了」当成「代码对了」。

---

## 7. 遗留

| 项 | 为什么留着 |
|---|---|
| ~~`9router1` 对端卡住~~ | 2026-10-28 08:4x 复测对端已恢复（200/1.75s），摘除建议撤回；原因仍不可归因 |
| B8 的 `egressRetried` 标记 | 需要先确认三个消费方不按结构相等判断返回值 |
| 修复后 30 分钟窗口的成功率 | 修复前的 108/0 已无法复核（`requestDetails` 按 `settings.observabilityMaxRecords`（当前 2000 行，代码默认 200）滚动裁剪（`requestDetailsRepo.js:128`））；当前该连接最近 10 条为 7 成功 3 失败 |

---

## 8. 对抗审查后的订正索引

本文所有订正都源于对实现的逐条复核，明细（含复现命令）见任务书 §4。改动清单：

| 小节 | 订正内容 |
|---|---|
| 1.2 | sing-box 是静态 `selector`，不是「IP 在下面轮换」 |
| 2.1 | 进程里 `NO_PROXY` / `no_proxy` 两个变量并存且值不同 |
| 2.3 | 节点数 28 + 20，不是 83；`opencode.ai` 单独走 `opencode-out` |
| 3 | 249s vs 235.5s 的缺口写明；本节已被 `55f31496` 取代 |
| 5.2 | sidecar 单例成立，但 socket 池按 proxy 分键 |
| 6.5 | mtime 判据失效，改为 grep 构建产物 |
