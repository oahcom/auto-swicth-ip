# 四种隧道类型

本文只回答一个问题：**我们有四种把流量送到 WARP 的路径，它们分别是什么协议、
经过谁的机器、能拿到什么样的出口 —— 以及为什么需要四种而不是一种。**

配额与封禁的判定口径见 [established-facts.md](../established-facts.md)，
名词定义见 [glossary.md](../glossary.md)。

---

## 一、总表

| 类型 | 协议 | 本机 → 中转 | 中转 → Cloudflare | 单元 | 代码 |
|---|---|---|---|---|---|
| **直连 usque** | MASQUE over QUIC | 无中转，直连 | UDP/443 | `usque-warp*.service` | `deploy/usque-launch.sh` |
| **H2** | MASQUE over HTTP/2 | 机场节点（TCP） | TCP/443（裸 TCP 经节点） | `usque-h2-*.service` | `deploy/render_h2_lines.py` |
| **WG** | WireGuard over UDP | 机场节点（UDP 转发） | UDP/2408 | `warp-wg-*.service` | `warp_wg.py` |
| **proxy 池** | 任意机场协议 | 机场节点 | 不经 CF | 无（主 sing-box 内） | `parse_subscription.py` |

四条路径的**出口完全不同**，混用会让「换 IP」这个动作失效——这是下面每一节
都要强调的事。

---

## 二、直连 usque（MASQUE over QUIC）

### 它做什么

本机直接向 Cloudflare 的 MASQUE 端点 `162.159.198.2:443` 建 QUIC 连接，
在里面跑 IP-over-HTTP/3。usque 进程在本机开一个 SOCKS 端口，主 sing-box 把
它当一个普通 socks outbound 用。

### 为什么它是主力

**它不借机场的机器。** 机场封不封 WARP 网段、节点承不承载 UDP，都与它无关——
只要本机能到 CF 的 443 就行。实测本机到 `162.159.198.2:443` 直连可用。

### 代价：出口数量 = 设备数

每个 usque 进程一份**独立注册的设备凭据**（`.warp/usque*/config.json`）。
CF 侧同一设备只能有一个会话，所以：

> **N 条直连线 = N 个注册设备 = N 个出口。** 不能靠多开进程扩容。

这也是为什么设备注册要在部署时逐台做（见
[provisioning.md](provisioning.md)），且每台设备的 `config.json` 是
**机器本地**的、不进 git。

### 换 IP 的方式

重启进程即可。v6 每次重连换一个新的 /128（连 /64 都换）；v4 由落地 colo
决定，同 colo 重启拿回同一批地址，**要换 v4 必须换 colo**。

---

## 三、H2（MASQUE over HTTP/2）

### 它做什么

```
usque (--http2) → 127.0.0.1:227xx (sing-box direct 入站)
                → 改写目标为 162.159.198.2:443
                → 经机场节点出去（裸 TCP）
                → Cloudflare 的 MASQUE 端点
```

关键点：**中间的 sing-box 只做「改写目标 + 经节点转发」，不理解 MASQUE。**
它收到的是原始 TCP 字节流，改写目标地址后原样塞给机场节点。

### 为什么需要它

有些机场节点**能承载 WARP 但走不了 WG**：UDP 被拦，TCP 却通。H2 把 MASQUE
套进 TCP，正好绕过这个限制。

实测同一批节点里，43 个节点的 `carrier` 被标成 `h2`——它们只能走这条路
（见 `.warp/block-map.json`）。

### 三个硬条件（少一个就不通）

1. **空 SNI**：`usque -s ''`。带 SNI 时 CF 会拒。
2. **RFC 7540 普通 CONNECT**，不是 extended CONNECT。
3. **裸 TCP 中继**：sing-box 的 `direct` 入站规则必须显式写
   `"action": "route"`。sing-box 1.13 里缺省 action 的规则被当作 **reject**，
   连接建立后立刻关，日志里只有一行 inbound connection，看起来像节点断流，
   实际是本机把连接拒了（实测踩到）。

### 它和 WG 的分工

`deploy/render_h2_lines.py` 与 `deploy/wg-lines-install.sh` 各管一半渠道：
`carrier == "h2"` 的节点归 H2，其余归 WG。同一渠道不会被两套单元同时占用。

---

## 四、WG（WireGuard over UDP，经代理节点中转）

### 它做什么

本机跑原生 WireGuard（sing-box 的 `endpoints`），但 **endpoint 带 `detour`**，
由机场节点代为发出 UDP 到 `162.159.192.1:2408`。

### 为什么必须经代理

本机到 `162.159.192.1:2408` 的 UDP 被运营商 ACL 拦（ICMP type 3 code 13），
直连握手只会超时。所以 UDP 必须借道机场。

### 为什么每条线一个进程

WireGuard 的 endpoint 带 `detour` 时，**每个 endpoint 一条独立会话**。要让
N 条线各自拿到独立出口，就得 N 个进程、N 个 `wgcf` 注册身份。

> 把 endpoint 塞进主 sing-box 只能得到一条会话——所有线共用同一个出口，
> 「换 IP」就退化成「换同一个 IP」。

### 换 IP 的方式

重启进程换 v6；v4 换 colo。与直连 usque 相同的规则。

---

## 五、proxy 池（兜底）

### 它做什么

直接走机场节点出网，**不经过 Cloudflare**。出口是机场自己的 IP。

### 为什么它是最后兜底

它的出口不是 WARP 地址，所以：

- 不消耗 WARP 设备槽位（可以无限加节点）
- 但也没有 WARP 的「重连换 IP」能力——只能换节点，而节点 IP 是机场固定的

daemon 只在 WARP 侧全部不可用时才把流量落在这里（`reconnect_warp` 的落池
分支），且落池有 3 小时冷却。**池不该是「地址全被标记」的直接结果**——
这一点在 `established-facts.md` 里有专门记录。

---

## 六、四条路径怎么选

`opencode-out` 是一个 sing-box selector，成员是全部 WARP 线路（当前 50 条）
外加 `proxy` 兜底（共 51 个）。daemon 通过 Clash API 切换它，切一次是毫秒级、
不断在途连接。

| 场景 | 走哪条 |
|---|---|
| 默认 | 任意一条 `last_ok_ts` 新鲜的 WARP 线 |
| 节点 UDP 被拦 | H2 |
| 节点 TCP 被拦但 UDP 通 | WG |
| 全部 WARP 线都不可用 | proxy 池（3h 冷却） |

---

## 七、常见误解

**「四种是冗余，留一种就行」**
不是。它们的**限制条件互不重叠**：直连受设备数限制、H2 受节点 TCP 限制、
WG 受节点 UDP 限制、proxy 池没有 WARP 出口。砍掉任何一种，对应的那批节点
就变成死资产。实测 94 个节点里，43 个只能走 H2。

**「H2 和 WG 都是经机场，可以合并」**
协议不同。WG 是 UDP、H2 是 TCP，一个节点的两个方向可以只通一个。合并等于
把「这个节点能承载 WARP」降级成「这个节点 TCP 通」。

**「proxy 池出口也能算 WARP 出口」**
不能。它不经过 CF，`warp=off`，出口是机场的地址。探测时
`probe_egress(require_warp=True)` 会直接判它不合格。

---

相关文档：[ip-pools.md](ip-pools.md) · [provisioning.md](provisioning.md) ·
[glossary.md](../glossary.md) · [warp-wg-over-proxy.md](../warp-wg-over-proxy.md) ·
[established-facts.md](../established-facts.md) 第 13、14 条
