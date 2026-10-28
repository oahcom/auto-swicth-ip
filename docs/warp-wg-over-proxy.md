# WARP-over-proxy：原生 WireGuard 线路

本文记录「用 sing-box 原生 WireGuard 接 WARP，把 UDP 经代理节点中转」的实现、
换 IP 的实测结论，以及 wgcf 账号注册流程。

- 实现：`warp_wg.py`、`deploy/wg-lines-install.sh`
- 默认关闭：`WARP_WG_COUNT=0`。设成正数才生成线路、并入 daemon 的成员表。
- 与现网 usque 线并存，互不改写对方的单元与凭据。

---

## 1. 为什么需要 detour

本机到 Cloudflare WARP 的 WireGuard 端口 **直连不通**，但原因不是「UDP 被封」，
而是到 `162.159.192.1:2408` 这条路径被 ACL 拦：

| 探测 | 结果 |
|------|------|
| UDP 53 / STUN 3478 / UDP 443 | 正常回包 |
| UDP traceroute → 2408 | 经 CGNAT、广东电信、`202.97.x` 到 Cloudflare 边缘 `172.68.188.x` |
| 中途一跳 | `27.50.59.99` 返回 **ICMP type 3 code 13**（Administratively Prohibited） |
| 真实 WG 握手（直连） | 12–24s 超时，sing-box 报 `context canceled` |

所以同一份 WireGuard 配置，只要把 UDP 交给一个能出海的代理节点转发，就能握手成功。
sing-box 1.13 的 `endpoints[]` 支持给 endpoint 指定 `detour`，正好表达这件事。

---

## 2. 架构

```
9router :20127
   └─ sing-box :7890（主进程）
        ├─ selector proxy ────────► 83 个订阅节点
        └─ selector opencode-out
             ├─ WARP-CF…WARP-CF-20 ─► socks 127.0.0.1:1080-1099（usque / MASQUE）
             └─ WARP-WG-1…N ────────► socks 127.0.0.1:1110+（本方案）
                                          │
                                          ▼
                                   sing-box 线路进程（每条线一个）
                                     inbound :1110+（仅环回）
                                     outbound：某个代理节点（vless）
                                     endpoint wireguard
                                       detour = 该代理节点  ← UDP 由此转发
                                     route.final = warp
                                     DNS 用本机解析器（同主配置 dns-default）
                                     地址族 = dns.strategy（v4 线 prefer_ipv4，
                                                v6 线 prefer_ipv6）
```

线路按「落地节点 × 渠道」铺开：一个落地节点挂 `per_node` 条线，其中一条走 v4、
其余走 v6，每条线各自一个 WARP 设备与进程。`warp_wg.py survey` 实测的 colo 表
决定哪几个节点算「不同落地」——同 colo 的节点出口网段相同，不能同时当 v4 渠道。

设计上的三个取舍：

1. **每条线一个进程**。重启只影响这条线，daemon 现有的
   `_restart_service` / `_probe_refresh_egress` / 预热逻辑原样可用。
   把 endpoint 塞进主进程会导致「换一条线要重启全部代理流量」。
2. **detour 绑定到具体节点，而不是主池的 7890**。出口 IP 由 WARP 落地的 colo
   决定；共用 7890 会把所有线压进同一个 colo，拿到同一个地址（见第 4 节实测）。
3. **渠道族写在线路里，不写在与主进程的约定里**。主进程的 socks 出站只透传
   域名，出口族完全由线路的 `dns.strategy` 决定，换族不用动主配置。
4. **DNS 不做 detour**。节点 `server` 是域名；若线路的 DNS 也 detour 到该节点，
   解析节点自身就要先连上节点，形成环路（sing-box 报
   `DNS query loopback in transport[dns]`）。所以 DNS 沿用主配置的
   `{"type":"local"}`，peer 直接用 IP。

---

## 3. 账号注册流程（wgcf）

### 3.1 安装 wgcf

```bash
mkdir -p .warp
curl -fL -o .warp/wgcf \
  https://github.com/ViRb3/wgcf/releases/latest/download/wgcf_2.3.1_linux_amd64
chmod +x .warp/wgcf
.warp/wgcf --version
```

非 amd64 换成对应架构（`_linux_arm64`）。

### 3.2 手动注册（单条线）

```bash
D=.warp/wglines/line1
mkdir -p "$D"
timeout 60 .warp/wgcf register --config "$D/account.toml" --accept-tos -n WARP-WG-1
timeout 60 .warp/wgcf generate --config "$D/account.toml" -p "$D/profile.conf"
chmod 600 "$D/account.toml" "$D/profile.conf"
```

