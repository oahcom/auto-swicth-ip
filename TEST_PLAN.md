# Auto-Switch-IP 测试工作流文档

> 文档版本: v1.3
> 最后更新: 2026-10-16
> 测试对象: 9router proxy-global.js + daemon 代理自动切换系统
> 订阅链路与轮换健康的验证资产见 §10（`test_subscription_parse.py` / `verify_rotation.py`）

> ⚠ 文档漂移提示（2026-08-22）：第 9 节 `switch-proxy.py` / `proxy_pool.py` 验证用例（T-091~T-098）依赖的模块已在 Round 1 删除（无生产调用方）。其覆盖点已并入 `daemon.py` 与新增 `test_daemon_unit.py` 的确定性单测。第 9 节保留为历史归档，不再执行。

---

## 1. 测试范围和目标

### 1.1 测试范围

本测试计划覆盖 auto-switch-ip 系统的全部功能模块：

| 模块 | 覆盖组件 | 测试点数 |
|------|----------|----------|
| 代理链路 (proxy-chain) | proxy-global.js CONNECT 隧道、NO_PROXY、SQLite 配置加载、secret 管理 | T-001~T-006, T-071~T-072 |
| 错误检测 (error-detection) | daemon get_9router_error_rate、窗口统计、冷却期 | T-007~T-012 |
| 节点轮换 (node-rotation) | P2C 选择算法、去重、候选筛选、计数管理 | T-013~T-017 |
| 重试机制 (retry-mechanism) | HTTP 429/5xx、空 body、SSE 空流/error chunk、网络错误、重试耗尽 | T-018~T-026 |
| 共享状态 (shared-state) | bad_nodes.json 读写、TOCTOU 防护、mtime 缓存 | T-027~T-031 |
| 节点健康 (node-health) | 批量延迟测试、死节点 TTL 指数退避、保底机制、延迟评分 | T-032~T-037 |
| sing-box 看门狗 (singbox-watchdog) | Clash API 双检存活、重启流程、重启次数上限、代理开关原子性、日志轮转 | T-038~T-042 |
| BYPASS 模式 (bypass) | 3 次 CONNECT 失败进入、健康检查恢复、计数器衰减 | T-043~T-045 |
| 降级策略 (fallback) | 全部节点死亡降级 okz、节点恢复回切、订阅刷新 | T-046~T-049 |
| 并发安全 (concurrency) | retry 隔离、并发 Clash API 调用、RLock、线程池优雅关闭 | T-050~T-053 |
| 策略模式 (strategy) | 正常/压力模式切换、带回滞的阈值、调用计数 | T-054~T-056 |
| 异常场景 (anomaly) | sing-box 崩溃、批量节点死亡、SQLite 不可用、文件写入失败、JSON 损坏 | T-057~T-063 |
| fetch 包装 (fetch-retry) | globalThis.fetch 包装、SSE peek 超时、Dispatcher 请求 | T-064~T-066 |
| 边界情况 (edge-case) | 窗口样本不足、全零延迟、非瞬态错误排除、空 secret、secret 刷新 | T-067~T-070 |

### 1.2 测试目标

1. 验证代理链路端到端完整工作，请求经 9router -> proxy-global.js -> sing-box -> 出口节点的每个环节无误。
2. 验证错误检测窗口准确计算错误率，并在阈值达标时触发节点切换。
3. 验证节点轮换算法（P2C、同 IP 去重、候选不足回退）按预期工作。
4. 验证所有重试条件（429、5xx、空 body、SSE 空流/error chunk、网络错误）均触发节点切换 + 重试，耗尽后返回 502。
5. 验证 daemon 和 proxy-global.js 通过 bad_nodes.json 共享状态，包含 TOCTOU 防护。
6. 验证节点健康管理（延迟测试、死节点 TTL 指数退避、保底机制）。
7. 验证 sing-box watchdog 在崩溃后自动重启，重启次数上限后降级到 fallback 代理。
8. 验证 BYPASS 模式在 CONNECT 连续失败 3 次后进入，恢复后自动退出。
9. 验证全部节点死亡时降级到 okz:6696，节点恢复后自动回切到 sing-box。
10. 验证并发场景下的隔离性和安全性（RLock、retry 隔离、_switching 去重）。

---

## 2. 测试环境要求

### 2.1 硬件/软件要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Linux（WSL2 或原生 Ubuntu 22.04+） |
| Node.js | v18+（proxy-global.js 运行环境） |
| Python | 3.10+（daemon 运行环境） |
| sing-box | 最新稳定版（Clash API 兼容） |
| 9router | 正常运行 |
| SQLite | 3.x（data.sqlite 读/写） |
| 磁盘 | 100MB+ 可用空间（日志 + 测试数据） |

### 2.2 服务/端口依赖

| 服务 | 端口 | 用途 |
|------|------|------|
| 9router | 20128 | 模型路由 API |
| sing-box Clash API | 9090 | 代理管理 REST API |
| sing-box mixed port | 7890 | SOCKS5/HTTP 混合代理 |
| proxy-global.js | 动态 | 请求转发 CONNECT 隧道 |
| mock HTTP 服务器 | 18443（测试） | 模拟目标 API（因 proxy-global.js CONNECT 隧道透传，mock 只需 HTTP） |
| fallback 代理 okz | 6696 | 降级目标 |
| 9router data.sqlite | - | provider 错误记录读/写 |

### 2.3 工具要求

| 工具 | 用途 |
|------|------|
| curl / wscat | HTTP/WebSocket 请求发送 |
| ncat / nc | TCP 端口检测 |
| sqlite3 | SQLite 数据操作 |
| iptables / nftables | 网络故障注入 |
| jq | JSON 解析 |
| parallel | 并发请求测试 |
| pkill / fuser | 进程管理 |
| node | proxy-global.js 单元测试执行 |

### 2.4 配置文件/路径

| 文件 | 用途 |
|------|------|
| `~/.hermes/9router/config/data.sqlite` | 9router 持久化数据 |
| `~/.hermes/9router/config/singbox-config.json` | sing-box 运行时配置 |
| `~/.singbox_secret` | sing-box 认证密钥（mode 600） |
| `.bad_nodes.json` | daemon 与 proxy-global 共享的坏节点列表 |
| `singbox.log` | sing-box 运行日志 |
| `config.yaml` | 9router 配置（绝对禁止 yaml.dump 重写） |

---

## 3. 测试点清单

### 3.1 按优先级分组

#### P0（关键路径，阻塞发布）— 24 项

| ID | 名称 | 模块 | 类型 |
|----|------|------|------|
| T-001 | 请求完整链路过代理 | proxy-chain | e2e |
| T-002 | CONNECT 隧道建立 | proxy-chain | unit |
| T-003 | NO_PROXY 私网地址绕过 | proxy-chain | unit |
| T-007 | SQLite 错误率窗口检测 | error-detection | unit |
| T-008 | 错误率 > 10% 触发轮换 | error-detection | integration |
| T-009 | 连续失败 >= 2 触发轮换 | error-detection | integration |
| T-013 | 主动轮换每 60s P2C 选节点 | node-rotation | integration |
| T-018 | HTTP 429/5xx 触发节点切换+重试 | retry-mechanism | unit |
| T-019 | 空 200 响应体触发节点切换+重试 | retry-mechanism | unit |
| T-020 | SSE 空流触发节点切换+重试 | retry-mechanism | unit |
| T-021 | SSE error chunk 触发节点切换+重试 | retry-mechanism | unit |
| T-027 | daemon 写 bad_nodes 共享文件 | shared-state | integration |
| T-028 | proxy-global 读 bad_nodes 共享文件 | shared-state | integration |
| T-032 | 批量节点延迟测试 | node-health | integration |
| T-033 | 死节点 TTL 指数退避 | node-health | unit |
| T-038 | Clash API + 端口双检存活 | singbox-watchdog | integration |
| T-039 | watchdog 重启流程 | singbox-watchdog | integration |
| T-043 | BYPASS 模式进入（3 次 CONNECT 失败） | bypass | integration |
| T-044 | BYPASS 恢复健康检查 | bypass | integration |
| T-046 | 全部节点死亡降级 okz | fallback | e2e |
| T-047 | 节点恢复后从 okz 切回 sing-box | fallback | e2e |
| T-057 | sing-box 进程崩溃恢复 | anomaly | e2e |
| T-058 | 全部 anytls 节点死亡恢复 | anomaly | e2e |
| T-059 | 9router SQLite 不可用 | anomaly | integration |

#### P1（重要功能）— 24 项

| ID | 名称 | 模块 | 类型 |
|----|------|------|------|
| T-004 | proxyUrl 从 SQLite 加载 | proxy-chain | integration |
| T-005 | proxyUrl 无效回退 | proxy-chain | unit |
| T-010 | 仅 opencode provider 触发轮换 | error-detection | integration |
| T-011 | 空结果窗口处理 | error-detection | unit |
| T-012 | 冷却期内跳过切换 | error-detection | integration |
| T-014 | 错误触发轮换不计入调用次数 | node-rotation | integration |
| T-015 | P2C 选择算法 correct | node-rotation | unit |
| T-016 | 同 IP 去重保留最低调用节点 | node-rotation | unit |
| T-022 | 网络错误触发节点切换+重试 | retry-mechanism | unit |
| T-023 | 200 error body 触发重试 | retry-mechanism | unit |
| T-024 | 重试耗尽返回 502 | retry-mechanism | unit |
| T-025 | triggerNodeSwitch 并发去重 | retry-mechanism | unit |
| T-026 | 节点切换锁定后返回 502 | retry-mechanism | unit |
| T-029 | TOCTOU 防护：stat 缓存 + 二次确认 | shared-state | unit |
| T-034 | 保底机制跳过拉黑 | node-health | unit |
| T-035 | TTL 过期释放死节点 | node-health | unit |
| T-036 | 候选不足时复活过期死节点 | node-health | integration |
| T-040 | 重启次数上限及 fallback | singbox-watchdog | integration |
| T-041 | 重启前后代理开关原子性 | singbox-watchdog | integration |
| T-045 | BYPASS 计数器衰减 | bypass | unit |
| T-048 | 订阅刷新+重启 sing-box | fallback | integration |
| T-049 | 订阅刷新失败不阻塞 | fallback | integration |
| T-050 | 并发请求 proxy-global retry 隔离 | concurrency | integration |
| T-051 | daemon + proxy-global 并发调用 Clash API | concurrency | integration |
| T-052 | _db_lock 重入安全性 | concurrency | unit |
| T-054 | 正常->压力模式切换（30% 阈值带回滞） | strategy | integration |
| T-055 | 压力->正常模式切换（10% 阈值带回滞） | strategy | integration |
| T-060 | bad_nodes.json 写入失败 | anomaly | unit |
| T-061 | bad_nodes.json 格式损坏 | anomaly | unit |
| T-062 | 节点批测全部超时 | anomaly | integration |
| T-064 | globalThis.fetch 包装与 retry | fetch-retry | integration |
| T-065 | SSE peek 超时 2s | fetch-retry | unit |

#### P2（边缘/健壮性）— 22 项

| ID | 名称 | 模块 | 类型 |
|----|------|------|------|
| T-006 | sing-box secret 读取与权限检查 | proxy-chain | unit |
| T-017 | 候选不足时回退行为 | node-rotation | unit |
| T-030 | daemon 导入 proxy-global 坏节点 | shared-state | integration |
| T-031 | 坏节点 mtime 缓存 | shared-state | unit |
| T-037 | 延迟评分函数 | node-health | unit |
| T-042 | sing-box 日志轮转 | singbox-watchdog | unit |
| T-053 | 线程池优雅关闭 | concurrency | integration |
| T-056 | 压力模式下调用计数重置跳过 | strategy | integration |
| T-063 | WAL checkpoint 失败 | anomaly | unit |
| T-066 | Dispatcher 请求状态检查 | fetch-retry | unit |
| T-067 | 窗口检测总请求数略低于阈值 | edge-case | unit |
| T-068 | 延迟评分 all-zero 延迟 | edge-case | unit |
| T-069 | retry 匹配排除 auth/bad-request | edge-case | unit |
| T-070 | sing-box secret 空文件 | edge-case | unit |
| T-071 | proxy-global 初始节点数从 Clash API 刷新 | proxy-chain | integration |
| T-072 | Secret mtime 自动刷新 | proxy-chain | unit |

### 3.2 按模块分组

