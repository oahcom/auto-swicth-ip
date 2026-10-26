# Auto-Switch-IP 测试工作流文档

> 文档版本: v1.3
> 最后更新: 2026-10-16
> 测试对象: 9router (custom-server.js) + daemon 代理自动切换系统
> 订阅链路与轮换健康的验证资产见 §10（`test_subscription_parse.py` / `verify_rotation.py`）

> ⚠ 文档漂移提示（2026-08-22）：第 9 节 `switch-proxy.py` / `proxy_pool.py` 验证用例（T-091~T-098）依赖的模块已在 Round 1 删除（无生产调用方）。其覆盖点已并入 `daemon.py` 与新增 `test_daemon_unit.py` 的确定性单测。第 9 节保留为历史归档，不再执行。

> ⚠ 文档漂移提示（2026-10-21）：`~/.9router/proxy-global.js` 与 `bootstrap-undici.js` 已不存在，
> 9router 进程无 NODE_OPTIONS，出站改由 `data.sqlite.settings.outboundProxyUrl=http://127.0.0.1:7890`
> 直连 sing-box。凡以 `proxy-global` 为被测对象的用例（T-001~T-006、T-018~T-026、T-028、T-030、
> T-050、T-051、T-064~T-072）**保留为历史归档，不再执行**；其现行等价覆盖点：
> 请求级重试由 9router 自身负责（无本地重试层），换线判据全部落在 `daemon.py`
> （见 T-181~T-189 与 `test_daemon_unit.py` / `test_warp_refresh.py`）。

---

## 1. 测试范围和目标

### 1.1 测试范围

本测试计划覆盖 auto-switch-ip 系统的全部功能模块：

| 模块 | 覆盖组件 | 测试点数 |
|------|----------|----------|
| 代理链路 (proxy-chain) ⚠历史归档 | proxy-global.js CONNECT 隧道、NO_PROXY、SQLite 配置加载、secret 管理 | T-001~T-006, T-071~T-072 |
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

1. 验证代理链路端到端完整工作，请求经 9router(:20127) -> sing-box:7890 -> 出口节点的每个环节无误。（历史版本经 proxy-global.js，该层已移除）
2. 验证错误检测窗口准确计算错误率，并在阈值达标时触发节点切换。
3. 验证节点轮换算法（P2C、同 IP 去重、候选不足回退）按预期工作。
4. 验证所有重试条件（429、5xx、空 body、SSE 空流/error chunk、网络错误）均触发节点切换 + 重试，耗尽后返回 502。
5. 验证 daemon 独占读写 bad_nodes.json 的 TOCTOU 防护。（历史版本由 daemon 与 proxy-global.js 共享）
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
| Node.js | v18+（仅历史归档的 proxy-global.js 用例需要） |
| Python | 3.10+（daemon 运行环境） |
| sing-box | 最新稳定版（Clash API 兼容） |
| 9router | 正常运行 |
| SQLite | 3.x（data.sqlite 读/写） |
| 磁盘 | 100MB+ 可用空间（日志 + 测试数据） |

### 2.2 服务/端口依赖

| 服务 | 端口 | 用途 |
|------|------|------|
| 9router | 20127 | 模型路由 API |
| sing-box Clash API | 9090 | 代理管理 REST API |
| sing-box mixed port | 7890 | SOCKS5/HTTP 混合代理（9router 的 outboundProxyUrl 指向这里） |
| mock HTTP 服务器 | 18443（测试） | 模拟目标 API（走 sing-box mixed，mock 只需 HTTP） |
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
| node | 历史归档的 proxy-global.js 单元测试（现行回归不依赖） |

### 2.4 配置文件/路径

| 文件 | 用途 |
|------|------|
| `~/.9router/db/data.sqlite` | 9router 持久化数据（requestDetails / settings） |
| `singbox-config.json` | sing-box 运行时配置（本项目根目录，make_config 生成） |
| `~/.singbox_secret` | sing-box 认证密钥（mode 600） |
| `.bad_nodes.json` | daemon 独占读写的坏节点列表（proxy-global.js 已移除，无第二读取者） |
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
   curl -X POST http://localhost:20127/v1/chat/completions \
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
  daemon 写 bad_nodes -> daemon 自读（proxy-global 读取者已移除）

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
curl -s -o /dev/null -w "%{http_code}" http://localhost:20127/v1/models
curl -s -o /dev/null -w "%{http_code}" http://localhost:9090/proxies
nc -z 127.0.0.1 7890 && echo "PORT_7890_OPEN"

# ── 9router 进程（无 NODE_OPTIONS / 无 proxy-global.js 注入）──
pgrep -af "custom-server.js" | head -2
tr '\0' '\n' < /proc/$(pgrep -f custom-server.js | head -1)/environ | grep -c NODE_OPTIONS

# ── 9router 出站代理指向 7890 ──
sqlite3 ~/.9router/db/data.sqlite \
  "SELECT json_extract(data,'\$.outboundProxyEnabled'), json_extract(data,'\$.outboundProxyUrl') FROM settings WHERE id=1;"

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
| 9router 崩溃 | `pkill -f custom-server.js` | 全部模型请求失败，需人工拉起 | - |
| daemon 崩溃 | `pkill -f daemon.py` | 9router 继续用最后状态运行 | - |
| 订阅解析器失败 | `chmod -x parse_subscription.py` | catch 异常，记录日志 | T-049 |
| 批量节点不可达 | 所有节点端口被 iptables DROP | batch_test_nodes 全部标记 dead | T-062 |

### 6.4 并发故障注入

| 场景 | 注入方法 | 预期行为 | 关联测试点 |
|------|----------|----------|-----------|
| 并发 retry | `seq 5 \| parallel -j5 curl ...` | 各 retry 序列独立隔离 | T-050 |
| 并发节点切换 | 两个 daemon 线程同时 PUT /proxies/proxy | 幂等，不返回 4xx/5xx | T-051 |
| 并发 SQLite 读 | `seq 20 \| parallel -j20 python3 -c "..."` | RLock 重入安全 | T-052 |
| 并发 triggerNodeSwitch | 同时 3 个请求触发切换 | _switching 去重，只切换一次 | T-025 |

---

## 7. 回归测试清单

### 7.1 每次变更后的最小回归集（15 分钟）

```bash
# ── P0 快速验证（~5 分钟）──

# 1. 代理链路可用
curl -s -o /dev/null -w "%{http_code}" http://localhost:20127/v1/chat/completions \
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
URL=${1:-http://localhost:20127/v1/chat/completions}
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
| T-109 | 9router 网关 (`:20127/v1/models`) 可达 | P0 | `[1]` 段末项 |
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

## 10A. 局域网入站（T-127~T-134）

背景：把代理和 9router 开放给同网段的手机/其他机器。核心风险不是「能不能连」，而是
「会不会变成谁都能用的开放中继」，所以测试重点在**失败即关闭**与**来源限制**。

| ID | 描述 | 优先级 | 验证方法 |
|----|------|--------|----------|
| T-127 | 无认证文件时不生成 `mixed-lan` 入站 | P0 | `test_lan_inbound_absent_without_auth_file` |
| T-128 | 无认证文件时**不生成**引用 `mixed-lan` 的来源规则（悬空规则能过 `check`，是纯垃圾） | P0 | 同上，断言 route 无 `logical+inbound` 规则 |
| T-129 | 有认证文件时生成 `0.0.0.0:<lan_port>` + `users` 入站 | P0 | `test_lan_inbound_present_with_auth_file` |
| T-130 | `lan_port=0` 时完全不建局域网入站 | P1 | `test_lan_inbound_skipped_when_port_zero` |
| T-131 | 来源规则对非私网来源 reject，且排在 route 第一条 | P0 | `test_lan_guard_rejects_non_private_source` |
| T-132 | `lan_port=0` 时不生成来源规则 | P1 | `test_lan_guard_absent_when_port_zero` |
| T-133 | 带局域网入站的配置通过 `sing-box check` | P0 | `test_lan_inbound_passes_singbox_check` |
| T-134 | 认证文件格式非法（缺 `:`／空用户名／空密码）→ `get_lan_auth()` 返回 None | P0 | `test_get_lan_auth_rejects_malformed` |

**手工端到端验证**（单元测试盖不到真实网络行为，每次改入站/防火墙后必做）：

```bash
LAN=$(cat ~/.singbox_lan_auth); IP=<本机局域网IP>
# 1) 无认证 → 407        2) 错密码 → 407        3) 正确 → 204/200
curl -s -o /dev/null -w '%{http_code}\n' -x http://$IP:7891 http://www.gstatic.com/generate_204
curl -s -o /dev/null -w '%{http_code}\n' -x http://lan:wrong@$IP:7891 http://www.gstatic.com/generate_204
curl -s -o /dev/null -w '%{http_code}\n' -x http://$LAN@$IP:7891 https://api.github.com/zen

