# 一条隧道从零建起来

本文只回答一个问题：**新增一条隧道，到底要做哪些步骤、每一步做什么、
漏了会在什么时候以什么形式炸。**

名词定义见 [glossary.md](../glossary.md)，四种隧道的协议差异见
[tunnel-types.md](tunnel-types.md)。

---

## 一、为什么这件事不能「一条命令搞定」

隧道不是「配置里加一行」。一条能跑 WARP 的线由**四样东西**组成，缺一样
都不报错、只在运行时表现为「不通」：

| 组成 | 存在哪 | 进 git 吗 |
|---|---|---|
| 设备凭据（WARP 身份） | `.warp/usque*/config.json` | **否** |
| 线路定义（端口、单元名） | `config.py` 的 `warp.members` | 是 |
| systemd 单元 | `/etc/systemd/system/*.service` | 否（生成物） |
| 主 sing-box 的 outbound | `singbox-config.json` | 否（生成物） |

**注册凭据不能进 git**：它是这台机器在 CF 侧的设备身份，泄漏等于别人能冒充
这台机器。所以每台新主机都要**自己注册一遍**——这是部署脚本里唯一无法省略
的联网步骤。

---

## 二、完整步骤

### 步骤 1：确认这个节点能承载 WARP

**不要靠延迟或 TCP 连通性判断。** 一个节点 TCP 通、延迟低，完全可能跑不了
WARP；反过来 `pq.aws48` 的 STUN 探测是 False，它承载的 WARP-WG-5 却一直在
正常出 v6 流量。

唯一可信的判据是 `warp_diag.py`——它真的建一条隧道、真的打一次 trace：

```bash
python3 warp_diag.py --job job.json --out result.jsonl --rounds 2 --family v6
```

判据是输出里的 `warp=on`，不是 `delay` 也不是 `TCP 通`。

> **历史教训**：曾经用 STUN(3478) 判断节点能不能跑 WARP。WARP 走的是
> **2408**，节点完全可以只放行 2408 而不放行 3478。缓存里 228 条有 192 条
> 被记成 False，`apply()` 据此把 22 条渠道砍到 8 条。

### 步骤 2：注册设备

```bash
cd .warp/usque7
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
    ../usque/usque register --accept-tos -n <tag>
```

三个必须注意的点：

**① 必须在设备目录里执行。** 二进制用绝对路径调用也行，但 `config.json`
会写到**当前工作目录**。cwd 不对时文件落到仓库根，下一轮又提示
`You already have a config. Do you want to overwrite it?`，看起来像注册失败，
实际是写错了地方（实测踩过）。

**② 必须直连。** 本机如果 export 了 `http_proxy`，usque 会走代理，CF 返回
非 JSON 错误页（`failed to parse error response`）。所以命令前面要
`env -u http_proxy -u https_proxy ...`。

**③ 一台设备一个身份，不能复制。** CF 侧同一 device 只能有一个会话，
两个进程用同一份 `config.json` 会互相踢下线。

设备目录名由端口推导：`1080 → usque`、`1081 → usque2` …… `1099 → usque20`。
H2 线用剩下的空闲设备（当前是 `usque3`、`usque7..24`）。

### 步骤 3：写进 `config.py` 的 members

```python
{"tag": "WARP-CF-J", "port": 1089, "service": "usque-warp-j.service", "ipv6": True},
```

这是**唯一的事实来源**：单元文件、注册目录、主 sing-box 的 outbound 全部
从它推导。改完跑：

```bash
./deploy/install.sh --units-only
```

> **不要手写单元文件。** 它们会把 `ExecStart` 钉成本次运行的项目路径；
> 在 `/tmp` 的副本里跑一次，现网 `auto-switch-ip.service` + `asi-selfcheck.*` +
> 全部 usque 单元就都被改指到副本，服务下次重启即崩
> （真实事故）。

### 步骤 4：生成主 sing-box 的 outbound

```bash
python3 deploy/members.py --write-warp-outbound
python3 parse_subscription.py
```

**漏了这一步的症状很隐蔽**：`.warp/warp-outbound.json` 不存在时，主配置里
没有 WARP 出口 → `opencode-out` selector 整个缺失 → daemon 所有换线都打空，
但日志只写「无新鲜备线」，看不出是配置没生成。

### 步骤 5：拉起并验收

```bash
sudo systemctl restart <unit>
```

验收**必须看 `warp=on`**：

```bash
curl -s --socks5-hostname 127.0.0.1:<port> \
     https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(ip|warp)='
```

只看到 `ip=` 不够——那可能是机场节点的出口，没走 WARP。

---

## 三、H2 线的额外步骤

H2 不走 `install.sh`，走自己的渲染器：

```bash
./deploy/h2-install.sh          # 渲染 → 校验 → 安装 → 启用
./deploy/h2-install.sh --render-only   # 只渲染，不装
```

渲染器产出 5 类文件到 `.warp/h2/`（全部 gitignored）：

