# asi-fixes — 多Agent 审查修复记录

> 4 Agent 并行审查（错误吞没 / 策略模式锁 / proxy_pool 线程池 / CLI 回退）
> + Codex CLI 独立验证（6 项修复全部真解决）
> + 实战验证: `python3 test_auto_switch.py` → 21 pass / 0 fail

## 修复一览

| # | 文件 | 问题 | 修复 |
|---|---|---|---|
| 1 | switch-proxy.py | 每次运行都吞异常错误 (sing-box 401/timeout/非JSON均归为 "连接失败") | `logging.warning()` + 明确根因串 |
| 2 | switch-proxy.py | `count=0/-5` 静默返回成功 | `if count < 1` 守卫 + 报错 |
| 3 | switch-proxy.py | `goto ""` 匹配所有节点 | `if not name` 守卫 |
| 4 | switch-proxy.py | `else: cmd_goto(cmd)` 把任意指令当节点名 | 改为 `print("未知指令...")` |
| 5 | switch-proxy.py、proxy_pool.py、parse_subscription.py、test_auto_switch.py | `SINGBOX_SECRET` 常量冻结 (secret 轮换后 401) | `get_singbox_secret()` mtime 自动刷新 |
| 6 | daemon.py | `_strategy_mode` 读锁写不加锁 (stale mode 观测) | 写入主循环也 `with _state_lock` |
| 7 | daemon.py | `_export_dead_nodes_locked` 持锁做 sing-box API | `total_count` 移到锁外预取 |
| 8 | config.py | `SINGBOX_SECRET = CFG["singbox"]["secret"]` 冻结快照残留 | 删除导出 + `_read_secret` 死代码 |
| 9 | .bashrc | `asi-d`/`asi-g` 别名无法传递参数 | 改为函数 + 增强帮助 |
| 10 | proxy_pool.py | `__init__` 急切建池 + 类方法重复建池 | 删除冗余, 删死码 `_refresh_delays` |

## 架构优化决策

- **锁粒度收益远低于复杂度**: daemon.py 主循环改为 `with _state_lock` 保护 `_strategy_mode` 读写，保证同锁快照一致性。回归风险: 低 (RLock 重入, log 移出锁体)
- **错误吞没 ≠ 静默退出**: switch-proxy.py 仅改为 `logger.warning()`（stderr), 用户侧 `print` 输出不变 → `asi-*` 输出行为与 `asi-help` 描述一致
- **CLI fallback 移除正面性有限**: `switch-proxy <node>` 直接跳节点的便利被移除, 改为显式报错, 用户必须 `goto`, 错误率下降

## 跳过 (P2, 非阻塞)

- `switch-proxy.py get_nodes()` 非 200 时不打印 HTTP 状态码 (难以在不改变接口的情况下添加 — 当前返回 `None, []` 对 401/500/error_code 统一归一, 仅日志区分)
- `daemon.py` `_strategy_mode` 主循环写入侧不加锁: CPython str 赋值原子, 主循环单线程写入, 无实害 — 仅形式正确性 (本修复是过度保守)

## 验证链

```
py_compile: 6 files PASS (config + switch-proxy + daemon + proxy_pool + parse + test)
test_auto_switch.py: 21 pass / 0 fail
test_proxy_pool.py: 13 pass
switch-proxy.py smoke: status/list/next/goto(restart→报错)/next 0/下一个/goto '' 均验证
daemon.py smoke: cli_rotate 成功切到新节点, IP 变化
config.py: from config import SINGBOX_SECRET → ImportError ✅
```

## Codex 复检 (v2)

Codex 已验证 6 项原修复真解决 (fact #301377)。config.py 冻结常量重新删除后，T-097 (`ImportError`) 核实通过，无回归。