#### proxy-chain（8 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-001 | 请求完整链路过代理 | P0 | e2e |
| T-002 | CONNECT 隧道建立 | P0 | unit |
| T-003 | NO_PROXY 私网地址绕过 | P0 | unit |
| T-004 | proxyUrl 从 SQLite 加载 | P1 | integration |
| T-005 | proxyUrl 无效回退 | P1 | unit |
| T-006 | sing-box secret 读取与权限检查 | P2 | unit |
| T-071 | proxy-global 初始节点数从 Clash API 刷新 | P2 | integration |
| T-072 | Secret mtime 自动刷新 | P2 | unit |

#### error-detection（6 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-007 | SQLite 错误率窗口检测 | P0 | unit |
| T-008 | 错误率 > 10% 触发轮换 | P0 | integration |
| T-009 | 连续失败 >= 2 触发轮换 | P0 | integration |
| T-010 | 仅 opencode provider 触发轮换 | P1 | integration |
| T-011 | 空结果窗口处理 | P1 | unit |
| T-012 | 冷却期内跳过切换 | P1 | integration |

#### node-rotation（5 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-013 | 主动轮换每 60s P2C 选节点 | P0 | integration |
| T-014 | 错误触发轮换不计入调用次数 | P1 | integration |
| T-015 | P2C 选择算法 correct | P1 | unit |
| T-016 | 同 IP 去重保留最低调用节点 | P1 | unit |
| T-017 | 候选不足时回退行为 | P2 | unit |

#### retry-mechanism（9 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-018 | HTTP 429/5xx 触发节点切换+重试 | P0 | unit |
| T-019 | 空 200 响应体触发节点切换+重试 | P0 | unit |
| T-020 | SSE 空流触发节点切换+重试 | P0 | unit |
| T-021 | SSE error chunk 触发节点切换+重试 | P0 | unit |
| T-022 | 网络错误触发节点切换+重试 | P1 | unit |
| T-023 | 200 error body 触发重试 | P1 | unit |
| T-024 | 重试耗尽返回 502 | P1 | unit |
| T-025 | triggerNodeSwitch 并发去重 | P1 | unit |
| T-026 | 节点切换锁定后返回 502 | P1 | unit |

#### shared-state（5 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-027 | daemon 写 bad_nodes 共享文件 | P0 | integration |
| T-028 | proxy-global 读 bad_nodes 共享文件 | P0 | integration |
| T-029 | TOCTOU 防护：stat 缓存 + 二次确认 | P1 | unit |
| T-030 | daemon 导入 proxy-global 坏节点 | P2 | integration |
| T-031 | 坏节点 mtime 缓存 | P2 | unit |

#### node-health（6 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-032 | 批量节点延迟测试 | P0 | integration |
| T-033 | 死节点 TTL 指数退避 | P0 | unit |
| T-034 | 保底机制跳过拉黑 | P1 | unit |
| T-035 | TTL 过期释放死节点 | P1 | unit |
| T-036 | 候选不足时复活过期死节点 | P1 | integration |
| T-037 | 延迟评分函数 | P2 | unit |

#### singbox-watchdog（5 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-038 | Clash API + 端口双检存活 | P0 | integration |
| T-039 | watchdog 重启流程 | P0 | integration |
| T-040 | 重启次数上限及 fallback | P1 | integration |
| T-041 | 重启前后代理开关原子性 | P1 | integration |
| T-042 | sing-box 日志轮转 | P2 | unit |

#### bypass（3 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-043 | BYPASS 模式进入（3 次 CONNECT 失败） | P0 | integration |
| T-044 | BYPASS 恢复健康检查 | P0 | integration |
| T-045 | BYPASS 计数器衰减 | P1 | unit |

#### fallback（4 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-046 | 全部节点死亡降级 okz | P0 | e2e |
| T-047 | 节点恢复后从 okz 切回 sing-box | P0 | e2e |
| T-048 | 订阅刷新+重启 sing-box | P1 | integration |
| T-049 | 订阅刷新失败不阻塞 | P1 | integration |

#### concurrency（4 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-050 | 并发请求 proxy-global retry 隔离 | P1 | integration |
| T-051 | daemon + proxy-global 并发调用 Clash API | P1 | integration |
| T-052 | _db_lock 重入安全性 | P1 | unit |
| T-053 | 线程池优雅关闭 | P2 | integration |

#### strategy（3 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-054 | 正常->压力模式切换（30% 阈值带回滞） | P1 | integration |
| T-055 | 压力->正常模式切换（10% 阈值带回滞） | P1 | integration |
| T-056 | 压力模式下调用计数重置跳过 | P2 | integration |

#### anomaly（7 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-057 | sing-box 进程崩溃恢复 | P0 | e2e |
| T-058 | 全部 anytls 节点死亡恢复 | P0 | e2e |
| T-059 | 9router SQLite 不可用 | P0 | integration |
| T-060 | bad_nodes.json 写入失败 | P1 | unit |
| T-061 | bad_nodes.json 格式损坏 | P1 | unit |
| T-062 | 节点批测全部超时 | P1 | integration |
| T-063 | WAL checkpoint 失败 | P2 | unit |

#### fetch-retry（3 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-064 | globalThis.fetch 包装与 retry | P1 | integration |
| T-065 | SSE peek 超时 2s | P1 | unit |
| T-066 | Dispatcher 请求状态检查 | P2 | unit |

#### edge-case（4 项）

| ID | 名称 | 优先级 | 类型 |
|----|------|--------|------|
| T-067 | 窗口检测总请求数略低于阈值 | P2 | unit |
| T-068 | 延迟评分 all-zero 延迟 | P2 | unit |
| T-069 | retry 匹配排除 auth/bad-request | P2 | unit |
| T-070 | sing-box secret 空文件 | P2 | unit |

---

## 4. 详细测试用例（P0 级别）

### 4.1 代理链路 (proxy-chain)

---

#### T-001: 请求完整链路过代理

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | e2e |
| 关联模块 | proxy-global.js, sing-box, 9router |

**前置条件**
- mock HTTP 服务器已启动在端口 18443（返回固定 JSON 响应）
- sing-box 运行中且 Clash API 可用（端口 9090）
- 9router 已加载 proxy-global.js
- 当前 outboundProxyUrl=http://127.0.0.1:7890 已备份

**测试步骤**
1. 通过 9router API 发送 AI 客户端请求：
   ```bash
   curl -X POST http://localhost:20128/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"test-model","messages":[{"role":"user","content":"hello"}]}'
   ```
2. 检查 9router 返回的响应内容与 mock 服务器返回一致（状态码 200）。
3. 检查 proxy-global.js 日志显示 CONNECT 隧道成功建立。
4. 测量完整链路时延。
5. 验证 proxy-global.js 未报错（日志无 "Node switch" / "retry" / "502" 关键词）。

**预期结果**
- mock 服务器收到请求，来源 IP 为出口节点 IP（非 127.0.0.1）。
- 9router 正确返回 AI 响应，状态码 200。
- proxy-global.js 日志显示 CONNECT 隧道成功建立。
- 完整链路时延在预期范围内。

**后置条件**
- 关闭 mock 服务器。
- 恢复 outboundProxyUrl 至备份值。
- 清理 9router 请求日志。

---

#### T-002: CONNECT 隧道建立

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | unit |
| 关联模块 | proxy-global.js tlsTunnel / connectThroughProxy |

**前置条件**
- mock HTTP 服务器已启动在端口 18443。
- proxy-global.js 运行中。

**测试步骤**
1. 构造 CONNECT 请求发送至 proxy-global.js 监听端口，目标为 `mock-https-server:18443`。
2. 检查握手响应状态码在 200-299 范围内。
3. 验证通过已建立的 CONNECT 隧道发送 HTTP GET 请求可获得正确响应。
4. 验证 connectThroughProxy 正确转发流量到 sing-box:7890。

**预期结果**
- CONNECT 响应状态码在 200-299 范围内。
- tlsTunnel 成功建立 TLS 连接到目标。
- 通过隧道发送的 HTTP GET 请求返回 mock 服务器的正确响应。
- connectThroughProxy 正确转发流量到 sing-box:7890。

**后置条件**
- 关闭 mock 服务器。

---

#### T-003: NO_PROXY 私网地址绕过

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | unit |
| 关联模块 | proxy-global.js shouldUseProxy |

**前置条件**
- 准备 Node.js 测试脚本，导入 proxy-global.js 的 shouldUseProxy 函数。

**测试步骤**
1. 对 shouldUseProxy 传入私网地址测试集：
   - `127.0.0.1:8080`
   - `localhost:3000`
   - `::1`
   - `10.0.0.1:80`
   - `172.16.0.1:443`
   - `192.168.1.1:9090`
   - `169.254.1.1:53`
   - `[::1]:8080`
2. 对每个地址检查返回值是否为 false。
3. 验证公网地址 `8.8.8.8:53`、`1.1.1.1:443` 返回 true。

**预期结果**
- 所有私网地址 shouldUseProxy 返回 false。
- 所有公网地址 shouldUseProxy 返回 true。
- IPv6 括号格式正确处理。
- 匹配规则与 `_NO_PROXY` 列表完全一致。

**后置条件**
- 无。

---

### 4.2 错误检测 (error-detection)

---

#### T-007: SQLite 错误率窗口检测

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | unit |
| 关联模块 | daemon get_9router_error_rate |

**前置条件**
- 创建临时 SQLite 数据库，建 requestDetails 表（含 provider、status、created_at 等字段）。
- 插入 10 条 opencode provider 记录：2 条 status=500，8 条 status=200。
- 确保记录按时间顺序排列。

**测试步骤**
1. 调用 `get_9router_error_rate()`。
2. 验证返回结果格式：`(total, errors, [...], consecutive_N)`。
3. 验证 total=10, errors=2, error_rate=0.2。
4. 插入第 11 条记录，验证窗口限制为最近 10 条。
5. 验证 consecutive_errors 数值正确（取决于最后几条记录的失败顺序）。

**预期结果**
- 返回 `(10, 2, [...], consecutive_N)`。
- 计算 error_rate=0.2 (20%)。
- 11 条时只统计最近 10 条。
- consecutive_errors 反映最后连续失败条数。

**后置条件**
- 清理临时 SQLite。

---

#### T-008: 错误率 > 10% 触发轮换

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | integration |
| 关联模块 | daemon 主循环 + 错误轮换 |

**前置条件**
- 注入 2 条错误记录使窗口错误率 20% > 10%（10 条中 2 条失败）。
- 确保主循环不在 cooldown 期内（`cooldown_until < now`）。

**测试步骤**
1. 执行 daemon 主循环的一次迭代。
2. 检查当前节点是否被加入 `fail_set`。
3. 检查 `pick_next_node` 是否被调用并返回新节点。
4. 检查 `switch_proxy` 是否被调用。
5. 检查 `cooldown_until` 是否更新为 `now + 10s`。

**预期结果**
- 错误率 20% > 10% 阈值触发轮换。
- 当前节点被加入 fail_set。
- pick_next_node 被调用并返回新节点。
- switch_proxy 被调用。
- cooldown_until 被更新为 now + 10s。

**后置条件**
- 清理测试数据。
- 重置 fail_set 和 cooldown。

---

#### T-009: 连续失败 >= 2 触发轮换

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | integration |
| 关联模块 | daemon 主循环 + 错误轮换 |

**前置条件**
- 构造 `total=20, errors=1, consecutive_errors=2` 的场景（最后 2 条连续失败，总体错误率仅 5%）。

**测试步骤**
1. 执行主循环。
2. 验证 `consecutive_errors >= 2` 独立触发轮换，即使总体错误率 5% < 10%。

**预期结果**
- 连续失败 >= 2 独立触发轮换，即使总体错误率 5% < 10%。
- 当前节点被加入 fail_set。
- pick_next_node 和 switch_proxy 被调用。
- cooldown 被设置。

**后置条件**
- 清理测试数据。

---

### 4.3 节点轮换 (node-rotation)

---

#### T-013: 主动轮换每 60s P2C 选节点

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | integration |
| 关联模块 | daemon 主循环 + P2C 选择 |

**前置条件**
- 设置 `proactive_rotate_sec=60`。
- 确保有足够可用候选节点（>=5）。

**测试步骤**
1. 记录当前时间 T0。
2. 等待 60s 或模拟时间推进触发一轮主循环迭代。
3. 检查是否调用了 `pick_next_node` 和 `switch_proxy`。
4. 连续执行 6 次主动轮换，验证 `_recent_nodes` 队列始终保持 `maxlen=5`。