- `register` 会创建一台新的 Cloudflare 设备（独立身份），首次可能卡住不返回；
  实测重跑一次即可，所以上面套了 `timeout`。
- `generate` 产出 WireGuard 私钥与接口地址。WARP 的 peer 公钥是固定的
  `bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=`，由 profile 自带。
- **不能复用** `.warp/usque*/config.json`：那是 MASQUE 的 ECDSA PEM 密钥，
  和 WireGuard 的 Curve25519 密钥不是一回事。

### 3.3 批量注册（推荐）

```bash
# 先实测落地 colo：v4 渠道的上限就是不同 colo 的个数
python3 warp_wg.py survey
# 4 个落地节点 × 每节点 4 条渠道（1 条 v4 + 3 条 v6）= 16 条线
WARP_WG_FAMILIES=v4,v6,v6,v6 WARP_WG_NODES=4 \
  WARP_WG_PROBE=1 ./deploy/wg-lines-install.sh
```

`apply` 会跳过已存在的 profile（重复注册会换掉正在使用的密钥，让隧道掉线）。

Cloudflare 对设备注册限流：实测同一窗口到第 10 台返回 `429 Too Many Requests`。
`register_line` 按 30/90/240s 退避重试，仍失败则删掉半成品目录后退出，并提示
「已就绪 N 条线，可先按 `--count N` 生成配置」。线路数超过 8 条时分批注册。

### 3.4 文件布局

```
.warp/                      # gitignored
  wgcf                      # 二进制
  wglines/line1/
    account.toml            # 设备身份（含私钥/令牌），600
    profile.conf            # WG 密钥与接口地址，600
    singbox.json            # 由 warp_wg.py 生成，600
  warp_wg_state.json        # 线 → 节点 的当前分配
  warp-outbound.json        # 主 sing-box 的出站清单（usque + WG 共用）
```

---

## 4. 换出口 IP：实测结论

出口 IP 由 `(落地 colo × 地址族 × 会话)` 决定，与设备数量无关。
2026-10-28 实测（`icanhazip` / `cdn-cgi/trace`）：

| 操作 | 出口 | 说明 |
|------|------|------|
| v4 渠道 → NRT，重启 1/2 | `104.28.243.105` → `104.28.211.105` | 同 colo 池内换地址 |
| v4 渠道 rebind 到 SIN | `104.28.222.47` | 换 colo 才换段 |
| v6 渠道 → NRT，重启 1/2 | `2a09:bac5:4304:dc::16:1b1` → `2a09:bac5:4302:dc::16:1bb` | 连 /64 都换 |
| v6 渠道 → NRT，4 次重启 | 4 个互不相同的 `/64` | 每次都换 |

**v4 出口与节点无关，是每个会话从 colo 池里随机抽**。同一批次实测
（5 个 NRT 节点 × 3 次重启、每个节点都用同一份 profile）：

```
pq.aws48   104.28.211.105 | 104.28.243.105 | 104.28.243.105
pq.aws49   104.28.211.105 | 104.28.243.105 | 104.28.211.105
pq.aws53   104.28.243.105 | 104.28.243.105 | 104.28.211.105
pq.aws54   104.28.211.105 | 104.28.211.105 | 104.28.211.105
unamecf    104.28.243.105 | 104.28.243.105 | 104.28.211.105
```

所以「同 colo 的节点各挂一条 v4 = 多个出口」不成立，survey 表里按节点分组的
`.243`/`.211` 只是单次快照的时序巧合。各 colo 的池子（18 次采样）：

| colo | 池内地址 | 段（/24） | 主导地址占比 |
|------|----------|-----------|--------------|
| NRT | `.211.105` `.243.105` | 2 | 约 50/50 |
| PHX | `.205.113` `.205.117` | 1 | 4/6 落在 `.117` |
| SIN | `.222.43` `.222.46` `.254.46` | 2 | 4/6 落在 `.222.43` |
| SJC | `.201.80` `.233.73` | 2 | 5/6 落在 `.201.80` |

### colo 清单与代理节点的对应（2026-10-28 全量实测）

27 个节点里 23 个转发 UDP，只落 4 个 colo。**colo 与代理节点的物理位置不必
一致**，它取决于该节点出站时被 Cloudflare 接在哪：

