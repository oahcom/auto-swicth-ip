# 项目文档

## 代理架构

```
Claude Code / AI Client
    ↓ HTTP/HTTPS
9router MITM (port 20128)
    ↓ https.request → proxy-global.js (NODE_OPTIONS --require)
    │
    ├── Layer 1: CONNECT tunnel → sing-box:7890
    │   → ProxyHttpsAgent 替换 https.globalAgent
    │   → tlsTunnel/connectThroughProxy 建 CONNECT 隧道
    │
    ├── Layer 2: 429/5xx/空响应 → 3 次重试 + 切节点
    │   → triggerNodeSwitch() → Clash API PUT /proxies/proxy
    │   → 空 body / SSE 错误 / 200 error-body 都触发
    │   → sleep(500*attempt) 指数退避
    │   → 重试失败返回 502 proxy-global_error
    │
    ├── Layer 3: BYPASS mode
    │   → sing-box 不可达（3 次 CONNECT 失败）
    │   → proxyEnabled=false，直连
    │   → 每 10s health check 恢复
    │
    └── bad-node tracking
        → .bad_nodes.json 共享文件（dict {expire, reason}）
        → daemon.py 独占写入，proxy-global.js 只读
        → _markBadEntry 统一接口（daemon 端写入）
        → per-key 80% 安全阀（nodeTag vs host:port-any 分开统计）
        → isBadNode 过滤 host:port-any 格式

sing-box (port 7890)
    ← Clash API (port 9090, Bearer auth)
    → 56 anytls + 1 trojan 节点池
    → 代理轮换：daemon / proxy-global.js 共用同一套 API

daemon.py
    ← 读 9router SQLite requestDetails（只查 provider="opencode"）
    → 两层检测：10条窗口错误率 > 10% 或 连续失败 ≥ 2 → 立即切
    → 主动轮换：每 60s 加权选新节点
    → 自适应策略：normal=延迟优先 / pressure=均匀分散
    → 节点健康检查：每 300s 并发测试（_health_pool, _Dead_TTL_STAGES）
    → sing-box watchdog：重启检测
    → 死节点导出：cleanup_dead_ttl() 写 _DEAD_NODES 到共享文件
    → 时间戳统一：_now_ts() int 秒（与 JS 对齐）
    → 订阅刷新：每 1h 后台执行，清理旧 node_info
    → 节点调用计数重置：每 20min（pressure 模式跳过）
    → 健康检查前从 Clash API 实时刷新节点列表，避免缓存陈旧
    → Sing-box watchdog：重启计数受 _RESTART_LOCK 保护
    → _RESTART_COUNT 受 _RESTART_LOCK 保护，防多线程竞争
    → 线程池优雅关闭：wait=True + 10s timeout
    → SQLite 连接优雅关闭：atexit 关闭 WAL checkpoint
```

## 检测与触发机制

```
proxy-global.js 侧（请求级，低延迟）:
  - 200 空 body → 切节点重试
  - 200 SSE error chunk → 切节点重试
  - 200 SSE 全空 → 切节点重试（_peekSseStream 超时 2s）
  - HTTP 429/500+ → 切节点重试
  - 网络错误 → 切节点重试
  - BYPASS: CONNECT 3 次失败 → 直连 10s 恢复
  - server_error / upstream / connection 等 error 体 → 重试

daemon.py 侧（聚合级，高延迟）:
  - 两层检测: RECENT_WINDOW 窗口错误率 or 连续失败 ≥ 2
  - 只查 provider="opencode"（OpenCode Free）
  - 不查 kiro/nvidia/ollama 等（不触发 IP 轮换）
  - 结果: 错误模型名日志（如 "mimo-v2.5-free"）
  - 健康检查前从 Clash API 实时刷新节点列表，避免缓存陈旧
```

## 共享状态

| 资源 | 写入者 | 读取者 | 格式 |
|------|--------|--------|------|
| `.bad_nodes.json` | **daemon.py 独占写入** (dict) | proxy-global.js (只读) | `{"host:port|node-name": {"expire": int, "reason": str}}` |
| `sing-box Clash API` | daemon + proxy-global.js 切换节点 | 双方 | REST `/proxies/proxy` |
| `9router data.sqlite.requestDetails` | 9router | daemon 读 | provider="opencode" |

