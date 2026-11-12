# CF 九条线：全部走 h2（5 条 v4 入口 + 4 条 v6 入口）+ 改名为 WARP-CF4/CF6

时间：2026-11-13 00:07 – 00:25
备份后缀：`.bak-20261112-235644`

## 一、结论速览

| 项 | 状态 |
|---|---|
| 9 条承载 | **全部 h2**（MASQUE over TCP+TLS）：1080-1084 走 v4 入口，1085-1088 走 v6 入口 |
| tag | `WARP-CF4` / `WARP-CF4-B..E`（v4 入口）、`WARP-CF6` / `WARP-CF6-B..D`（v6 入口） |
| 探测纳入 | **9/9**，每条都有实时出口 IP 与 verdict |
| selector 闸口 | **9/9 可切换**（逐条 `PUT /proxies/opencode-out` 全 204，`now` 正确变更） |
| 测试 | asip 全量 **1134 通过** |
| 当前额度 | **全池为 0**（9 条 verdict 全 `quota`，Retry-After 7.6–8.6h） |

## 二、为什么要 h2，而且 v6 也要 h2

本机到 Cloudflare 的 **UDP 443 被运营商 ACL 拦**（`ICMP type 3 code 13`），QUIC 版 v4 端点
完全起不来；而 `--http2` 与 `-6` **可以叠加**，v6 侧同样能走 TCP 443。实测：

| 入口 | 端点 | 结果 |
|---|---|---|
| v4 | `162.159.198.2`（原配置值） | 无响应（UDP 与 TCP 都不通） |
| v4 | `162.159.192.1` / `193.1` / `204.1` | `tls: handshake failure` |
| v4 | **`162.159.198.3` – `198.9`** | 全部 `Connected to MASQUE server` |
| v6 | **`2606:4700:103::2`** | `Using HTTP/2 endpoint [...]:443` → `Connected` |

两种入口的**出口都是 WARP 分配的 `2a09:bac*` v6 地址** —— 所以 `transport`（本地到 CF 的承载）
与 `family`（出口地址族）是两个维度，配置里分开记。

### 5:4 的分配

| 端口 | tag | transport | 入口端点 |
|---|---|---|---|
| 1080 | `WARP-CF4` | h2 | `162.159.198.3` |
| 1081 | `WARP-CF4-B` | h2 | `162.159.198.4` |
| 1082 | `WARP-CF4-C` | h2 | `162.159.198.5` |
| 1083 | `WARP-CF4-D` | h2 | `162.159.198.6` |
| 1084 | `WARP-CF4-E` | h2 | `162.159.198.7` |
| 1085 | `WARP-CF6` | h2v6 | `2606:4700:103::2` |
| 1086 | `WARP-CF6-B` | h2v6 | `2606:4700:103::2` |
| 1087 | `WARP-CF6-C` | h2v6 | `2606:4700:103::2` |
| 1088 | `WARP-CF6-D` | h2v6 | `2606:4700:103::2` |

`WARP-CF6-*` 的四台设备 `config.json` 里 `endpoint_h2_v6` 原来是空串，这次填上了；
v4 端点保留，隧道在拿不到 v6 源地址时会自动降级成 `h2`。

## 三、改了什么

| 对象 | 改动 |
|---|---|
| `.warp/usque{,2..9}/config.json` | v4 入口设 `endpoint_h2_v4`；v6 入口设 `endpoint_h2_v6`（原为空串，不填就走不了 v6/h2） |
| `deploy/usque-launch.sh` | 四态：`h2`（`--http2 -s '' -P 443`）/ `h2v6`（多一个 `-6`）/ `-6`（QUIC over v6）/ 缺省（QUIC over v4）；需要 v6 源地址的两态拿不到源地址时降级到对应 v4 通路 |
| 9 个 `usque-warp*.service` | `ExecStart` 末参：1080-1084 → `h2`，1085-1088 → `h2v6` |
| `config.py` `warp.members` | 9 条换成新 tag，并加 `transport` / `entry` / `family` / `h2_endpoint` |
| `deploy/members.py` | 第 5 列按 `transport` 渲染（`h2` / `h2v6` / `-6` / `-`），回落 `ipv6` |
| `.warp/warp-outbound.json` | 用 `members.py --write-warp-outbound` 重新生成（旧 tag 会在这里留一份副本，必须同步） |
| 6 份 sing-box 配置 | 文本级精确替换 tag（每份 19 处：9 定义 + 9 selector 成员 + 1 default），先长后短避免 `WARP-CF` 吃掉 `WARP-CF-B` |
| `.warp_line_state.json` | `lines` 下 9 个 key 迁移到新名，保住 `ban_probes` 历史（否则报表显示「新线无样本」） |
| `ipv6_segments.py` / `tools/channel_report.py` | PORTS 映射与 tag→service 推导支持新名，旧名保留兼容 |
| `test_portability.py` / `test_daemon_unit.py` | 断言与测试数据换成新契约 |

sing-box 的 **inbound 端口、socks 类型、selector 名都没变**，所以 9router 侧无需改动。

## 四、探测与闸口验证

**探测**：9 条在 `daemon.log` 里各有 17–168 次提及，`.warp_line_state.json` 里都有
`last_verdict` 与 `ban_probes` 计数 —— 说明它们真的在探测循环里，不是只写在配置里。

**闸口**：逐条切换 `opencode-out`，全部成功：

