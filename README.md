# auto-switch-ip

让 9router 永远不缺 token：自动拉 okz 订阅 → 用 sing-box 当出口代理 → 主动轮换 IP → 绕开 opencode 等 free provider 的 IP 限流。

## 背景

9router 通过 `outboundProxyUrl` 配置上游代理（默认指向 okz:6696）。okz 提供多个 hysteria2/anytls/trojan 节点，每次手动切代理可换出口 IP，但 opencode free 等 provider 看到 IP 不变就会限流（402 MONTHLY_REQUEST_COUNT）。

本项目**完全接管 9router 的 outbound 代理**：拉 okz 订阅、用 sing-box 当代理池、daemon 主动轮换节点，每 5 分钟切一次 IP，opencode 永远看到不同 IP → 永远不限流。

## 架构

```
[openclaw/opencode/etc]  →  9router:20128  →  sing-box:7890  →  60× anytls nodes  →  Internet
                                       ↑                                ↑
                              outboundProxyUrl = 7890              订阅拉取
                                       ↑
                            daemon.py 自动切节点
```

## 文件

- `parse_subscription.py` — 拉 okz 订阅 + 解析 hysteria2/anytls/trojan + 生成 sing-box config
- `daemon.py` — 监控 9router + 主动轮换 sing-box 节点
- `singbox-config.json` — 生成的 sing-box 配置（56 anytls + 1 trojan + selector + clash API）
- `sing-box` — Linux x64 二进制（1.13.0）
- `daemon.log` — daemon 运行日志

## 安装

```bash
# 1. 装 sing-box（如果还没装）
./parse_subscription.py  # 拉订阅 + 生成 config

# 2. 启动 sing-box
./sing-box run -c singbox-config.json &

# 3. 改 9router 出口代理为 7890
python3 -c "
import sqlite3, json
con = sqlite3.connect('/home/administrator/.9router/db/data.sqlite')
cur = con.cursor()
cur.execute('SELECT data FROM settings WHERE id=1')
s = json.loads(cur.fetchone()[0])
s['outboundProxyUrl'] = 'http://127.0.0.1:7890'
cur.execute('UPDATE settings SET data = ? WHERE id=1', (json.dumps(s),))
con.commit()
con.close()
print('done')
"

# 4. 启动 daemon
python3 daemon.py &

# 5. 验证
curl -x http://127.0.0.1:7890 https://api.ipify.org  # 应返回非本地 IP
```

## 配置

`daemon.py` 顶部 CFG dict:
- `proactive_rotate_sec`: 主动轮换间隔（默认 300s = 5 分钟）
- `recent_window`: 错误检测看最近 N 条
- `opencode_min_success_rate`: 低于这个成功率触发切代理

## 工作原理

**主动轮换**（每 5 分钟）：
- daemon 调 sing-box clash API `PUT /proxies/proxy` 切换到下一个 anytls 节点
- 9router 下次出站请求自动走新节点（无需重启 9router）

**错误检测**（被动应急）：
- daemon 调 9router `/api/usage/request-details` 看最近 20 条
- opencode 失败率 > 50% → 立即切代理（不等 5 分钟）

**安全特性**：
- 跳过"最近用过的 10 个节点"避免连续切到慢的
- 失败节点加入 fail_set（不重试）
- 节点不够时退路：跳过 fail_set → 跳过 recent_used → 报错

## 网络要求

- WSL2 mirrored 模式（`/mnt/c/Users/Administrator/.wslconfig` 里 `networkingMode=mirrored`）— Windows 127.0.0.1 能直通 WSL2 内的服务
- hysteria2 在 WSL2 下有 utls 不兼容问题（sing-box 报错 "unsupported usage for uTLS"），本项目默认排除 h2 节点（用 `--include-h2` 开启）

## 恢复

如果出问题恢复原状：
```bash
# 9router 改回 6696 (okz)
python3 -c "
import sqlite3, json
con = sqlite3.connect('/home/administrator/.9router/db/data.sqlite')
cur = con.cursor()
cur.execute('SELECT data FROM settings WHERE id=1')
s = json.loads(cur.fetchone()[0])
s['outboundProxyUrl'] = 'http://127.0.0.1:6696'
cur.execute('UPDATE settings SET data = ? WHERE id=1', (json.dumps(s),))
con.commit()
con.close()
"
# 停 sing-box 和 daemon
pkill -f "sing-box run"
pkill -f "daemon.py"
```
