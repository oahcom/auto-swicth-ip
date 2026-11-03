# 端口与单元参考

> **本文回答什么问题**：什么占哪个端口？单元怎么命名？看到一个
> `usque-h2-7` / `WARP-WG-13` / `:1187`，怎么一眼认出它是哪条线？

## 一、端口总表

全部监听在 `127.0.0.1`（局域网入站除外）。

| 区间 | 数量 | 谁在听 | 对应 |
|---|---|---|---|
| `1080`, `1081`, `1083`~`1085` | 5 | `usque` | 直连 WARP（`WARP-CF` / `-B` / `-D` / `-E` / `-F`） |
| **`1110`~`1131`** | **22** | `sing-box`（每线一个进程） | WG 线 `WARP-WG-1`~`22` |
| **`1180`~`1198`** | **19** | `usque` | H2 线 `WARP-H2-<COLO>-N` |
| `22700`~`22718` | 19 | `sing-box`（`asip-h2-singbox`） | H2 的内部入站（不对外） |
| `7890` | 1 | 主 `sing-box` | mixed 环回入站（9router / daemon / 本机 curl） |
| `7891` | 1 | 主 `sing-box` | mixed 局域网入站（需认证，`lan_port=0` 可关） |
| `9090` | 1 | 主 `sing-box` | Clash API（**Bearer 认证**，密钥在 `~/.singbox_secret`） |
| `20127` | 1 | 9router | 客户端入口（本机） |
| `38731` | 1 | 9router | 客户端入口（**远端**，见下） |
| `20128` | 1 | **relay-guard** | ⚠️ 不是 9router（见下） |

### `1082` 与 `1086`~`1099` 为什么空着

`1082` 是 `WARP-CF-C` 的位置 —— 该单元 2026-10-28 缩容时停用，成员仍留在
`config.py` 的 `members` 里（既定事实第 26 条）。`1086`~`1099` 是预留槽位。

**推论**：不要按"端口连续"假设线路编号连续。看 `config.py` 的 `members`。

### 端口是**推导**出来的，不是手写的

```
WARP-WG-N   →  端口 = base_port + N - 1  =  1110 + N - 1
             →  单元 = warp-wg-N.service
WARP-H2-*   →  端口由 .warp/h2/channels.json 的 inbound_port 决定（22700 起）
```

`deploy/members.py` 把 `CFG["warp"]["members"]` 渲染成
`tag port service dir family` 五行，供 `install.sh` 用。
**改 `members` 后必须重跑**，否则 daemon 的 `systemctl restart` 会打空。

### ⚠️ `members` 的长度取决于是否 source 了 `.env`

`CFG["warp"]["members"]` 的 WG 部分由 `_append_wg_members()` 在导入时追加，
条件 `wg.enabled` 即 `_wg_count > 0`，而 `_wg_count` 来自 `WARP_WG_FAMILIES`：

```bash
python3 -c "import config; print(len(config.CFG['warp']['members']))"
#   28   ← 没 source .env：只有 5 直连 + 19 H2 + 4 停用
set -a; . ./.env; set +a
python3 -c "import config; print(len(config.CFG['warp']['members']))"
#   50   ← 22 条 WG 进来了
```

**这不是 bug，是 `.env` 没加载。** 2026-11-03 实测踩过：直接跑
`members.py` 只写出 28 条，因为 `WARP_WG_FAMILIES` 为空。所有生成类命令
（`members.py` / `wg-lines-install.sh` / `warp_blocks.py plan`）都必须先
`set -a; . ./.env; set +a`。

## 二、单元命名规则

| 前缀 | 数量 | 由谁生成 | 说明 |
|---|---|---|---|
| `auto-switch-ip.service` | 1 | `deploy/install.sh` | daemon 本体，`KillMode=process` |
| `asi-selfcheck.{service,timer}` | 2 | `deploy/install.sh` | 每 10min 跑巡检引擎 |
| `usque-warp*.service` | 5 | `deploy/install.sh` | 直连 WARP 线 |
| `usque-h2-*.service` | 19 | `deploy/h2-install.sh` | H2 线 |
| `warp-wg-*.service` | 22 | `deploy/wg-lines-install.sh` | WG 线 |
| `asip-h2-singbox.service` | 1 | `deploy/h2-install.sh` | H2 的共享 sing-box |
| `nine-router-src.service` | 1 | `deploy/install-9router.sh` | 9router |
| `dormant-probe.{service,timer}` | 2 | 单独安装 | 沉睡池探测 |

### 三个硬规则

1. **单元名必须与 `config.py` 的 `members[].service` 一致**。不一致 → daemon
   的 `systemctl restart` 返回 0 而什么也没做（最坏的那种失败）。

