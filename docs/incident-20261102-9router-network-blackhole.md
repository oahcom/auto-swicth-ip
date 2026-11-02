# 2026-11-02 9router 全站不可用：两个独立故障（warp 全局 TUN + WSL mirrored 端口镜像失效）

记录人：opencode
范围：`/var/lib/cloudflare-warp/settings.json`、WSL mirrored 端口镜像、`auto-swicth-ip/daemon.py`
关联：`docs/warp-channel-allocation.md`（H2 路线终局）、提交 `a335a41`

## 一句话

**两个独立故障叠加**，我一开始只找到第一个就宣布修好了，这是判断失误：

- **故障 A**：warp 被我切成 `operation_mode=Warp` 且 `override_warp_endpoint=127.0.0.1:443`
  （本机无人监听），warp 建全局 TUN，把 TCP/UDP 出网导进黑洞 → 公网全断。
- **故障 B**：WSL mirrored 模式的**端口镜像整体失效**。WSL 内服务全在跑、`curl 127.0.0.1`
  正常，但 Windows 侧 `netstat` 看不到任何 20127 的 LISTEN，从宿主机连不进去。
  这才是用户「9router 没起来」的**直接原因**。

故障 A 于 13:36 修完，但 13:34–14:14 用户仍连不上，因为 B 还在。
B 只能靠 `wsl --shutdown`（重启 WSL 虚拟机）恢复。

## 重要更正：`loopback0` 与 127/128 规则不是元凶

初版文档把 `loopback0` + `ipproto tcp/udp lookup 127|128` + `169.254.73.249`
判为 warp 写死的残留，**这是错的**。实测证据（`warp-svc` 已 `inactive/disabled`）：

```
$ ip rule | grep -E "lookup 12[78]"
1:  from all ipproto tcp lookup 127
1:  from all ipproto udp lookup 127
1:  from all ipproto tcp lookup 128
1:  from all ipproto udp lookup 128

$ ip -br link | grep loopback0
loopback0  UP  00:15:5d:ef:2a:b2

$ ip route show table 127 → 127.0.0.1 via 169.254.73.249 dev loopback0
$ ip route show table 128 → 192.168.1.97 via 169.254.73.249 dev eth0

# 而此刻网络完全正常：
$ curl -4 https://www.baidu.com        → 200
$ ping -4 -c 2 223.5.5.5               → 0% packet loss
```

warp 停掉、规则还在、网络正常 → 这套 rule 是 **WSL mirrored 模式自带的基础设施**
（`169.254.73.249` 是 mirrored 的 loopback 代理地址，`loopback0` 由 WSL 网络栈托管），
不是故障源。删掉它们重启 WSL 后会自动重建。**以后不要再把它们当病灶。**

## 现象

- `9router` 服务 `active`，`20127/20128` 正常监听，`/v1/models` 本机 curl 200。
- 但任何真实请求 63s 后 `503`，报
  `Bun transport unavailable (caller-aborted)` → `sidecar unavailable`。
- combo 上游报 `EHOSTUNREACH: connect EHOSTUNREACH 192.168.1.143:20127`。
- 用户侧观感：「9router 坏了一个晚上」「重启电脑也没用」。
- **宿主机视角（故障 B 的核心）**：Windows 上 `curl localhost:20127` 全超时，
  `netstat` 查不到 20127 的 LISTEN，连 WSL 的 22 端口也消失。

## 关键证据

### 1. 出网全断，但 LAN 与 DNS 正常（这是最强指征）

```
$ ping -c 2 192.168.1.1          → 0% loss（网关通）
$ ping -4 -c 2 223.5.5.5         → 100% packet loss（公网不通）
$ getent hosts www.baidu.com     → 正常解析
$ curl -4 https://www.baidu.com  → 超时
```

`ping` 内网通 + `ping` 公网不通 + DNS 正常，排除了 DNS、网关、物理链路，
指向**按协议族劫持**：ICMP 不受影响，TCP/UDP 全灭。这是故障 A 的典型指纹。

### 2. warp 配置：全局 TUN + 打不通的 endpoint

```json
"operation_mode": "Warp",                    ← 应为 WarpProxy
"override_warp_endpoint": "127.0.0.1:443",   ← 本机无人监听
"masque_settings": { "masque_http_version": "h2_only" }
```

warp 日志自证它一直在往 `127.0.0.1:443` 打 H2：

```
connect_single_protocol{protocol="H2"}: warp_edge::happy_eyeballs:
  Happy Eyeballs check failed v4=127.0.0.1:443
  err=Connect(Os { code: 110, kind: TimedOut })
ERROR warp_edge::fallback::single_protocol:
  MASQUE (HTTPS via TCP)(Non-PQ) failed err=HappyEyeballs(AllChecksTimedOut)
```

### 3. `EHOSTUNREACH` 指向局域网设备

9router combo 上游报 `EHOSTUNREACH: connect EHOSTUNREACH 192.168.1.143:20127`
——目标是同网段的另一台 9router2 节点，不是公网。warp 的全局 TUN 同样劫持了
到 LAN 的 TCP 连接，所以 combo 上游一并失败。

