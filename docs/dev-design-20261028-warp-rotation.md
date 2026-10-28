# 开发设计书：WARP 轮换体系改造（2026-10-28）

- 项目：`auto-switch-ip`
- 时间：2026-10-28 04:20 ~ 05:10
- 参与：主人（决策）、Claude Code（实施）
- 关联文档：`docs/warp-wg-over-proxy.md`（WG 落地拓扑）、`INCIDENTS.md` INC-012

---

## 1. 目的与范围

本次改造起因是主人连问三个"为什么"，从质疑换线限制开始，逐步把整套探测/重启节流拆掉，
并把 CF（usque）线与 WG 线的部署层级、数量、切换判据重新定调。范围包括：

| # | 需求原话 | 落点 |
|---|---------|------|
| 1 | "为什么换线的限制被取消了，坏的线路也被换" | 回退此前为让 WG 进池而加的三处放宽 |
| 2 | "为什么会是用户级？跟整体的适配啊。改成系统级的" | 16 条 WG 线从用户总线迁到系统总线 |
| 3 | "不允许60一轮啊。探测设限制的都是蠢材" | 探测 cadence / 每分钟上限归零 |
| 4 | "设置为12吧，另外这些参数的意思都告诉我" | 重启并发 4 → 12 + 参数注释 |
| 5 | "本小时重启已达上限，这个限制去掉" | 每线每小时重启上限 20 → 0（不限） |
| 6 | "既然上面的退避基本没生效，就给我好好的删掉吧" | 删除重启退避曲线 |
| 7 | "CF等20条线路，可以减少成3条。一条v4和两条v6" | usque 20 → 3 |
| 8 | "把主动切换的模式改回requests，150条" | `proactive_mode` fixed → requests |
| 9 | "重启 warp-wg-1.service 失败…依然有报错的重启" | 单元层加固（start-limit / 端口残留） |

---

## 2. 变更明细（按提出顺序）

### 2.1 回退"放宽换线质量门"（daemon.py、systemd unit）

**背景**：WG 线刚接入时因探测被上游统一拒绝（`429 FreeUsageLimitError`）而拿不到 `last_ok_ts`，
永远进不了严格候选池；我为此加了"关掉模型验收按地址新鲜度选线"和"没有已验证备线就用从未出场的
地址兜底"两条旁路，并把 systemd 单元的 `ASIP_REFRESH_MODEL_CHECK` 置 0。结果出现了主人看到的
"坏的线路也被换"。

**改动**（全部删除，恢复原契约）：

| 位置 | 内容 |
|------|------|
| `daemon.py` `_warp_line_fresh_candidates` | 删除 `refresh_model_check=False` 时只按地址新鲜度入围的分支 |
| `daemon.py` `_reconnect_warp_by_table` | 删除 `fallback_tags` 兜底分支 |
| `daemon.py` `_warp_line_unused_candidates` | 整个函数删除 |
| `/etc/systemd/system/auto-switch-ip.service` | 删除 `Environment=ASIP_REFRESH_MODEL_CHECK=0` |
| `config.py` | 保留 `refresh_model_check` 的 env 钩子（默认 True），便于回滚调试 |

**原因**：`last_ok_ts` 是"这条线真的能服务"的唯一证据。绕开它等于把未验证地址当备线，
换线后失败率上升，是拿"看起来能切"换"切了就坏"。

**验证**：`grep -c ASIP_REFRESH /etc/systemd/system/auto-switch-ip.service` → `0`；
`python3 -m pytest -q test_warp_prober.py` → 15 passed（回退时点）。

**后续纠正**：此后实测证明"换 IP 确实能解决"，我此前"账号级额度、换 IP 无效"的结论是片面判断
（只覆盖了当时池内的坏地址）。这条认知已写回 `docs/warp-wg-over-proxy.md` 与 INC-012。

---

### 2.2 WG 线从用户级单元迁到系统级单元

**背景**：16 条 `warp-wg-*.service` 当初装在 `~/.config/systemd/user/`，而 daemon 自己是
`/etc/systemd/system/auto-switch-ip.service`（`User=administrator`）。daemon 走系统总线去
`systemctl restart warp-wg-N` 必然撞 polkit 交互认证 → 日志里留下 81 次
`WARP-WG-N 重启后未就绪（出口处于惩罚期）`。这与同项目 `usque-warp.service` 的部署方式也不一致。

**改动**：

1. 生成 16 个系统单元 `/etc/systemd/system/warp-wg-{1..16}.service`
   - `User=administrator` / `Group=administrator` / `Environment=HOME=/home/administrator`
   - `WorkingDirectory=<proj>/.warp/wglines/lineN`，`ExecStart=<proj>/sing-box run -c .../singbox.json`
   - `Restart=always`、`LimitNOFILE=65535`、`WantedBy=multi-user.target`
2. polkit 放行：`/etc/polkit-1/rules.d/49-asip-usque.rules` 增加
   `unit.indexOf("warp-wg-") === 0 → Result.YES`（与 usque-warp 同一条规则）
