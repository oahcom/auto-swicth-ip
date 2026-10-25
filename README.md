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
- `asip/` — 无副作用的可复用工具（原子 JSON 写、密钥脱敏、Clash API 客户端、出口探测）；daemon 通过同名门面委托，便于单测与复用
- `.env` — 订阅 token 与路径覆盖（**不入库**，chmod 600）
- `singbox-config.json` — 生成的 sing-box 配置（**不入库**，含 Clash API secret 与节点凭据；启动时按需生成）
- `sing-box` — Linux x64 二进制（1.13.0）
- `daemon.log` — daemon 运行日志

## 安装

### 0. 前置条件

- Linux x86_64（WSL2 可用），systemd 用户实例（`systemctl --user`），Python ≥ 3.10
- 二进制由 `./deploy/fetch-binaries.sh` 自动获取并校验（详见 §0.1），无需手工下载
- Python 依赖：`python3 -m pip install -r requirements.txt`
- 9router 已在本机运行（`~/.9router/db/data.sqlite` 存在），或用 `NINE_ROUTER_DB_PATH` 指过去
- WSL2 需 mirrored 网络模式（Windows 侧 `.wslconfig` 里 `networkingMode=Mirrored`），
  否则 Windows 的 127.0.0.1 到不了 WSL 内的 7890

订阅地址、路径都可覆盖：`cp .env.example .env` 后改，脚本与 systemd 单元都会读。
订阅源**没有内置默认值**：`.env` 至少要有一个 `*_SUB_URL`，否则订阅刷新会明确
失败，而不是静默使用仓库里的 token。历史提交中出现过真实 token，即使已在工作树
移除，也应在服务商侧轮换一次。

### 0.1 二进制获取

```bash
./deploy/fetch-binaries.sh            # 缺什么补什么；已存在且架构正确则跳过
./deploy/fetch-binaries.sh --force    # 全部重下
BIN_MIRROR=https://your-mirror ./deploy/fetch-binaries.sh   # GitHub release 不可达时
```

下载按 **官方 release 的 sha256** 校验（版本与哈希钉在脚本的 `SHA256_ALL` 表里），
不符即拒绝落盘。架构自动识别：amd64 / arm64。`install.sh` 检测到二进制缺失或
ELF 架构不符时会自动调用它——所以从 arm64 机器 clone（仓库内是 amd64 构建）也能用。

`.warp/usque/usque` 必须是**真实文件**，不能是链接：其余 `.warp/usqueN/` 里的
`usque` 都指向它。脚本建链接时已排除主目录自身（`usque*/` 会匹配到 `usque/`，
把真二进制换成指向自己的链接 → 20 条隧道全部 `ELOOP`）。

### 1. 一键部署（推荐）

```bash
./deploy/install.sh          # 生成密钥 + 注册 20 条隧道 + 写 systemd 单元 + 启用
# 若现网单元已指向另一个目录（如从别处 clone），会拒绝覆盖；加 --adopt 确认迁移
./deploy/install.sh --adopt
```

单元内容全部由 `config.py` 的 `warp.members` 推导，clone 到任何目录、任何用户都能跑，
不需要手改 20 个 unit 文件。改了 `members` 之后：

```bash
./deploy/install.sh --units-only   # 只重生成单元，不重启
```

纯手动启动（不走 systemd）可用 `./start-auto-switch-ip.sh`。

### 2. 把 9router 出口指到 7890

`deploy/install.sh` 不碰 9router 的库。首次部署需要这一步（之后由 daemon 自行维护）：

```bash
python3 -c "
import sqlite3, json, os
db = os.environ.get('NINE_ROUTER_DB_PATH') or os.path.expanduser('~/.9router/db/data.sqlite')
con = sqlite3.connect(db); cur = con.cursor()
cur.execute('SELECT data FROM settings WHERE id=1')
s = json.loads(cur.fetchone()[0])
s['outboundProxyUrl'] = 'http://127.0.0.1:7890'
cur.execute('UPDATE settings SET data = ? WHERE id=1', (json.dumps(s),))
con.commit(); con.close(); print('done')
"

# 3. 验证
curl -x http://127.0.0.1:7890 https://api.ipify.org  # 应返回非本地 IP
```

### 3. 检查部署

```bash
python3 -m pytest test_portability.py -q          # 路径/单元可移植性
python3 -m pytest test_daemon_unit.py test_subscription_parse.py test_warp_refresh.py -q
systemctl --user status auto-switch-ip.service
curl -s -H "Authorization: Bearer $(cat ~/.singbox_secret)" \
     http://127.0.0.1:9090/proxies/opencode-out | head -c 200
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
- 主动轮换不等出错就换，两种判据二选一（`proactive_mode`，互斥）：`requests`（当前生效）
  按当前出口已用的 dsv4 配额格数（`proactive_after_dsv4_ok`=150）；`fixed` 每 60s

**轮换池（非 opencode 流量）**：
- daemon 每 60s 调 Clash API `PUT /proxies/proxy` 切换池节点
- 9router 下次出站请求自动走新节点（无需重启 9router）

**安全特性**：
- 出口 IP 记录落盘 `.warp_ip_used.json`，24h 内用过的地址不复用
- 403/429 标脏的 IP 按 6h/24h 恢复期挡在候选池外
- 所有 WARP 不可用时原地重连当前 WARP 线；**落 proxy 池已禁用**（2026-10-26，
  `warp.proxy_fallback_enabled=false`，池内节点全挂）

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