### 4. 端口镜像失效（故障 B 的直接证据）

```
# 宿主机
$ netstat -ano | findstr 20127   → 空
$ Test-NetConnection 127.0.0.1 -Port 22   → False

# 同一时刻 WSL 内部
$ ss -ltn | grep 20127           → LISTEN 0.0.0.0:20127
$ curl 127.0.0.1:20127/v1/models → 200
```

### 5. 时间线

| 时刻 (CST) | 事件 |
|---|---|
| 11-01 23:3x–11-02 04:0x | 测 H2 路线：`warp-cli mode warp` + `tunnel endpoint set 127.0.0.1:9450`，最终 `override_warp_endpoint` 落盘为 `127.0.0.1:443` |
| 11-02 03:31:48 | 9router 首条 `EHOSTUNREACH`（psql 侧已断） |
| 11-02 03:58:00 | 最后一条 `EHOSTUNREACH` |
| 11-02 04:21:37 | Windows 侧关机（用户重启电脑），WSL 随之终止 |
| 11-02 11:48:39 | 开机，warp-svc **仍随 systemd 自启**（`is-enabled=enabled`）→ 立刻重建黑洞 |
| 11-02 11:51:29 | `daemon.py` 首条 `ValueError: too many values to unpack` |
| 11-02 12:12 | 断开 warp + 清 iptables（但没清策略路由，warp 也没 disable） |
| 11-02 13:34 | 用户报「9router 炸了」；实测出网仍全断，warp-svc=active |
| 11-02 13:36:45 | `systemctl stop + disable warp-svc`、删 4 条 rule、flush table 127/128 |
| 11-02 13:39 | **故障 A 修复**，WSL 内部端到端 3 次 200 —— 但这是自证，不是用户视角 |
| 11-02 13:5x | 用户在 Windows 侧仍是全超时，报「9router 还没起来」 |
| 11-02 14:14 | 从 Windows 侧实测：`netstat` 无 20127 LISTEN、22 端口也不通 → **定位故障 B** |
| 11-02 14:15 | `wsl --shutdown` 重启 WSL 虚拟机 —— **副作用：用户在跑的 claude 会话被中断** |
| 11-02 14:1x | 端口镜像恢复，Windows → `localhost:20127` POST 200 / 200 / 200 |

「重启电脑也没用」的原因：warp-svc 是 enabled，开机即重建同一套路由。

## 故障 B 详解：WSL mirrored 端口镜像失效

### 症状（最容易误判的一种）

```
# WSL 内部——完全正常
$ curl http://127.0.0.1:20127/v1/models     → 200
$ ss -ltn | grep 20127                       → LISTEN 0.0.0.0:20127

# Windows 宿主机——全超时
$ curl http://localhost:20127               → timeout
$ curl http://127.0.0.1:20127               → timeout
$ curl http://192.168.1.97:20127            → timeout
$ netstat -ano | findstr 20127              → 没有任何 LISTENING 记录
$ Test-NetConnection 127.0.0.1 -Port 22     → False   ← 连 WSL 自带的 22 也没了
```

**判定要点**：Windows 侧 `netstat` 完全看不到 WSL 里的监听端口 = 端口镜像层没了，
不是 9router 的问题、不是防火墙、不是 `localhostForwarding` 配置。
`.wslconfig` 里 `networkingMode=mirrored` 时，WSL 的监听端口本应镜像到宿主
`127.0.0.1`，镜像层挂掉后所有端口（含 sshd）一起消失。

### 为什么会被打断

mirrored 模式依赖 Hyper-V 的 hns/GNS 端口跟踪。当晚 Windows 侧出现
**虚拟内存耗尽**（Resource-Exhaustion-Detector，03:04–03:28 共 4 次），
加上随后 `wsl --shutdown` 强杀，端口跟踪状态没有被正确重建。

### 修复

```
wsl --shutdown          # 必须整机重启，restart 单个服务无效
# 等 5 秒后重新进入 WSL，服务会随 systemd 自启
wsl -d Ubuntu -e bash -lc "systemctl is-active nine-router-src auto-switch-ip"
```

验证要从 **Windows 侧**发起，不要只在 WSL 里 curl：

```
curl.exe -s -o NUL -w "%{http_code}" -H "Authorization: Bearer <key>" http://localhost:20127/v1/models
netstat -ano | findstr 20127    # 这次应能看到 LISTENING
```

### 代价（重要）

`wsl --shutdown` 会杀掉 WSL 里所有前台交互进程，**包括正在跑的 `claude` / `codex` 会话**。
本次就因此中断了用户的 Claude 会话。以后再用这条命令前必须先确认没有交互式会话在跑：

```bash
ps -eo pid,etime,args | grep -E "[c]laude|[c]odex"   # 有输出就先问用户
```

Claude 会话文件本身不会丢（`~/.claude/projects/<项目>/<uuid>.jsonl`），
`claude -r` 可以接回，但**正在执行的工具调用会中断在半途**。

