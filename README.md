# auto-switch-ip

让 9router 永远不缺 token：自动拉 okz 订阅 → 用 sing-box 当出口代理 → 主动轮换 IP → 绕开 opencode 等 free provider 的 IP 限流。

## 背景

9router 通过 `outboundProxyUrl` 配置上游代理（默认指向 okz:6696）。okz 提供多个 hysteria2/anytls/trojan 节点，每次手动切代理可换出口 IP，但 opencode free 等 provider 看到 IP 不变就会限流（402 MONTHLY_REQUEST_COUNT）。

本项目**完全接管 9router 的 outbound 代理**：拉 okz 订阅、用 sing-box 当代理池、daemon 主动轮换节点（opencode 出口走 WARP 隧道，按实测 IP 寿命换线），opencode 尽量看到不同 IP → 尽量不限流。

## 架构

```
[openclaw/opencode/etc]  →  9router:20127  →  sing-box:7890  →  83 节点池 + 10 WARP  →  Internet
                                       ↑                                ↑
                              outboundProxyUrl = 7890              订阅拉取
                                       ↑
                            daemon.py 自动切节点
```

## 文件

- `parse_subscription.py` — 拉 okz 订阅 + 解析 vless/vmess/hysteria2/anytls/trojan/shadowsocks + 生成 sing-box config
- `daemon.py` — 监控 9router + 主动轮换 sing-box 节点
- `singbox-config.json` — 生成的 sing-box 配置（**不入库**，含 Clash API secret 与节点凭据；启动时按需生成）
- `sing-box` — Linux x64 二进制（1.13.0）
- `daemon.log` — daemon 运行日志

## 安装

```bash
# 1. 生成 config（订阅地址见 config.py CFG["okz"]["sub_url"]）
python3 parse_subscription.py                      # 用 OKZ_SUB_URL 环境变量
# 或显式指定：
python3 parse_subscription.py --urls "<订阅URL>" [更多源...]

# 2. 启动 sing-box（先确认 config 已生成）
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

`config.py` 的 CFG dict（与 daemon.py 共用），常用项：
- `thresholds.recent_window`: 错误判据窗口（默认 10 条）
- `thresholds.opencode_min_success_rate`: 错误率 > (1-该值) 触发轮换（默认 0.9 = 90%）
- `warp.block_threshold` / `warp.rate429_threshold`: 403/429 触发阈值（均 1）
- `warp.cooldown_ok_sec`: 换线成功后冷却（5s，满窗全错绕过）
- `warp.ip_reuse_guard_sec`: 出口 IP 复用窗口（86400 = 24h）

## 工作原理

**opencode 出口（WARP 主备换线）**：
- daemon 读 9router SQLite requestDetails（provider="opencode"）
- 403（abusive traffic）/429（Rate limit exceeded）各达阈值 1，且窗口内连错 ≥3 条
  → `PUT /proxies/opencode-out` 切到另一条 WARP 隧道（在途请求不断）
- 主动轮换按实测 IP 寿命（proactive_mode=lifetime），不等出错就换

**轮换池（非 opencode 流量）**：
- daemon 每 60s 调 Clash API `PUT /proxies/proxy` 切换池节点
- 9router 下次出站请求自动走新节点（无需重启 9router）

**安全特性**：
- 出口 IP 记录落盘 `.warp_ip_used.json`，24h 内用过的地址不复用
- 403/429 标脏的 IP 按 6h/24h 恢复期挡在候选池外
- 所有 WARP 不可用时兜底落 proxy 池（3h 冷却防来回弹）

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