# 4) 来源限制：环回源进 7891 必须被丢。
#    判据看日志有没有 outbound 行，不要看客户端状态码 —— reject 也回 502。
curl -s -o /dev/null -x http://$LAN@127.0.0.1:7891 http://www.gstatic.com/generate_204
grep mixed-lan singbox.log | tail -4   # 被拒的连接只有 inbound 行，没有 outbound/
```

**易踩的三个坑**：

- `502` 有两种来源：节点不通（日志有 `outbound/...` + `i/o timeout`）与来源被拒（日志只有
  `inbound`）。只看 curl 返回码会把 guard 生效误判为故障。
- 改完配置**必须重启 sing-box**；`daemon.restart_singbox()` 是生产路径，直接改文件不重载。
- `refresh_subscription()` 在「节点数骤降」护栏下会拒绝覆盖配置。LAN 入站与节点列表正交，
  此时应就地注入入站（复用 `_build_inbounds` / `_build_lan_guard`），不要为准入而剥离护栏。

9router 侧无需改代码（已监听 `*:20127` 并按请求校验 API key），只需防火墙放行：

```powershell
New-NetFirewallRule -DisplayName '9router LAN (TCP 20127)' -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 20127 -RemoteAddress LocalSubnet -Profile Private,Public
New-NetFirewallRule -DisplayName 'auto-switch-ip LAN proxy (TCP 7891)' -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 7891 -RemoteAddress LocalSubnet -Profile Private,Public
```

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
| T-141b | P0 | 判闲观察窗按路径分三档，错误换线不得吃后台的 12s | `pytest test_daemon_unit.py -k error_path_uses_short_busy_window` | 错误换线路径 `reconnect_warp` 的兜底分支必须显式传 `_ERROR_PATH_BUSY_WINDOW_SEC`(2s)，`_wait_warp_idle` 的轮询窗同样传 2s；后台补刷/脏线守护用 `busy_window_sec`(12s)；被切下的主渠道排空用 `_DRAIN_BUSY_WINDOW_SEC`(30s)。WHY：12s 覆盖 SSE token 静默 p90=9.1s，但按候选逐条睡 12s 时最坏 ~120s 才把坏出口切走，期间它一直在服务失败请求（codex review P1）；30s 覆盖静默分布 max=16.1s 并留一倍余量，候选线的重启则完全不需要窗口（不在服务流量，等端口就绪即可） |
| T-141c | P0 | 判据 ② 不得要求 `download==0` | `pytest test_warp_refresh.py -k young_conn_has_bytes_but_stalled` | 连接已收过字节（download>0）但正处静默期 → 仍须判忙。旧判据要求 `download==0` 才计入等待连接，12:22 实测被重启掐断的 4 条连接各已收 ~1MB、年龄 94~116s、12s 零增长，同时命中判据 ①（零增量）和判据 ②（download>0 被跳过）两个漏洞。120s 年龄上限仍需保留：活跃线上有年龄中位 476s 的复用长连接 |
| T-142 | P0 | 被换下的线等不到空闲必须补刷 | `pytest test_warp_refresh.py -k retry_loop` | 首轮忙之后仍重试并刷新成功；恒忙/刷不出新 IP 时重试到 `ip_refresh_retry_sec` 为止。实测旧行为致 WARP-CF-C 被换下 8 次仅刷 2 次 |
| T-143 | P0 | 三条隧道全不可用时切轮换池 proxy | `pytest test_daemon_unit.py -k falls_back_to_proxy or skips_dead_standby` | 日志 `⚠ 无可用备线，切轮换池兜底` 后 `✓ 已切到轮换池 proxy`；**不**原地重连（D-Bus 断时必败） |
| T-144 | P0 | 停在 proxy 时可切回 WARP | `python3 -c "import daemon; daemon.reconnect_warp('手动验证')"` | 切回 WARP 前日志 `当前在轮换池 proxy，尝试切回 WARP 隧道`；成功后 `active_warp_tag()` 非空 |
| T-145 | P1 | 测试不得写真实 sing-box | `pytest test_daemon_unit.py -q`（fixture 含写操作防线） | 漏 stub 时 `AssertionError: 测试试图写真实 sing-box`，而非静默翻转生产 selector（实测发生过） |
| T-146 | P1 | 测试文件可被 pytest 收集 | `pytest test_warp_refresh.py --collect-only -q \| tail -1` | 收集到 15 个用例。函数式 `main()`+`check()` 写法 pytest 收集不到，`no tests ran` 仍退出 0 —— 假成功 |
| T-181 | P0 | 健康行标出当前是 WARP 还是轮换池 | `tail -50 daemon.log \| grep 出口=` | 前缀为 `WARP:<tag>(<ip>)` 或 `PROXY:<节点名>`，不再是无前缀的裸 tag |
| T-182 | P0 | 模式切换必须留日志 | `pytest test_daemon_unit.py -k switch_warp_logs_mode` + `grep '⇄ 出口模式切换' daemon.log` | warp↔proxy 切换打 `⇄ 出口模式切换: a -> b（x -> y）`；同模式内换线（W1→W2）**不**打 |
| T-183 | P1 | `asip` 隧道清单与 config.py 同源 | `asip warp status \| grep -c '^  :1'` | 条数等于 `config.py` 中 `WARP-CF*` 条目数（当前 10），不再硬编码 5 条 |
| T-184 | P1 | `asip` 跟踪日志轮转 | 轮转后跑 `asip s` | 用 `tail -F`（按文件名重开）；`tail -f` 过 `daemon.log`→`.1` 后停在旧 inode，实测不再输出 |
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
| T-160 | P0 | 未报错也主动换线（按已用配额） | `pytest test_daemon_unit.py -k "proactively_rotate or proactive_rotate"` | 无任何 403/429，当前出口上**成功的 dsv4 调用**累计满 `proactive_after_dsv4_ok`=150 即换线；未达阈值不换。WHY：报错后才换已经冲断在途对话。WHY 数成功而非请求条数：配额按成功的 dsv4 调用扣，把被 429 拒掉的请求也算成消耗会在风暴里越切越勤 |
| T-161 | P1 | 主动轮换遵守换线冷却 | `pytest test_daemon_unit.py -k proactively_rotate` | 冷却期内（换线后 20s）即使满足请求数也不换，防止请求突发把隧道变成永动机 |
| T-162 | P0 | 选线按「线」的最近选择时刻轮转 | `pytest test_daemon_unit.py -k longest_unselected_fresh_line` | 多条新鲜备线时选 `_WARP_LINE_STATE[tag].sel` 最小的那条。按 IP 使用表排序不成立：出窗口的条目会被清理，清理后所有备线并列最老，退化成 members 顺序（实测 D/E 被选中 0 次） |
| T-163 | P1 | 备线预热跳过新鲜线、只刷两条以内 | `pytest test_daemon_unit.py -k prewarm` | 挑最久没被选过且已知 IP 不新鲜的线调度刷新；`_WARP_REFRESHING` ≥2 时不预热。目的：换线时总有现成新鲜 IP 可直接切，省掉 7~9s 同步刷新。`blocked` 标记与「没有 `last_ok_ts`」的地址一律算不新鲜（T-176） |
| T-164 | P0 | 出口 IP 复用窗口 24h 生效 | `pytest test_daemon_unit.py -k test_warp_ip_reuse_window_24h` | `ip_reuse_guard_sec` 与 `blocked_ttl_sec` 均为 86400：23h 前用过的 IP 仍判「窗口内」并触发先刷新会话；超 24h 的条目被清理、可直接切。WHY：实测出口 IP 可用周期 p50=19s、max=65s，10~50 分钟切回几乎立刻再 403，一天内复用等于反复踩同一批烧掉的地址 |
| T-165 | P0 | cycle 样本采集与 24h 过滤 | `pytest test_daemon_unit.py -k test_warp_cycle_pruning_24h_window` | 换线生效记起点、该 IP 首次 403/429 记终点，样本 `{ip, cycle, ended_at, ok}` 落盘 `.warp_ip_cycles.json`（`WARP_IP_CYCLES_FILE_PATH` 可覆盖，测试隔离）；超 24h 的样本被清除 |
| T-166 | P0 | 已废弃：典型周期重算已随 `lifetime` 移除 | `grep -c "_recompute_min_warp_cycle\|_WARP_MIN_CYCLE_SEC" daemon.py` | 必须为 0。周期重算测的是请求间隔而非 IP 寿命（段跨度中位 3.8s，而请求本身 p50 6.5s），生效值恒被 60s 地板顶住，等于一个测不准的固定 60s。样本台账 `.warp_ip_cycles.json` 保留，只作离线诊断与 `sampled` 去重 |
| T-167 | P0 | 主动轮换只绕成功换线后的冷却 | `pytest test_daemon_unit.py -k "keeps_fail_cooldown or proactive_rotate_respects_cooldown"` | 满足阈值时传 `ignore_cooldown = _last_warp_reconnect_ts <= _last_warp_switch_ok_ts`，即只绕「成功换线后」的 `cooldown_ok_sec`；上次换线失败时 `_warp_cooldown_until` 是 `cooldown_fail_sec`，不绕（防连切连败把隧道刷成雷暴）。并发始终由 `_WARP_LOCK` 保证 (T-169) |
| T-167b | P0 | 冷却期内的 403 仍须标脏 IP 并采到周期样本 | `pytest test_daemon_unit.py -k test_maybe_reconnect_marks_ip_blocked_inside_cooldown` | 冷却未结束时触发采样：出口 IP `blocked=True`、本段写入一条 `cycle = now - _last_warp_switch_ok_ts` 样本、且不 spawn 换线线程；同段重复触发不重复采样。修复前这些段整体被丢弃，IP 不标脏会被下次换线当干净地址选回 |
| T-168 | P0 | 配额计数只数成功的 dsv4 调用 | `pytest test_daemon_unit.py -k count_dsv4_ok_since` | `count_dsv4_ok_since(ts)` 只统计 `provider=opencode` + `model=proactive_quota_model` + `status='ok'` 且 `timestamp > ts` 的行（严格大于，恰在边界的行不计）。起点非正直接返 0 不查库；查库失败返回 0 并记日志。走 `INDEXED BY idx_uh_ts`：指定 provider/model 过滤要扫 18 万行（实测 250ms），时间索引 0.1ms，而它每 5s 被调一次。异常只吞 `sqlite3.Error`：配置缺键（KeyError）必须抛出，否则判据静默变 0、永不触发（`test_count_dsv4_ok_since_does_not_swallow_config_errors`）；`config.py` 导入时另校验 `proactive_mode` 取值与 `proactive_quota_model` 非空。阈值 150 的实测效果（2026-10-22 12:23~12:33 直连观察 5 段）：3 段跑满（138s/150 格、167s/151 格、116s/151 格，均本判据主动换线），1 段在 86 格撞 `rate429 ×3` 由错误判据接管，1 段进行中 —— 跑满时每段约 2~2.8 分钟。早先「T=150 仅 3% 段可达」的回放取自 fixed 模式（205 段中位段长 61s），是右删失，**不可用来判断 requests 模式的阈值** |
| T-169 | P0 | ignore_cooldown 仍受 `_WARP_LOCK` 约束 | `pytest test_daemon_unit.py -k test_ignore_cooldown_still_requires_warp_lock` | 锁被占用时返回 False 且不 spawn 换线线程；锁空闲才发起。WHY：绕过冷却只是放宽时间判据，不能同时放宽并发互斥，否则两个换线线程会互相踩隧道 |
| T-170 | P1 | 预热按 24h 流量门控 | `pytest test_daemon_unit.py -k test_prewarm_gate_uses_24h_window` | 最近 24h 内有 opencode 请求才预热备线（2h 前的旧流量仍算有）；24h 内零请求不预热。旧窗口 10 分钟会让冷启动/低峰期永不预热 |
| T-171 | P1 | 全不新鲜时按休息时间兜底选线 | `pytest test_daemon_unit.py -k test_reconnect_warp_picks_longest_rest_when_none_fresh` | 所有备线都刷新不出 24h 窗口外的新 IP 时，选 `now - 上次使用时刻` 最大的一条；日志打印该 IP 休息时长与样本平均休息时长，切前仍先刷新会话 |
| T-172 | P0 | 补刷不得重启已成当前出口的线 | `pytest test_warp_refresh.py -k test_retry_loop_aborts_when_member_becomes_active` | 空闲后这条线已被切成当前出口时必须放弃并返回 False，不得调 `_refresh_idle_warp`（重启会掐断在途连接 → upstream connection lost）。判据落在**重启之前**而非循环开头：换线线程的快照早于本次调度，等空闲的 40s 内这条线才变成出口 —— 放在本轮开头挡不住这一段。线下次被换下时 `reconnect_warp` 会重新调度补刷 |
| T-173 | P0 | IP 使用记录与周期样本须原子落盘 | `pytest test_daemon_unit.py -k test_persistence_write_is_atomic_and_logs_failure` | `.warp_ip_used.json` 与 `.warp_ip_cycles.json` 走「写 `.tmp` → `os.replace`」；写入失败时原文件保持完好（不先截断），并记 `✗ 写 xxx 失败`。WHY：这两份记录是重启后唯一能恢复「哪些地址已被烧」的证据，直接 `open(w)` 在崩溃/掉电时留半截 JSON → 下次启动解析失败 → 记录全丢 → 脏地址被当干净地址切回（实测 22:22 重启即如此，22:27 切回已烧地址，403 持续 144s）。旧实现的 `except: pass` 让这种丢失无从察觉 |
| T-174 | P0 | 补刷在途不得被并发换线踩到 | `pytest test_warp_refresh.py -k "retry_loop"` | `_WARP_REFRESH_LOCK` 用 RLock：`_prewarm_standby_warp` 持锁后调 `_schedule_idle_refresh`（后者同锁）不再自锁死。另：补刷循环里查当前出口的真函数 `active_warp_tag()` 会发 Clash API 请求，测试必须打桩 —— 否则假时钟 tick 预算被吃光且结果取决于线上此刻哪条线在服务（实测同一用例在不同时刻分别命中 WARP-CF-E / WARP-CF-C，flaky） |
| T-175 | P0 | 备线出口全被标记时刷新而非跳过 | `pytest test_daemon_unit.py -k "refreshes_blocked or falls_back_when_blocked"` | 探测到备线出口 `blocked` 时不跳过、不直接落 proxy：重启该线换会话 IP 后切过去；所有脏线都刷不出新地址才落 proxy。WHY：每条隧道同一时刻只有一个出口 IP，全跳过会让候选池恒为 0（403 风暴几秒就烧完全部备线），流量被甩到同样被限流的池 —— 实测 19:16 兜底死循环。刷新后仍拿回 blocked 地址也视为失败 |
| T-176 | P0 | 疑似脏 IP 的备线必须重连到换出优质 IP | `pytest test_daemon_unit.py -k "prewarm_refreshes_blocked or schedules_refresh_for_blocked"` | 巡检把 `blocked` 或复用窗口内的 IP 都判为脏（含没有 `last_ok_ts` 的 blocked 地址）；换线成功时立即把剩余脏备线排进补刷。巡检在 proxy 池上同样运行，最多两条并发，直到 `_retry_refresh_idle_warp` 重试上限 |
| T-179 | P0 | 主动轮换两种判据互斥可切换 | `pytest test_daemon_unit.py -k "proactive_mode"` | `proactive_mode=requests`（**当前生效**，满 `proactive_after_dsv4_ok`=150 格成功的 dsv4 调用）/`fixed`（每 `proactive_fixed_sec`=60s）。**两者互斥**：fixed 生效时不读配额，requests 生效时不因时长换线（`test_proactive_modes_are_mutually_exclusive`，去掉分支末 `return` 即失败）；非法值（含遗留的 `lifetime`）静默回退 `fixed`，不让轮换停摆 |
| T-180 | P0 | 已废弃：`lifetime` 判据整模式移除 | `pytest test_daemon_unit.py -k lifetime` | 断言写 `lifetime` 的旧配置静默回退 `fixed` 且仍会轮换。移除理由：被测量本身错 —— 段跨度（首条成功→末条成功）跟的是请求间隔（中位 3.8s，而请求 p50 6.5s），且在 60s 上限处右删失 |
| T-178 | P0 | 脏线巡检为常驻守护线程且不占用换线候选 | `pytest test_daemon_unit.py -k "dirty_janitor or dirty_worker"` | `_dirty_line_janitor` 无周期闸门（`while True` + 5s 防打爆），非当前出口的线全被巡检覆盖。探测阶段只占 `_WARP_PROBING`，**确认脏后才占 `_WARP_REFRESHING`**：后者会让换线路径把该线当「不可选」，探测中的干净线若也占进去，换线会看到「备线 0 条」直接落 proxy（实测重启后 17s 踩到）。worker 在重启前复查该线是否已变当前出口，变了就放弃，否则掐断在途连接 |
| T-177 | P1 | 当前出口 IP 补探与成功流量打点 | `pytest test_daemon_unit.py -k "init_active_ip or record_active_ip_success"` | 启动探测失败时后台补探 `_ACTIVE_WARP_IP`（30s 最多一次）；当前出口没有记录时补建条目再写 `last_ok_ts`，否则选线日志恒为「有成功记录 0 条」 |
| T-185 | P0 | 错误归属按请求**开始**时刻，不按完成时刻 | `pytest test_daemon_unit.py -k attributes_by_request_start` | 9router 在请求结束时才写 `requestDetails` 行（`requestDetailsRepo.js:102`），而换线用 `interrupt_exist_connections=false` 不掐在途请求；延迟 p50 8.8s / p90 27.9s 与换线间隔 p50 8s 同量级。实测 2000 行里 38.4% 的请求生命周期跨越一次换线，按完成时刻归属会把上一出口的 429 算成新出口的错。窗口起点改为 `timestamp − latency.total`（`_START_TS_SQL`）。重放 34 次真实触发：旧逻辑 33 次命中、新逻辑 20 次，14 次是纯误触发 —— 这些全是「切上去即报错」的 1 条 429，重新归属后该出口根本没收到过错误 |


| T-186 | P0 | 连错 ≤2 条 → 不换线（连错门控） | `pytest test_daemon_unit.py -k "consecutive_errors or third_consecutive"` | `count_warp_blocked_since()` 增加 `consec_err`（从窗口最新一条往前数、停在第一条成功处的连续失败数）。`maybe_reconnect_warp()` 在阈值命中且**采样/标脏之后**、冷却判断之前拦截 `consec_err < 3`：用户规则「刚切线的 10 秒内允许出现 2 次报错而不切线」。门控必须放在采样之后 —— 连错 1~2 条时出口已收到 403，该 IP 仍须标脏并记周期样本，只是暂不换线。实测 90 次真实触发中该门控抑制 11 次（12%），其中 3 次窗口内成功 ≥5（出口明显健康，换掉即误切）。旧实现看 `last_ok`（窗口最新一条是否成功）：在 429 风暴中成功与错误交替，该判据每行翻转，90 次触发里只抑制 1 次（1%），拦不住误切 |
| T-187 | P1 | 满窗 10 条全错 → 绕过冷却立即换线 | `pytest test_daemon_unit.py -k full_error_window` | `count_warp_blocked_since()` 增加 `full_err`（`len(rows) >= RECENT_WINDOW and ok == 0`）。`maybe_reconnect_warp()` 的冷却判断改为 `if now < _warp_cooldown_until and not full_err`。实测 67 次触发里满窗 0 次（429 风暴中总夹杂成功），保留为风暴兜底 |
| T-188 | P1 | 换线后冷却 5s（原 1s），防「刚换就错」连续触发 | `grep -A1 '"cooldown_ok_sec"' config.py` | 新出口首条错误延迟实测 n=52：p25=0.7s / p50=1.6s / p75=4.5s / p90=10.5s。1s 冷却让 40% 的「刚换就错」在冷却后立刻再换，构成日志里的锯齿。5s 覆盖 79%，且被 T-187 的满窗例外兜底 |
| T-189 | P0 | `upstream connection lost` 不得触发换线/切节点 | `python3 - <<'EOF'`：查 `requestDetails` 中含该串的行的 `status`；`grep -n "upstream connection lost" daemon.py` | 该文案是 9router SSE 管道（`~/.9router/.next/server/chunks/8895.js`）`m` 变量的初始值，作为末帧注入流中，**不是错误**：实测全表 18 行含该串的记录 `status` 全为 `success`（17 行出现在 `response`、1 行在 `providerResponse`）。`count_warp_blocked_since()` 只按键词 `abusive traffic` / `Rate limit exceeded` 分类、`get_9router_error_rate()` 与既有一切换线判据都以 `status != success` 为准，因此该串在两条链路上都不计入错误、不触发换线。回归方式：出现新的 error 类文案时，先按 `status` 过滤确认它是否真被记为 error，再决定是否要加分类 —— 不要按文案字面加进错误率 |
| T-190 | P0 | 模型模式：探针通过即视为可用，IP 历史不作判据 | `pytest test_warp_refresh.py -k "model_mode or old_mode"` | `refresh_model_check=True` 时 `_ip_disqualified()` 恒返 False：被 403/429 标记过、在 24h 复用窗口内都用「此刻实测」代替历史推断。依据：实测重启后从没成功过的新地址首条请求照样 429（8/10 线），旧地址过了惩罚期也可复用 —— 历史与事实不一致时以探测为准。原模式（`refresh_model_check=False`）保留整套历史判据，一行开关切回 |
| T-191 | P0 | 刷新备线必须过模型层探测 | `pytest test_warp_refresh.py -k model_probe` | `_refresh_idle_warp()` 拿到新地址后调 `probe_warp.probe()`，只有 `OK` 才算刷新成功；`quota`/`flagged`/`unreachable`/`err` 都继续重启。原模式只验 IP 新鲜度，新地址必然通过，于是切上去头几条就 429（实测 10 条线 5 条不可用） |
| T-197 | P0 | 429 文案换版必须仍判为出口级错误 | `pytest test_daemon_unit.py -k classify_egress` | 免费层 429 现版文案是 `Free usage limit reached. Subscribe to OpenCode Go...`（type 仍 `FreeUsageLimitError`），旧版是 `Rate limit exceeded`。只认旧文案时实测最近 2000 条错误里 1751 条 429 全被判成「非出口级错误」→ `consec_err` 恒 0 → 87% 错误率下也永不换线（生产宕机 2026-10-22 13:45，daemon 重启后落 proxy 池不自救）。两个文案都留，分类器对 4 类样本：现版 429 / 旧版 429 → `rate429`，`abusive traffic` → `abuse403`，请求超长 → `None` |
| T-198 | P0 | 同一 IP 被多条隧道持有时只留一条候选 | `pytest test_daemon_unit.py -k dedups_same_ip` | 重连竞态会让两条隧道指向同一会话 IP；两条都进候选池等于「换了隧道但 IP 没变」当换线 —— 切过去照样 429。`_cached_warp_standby` 排序后按 IP 去重，只留最优的一条（已排序，首条即最优）。实测 20 条隧道当前 20 个唯一 IP，无重复，但那是运气不是约束 |
| T-199 | P0 | 启动时当前出口低质必须立即换线 | `pytest test_daemon_unit.py -k startup_` | sing-box 重启后 selector 复位到配置 default（主隧道 WARP-CF），它可能正处 429 惩罚期或被模型层判 quota。不校验就等于「重启后先白烧一段 IP，等第一条 429 才纠正」—— 实测 2026-10-22 13:45 重启后 WARP-CF 立即 429 ×3 才被换走。`main()` 启动即校验 `_warp_ip_needs_refresh`，低质则 `_reconnect_warp_now("启动校验 当前出口低质")`。健康出口不得被动（对照用例）。校验体抽成 `_validate_startup_egress()`：用例原先复刻分支逻辑，main 里删掉整段仍会通过（codex P3）。换线是异步线程，校验后不得复探 `active_warp_tag()`（读到旧线 + 白等 20s） |
| T-200 | P0 | 刷新路径探测超时必须复探一次 | `pytest test_daemon_unit.py -k probe_refresh_egress` | 短预算 7s 只覆盖「隧道没起来」，漏了「首探冷」：实测 `usque-warp-20` 重启后端口 0.12s 就绪，7s 预算首探 7.01s 到点 rc=28，紧接着同预算探测 1.68s 就返回 —— 隧道是好的。旧实现单次探测失败即记 attempt 失败 → 重启同一条线 → 还是冷的 → 循环：实测 14:58 单分钟 60 次 `systemd Started`，「刷新 #3 失败 → 同线再重启」实测 135 次，间隔以 65s 为最多（38 次，即 3 次重启 + 2/5/10 退避的固定节奏），全库共 748 条「出口探测失败」。`_probe_refresh_egress()` 先短探、失败再用 `ready_timeout_sec` 复探一次；首探成功不得重探（每次探测都走出口配额）；两次都失败才返回 None。三个用例：慢隧道复探成功 / 两次都败返回 None / 首探成功只探一次 |
| T-208 | P0 | 换线候选不得等于当前出口，且候选间不得重复 | `pytest test_daemon_unit.py -k exclude_active_ip` | 实测 2026-10-22 08:03~17:59 共 817 次换线，其中 **111 次（13.7%）新出口 == 旧出口**，全部由 rate429 触发 —— 切到同一个地址等于没换，白白重启了一条隧道（每条隧道同一时刻只有一个出口 IP）。`_exclude_active_ip(pairs, old_ip)` 滤掉等于当前出口与互相重复的候选，保留首条（调用方已按选线优先级排序）；全被滤空时退回原列表 —— 宁可换到旧地址，也不能因一条理论约束把流量甩进轮换池（池有 3h 冷却）。**位置必须在所有候选来源汇合之后**：缓存快速路径、并发探测、同步刷新三条分支都会往 `probes` 追加条目，放在实测分支之后会被同步刷新绕过（首版即错在此）。三个用例：滤掉当前出口 / 跨隧道同 IP 只留首条 / 全滤空时退回 |
| T-222 | P0 | 脏 IP 的惩罚期排除必须在探测**之前**做，探测之后的 OK 不得再被旧台账否决 | `pytest test_warp_refresh.py -k "excludes_blocked_ip_before_probing or trusts_probe_not_history or state_is_isolated"` | T-211 之后的回归：`_ip_disqualified` 两模式都查惩罚期，但 `reconnect_warp` 在两处用它 —— **入池前**（缓存路径 / 探测循环 / 刷新路径）该查，**实测后再复查**就等于用上一段的标记推翻本刻的观测。现场：历史标脏的地址实测 OK 却被丢，候选池清空 → 落轮换池 proxy（3h 冷却）。修法：①探测循环的惩罚期判据去掉 `_ip_history_is_truth()` 前置 —— 挡在探测前才省配额，探过再否决等于每格都付了学费；②选线末尾模型模式下不再用历史过滤 `probes`（原模式照旧）。同时补测试隔离：`WARP_PROXY_FALLBACK_FILE_PATH` 未隔离时任何走到 `_fallback_to_proxy` 的用例都会把落池时刻写进生产目录，下一次运行命中 3h 冷却改走盲切分支 —— 断言由上次运行的残留作答（实测同目录连跑两次 17s/失败 与 0.06s/通过 相反）。三处变异验证全部转红 | T-222 |
| T-211 | P0 | 脏 IP 排除必须在模型探测**之前**生效，模型层判 quota/flagged 要落进 blocked 台账 | `pytest test_warp_refresh.py -k "keeps_penalty or verdict_marks or verdict_maps or verdict_does_not"` | 两个缺口：①`_ip_disqualified` 在 `refresh_model_check=True` 时恒返 `False`，连惩罚期也旁路 —— 脏 IP 设计对模型模式整体失效；②`_warp_model_usable` 判 quota 后只写探测缓存、不写 `_WARP_IP_USED`，那格配额烧完什么都不剩。实测 7 个烧毁段里 **648 个不同 /128 被逐个探测、608 个（94%）判 quota**，占全天 3660 次探测的 17.7% —— 全是本该在探测前挡住的。修法：`_warp_ip_is_blocked` 提到 `_ip_history_is_truth` 之前（惩罚期是确定事实，复用窗口才是启发式）；新增 `_MODEL_VERDICT_BLOCK_KIND = {quota: rate429, flagged: abuse403}`，记账时 `forget_probe=False`（同一次观测的产物不该被自己清掉，否则下次调用重探一遍再烧一格）。五个用例覆盖：惩罚期两模式都排除 / quota→rate429 / flagged→abuse403 / OK 不标脏 / 结论留在缓存 | T-211 |
| T-210 | P0 | 模型探测不可用时必须降级到历史判据，不得把探测通道故障当成线路故障 | `pytest test_warp_refresh.py -k blind` | 判据：一整批探测**全部** `unreachable`。实测 16:50:18 一批 17 条同一秒全灭，重启换 IP 后 16:50:50~52 全部 `OK` —— 那 17 次重启与配额全是白烧；今日 2345 批中 13 批为整批 ≥3 条全灭（最高 17/17）。失明 120s 内 `_ip_history_is_truth()` 返回真、三档与复用窗口重新生效，且不再发探测（省配额）；缓存的 `unreachable` 也不再驱动重启与刷新理由。单条 `unreachable` 不算失明（TTL 仍 2min），四个用例覆盖：整批降级 / 部分失败不降级 / 失明时不再探测 / 失明时旧结论不驱动重启 | T-210 |
| T-209 | P1 | 分级选线的三档语义与 `blocked_ttl_sec` 的必要性需要用数据支撑 | `pytest test_daemon_unit.py -k "warp_ip_rank or disqualified"` | 三档：0=从未出现（台账里没有）/ 1=有过成功流量且已过惩罚期（403 6h、429 24h，**可复用不是废址**）/ 2=出现过但从没成功过（实测 350 条记录里 97 条）。档内再按 /64 静置排：opencode 免费额度按出口段计，新 /128 落在刚用过的 /64 里首条请求照样 429。`blocked_ttl_sec`=86400 不是「永久拉黑」：实测历史 19790 条记录中「上一次记录=已标记」之后再次出现的 6421 次，**本次未标记 6199 次（96.5%）**，只有 222 次（3.5%）又被标记 —— 惩罚期满后地址大概率已恢复，24h 只是保守上界。`last_ok_ts` 运行时确在写：台账 1858 条中 1700 条（91.5%）有该字段，最近 30min 内更新 24 条 | - |
| T-221 | P0 | 出口地址被 403/429 标记后，它的旧探测 OK 必须立即作废 | `pytest test_warp_refresh.py -k "invalidates or forget_by_ip"` | 模型模式下历史不否决候选、只认 `_WARP_PROBE_SEEN` 台账，而 OK 的瞬时档 TTL=120s 远长于实测段寿命（凭缓存 OK 选中的段 p50 6~7s，凭实测 OK 选中 p50 95s）。后果是闭环：**829 次换线里 170 次（20.5%）是同一 IP 在 ≤120s 内被重新选中，170/170 都回到同一条隧道**；其中 **158 次（92.9%）所依据的 OK 早于该 IP 的标脏时刻**（11 次晚于，1 次无脏标记）。这不是「历史没被排除」——模型模式下历史本就不否决；是**证据比事实旧**。修法：`_mark_warp_ip_used(blocked=True)` 后调 `_probe_cache_forget(ip)`，按 **IP** 全清（被烧是地址属性，同一地址可能被两条线先后持有，只清当前 tag 会留下另一条的 OK）。两用例：标脏即失活 + 按 IP 跨线全清且不牵连别的地址。变异验证：把 `_probe_cache_forget` 打成空操作，`_probe_cache_verdict` 立刻回 `OK`，用例转红 | - |
| T-192 | P0 | 选线也要实测，不能只靠刷新阶段的门 | `pytest test_warp_refresh.py -k selection_trusts_probe` | 12s 探测缓存只验过出口 IP，可能送来一条此刻已 429 的线；`reconnect_warp()` 在选线前对全部候选并发跑 `_warp_model_test_many()`。实测 20:28 一次选线：8 条缓存命中 → 并发实测 → 仅 4 条 OK → 切到其中一条 |
| T-193 | P1 | 探针与 daemon 同解释器可跑（无 PySocks）、指纹校验覆盖 schema | `/usr/bin/python3.12 probe_warp.py --tag WARP-CF-C`；`python3 probe_warp.py --check-fingerprint` | 走 curl `--socks5-hostname` 而非 python `requests` 的 `socks5h://`：daemon 跑在 `/usr/bin/python3.12`（无 PySocks），用 requests 会立即 `InvalidSchema` 并被误计成 `unreachable`（实测 0.0s 返回）。指纹校验比对 description **与 parameters 两者**（与上游 `isFingerprintDefinition` 对齐）：只比 description 会漏掉 schema 变更，线上探测全 403 而校验仍报「一致」。归一化去空白/引号/逗号（JS 对象字面量与 `json.dumps` 的差别），已反向验证：篡改 `required` 后校验必红 |
| T-194 | P0 | 模型层测不过的备线必须重连，不得直接落 proxy 池 | `pytest test_warp_refresh.py -k "probe_failure_reconnects or after_reconnect_still_falls"` | 测不过的线与原模式「出口已被标记」同义 —— 都只有重连换会话 IP 才有意义，故并入 `blocked_lines` 走同一条刷新路径。回归点（codex P1）：阻塞块原本在实测之前，实测清空 `probes` 后一条备线都没重启就走向「无可用备线，切轮换池兜底」，T-175 退化；且任何系统性探测故障（缺文件/指纹漂移/curl 失败）都会把全量流量拨到 83 节点池。正确顺序：实测 → 同步重连一条拿本次候选 → 其余后台并发补刷 → 仍无可用才落池 |
| T-195 | P0 | 生效窗口内的陈旧错误不得把新出口标脏 | `pytest test_warp_refresh.py -k "min_gap_does_not_mark or real_error_after_new_exit"` | 触发→生效 p50=4s，新出口刚上线时上一段的在途请求才报错。旧顺序（先采样/标脏再查 min-gap）会把刚验证过的好出口记成坏地址 6~24h，且 `_record_warp_cycle` 以陈旧首错记段，令 `sampled` 恒真、本段永远采不到真正的结束点（实测 46 次触发里 39 次是这一类）。判据：生效不足 `min_switch_gap_sec` **且** 生效之后一条错误都没有（`count_warp_blocked_since(_last_warp_switch_ok_ts)` 的 403/429 均为 0）→ 跳过标脏与记段。对照组：新出口自己的错误必须照旧标脏，硬闸仍拦住换线 |
| T-196 | P1 | 模型层探测台账按 /128 键控、TTL 按结论持久性分档 | `pytest test_warp_refresh.py -k "probe_cache or once_per_address"` | 30h 冻结快照 N=10113 回放：同一 /128 重探，判 quota 后 99.2% 仍 quota（n=2400，30h 内仅 1 次恢复、间隔 56min），判 OK 后复现率 <10s 97.9% / <2min 94.6% / <15min 91.0% / <1h 76.5% / ≥1h 43.2%。故 TTL 不按好坏而按持久性分档：`quota`/`flagged` 用 `probe_persistent_ttl_sec`=21600，`OK`/`unreachable`/`err` 用 `probe_transient_ttl_sec`=120 —— 后者是**探测侧故障**而非地址状态（日志实测同一地址 err 后 66s 即 OK），会话代次删除后套长 TTL 会让一次抖动把健康线锁死 6h（codex review 发现）。**键不按 /64 聚合**：控制变量（排除日志没探过但生产用过的 IP）后已烧段全新 /128 OK=0/2469、未烧段全新 /128 OK=3/152 —— 段非充分条件，判死的是地址自己。会话代次（epoch）已删：它让每次重连作废全部结论，71% 的探测打在已判 quota 的地址上、99.1% 结果仍 quota。台账结论仍须回灌 `_warp_ip_needs_refresh`（T-189 遗留的焊死点） |