**预期结果**
- 每 60s 主动轮换触发一次。
- pick_next_node 被调用并返回新节点。
- switch_proxy 被调用。
- 选中节点被加入 _recent_nodes。
- _recent_nodes 队列始终保持 maxlen=5，旧节点被移出。

**后置条件**
- 重置 proactive_rotate_sec。
- 清空 _recent_nodes。

---

### 4.4 重试机制 (retry-mechanism)

---

#### T-018: HTTP 429/5xx 触发节点切换+重试

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | unit |
| 关联模块 | proxy-global.js makeRetryableRequest |

**前置条件**
- 配置 mock 上游服务器返回 HTTP 429 Too Many Requests。
- proxy-global.js 正常运行。

**测试步骤**
1. 通过 proxy-global.js 发送一条请求。
2. 检查 429 响应是否触发 `triggerNodeSwitch`。
3. 验证节点切换后重试请求，退避 `sleep(500*attempt)`。
4. 配置 mock 服务器返回 HTTP 502，重复上述检查。

**预期结果**
- 429 响应触发 triggerNodeSwitch。
- 节点切换后重试请求，退避 sleep(500\*attempt) ms。
- 最多重试 MAX_RETRIES(3) 次。
- 500+ 状态码同样触发节点切换+重试。

**后置条件**
- 恢复 mock 服务器正常响应。

---

#### T-019: 空 200 响应体触发节点切换+重试

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | unit |
| 关联模块 | proxy-global.js 空 body 检测 |

**前置条件**
- 配置 mock 服务器返回 200 OK 但 body 为空（Content-Length: 0）。

**测试步骤**
1. 发送一条非流式 HTTP 请求。
2. 检查 `body.length === 0` 是否被识别为空响应。
3. 检查 `triggerNodeSwitch` 和重试是否触发。

**预期结果**
- 非流式响应 body.length === 0 被识别为空响应。
- triggerNodeSwitch 被调用。
- 重试向新节点发起请求。
- 3 次重试耗尽后返回 502。

**后置条件**
- 恢复 mock 服务器正常响应。

---

#### T-020: SSE 空流触发节点切换+重试

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | unit |
| 关联模块 | proxy-global.js _peekSseStream |

**前置条件**
- 配置 mock 服务器 SSE endpoint，建立连接后 3s 内不发送任何数据。

**测试步骤**
1. 发送带 `Accept: text/event-stream` 的请求。
2. 检查 `_peekSseStream` 是否在 2s 超时后返回 `hasData=false`。
3. 检查 `triggerNodeSwitch` + retry。

**预期结果**
- _peekSseStream 设置 AbortSignal.timeout(2000)。
- 2s 超时后 hasData=false。
- triggerNodeSwitch 被调用。
- 重试到新节点。
- 3 次耗尽后返回 502。

**后置条件**
- 恢复 mock 服务器正常 SSE。

---

#### T-021: SSE error chunk 触发节点切换+重试

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | unit |
| 关联模块 | proxy-global.js _isSseChunkError |

**前置条件**
- 配置 mock 服务器 SSE endpoint，chunk 中含 error 字段：
  `data: {"error":{"message":"rate_limit_exceeded"}}`.

**测试步骤**
1. 发送 SSE 请求。
2. 检查 `_isSseChunkError` 是否检测到 error 字段。
3. 检查是否中断当前流并 `triggerNodeSwitch` + retry。
4. 遍历测试所有 error 关键词：rate, limit, quota, exhausted, overload, unavailable, server_error。

**预期结果**
- SSE chunk 含 error 字段时 _isSseChunkError 返回 true。
- 当前 SSE 流被中断。
- triggerNodeSwitch 被调用。
- 节点切换后重试。
- 所有 error 关键词均被正确匹配。

**后置条件**
- 恢复 mock 服务器正常 SSE。

---

### 4.5 共享状态 (shared-state)

---

#### T-027: daemon 写 bad_nodes 共享文件

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | integration |
| 关联模块 | daemon cleanup_dead_ttl |

**前置条件**
- 向 `_DEAD_NODES` 添加几个测试节点。
- 备份现有 bad_nodes.json。

**测试步骤**
1. 调用 `cleanup_dead_ttl`。
2. 检查 `.bad_nodes.json` 文件内容格式。
3. 验证写入使用原子操作（先写临时文件再 rename）。
4. 验证旧条目（非 daemon 管理）被保留。
5. 验证已从 `_DEAD_NODES` 移除的节点从文件中删除。

**预期结果**
- bad_nodes.json 被写入（或更新）。
- 格式为 `{node: {expire: <int>, reason: 'daemon_dead'}}`。
- 旧条目（daemon 未覆盖的）被保留。
- 未在 _DEAD_NODES 中的旧节点从文件中移除。
- 写入使用原子操作（先写临时文件再 rename）。

**后置条件**
- 清理测试节点。
- 恢复 bad_nodes.json。

---

#### T-028: proxy-global 读 bad_nodes 共享文件

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | integration |
| 关联模块 | proxy-global.js loadBadNodes |

**前置条件**
- 准备多种格式的 `.bad_nodes.json`：部分 `{expire, reason}` dict 格式，部分纯数字时间戳格式。
- 确保有已过期和未过期的条目。

**测试步骤**
1. 调用 `loadBadNodes()`。
2. 检查返回值：dict 格式条目是否正常解析。
3. 检查纯数字格式条目是否正常解析。
4. 验证已过期条目被过滤（`expire < now`）。
5. 验证 mtime 缓存命中时跳过文件读取。

**预期结果**
- 兼容 dict 格式 `{expire, reason}` 正常解析。
- 兼容纯数字时间戳格式。
- 已过期条目被过滤掉（expire < now）。
- 未过期条目正常返回。
- mtime 缓存命中时跳过文件读取。

**后置条件**
- 清理测试文件。

---

### 4.6 节点健康 (node-health)

---

#### T-032: 批量节点延迟测试

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | integration |
| 关联模块 | daemon batch_test_nodes |

**前置条件**
- 准备 10 个测试节点（部分可达、部分不可达）。

**测试步骤**
1. 调用 `batch_test_nodes`。
2. 检查 `_NODE_DELAYS` 中存活节点的延迟记录。
3. 检查 `_DEAD_NODES` 中超时/失败节点的加入情况。
4. 检查 `_health_pool` 线程池的并发行为（30 workers）。

**预期结果**
- 可达节点的延迟被记录到 _NODE_DELAYS。
- 不可达节点按指数退避 TTL 加入 _DEAD_NODES。
- 30 workers 并发测试所有节点。
- 测试结果正确反映节点的网络可达性。

**后置条件**
- 清空测试节点列表。

---

#### T-033: 死节点 TTL 指数退避

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | unit |
| 关联模块 | daemon _DEAD_TTL_STAGES |

**前置条件**
- 准备单个测试节点。

**测试步骤**
1. 对同一节点连续触发失败，第 1、2、3、4、5 次。
2. 每次检查该节点在 `_DEAD_NODES` 中的 TTL。
3. 检查 `_dead_fail_count` 的递增。

**预期结果**
- 第 1 次失败：TTL=60s, fail_count=1。
- 第 2 次失败：TTL=120s, fail_count=2。
- 第 3 次失败：TTL=300s, fail_count=3。
- 第 4 次失败：TTL=600s, fail_count=4。
- 第 5 次失败：TTL=永久(inf), fail_count=5。
- `_dead_fail_count[fail_count-1]` 正确映射到 `_DEAD_TTL_STAGES=[60,120,300,600]`。

**后置条件**
- 重置 _dead_fail_count 和 _DEAD_NODES。

---

### 4.7 sing-box 看门狗 (singbox-watchdog)

---

#### T-038: Clash API + 端口双检存活

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | integration |
| 关联模块 | daemon is_singbox_alive |

**前置条件**
- sing-box 正常运行且 Clash API 端口 9090 和 MIXED_PORT 7890 监听。

**测试步骤**
1. 调用 `is_singbox_alive()`。
2. 验证先检查 Clash API `/proxies` 返回 200。
3. 验证再检查 7890 端口 socket 可连接（`nc -z 127.0.0.1 7890`）。
4. 模拟 Clash API 不通（停止 sing-box），再次检查。

**预期结果**
- Clash API 正常时返回 True。
- 7890 端口可达时返回 True。
- 任一失败返回 False。
- 总执行时间不超过 4s（硬超时）。

**后置条件**
- 恢复 sing-box（如果停止）。

---

#### T-039: watchdog 重启流程

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | integration |
| 关联模块 | daemon restart_singbox |

**前置条件**
- sing-box 运行中。
- 备份当前代理配置。

**测试步骤**
1. 触发 `restart_singbox()`。
2. 按顺序验证每个中间步骤的状态。

**预期结果**
完整的重启顺序：
1. 调用 `_set_outbound_proxy(enabled=False)` 停代理。
2. `pkill sing-box` 成功。
3. `sleep 2s` 等待进程退出。
4. `fuser -k 7890/tcp` 释放端口绑定。
5. 日志轮转执行（日志 > 10MB 时备份，否则跳过）。
6. `Popen` 启动新 sing-box 进程。
7. `sleep 10s` 等待服务就绪。
8. `check alive` 验证新进程健康。
9. 调用 `_set_outbound_proxy(url, enabled=True)` 恢复代理。
10. `_RESTART_LOCK` 防止并发重启。

**后置条件**
- 恢复代理配置。
- 确认 sing-box 正常运行。

---

### 4.8 BYPASS 模式 (bypass)

---

#### T-043: BYPASS 模式进入（3 次 CONNECT 失败）

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | integration |
| 关联模块 | proxy-global.js recordProxyConnectResult |

**前置条件**
- 初始化 `connectFailCount=0`。

**测试步骤**
1. 连续 3 次调用 `recordProxyConnectResult(false)`。
2. 每次调用后检查 `connectFailCount` 的累加值。
3. 达到 `BYPASS_THRESHOLD(3)` 后检查 `bypassMode` 和 `proxyEnabled`。
4. 确认后续请求走直连（不经过代理）。

**预期结果**
- 第 1 次 false：connectFailCount=1。
- 第 2 次 false：connectFailCount=2。
- 第 3 次 false：connectFailCount=3 >= BYPASS_THRESHOLD。
- bypassMode=true, proxyEnabled=false。
- 后续请求绕过代理直接发出。

**后置条件**
- 退出 BYPASS 模式。
- 重置 connectFailCount。

---

#### T-044: BYPASS 恢复健康检查

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | integration |
| 关联模块 | proxy-global.js checkBypassHealth / exitBypass |

**前置条件**
- 先进入 BYPASS 模式（3 次 CONNECT 失败）。

**测试步骤**
1. 确认 `setInterval(checkBypassHealth, 10000)` 已启动。
2. 恢复 sing-box 到正常状态。
3. 等待下一次健康检查（最多 11s）。
4. 检查 `bypassMode` 和 `proxyEnabled` 是否恢复。

**预期结果**
- BYPASS 模式下 setInterval 每 10s 执行健康检查。
- 健康检查通过 `net.connect(7890, '127.0.0.1')` 检测 sing-box 恢复。
- 恢复后调用 `exitBypass`。
- exitBypass 设置 bypassMode=false, proxyEnabled=true。
- 后续请求重新走代理。

**后置条件**
- 退出 BYPASS 模式。
- 重启 sing-box。

---

### 4.9 降级策略 (fallback)

---

#### T-046: 全部节点死亡降级 okz

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | e2e |
| 关联模块 | daemon check_and_handle_fallback |

**前置条件**
- 将所有节点标记为 dead（`_DEAD_NODES` 包含全部节点）。
- 备份当前 outboundProxyUrl。

**测试步骤**
1. 调用 `check_and_handle_fallback`。
2. 检查 alive 节点数是否为 0。
3. 检查 `_set_outbound_proxy` 被调用且参数为 `okz:6696`。
4. 验证 9router 的 outboundProxyUrl 被修改为 okz:6696。
5. 检查 `_ALL_DEAD_FALLBACK` 是否为 True。

**预期结果**
- alive 节点数为 0 触发 fallback。
- `_set_outbound_proxy('http://okz:6696/')` 被调用。
- 9router 的 outboundProxyUrl 变为 okz:6696。
- `_ALL_DEAD_FALLBACK=True` 防止重复降级。
- 日志记录 fallback 触发信息。

**后置条件**
- 恢复 outboundProxyUrl。
- 重置 `_ALL_DEAD_FALLBACK`。

---

#### T-047: 节点恢复后从 okz 切回 sing-box

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | e2e |
| 关联模块 | daemon check_and_handle_fallback |

