# 2026-11-02 9router 全站不可用：warp 测试残留把 WSL 出口打进黑洞

记录人：opencode
范围：`/var/lib/cloudflare-warp/settings.json`、WSL 策略路由、`auto-swicth-ip/daemon.py`
关联：`docs/warp-channel-allocation.md`（H2 路线终局）、提交 `a335a41`

## 一句话

昨晚为测 CF MASQUE H2 路线，把 warp 切成 `operation_mode=Warp` 并把
`override_warp_endpoint` 指向 `127.0.0.1:443`（本机无人监听）。warp 据此建
`loopback0`、写入 `ipproto tcp/udp lookup 127|128` 策略路由，下一跳是
**不存在的 `169.254.73.249`**。结果本机所有 TCP/UDP 出网（含 LAN 内）被强制
导向黑洞，9router 全部上游请求失败。跟 9router 自身代码无关。

## 现象

- `9router` 服务 `active`，`20127/20128` 正常监听，`/v1/models` 本机 curl 200。
- 但任何真实请求 63s 后 `503`，报
  `Bun transport unavailable (caller-aborted)` → `sidecar unavailable`。
- combo 上游报 `EHOSTUNREACH: connect EHOSTUNREACH 192.168.1.143:20127`。
- 用户侧观感：「9router 坏了一个晚上」「重启电脑也没用」。

## 关键证据

### 1. 出网全断，但 LAN 与 DNS 正常（这是最强指征）

```
$ ping -c 2 192.168.1.1          → 0% loss（网关通）
$ ping -4 -c 2 223.5.5.5         → 100% packet loss（公网不通）
$ getent hosts www.baidu.com     → 正常解析
$ curl -4 https://www.baidu.com  → 超时
```

`ping` 内网通 + `ping` 公网不通 + DNS 正常，排除了 DNS、网关、物理链路，
指向**策略路由按协议族劫持**：ICMP 不受影响，TCP/UDP 全灭。

### 2. 策略路由表指向不存在的下一跳

```
$ ip rule | grep -E "lookup 12[78]"
1:  from all ipproto tcp lookup 127
1:  from all ipproto udp lookup 127
1:  from all ipproto tcp lookup 128
1:  from all ipproto udp lookup 128

$ ip route show table 127
127.0.0.1 via 169.254.73.249 dev loopback0 proto kernel src 127.0.0.1 onlink

$ ip route show table 128
192.168.1.97 via 169.254.73.249 dev eth0 proto kernel src 192.168.1.97 onlink

$ ip -br link | grep loopback0
loopback0  UP  00:15:5d:ef:2a:b2 <BROADCAST,MULTICAST,UP,LOWER_UP>
```

`169.254.73.249` 是 warp 内部的 fake-dns/loopback 地址。本机没有设备应答它，
所有命中这两张表的包直接丢弃 → 内核回 `EHOSTUNREACH`。

注意 table 128 是**源地址匹配**（`192.168.1.97` = 本机 eth0），意味着连
`192.168.1.143:20127`（同网段另一台 9router2）也在劫持范围内——这解释了
为什么 LAN 内的 combo 上游会 `EHOSTUNREACH`，而 `ping 192.168.1.143` 却是通的。

### 3. 元凶配置（warp settings.json）

```json
"operation_mode": "Warp",                    ← 应为 WarpProxy
"override_warp_endpoint": "127.0.0.1:443",   ← 本机无人监听
"masque_settings": { "masque_http_version": "h2_only" }
```

warp 日志自证它一直在往 127.0.0.1:443 打 H2：

```
connect_single_protocol{protocol="H2"}: warp_edge::happy_eyeballs:
  Happy Eyeballs check failed v4=127.0.0.1:443
  err=Connect(Os { code: 110, kind: TimedOut })
ERROR warp_edge::fallback::single_protocol:
  MASQUE (HTTPS via TCP)(Non-PQ) failed err=HappyEyeballs(AllChecksTimedOut)
```

### 4. 时间线

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
| 11-02 13:39 | 端到端恢复，连续 3 次 200 |

「重启电脑也没用」的原因：warp-svc 是 enabled，开机即重建同一套路由。

## 修复

```bash
# 1) 停掉并使 warp 不再自启
sudo systemctl stop warp-svc
sudo systemctl disable warp-svc

# 2) 删策略路由规则（必须 sudo，普通用户删不动）
for p in tcp udp; do for t in 127 128; do sudo ip rule del ipproto $p lookup $t; done; done
sudo ip route flush table 127
sudo ip route flush table 128

# 3) 改回 Proxy 模式并清掉 endpoint 覆盖（先备份）
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

`loopback0` 网卡删不掉（`RTNETLINK answers: Operation not supported`，mirrored
模式下由 WSL 网络栈托管）。它无害：规则删掉后没人查那两张表。留着不影响。

## 验证

```
nine-router-src   active / enabled
auto-switch-ip    active / enabled
warp-svc          inactive / disabled

ping -4 223.5.5.5      → 0% loss
curl -4 baidu          → 200
经 7890 出海 opencode  → 200

POST /v1/chat/completions × 3 → 200 / 200 / 200，1.4–1.8s
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
3. **断网先看 `ip rule`，再看 `ip route`**。主路由表干净不代表没被劫持——
   策略路由优先级更高，`ip route` 默认只显示 main 表。
4. **`ping` 通不代表能连**。ICMP 不命中 `ipproto tcp/udp` 规则，最容易骗人。
   判定出网要用 `curl`/`ping -4 公网IP` 组合。
5. 测试用 warp 请开独立 netns（`ip netns exec`），别在主 netns 里切 `mode warp`。