> 主动轮换判据的数据依据（2026-10-22 重算，取代此前所有基于 `.warp_ip_cycles.json` 的容量估计）：按日志时间轴切段 + `usageHistory` 按**开始时刻**归属（10.8h / 48617 条成功，覆盖率 100%），**主动轮换（配额未尽）204 段：dsv4 成功中位 113.5 / p90 175 / max 321**；被动 429 段 1548 段中位 10。两个关键读法：① 主动段是**右删失下界** —— 我们在 60s 到点就切了，线还没死，所以单 IP 容量至少 113.5，不是 113.5 左右；② 429 段中位 10 **不能当容量** —— 429 段之后紧接的下一段中位仅 9，而主动段之后出现的 429 段中位 53，同一批请求流在两条路径上差 5 倍，429 段捕捉的是「配额已耗尽后的残段」。采集口径注意：`.warp_ip_cycles.json` 只记错误终止的段（`_record_warp_cycle` 唯一调用点在错误路径），主动轮换不留样本，直接读它会把 `ok` 中位 5 误读成容量（T-166/T-180 已把该重算逻辑删除）。

路由两条硬约束（踩过的坑）：
1. `domain_suffix` 写 `opencode.ai`，写 `.opencode.ai` 匹配不到裸域；
2. 出站规则必须排在 `action: resolve` 之前，resolve 后 destination 变成 IP，domain 规则不再命中。

