# 事故记录

## INC-001: 重启 9router 导致服务中断

**日期**: 2026-07-02  
**等级**: P2（部分用户受影响，持续 ~10 分钟）  
**原因**: 误杀进程 + 启动参数错误

### 时间线

| 时间 | 事件 |
|------|------|
| 12:33 | 修改 proxy-global.js SSE streaming 修复 |
| 12:33 | `kill` 掉旧的 next-server 进程，期望 cli.js 自动重启 |
| 12:33 | qrouter cli.js 也随 kill 流程被清理 |
| 12:38 | 手动重启 9router，但未清理旧端口占用 |
| 12:38 | 用 `--no-browser --skip-update` 启动失败（端口被旧进程占用）|
| 12:39 | 改用 `--tray` 模式启动成功 |
| 12:40 | 用户报告 7890 端口也炸了（实际没炸，是测试请求 timeout）|

### 根因

1. **kill 范围过大** — `kill $(pgrep -f "next-server")` 只杀 next-server，期望 cli.js spawner 自动重启。但 cli.js 也可能在 clean up 流程中被连带杀掉。
2. **端口冲突** — 旧进程被杀后端口未立即释放，新进程启动失败
3. **无健康检查** — 重启后未确认服务就绪就继续操作
4. **`--no-browser` vs `--tray`** — cli.js 启动模式区分，`--no-browser` 是 TUI 模式，需 TTY 才能保持；后台进程应用 `--tray` 模式

### 修复措施

1. 停止 9router 必须用：`pkill -f "cli.js"` 或 `pkill -f "next-server"` + 等 cli.js spawner 自动重启
2. 重启后验证：`pgrep -f "next-server"` + `curl ... /_mitm_health`
3. 后台启动必须用：`--tray --skip-update`

### 经验

- 不要直接 `kill` next-server，让 cli.js 管理进程生命周期
- `--tray` 模式适合无终端后台运行，`--no-browser` 需要 TTY
- 每次 deploy 后执行健康检查，确认服务就绪再通知用户