3. `daemon.py` `_systemctl_scope()` 删除 `warp-wg-* → systemctl --user` 的特殊分支，
   恢复"同一条总线、不按单元名分流"，并保留坑位注释（`daemon.py:2240` 附近）
4. `deploy/wg-lines-install.sh`：`UNIT_DIR` 默认 `/etc/systemd/system`，
   单元写 `User=$(id -un)` / `Environment=HOME`，启用时按 `id -u` 自动加 `sudo`

**原因**：daemon 不能跨总线管理单元；统一到系统级后，polkit 规则、部署脚本、运维命令三者一致。

**验证**：`systemctl is-active warp-wg-{1..16}` 全 active；日志出现
`✓ WARP-WG-16 重启后出口=...`、`✓ WARP-WG-2 重启后出口=...`；
以 daemon 身份模拟重启 `sudo -u administrator env -i ... systemctl restart warp-wg-3` → OK。

---

### 2.3 探测调度：取消"60 秒一轮"与每分钟上限

**背景**：实测吞吐峰值 36 条结论/分钟，正好等于 36 条线 × `line_probe_cadence_sec=60` 的数学上限，
而单条探测只要 1.9s（WG v4）~ 5.7s（CF）。加渠道本是为提速，却用"渠道数 × 60s"算轮次，等于白加。
主人的判断：给探测设节流本身就是错的。

**改动**（`config.py`）：

| 参数 | 旧值 | 新值 | 含义 |
|------|------|------|------|
| `line_probe_cadence_sec` | 60 | **0** | 同一线两次探测最小间隔；0 = 探完立刻重排队 |
| `probe_max_per_min` | 600 | **0** | 全局每分钟探测上限；0 = 不限 |

**原因**：探测是观测行为，没有副作用；真正的成本闸在"判脏地址入口短路"和"重启"，
不该在观测层再压一次。并发（`probe_concurrency=32` / `model_probe_concurrency=32`）
成为唯一物理上限。

**副作用修复**（`daemon.py:2775` 附近）：`unknown` 结论（连出口 IP 都没拿到）原先用 cadence 当
重试间隔，cadence 归零后会变成"探不出结果立刻再撞"的死循环。改为单独走
`_line_unknown_retry()`（`probe_unknown_retry_sec=5`），只作用于这一条线。

**验证**：改后近 3 分钟 52.3 条结论/分钟（改前理论上限 36），相邻结论间隔中位 **0.00s**。

---

### 2.4 重启并发 4 → 12

**背景**：探测不再是瓶颈后，真正的瓶颈暴露为重启：每条 quota 线都要
"重启换地址 → 等就绪 → 重探"，4 个槽 × 一次 3~5s ≈ 20~40 条/分钟。

**改动**：`config.py` `restart_concurrency: 4 → 12`。

**原因**：12 是折中值 —— 16 条线同时注册会撞 Cloudflare 约 9 台/窗口的注册限流，吃 429 后
按 30/90/240s 退避，反而比排队更慢，所以留 4 个空槽。

---

### 2.5 删除"每线每小时重启上限"

**背景**：日志频繁出现 `本小时重启已达上限，跳过重启`。撞上限后线路带着确定不能用的地址
继续跑，比多重启几次更糟。

**改动**：

- `config.py` `restart_max_per_hour_per_line: 20 → 0`
- `daemon.py:2907` `_warp_line_restart_allowed()` 增加 `cap <= 0 → return True`

**原因**：这个上限压的是"换掉坏地址"这个必要动作，不是观测；重启真正的代价已由
并发槽位与（当时的）退避承担。

**验证**：60s 窗口 `撞上限日志 0` 条，结论 93 条/分钟、重启 168 次/分钟。

---

### 2.6 删除重启退避曲线

**背景**：`restart_backoff_sec=[2,5,10,20,60]` 按 `fail_streak` 查表，看似在控频。
实测发现它从未生效：

```
重启成功 → next_restart_ts = 0 → cadence=0 立刻重探 → quota → 立刻再重启
```

状态表里 `fail_streak` 已涨到 6586，而重启间隔仍是最低档；60s 窗口内 168 次重启。
另外 `restart_backoff_max_sec=120` 是死配置（序列最长 60s，`min(60,120)=60`）。

**改动**：

- `daemon.py` 删除 `_warp_line_restart_backoff()` 函数
- 两处调用点（重启后未就绪、重启异常）改为固定冷却
  `time.time() + restart_cooldown_sec(5)`
- `config.py` 删除 `restart_backoff_sec`、`restart_backoff_max_sec`，注释记录删除原因
- `_warp_line_restart_allowed()` 的注释同步（"唯一的间隔来源是固定冷却"）

**原因**：一个不生效的曲线比没有更糟 —— 它让人误以为有刹车。保留 `restart_cooldown_sec=5`
只是"别原地空转"，不是惩罚。