## 12. 可移植性与部署（T-212~T-215）

背景：此前所有路径写死 `/home/administrator/...`，23 个 systemd 单元只存在于本机
`~/.config/systemd/user/`（不入库），换用户/换目录/换机器即失效。目标：clone 到任意
目录、任意用户，跑 `./deploy/install.sh` 即可部署。

| ID | 级别 | 测试点 | 方法 | 预期 |
|----|------|--------|------|------|
| T-212 | P0 | 项目内路径随仓库走 | `pytest test_portability.py -k resolve_inside_repo` | `binary`/`config_file`/`log_file` 均在 `PROJECT_DIR` 下；副本目录 + `HOME=/tmp/fakehome` 时仍指向副本 |
| T-213 | P0 | 运行期文件无硬编码家目录 | `pytest test_portability.py -k hardcoded_home` | `config.py`/`daemon.py`/`parse_subscription.py`/`probe_warp.py`/`self_evolve_checkpoint.py`/两个脚本中 `/home/<user>` 出现 0 次 |
| T-214 | P0 | 隧道成员可部署且单元全覆盖 | `pytest test_portability.py -k deployable`、`-k renders_every_member` | tag/port/service 各自唯一；`deploy/members.py` 输出行数 == `members` 长度，且地址族开关逐条一致 |
| T-215 | P1 | 单元生成与现网一致（地址族不退化） | `SYSTEMD_DIR=$(mktemp -d) ./deploy/install.sh --units-only`，比对 `ExecStart` | 1080/1081/1082/1087/1088 含 `-6`，其余不含；目录与端口按 1080→`usque`、1081→`usque2` … 1099→`usque20` |
| T-216 | P0 | `.env` 的 `$HOME/x` 两条启动路径同解 | `pytest test_portability.py -k expands_home`（`NINE_ROUTER_DB_PATH='$HOME/...'`） | 解析结果中无 `$`，且以真实家目录开头。**实测 systemd `EnvironmentFile` 不展开 `$HOME`，原样传入**（`journalctl` 显示 `VAL=$HOME/.some/path`），而 start 脚本 `source` 会展开 —— 展开必须落在 `_env_path`，否则同一份 `.env` 两种结果 |
| T-217 | P0 | 符号链接不得覆盖主二进制 | `pytest test_portability.py -k links_main_usque` | `install.sh` 创建 `.warp/usqueN/usque` 时必须排除 `usque/` 自身。`for d in .warp/usque*/` **包含主目录**，实测沙箱内执行后 `cat .warp/usque/usque` → `Too many levels of symbolic links`，20 条隧道全部 ELOOP |
| T-218 | P1 | 单元解释器不跟调用者 PATH 走 | `pytest test_portability.py -k pins_interpreter` | `PYBIN="${PYTHON:-/usr/bin/python3}"`；首版用 `command -v python3`，在 conda shell 里生成 `ExecStart=.../miniforge3/bin/python3`，与现网 `/usr/bin/env python3` 不一致且换 shell 即变 |
| T-219 | P2 | 隧道注册失败必须让部署返回非零 | 人工：断网跑 `./deploy/install.sh` | 收集失败条数，末尾以非零退出。首版只打 `⚠` 继续 `enable`，注册失败的单元会立即退出并无脑重启，而部署看起来是成功的 |
| T-223 | P0 | 补刷无全池并发上限，只有单 tag 互斥 | `pytest test_daemon_unit.py -k "multiple_lines_can_hold_slots_at_once or refresh_has_no_pool_wide_cap"` | 20 条线同时申请名额必须全部放行；同一 tag 二次申请必须返回 False。全池上限 `_refresh_concurrency` 已删除 —— 它默认等于成员数所以平时不生效，生效时日志报的「N 条在重启」是上限常量而非实际集合大小：实测 134 次「名额已满」中 128 次（95.5%）是同一个 tag 自己撞自己 |
| T-224 | P1 | 刷新日志必须区分「离渠总时长」与「上次成功」 | 人工：观察 `刷新成功` / `模型层:` 行 | 格式为 `（离渠后第 N 次刷，X min，上次成功 Y min 前）`。`X min` 含静置期不是单次耗时：实测 WARP-CF-C `65.4min/第 29 次刷` 中 45.6min 是 OK 着不动，只有 34 秒在真刷新；757 次刷新 0% 超 60s。地址由 usque 自行更换 —— 同 tag 间隔 10~30s 时 99.4% 换了地址，所以「刚 OK 又判 quota」是地址被换掉，不是结论被无视 |
| T-225 | P0 | OK 探测结论单独一档 TTL（600s），不与 err/unreachable 同档 | `pytest test_warp_refresh.py -k ttl_by_persistence` | 三档：quota/flagged 21600s > OK 600s > err/unreachable 120s。判据是同一 `(tag, /128)` 的重探间隔中位 212s —— 120s 档位命中不到自己，实测 1980 次「前一次 OK」里 1831 次（92.5%）结论不变；按 600s 可省 33.8% 探测量，判错 8.0%，1200s 只再省 0.1 个百分点。安全网是标脏即作废：47 例 OK→quota 里 45 例期间有生产侧 429/403 换线走 `_probe_cache_forget`，变异验证把该调用换成 `pass` 后回归用例立即转红 |
| T-226 | P1 | `/64` 段状态不得升级为选线准入或跨档判据 | 时间序回放台账（段状态取探前 `blocked_ts`） | 只用于同档内静置排序。回放结果：T0 已烧段 OK 56/1024（5.47%）vs 干净段 1/108（0.93%），T1 已烧段 95.44% vs 干净段 98.59% —— 干净段里的全新地址并不更好。反例记录：按「24/24 quota 地址落在已烧段内」曾推断段级过滤可省 1762 格，那是用被判死的探测反推段状态的自证偏差 |