| 代理节点 | 节点物理位置（ISP） | WARP v4 | WARP v6 |
|----------|--------------------|---------|---------|
| aws48–57（10） | 日本东京（AWS） | NRT | NRT |
| aws63–67（5） | 新加坡（AWS） | SIN | SIN |
| us01–04（4） | 美国凤凰城（1GSERVERS） | PHX | PHX |
| us05–07（3） | 美国锡拉丘兹（Black Mesa） | SJC | SJC |
| unamecf / unamecf2 | Cloudflare 前置（Ashburn） | NRT | NRT |
| downloadcfpro / unamecf3 / 203.10.96.138 / aws52 | — | UDP 不通，WARP 握手失败 | — |

v4 与 v6 的 colo 在 23/23 个节点上一致：`dns.strategy` 只换地址族，不换落地。
理论上若代理对 v4/v6 走不同上游会分叉，所以换订阅源后仍应用
`warp_wg.py survey`（v4）加 v6 trace 复核，而不是假定一致。

常见 colo 的 v6 前缀也固定：SIN 只在 `2a09:bac1:65xx:8::/…`，SJC 在
`2a09:bac1:76c0:28::` 与 `2a09:bac5:621x:…`，NRT 在 `2a09:bac5:43xx:dc::` 与
`2a09:bac1:3bxx:10::`。

### usque（MASQUE）与 WG 的关系

2026-10-28 同时刻实测 20 条 usque（1080–1099，18 条成功、2 条不通）：

| 维度 | usque | WG（16 条） | 是否冲突 |
|------|-------|-------------|----------|
| colo | SJC ×17、LAX ×1 | NRT/PHX/SIN/SJC 各 4 | **SJC 重叠** |
| 出口族 | 全部 IPv6（历史 10.7 万条采样 0 条 v4） | 4 v4 + 12 v6 | 不冲突 |
| `/32` | `2a09:bac1::` `2a09:bac5::` | 同两个 | 共享 |
| `/48` | 29 个（`bac1:19xx` 4 个、`bac5:1ef8–1f07`/`2058–205f`/`20b3` 25 个） | 10 个（`bac1:3b20/3b40/652x/76x0`、`bac5:4304/55f9/55fb`） | **无交集** |
| `/64` | 历史 161 个 | 当前 11 个 | 无交集 |
| trace `loc` | **CN** | JP / SG / US | 画像不同 |

结论：两套系统抢的是**同一个 colo（SJC）**，但拿的是**不同的 `/48` 块**。所以
按单 IP 或按 /48 计的风险不互相污染；按 colo/地区计的风险会叠加。usque 的
`loc=CN` 与 WG 的真实 loc 不同，若上游按地域判信誉，两者画像并不等价。

结论：**v4 渠道数等于 colo 数就够了**。同 colo 第二条 v4 线与第一条争抢同一个
主导地址，只有 17%~50% 的概率落到另一个，收益抵不上一条线 46MB 的常驻内存；
要数量就用 v6 渠道，它每次重启必然换 `/64`。

三条结论：

1. **v6 出口重启必换**，v4 出口只在 colo 地址池内翻牌（NRT 观察到 `.211`/`.243`，
   SIN `.222`，PHX `.205`，SJC `.233`）。所以拓扑是「每个落地节点 1 条 v4
   渠道 + 若干 v6 渠道」，v4 的多样性上限等于 colo 个数。
2. **地址族由线路自己的 `dns.strategy` 决定**。主 sing-box 的 socks 出站透传
   域名，线路里 `prefer_ipv4`/`prefer_ipv6` 直接决定 WARP 出口族。不能用
   `ipv4_only`/`ipv6_only`：只有 AAAA 的目标会直接不可达。
3. **同 colo 是同一出口**。aws49 与 unamecf 实测同为 NRT，v4 相同。
   `region_key` 因此优先用 `survey` 实测的 colo 分组，主机名只在无数据时兜底。

因此本实现的定位是：

- v6 渠道：`systemctl restart warp-wg-<i>` 就换地址，daemon 的预热/换线直接生效。
- v4 渠道：重启只换主机地址，要换网段必须 `rebind`。daemon 在
  `_refresh_idle_warp` / `_restart_warp_inplace` 的第 2 次及以后重试时才调用
  `rebind`，把换节点的代价留在失败路径上。

## 5. 启用与关闭

### 启用

