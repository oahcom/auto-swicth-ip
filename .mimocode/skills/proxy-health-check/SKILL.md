---
name: proxy-health-check
description: |
  auto-switch-ip 代理链路健康检查 — 一键诊断 sing-box + daemon + proxy_429 + Clash API + 9router 全链路。
  当用户说"检查代理"、"proxy health"、"代理链路"、"sing-box 状态"、"节点状态"时加载。
triggers:
  - 代理
  - sing-box
  - proxy
  - health check
  - 健康检查
  - 节点
tags: [运维, 代理]
---

# Proxy Chain Health Check

## 概述

auto-switch-ip 项目的代理链路包含 5 个环节，任一环节故障都会导致请求失败。本 skill 定义标准检查流程。

## 检查流程（按顺序执行）

### Step 1: 进程存活检查
```bash
pgrep -af "sing-box|daemon.py" | grep -v grep | grep -v snapshot
```
- sing-box: 代理核心进程
- daemon.py: 节点轮换守护进程

### Step 2: 端口监听检查
```bash
ss -tlnp 2>/dev/null | grep -E '9090|7890|7891' || netstat -tlnp 2>/dev/null | grep -E '9090|7890|7891'
```
- 9090: sing-box Clash API
- 7890: sing-box mixed port (HTTP/SOCKS5)
- 7891: proxy_429 代理端口

### Step 3: Clash API 连通性
```bash
curl -m 5 -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $(cat ~/.singbox_secret)" http://127.0.0.1:9090/proxies/proxy
```
- 200 = 正常
- 非 200 = sing-box 异常或 secret 错误

### Step 4: 节点信息查询
```bash
curl -m 5 -s "http://127.0.0.1:9090/proxies/proxy" -H "Authorization: Bearer $(cat ~/.singbox_secret)" | python3 -c "
import sys,json
d=json.load(sys.stdin)
now = d.get('now','?')
all_nodes = d.get('all',[])
print(f'当前节点: {now}')
print(f'总节点数: {len(all_nodes)}')
"
```

### Step 5: 代理链路连通性测试
```bash
# 测试 7890 (sing-box)
curl -m 15 -s -x http://127.0.0.1:7890 https://api.ipify.org && echo " (7890 OK)"

# 测试 7891 (proxy_429)
curl -m 15 -s -x http://127.0.0.1:7891 https://api.ipify.org && echo " (7891 OK)"
```

### Step 6: 9router 配置检查
```bash
python3 -c "
import sqlite3, json
c = sqlite3.connect('/home/administrator/.9router/db/data.sqlite')
s = json.loads(c.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
print(f'outboundProxyUrl: {s.get(\"outboundProxyUrl\",\"未设置\")}')
print(f'outboundProxyEnabled: {s.get(\"outboundProxyEnabled\",\"未设置\")}')
c.close()
"
```

### Step 7: 日志最近错误
```bash
grep -E "ERROR|error|429|Bad node" /home/administrator/dkk-projects/auto-switch-ip/daemon.log 2>/dev/null | tail -10
```

## 快速诊断脚本

一键运行全部检查：
```bash
echo "=== Process ===" && pgrep -af "sing-box|daemon.py" | grep -v grep | grep -v snapshot && echo "" && echo "=== Ports ===" && ss -tlnp 2>/dev/null | grep -E '9090|7890|7891' && echo "" && echo "=== Clash API ===" && curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $(cat ~/.singbox_secret)" http://127.0.0.1:9090/proxies/proxy 2>/dev/null && echo "" && echo "=== 7890 ===" && curl -m 15 -s -x http://127.0.0.1:7890 https://api.ipify.org 2>/dev/null && echo "" && echo "=== 7891 ===" && curl -m 15 -s -x http://127.0.0.1:7891 https://api.ipify.org 2>/dev/null
```

## 常见故障诊断

| 症状 | 可能原因 | 修复 |
|------|---------|------|
| sing-box 未运行 | WSL2 重启 | `bash start-auto-switch-ip.sh` |
| 端口 9090 未监听 | sing-box 启动失败 | 查 singbox.log |
| Clash API 返回非 200 | secret 过期 | 检查 ~/.singbox_secret |
| 7890 连接失败 | 全部节点死亡 | 检查 bad_nodes.json |
| 7891 连接失败 | proxy_429 进程挂 | 重启 proxy_429 |
| 9router outboundProxy 未设置 | 配置丢失 | 重新设置 SQLite |

## 关键文件

| 文件 | 用途 |
|------|------|
| `~/.9router/db/data.sqlite` settings | 9router 出站代理指向（outboundProxyUrl） |
| `daemon.py` | 节点轮换守护进程 |
| `singbox-config.json` | sing-box 配置 |
| `.bad_nodes.json` | 坏节点共享文件 |
| `daemon.log` | daemon 运行日志 |
| `singbox.log` | sing-box 运行日志 |
