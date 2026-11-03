# 排障树

> **本文回答什么问题**：出现某种症状时，**按什么顺序查**，每一步看到什么
> 才能排除或确认？

## 通用第一步：先确认你看的是**正在跑的**代码

2026-11-03 实测：`daemon.py` 改于 03:04，进程还是前一晚 22:28 起的那个 ——
新代码一行没跑，而日志看起来完全正常。

```bash
ls -l --time-style=+'%m-%d %H:%M' daemon.py
ps -eo lstart,cmd | grep daemon.py | grep -v grep
```

**进程启动时间必须晚于文件修改时间**。不是的话先重启：

```bash
sudo systemctl restart auto-switch-ip.service
```

同理，改了 `singbox-config.json` 不重启 sing-box 等于没改。

---

## 症状 1：opencode 全在报 429

> `Rate limit exceeded` / `FreeUsageLimitError`

429 是**按出口 IP** 判的配额，不是按账号。先分清两种情形：

### 步骤 1：同一时刻做对照实验

```bash
# 经当前出口
for i in $(seq 1 6); do
  curl -s -o /dev/null -w '%{http_code} ' --proxy http://127.0.0.1:7890 \
    https://api.opencode.ai/v1/models
done; echo
```

如果**两台机器**（本地/云端）同时打都是 429 → **不是部署问题，是池子干了**。
见既定事实第 24 条：实测同一时刻两边成功率完全相同，差异只在"这一分钟谁
撞到干净地址"的抽样噪声。

### 步骤 2：看新鲜备线数

```bash
grep '备线' daemon.log | tail -5
```

`_warp_line_fresh_candidates` 要求 `last_ok_ts` 在 `line_fresh_sec`(120s) 内。
深夜这个数经常是 **0** —— 于是换线只能反复空转（实测云端 68 次尝试成功 6 次）。
**这不是换线逻辑坏了，是没有合格候选。**

### 步骤 3：看时段

| 时段 | 池子状态 |
|---|---|
| 08:00~16:00 | 相对好 |
| 傍晚之后 | 开始恶化 |
| 深夜~凌晨 | 只有少数 IP 存活 |

**拿深夜数据论证"换 IP 无效"= 用枯水期论证河里没有水**（既定事实第 0 条）。

### 不要做的事

- 不要因为"换了还是 429"就删掉换 IP 机制
- 不要在此时调小 `cooldown_ok_sec` —— 冷却不是瓶颈，候选才是
- 不要改 `proactive_after_dsv4_ok`，它管的是"用完了再换"，不是"换得更快"

---

## 症状 2：selector 打空 / `status=404`

> 日志刷 `✗ 切换 WARP 线路失败 (WARP-XXX): status=404`

**404 意味着 `opencode-out` 这个 selector 根本不在运行中的配置里。**

### 步骤 1：确认 selector 真没了

```bash
S=$(cat ~/.singbox_secret)
curl -s http://127.0.0.1:9090/proxies/opencode-out -H "Authorization: Bearer $S"
```

404 → 确认。这一步**不能用**"7890 能不能连"代替 —— 那是两个不同的判据。

### 步骤 2：看配置里有没有 WARP 出口

```bash
python3 -c "
import json;c=json.load(open('singbox-config.json'))
print('socks 出口:', sum(1 for o in c['outbounds'] if o.get('type')=='socks'))
print('selectors:', [s['tag'] for s in c['outbounds'] if s.get('type')=='selector'])"
```

`socks: 0` → 配置就是坏的。**这是一份合法配置**（`sing-box check` 会通过），
所以探活发现不了（既定事实第 28 条）。

### 步骤 3：看 `warp-outbound.json` 能不能读

```bash
ls -l .warp/warp-outbound.json
sudo -u <daemon运行用户> cat .warp/warp-outbound.json > /dev/null && echo "可读"
```

属主是 root、600 → daemon 读不到 → 生成没有任何 WARP 的配置。
这是 2026-11-03 那次故障的根因。

### 步骤 4：等自愈

watchdog 每 30s 一轮，检测到就会重建：

```
⚠ 主配置里 opencode-out 缺失或成员异常，开始重建（第 1 次）
✓ warp-outbound.json 已重建，重新生成主配置
🔄 刷新订阅（WARP 出口自愈）...
⚠ 骤降护栏让路（WARP 出口自愈）：线上配置没有 opencode-out...
✓ 订阅刷新完毕 → 重启 sing-box
```

看到「仍缺失，等重建重试（Ns）」说明在退避（`60→120→240→480→600`）。
**连续多次都不成功时看下一行**：如果每次都报「重建命令成功，但运行中的配置
仍缺 WARP 出口」，问题在 sing-box 起不来，不是生成侧。

### 步骤 5：手动兜底

```bash
set -a; . ./.env; set +a
python3 deploy/members.py --write-warp-outbound
python3 parse_subscription.py
sudo systemctl restart auto-switch-ip.service
```