补充（自对抗审查记录）：并发会话改 `daemon.py` 期间，`test_warp_refresh.py` 有 2 例瞬时失败 —— 失败跑在 20:44，而 `daemon.py` 的 mtime 是 20:46，即跑的是半截改动；修复落盘后两次复跑均 **284 passed**。教训：混跑结果异常时先看被测文件 mtime 是否晚于测试启动时刻。

补充（无自动用例，部署时人工核对）：

- `.warp/warp-outbound.json` 缺失时 `install.sh` 自动从 `config.py` 重建；重建后
  `parse_subscription` 产出的配置里必须有 `opencode-out` 与 20 条 `WARP-CF*`。
- `install.sh` 的前置检查要报出缺失的 `sing-box` / `.warp/usque/usque` / Python 依赖，
  而不是让单元静默起不来。
- 密钥文件已存在时不得覆盖（覆盖会让正在跑的 sing-box 掉线）。

## 13. 共享层与按线 worker（T-230~T-239）

**范围**：`asip/{proc,http,egress,atomic_io,clash_client}.py`、`warp_line_worker.py`、
daemon 的 asip 边界。全部用例离线（monkeypatch 子进程/HTTP，不访问 Clash 与订阅源），
单文件运行 <1s。

```bash
python3 -m pytest test_asip_proc.py test_asip_http.py test_asip_egress.py \
    test_asip_atomic.py test_asip_clash.py test_asip_integration.py \
    test_warp_line_worker.py -q
```