## 配置（config.py）

```python
OPENCODE_FREE_PROVIDER = "opencode"            # 9router provider key
opencode_min_success_rate: 0.9                 # 错误率 > 10% → 轮换
pressure_error_rate: 0.3                       # 正常→压力模式阈值
normal_error_rate: 0.1                         # 压力→正常模式阈值
proactive_rotate_sec: 60                       # 主动轮换间隔
cooldown_after_switch_sec: 10                  # 切换后冷却
node_health_sec: 300                           # 健康检查间隔 (5min)
node_test_timeout_ms: 15000                    # 节点测试超时
singbox.max_restart_attempts: 3                # 最大重启次数
bad_nodes.ttl_sec: 300                         # bad node TTL (5min)
_DEAD_TTL_STAGES: [60, 120, 300, 600]          # 指数退避 TTL
_pool_POOL_MIN_RATIO: 0.3                      # 允许 70% 节点被拉黑
```

## proxy-global.js 关键设计（当前迭代）

| 修复 | 说明 |
|------|------|
| `_nodeCount` 显式声明 | `let _nodeCount = 56`，启动时从 Clash API 初始化 |
| `_markBadEntry` 共享函数 | 统一 bad node 标记逻辑，per-key 80% 安全阀 |
| `triggerNodeSwitch` 并发保护 | `_switching` Promise chain，并发调用者等待切换完成 |
| PUT 响应校验 | `putRes.ok` 检查，失败返回 false |
| 重试退避 | `sleep(500*attempt)` 指数退避 |
| URL 校验 | `new URL(proxyUrl)` try/catch fallback |
| `fakeReq.write` guard | `writableEnded` 检查 |
| TOCTOU 修复 | `loadBadNodes` 先 stat 再读，读完二次确认 |
| TTL/路径统一 | 支持 `BAD_NODES_TTL` / `BAD_NODES_FILE_PATH` 环境变量 |
| `tlsTunnel` 记录成功 | CONNECT 成功调 `recordProxyConnectResult(true)` |
| `makeRetryableRequest` 429/5xx | else 分支加 status 检查 + 重试 |
| **server_error 识别** | `_shouldRetryFetch` regex 加 `server_error\|upstream\|connection` |
| **SSE error 同步** | `_isSseChunkError` 同步加 `server_error\|upstream` |
| **锁竞争修复** | `triggerNodeSwitch` 返回 false → 直接返回 502 |
| **`res.headers` scope 修复** | error handler 用 `_headersOnError \|\| {}` 而非 `res.headers` |
| **过度匹配修复** | `_shouldRetryFetch` 只匹配 `error.message` 而非整个对象 |
| **console.log 数组修复** | 去掉数组括号 |
| `_markBadEntry` 只读 | `proxy-global.js` 只读，daemon.py 独占写入 |
| `secret` 权限检查 | `_readSingboxSecret` 加 `stat.mode & 0o077` 警告 |
| `secret` 自动刷新 | mtime 检测自动刷新 |
| `triggerNodeSwitch` 并发等待 | Promise chain，并发调用者等待切换完成 |
| Socket body 消费 | `res.resume()` 防止泄漏 |
| `markBadNode/Singbox` 写 dict | 统一格式 `{expire, reason}` + 清理过期 |
| `loadBadNodes` TOCTOU | 先 stat 命中缓存，读完二次确认 mtime |
| `triggerNodeSwitch` PUT 校验 | `putRes.ok` 检查 |
| `_shouldRetryFetch` regex | 同步追加 `streaming response` |
| SSE `_isSseChunkError` regex | 同步追加 `server_error\|upstream` |
| `res.headers` scope 修复 | error handler 用 `_headersOnError \|\| {}` |
| 过度匹配修复 | 改 `String(error.message)` 仅匹配 message |
| `console.log` 数组修复 | 去掉数组括号 |
| `_markBadEntry` 只读 | `proxy-global.js` 只读，daemon.py 独占写入 |