**验证**：`pytest test_warp_prober.py test_warp_refresh.py test_auto_switch.py` → **98 passed**；
`grep -rn restart_backoff --include=*.py` 只剩 config 的历史注释。

---

### 2.7 CF（usque）线 20 → 3（1 条 v4 + 2 条 v6）

**背景**：20 条 usque 线是 2026-10-22 为扛排空并发扩的（当时池里只有 usque）。现在 16 条
WARP-WG 线与 usque 同在 `warp.members` 一张表里参与轮换，容量不再需要靠 usque 数量堆；
多留的 17 条只是多 17 份注册身份、17 个待探测端口、17 个常驻进程。

**改动**（`config.py:233`）：

```python
"members": [
    {"tag": "WARP-CF",   "port": 1080, "service": "usque-warp.service",   "ipv6": True},
    {"tag": "WARP-CF-B", "port": 1081, "service": "usque-warp-b.service", "ipv6": True},
    {"tag": "WARP-CF-D", "port": 1083, "service": "usque-warp-d.service"},   # v4
]
```

- `.warp/warp-outbound.json` 重新生成（**必须带 `WARP_WG_FAMILIES` / `WARP_WG_NODES`**，
  否则 `_append_wg_members` 不生效，会连带把 16 条 WG 冲掉 —— 本次已踩过一次并修正）
- `singbox-config.json` 用 `parse_subscription.make_config(nodes)` 从现有 27 个节点重建
  （订阅源不可用，只能走"提取现有节点 + 合并 WARP 出口"路径），`sing-box check` 通过
- 17 个 usque 单元 `stop`，单元文件移到 `/root/usque-units-removed/`（`mask` 对真实单元文件无效，
  必须移走），`daemon-reload`

**原因**：v4/v6 各留一条是为了保住两个出口地理位置（v6 → SJC，v4 → LAX）；缩到 3 条后
CF 只作为兜底，主力轮换交给 16 条 WG。

**验证**：`/etc/systemd/system` 只剩 `usque-warp{,-b,-d}.service`；端口只剩 1080/1081/1083；
Clash API `proxies=50`（27 节点 + 3 CF + 16 WG + selector 等），`CF 3 / WG 16`。

---

### 2.8 主动换线模式 fixed → requests（阈值 150 不变）

**背景**：此前因 503 风暴把 `proactive_mode` 临时改成 `fixed`（60s 到点切走）。

**改动**：`config.py:274` `proactive_mode: "fixed" → "requests"`；
`proactive_after_dsv4_ok` 保持 **150**（`config.py:293`）。

**原因**：时间判据与配额扣减不同源 —— 实测主动段 dsv4 成功中位 113.5 / p90 175，60s 上限本身
就在扔掉没用完的配额；而 429 段存活中位仅 6s。requests 模式数的是配额格数，与扣减同源，
流量大小自动适应。150 是历史实测值（多段 138s~167s 跑满 150 格）。

**验证**：`from config import CFG` 打印 `proactive_mode=requests / after_dsv4_ok=150 /
model=deepseek-v4.1-flash-free`；daemon 重启后 `active`。

---

### 2.9 WG 单元加固（start-limit 与端口残留）

**背景**：主人看到 `Job for warp-wg-1.service failed because the control process exited with
error code`。日志显示两类失败：

1. `start-limit-hit` —— daemon 峰值 228 次/分钟重启，systemd 默认"10s 内 5 次启动"直接把单元锁死
2. `bind: address already in use` —— 上一实例（含历史用户级实例）未退干净仍占 1110/1118

**改动**（16 个系统单元 + `deploy/wg-lines-install.sh` 同步）：

```ini
[Unit]
StartLimitIntervalSec=0        # 不限启动次数，杜绝 start-limit-hit 锁死

[Service]
ExecStartPre=-/usr/bin/fuser -k <port>/tcp   # start 前清掉残留端口持有者
Restart=always
RestartSec=2                   # 原 3s
```

**原因**：daemon 的重启频率由业务决定，systemd 的启动频率闸是另一套坐标，会把单元锁成
永久 failed；残留孤儿进程则是"单元看起来 active、端口却属于别的 cgroup"的根因。

**验证**：重写单元 + `reset-failed` + 重启后 `异常: 无`，端口 16/16，
非 MainPID 的重复实例被清理。

---

## 3. 当前生效参数总表