```bash
# daemon 与部署脚本都读同一组变量，必须在两处一致
export WARP_WG_FAMILIES=v4,v6,v6,v6
export WARP_WG_NODES=2          # → 8 条线：每节点 1 v4 + 3 v6，端口 1110..1117

# 先只生成配置，不把还没起起来的端口写进主配置
WARP_WG_FAMILIES=... WARP_WG_NODES=2 python3 warp_wg.py apply --no-publish
# 人工起 1-2 条验证出口族，再安装/启用单元并发布出站清单
WARP_WG_FAMILIES=... WARP_WG_NODES=2 WARP_WG_PROBE=1 ./deploy/wg-lines-install.sh
```

**daemon 也必须带同样的变量**，否则它根本不认识这些线：

```ini
# /etc/systemd/system/auto-switch-ip.service
Environment=WARP_WG_FAMILIES=v4,v6,v6,v6
Environment=WARP_WG_NODES=4
```

`CFG["warp"]["members"]` 在 config.py 导入时按这两个变量展开；daemon 进程没有
它们，`members` 里就只有 usque，sing-box 里虽有 16 个出站但永远不会被选中、
预热或换线。2026-10-28 实测：16 条线 active、`daemon.log` 里 `WARP-WG` 出现
**0 次**，全部轮换都发生在 `WARP-CF-*`（usque）上。改完要
`systemctl daemon-reload && systemctl restart auto-switch-ip`。

`install.sh` 之外还要让主 sing-box 读到新的 socks 出站：走项目现有的刷新路径
重新生成 `singbox-config.json` 并重载 sing-box。之后 `opencode-out` 里会出现
`WARP-WG-1…N`，daemon 把它们当普通成员对待（探测、预热、换线）。

### 关闭

```bash
systemctl --user disable --now warp-wg-{1..8}.service
unset WARP_WG_FAMILIES WARP_WG_NODES   # 或置 0
python3 deploy/members.py --write-warp-outbound   # 主配置里移除这些出站
```

`.warp/wglines/` 可保留，下次启用不必重新注册。

### 单独操作

```bash
python3 warp_wg.py status          # 单元状态 + 当前节点
python3 warp_wg.py rotate 2        # 换节点并重启第 2 条线
python3 warp_wg.py apply --dry-run # 只打印分配，不写文件
```

---

## 6. 已知限制

- **不是所有节点都转发 UDP**。`--probe` 实测：aws48/aws49/unamecf OK，
  downloadcfpro FAIL（WARP 握手反复 `read packet: EOF`）。
  `--probe` 用 STUN 3478 预检（而不是 53，避免只放行 DNS 的节点蒙混过关），
  但探测通过不保证 2408 一定可用；首次启用请人工核对 `warp=on`。
- **同 colo 的设备共享出口网段**。多注册账号只在 colo 地址池内翻牌，不会多出
  地理位置；`survey` 的 colo 个数就是 v4 渠道的多样性上限。
- **注册有速率限制**。约 9 台/窗口，超出见 3.3 的退避与分批策略。
- **MTU 1280**，比 usque 保守；不要调大，代理链路会分片。
- **主 sing-box 重载是断流操作**（与 usque 扩容时相同），应放在低峰。
- 未验证：长时间流式连接的稳定性、经该出口访问 opencode 的 200/403 比例。
- 每条线一个进程，PSS 约 9 MB（RSS 43 MB 里大部分是共享二进制，见第 8 节）。
- **模型层 quota 与出口 IP 无关**。2026-10-28 04:05 观测到 opencode free 返回
  `模型层: quota（Retry-After 3.9h）`且 403 占比 0%：这种情况换线、重启都无效，
  但 daemon 把它归为「出口级错误」，单条 usque 线一个会话被重启 33–35 次，
  日志按分钟刷屏，还挤掉了真正的 IP 级故障处理。
  **已分流**（`_QUOTA_HOLD`，2026-10-28）：Retry-After ≥ 30 分钟的 quota 判定为
  账号/模型级额度耗尽，开全局闸（闸长 = Retry-After，上限 4h，只延长不缩短），
  闸内跳过「quota / 惩罚期 / 429」类重启并推迟重探（≤15 分钟）；短 Retry-After
  与隧道半死（连续取不到地址）仍照常处理。实测效果：改动前 30 分钟重启 253 次，
  改动后 04:11–04:20 重启 2 次。

---

## 7. 测试

```bash
python3 -m pytest test_warp_wg.py -q
```

覆盖：profile 解析、IPv6 peer 拆分、detour/final/DNS 路由、`dns.strategy`
随渠道族切换、生成配置的 `sing-box check`、按 colo 分组的节点分配、
rotate 的「先写配置后重启」顺序、`WARP_WG_FAMILIES × WARP_WG_NODES`
驱动 tag/端口/单元名与 home 分组。