**前置条件**
- `_ALL_DEAD_FALLBACK=True` 且 `outboundProxyUrl=okz:6696`。
- 确保有节点恢复为 alive。

**测试步骤**
1. 调用 `check_and_handle_fallback`。
2. 检查 `_set_outbound_proxy` 被调用且参数为 `sing-box:7890`。
3. 检查 `_ALL_DEAD_FALLBACK` 重置为 False。

**预期结果**
- `_ALL_DEAD_FALLBACK=True` 且有可用节点时触发回切。
- `_set_outbound_proxy('http://127.0.0.1:7890')` 被调用。
- outboundProxyUrl 恢复为 sing-box:7890。
- `_ALL_DEAD_FALLBACK=False`。
- 日志记录从 fallback 恢复。

**后置条件**
- 恢复代理配置。

---

### 4.10 异常场景 (anomaly)

---

#### T-057: sing-box 进程崩溃恢复

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | e2e |
| 关联模块 | daemon watchdog + restart_singbox |

**前置条件**
- sing-box 正常运行且 daemon watchdog 在监控。
- 记录当前 sing-box PID。

**测试步骤**
1. 使用 `pkill -9 sing-box` 模拟崩溃。
2. 等待 daemon 检测到 Clash API 不通（最多 4s 超时）。
3. 检查 daemon 是否调用 `restart_singbox`。
4. 检查 sing-box 是否被重启并恢复正常。

**预期结果**
- `is_singbox_alive` 检测到 Clash API 不通后返回 False。
- watchdog 逻辑触发 `restart_singbox`。
- 新 sing-box 进程启动成功。
- Clash API 恢复可用。
- 7890 端口恢复监听。

**后置条件**
- 确认 sing-box 运行正常。

---

#### T-058: 全部 anytls 节点死亡恢复

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | e2e |
| 关联模块 | daemon 节点轮换 + fallback |

**前置条件**
- 使用 iptables DROP 规则阻断到所有 anytls 节点 ip:port 的流量。

**测试步骤**
1. 运行 daemon 主循环。
2. 检查 `pick_next_node` 是否反复返回 None。
3. 检查 `check_and_handle_fallback` 是否触发。
4. 检查 outboundProxyUrl 是否切换到 okz:6696。

**预期结果**
- 所有 anytls 节点测试超时/失败。
- pick_next_node 返回 None。
- check_and_handle_fallback 触发降级到 okz:6696。
- outboundProxyUrl 指向 okz:6696。
- 日志记录 "all nodes dead fallback"。

**后置条件**
- 清除 iptables 规则。
- 恢复节点可达性。

---

#### T-059: 9router SQLite 不可用

| 属性 | 值 |
|------|-----|
| 优先级 | P0 |
| 类型 | integration |
| 关联模块 | daemon get_9router_error_rate |

**前置条件**
- 模拟 data.sqlite 被锁定（启动长期 SQLite 事务）或文件不存在。

**测试步骤**
1. 调用 `get_9router_error_rate`。
2. 验证返回值为 `(0, 0, [], 0)`。
3. 验证不抛出未捕获异常。
4. 模拟文件不可读（权限错误），重复验证。

**预期结果**
- SQLite 锁定时返回 (0, 0, [], 0)。
- 文件不存在时返回 (0, 0, [], 0)。
- 权限错误时返回 (0, 0, [], 0)。
- 不抛出未捕获异常。
- 主循环继续运行。

**后置条件**
- 恢复 data.sqlite 正常状态。

---

## 5. 集成测试流程

### 5.1 完整链路集成测试

本流程串联多个测试点，验证系统端到端行为。

```
阶段 1: 正常代理链路
  T-001 -> T-002 -> T-003 -> T-071
  确认基础代理链路正常，NO_PROXY 正确旁路私有地址

阶段 2: 错误检测与节点轮换
  T-007 -> T-008 -> T-009 -> T-013
  注入错误记录，验证窗口检测 -> 节点切换

阶段 3: 重试机制
  T-018 -> T-019 -> T-020 -> T-021 -> T-024
  验证所有错误条件触发重试，耗尽后返回 502

阶段 4: 共享状态同步
  T-027 -> T-028 -> T-029
  daemon 写 bad_nodes -> proxy-global 读取

阶段 5: 节点健康管理
  T-032 -> T-033 -> T-035 -> T-036
  批量测试 -> 指数退避 -> 过期释放 -> 候选不足复活

阶段 6: sing-box 看门狗
  T-038 -> T-039 -> T-040 -> T-041
  存活检测 -> 重启 -> 上限降级 -> 代理开关原子性

阶段 7: BYPASS 模式
  T-043 -> T-044 -> T-045
  进入 BYPASS -> 健康检查恢复 -> 计数器衰减

阶段 8: 降级与恢复
  T-046 -> T-047 -> T-048 -> T-049
  全节点死亡降级 -> 恢复回切 -> 订阅刷新

阶段 9: 策略模式
  T-054 -> T-055 -> T-056
  压力模式切入 -> 带回滞切换 -> 调用计数管理

阶段 10: 异常恢复
  T-057 -> T-058 -> T-059 -> T-062
  进程崩溃 -> 全节点死亡 -> SQLite 不可用 -> 批测全超时
```

### 5.2 集成测试前置条件检查清单

```bash
# ── 基础环境确认 ──
curl -s -o /dev/null -w "%{http_code}" http://localhost:20128/health
curl -s -o /dev/null -w "%{http_code}" http://localhost:9090/proxies
nc -z 127.0.0.1 7890 && echo "PORT_7890_OPEN"

# ── proxy-global.js 状态 ──
pgrep -f proxy-global.js > /dev/null && echo "PROXY_GLOBAL_RUNNING"

# ── 9router 配置 ──
sqlite3 ~/.hermes/9router/config/data.sqlite \
  "SELECT key, value FROM settings WHERE key IN ('outboundProxyUrl','outboundProxyEnabled');"

# ── sing-box secret ──
stat -c "%a %n" ~/.singbox_secret

# ── 坏节点文件 ──
cat .bad_nodes.json | jq .
```

### 5.3 集成测试后置清理清单

```bash
# ── 恢复代理配置 ──
sqlite3 ~/.hermes/9router/config/data.sqlite \
  "UPDATE settings SET value='http://127.0.0.1:7890' WHERE key='outboundProxyUrl';"
sqlite3 ~/.hermes/9router/config/data.sqlite \
  "UPDATE settings SET value='1' WHERE key='outboundProxyEnabled';"

# ── 清理测试数据 ──
sqlite3 ~/.hermes/9router/config/data.sqlite \
  "DELETE FROM requestDetails WHERE provider='test_opencode';"

# ── 重置 daemon 状态 ──
rm -f .bad_nodes.json
echo '{}' > .bad_nodes.json

# ── 恢复 sing-box ──
pkill sing-box && sleep 2 && sing-box run -c singbox-config.json &
```

---

## 6. 故障注入测试

### 6.1 网络故障注入

| 场景 | 注入方法 | 预期行为 | 关联测试点 |
|------|----------|----------|-----------|
| 节点不可达 | `iptables -A OUTPUT -d <node_ip> -j DROP` | 节点标记 dead，指数退避 | T-032, T-033 |
| 全节点不可达 | `iptables -A OUTPUT -p tcp --dport <port> -j DROP` | 降级到 okz:6696 | T-046, T-058, T-062 |
| 端口占用 | `fuser -k 7890/tcp` + `pkill sing-box` | watchdog 重启 | T-039, T-057 |
| 连接超时 | `tc qdisc add dev eth0 root netem delay 5000ms` | 节点延迟增加，分数降低 | T-032, T-037 |
| CONNECT 失败 | 关闭 sing-box 端口 | BYPASS 模式触发 | T-043 |

### 6.2 文件系统故障注入

| 场景 | 注入方法 | 预期行为 | 关联测试点 |
|------|----------|----------|-----------|
| bad_nodes.json 写入失败 | `chmod 444 /path/to/dir` | 异常被 catch，主循环继续 | T-060 |
| bad_nodes.json 格式损坏 | `echo '{invalid' > .bad_nodes.json` | 返回空对象，不崩溃 | T-061 |
| SQLite 文件锁定 | 启动 `sqlite3 data.sqlite "BEGIN EXCLUSIVE; SELECT 1;"` | 返回 (0,0,[],0) | T-059 |
| SQLite 文件删除 | `rm data.sqlite` | 返回 (0,0,[],0) | T-059 |
| 日志文件满 | `dd if=/dev/zero of=singbox.log bs=1M count=12` | 触发日志轮转 | T-042 |
| 目录不可写 | `chmod 444 /path/to/bad_nodes_dir` | cleanup_dead_ttl catch 异常 | T-060 |

### 6.3 进程故障注入

| 场景 | 注入方法 | 预期行为 | 关联测试点 |
|------|----------|----------|-----------|
| sing-box SIGKILL | `pkill -9 sing-box` | watchdog 检测 + restart | T-057 |
| proxy-global 崩溃 | `pkill -f proxy-global.js` | 9router 请求失败（人工干预恢复） | - |
| daemon 崩溃 | `pkill -f daemon.py` | 9router 继续用最后状态运行 | - |
| 订阅解析器失败 | `chmod -x parse_subscription.py` | catch 异常，记录日志 | T-049 |
| 批量节点不可达 | 所有节点端口被 iptables DROP | batch_test_nodes 全部标记 dead | T-062 |

### 6.4 并发故障注入

| 场景 | 注入方法 | 预期行为 | 关联测试点 |
|------|----------|----------|-----------|
| 并发 retry | `seq 5 \| parallel -j5 curl ...` | 各 retry 序列独立隔离 | T-050 |
| 并发节点切换 | daemon + proxy 同时 PUT /proxies/proxy | 幂等，不返回 4xx/5xx | T-051 |
| 并发 SQLite 读 | `seq 20 \| parallel -j20 python3 -c "..."` | RLock 重入安全 | T-052 |
| 并发 triggerNodeSwitch | 同时 3 个请求触发切换 | _switching 去重，只切换一次 | T-025 |

---

## 7. 回归测试清单

### 7.1 每次变更后的最小回归集（15 分钟）

```bash
# ── P0 快速验证（~5 分钟）──

# 1. 代理链路可用
curl -s -o /dev/null -w "%{http_code}" http://localhost:20128/v1/chat/completions \
  -X POST -H "Content-Type: application/json" \
  -d '{"model":"test","messages":[{"role":"user","content":"hi"}],"max_tokens":1}'

# 2. sing-box 存活确认
curl -s http://localhost:9090/proxies | jq '.proxies | length'
nc -z 127.0.0.1 7890 && echo "OK"

# 3. daemon 主循环运行
pgrep -f auto_switch_ip_daemon

# 4. bad_nodes.json 可读写
touch .bad_nodes.json && echo '{}' > .bad_nodes.json && echo "RW_OK"

# 5. SQLite 错误检测可用
sqlite3 ~/.hermes/9router/config/data.sqlite \
  "SELECT COUNT(*) FROM requestDetails WHERE provider='opencode';"
```

### 7.2 完整回归集（按模块，~2 小时）

| 模块 | 测试点 | 预估耗时 |
|------|--------|----------|
| proxy-chain | T-001, T-002, T-003, T-004, T-005, T-006, T-071, T-072 | 20min |
| error-detection | T-007, T-008, T-009, T-010, T-011, T-012 | 15min |
| node-rotation | T-013, T-014, T-015, T-016, T-017 | 15min |
| retry-mechanism | T-018, T-019, T-020, T-021, T-022, T-023, T-024, T-025, T-026 | 20min |
| shared-state | T-027, T-028, T-029, T-030, T-031 | 10min |
| node-health | T-032, T-033, T-034, T-035, T-036, T-037 | 15min |
| singbox-watchdog | T-038, T-039, T-040, T-041, T-042 | 10min |
| bypass | T-043, T-044, T-045 | 5min |
| fallback | T-046, T-047, T-048, T-049 | 10min |
| concurrency | T-050, T-051, T-052, T-053 | 10min |
| strategy | T-054, T-055, T-056 | 5min |
| anomaly | T-057, T-058, T-059, T-060, T-061, T-062, T-063 | 15min |
| fetch-retry | T-064, T-065, T-066 | 5min |
| edge-case | T-067, T-068, T-069, T-070 | 5min |

### 7.3 版本发布前回归检查清单