| 参数 | 值 | 含义 |
|------|----|------|
| `proactive_mode` | `requests` | 按成功 dsv4 调用（配额格）触发换线 |
| `proactive_after_dsv4_ok` | 150 | 累计满 150 格换线 |
| `line_probe_cadence_sec` | 0 | 同线探测间隔，0 = 探完即再探 |
| `probe_unknown_retry_sec` | 5 | 拿不到结论时该线重试间隔 |
| `probe_concurrency` | 32 | 行级探测线程并发（唯一物理上限） |
| `model_probe_concurrency` | 32 | 打上游 chat/completions 的并发 |
| `probe_max_per_min` | 0 | 全局探测频率上限，0 = 不限 |
| `probe_ok_ttl_sec` | 600 | IP 未变且已验 OK 的结论缓存 |
| `prober_idle_wait_sec` | 30 | 全部线闲着时的最长休眠 |
| `line_fresh_sec` | 120 | 备线"新鲜"（`last_ok_ts` 窗口） |
| `min_gap_verified_sec` | 1.5 | 换线间隔的**地板**；实际间隔见第 9 节 |
| `restart_concurrency` | 12 | 同时在飞的重启槽位 |
| `restart_cooldown_sec` | 5 | 重启被非失败原因挡下时的固定冷却 |
| `restart_max_per_hour_per_line` | 0 | 每线每小时重启上限，0 = 不限 |
| `switch_wait_sec` / `fallback_idle_wait_sec` | 3 / 3 | 等备线的时间上限 |

线程池/单元层：`_RESTART_SEM=restart_concurrency`、`_MODEL_PROBE_SEM=model_probe_concurrency`、
`StartLimitIntervalSec=0`、`RestartSec=2`、`ExecStartPre=-fuser -k <port>/tcp`。

---

## 4. 运维记录（本次实际执行）

```bash
# 1) WG 系统级迁移
sudo python3 - <<'PY'  # 生成 16 个 /etc/systemd/system/warp-wg-N.service
PY
sudo systemctl daemon-reload
sudo systemctl enable --now warp-wg-{1..16}

# 2) 清用户级残留（mask 对真实单元文件无效，改移走）
mv ~/.config/systemd/user/warp-wg-*.service ~/warp-wg-user-units-removed/
systemctl --user daemon-reload && systemctl --user stop warp-wg-{1..16}

# 3) CF 线缩减
for u in c e f g h i j 11..20; do sudo systemctl disable --now usque-warp-$u; done
sudo mv /etc/systemd/system/usque-warp-$u.service /root/usque-units-removed/
WARP_WG_FAMILIES=v4,v6,v6,v6 WARP_WG_NODES=4 python3 deploy/members.py --write-warp-outbound
python3 -c "from parse_subscription import make_config; ..."   # 重建 singbox-config.json
./sing-box check -c /tmp/singbox-config.new.json && cp /tmp/... singbox-config.json

# 4) 主 sing-box 用新配置重启（daemon 的 KillMode=process 不会连带重启它）
sudo pkill -f "sing-box run -c /home/administrator/dkk-projects/auto-switch-ip/singbox-config.json"
```

---

## 5. 遗留问题与风险

1. **503 不是本地问题：detour 节点失效**（2026-10-28 09:20 已定论，见第 9.4 节）
   `connect to server: unexpected HTTP response status: 503` → sing-box 干净退出 → 单元被拉起。
   驱动者是 daemon 的 `↻ 重启 …（出口处于惩罚期）`，1~2 秒一轮：v4 地址从 colo 池随机抽，
   重启很容易抽回台账里已知在惩罚期的地址。两条候选方案：
   - A（对症）：重启前查台账，若该池所有 v4 地址都在惩罚期则跳过重启，直到惩罚到期
   - B（不对症）：全局降重启频率（等于把刚删的限流加回来）
2. **Cloudflare 注册/连接限流**：228 次重启/分钟、quota:OK ≈ 69:1，若日志出现 `429 uid` 即为反噬，
   届时该动的是重启频率而不是探测频率。
3. **订阅源可达但前置不可用**（2026-10-28 复测，结论已更新，详见第 7.4 节）：
   订阅本身是活的（http=200，67 条），失败原因是本地 DNS 污染 + 前置路由不通。
   主配置仍只能走"提取现有节点 + `make_config()`"合并路径。
4. **探测被上游拒绝的历史结论需保留**：全新 /128、非 WARP 出口（103.62.49.138）均返回同一
   `429 FreeUsageLimitError`，说明"被拒的是探测请求本身"；`Authorization: Bearer public`
   已疑似失效，改写真实凭据前不要把这个 429 当配额读。
5. **`probe_ok_ttl_sec=600` 是唯一仍在起作用的"周期"**：已验 OK 且 IP 未变的线 10 分钟内不重探。
   若主人要求连这个也取消，改 0 即可（会让已验证地址被反复重探，计入并发）。

---

## 6. 验证记录（实际命令与实际输出）

| 时间 | 命令 | 输出 |
|------|------|------|
| 04:32 | `systemctl is-active warp-wg-{1..16}` | 15 active / 1 activating；端口 16 |
| 04:33 | `journalctl -u warp-wg-1` | `✓ WARP-WG-16 重启后出口=...`、`✓ WARP-WG-2 ...` |
| 04:39 | 统计 daemon.log | 结论 100 条/分钟（cadence 归零后瞬时） |
| 04:41 | 3 分钟窗口统计 | 52.3 条/分钟，间隔中位 0.00s |
| 04:45 | 60s 窗口 | 结论 93 / 重启 168 / 撞上限 0 |
| 04:52 | `pytest test_warp_prober test_warp_refresh test_auto_switch` | 98 passed (70.73s) |
| 04:56 | 60s 窗口（删退避后） | 结论 70 / 重启 228 / verdict quota 69, OK 1 |
| 05:01 | Clash API `/proxies` | 50 个；CF 3 / WG 16 |
| 05:04 | `ss -ltn` | 仅 1080/1081/1083（usque）；1110–1125（WG） |
| 05:08 | `systemctl is-enabled usque-warp-c` 等 | 单元文件已移出，`/etc/systemd/system` 仅剩 3 个 usque |
| 05:10 | WG 单元重写后自检 | `异常: 无`，端口 16/16 |

