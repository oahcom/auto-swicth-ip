# 基础层延迟优化：前缀缓存 × env 稳定性

## 一、测量：延迟都花在哪（00:00–05:26，5185 请求）

```
POST 进入 ──[发送前管线]──▶ PROXY 发出 ──[上传+上游 prefill]──▶ 首字节 ──[生成]──▶ DONE
            p50=3.0s                     p50≈9.5s (TTFT扣管线)          视 OUT
```

- 发送前管线 p50=3.0s，但**没有 headroom 超时的请求 p50=0.0s**——3s 全是 headroom 服务挂掉后的 AbortSignal 超时（现已修复，税消失；建议加熔断防复发）。
- TTFT 从 POST 时刻起算（`ttftAt - requestStartTime`），管线耗时直接进 TTFT。

## 二、核心发现：上游前缀缓存被我们自己打断

CACHE ↻（上游 usage.cached_tokens）命中率随输入规模暴跌：

| 输入规模 | 命中率 p50 | 解释 |
|---|---|---|
| 0–50K | **65.4%** | 小请求，命中值就是那个常数 |
| 50–100K | 16.1% | |
| 100–150K | 7.8% | 10 万 token 历史几乎全量重算 |
| 150–200K | 6.2% | |
| 200–250K | 5.4% | |

- CACHE 值 p50=**8960** 是常数 = 系统提示词头（缓存断点之前的部分）。
- **决定性旁证**：max=179840——自带 `<env>` 被透传的真实 opencode 客户端（提示词稳定）在同一上游吃到 17.9 万缓存。上游前缀缓存完全正常。

**根因**：`opencodeFingerprint.js` 的 `applyEnvFingerprint` 每次调用 `envFingerprintPrompt()` → `envWorkdir()` 用 CSPRNG **逐请求随机生成 workdir** 路径（INC-018 防拉黑引入）。`<env>` 块位于系统提示词**尾部**——它每发一变，前缀匹配就断在那里，后面的工具+全部会话历史（100–250K）每条请求全量重算 prefill。这就是上游侧 p50 9.5s 的主体。

## 三、修复（已部署，MATCH `_j5zscm8J`）

`envWorkdirFor(conversationKey)`：FNV-1a 从**首条用户侧消息**推导路径（base + 3 位后缀，形状不变）：

- **同会话字节恒定** → 上游缓存命中整段历史（只算新尾部 ~8K 而不是 150K）；
- **跨会话多样** → INC-018 防拉黑保留（几百条各自只活一个会话的假项目路径收不进静态黑名单；真实 CLI 会话 cwd 本来就跨轮次不变，逐请求变反而是破绽）；
- 无锚点（探测/无消息体）回退随机；`OPENCODE_ENV_WORKING_DIR` 钉死开关保留。

测试：新增 `opencode-env-stability.test.js` 7 例（同会话字节恒定/跨会话不同/100 次推导恒定/透传不改写/flat 形状），fingerprint+asip 簇 89/89 绿。

**预期效果**（可从长窗观测验证——看 CACHE ↻ 是否出现随会话增长的 30K–150K 命中）：

| 指标 | 修复前 | 预期 |
|---|---|---|
| 大请求缓存命中率 | 5–8% | ~90%+（同会话内） |
| 上游侧 TTFT p50 | ~9.5s | ~2–3s |
| C 类（TTFT 慢）>20s | 577 条 | 大幅收敛 |

## 四、其余基础层观察（按收益排）

1. **headroom 3s 税**（已随服务修复消失）：建议 headroom.js 加连续失败熔断（3 次失败→60s 直接跳过）防复发——归你。
2. **上传带宽**：100–250KB JSON × ~10 并发共享一条 WARP 隧道，上传排队 ~0.2–0.5s。请求体 gzip（若上游接受 Content-Encoding）可再省一点，收益小。
3. **换线后连接预热**：rotate 后新闸口首个请求吃满 TCP+TLS 握手（经 WARP ~300–600ms）。可在换线后后台 preconnect 预热，省掉临界路径上的握手。
4. **不可无损压缩的**：新 token 的 prefill + 生成（D 类 605 条）是模型本体耗时；THINK:high 的思考时间也在 TTFT 里。

## 五、改动文件

- `open-sse/utils/opencodeFingerprint.js`：envWorkdirFor + conversationAnchor + 注入点改造
- `tests/unit/opencode-env-stability.test.js`：新增
- （上一轮）`opencode.js` borrowed/holder 修复、`daemon.py` already_retired veto——均已部署
- 全部**未 commit/push**，要推 97/zeus 说一声