```markdown
## 发布前回归检查

### 关键路径（P0 全部通过）
- [ ] T-001: 请求完整链路过代理
- [ ] T-002: CONNECT 隧道建立
- [ ] T-003: NO_PROXY 私网地址绕过
- [ ] T-007: SQLite 错误率窗口检测
- [ ] T-008: 错误率 > 10% 触发轮换
- [ ] T-009: 连续失败 >= 2 触发轮换
- [ ] T-013: 主动轮换每 60s P2C 选节点
- [ ] T-018: HTTP 429/5xx 触发节点切换+重试
- [ ] T-019: 空 200 响应体触发节点切换+重试
- [ ] T-020: SSE 空流触发节点切换+重试
- [ ] T-021: SSE error chunk 触发节点切换+重试
- [ ] T-027: daemon 写 bad_nodes 共享文件
- [ ] T-028: proxy-global 读 bad_nodes 共享文件
- [ ] T-032: 批量节点延迟测试
- [ ] T-033: 死节点 TTL 指数退避
- [ ] T-038: Clash API + 端口双检存活
- [ ] T-039: watchdog 重启流程
- [ ] T-043: BYPASS 模式进入（3 次 CONNECT 失败）
- [ ] T-044: BYPASS 恢复健康检查
- [ ] T-046: 全部节点死亡降级 okz
- [ ] T-047: 节点恢复后从 okz 切回 sing-box
- [ ] T-057: sing-box 进程崩溃恢复
- [ ] T-058: 全部 anytls 节点死亡恢复
- [ ] T-059: 9router SQLite 不可用

### P1 验证（至少覆盖 80%）
- [ ] T-004: proxyUrl 从 SQLite 加载
- [ ] T-005: proxyUrl 无效回退
- [ ] T-010: 仅 opencode provider 触发轮换
- [ ] T-011: 空结果窗口处理
- [ ] T-012: 冷却期内跳过切换
- [ ] T-014: 错误触发轮换不计入调用次数
- [ ] T-015: P2C 选择算法正确
- [ ] T-016: 同 IP 去重保留最低调用节点
- [ ] T-022: 网络错误触发节点切换+重试
- [ ] T-023: 200 error body 触发重试
- [ ] T-024: 重试耗尽返回 502
- [ ] T-025: triggerNodeSwitch 并发去重
- [ ] T-026: 节点切换锁定后返回 502
- [ ] T-029: TOCTOU 防护
- [ ] T-034: 保底机制跳过拉黑
- [ ] T-035: TTL 过期释放死节点
- [ ] T-036: 候选不足时复活过期死节点
- [ ] T-040: 重启次数上限及 fallback
- [ ] T-041: 重启前后代理开关原子性
- [ ] T-045: BYPASS 计数器衰减
- [ ] T-048: 订阅刷新+重启 sing-box
- [ ] T-049: 订阅刷新失败不阻塞
- [ ] T-050: 并发请求 retry 隔离
- [ ] T-051: daemon + proxy-global 并发 Clash API
- [ ] T-052: _db_lock 重入安全性
- [ ] T-054: 正常->压力模式切换
- [ ] T-055: 压力->正常模式切换
- [ ] T-060: bad_nodes.json 写入失败
- [ ] T-061: bad_nodes.json 格式损坏
- [ ] T-062: 节点批测全部超时
- [ ] T-064: globalThis.fetch 包装与 retry
- [ ] T-065: SSE peek 超时 2s

### 文档一致性
- [ ] 测试点清单与实现代码的断言条件一致
- [ ] 通过/失败标准可测量
- [ ] 故障注入方法可复现
```

---

## 8. 通过标准

### 8.1 各级别通过条件

| 级别 | 条件 |
|------|------|
| **P0** | 100% 通过。任何 P0 失败阻塞发布。 |
| **P1** | >= 90% 通过。失败的 P1 须有书面风险评估和修复计划。 |
| **P2** | >= 80% 通过。失败的 P2 须记录到已知问题列表。 |

### 8.2 单个测试点的通过条件

一个测试点必须同时满足以下条件才算通过：

1. **所有预期结果得到验证**：测试步骤中每个预期结果条目均已检查并通过。
2. **无未捕获异常**：测试过程中无未处理的异常抛出。
3. **副作用可控**：测试后置条件已执行，环境恢复至测试前状态。
4. **日志验证**：关键路径的日志输出（节点切换、降级、重启等）与预期一致。

### 8.3 失败分级与处理

| 严重度 | 定义 | 响应 |
|--------|------|------|
| **CRITICAL** | P0 断言失败、数据丢失、服务不可用 | 立即停止发布，hotfix 后重跑全部 P0 |
| **MAJOR** | P1 断言失败、功能子集不可用 | 评估影响，2 个工作日内修复 |
| **MINOR** | P2 断言失败、边界/健壮性问题 | 记录到已知问题列表，迭代修复 |
| **INFO** | 日志告警、非功能性建议 | 记录，不阻塞测试结论 |

### 8.4 测试报告模板

每次测试执行后，生成以下格式的报告：

```markdown
## 测试执行报告

### 概要
- 日期: YYYY-MM-DD HH:mm
- 执行人: [name]
- 运行级别: [P0-only / all]
- 环境: [WSL2 / Ubuntu / ...]

### 结果统计
- 总测试点: 72
- 通过: XX
- 失败: XX
- 跳过: XX
- 通过率: XX%

### P0 结果
- 总 P0: 24
- 通过: XX
- 失败: [ID 列表]

### P1 结果
- 总 P1: 25
- 通过: XX
- 失败: [ID 列表]

### P2 结果
- 总 P2: 23
- 通过: XX
- 失败: [ID 列表]

### 失败详情
| ID | 预期 | 实际 | 根因 | 严重度 | 修复时间 |
|----|------|------|------|--------|----------|
| ... | ... | ... | ... | ... | ... |

### 已知问题
| ID | 描述 | 影响 | 计划修复 |
|----|------|------|----------|
| ... | ... | ... | ... |

### 结论
[通过 / 带条件通过 / 不通过]
```

---

## 附录 A: mock 服务器配置参考

### A.1 Node.js mock HTTP 服务器

```javascript
const https = require('https');
const fs = require('fs');

const options = {
  key: fs.readFileSync('server.key'),
  cert: fs.readFileSync('server.crt')
};

const server = http.createServer((req, res) => {
  if (req.url === '/v1/chat/completions') {
    // 正常响应
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      id: 'mock-' + Date.now(),
      choices: [{ message: { content: 'mock response' } }]
    }));
  } else if (req.url === '/sse') {
    // SSE 端点（可配置延迟发送）
    const delay = parseInt(req.headers['x-sse-delay']) || 0;
    if (delay > 0) {
      setTimeout(() => {
        res.writeHead(200, { 'Content-Type': 'text/event-stream' });
        res.write('data: {"type":"hello"}\n\n');
        res.end();
      }, delay);
    } else {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.write('data: {"type":"hello"}\n\n');
      res.end();
    }
  } else if (req.url === '/error-sse') {
    // SSE error chunk
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.write('data: {"error":{"message":"rate_limit_exceeded"}}\n\n');
    res.end();
  } else if (req.url === '/empty') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(); // 空 body
  } else if (req.url === '/429') {
    res.writeHead(429, { 'Retry-After': '1' });
    res.end(JSON.stringify({ error: { message: 'Too Many Requests' } }));
  } else if (req.url === '/502') {
    res.writeHead(502, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { message: 'Bad Gateway' } }));
  } else {
    res.writeHead(404);
    res.end('Not Found');
  }
});

server.listen(18443, () => {
  console.log('Mock HTTP server on :18443');
});
```

### A.2 sing-box 测试配置

```json
{
  "log": { "level": "info", "output": "singbox.log" },
  "inbounds": [
    {
      "type": "mixed",
      "tag": "mixed-in",
      "listen": "127.0.0.1",
      "listen_port": 7890
    }
  ],
  "experimental": {
    "clash_api": {
      "external_controller": "127.0.0.1:9090",
      "secret": "",
      "default_mode": "Rule"
    }
  },
  "outbounds": [
    {
      "type": "direct",
      "tag": "direct"
    },
    {
      "type": "block",
      "tag": "block"
    }
  ]
}
```

---

## 附录 B: 测试辅助脚本

### B.1 并发请求测试

```bash
#!/bin/bash
# Usage: ./concurrent_test.sh <url> <count>
URL=${1:-http://localhost:20128/v1/chat/completions}
COUNT=${2:-5}

for i in $(seq 1 $COUNT); do
  curl -s -o /dev/null -w "req_${i}: %{http_code} (%{time_total}s)\n" \
    -X POST "$URL" \
    -H "Content-Type: application/json" \
    -d '{"model":"test","messages":[{"role":"user","content":"hi"}],"max_tokens":1}' &
done
wait
```

### B.2 sing-box 状态监控

```bash
#!/bin/bash
# monitor_singbox.sh — sing-box 状态实时监控
while true; do
  clear
  date '+%Y-%m-%d %H:%M:%S'
  echo "=== Clash API ==="
  curl -s http://localhost:9090/proxies 2>/dev/null | jq -r '.proxies | to_entries[] | select(.value.type=="Shadowsocks" or .value.type=="VLess") | "\(.key): \(.value.now)"' 2>/dev/null || echo "Clash API UNREACHABLE"
  echo "=== Port 7890 ==="
  nc -z 127.0.0.1 7890 && echo "OPEN" || echo "CLOSED"
  echo "=== PID ==="
  pgrep -f sing-box || echo "NOT RUNNING"
  sleep 5
done
```

### B.3 SQLite 错误注入

```bash
#!/bin/bash
# inject_errors.sh <db_path> <count> <provider>
DB=${1:-~/.hermes/9router/config/data.sqlite}
COUNT=${2:-2}
PROVIDER=${3:-opencode}

for i in $(seq 1 $COUNT); do
  sqlite3 "$DB" "INSERT INTO requestDetails (provider, model, status, duration_ms, created_at, request_id, endpoint) VALUES ('$PROVIDER', 'test-model', 500, 1000, datetime('now','-$i minutes'), 'test-$RANDOM', '/chat/completions');"
done
echo "Injected $COUNT error records for provider=$PROVIDER"

sqlite3 "$DB" "SELECT provider, status, COUNT(*) FROM requestDetails WHERE provider='$PROVIDER' GROUP BY status;"
```

### B.4 单元测试骨架 (Node.js)

```javascript
// test_proxy_global.js — proxy-global.js 核心函数单元测试
// 注意: proxy-global.js 不导出函数，需通过 --require 加载后访问闭包变量
// 此骨架仅演示可直接导出的函数，需在 proxy-global.js 末尾加 export 语句
const assert = require('assert');

// 如果 proxy-global.js 导出了 shouldUseProxy，则直接 require
// 否则此骨架仅供参考，需改为集成测试（直接发 HTTP 请求验证行为）

// === T-003: NO_PROXY 私网地址绕过 ===
// 仅在 proxy-global.js 导出 shouldUseProxy 时可用
const shouldUseProxy = (host) => {
  const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost', '::1', '::ffff:127.0.0.1', '0.0.0.0']);
  if (!host) return false;
  if (LOCAL_HOSTS.has(host.split(':')[0])) return false;
  if (/^(10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|169\.254\.)/.test(host.split(':')[0])) return false;
  return true;
};
assert.strictEqual(shouldUseProxy('127.0.0.1:8080'), false, '127.0.0.1');
assert.strictEqual(shouldUseProxy('localhost:3000'), false, 'localhost');
assert.strictEqual(shouldUseProxy('10.0.0.1:80'), false, '10.x.x.x');
assert.strictEqual(shouldUseProxy('192.168.1.1:9090'), false, '192.168.x.x');
assert.strictEqual(shouldUseProxy('[::1]:8080'), false, 'IPv6 bracket');
assert.strictEqual(shouldUseProxy('8.8.8.8:53'), true, 'public DNS');

// === T-023: _shouldRetryFetch — 需要 String body（不是对象） ===
// 仅在 proxy-global.js 导出 _shouldRetryFetch 时可用
// 此处内联简化版本用于演示参数格式
const _shouldRetryFetch = (statusCode, bodyText) => {
  if (!bodyText || typeof bodyText !== 'string') return false;
  if ([429, 500, 501, 502, 503, 504].includes(statusCode)) return true;
  try {
    const parsed = JSON.parse(bodyText);
    if (parsed && parsed.error) {
      const errMsg = String(parsed.error?.message || parsed.error?.type || parsed.error).toLowerCase();
      return /rate|limit|quota|exhausted|overload|retry|timeout|unavailable|server_error|upstream/i.test(errMsg);
    }
  } catch {}
  return false;
};

// 200 + error body（必须是 JSON 字符串）
assert.strictEqual(_shouldRetryFetch(200, '{"error":{"message":"rate_limit_exceeded"}}'), true, 'rate_limit');
assert.strictEqual(_shouldRetryFetch(200, '{"error":{"message":"quota_exhausted"}}'), true, 'quota_exhausted');
assert.strictEqual(_shouldRetryFetch(200, '{"error":{"message":"server_error"}}'), true, 'server_error');

// T-069: 非瞬态错误排除（auth/invalid 不重试）
assert.strictEqual(_shouldRetryFetch(200, '{"error":{"message":"bad param"}}'), false, 'invalid_request');
assert.strictEqual(_shouldRetryFetch(200, '{"error":{"message":"bad key"}}'), false, 'auth_error');

// 5xx 状态码触发重试
assert.strictEqual(_shouldRetryFetch(500, ''), true, '500');
assert.strictEqual(_shouldRetryFetch(502, ''), true, '502');
assert.strictEqual(_shouldRetryFetch(503, ''), true, '503');

console.log('All proxy-global unit tests PASS');
```