> 未验证：全量 `pytest -q`（本次只跑相关子集）；Cloudflare 429 是否因 228 次/分钟重启而上升；
> WG-1/WG-9 的 503 循环在长时间下的表现。

---

## 7. 地址池与 Colo 实测（2026-10-28）

### 7.1 结论：colo 由 detour 节点所在 PoP 决定，不由 WARP endpoint IP 决定

用 76 个 endpoint（`162.159.192.0/24` 每 5 取 1、`188.114.96-99.{1,33,65}`、`162.159.193/195` 抽样），
分别经四个地面的 detour 节点做 WARP 握手并读 `cdn-cgi/trace` 的 `colo=`：

| detour 所在 | 样本 | colo 分布 | 实测地址池 |
|---|---|---|---|
| NRT（`unamecf`） | 68/75 | 100% NRT | `104.28.211.105`、`.211.106`、`104.28.243.105` |
| SIN（`pq.aws63`） | 69/75 | 100% SIN | `104.28.222.43`、`.222.46`、`104.28.254.46`、`.254.47` |
| SJC（`pq.us05`） | 68/75 | 100% SJC | `104.28.201.73`、`.201.80`、`104.28.233.73` |
| PHX（`pq.us01`） | 12/12 | 100% PHX | `104.28.205.113`、`.205.117`、`104.28.237.117` |
| 直连（无 detour，2408/500/4500） | 0/36 | 全超时 | — |

由此得到三条可操作事实：

1. **换 endpoint IP 换不了 colo**，只改变同 colo 内抽到哪个地址。例：NRT 组用 `188.114.96-99.x`
   抽到 `.211.106`（新地址），SIN 组用 `162.159.195.10` 抽到 `.254.47`。
   → 在不换 colo 的前提下给被烧的组扩池，改 endpoint 即可。
2. **`162.159.193.x` 在四个 vantage 全部超时**（疑似 MASQUE/HTTP3 专用端点），不再浪费时间扫。
3. **本机直连 WARP 完全不通**（2408/500/4500 全超时）→ 没有本地视角，colo 只能跟着节点走。

### 7.2 不变量：colo 池互不相交，且组与 colo 固定绑定

- 台账映射（`wg_colo_map.json` / `wg_colo_v6.json`）与实测采样均无跨 colo 重复 IP；
  每 colo 的 v4 `/24` 互不相同，v6 `/48` 无跨 colo 复用。
- 组↔colo 由 detour 决定且固定：**WG-1~4=NRT、5~8=PHX、9~12=SIN、13~16=SJC**。
  → 某组地址被烧 = 整组被烧，重启换不出别的 colo。这是 WG-1/2/3 看起来"死"的根因。

### 7.3 现有 27 节点清单的实测 colo

逐个探测：**NRT×11、SIN×6、PHX×4、SJC×3、LAX×1**，另 2 个死节点（`pq.aws52`、`unamecf3`）。
LAX 节点（`203.10.96.138:443-vl`）自身可探通（trace colo=LAX），但经它做 WARP 时 75/75 全部超时
（该节点出不了 UDP/2408），所以 LAX 不可用作 WARP vantage。

### 7.4 订阅源复测：订阅是活的，前置不可达

- **本地 DNS 污染**：`s-dywrwizazu.cn-shanghai.fcapp.run` 本地解析到 `59.24.3.174`（连接超时，死）。
  DoH（经节点查 `1.1.1.1`）真值为 `47.117.175.35`；用 `curl --resolve <host>:443:47.117.175.35`
  直连可取，`http=200`。
- **内容随 UA 变化**：无 UA → 只给 2 条（1 真 1 广告）；`-A v2rayN/6.42` → 67 条；
  `clash-verge` / `ClashforWindows` / `sing-box` UA 返回的是 clash/sing-box 格式配置。
- **67 条的地区覆盖**：HK 18、TW 6、KR 2、US 7、SG 3、JP 3，另有 UK/AT/VN/NL/MY/ID/TR/AR/TH/PH/RU
  各 1~2 条。协议为 anytls 65 + trojan 2。
  注意：我们现用的 27 个节点**不在这份订阅里**（属另一套 `yydjc.top` / `unamecf` 前置）。