---

## 8. 资源开销（16 条线实测，2026-10-28）

```
MemTotal 15.9G   MemAvailable 7.4G   Swap 8G（已用 36M）
AnonPages 7.8G   Cached 5.3G
```

| 分组 | 进程 | PSS 合计 | 单进程 |
|------|------|----------|--------|
| 16 条 WG 线路 sing-box | 16 | 150 MB | 8–10 MB |
| 主 sing-box（7890 + selector） | 1 | 18 MB | 18 MB |
| usque（MASQUE，1080–1099） | 20 | 66 MB | 3 MB |
| asip daemon（含 worker 线程） | 5 | 368 MB | — |

**按 PSS 算，一条 WG 线约 9 MB**；`ps` 看到的 RSS 是 43 MB/条、合计 740 MB，
因为每条线都映射同一份 40 MB 级的 sing-box 二进制与共享库——用 RSS 估容量会
高估 4~5 倍。真实增量以 PSS 为准：再铺 16 条约 +150 MB。

同期全机 RSS 合计 12.2 GB / PSS 合计 8.6 GB，占用大头不在代理：
PyCharm remote 2.9 GB、9router 12 进程 1.9 GB、opencode TUI 1.4 GB。


---

## 9. 上线结果与两处选线回归（2026-10-28 08:00–08:30）

### 9.1 部署

```bash
WARP_WG_FAMILIES=v4,v6,v6,v6 WARP_WG_NODES=4 WARP_WG_PROBE=1 \
  ./deploy/wg-lines-install.sh
```

- 16 个系统级单元（`warp-wg-1..16`）全部 active，端口 1110–1125。
- 16 个独立 WARP 设备（wgcf `register`），第 9 台起撞 CF 限流，30/90/240s
  退避后全部拿到 profile。
- 实测出口：4 条 v4（NRT/PHX/SIN/SJC 各一）+ 12 条 v6，**16 个互不相同的地址**。
- `.env` 加 `WARP_WG_FAMILIES`/`WARP_WG_NODES`，daemon 重启后成员表
  19 条（3 usque + 16 WG）。

### 9.2 回归一：订阅源为空时新出口进不了主配置

`OKZ_SUB_URL` 等四个订阅变量当前都没配，`refresh_subscription` 直接
「无可用订阅源」返回，`singbox-config.json` 不会重生成 —— `warp-outbound.json`
里新增的 16 条 socks 出口进不了 `opencode-out` selector，切换全部
`status=400`。

处置：`patch_wg_outbounds.py` 就地并入（幂等），`sing-box check` 通过后重启主
sing-box。**订阅一旦配回来，这一步会被 `parse_subscription.py` 自动接管**，
补丁脚本只在无订阅期用。

### 9.3 回归二：`/rotate` 换走的出口没有拉黑（真 bug）

`_rotate_once` 重构为复用 `_reconnect_warp_by_table` 后，只保留了
`_mark_warp_ip_used(old_ip, now)`（= 记「用过」），**丢了 `blocked=True, kind=...`**。

后果：被 429 打下来的出口 60s 后重新成为候选，而它在台账里有 `last_ok_ts`
（档位 1，高于只有 probe_ok 的档位 2），于是轮换在**两条撑过生产流量的线之间
来回**，另外十几条新鲜线永远轮不到。实测 10 次连续 `/rotate` 只在
`WARP-CF-B` 与 `WARP-WG-1` 之间乒乓。

修复：`_rotate_once` 换线前取 `old_ip`，成功后按 reason 拉黑
（`proactive` 除外）。修复后同样 10 次轮换落到 WG-5/6/7/9、CF-D、WG-4/15
共 7 条不同线。

同时发现 `tests/test_rotate.py` 仍在 monkeypatch 已删除的 `_rotate_pick` /
`_finalize_warp_switch` / `_close_line_conns_now`，**这条路径早就没有回归覆盖**
—— 这才是缺陷能溜进主干的直接原因。已按当前实现重写（合并窗口、限速、
无备线、拉黑 429/403、主动轮换不拉黑），`tests/test_finalize.py` 与
`tests/test_rotate_add.py` 测的是已废弃接口，一并删除。

### 9.4 容量

单轮 10 次轮换统计：`switched=10`、distinct egress=7、`too_frequent=22`
（测试节奏 9s < 8s 限速边界的边界抖动）、`no_warm_standby=1`（探测器补货中）。
比修复前「2 条线乒乓」实质改善了供给面。