## 故障 A 详解：全局 TUN 劫持

`operation_mode=Warp`（正确值是 `WarpProxy`）会让 warp 接管**整机路由**而非只开本地
SOCKS 代理；配合 `override_warp_endpoint=127.0.0.1:443`（本机无人监听），隧道永远
建不起来，但路由劫持已经生效 → 所有 TCP/UDP 出网进黑洞。

warp 日志自证它一直在往 `127.0.0.1:443` 打 H2：

```
connect_single_protocol{protocol="H2"}: warp_edge::happy_eyeballs:
  Happy Eyeballs check failed v4=127.0.0.1:443
  err=Connect(Os { code: 110, kind: TimedOut })
ERROR warp_edge::fallback::single_protocol:
  MASQUE (HTTPS via TCP)(Non-PQ) failed err=HappyEyeballs(AllChecksTimedOut)
```

## 修复

```bash
# 1) 停掉并使 warp 不再自启（这是治故障 A 的关键一步）
sudo systemctl stop warp-svc
sudo systemctl disable warp-svc
systemctl is-enabled warp-svc      # 必须复核，别让管道吞掉输出

# 2) 改回 Proxy 模式并清掉 endpoint 覆盖（先备份）
sudo cp /var/lib/cloudflare-warp/settings.json /var/lib/cloudflare-warp/settings.json.bak.$(date +%s)
sudo python3 - <<PY
import json
p = "/var/lib/cloudflare-warp/settings.json"
d = json.load(open(p))
d["operation_mode"] = "WarpProxy"
d.pop("override_warp_endpoint", None)
d.pop("masque_settings", None)
json.dump(d, open(p, "w"), indent=2)
PY
```

> **不需要**删 `ip rule` 里的 127/128 规则，也不要删 `loopback0`——见上文「重要更正」，
> 它们是 WSL mirrored 自带基础设施，删了重启还会回来，且与故障无关。
> 初版文档把这一步写成"修复步骤 2"，是错误结论，已删除。

## 验证（必须两边都测）

**WSL 内部**：

```
nine-router-src   active / enabled
auto-switch-ip    active / enabled
warp-svc          inactive / disabled

ping -4 223.5.5.5      → 0% loss
curl -4 baidu          → 200
经 7890 出海 opencode  → 200
```

**Windows 宿主机（关键，本次就是漏了这步才误判）**：

```
netstat -ano | findstr 20127        → 必须有 LISTENING
curl.exe -s -o NUL -w "%{http_code}" http://localhost:20127/v1/models   → 200
POST /v1/chat/completions × 3       → 200 / 200 / 200
```

## 同期修复：daemon.py 解包崩溃

`692790b` 给 `ErrorWindow` 加了第 6 个字段 `transport_errors`，但 `main()`
里的解包还写死 5 个：

```python
_, errors_after_switch, _, _, _ = get_9router_error_rate(...)   # ValueError
```

主循环每 10s 抛一次 `ValueError: too many values to unpack (expected 5)`，
换线判定整段失效——这也是「换线看起来没在工作」的直接原因。改为兜尾解包：

```python
_, errors_after_switch, *_ = get_9router_error_rate(...)
```

提交 `a335a41`，重启服务后 `too many values` 计数归零、`NRestarts=0`。

## 教训（下次别再踩）

1. **动 warp 前先记下原始 settings**，测完立刻还原。`operation_mode` 是持久化的，
   一次 `mode warp` 会跟着机器活到下次重启，甚至跨重启。
2. **`systemctl disable` 必须复核 `is-enabled`**。这次 12:12 那次
   `disable` 的具体输出被管道吞了，没验证，导致开机又自启。
3. **`ping` 通不代表能连**。ICMP 不命中 `ipproto tcp/udp` 规则，最容易骗人。
   判定出网要用 `curl` / `ping -4 公网IP` 组合。
4. **测试用 warp 请开独立 netns**（`ip netns exec`），别在主 netns 里切 `mode warp`。
5. **不要在 WSL 内部验证「宿主机能否访问」**。这是本次最大的误判：
   WSL 里 `curl 127.0.0.1:20127` 返回 200，就宣布修好了，但用户从 Windows
   一直连不上。**跨边界的连通性必须在对端验证**。
6. **端口全丢时先看宿主 `netstat`**。WSL 里 `ss -ltn` 有监听、宿主 `netstat`
   没有 = mirrored 端口镜像挂了，`wsl --shutdown` 是唯一解。
7. **`wsl --shutdown` 是破坏性操作**。会杀掉所有前台交互会话（claude/codex/
   长任务）。执行前先 `ps -eo pid,etime,args | grep -E "[c]laude|[c]odex"`，
   有交互进程就必须先问用户，不能自作主张。
8. **不要急着下"根因已确认"的结论**。本次三个错误结论（loopback0 是残留、
   12:12 已修好、warp 是唯一原因）都是因为只验证了一半就收工。
   症状消失 ≠ 根因排除，要能解释**每一条**观测到的现象。