## daemon.py 关键设计（当前迭代）

| 修复 | 说明 |
|------|------|
| 错误检测只查 opencode | 排除 kiro/nvidia/ollama 干扰 |
| 连续失败检测 | `consecutive_errors ≥ 2` 立即触发轮换 |
| 时间戳统一 | `_now_ts()` int 秒（与 proxy-global.js 对齐）|
| 死节点导出 | `cleanup_dead_ttl()` 导出到 `.bad_nodes.json` |
| 死变量清理 | `_last_switch_country` / `last_switch_ts` 删除 |
| 静默吞异常修复 | `test_node_delay` / `_load_shared_bad_nodes` 加 log |
| 缓存时序修复 | `_shared_bad_check_ts` 移到缓存成功填充后 |
| NULL guard | `json_set(COALESCE(data, '{}'), ...)` |
| 自适应策略 | `_p2c_select` 支持 normal/pressure 双模式 |
| 正常执行无需 sql 注释 | 原本代码中的sql注释是代码注释而不是sql注释 |
| CLI 入口重复 | 清理死代码 `cli_rotate` 重复调用 |
| 优雅关闭 | SIGTERM handler + SystemExit |
| 线程池优雅关闭 | `wait=True` + 10s timeout |
| SQLite 连接关闭 | atexit 关闭 WAL checkpoint |
| 订阅刷新清理旧节点 | 每 10min 清理 `node_info` 过期记录 |
| 健康检查前刷新节点列表 | 直接从 Clash API 刷新，避免缓存陈旧 |
| `_RESTART_COUNT` 线程安全 | `_RESTART_LOCK` 保护多线程读写 |

## 关键文件

| 文件 | 行数 | 说明 |
|------|------|------|
| `~/.9router/proxy-global.js` | ~790 | 三层代理补丁（CONNECT + 重试 + BYPASS）|
| `~/.9router/bootstrap-undici.js` | ~50 | NODE_OPTIONS 入口 |
| `daemon.py` | ~1200 | 节点管理 + 错误检测 + 轮换 |
| `config.py` | ~90 | 共享配置常量 |
| `proxy_pool.py` | ~280 | 请求级代理池（按节点加权）|
| `singbox-config.json` | ~600 | sing-box 代理节点配置 |
| `.bad_nodes.json` | 动态 | 坏节点共享文件 |

## provider 多样性约束（2026-07-08 确认）

**根因**：目前 `9router_hermes` → `op` combo 仅包含 3 个 opencode.ai 免费模型（`oc/deepseek-v4-flash-free`、`oc/nemotron-3-ultra-free`、`oc/mimo-v2.5-free`），全走同一 Public 账户。

**限制**：其他 provider（nvidia、openrouter、pollinations、alicode 等）虽配置了 API key，但有硬配额限制，**无法通过代理 IP 轮换绕过配额**。只有 opencode.ai 的免费接口能通过 sing-box 代理轮换 IP 来绕过限额。

**结论**：不能简单在 combo 添加其他 provider 作为 fallback，因为一旦 opencode 配额耗尽，fallback 到的 provider 同样会立即触发配额限制。根本解决路径是：
1. 维护 sing-box 节点池健康（确保 IP 轮换有效）
2. 优化轮换策略（更快切换、更准的错误检测）
3. 如需 provider 多样性，必须获取可付费/可续费的 API key（alicode renew、openrouter 付费等）

## 测试工作流要求

**所有代码变更必须经过 Codex CLI 审查流程：**

1. **代码修改后** → 运行 `codex review` 对变更文件进行审查
2. **测试文档更新** → TEST_PLAN.md 同步更新测试点
3. **语法验证** → `node --check proxy-global.js` && `python3 -m py_compile daemon.py`
4. **Codex CLI 审查通过** → 输出 `PASS` 或 `PASS_WITH_NOTES` 才可合并
5. **测试执行** → 按 TEST_PLAN.md 执行 P0/P1 测试点

**禁止**：
- 直接修改代码跳过 Codex CLI 审查
- 直接修改 TEST_PLAN.md 而未同步代码变更
- 跳过语法验证直接提交