> **注意**: T-037 延迟评分、T-015 P2C 选择、T-016 同 IP 去重 均为 Python daemon 函数，
> 不可在 Node.js 单元测试中直接调用。这些测试应在 `python3 -m pytest` 或 daemon 的
> 集成测试中验证，而非在此骨架中。

---

## 9. switch-proxy.py 修复验证 (P1 增补)

> 覆盖 `asi-fixes.md` 中的 switch-proxy.py / proxy_pool.py / config.py 修复点。

| ID | 描述 | 优先级 | 验证方法 |
|----|------|--------|----------|
| T-090 | `SINGBOX_SECRET` 替换为 `get_singbox_secret()`，secret 轮换后不 401 | P0 | `grep 'SINGBOX_SECRET' *.py` (仅 config.py 内部残留) → 运行 proxy_global 刷新后，`asi-s` 仍正常 |
| T-091 | `cmd_next(count=0/-5)` 守卫: 返回错误码 1 而非静默成功 | P1 | `python3 switch-proxy.py next 0` → "count 必须是正整数", exit 1 |
| T-092 | `cmd_goto("")` 空串守卫 | P1 | `python3 switch-proxy.py goto ""` → "节点名不能为空", exit 1 |
| T-093 | `main()` 未知指令不再回退为节点名 | P1 | `python3 switch-proxy.py restart` → "未知指令 'restart'", exit 1 |
| T-094 | 错误路径日志可诊断 (401/timeout/JSON 区分) | P1 | `python3 -W error switch-proxy.py status 2>&1` 显示 `logging.warning` 带异常类型 |
| T-095 | `_strategy_mode` 写侧加 `_state_lock` (daemon.py) | P2 | `python3 -m py_compile daemon.py` → PASS; Codex verify |
| T-096 | `_export_dead_nodes_locked` 锁外预取 total_count | P2 | 实测 daemon health check 不阻塞 P2C 选节点 (latency < 1s) |
| T-097 | `config.py` 不再导出冻结 `SINGBOX_SECRET` 常量 | P1 | `python3 -c "from config import SINGBOX_SECRET"` → ImportError |
| T-098 | `proxy_pool.py` `skip_init=True` 不网络初始化 | P1 | `OKZProxyPool(skip_init=True)` 不触发 Clash API 调用 |

**验证命令**:
```bash
python3 switch-proxy.py next 0; echo $?       # 应为 1
python3 switch-proxy.py goto ''; echo $?      # 应为 1
python3 switch-proxy.py restart; echo $?      # 应为 1, 打印未知指令
python3 -c "from config import SINGBOX_SECRET" 2>&1 | tail -1  # ImportError
python3 -m pytest test_proxy_pool.py -v 2>&1 | tail -3  # 13 pass
```

## 10. 订阅链路与轮换健康 (T-099~T-112)

> 背景：2026-10-16 更换 okz 订阅 token 时发现 5 个阻断性 bug，订阅刷新此前**一直静默失效**。
> 本节把修复点和三项运维验证固化为可重复执行的资产。

### 10.1 测试资产

| 脚本 | 覆盖 | 耗时 | 网络 |
|------|------|------|------|
| `test_subscription_parse.py` | T-099~T-106, T-113~T-117 订阅解析确定性回归 | ~30s | 否 |
| `verify_rotation.py --quick` | T-107~T-110, T-112 端口/切换/合理性 | ~20s | 是 |
| `verify_rotation.py` | 上述全部 + T-111 全池扫描 + 地理分布 | ~2min | 是 |

```bash
# 解析侧回归（无网络，可在 CI 跑）
python3 -m pytest test_subscription_parse.py -v

# 运维侧健康验证
python3 verify_rotation.py --quick        # 快速：每次改代理配置后
python3 verify_rotation.py                # 全量：换订阅/换节点池后
python3 verify_rotation.py --samples 30   # 加大逐节点采样
```

### 10.2 订阅解析回归 (test_subscription_parse.py)

| ID | 描述 | 优先级 | 验证方法 |
|----|------|--------|----------|
| T-099 | `PARSERS` 所有值可调用（防 `parse_vless` 缺失类 NameError） | P0 | `test_all_parsers_are_callable` |
| T-100 | `parse_vless` 已定义且可解析 reality/tls/ws | P0 | `test_parse_vless_is_defined`, `test_parse_vless_tls_and_reality`, `test_parse_vless_ws_transport` |
| T-101 | `parse_trojan` 不生成 `transport.type="tcp"`（sing-box 视为非法 → 启动 FATAL） | P0 | `test_parse_trojan_no_tcp_transport` (参数化 `""` / `?type=tcp`) |
| T-102 | `parse_trojan` 正确解析 ws transport | P1 | `test_parse_trojan_ws_transport` |
| T-103 | 单个订阅源失败（403/连接拒绝）不中断全流程，活源仍产出配置 | P0 | `test_dead_source_still_writes_config`（本地 HTTP 模拟死源+活源，离线端到端） |
| T-104 | `refresh_subscription()` 在解析 rc≠0 时返回 False，不重启 sing-box | P0 | `test_parse_failure_makes_refresh_return_false` |
| T-105 | `make_config` 产出的所有 outbound 无非法 transport | P0 | `test_make_config_no_invalid_transport` |
| T-106 | 生成的配置可通过 `sing-box check` | P0 | `test_generated_config_passes_singbox_check`（真实调用 `sing-box check`） |
| T-113 | ws `path` 只解码一次（`parse_qs` 已解码，二次 `unquote` 会破坏 `%2F`） | P1 | `test_ws_path_decoded_once` |
| T-114 | ss `plugin_opts` 为分号拼接字符串（dict 形式 sing-box FATAL） | P0 | `test_ss_plugin_opts_is_string` |
| T-115 | vmess `base64-JSON` 格式可解析（v2rayN/Clash 主流格式；不识别会把整串当 hostname） | P0 | `test_vmess_base64_json_parsed`, `test_vmess_standard_url_format_still_works`, `test_vmess_garbage_returns_empty` |
| T-116 | 残缺节点（无 server/port）被 `make_config` 丢弃，不进轮换池 | P0 | `test_make_config_drops_incomplete_nodes` |
| T-117 | reality 块以 `pbk` 存在为准：Clash 源 reality 保留、空 `pbk` 不下发（否则 sing-box FATAL） | P0 | `test_reality_from_clash_source_kept`, `test_reality_without_pbk_omitted` |

**回归触发条件**：任何对 `parse_subscription.py` / `config.py` 订阅字段 / `daemon.refresh_subscription` 的改动。

### 10.3 端口与轮换健康 (verify_rotation.py)

| ID | 描述 | 优先级 | 验证方法 |
|----|------|--------|----------|
| T-107 | 7890 + 9090 监听，sing-box 进程存活 | P0 | `[1]` 段前 3 项 |
| T-108 | 7890 全协议可用：HTTP / HTTPS / CONNECT 隧道 / DNS over / 大文件 | P0 | `[1]` 段 curl 组 |
| T-109 | 9router 网关 (`:20128/v1/models`) 可达 | P0 | `[1]` 段末项 |
| T-110 | 连续强制切换节点，出口 IP 互不相同 | P0 | `[2]` 段切换采样 |
| T-111 | 逐节点出口 IP 唯一率 >80%（IP 去重有效） | P1 | `[2]` 段采样（`--quick` 跳过） |
| T-112 | 切换合理性：轮换间隔中位数 ≈ `proactive_rotate_sec`、切换目标不含死节点、策略模式已判定、daemon 无异常 | P0 | `[3]` 段 |

**已知干扰**：服务重启或人工切换会引入离群间隔（如 10s / 158s），故 T-112 用**中位数**判定而非均值。判定日志窗口默认近 30 分钟。

### 10.4 换订阅 token 的操作清单

换 **okz** token 时必须同步改 **3 处**（漏改会导致新旧订阅混用或 daemon 用旧 URL）：

```bash
grep -rn 'okz/sub?token=' *.py *.sh *.ps1   # 应恰好 3 处
#   config.py               CFG["okz"]["sub_url"]   ← daemon 实际使用
#   start-auto-switch-ip.sh OKZ_SUB_URL             ← WSL2 启动时导出
#   win_test_nodes.ps1      $sub                    ← Windows 侧测试
```

> 不加 `okz/sub?` 过滤时 `grep -rn 'token=' *.py *.sh *.ps1` 会多列一处
> （`config.py` 的 `CFG["hd23n"]["sub_url"]`，另一个订阅源，与 okz token 无关）。

顺序：改 token → `python3 -m pytest test_subscription_parse.py` → 重启 daemon →
`python3 verify_rotation.py` → 确认节点池已换（比对出口 IP 与旧池不同）。

## 11. WARP 专用出口（opencode）

背景：opencode 免费层的 403 有两种文案——`abusive traffic from this IP` 是 IP 信誉拦截（9router 命中的就是这种），`only be used from within OpenCode` 是客户端指纹（裸 curl 必现，换 IP 无效）。因此出口换成 WARP 后即可恢复。

| ID | 级别 | 测试点 | 方法 | 预期 |
|----|------|--------|------|------|
| T-118 | P0 | opencode 流量走 WARP | `grep opencode.ai singbox.log \| tail -2` | 出站为 `outbound/socks[WARP-CF]` |
| T-119 | P0 | 端到端可用 | `curl -H 'Authorization: Bearer 9router-local' -d '{"model":"oc-ds-mimo","messages":[{"role":"user","content":"ping"}],"max_tokens":4}' :20127/v1/chat/completions` | 200 且返回 choices |
| T-120 | P1 | 轮换池不含 WARP | Clash API `/proxies/proxy` 的 all 列表 | 不含 `WARP-CF` |
| T-121 | P1 | usque 服务自愈 | `kill $(pgrep -f '[u]sque socks')` | systemd 5s 内拉起，`is-active` = active |
| T-122 | P1 | 配置刷新保留 WARP | 手跑 `parse_subscription.py --urls <sub>` 后 `sing-box check` | 新配置仍有 `opencode-out` 与 `WARP-CF` |
| T-123 | P2 | 回落路径 | `PUT /proxies/opencode-out {"name":"proxy"}` | 改走轮换池；再切回 `WARP-CF` 恢复 |
| T-124 | P0 | 主备换线：备线可用时切过去而非原地重启 | `python3 -c "import daemon; daemon.reconnect_warp('手动验证')"` | 日志 `✓ WARP 换线成功: WARP-CF -> WARP-CF-B`；`active_warp_tag()` 变为备线 |
| T-125 | P1 | 被换下的线后台刷新会话 | 同上，随后 `grep 会话已刷新 daemon.log` | `↻ WARP-CF 会话已刷新: <新 IP>` |
| T-126 | P0 | abuse 403 达阈值自动换线 | 观察 `grep 换线 daemon.log` | 日志 `↻ WARP 换线 (abusive 403 ×N)` 后跟 `✓ 换线成功` |
| T-127 | P1 | 换线冷却防风暴 | 连续两次 `maybe_reconnect_warp` | 冷却窗口内第二次不产生新换线日志 |
| T-128 | P2 | sing-box 重启保留线路 | 在备线上 `restart_singbox()` | 日志 `↺ WARP 线路恢复为 WARP-CF-B`，`active_warp_tag()` 不变 |
| T-129 | P0 | 换线不受节点切换时间戳门控 | `test_warp_failover_not_gated_by_node_switch` | 一次 abuse 403 即触发换线；主动轮换刷新时间戳不影响 |
| T-130 | P0 | daemon 重启不连带杀 sing-box | `systemctl --user restart auto-switch-ip.service` 后比对 sing-box PID | PID 不变；`KillMode=process` |