| 编号 | 级别 | 验证点 | 方法 | 通过标准 |
|------|------|--------|------|----------|
| T-230 | P0 | 子进程不得无超时 | `pytest test_asip_proc.py` | `timeout<=0` 抛 `ValueError`；超时返回 `timed_out=True` 且 `code=None`；二进制缺失返回 `error="binary not found: ..."` 而不抛 |
| T-231 | P0 | 曲奇/代理参数不经 shell | `grep -n "shell=True" asip/ proc 调用点` | 全仓 0 命中；`run_curl` 的 socks/http 代理互斥，HTTP 变量为 `--proxy socks5h://<host>:<port>` |
| T-232 | P0 | 传输失败与 HTTP 错误必须可区分 | `pytest test_asip_http.py test_asip_integration.py` | 传输失败 `status=None`；`singbox_api` 包装后为 `(0, None)`；4xx/5xx 原样返回状态码；重试仅在显式 `retries>0` 时发生 |
| T-233 | P0 | 出口探测保留失败原因且要求 warp=on | `pytest test_asip_egress.py` | `probe_egress` 返回 `curl_missing/timeout/rc/bad_body/invalid_ip/warp_off` 优先级最高者；`warp_egress_ip` 传入 `require_warp=True` 与配置里的 `socks_host` |
| T-234 | P0 | JSONL 追加不得交错 | `pytest test_asip_atomic.py -k concurrent` | 8 线程 × 50 行后 400 行全部可 `json.loads`；文件权限 0600（已存在的 0644 会被收紧） |
| T-235 | P0 | read-modify-write 必须持锁且失败不落盘 | `pytest test_asip_atomic.py -k update_json` | 有 `lock_path` 时全程持 `flock`；`merge` 抛异常后原文件字节不变 |
| T-236 | P1 | Clash 节点名必须 URL 编码 | `pytest test_asip_clash.py` | 含 `/` 或空格的节点名在 `select/delay/selector_state` 中 `quote(name, safe="")`；`delays` 对重复节点只请求一次且保序归因 |
| T-237 | P0 | 日志不得被消息内换行伪造 | `pytest test_asip_integration.py -k log_message` | `\n`/`\r\n` 压成字面 `\n`；超长消息截断并带 `…(truncated)` |
| T-238 | P0 | worker 默认不接管，灰度期旧路径继续干活 | `pytest test_asip_integration.py -k "line_worker or schedule_idle"` | 默认 `enabled=False`；`enabled+not dry_run` 时 `_prewarm_standby_warp` 不调、`_schedule_idle_refresh` 不登记、脏线守护不起；`dry_run=True` 时 worker 只观测，三条旧路径仍运行 |
| T-239 | P0 | dry_run 不得产生副作用 | `pytest test_warp_line_worker.py`；`python3 warp_line_worker.py` | dry-run 下不调 `_warp_model_usable`（会真实扣配额并写 `.probe_sessions.json`）、`_mark_warp_ip_used`/`_set_warp_line_ip`/`_restart_service`/`_reconnect_warp_now`，只记 `[dry-run] 应换线/应模型验收`；自检 `selftest OK (9 cases)` |
| T-240 | P0 | 大订阅不得被 HTTP 层截断 | `pytest test_asip_http.py test_subscription_parse.py -k "text_limit or truncated"` | `request_json(text_limit=None)` 返回全文；120 节点（>2000 字符 base64）订阅解码后节点数不丢 |
| T-241 | P0 | 决策后线被选为当前出口必须放弃重启/换线 | `pytest test_warp_line_worker.py -k stale` | 观测后 `active_warp_tag` 变为本线时，不调 `_restart_service`/`_set_warp_line_ip`；变为其他线时不调 `_reconnect_warp_now`，日志含「取消 restart」/「取消换线」 |
| T-242 | P1 | 日志检查不得把 grep 失败当「无匹配」 | `pytest test_self_evolve_checkpoint.py` | grep rc=1 视为正常无匹配；超时/rc>1/启动失败返回 `unavailable`（`ok=False, metric=None`） |
| T-243 | P1 | Clash API 非 2xx 不返回错误页 | `pytest test_asip_integration.py -k "error_body or pass_through"` | 200 返回解析体；401/500 返回 `(status, None)`；传输失败 `(0, None)` |
| T-244 | P1 | 子进程超时必须回收整个进程组 | `pytest test_asip_proc.py -k grandchild` | 子进程再 spawn 孙进程时，`timeout` 到期后两者都被杀；孙进程 2s 内消失 |
| T-245 | P2 | Clash 连接表读取异常不得冒泡 | `pytest test_asip_integration.py -k connections_json` | 客户端抛任意异常时 `_connections_json()` 返回 `None` 并缓存，调用方不崩 |
| T-246 | P2 | 日志注入覆盖 Unicode 行分隔符 | `pytest test_asip_integration.py -k log_message` | `\n`/`\r\n`/`\u2028`/`\u2029`/`\x85`/`\v`/`\f` 都压成字面 `\n` |
| T-247 | P1 | 同步刷新与后台补刷/按线 worker 共用按线锁 | `pytest test_asip_integration.py -k refresh_line_serialized` | 锁被占时返回 `(None, False)` 且不调 `_refresh_idle_warp`；`_refresh_idle_warp` 抛异常时仍释放锁 |
| T-248 | P2 | 任意 2xx 视为成功（与 raise_for_status 一致） | `pytest test_asip_http.py -k 2xx` | 200/206/299 `ok=True`；300/`None` `ok=False` |
| T-249 | P2 | 非法轮询间隔启动即拒绝 | `pytest test_warp_line_worker.py -k interval` | `interval_sec` 为 0 或负数时 `start` 抛 `ValueError` |
| T-250 | P2 | 出口探测统一走 `asip.proc` | `pytest test_asip_egress.py`；`grep -n "subprocess" asip/egress.py` | 无直接 `subprocess.run`；超时/启动失败/非零退出按 `ProcResult` 分类 |
| T-251 | P2 | 验收脚本不得把「读不到日志」当「无异常」 | `pytest test_verify_rotation.py` | journalctl 超时/rc≠0 时 `JournalLines.error` 非空，调用方报失败而非通过 |
| T-252 | P2 | 子进程输出解码失败不得外抛 | `pytest test_asip_proc.py -k invalid_utf8` | 非 UTF-8 字节以替换符解码，`run` 仍返回 `ProcResult` |
| T-253 | P2 | text_limit 负数必须拒绝 | `pytest test_asip_http.py -k negative_text_limit` | `text_limit=-1` 抛 `ValueError`，不静默截错 |
| T-254 | P1 | 选中备线到 PUT 之间不得被 worker 重启 | `grep -n "_acquire_refresh_slot(m\[\"tag\"\])" daemon.py`；`pytest test_asip_integration.py` | `_switch_warp` 前占按线锁、finally 释放；拿不到锁放弃本次换线 |
| T-255 | P1 | daemon 与 cron 共用 `.bad_nodes.json` 锁 | `pytest test_asip_integration.py -k dead_nodes_export` | 写路径用 `update_json(lock_path=".bad_nodes.json.lock")`；外来条目保留 |
| T-256 | P2 | 日志剥离 ANSI/C0 控制符 | `pytest test_asip_integration.py -k log_message` | `ESC[31m` 被移除，`\x00` 变空格 |
| T-257 | P2 | killpg 后等待有上界 | `pytest test_asip_proc.py -k pipe_holding_descendant` | 孙进程逃逸时最多再等 5s，返回 `timed_out` 而非阻塞 |
| T-258 | P2 | 恢复原选择失败必须可见 | 审查 `verify_rotation.py` | `switch(original)` 返回 False 时打印 `⚠ 恢复原选择失败` |
| T-259 | P2 | 锁文件权限与账本一致 | `pytest test_asip_atomic.py -k lock_file_is_private` | `chmod 0600`；`append_line` 恒 `fsync` |
| T-260 | P2 | Clash 返回值逐字段校验 | `pytest test_asip_clash.py` | `all`/`now` 独立降级；`delay` 拒 bool/NaN/inf 并归一为 int |
| T-261 | P2 | 超时结果带可读 error | `pytest test_asip_proc.py` | `ProcResult.error == "timeout after Xs"`；映射型 env 可传入 |
| T-262 | P2 | 错误换线候选分批实测 | `pytest test_daemon_unit.py -k slice_probe` | `probe_candidates_per_switch=2` 时单轮只测 2 条，游标轮转覆盖全池 |
| T-263 | P1 | 未实测候选不得放行 | `pytest test_daemon_unit.py -k admit_tested` | 只放行本轮 `tested` 为真的候选；`{}`/False 均得空集（变异验证已杀） |
| T-264 | P1 | 新鲜 OK 必须同 IP 且 ≤120s | `pytest test_warp_prober.py -k ok_fresh` | 同 IP 90s OK 为真；同 IP 130s、异 IP 90s 均为假 |
| T-265 | P1 | 无新鲜 OK 只等不切 | `pytest test_warp_prober.py -k waits_and_does_not_switch` | `wait_switch` 返回 None；`_switch_warp` 零调用；不落 proxy |
| T-266 | P1 | IP 未知不重启 | `pytest test_warp_prober.py -k unknown_ip_does_not_restart` | 记 `next_probe_ts=now+5`；`_warp_line_restart` 零调用 |
| T-267 | P1 | quota 只重启非 active 线 | `pytest test_warp_prober.py -k quota_restarts_non_active_line` | active 线不重启；非 active 线重启一次且原因带 quota |

**实战验证（改动合入前人工执行一次）**：