- **这些新前置在本机全部不可达**（问题不是"延迟大"，是 TCP 层不通）：
  - `acczzz.com` 域名公网无 A 记录（`1.1.1.1` DoH 返回 NXDOMAIN），其 SNI 域名
    `hi2026.net` 解析到 `95.40.66.254`，TCP 不通；
  - CDN 前置 `bilibili-image-cdn.juhazf.cn` → `35.77.4.68`，`80` / `443` / `50013` / `55067`
    全部 TCP 超时；
  - 对照组：本机直连 `47.117.175.35:443` 通、`1.1.1.1:443` 不通 → 属于该前置对本机的路由/封锁，
    而非通用出网故障。

→ 结论：**要拿 ICN/TPE/HKG 的 WARP colo，必须先有一条能到达该地区节点的前置**；
当前 27 节点里没有任何 KR/TW/HK 落地，订阅里的 KR/TW/HK 前置又不通，所以暂时拿不到。

### 7.5 订阅 67 条节点的前置全量连通性（2026-10-28）

把 67 条按 `(host, port)` 去重（64 个前置）+ 全部 SNI 域名做 DoH 解析后逐条 TCP 探测：

| 指标 | 结果 |
|---|---|
| 唯一 host/sni | 76 |
| 有 A 记录 | 62（`acczzz.com` 系列全部 NXDOMAIN） |
| TCP 可达前置 | **0 / 64** |

逐类失败形态：

- `acczzz.com`（HK-G1..G6 / JP-G1..G2 / SG-G1..G2 的 9 条）：公网无 A 记录，完全无法开始连接。
- `bilibili-image-cdn.juhazf.cn:500xx/550xx`（50 余条，HK/TW/KR/US/EU 出口都挂在这个前置上）：
  解析到 `35.77.4.68`（CNAME `3f.okzdns.com`），本机 `80/443/50013/55025` 全 TCP 超时；
  经 JP 节点访问时 `80/443` 仍超时、`50013` 能建连但立刻 EOF。
  用二级转发（本机 → JP 节点 → 该前置）跑 anytls，握手报 `failed to create session: EOF`。
- 反向验证不是全局限流：同一批 IP 的 `:443` 大多可达（`1.162.155.159` TW HiNet、
  `114.42.194.93`、`209.141.54.251` BuyVM、`91.199.84.40`、`16.106.33.212` 均通），
  只有订阅里用的那个前置不通。

→ 判定：**这套订阅的前置当前处于不可用状态**（与"节点最近被打击"一致），
不是延迟、不是 DNS、也不是本机出网全断。要拿 KR/TW/HK 的 WARP colo，必须先有一条
能到达该地区落地的前置；当前最实际的做法是向服务商要客户端/备用入口，或换一套可用订阅。

**DNS 交叉验证（2026-10-28）**：换解析器结果一致，不是 split-horizon。

| 解析途径 | `bilibili-image-cdn.juhazf.cn` | `hk-aws-122.acczzz.com` | `acczzz.com` apex |
|---|---|---|---|
| WARP 隧道内查 Cloudflare DoH | `3f.okzdns.com` → `35.77.4.68` | NXDOMAIN | `104.218.232.59` |
| 本机直连阿里 DoH / DNSPod DoH | `35.77.4.68` | NXDOMAIN | `104.218.232.59` |
| 经 JP 节点查 Cloudflare DoH | `35.77.4.68` | NXDOMAIN | — |

即前置地址唯一且确定，问题不在解析。附带发现：`www.acczzz.com`（`104.218.232.59`）本机可达，
页面跳转 `okztwo.net`（`8.7.198.46`，本机直连不通、经 JP 节点 200）——说明旧订阅域名挂在已停用的
入口上，新入口在面板/客户端侧；`/api/v1/client/subscribe?token=…` 直接访问返回 404。

**权威 NS 与后端直连复核（2026-10-28）**：`okzdns.com` 托管在 Cloudflare NS，两个 NS 直接查询
`3f.okzdns.com` 都只返回单个 A `35.77.4.68`（无轮询、无备用 IP），可确认前置地址唯一。
而 62 个 SNI 后端里有 **56 个 `:443` 可直连**（HK/TW/KR/SG/JP/US/EU 全在内），说明服务器本体是活的；
但对后端 IP 直连跑 anytls（SNI 正确）会立刻被 RST
（`failed to create session: read tcp …->109.61.127.197:443: connection reset by peer`），
即后端只接受自己前置转发进来的连接。结论：**这套订阅必须经其前置才能用，前置一死整池不可用**。

## 8. 地址池清算（2026-10-28）

测法：固定 WARP 入口 `162.159.192.1`，只变 detour 节点；每条线每次起 sing-box 都是一次新
WARP 会话，用 `cdn-cgi/trace` 读出出口地址。v4 每节点 8 样本，v6 每节点 6 样本。

### 8.1 v4 池是 colo 级，不是节点级

