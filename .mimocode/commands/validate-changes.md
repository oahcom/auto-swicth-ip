---
name: validate-changes
description: |
  auto-switch-ip 代码变更验证流水线 — compile → test → codex review → TEST_PLAN 同步。
  修改代码后运行此命令，确保变更符合 CLAUDE.md 要求的验证流程。
agent: codex
---

# 代码变更验证流水线

## 用途

修改 auto-switch-ip 项目代码后，按 CLAUDE.md 要求的顺序执行验证：
1. 语法验证（py_compile）
2. 测试执行（pytest）
3. Codex CLI 审查
4. TEST_PLAN.md 同步检查

## 使用方式

```
/validate-changes
```

## 执行步骤

### Step 1: 语法验证
对所有 Python 文件执行编译检查：
```bash
python3 -m py_compile config.py && echo "✔ config.py" && \
python3 -m py_compile daemon.py && echo "✔ daemon.py" && \
python3 -m py_compile proxy_pool.py && echo "✔ proxy_pool.py" && \
python3 -m py_compile parse_subscription.py && echo "✔ parse_subscription.py" && \
python3 -m py_compile switch-proxy.py && echo "✔ switch-proxy.py" && \
python3 -m py_compile test_auto_switch.py && echo "✔ test_auto_switch.py" && \
python3 -m py_compile test_proxy_429.py && echo "✔ test_proxy_429.py" && \
python3 -m py_compile test_proxy_pool.py && echo "✔ test_proxy_pool.py"
```

如果任何文件编译失败，**立即停止**，修复语法错误后重新运行。

### Step 2: 测试执行
```bash
python3 -m pytest test_proxy_429.py test_proxy_pool.py -v 2>&1 | tail -20
```

如果测试失败，分析失败原因，修复后重新运行。

### Step 3: Codex CLI 审查
使用 Agent 工具对变更文件执行 6 维代码审查：
- D1 正确性：逻辑符合需求？边界覆盖？
- D2 安全性：输入验证？权限最小化？无硬编码密钥？
- D3 可维护性：命名揭示意图？函数长度合理？
- D4 性能：N+1？内存泄漏？阻塞调用？
- D5 一致性：遵循现有风格？
- D6 可测试性：关键路径有测试？

审查结论必须为 `PASS` 或 `PASS_WITH_NOTES` 才可继续。

### Step 4: TEST_PLAN.md 同步检查
确认以下内容：
1. 变更涉及的模块在 TEST_PLAN.md 中有对应测试点
2. 新增功能有对应测试点
3. 修改的测试点描述与代码行为一致

如果 TEST_PLAN.md 需要更新，先更新再提交。

## 禁止事项

- ❌ 直接修改代码跳过 Codex CLI 审查
- ❌ 直接修改 TEST_PLAN.md 而未同步代码变更
- ❌ 跳过语法验证直接提交
- ❌ 测试失败后不修复直接提交