| T-135 | P0 | IP 新鲜度守卫：窗口内用过的出口先刷新再切 | `test_warp_ip_reuse_window_and_cleanup` | 300s 内用过的 IP 触发刷新；窗口外的直接用 |
| T-136 | P0 | 换线前刷新目标线出口 | `test_warp_failover_refreshes_target_ip_before_switch` | 日志 `↻ <tag> 会话已刷新: <IP>` 后再 `✓ 换线成功` |
| T-137 | P1 | 目标线 IP 已新鲜则跳过刷新 | `test_warp_failover_skips_refresh_for_fresh_ip` | 不产生刷新日志，直接换线 |
| T-138 | P1 | 刷新后仍是旧 IP 则换下一条备线 | `test_warp_failover_skips_target_when_refresh_keeps_stale_ip` | 跳过该线；全不新鲜则切轮换池 `proxy` |
| T-139 | P0 | 有在途连接不强刷（防 upstream connection lost） | `systemctl --user stop auto-switch-ip` 后 `grep '备线均在服务在途连接' daemon.log` | 日志出现该行时**不**出现 `强刷`；连接归零后才刷新 |
| T-140 | P0 | user manager 未重启（排「全线崩」） | `systemctl --user show user@1000.service -p ActiveEnterTimestamp --value` + `journalctl -u user@1000.service \| grep -E 'Exit the Session\|Deactivated'` | manager Active 起始时间早于当前故障窗口；出现 `Exit the Session` 则按 INC-006 处置（非代理问题） |
| T-141 | P0 | 判「线忙闲」用字节增量而非连接数 | `pytest test_warp_refresh.py -k line_busy` | 字节增长=忙、不变/回落=闲、查询失败=None；活跃线恒有 7~10 连接故连接数判据必然超时 |
| T-142 | P0 | 被换下的线等不到空闲必须补刷 | `pytest test_warp_refresh.py -k retry_loop` | 首轮忙之后仍重试并刷新成功；恒忙/刷不出新 IP 时重试到 `ip_refresh_retry_sec` 为止。实测旧行为致 WARP-CF-C 被换下 8 次仅刷 2 次 |
| T-143 | P0 | 三条隧道全不可用时切轮换池 proxy | `pytest test_daemon_unit.py -k falls_back_to_proxy or skips_dead_standby` | 日志 `⚠ 无可用备线，切轮换池兜底` 后 `✓ 已切到轮换池 proxy`；**不**原地重连（D-Bus 断时必败） |
| T-144 | P0 | 停在 proxy 时可切回 WARP | `python3 -c "import daemon; daemon.reconnect_warp('手动验证')"` | 切回 WARP 前日志 `当前在轮换池 proxy，尝试切回 WARP 隧道`；成功后 `active_warp_tag()` 非空 |
| T-145 | P1 | 测试不得写真实 sing-box | `pytest test_daemon_unit.py -q`（fixture 含写操作防线） | 漏 stub 时 `AssertionError: 测试试图写真实 sing-box`，而非静默翻转生产 selector（实测发生过） |
| T-146 | P1 | 测试文件可被 pytest 收集 | `pytest test_warp_refresh.py --collect-only -q \| tail -1` | 收集到 15 个用例。函数式 `main()`+`check()` 写法 pytest 收集不到，`no tests ran` 仍退出 0 —— 假成功 |
| T-147 | P0 | 补刷中的线不得被选为换线目标 | `pytest test_daemon_unit.py -k excludes_line_under_refresh` | 该线在 `_WARP_REFRESHING` 时跳过；切到其他空闲新鲜线。补刷窗口 60→600s 后撞上概率显著上升 |
| T-148 | P0 | 出口 IP 复用窗口生效（事故复现对照） | `grep "✓ WARP 换线成功" daemon.log` 后统计同一 IP 的相邻使用间隔 | 900s 内不得出现重复地址。**旧代码实测**：`2a09:bac5:1efa:183c::26a:bf` 在 21:10:15/21:18:16/21:27:50/21:38:38 连续三次切回（间隔 8.0/9.6/10.8min），同期 opencode abuse403 365+102 条、成功率 12%→57% |
| T-149 | P0 | proxy 池健康探测（双向兜底反向） | `pytest test_daemon_unit.py -k "proxy_pool or failback"` | 采样 3 节点有 1 个通=健康；全失败=不可用；查询失败=None；WARP 成员不参与探测 |
| T-150 | P0 | 池挂且 WARP 可达时主动切回 | `pytest test_daemon_unit.py -k failback` | 已走 WARP 不重复触发；WARP 不可达不切；`proxy_check_sec` 内不重复探测 |
| T-151 | P1 | 双向兜底接线未丢失 | `pytest test_warp_refresh.py -k wired` | 主循环含 `_maybe_failback_to_warp(now)`；换线路径 `_fallback_to_proxy(` ≥4 处 |
| T-152 | P1 | opencode 域名规则覆盖裸域 | 生成配置后检查 `dns.rules` / `resolve` | `["opencode.ai", ".opencode.ai"]` 两种形式都要有。实测流量 12086 次裸域 / 2 次子域 |
| T-153 | P0 | 五条 WARP 隧道全部进入 selector | 生成配置后打印 `opencode-out.outbounds` | 含 WARP-CF/-B/-C/-D/-E **五条** + proxy。实测 `.warp/warp-outbound.json` 只有 A/B/C，D/E 两条隧道空转，池更快耗尽（03:35 "17 个 IP 全被标记"） |
| T-154 | P0 | 回切 WARP 由三条证据判据触发，非「WARP 探测通过」 | `pytest test_daemon_unit.py -k failback` | ①池健康探测**连续** `proxy_fail_streak` 次全失败；②落池后仍被 403/429 拦截 ≥ `proxy_blocked_threshold`；③黑名单占比 ≥ `proxy_dead_ratio`。池健康时必须留在池上。实测旧行为 00:02:45 落池、00:02:52 即回切（7s），83 节点池零请求 |
| T-155 | P0 | 池健康采样随机且并发 | `pytest test_daemon_unit.py -k proxy_pool` | 随机 3 节点并发探测；固定取前 3 个时这 3 个恰好挂了就把整池判死。串行 3×15s 会卡主循环 |
| T-156 | P0 | 落池时刻取切换生效当下而非调用方 now | `pytest test_daemon_unit.py -k stale_caller_now` | `reconnect_warp` 的 now 取自探测/重启前，可能已过数分钟；用它当 `_proxy_dwell_since` 会让判据②统计到落池前的旧 403/429 |
| T-157 | P1 | 未发起切换时不得清空回切证据 | `pytest test_daemon_unit.py -k keeps_evidence` | `_reconnect_warp_now` 在冷却/锁竞争时返回 False，此时清 `_proxy_fail_streak` 等于丢掉已积累的判据 |
| T-158 | P1 | 补刷成功后不二次重启隧道 | `grep -c "会话已刷新" daemon.log` 与换线次数对照 | `_schedule_idle_refresh` 只调 `_retry_refresh_idle_warp`；叠加无条件 `_refresh_idle_warp` 会再换一个 IP 并可能掐断在途连接 |
| T-159 | P1 | 探测超时放宽到 20s | `for p in 1080..1084; curl --max-time 20 --socks5 ...` | 实测 B/C 偶发 >8s 响应（触发旧超时），20s 内稳定 1.2-1.4s |
| T-160 | P0 | 未报错也主动换线（按请求数） | `pytest test_daemon_unit.py -k proactively_rotate` | 无任何 403/429，累计 `proactive_after_requests`=20 条即发起换线；未达阈值不换。WHY：报错后才换已经冲断在途对话；实测同一 IP 首次 403/429 前请求数中位数 28（p25=10，p75=73） |
| T-161 | P1 | 主动轮换遵守换线冷却 | `pytest test_daemon_unit.py -k proactively_rotate` | 冷却期内（换线后 20s）即使满足请求数也不换，防止请求突发把隧道变成永动机 |
| T-162 | P0 | 选线按「线」的最近选择时刻轮转 | `pytest test_daemon_unit.py -k longest_unselected_fresh_line` | 多条新鲜备线时选 `_WARP_LINE_STATE[tag].sel` 最小的那条。按 IP 使用表排序不成立：出窗口的条目会被清理，清理后所有备线并列最老，退化成 members 顺序（实测 D/E 被选中 0 次） |
| T-163 | P1 | 备线预热跳过新鲜线、只刷两条以内 | `pytest test_daemon_unit.py -k prewarm` | 挑最久没被选过且已知 IP 不新鲜的线调度刷新；`_WARP_REFRESHING` ≥2 时不预热。目的：换线时总有现成新鲜 IP 可直接切，省掉 7~9s 同步刷新。`blocked` 标记与「没有 `last_ok_ts`」的地址一律算不新鲜（T-176） |
| T-164 | P0 | 出口 IP 复用窗口 24h 生效 | `pytest test_daemon_unit.py -k test_warp_ip_reuse_window_24h` | `ip_reuse_guard_sec` 与 `blocked_ttl_sec` 均为 86400：23h 前用过的 IP 仍判「窗口内」并触发先刷新会话；超 24h 的条目被清理、可直接切。WHY：实测出口 IP 可用周期 p50=19s、max=65s，10~50 分钟切回几乎立刻再 403，一天内复用等于反复踩同一批烧掉的地址 |
| T-165 | P0 | cycle 样本采集与 24h 过滤 | `pytest test_daemon_unit.py -k test_warp_cycle_pruning_24h_window` | 换线生效记起点、该 IP 首次 403/429 记终点，样本 `{ip, cycle, ended_at, ok}` 落盘 `.warp_ip_cycles.json`（`WARP_IP_CYCLES_FILE_PATH` 可覆盖，测试隔离）；超 24h 的样本被清除 |
| T-166 | P0 | 典型周期重算排除无效样本 | `pytest test_daemon_unit.py -k test_recompute_min_cycle` | 每 `min_cycle_recalc_sec`(300s) 取 24h 内所有 **cycle > 0 且 ok > 0** 样本的**中位**存入 `_WARP_MIN_CYCLE_SEC`（下限 5s）。cycle == 0（切上去首请求即错）与 ok == 0 不参与。取中位而非最小值：最小值由最倒霉的一条线决定（实测 136 条可用样本 min 0.1s / p25 4.2s / 中位 17.6s），会让主动轮换每几秒切一次。日志 `↻ 典型可用周期重算: 中位 17.6s（min 0.1s / p25 4.2s / p75 21.5s，可用样本 136 条，首错即废 52 条已排除）→ 生效 18s` |
| T-167 | P0 | 主动轮换按典型周期触发并绕开成功冷却 | `pytest test_daemon_unit.py -k "test_proactive_rotate_uses_min_cycle_sample or test_proactive_rotate_keeps_fail_cooldown"` | 有样本时 `now - 段起点 ≥ _WARP_MIN_CYCLE_SEC` 即换并传 `ignore_cooldown=True`（周期下限 5s 短于 `cooldown_ok_sec` 20s，受冷却就永远等不到触发）；未到周期不换、也不查请求计数。**只绕成功换线后的冷却**：`_last_warp_reconnect_ts <= _last_warp_switch_ok_ts`；上次换线失败时 `_warp_cooldown_until` 是 `cooldown_fail_sec`，不绕（防连切连败）。并发始终由 `_WARP_LOCK` 保证 |
| T-167b | P0 | 冷却期内的 403 仍须标脏 IP 并采到周期样本 | `pytest test_daemon_unit.py -k test_maybe_reconnect_marks_ip_blocked_inside_cooldown` | 冷却未结束时触发采样：出口 IP `blocked=True`、本段写入一条 `cycle = now - _last_warp_switch_ok_ts` 样本、且不 spawn 换线线程；同段重复触发不重复采样。修复前这些段整体被丢弃，IP 不标脏会被下次换线当干净地址选回 |
| T-168 | P1 | 请求数判据 | `pytest test_daemon_unit.py -k test_proactive_rotate_falls_back_to_request_count` | 已服务 `proactive_after_requests`(60) 条请求即换线，未达阈值不换。取 60 的依据：61 段实测 30% 零错误、每段成功中位 14 条（max 56） |
| T-169 | P0 | ignore_cooldown 仍受 `_WARP_LOCK` 约束 | `pytest test_daemon_unit.py -k test_ignore_cooldown_still_requires_warp_lock` | 锁被占用时返回 False 且不 spawn 换线线程；锁空闲才发起。WHY：绕过冷却只是放宽时间判据，不能同时放宽并发互斥，否则两个换线线程会互相踩隧道 |
| T-170 | P1 | 预热按 24h 流量门控 | `pytest test_daemon_unit.py -k test_prewarm_gate_uses_24h_window` | 最近 24h 内有 opencode 请求才预热备线（2h 前的旧流量仍算有）；24h 内零请求不预热。旧窗口 10 分钟会让冷启动/低峰期永不预热 |
| T-171 | P1 | 全不新鲜时按休息时间兜底选线 | `pytest test_daemon_unit.py -k test_reconnect_warp_picks_longest_rest_when_none_fresh` | 所有备线都刷新不出 24h 窗口外的新 IP 时，选 `now - 上次使用时刻` 最大的一条；日志打印该 IP 休息时长与样本平均休息时长，切前仍先刷新会话 |
| T-172 | P0 | 补刷不得重启已成当前出口的线 | `pytest test_warp_refresh.py -k test_retry_loop_aborts_when_member_becomes_active` | 空闲后这条线已被切成当前出口时必须放弃并返回 False，不得调 `_refresh_idle_warp`（重启会掐断在途连接 → upstream connection lost）。判据落在**重启之前**而非循环开头：换线线程的快照早于本次调度，等空闲的 40s 内这条线才变成出口 —— 放在本轮开头挡不住这一段。线下次被换下时 `reconnect_warp` 会重新调度补刷 |
| T-173 | P0 | IP 使用记录与周期样本须原子落盘 | `pytest test_daemon_unit.py -k test_persistence_write_is_atomic_and_logs_failure` | `.warp_ip_used.json` 与 `.warp_ip_cycles.json` 走「写 `.tmp` → `os.replace`」；写入失败时原文件保持完好（不先截断），并记 `✗ 写 xxx 失败`。WHY：这两份记录是重启后唯一能恢复「哪些地址已被烧」的证据，直接 `open(w)` 在崩溃/掉电时留半截 JSON → 下次启动解析失败 → 记录全丢 → 脏地址被当干净地址切回（实测 22:22 重启即如此，22:27 切回已烧地址，403 持续 144s）。旧实现的 `except: pass` 让这种丢失无从察觉 |
| T-174 | P0 | 补刷在途不得被并发换线踩到 | `pytest test_warp_refresh.py -k "retry_loop"` | `_WARP_REFRESH_LOCK` 用 RLock：`_prewarm_standby_warp` 持锁后调 `_schedule_idle_refresh`（后者同锁）不再自锁死。另：补刷循环里查当前出口的真函数 `active_warp_tag()` 会发 Clash API 请求，测试必须打桩 —— 否则假时钟 tick 预算被吃光且结果取决于线上此刻哪条线在服务（实测同一用例在不同时刻分别命中 WARP-CF-E / WARP-CF-C，flaky） |
| T-175 | P0 | 备线出口全被标记时刷新而非跳过 | `pytest test_daemon_unit.py -k "refreshes_blocked or falls_back_when_blocked"` | 探测到备线出口 `blocked` 时不跳过、不直接落 proxy：重启该线换会话 IP 后切过去；所有脏线都刷不出新地址才落 proxy。WHY：每条隧道同一时刻只有一个出口 IP，全跳过会让候选池恒为 0（403 风暴几秒就烧完全部备线），流量被甩到同样被限流的池 —— 实测 19:16 兜底死循环。刷新后仍拿回 blocked 地址也视为失败 |
| T-176 | P0 | 疑似脏 IP 的备线必须重连到换出优质 IP | `pytest test_daemon_unit.py -k "prewarm_refreshes_blocked or schedules_refresh_for_blocked"` | 巡检把 `blocked` 或复用窗口内的 IP 都判为脏（含没有 `last_ok_ts` 的 blocked 地址）；换线成功时立即把剩余脏备线排进补刷。巡检在 proxy 池上同样运行，最多两条并发，直到 `_retry_refresh_idle_warp` 重试上限 |
| T-177 | P1 | 当前出口 IP 补探与成功流量打点 | `pytest test_daemon_unit.py -k "init_active_ip or record_active_ip_success"` | 启动探测失败时后台补探 `_ACTIVE_WARP_IP`（30s 最多一次）；当前出口没有记录时补建条目再写 `last_ok_ts`，否则选线日志恒为「有成功记录 0 条」 |