| colo | 节点数 | 样本 | 唯一地址 | 地址池 | 各节点唯一数 |
|---|---|---|---|---|---|
| NRT | 11 | 84（超时 4） | 2 | `104.28.211.105` `104.28.243.105` | 全为 2 |
| SIN | 6 | 40（超时 8） | 5 | `.222.43` `.222.46` `.222.47` `.254.46` `.254.47` | 0/4/1/2/4/3 |
| PHX | 4 | 32 | 3 | `.205.113` `.205.117` `.237.117` | 3/3/3/2 |
| SJC | 3 | 24 | 2 | `.201.80` `.233.73` | 2/2/1 |

NRT 的 11 个节点各打 8 次，全部只落在同一对地址上 —— **「同 colo 不同节点打出不同池子」不成立**。
SIN/SJC 节点间看到的唯一地址数差异（1 vs 4）是 8 次抽样覆盖不全的噪声，不是池子不同：
SIN 5 个地址里最偏的一个节点 8 次全抽到 `.222.43`，另一个节点 8 次就覆盖了 4 个。

### 8.2 池随 WARP 入口端点变，不随节点变

换成 `188.114.96-99.12` 等入口，NRT 同 colo 多出 `104.28.211.106`；SJC 多出 `104.28.201.73`。
即 v4 已知总池 = **14 个地址 / 4 个 colo**，杠杆在入口端点，不在 detour 节点。

### 8.3 v6 池近乎无限

| colo | 样本 | 唯一地址 | /48 段数 |
|---|---|---|---|
| NRT | 66 | 56 | 12 |
| SIN | 30 | 30 | 10 |
| PHX | 24 | 23 | 10 |
| SJC | 18 | 18 | 9 |

每次拨号抽一个新 /128，重复率 <4%。段集合固定（NRT `2a09:bac5:4300-4307`、`2a09:bac1:3b00/3b20/3b40/3b60`
等）。**封单个 v6 地址对下一次拨号没有影响**，所以「v6 线被封死」不可能是地址层面的原因。

### 8.4 与台账对账

`104.28.211.106` 台账无记录（11 条已知 v4 里唯一没有历史的一条）；其余全部 `rate429`、剩余约
1.8h，只有 PHX `104.28.205.113` 曾连续服务 5816s（`last_ok 06:08`）。

### 8.5 线路映射

每个 colo 一条 v4（`WARP_WG_FAMILIES=v4,v6,v6,v6`）→ v4 线只有 WG-1/5/9/13，其余 12 条走 v6。
于是 4 条 v4 线共享上面那 14 个地址，12 条 v6 线地址近似无限。

### 8.6 结论

1. **地址数量不是瓶颈**。v6 地址无限，NRT/SIN/SJC 的 9 条 v6 线依然没有可用结论。
2. 当前判死的直接证据是账号级额度窗：同一时刻 NRT 与 PHX 的 v6 出口都给出 `quota` +
   `Retry-After 6300s`，与出口地址无关（`_QUOTA_HOLD_UNTIL` 因此在生效）。
3. 所以「扩池救线」方向不成立：扩出来的地址仍会撞同一额度窗。真正的杠杆是换凭据/换模型，
   或等窗口过去；地址维度唯一还有效的杠杆是换入口端点（见 8.2），但它不改变结论。
4. `probe_warp.py` 用 `Bearer public` 时对每条线都返回 `quota`，包括历史上 OK 的线 ——
   在接真实凭据之前，它不能用来区分 colo 级与地址级的失败。

---

## 9. 调用方同步换线 + 间隔自适应（2026-10-28 09:00–09:25）

### 9.1 结论先说

| 问题 | 答案 | 证据 |
|------|------|------|
| S6/S7 做了吗 | **早就做了，而且一直在生产里跑** | 40 分钟内 9router 日志 `asip rotated` 191 次 |
| 换线超时取多少 | **2000ms** | 10 次实测 min 774 / p50 1090 / p90 1314 / max 1374 |
| 供给为什么不足 | **不是候选不够，是固定 8s 闸** | 候选常年 8–13 条，`no_standby` 恒为 0；191 次调用里 82 次被闸拒 |

### 9.2 S6/S7 的真实状态

`open-sse/utils/asipRotate.js`（UDS + token 客户端）和 `open-sse/executors/opencode.js`
里的 429 分支本来就已经写好，但**两处关键事实没人核对过**：

1. 它是不是真的在跑 —— 9router 的 server 是 node22，sidecar 才是 bun。`node:http`
   的 `socketPath` 在 bun 里能不能用，之前没验证。实测 bun 1.4.4 下 `readAsipState()`
   / `rotateEgress()` 都正常返回，这条路是通的。
2. 它是不是进了构建 —— 源码改了不等于跑着的进程里有。`.next/server/chunks/5330.js`
   里能搜到 `abusive traffic` 与 `ASIP_TIMEOUT_MS`，说明当前构建包含这段。

本次补的是**403**：原来只有 429 会触发换线。照搬 daemon 的 `_classify_egress_error`，
403 必须先看正文 —— 只有 `abusive traffic` 是出口信誉问题，`FreeTierError` 是请求指纹/
请求形状被拒，换 IP 无效，还会把另一条线拖进惩罚期。