2. **系统级，禁止 `systemctl --user`**。单元在 `/etc/systemd/system/`。对系统级
   单元跑 `--user` **会返回 0 而什么也不做** —— `asip warp stop` 报成功、隧道
   照跑。所有状态/重启命令一律 `sudo systemctl`。

3. **`auto-switch-ip.service` 必须保持 `KillMode=process`**。默认的
   `control-group` 会让每次 `systemctl restart` 连带杀掉 sing-box，daemon 随即
   置 bypass 直连，opencode 看到本地 IP 直接返回 abuse 403（实测 15:31/15:36/
   15:50 三次重启各触发 50~100 条 403）。

## 三、tag 命名

| tag | 例 | 含义 |
|---|---|---|
| `WARP-CF` / `WARP-CF-B` … | `WARP-CF-D` | 直连 WARP（usque 隧道，不经机场） |
| `WARP-H2-<COLO>-<N>` | `WARP-H2-NRT-0` | MASQUE over HTTP/2，经机场 TCP |
| `WARP-WG-<N>` | `WARP-WG-13` | WireGuard over UDP，经机场节点 detour |
| `WARP-BLK-<COLO>-<TYPE>-<N>` | `WARP-BLK-NRT-REG-0` | 渠道模型里的渠道名（`warp_blocks`） |
| `<节点域名>:<端口>-<协议>` | `cn.bing.com:443-any` | 机场节点（订阅原样） |

`WARP-H2-<COLO>` 里的 COLO 是**实测 trace 值**，不是机场的订阅桶标签 ——
桶叫 CFB 落地可能是 SJC（既定事实第 6 条）。

## 四、⚠️ `20128` 是 relay-guard，不是 9router

**这是最容易踩的坑**，因为它长得完全像 9router：

```
$ curl -o /dev/null -w '%{http_code}' http://127.0.0.1:20128/dashboard
307                                  ← 看起来就是个活着的 web 服务
$ ps -p <pid> -o cmd
/usr/bin/node /home/<user>/relay-guard/guard.mjs     ← 但它不是 9router
```

9router 的端口**不是常量**：

| 部署 | 单元里怎么写的 |
|---|---|
| 本机 | `ExecStart=... custom-server.js --port 20127` |
| 远端 | `Environment=PORT=38731`（ExecStart 不带 `--port`） |

所以 `NINE_ROUTER_BASE` **必须从单元声明取，不能按"哪个端口有人应答"探测**。
2026-11-03 实测：旧版 `remote-deploy.sh` 按 20127/20128 探测，把 20128 写进
`.env`，于是 `check_9router_health()` 永远对着 relay-guard 返回健康 ——
**9router 真死了这层检查也不会说话**。

取端口的正确姿势（两种写法取并集）：

```bash
U=nine-router-src.service
a=$(systemctl show $U -p ExecStart  --value | grep -o -- "--port [0-9]*" | grep -o "[0-9]*")
b=$(systemctl show $U -p Environment --value | tr ' ' '\n' | sed -n 's/^PORT=//p')
echo "${a:-$b}"
```

改完 `.env` **必须重启 daemon**：`EnvironmentFile=` 只在进程启动时读一次。

## 五、`asip` CLI 怎么知道作用域

`deploy/asip-cli.sh` 里没有一处路径、端口或单元名，启动时 source
`deploy/asip-cli-conf.sh` 生成的 `~/.config/asip/cli.env`。

后者在生成时**探测** `/etc/systemd/system/auto-switch-ip.service` 是否存在，
据此写 `ASIP_SYSTEMD_SCOPE`；CLI 再据此选 `systemctl` / `sudo -n systemctl`。
`ASIP_SYSTEM_UNIT_DIR` 可覆盖探测目标（测试用）。

**为什么不再手抄**：旧版把 10 条隧道的端口/单元名写死在脚本里，而 `members`
实际是 20 条，`_warp_members()` 的 sed 又按双引号格式解析（config 用单引号）
于是永远回落到写死那份。症状是 `warp stop` 只停了 10 条、另 10 条照跑，
**看起来像屏蔽失效**。

## 六、改了拓扑后必须做的三件事

新增隧道时**缺一步就是"隧道起着但用不上"**：

1. 建 systemd 单元并起进程
2. `.warp/warp-outbound.json` 加条目（`make_config` 据此并入 sing-box）
3. 重新生成 `singbox-config.json` **并重启 sing-box 进程**（改文件不重载无效）

第三条最容易漏 —— `parse_subscription` 写完文件，进程还跑着旧配置。

---

## 相关文档

- [config.md](config.md) —— 端口/命名相关的配置项
- [state-files.md](state-files.md) —— 每个单元读写哪些状态文件
- [../architecture/provisioning.md](../architecture/provisioning.md) —— 加隧道的完整流程
- [../established-facts.md](../established-facts.md) —— 第 15/16/17/26 条