**注意**：手工跑 `parse_subscription.py` 会**绕过骤降护栏**。只在确认
池子没问题时这么做。

---

## 症状 3：某条线一直不通

### 步骤 1：分清"隧道不通"和"地址被封"

```bash
python3 -c "
import daemon; print(daemon._warp_line_state('WARP-WG-13'))"
```

| 看到 | 含义 |
|---|---|
| `ip` 有值、`last_verdict: quota` | **隧道正常**，地址被封。换线/重启换地址 |
| `ip` 为空、`fail_streak` 在涨 | 隧道**没拿到出口**。这是真故障 |
| `last_ok_ts: 0` 但 `ip` 有值 | 拿到过地址但从没成功服务过 |

**不要拿 `last_ok_ts` 判断隧道通不通** —— 它在 `blocked/quota` 时被清零。

### 步骤 2：看是不是落地节点死了

```bash
python3 -c "
import warp_wg
for t,n in warp_wg.line_nodes().items(): print(t,n)" | grep WARP-WG-13
```

节点不在当前订阅里 → `_rehome_one` 返回 `gone`，同块轮回会换节点。

### 步骤 3：确认节点真能承载 WARP

**只信 `warp_diag.py`**（既定事实第 8/10 条）：

```bash
python3 warp_diag.py --job job.json --out result.jsonl --rounds 2 --family v6
```

- STUN 不通 **不代表** WARP 不通（`pq.aws48` STUN=False 但正常出流量）
- 节点 TCP 通 **不代表** 这条线能用（机场节点存在"收 TCP 不转发 UDP:2408"）

### 步骤 4：看是不是被锁挡住了

```bash
grep '渠道锁定中\|夜间锁\|额度隔离' daemon.log | tail
```

四把锁会拦重启。**刚换过节点的线必须让路**（`rebound=True`）—— 若日志显示
锁在拦一条刚换过节点的线，那是 bug 不是设计。

---

## 症状 4：单元显示 active 但探不到

### 权限 / 作用域

```bash
systemctl status usque-h2-7.service     # 系统级
systemctl --user status usque-h2-7.service   # ← 错的，会返回 0 什么也不做
```

### 成员声明 vs 实际在跑

`config.py` 的 `members` 是**声明**，不等于单元在跑：

```bash
systemctl is-active usque-warp-c.service   # inactive
grep 'WARP-CF-C' config.py                 # 仍在 members 里
```

prober 会用 `systemctl is-active` 过滤（带 60s 缓存），但 `fail_streak` 仍可能涨
（既定事实第 26 条：实测涨到 22 万）。

**想让一条线彻底退出调度**：光 disable 不够，还要从 `members` 删掉。

---

## 症状 5：9router 挂了但 daemon 说它健康

**先确认 `NINE_ROUTER_BASE` 指对了**：

```bash
pid=$(systemctl show auto-switch-ip.service -p MainPID --value)
tr '\0' '\n' < /proc/$pid/environ | grep NINE_ROUTER_BASE
```

然后确认那个端口上真的是 9router：

```bash
ps -p $(ss -lntp | grep ':PORT' | grep -oP 'pid=\K[0-9]+') -o cmd
```

`relay-guard/guard.mjs` → **指错了**。见 [ports-units.md](../reference/ports-units.md) §4。

改完必须重启 daemon（`EnvironmentFile` 只在启动时读）。

---

## 症状 6：两台机器行为不一致

**先怀疑池子，不要先怀疑部署**（既定事实第 24 条、第 29 条）。

| 差异 | 常见真因 |
|---|---|
| 429 率不同 | 各自从共享池抽到不同样本 |
| `proxy` 池成员数差几倍 | `.dormant_state.json` 各自独立生成（本地 97 条判死 / 远端无此文件） |
| 换线频率不同 | 新鲜备线数不同 |

做对照实验：同一时刻、同一端口、同一批探针。两边一致 → 不是部署问题。

---

## 症状 7：日志里没有线索

2026-11-03 的教训：决定性的一行是 `WARP 出口配置存在但读取失败`，
而 daemon 只转发 `⚠` 开头的 stderr 行 —— **它被静默吞了 3.5 小时**。

现在已改为全量转发（正常一轮 stderr 只有 4 行以内）。若你看的版本还是只转 `⚠`：

```bash
grep -n 'for ln in' daemon.py        # 确认是全量转发
```

**排查任何"没有日志"的问题时，先确认日志过滤器本身不是凶手。**

---

## 相关文档

- [../established-facts.md](../established-facts.md) —— 按症状查"这是不是设计"
- [add-tunnel.md](add-tunnel.md) —— 加隧道时的已知坑
- [../reference/ports-units.md](../reference/ports-units.md) —— 端口/单元对照
- [../subsystems/prober.md](../subsystems/prober.md) —— verdict 五档的含义