### 9.3 换线超时：2000ms

实测 10 次（16 条 WG + 3 条 usque，探测器同时在跑，198s 间隔 3.4s 连续触发）：

```
min 774 / p50 1090 / p90 1314 / max 1374  (ms)
```

- **800ms 不可行**：会砍掉一半正常换线。
- **1500ms 太薄**：对 max 1374 只剩 9% 余量，尾巴一抖就超。
- 超时还有隐性代价：客户端销毁请求，但 asip 那边的换线**不会停**。于是 9router 在旧
  隧道上重试、出口却已经变了 —— 比多等 0.5s 糟得多。

### 9.4 供给：不是候选不够，是闸太慢

连续 12 次 `rate429` 压测（间隔 1.2s）的原始输出：

```
成功=3  限速=20  无备线=0   cands 8~13 / 19
```

候选从来没缺过，`no_warm_standby` 一次没出现。真正的限制是固定 `min_gap_verified_sec=8.0`：
429 惩罚 60s、19 条线，可持续换线速率本来就是 3.16s 一次，8s 等于主动丢掉一半机会，
客户端只能拿着已经被拦的 IP 继续重试（生产日志里 60 次 `too_frequent` 就是它）。

改成随规模自适应：

```python
gap = max(1.5, rate429_guard_sec / len(members))   # 19 条 -> 3.16s；3 条 -> 20s
```

同一波压测变成 **成功=7 / 限速=5 / 无备线=0**。另外两处：

- 当前出口**已被拉黑**时立刻放行（那时换出去只会更好，没必要等间隔）。
- 记账挪到判限速**之前**：被拒的那次调用带来的也是真证据，拒了也不能当那条 IP 是干净的。

### 9.5 503 的真身：detour 节点死了，重启无效

`connect to server: unexpected HTTP response status: 503` 意味着这条线的**中转节点**
失效，本地怎么重启都拿不回新节点。对策是 `warp_wg.py rebind <i> --probe` 换节点重写
该线配置。2026-10-28 实测 line1 / line12 / line13 就是这样救回来的。

`deploy/wg-bootstrap.sh --verify-only` 的自愈因此改成两段：重启 → 重测 → 仍不通则
rebind → 重启 → 重测。

### 9.6 顺手修掉的老坑：rebind 写配置的属主

`rebind` 以 root 跑时写出的 `singbox.json` 是 0600 + root，而单元是 `User=administrator`，
sing-box 报 `FATAL read config: permission denied` —— 报错读起来像文件损坏，实测把两条线
误判成「节点 503」。现在 `write_line()` 写完就把属主对齐目录属主（目录是安装时以运行用户
建的），从根上不再依赖调用者是谁。

### 9.7 台账与吞吐的实测（2026-10-28 09:40–09:50）

**台账规模不是泄漏。** 11,423 条记录、最大年龄 23.58h —— 修剪窗口就是 24h，表长等于
窗口内 IP 数，符合设计。其中 595 条处于罚期，且 `ban_until` 剩余时间高度一致（读数都是
22.3h）。我一开始把它判成「固定值误写」，是读错了：**上游把 429 统一压到次日 08:00**，
所以所有记录指向同一个绝对时刻，剩余读数自然一样。这类长封禁来自真实 Retry-After，
不是本地的默认窗口（`rate429` 本地默认只有 60s，`_warp_block_ttl('rate429')` 实测 60）。

写入成本：一次记账 `mark` 约 15ms，其中落盘 `save` 约 40ms（2.7MB JSON 全量重写，
持 `_WARP_IP_LOCK`）。当前 10 次/分钟量级可忽略，但如果换线频率上一个数量级，
这里会先成为瓶颈。

**吞吐上限：正好卡在满负荷，没有余量。** 40 次连续 `rate429` 换线（间隔 3.2s，
约等于按 19 条线算出的可持续速率）：

```
成功 40 / 40     too_frequent 0     no_warm_standby 0     coalesced 0
候选深度 7 → 6 → 5 → 3 → 1 → 0 → 1 …（地板 0，稳定在 0~2）
```

结论：`gap = 60/N` 这条公式把系统精确地推到 100% 利用率 —— 60s 惩罚窗 ÷ 3.16s 间隔
≈ 19 条线，正好等于全部活线。所以候选深度必然被压到 0 附近，但一次 `no_warm_standby`
都没出现（选线路径会等新鲜线，不是硬失败）。

要不要留余量是个取舍：留 1.5 倍（间隔 4.7s）能让深度稳定在 6~9，代价是风暴期换线慢 50%。
目前选择留满负荷，理由是 9router 侧换线失败会立刻重试、且**没有任何一次真实失败**。

**纪律：压测不许在主人机器上随手跑。** 这次 40 次换线虽然没动模型额度（全是 `/rotate`，
一次上游请求都没发），但会真实刷新线路、并给被放弃的 IP 记 60s 罚期。要压先在 dry-run
或独立实例上做，或者先问。