> 周期数据依据（2026-10-20）：修复前落盘的 18 条样本全部落在 20.0~21.9s —— 那不是 IP 寿命，是 20s 冷却期本身，因为冷却期内出现的 403 根本没被采样（T-167b）。修好采样后实测：样本 min 1.6s / 中位 3.1s，并出现 `ok=1` 的 2.5s 样本（服务成功过随即被烧）。同期按 DB 逐段核对 45 个有流量的换线段：首错延迟中位 1s、16 段首请求即错、30% 零错误、每段成功中位 14 条。v1.15 曾据此回退请求数判据，但那是把「采样缺失」误归因为「时间判据本身错」；采样修复后 v1.17 恢复时间判据（T-167），请求数判据降为无样本时的回退路径（T-168）。

路由两条硬约束（踩过的坑）：
1. `domain_suffix` 写 `opencode.ai`，写 `.opencode.ai` 匹配不到裸域；
2. 出站规则必须排在 `action: resolve` 之前，resolve 后 destination 变成 IP，domain 规则不再命中。

## 修订历史

| 版本 | 日期 | 修改内容 | 作者 |
|------|------|----------|------|
| v1.0 | 2026-07-07 | 初始版本：72 个测试点，P0/P1/P2 分级，完整测试流程 | - |
| v1.1 | 2026-08-22 | 新增 switch-proxy.py 安全/健壮性测试点 (T-090~T-098), 覆盖 secret 刷新、count/empty/guard、CLI fallback、logging、daemon 锁优化 | 4-Agent review |
| v1.2 | 2026-10-16 | 新增 §10 订阅链路与轮换健康 (T-099~T-112)：订阅换 token 时修复 5 个阻断性 bug（parse_vless 缺失、单源失败中断全流程、refresh 不检查 rc、trojan tcp transport 致 sing-box FATAL、daemon global 声明缺失）；新增 test_subscription_parse.py（11 项）与 verify_rotation.py（19 项） | - |
| v1.3 | 2026-10-16 | 补齐 T-113~T-117（ws path 二次解码、ss plugin_opts 类型、vmess base64、节点完整性 guard、reality pbk）；标识符改英文；§10.4 引导路径修复 | - |
| v1.4 | 2026-10-19 | 新增 §11 WARP 专用出口 (T-118~T-123)：opencode 403 实为 IP 信誉拦截，经 usque MASQUE 的 `opencode-out` 出口恢复 | - |
| v1.5 | 2026-10-19 | WARP 改主备双隧道 (T-124~T-128)：abuse 403 达阈值时切到备线并后台刷新被换下的线；单线退化为原地重连 | - |
| v1.6 | 2026-10-19 | 阈值降为 1 条、冷却 20s 起指数退避 (T-129/T-131)；修复换线被 `_last_successful_switch_ts` 门控吞掉的根因；unit 加 `KillMode=process` 防 daemon 重启连带杀 sing-box (T-130) | - |
| v1.7 | 2026-10-19 | 429 纳入换线触发（阈值 5，T-132~T-134）；更正"429 换 IP 无效"的错误结论 —— 实测切 WARP 成员后成功率 3.5%→9.3% | - |
| v1.8 | 2026-10-19 | 三隧道 (WARP-CF/-B/-C) + 移除 2^n 退避改固定冷却；换线前 IP 新鲜度校验与 idle 守卫 (T-135~T-138)；新增 T-139 防强刷掐断在途连接、T-140 系统层排查；删 T-131（退避机制已移除）；记 INC-006（WSL 用户会话退出致 user@1000.service 整体重启；伴生 swap 满与 OOM）| - |
| v1.9 | 2026-10-19 | 修 INC-006 三项根因：判闲改字节增量 (T-141)、延迟刷新改补刷重试 (T-142)、全不可用切轮换池代理 (T-143/T-144)；复用窗口 300→900s（实测复用间隔 10.6min）；测试隔离加写操作防线 (T-145)、test_warp_refresh 改为 pytest 可收集 (T-146) | - |
| v1.10 | 2026-10-19 | 换线候选排除正在补刷的线 (T-147)：补刷窗口拉长到 600s 后，切到一台即将被重启的隧道会掐断在途连接 | - |
| v1.11 | 2026-10-19 | 双向兜底 (T-149~T-151)：proxy 池健康探测 + 主动切回 WARP，不再只靠下游 429 触发；接线断言防重构删除。修 opencode 域名规则裸域匹配 (T-152，codex review 发现) | - |
| v1.12 | 2026-10-20 | 五隧道补齐 (T-153)：`.warp/warp-outbound.json` 仅 A/B/C，D/E 空转；回切改为三条证据判据 (T-154)，不再因 WARP 探测通过就回切；池采样随机并发 (T-155)；修落池时间戳陈旧 (T-156)、失败丢证据 (T-157)、补刷后二次重启 (T-158)；探测超时 8→20s (T-159) | codex review |
| v1.13 | 2026-10-20 | 主动轮换 (T-160/T-161)：按已服务请求数触发换线，不等 403 冲断对话；选线改按「线」的最近选择时刻轮转 (T-162)，修掉 IP 表清理后排序退化、D/E 永不被选的问题；新增备线预热 (T-163) 让换线路径不必同步等待刷新；删 `_warp_ip_exhausted` 假耗尽短路；`_WARP_LINE_STATE` 记录每条线上次被选时刻与已知 IP | - |
| v1.15 | 2026-10-20 | 订正 v1.14 的时间判据：周期样本最小值恒等于冷却值（403 落在冷却期被丢弃，未采样也未标脏），主动轮换被锁成每 20 秒一次。修采样先于冷却判断 (T-167b)、主动轮换回归纯请求数判据且阈值 20→60 (T-167/T-168)。实测依据：45 段首错延迟中位 1s、16 段首请求即错、30% 段零错误、每段成功中位 14 条；修复后样本 min 1.6s / 中位 3.1s | - |
| v1.14 | 2026-10-20 | IP 复用窗口 3600→86400s (T-164)；新增出口 IP 周期采集 `.warp_ip_cycles.json` 与 24h 过滤 (T-165)、最小周期定期重算并排除 cycle=0 (T-166)；主动轮换改为按最小周期触发、无样本回退请求数判据 (T-167/T-168)；`ignore_cooldown` 仍受 `_WARP_LOCK` 约束 (T-169)；预热流量门控 10min→24h (T-170)；全不新鲜时按休息时间兜底选线 (T-171)。依据：24h 实测 80 个有流量换线段，42 段成功过再出错，cycle p25=7s / p50=19s / max=65s，13 段首请求即错 | - |
| v1.16 | 2026-10-20 | 补刷并发收口：`_WARP_REFRESH_LOCK` 改 RLock 解开 `_prewarm_standby_warp` → `_schedule_idle_refresh` 的自我死锁 (T-174)；守卫移到重启前，补上「等空闲期间被切成出口」这一段 (T-172)；两份记录改原子落盘且失败记日志 (T-173)。测试侧修 flaky：补刷用例打桩 `active_warp_tag`，不再依赖线上此刻哪条线在服务 | codex review |
| v1.17 | 2026-10-20 | 恢复周期时间判据 (T-166/T-167)：v1.15 的回退建立在采样缺失之上，修复后该理由不成立。有样本时按「典型周期（中位，下限 5s）」换线并绕开成功换线后的冷却，上次换线失败仍守 `cooldown_fail_sec`；无样本才回退请求数 (T-168)。周期取中位而非最小值：最小值由最倒霉的一条线决定（实测 min 0.1s / 中位 17.6s），会退化成每几秒切一次。同时段起点在重启后从 `.warp_ip_used.json` 恢复，避免把重启前的流量算进新段 | - |
| v1.18 | 2026-10-20 | 修 WARP 兜底死循环：备线出口被标记时不再跳过，改为刷新换会话 IP (T-175)；任一隧道疑似脏 IP 就重连到换出优质 IP（预热补 `blocked` 判据 + 换线后立即排补刷 + 落池期间继续巡检，T-176）；补 `_ACTIVE_WARP_IP` 启动补探与当前出口成功流量打点 (T-177) | - |