| 文件 | 作用 |
|---|---|
| `singbox.json` | 1 个 sing-box：N 个 direct 入站 + N 个节点出站 + 改写规则 |
| `conf/N.json` | 每条线一份派生设备配置（`endpoint_h2_v4` → 127.0.0.1） |
| `usque/N.sh` | 每条线的启动命令 |
| `units/*.service` | 每条线一个单元（**不 enable**） |
| `channels.json` | 渠道 ↔ 端口 ↔ 设备 ↔ 节点的台账 |

**顺序不能换**：`parse_subscription.py` 必须先跑，否则主配置里没有
`dns-airport`，H2 出站解析机场域名会失败，`sing-box check` 直接报
`domain resolver not found: dns-airport`。

### 台账兜底

渲染器正常从 `.warp/block-map.json` 里筛 `carrier == "h2"` 的渠道。但
`tools/merge_h2_blockmap.py` 对**已存在**的节点只追加 `h2` 字段、不覆盖
`carrier`，所以同一份台账在不同时点重渲染可能筛出 0 条。

筛不到时回退到 `.warp/h2/channels.json` 里已投产的分配，且**优先用台账里的
`device`**——否则 `WARP-H2-<colo>-<i>` 成员会整体平移，daemon 按 tag 记账
全部错位。

---

## 四、WG 线的额外步骤

```bash
WARP_WG_FAMILIES=v6 WARP_WG_NODES=22 ./deploy/wg-lines-install.sh
```

WG 用 `wgcf` 注册身份（不是 usque），所以**不占 usque 设备槽位**。线路数 =
`len(WARP_WG_FAMILIES) × WARP_WG_NODES`。

`WARP_WG_FAMILIES` 的长度是「每个落地节点的渠道数」，`WARP_WG_NODES` 是
落地节点数。**v4 渠道数不应超过可用 colo 数**——v4 由落地 colo 决定，
同 colo 的两条 v4 线必然拿到同一批地址（实测本地三条 v4 全部落在 PHX、
拿到同一个 `104.28.205.115`）。

---

## 五、漏了会怎样：症状对照表

| 漏掉的步骤 | 症状 | 看起来像 |
|---|---|---|
| 没跑 `warp_diag.py` 就选节点 | 线起来了但 `warp=off` | 节点坏了 |
| 注册时 cwd 不对 | `config.json` 落在仓库根，重复提示要覆盖 | 注册失败 |
| 注册时没 `env -u http_proxy` | `failed to parse error response` | CF 拒绝 |
| 没写 members | 单元不存在，`asip-unit` 报「不在白名单」 | daemon 有 bug |
| 没跑 `--write-warp-outbound` | selector 缺 WARP 出口，换线全打空 | 无新鲜备线 |
| 没跑 `parse_subscription.py` | `dns-airport` 缺失，H2 起不来 | 节点 DNS 挂了 |
| 单元 disabled 但仍留在 members | `fail_streak` 涨到几十万，日志刷屏 | 线路故障 |
| 设备不够 | 渲染器只渲前 N 条，其余静默丢弃 | 拓扑配错了 |

最后一条值得展开：`render_h2_lines.py` 的 `free_devices()` 只统计
`systemctl is-active` 为 active 的 `usque-warp*.service` 占用的设备。
单元文件存在但 disabled 的历史单元**不算占用**——否则 20 台设备会显示
「空闲 0 台」，白白浪费。

---

## 六、一台全新主机的顺序

```bash
# 1. 代码
git clone <repo> && cd auto-swicth-ip

# 2. 二进制
./deploy/fetch-binaries.sh

# 3. .env（订阅 token、拓扑开关；见 .env.example）
cp .env.example .env && $EDITOR .env

# 4. 订阅 → 主配置
python3 parse_subscription.py

# 5. 直连 usque（注册 + 单元）
./deploy/install.sh

# 6. WG 线
./deploy/wg-lines-install.sh

# 7. H2 线（需要 block-map.json，来自实测）
./deploy/h2-install.sh

# 8. 实测表（可从别的机器复制，或本地重新 build-map）
python3 warp_blocks.py build-map <probe_dir>

# 9. 验收
python3 warp_blocks.py plan      # 看会开几条什么渠道
systemctl list-units 'usque-*' 'warp-wg-*'
```

**第 8 步容易被跳过**：`block-map.json` 是 gitignored 的实测表，全新 clone
里没有。缺它时 `channels()` 返回空，`plan` 直接报

```
块模型未启用（warp.blocks.enabled=false）
```

——而 `enabled` 其实**是 true**。这条报错来自 `channels()` 返回空这个事实，
不是配置。看到它先查 `.warp/block-map.json` 在不在，别去改 `.env`。

远端一键复刻用 `./deploy/remote-deploy.sh user@host`，它把上面这些顺序和
坑都固化进去了。

---

相关文档：[tunnel-types.md](tunnel-types.md) · [ip-pools.md](ip-pools.md) ·
[glossary.md](../glossary.md) · [warp-node-rotation.md](../warp-node-rotation.md) ·
[established-facts.md](../established-facts.md) 第 13、14、15 条
