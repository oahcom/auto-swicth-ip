# 加一条隧道：速查清单

> **本文回答什么问题**：加一条隧道要做哪几件事？**漏了哪一步会得到什么症状？**
>
> 完整原理见 [../architecture/provisioning.md](../architecture/provisioning.md)，
> 这里只留可勾选的清单和出错时的症状对照。

## 零、先选类型

| 想要 | 选 | 代价 |
|---|---|---|
| 不依赖机场、自己一条隧道 | **直连 usque**（`WARP-CF-*`） | 要一台 CF 设备；v6 路由必须就绪 |
| 经机场 TCP 中转 | **H2**（`WARP-H2-*`） | 要一台 CF 设备 + 一个机场节点 |
| 经机场 UDP 中转 | **WG**（`WARP-WG-*`） | 要一个机场节点；**必须走 detour**，直连被运营商 ACL 拦 |

**直连 WG 不通是物理约束，不是配置问题**（既定事实第 14 条：到
`162.159.192.1:2408` 的 UDP 被拦，实测收到 ICMP type 3 code 13）。

---

## 一、直连 usque（`WARP-CF-*`）

- [ ] **注册设备**：`deploy/install.sh` 会做，或手工 `usque register`
- [ ] **设备目录**：`.warp/usque<N>/config.json`（600）
- [ ] **符号链接**：`.warp/usqueN/usque` → `../usque/usque`（clone 后必须重建）
- [ ] **`config.py` 加成员**：
      ```python
      {"tag": "WARP-CF-X", "port": 1086, "service": "usque-warp-x.service",
       "dir": ".warp/usque7", "ipv6": True}
      ```
- [ ] **重生成单元**：`./deploy/install.sh --units-only`
- [ ] **加进 `warp-outbound.json`**：`python3 deploy/members.py --write-warp-outbound`
- [ ] **重生成主配置**：`python3 parse_subscription.py`
- [ ] **重启 sing-box 进程**（不是 reload，是重启）

**`ipv6: True` 是期望不是必须**：`deploy/usque-launch.sh` 在启动瞬间判
`ip -6 route get 2606:4700:103::2`，不通就自动降级走 v4。诊断看
`journalctl -u usque-warp-x | grep usque-launch`。

---

## 二、H2（`WARP-H2-*`）

- [ ] **设备**：一台独立 CF 设备（CF 侧同一设备只能一个会话）
- [ ] **加入 `.warp/h2/channels.json`**：
      ```json
      {"index": 19, "channel": "WARP-BLK-XXX-REG-0", "colo": "XXX",
       "node": "<机场节点>", "device": "usque25", "inbound_port": 22719}
      ```
- [ ] **`inbound_port` 不能撞**：现用 22700~22718
- [ ] **`colo` 必须是实测 trace 值**，不是机场订阅桶标签（既定事实第 6 条）
- [ ] `./deploy/h2-install.sh`（会渲染 → 校验 → 安装 → 启用）

**三个硬条件缺一不可**（既定事实第 13 条）：

1. 空 SNI（`-s ''`）
2. RFC 7540 普通 CONNECT，不是 extended CONNECT
3. 原始 TCP 中继到 CF 的 IP 字面量，不经过 SOCKS 中转

**`usque -P` 只改端口不改 host** —— 必须在派生配置里把 `endpoint_h2_v4`
改写成 `127.0.0.1`。

---

## 三、WG（`WARP-WG-*`）

- [ ] **改 `.env`**：`WARP_WG_FAMILIES=v4,v6,v6,v6`（每节点 1 条 v4 + 3 条 v6）
- [ ] **`WARP_WG_NODES=N`** = colo 数
- [ ] **`set -a; . ./.env; set +a`** ← **漏了会是 28 条而不是 50 条**
- [ ] `./deploy/wg-lines-install.sh`
- [ ] 重新生成主配置 + 重启 sing-box

### 最容易踩的三个坑

**坑 1：忘记 source `.env`**

`WARP_WG_FAMILIES` 为空 → `_wg_count = 0` → `members.py` 只写出 28 条
（实测踩过）。检查：

```bash
python3 -c "import config; print(len(config.CFG['warp']['members']))"   # 应为 50
```

**坑 2：用 sudo 跑导致属主变成 root**

`wg-lines-install.sh` 的默认 `ASIP_RUN_USER` 是 `$(id -un)`，在 sudo 下就是
root。于是 `apply` → `members.py` 以 root 写出 600 属主的 `warp-outbound.json`，
daemon（`User=<普通用户>`）随后读不到 → 生成**没有 WARP 出口**的合法配置 →
opencode 静默落到机场池。**2026-11-03 远端因此丢了 3 小时 24 分出口。**

必须显式传：

```bash
sudo env ASIP_RUN_USER=<用户> ASIP_RUN_GROUP=<用户> ./deploy/wg-lines-install.sh
```

**坑 3：v4 线以为换个节点就换了地址**

v4 地址由**落地 colo** 决定，同 colo 换节点拿回来的还是那批地址。只有
`rebind` 才换池（既定事实第 4 条）。

---

## 四、通用收尾（三类都要）

- [ ] **端口没撞**：`ss -lntp | grep :<端口>`
- [ ] **单元在跑**：`systemctl is-active <单元>`（用 `sudo systemctl`，别用 `--user`）
- [ ] **selector 成员数涨了**：
      ```bash
      S=$(cat ~/.singbox_secret)
      curl -s http://127.0.0.1:9090/proxies/opencode-out -H "Authorization: Bearer $S" \
        | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("all") or []))'
      ```
- [ ] **这条线真的能出流量**：
      ```bash
      curl -s --socks5-hostname 127.0.0.1:<端口> \
        https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(ip|warp)='
      ```
      **`warp=on` 才算成功**；`warp=off` 说明流量没走 WARP。

### 探测超时别用 8 秒

实测 8s 超时会把刚起来的线误判成 fail。用 20s：

```bash
curl -s -m 20 --socks5-hostname 127.0.0.1:<端口> https://www.cloudflare.com/cdn-cgi/trace
```

### `curl --resolve` 验证会骗你

用 `socks5h://` 时域名在远端解析，绕过了 `opencode.ai` 的路由规则，直接落到
`final: proxy` —— 会看到 `warp=off`。要验证路由，用 Clash API 的 `/connections`
看 `opencode.ai` 的 `chains`。

---

## 五、症状对照表

| 症状 | 漏了哪一步 |
|---|---|
| 隧道起着但 selector 里没有它 | 没重生成主配置 / 没重启 sing-box |
| 单元 active 但 daemon restart 打空 | `members[].service` 与单元名不一致 |
| 50 条只剩 28 条 | 没 source `.env`（`WARP_WG_FAMILIES` 为空） |
| `status=404` 刷屏 | `warp-outbound.json` 读不了（属主被 root 抢走） |
| 端口在听但 `warp=off` | 走的是机场池路径，没命中 WARP 出口 |
| 探测 fail 但实际能用 | 超时太短（8s → 20s） |
| `systemctl --user` 返回 0 但没反应 | 用错作用域，系统级单元必须 `sudo systemctl` |

---

## 相关文档

- [../architecture/provisioning.md](../architecture/provisioning.md) —— 每一步的完整原理
- [../architecture/tunnel-types.md](../architecture/tunnel-types.md) —— 四种类型的边界
- [../reference/ports-units.md](../reference/ports-units.md) —— 端口与命名规则
- [troubleshoot.md](troubleshoot.md) —— 出事时怎么查