1. `python3 verify_rotation.py --quick` —— 端口/IP 切换正常，结束时恢复原选择。
2. `python3 switch-proxy.py status` / `list` —— 节点列表与当前选择可读，无 401。
3. `python3 probe_warp.py`（只读探测）—— 返回各线出口地址，不重启隧道。
4. `warp_line_worker` 灰度：把 `warp.line_worker` 设为 `{"enabled": true, "dry_run": true}` 并重启
   daemon，观察 `daemon.log` 只有 `[dry-run] 应…`，20 条线的 `systemctl` 重启次数不增；
   确认无异常后再关 `dry_run`。

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
| v1.18 | 2026-10-20 | 修 WARP 兜底死循环：备线出口被标记时不再跳过，改为刷新换会话 IP (T-175)；任一隧道疑似脏 IP 就重连到换出优质 IP（预热补 `blocked` 判据 + 换线后立即排补刷 + 落池期间继续巡检，T-176）；补 `_ACTIVE_WARP_IP` 启动补探与当前出口成功流量打点 (T-177)；脏线巡检由周期任务改为常驻守护线程（`_WARP_PROBING`/`_WARP_REFRESHING` 分离，T-178） | - |
| v1.19 | 2026-10-21 | 出口模式可观测：健康行加 `WARP:`/`PROXY:` 前缀 (T-181)，warp↔proxy 切换由 `_switch_warp` 统一打 `⇄ 出口模式切换` (T-182)，同模式内换线不刷屏；`asip` 隧道清单改从 `config.py` 解析（原硬编码 5 条，实际 10 条，T-183）、跟随日志改 `tail -F` 以跨轮转 (T-184)、`jlog` 修 `journalctl` 单元参数与用户单元噪声、`s`/`e` 过滤器重定 | - |
| v1.20 | 2026-10-21 | 错误归属改按请求开始时刻 (T-185)：`count_warp_blocked_since()` 用 `timestamp − latency.total` 与窗口起点比较 —— 9router 在请求结束时才落库、换线不掐断在途请求，旧出口的 429 会带着新时间戳落进新窗口。重放 34 次真实触发，旧逻辑 33 次命中 / 新逻辑 20 次，14 次纯误触发 | - |
| v1.21 | 2026-10-21 | 换线判据加成功门控与满窗例外 (T-186~T-188)：最近一条成功即不换线（拦下 36/65 次触发），满窗 10 条全错绕过冷却，`cooldown_ok_sec` 1s→5s（新出口首错 p50=1.6s，1s 冷却反而制造锯齿） | - |
| v1.22 | 2026-10-21 | 换线门控由「最近一条是否成功」改为「连错 ≥3 条」：429 风暴中成功与错误交替，`last_ok` 每行翻转，90 次触发只抑制 1 次（1%）；连错判据抑制 11 次（12%），其中 3 次窗口内成功 ≥5（出口健康）。门控移到采样/标脏之后，保脏 IP 仍被标记。新增 T-189 记录 `upstream connection lost` 属 SSE 末帧注入、`status=success`，不参与任何换线判据 | - |
| v1.23 | 2026-10-21 | 新增模型层验收模式（`warp.refresh_model_check`，默认开；`False` 回原模式）：刷新与选线都以 `probe_warp.probe()` 实测 `OK` 为唯一判据，IP 历史（403 6h / 429 24h / 24h 复用窗口）在模型模式下不作否决项 (T-190~T-192)。修复卡死链：测不过的备线并入 `blocked_lines` 走同一条重连路径（实测 → 同步重连一条 → 其余后台并发补刷 → 仍无可用才落池），旧顺序会在全测失败时跳过重连直接落 83 节点池 (T-194)。新增 `min_switch_gap_sec`（生效间隔硬闸）并修正采样顺序：生效窗口内且生效后无错误时不再把新出口标脏/记段 (T-195)。指纹校验改比 description+parameters 并对 schema 变更敏感 (T-193) | - |
| v1.24 | 2026-10-22 | 模型层探测台账改为按 /128 键控，TTL 按结论持久性分档（`quota`/`flagged` 6h、其余 2min），废除会话代次：同一地址的重复探测省 67.1%（30h 回放），脏地址结论跨重连存活 (T-196) | codex review |
| v1.25 | 2026-10-22 | 主动轮换收敛为两种判据并**删除 `lifetime`** (T-179)：连同 `_WARP_MIN_CYCLE_SEC` / `_recompute_min_warp_cycle` / 60s 地板 / `min_cycle_recalc_sec` 一并移除 —— 段跨度跟的是请求间隔（中位 3.8s vs 请求 p50 6.5s），生效值恒被地板顶住。`requests` 判据从「服务满 N 条请求」改为「成功用掉 N 格 dsv4 配额」并新增 `count_dsv4_ok_since`（走 `idx_uh_ts`，`usageHistory` 而非 2000 行滚动的 `requestDetails`）(T-160/T-168)；阈值 60→**100**，依据 204 个主动段 dsv4 中位 113.5（右删失下界）(T-168)。`fixed` 保持 60s 并设为默认。测试删 8 个 lifetime/重算用例，新增 3 个 `count_dsv4_ok_since` 用例 |
| v1.26 | 2026-10-22 | codex review P1 修复：`warp_line_busy` 默认窗 12s 会落到错误换线路径（逐候选 12s，最坏 ~120s），新增 `_ERROR_PATH_BUSY_WINDOW_SEC`=2s 并在 `reconnect_warp` 兜底分支与 `_wait_warp_idle` 显式传入 (T-141b)；12s 只留给后台补刷/脏线守护 | codex review |
| v1.27 | 2026-10-22 | **修生产宕机**：429 文案换版致分类器全漏 (T-197) —— 免费层现版文案 `Free usage limit reached`，旧代码只认 `Rate limit exceeded`，实测最近 2000 条错误里 1751 条 429 全被判成非出口级错误 → `consec_err` 恒 0 → 87% 错误率下永不换线。加 IP 去重 (T-198) 与启动时低质出口校验 (T-199)。二轮 codex：修 dedup 用例未走 monkeypatch 污染 `test_warp_refresh` 4 例 (P1)、兜底选线冷却判据写反致优先挑冷却线 (P2)、启动用例复刻分支体不覆盖 main (P3) | codex review |
| v1.28 | 2026-10-22 | 修刷新风暴 (T-200)：刷新路径探测超时后不再直接判失败 —— 实测 20 条隧道用 25s 预算全部可通，而 7s 预算下 6/20 超时（p50 3.0s / p90 8.9s / max 13.3s）；重启后首探冷实测 7.01s rc=28、紧接着 1.68s 返回。旧行为：单次超时→重启同线→再超时，14:58 单分钟 60 次 systemd Started、全库 748 条探测失败、「刷新 #3 失败→同线重启」135 次中 38 次间隔 65s（最多）。新增 `_probe_refresh_egress()`（短探 + `ready_timeout_sec` 复探一次），刷新与原地重连两路接入；首探成功不重探 | - |
| v1.29 | 2026-10-22 | 换线候选排除当前出口与重复 IP (T-208)：实测 817 次换线中 111 次（13.7%）新出口 == 旧出口，全部 rate429 触发，每笔都白重启一条隧道。`_exclude_active_ip()` 在三条候选来源汇合后统一去重；全滤空退回原列表避免落池。同轮口径勘误：排除 ≥5 错误触发/分钟的风暴分钟后，fixed(60s, 09:33~12:21) 主动 122 : 错误 40 = 3.05，requests(150格, 12:23~17:59) 主动 49 : 错误 59 = 0.83 —— 先前「10.07 vs 9.75」不成立（fixed 全天仅 124 次主动切换，分母不足以支撑该数字） | - |
| v1.30 | 2026-10-22 | 补齐分级选线文档与实测依据 (T-209)：三档语义 + `/64` 静置 + 冷却线垫底写进 CLAUDE.md（此前只在代码注释里）。`blocked_ttl_sec`=86400 的合理性用数据回答：被标记过的 IP 再次出现 6421 次，96.5% 本次未标记 —— 24h 不是永久拉黑，惩罚期满后大概率恢复；`last_ok_ts` 运行时确在写（1700/1858 条有该字段，近 30min 更新 24 条） | - |
| v1.38 | 2026-10-22 | 脏 IP 排除时机修正 (T-222)：T-211 把惩罚期做成两模式硬否决，但选线末尾又复查了一次 —— `reconnect_warp` 的 `usable` 用 `_ip_disqualified` 过滤刚实测 OK 的候选，于是「实测通过」被「上一段的标记」推翻，probes 清空后走兜底分支落轮换池。回归用例 `test_model_mode_selection_trusts_probe_not_history` 由绿转红、耗时 0.06s→17.02s（17s 全花在真实 `_fallback_to_proxy` 的 systemctl 尝试上）。修法按用户原话「脏 IP 要在探测之前就排除」：惩罚期判据移到探测循环（去掉 `_ip_history_is_truth()` 前置，两模式都生效），探测之后不再用旧台账复查。新增 `test_model_mode_excludes_blocked_ip_before_probing` 盯「标记过的地址不得被探测」。同轮修测试隔离：`test_warp_refresh.py` 的 autouse fixture 未隔离 `WARP_PROXY_FALLBACK_FILE_PATH`，落池时刻被写进生产仓库（`.warp_proxy_fallback.json` mtime 由测试推进），同一目录第二次跑结论由残留决定；加隔离 + 重置 `_last_proxy_fallback_ts`/`_proxy_fallback_ts_loaded` + 自检用例 `test_proxy_fallback_state_is_isolated_from_production`。变异验证三处：恢复 `_ip_history_is_truth()` 前置 → 探了 burned-ip 转红；恢复末尾复查 → 落池转红；撤隔离 → 自检转红。全套 `test_warp_refresh.py test_daemon_unit.py` **240 passed in 0.83s**，连跑两次同结果 | T-222 |
| v1.32 | 2026-10-22 | 脏 IP 设计在模型模式下失效 (T-211)：`_ip_disqualified` 在 `refresh_model_check=True` 时恒返 `False`，连 403/429 惩罚期也旁路 —— 而惩罚期是「地址确实被拦过」的确定事实，不是启发式；旁路它等于让整套脏 IP 机制对模型模式不生效。同轮补上第二个缺口：`_warp_model_usable` 判 `quota`/`flagged` 后只写探测缓存、不写 `_WARP_IP_USED`，那格配额烧完什么都不剩。实测 7 个烧毁段里 648 个不同 /128 被逐个探测、608 个（94%）判 quota，占全天 3660 次探测的 17.7%。修法：惩罚期判据提到 `_ip_history_is_truth` 之前；`_MODEL_VERDICT_BLOCK_KIND` 把 quota→rate429、flagged→abuse403（同一个 FreeUsageLimitError / abusive traffic 文案）；记账传 `forget_probe=False` 避免自己清掉刚写的结论。变异验证两处：去掉 blocked 写入 → 2 个用例红；惩罚期恢复旁路 → 1 个用例红 | T-211 |
| v1.39 | 2026-10-23 | OK 探测结论单独一档 TTL (T-225)：`probe_ok_ttl_sec`=600，从 `probe_transient_ttl_sec`(120) 里拆出来。判据是同一 `(tag, /128)` 的重探间隔中位 **212s** —— 2min 档位命中不到自己，同一地址每轮重探一遍。实测 1980 次「前一次结论 OK」里 1831 次（92.5%）结论不变；按 600s 可省 1748/5166=**33.8%** 探测量，判错 8.0%，1200s 只再省 0.1 个百分点。不套 6h 的理由：47 例 OK→quota 间隔中位 1267s，长 TTL 只是拉宽「凭旧 OK 选线」的窗口。安全网是标脏即作废：47 例里 45 例期间有生产侧 429/403 换线走 `_probe_cache_forget`。变异验证两处：删 `_WARP_PROBE_OK` 分支 → `ttl_by_persistence` 转红（1600s 处由 `OK` 变 `None`）；把 `_probe_cache_forget` 换成 `pass` → `test_marking_ip_blocked_invalidates_its_probe_verdicts` 转红。同轮记录一个**被证伪**的假设（T-226）：曾据「24 个 quota 地址全部落在已烧 /64 内」推断段级过滤可省 1762 格配额，换成台账 `blocked_ts` 时间序回放后结论消失 —— T0 已烧段 OK 5.47% 反而高于干净段 0.93%，那 1762 是**用被判死的探测本身反推段状态**的自证偏差。另：跳过 T2 候选只省 7.2%（290/4010）且 773 批里 0 批因此失去唯一 OK，收益不足未改。全套 `test_warp_refresh.py test_daemon_unit.py` **240 passed in 0.76s** | T-225 / T-226 |
| v1.31 | 2026-10-22 | 模型探测不可用时的降级路径 (T-210)：整批探测**全部** `unreachable` 即判定探测通道失明 120s，期间 `_ip_history_is_truth()` 返回真、退回三档 + 复用窗口历史判据，并停止发探测（省配额）。实测依据：`daemon.log` 今日 2345 批探测中 13 批为整批 ≥3 条全 `unreachable`（最高 17/17），其中 16:50:18 那批 17 条同一秒全灭、重启换 IP 后 16:50:50~52 全部 OK —— 17 次隧道重启与配额全是白烧。判据必须是「整批」：单条 `unreachable` 是该出口真实瞬态，TTL 仍 2min。变异验证：删掉 `_ip_history_is_truth` 里的 `or _warp_probe_blind()` 后 `test_blind_channel_falls_back_to_ip_history` 必红 | T-210 |
| v1.36 | 2026-10-22 | 地址被标脏即作废其全部探测结论 (T-221)：`_mark_warp_ip_used(blocked=True)` 后调 `_probe_cache_forget(ip)`，按 IP 跨线全清。实测依据：829 次换线里 170 次（20.5%）同一 IP 在 ≤120s 内被重新选中且 170/170 回到同一条线，其中 158 次（92.9%）所依据的 OK 早于标脏时刻 —— 旧证据把已烧地址重新认证成可用。控制变量排除混杂：缓存 OK 与实测 OK 各自组内，首用/复用的段寿命差 ≤10pp（缓存 7s vs 6s、实测 95s vs 60s），差异主要在**证据来源**（缓存 61~71% 的段 ≤10s 就死，实测只有 25%），不是「新地址 vs 旧地址」。变异验证：`_probe_cache_forget` 打成空操作后 verdict 回 `OK`，用例转红 | - |
| v1.37 | 2026-10-22 | codex P2：在途探测晚回来会重新写回已作废的结论。`_probe_cache_forget` 只删已有条目，而 `_warp_model_usable` 的探测要 1.4~8.7s，期间地址被烧掉后那次 `_probe_cache_put` 照样把标脏前起手的 OK 补进台账、还更新 `last_ok_ts` 把已烧地址认证成「刚成功过」。加墓碑 `_PROBE_BLOCKED_AT[ip]` + `_probe_cache_put(started_at=)` 拒收；`_warp_model_usable` 收拒收信号后不记 `last_ok_ts` 并判该线不可用。变异验证：忽略 `started_at` 后 put 回 True、verdict 回 `OK`，用例转红 | codex review |
| v1.33 | 2026-10-22 | 可移植性与部署 (T-212~T-215)：路径改为项目内相对 `config.py`、项目外以 `pwd` 家目录为基准 + 环境变量覆盖（订阅地址与 7 个路径键），删掉全部 `/home/administrator` 硬编码；新增 `deploy/install.sh` + `deploy/members.py`，由 `CFG["warp"]["members"]` 推导 23 个 systemd 单元（含 20 条隧道与地址族开关 `ipv6`）；`.warp/warp-outbound.json` 缺失时自动重建 —— 它被 gitignore 却是 WARP 出口的唯一依据，缺了全部换线打空；`parse_subscription.py` 无参数时回退到 CFG 全部订阅源（原来只认 `OKZ_SUB_URL`）；新增 `requirements.txt` / `.env.example` / `test_portability.py`。验证：`test_daemon_unit` + `test_subscription_parse` + `test_warp_refresh` + `test_portability` 共 **277 passed**（31.06s）；副本目录 + 假 HOME 下路径仍指向副本 | - |
| v1.34 | 2026-10-22 | 自对抗审查补 5 项 (T-216~T-219)：**3 项是上一轮我自己引入的 P0** —— ①`_env_path` 不展开，而 systemd `EnvironmentFile` 实测不展开 `$HOME`（`journalctl` 里原样是 `$HOME/.some/path`），start 脚本 source 却展开，同一份 `.env` 两条路径两种结果 → 展开移进 `_env_path`；②README 的 `for d in .warp/usque*/` 匹配到主目录自身，沙箱实测把唯一二进制换成自指链接（`Too many levels of symbolic links`），20 条隧道全灭 → `install.sh` 建链接时排除 `usque/` 并加「主二进制不得是链接」前置检查；③`PYBIN` 用 `command -v python3`，conda shell 下生成 `miniforge3/bin/python3`，与现网 `/usr/bin/env python3` 不符且换 shell 就变 → 钉死 `/usr/bin/python3`（`PYTHON=` 可覆盖）。另两项：README 自述「仓库不含二进制」但 `sing-box` 实际被 git 跟踪（57MB）→ 更正为「usque 需自备、sing-box 随仓库」；`parse_subscription.py` 无参回退此前只认 `OKZ_SUB_URL`，与 Q1 记的「四条订阅源」不符 → 复核为已覆盖并写进用例。变异验证：撤掉展开 → T-216 红；去掉 `[ "$dir" = "usque" ]` 排除 → T-217 红；恢复后 `test_portability.py` 7 passed，四文件混跑 **284 passed** | 自对抗 |
| v1.35 | 2026-10-22 | 补 T-219 落地：首版隧道注册失败只打 `⚠` 仍继续 `enable`，注册失败的单元启动即退出并无脑重启，而部署退出码为 0（假成功）。改为计数并在末尾非零退出；同时在 `--units-only` 的检查里加「主二进制存在且不是链接」 | 自对抗 |
| v1.36 | 2026-10-26 | ASIP 分层重构（R165）§13 与审查回归：T-240 订阅全文（修 HTTP 层 2000 字符截断导致节点丢失）、T-241 陈旧观测竞态、T-242 grep 失败语义、T-243 Clash 非 2xx 不返回错误页；T-239 补齐「dry-run 跳过模型验收」 | codex 20 维 review |
| v1.38 | 2026-10-26 | 第二轮审查修复：T-244 `asip.proc` 超时改杀进程组（孙进程不再泄漏）、T-245 Clash 连接表异常不外抛、T-246 日志注入覆盖 Unicode 行分隔符、T-241 扩展到「决策后已非当前出口则取消换线」、T-247 同步刷新改走按线锁、T-248~T-250 补齐 2xx/间隔/进程组 | codex 20 维 review |
| v1.40 | 2026-10-26 | 第三轮整改：T-238 灰度期旧守护继续运行（dry_run 只观测不接管）、T-251 验收脚本 journal 失败语义、T-252 非 UTF-8 输出解码兜底、T-253 负数 text_limit 拒绝；`singbox_api` 异常只记一条日志 | 三方五维审查 |
| v1.41 | 2026-10-26 | 第四轮整改：T-254 选线到 PUT 占按线锁、T-255 bad_nodes 跨进程共用 flock、T-256 日志剥 ANSI/C0；附 T-257 有界回收、T-258 恢复失败可见、T-259 锁文件 0600、T-260 Clash 字段校验、T-261 超时 error 文案 | 三方五维审查 |
| v1.42 | 2026-10-26 | 限流省配额：T-262 错误换线单轮实测 N 条并轮转游标；T-263 抽出 `_admit_tested` 锁住「没测到 ≠ 可用」，补变异验证（放行未测→转红）。全量 532 passed | 实战配额风暴 + 自对抗 |
| v1.43 | 2026-10-26 | 自查补测试（变异验证驱动）：T-256 增 ANSI/C0 用例（删 `_LOG_ANSI` 即转红）、T-254 增「选中线刷新槽被占则放弃换线」用例（去守卫即转红）；另修 workspace 文档中 `HttpResult.ok`/`text_limit`/`ProcResult.error` 等过期契约。全量待跑 | 三方 10 维自查 |
| v1.44 | 2026-10-26 | Codex 第二轮 P1：`_admit_tested` 由「按出口 IP 放行」改为「按 tag 放行」——同址另一条线可能隧道已坏，仅 IP 相同不证明可用；用例补同 IP 双线，变异（改回 IP 过滤）转红。全量待跑 | Codex 第二轮 |
| v1.45 | 2026-10-26 | 预热同样分批：`prewarm_max_per_round`(4) + `_PREWARM_CURSOR` 轮转；用例断言三轮切片顺序，变异（关闭切片）转红。Codex 单提交审查：e31faca PASS、7322c26 PASS、6ea3cf2 FAIL(P1 按 IP 放行)→已修(6d1ce68) | Codex 第二轮 |
| v1.46 | 2026-10-26 | 收尾三个 P2：①line worker 成员从配置移除即退出、异常日志 60s 节流；②`proc.run` 非超时异常先杀组再抛（不留孤儿）；③日志剥离 ST 结尾的 OSC。4 个新用例，四处变异分别转红 | 自查 P2 收口 |
| v1.47 | 2026-10-26 | Codex 第二轮（探测器段）4 项：warming 前置检查异常泄漏→已修；陈旧 OK 被判假成功→已修；晚到 unknown 清掉新 OK→已修；invalidate 缩短退避→按设计保留（blocked 分支不打模型接口，重启受 backoff 约束），补特征化用例。3 处变异分别转红 | Codex 第二轮 |
| v1.48 | 2026-10-26 | conftest 把 `CFG["log_file"]` 也指到 tmp：用例不再往线上 daemon.log 写「W2 重启前置检查异常」这类假事件（验证：单跑 prober 用例，产物在 basetemp，生产日志零新增用例行） | 测试基建 |
| v1.49 | 2026-10-26 | 按主人规则「每个 IP 只探一次；重启拿回同一个 IP 基本等于该 IP 已封禁」：新增跨线复用 `_probe_cache_get_ip`（同一 /128 的持久结论不二次交学费）；同址重启命中惩罚期时按 `_line_block_remaining` 停探到确切解禁（原先 transient 结论会 5s 后再探）。2 处变异各自转红 | 主人决策 |
| v1.50 | 2026-10-26 | 按主人澄清改正判据：脏不脏以**台账/探测结论**为准，不由「重启后还是同一个 IP」推断。`_warp_model_usable` 入口对惩罚期内地址直接跳过实测（跨线同样适用，`_PROBE_LAST_VERDICT=blocked`）；重启后地址若在惩罚期则停探到解禁（不再要求 same_ip）。删掉冗余的 `_probe_cache_get_ip`（被入口判脏覆盖）。2 处变异各自转红 | 主人澄清 |
| v1.51 | 2026-10-26 | 纳入并行 session WIP：20 条隧道 `ipv6: True`（F/G/J/11-20，实测单元已 `-6` 且出口为 v6）；旧连接豁免窗口 `switch_stale_grace_sec=120`。修正其实现：豁免仅在「换线后新出口未报过错」时成立（保留 `fresh` 判据），否则坏 IP 会被放行 2 分钟——原 WIP 版本令 `marks_ip_blocked_inside_cooldown`、`real_error_after_new_exit_is_still_marked` 两用例失败，修正后 561 passed | 并行 WIP + 修正 |
| v1.52 | 2026-10-26 | 纳入并行 WIP 并修正：传输层 503（`UpstreamTransportError`）单独计数，不进 `errors`/`consec_err`/`full_err`、不记脏台账；`count_warp_blocked_since` 增 `transport` 键；达阈值先经当前隧道探出口（`warp_egress_ip`，15s 节流），探通判 9router 侧故障不换线、探不通才换线；健康摘要增 `503=N`；`ErrorWindow` 尾字段带默认值，main 循环 6 元解包；修 WIP 遗留的 5 元解包（`maybe_reconnect_warp` 会 ValueError）。564 passed | 并行 WIP + 修正 |
| v1.53 | 2026-10-26 | 传输层探针去重：签名 `(换线窗口起点, transport 计数)`，同窗口同计数不再重复探出口（实测每 10~20s 刷一条「出口探测正常」且每次查 CF trace）；换线后窗口起点变化自动重新武装。补 2 例（同证据不重探 / 换线后重探）。566 passed | 实测刷屏修复 |
| v1.54 | 2026-10-26 | 审查修复：① 探测起手后该线已重启换址 → 旧地址结论丢弃（否则 `st["ip"]` 退回旧值并按旧地址 quota 惩罚期停探 16h）；② 传输层换线被冷却/闸门拒绝时撤掉探针签名并如实记日志（原日志恒称「→ 换线」，且同证据永久锁死不再重试）；③ 回切分支兜底 dict 补 `transport` 键。补 2 例。568 passed | 第三方/自查 |