```
WARP-CF4    PUT=204 now=WARP-CF4      WARP-CF6    PUT=204 now=WARP-CF6
WARP-CF4-B  PUT=204 now=WARP-CF4-B    WARP-CF6-B  PUT=204 now=WARP-CF6-B
WARP-CF4-C  PUT=204 now=WARP-CF4-C    WARP-CF6-C  PUT=204 now=WARP-CF6-C
WARP-CF4-D  PUT=204 now=WARP-CF4-D    WARP-CF6-D  PUT=204 now=WARP-CF6-D
WARP-CF4-E  PUT=204 now=WARP-CF4-E
```

（验证完已把 `now` 切回 daemon 原来的选择。）

## 五、容量实测：当前全池为 0

### 生产 9 条（00:19 逐条探测）

| 线 | 出口 | verdict |
|---|---|---|
| WARP-CF4 | `2a09:bac5:1efb:1173::1bd:b6` | quota（8.4h） |
| WARP-CF4-B | `2a09:bac5:1efb:2da5::48c:28` | quota（8.5h） |
| WARP-CF4-C | `2a09:bac5:1eff:3141::4e8:32` | quota（8.6h） |
| WARP-CF4-D | `2a09:bac5:1efb:78::c:3c5` | quota（7.9h）※首次探到 unreachable，复测正常 |
| WARP-CF4-E | `2a09:bac5:1efd:78::c:3c0` | quota（8.5h） |
| WARP-CF6 | `2a09:bac1:19a0:18::4d0:5a` | quota（8.3h） |
| WARP-CF6-B | `2a09:bac5:1f01:3050::4d0:53` | quota（8.1h） |
| WARP-CF6-C | `2a09:bac1:19c0:18::4cf:54` | quota（7.6h） |
| WARP-CF6-D | `2a09:bac1:1980:18::4:381` | quota（8.2h） |

### 新注册设备对照（关键）

用**全新注册**的设备（出口 IP 与生产 9 条不同）从零开始打：

| 入口 | 出口 IP | 结果 |
|---|---|---|
| v4-h2 | `2a09:bac5:1ef9:78::c:3c1` | **第 1 发就 quota**，Retry-After **28215s ≈ 7.8h** |
| v6-h2 | `2a09:bac1:19c0:18::1d2:b8` | **第 1 发就 quota**，Retry-After **27584s ≈ 7.7h** |

**新设备也拿到已打空的 IP** → WARP 的出口地址池是有限的，今晚约 3000 发把整池榨干了。
所以现在测不出单 IP 上限，与 h2 承载、入口族都无关。

Retry-After 7.6–8.6h 全部指向 **11-13 08:00 前后**，与滚动 24h 窗口模型吻合。

**已排一次性任务**：11-13 08:05 自动重跑 `zz_cf_cap.py v4h2` 与 `v6h2`，出容量数字。

## 六、踩到的两个坑（都已修）

**1. `KillMode=process` 让 sing-box 不随服务重启。**
`auto-switch-ip.service` 用 drop-in 设了 `KillMode=process`（避免重启时连带杀掉 sing-box
导致大批 403）。所以 `systemctl restart` 之后旧 sing-box 仍在跑旧配置 —— 改 tag 必须**先手动
清掉 6 个实例**，再由 daemon 的 watchdog 重新拉起。顺序：

```bash
sudo systemctl stop auto-switch-ip
# 按 /proc/<pid>/cmdline 精确匹配清理（不要用 pkill -f，会匹配到自己那条 bash）
sudo systemctl start auto-switch-ip      # watchdog 约 60s 后发现不通并拉起
```

**2. Clash API 的 `PUT /configs?force=true` 不会替换 outbound 的 tag。**
热重载返回 204、端口不中断，但 `/proxies` 里的 tag 名仍是旧的（实测两次）。它适合订阅刷新，
不适合改 tag 这类「outbound 结构变更」—— 必须重启进程。

**3. 顺带修掉一个隐患**：23:15 用 `nohup` 手工起的 daemon 占着 `.daemon.lock`，导致 systemd
版每 10 秒启动即退出、无限 auto-restart（日志刷满 `Found left-over process ... Ignoring`）。
已杀掉手工实例，现在由 systemd 正常接管。

## 七、回滚

```bash
cd ~/dkk-projects/auto-switch-ip
cp config.py.bak-20261112-235644 config.py
cp deploy/usque-launch.sh.bak-20261112-235644 deploy/usque-launch.sh
cp deploy/members.py.bak-20261112-235644 deploy/members.py
for d in usque usque2 usque3 usque4 usque5 usque6 usque7 usque8 usque9; do
  cp .warp/$d/config.json.bak-20261112-235644 .warp/$d/config.json
done
for f in singbox-config.json singbox-config.json-slot{2,3,4,5,6}.json; do
  cp "$f.bak-20261112-235644" "$f"      # 若当时未备份该文件，改用 git 或逐字改回
done
sudo cp /etc/systemd/system/usque-warp.service.bak-20261112-235644 /etc/systemd/system/usque-warp.service
# 其余 8 个 unit 同理
sudo systemctl daemon-reload
sudo systemctl restart auto-switch-ip usque-warp usque-warp-b usque-warp-c usque-warp-d \
     usque-warp-e usque-warp-f usque-warp-g usque-warp-h usque-warp-i
```

> 注意：`singbox-config.json*` 这次**没有单独备份**（改动是文本级精确替换，可用
> `WARP-CF4` → `WARP-CF` 反向替换复原）。

## 八、遗留

1. **单点承载**：9 条全押 TCP 443（v4 落在 `198.3-198.9`，v6 落在 `2606:4700:103::2`）。
   好处是 v4/v6 互为备份；坏处是同一批 CF 前端如果整段 TCP 被切，9 条一起废。
2. **空 SNI**（`-s ''`）是实测能通的选择，默认 SNI 在部分链路被 RST。
3. **`198.2` 已弃用**：9 份 config 都离开它了，但注册记录仍在，不必回切。
