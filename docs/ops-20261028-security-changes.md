# 2026-10-28 安全面改动台账

这份文件存在的唯一目的：**让不归任何部署脚本管的改动有痕迹。**
下次审计（或半年后的我）看到 `/etc/sudoers.d/` 里多出来的文件时，应该能在这里
查到它是谁、为什么、怎么撤。

## 1. `/etc/sudoers.d/90-administrator-nopasswd`（新增，0440 root:root）

```
administrator ALL=(ALL) NOPASSWD: ALL
```

**为什么**：伙伴当场给了 sudo 密码并要求「改成不需要密码」，便于连续运维
（当时要改 `/etc/systemd/system/usque-warp-d.service`、装 timer 等）。

**代价（必须知情）**：
- `deploy/asip-unit` 那套白名单**语义上失效**了。白名单本身仍在
  （`/etc/sudoers.d/auto-switch-ip`，由 `deploy/install.sh:208` 维护，只允许
  `asip-unit` 的 restart/start/stop/is-active），但 `90-` 文件覆盖一切。
  原注释里「不想让无交互的服务进程拿到 root」这句话，从今天起不再成立。
- 任何以 `administrator` 身份跑起来的进程（服务、脚本、mcp server、被投毒的
  订阅解析链）都能无提示提权。本机是单用户 WSL2，风险主要是「自己写错一行没
  兜底」。

**不在任何部署脚本里** —— `install.sh` 只写 `auto-switch-ip`，重装不会收紧它。

**撤回**：
```bash
sudo rm /etc/sudoers.d/90-administrator-nopasswd
```
撤之前确认 `/etc/sudoers.d/auto-switch-ip` 还在，否则 daemon 会失去重启 unit 的能力。

**缓解**：新增的 `dormant-probe.service` 已加 `NoNewPrivileges=yes` /
`PrivateTmp=yes` / `ProtectSystem=full` / `RestrictSUIDSGID=yes`，把「解析订阅
派生配置」这条链截断在提权之前。

## 2. `/etc/systemd/system/usque-warp-d.service`（手改，备份 `.bak-v6`）

`ExecStart` 从 `usque-launch.sh 1083 -6` 改为 `usque-launch.sh 1083`，让直连组
成为「2 条 v6 → SJC + 1 条 v4 → LAX」。实测改前 colo=SJC、改后 colo=LAX。

**注意（审查发现）**：这个改动**不可由配置复现**。`config.py:260` 的
`WARP-CF-D` 成员没有 `ipv6` 键，重跑 `deploy/install.sh` 会生成
`usque-launch.sh 1083 -`（`-` 等价于 v4），结果一致 —— 所以现状自洽。
但 `.bak-v6` 里的 `-6` 同样无法由配置复现：把备份拷回去只能管到下一次
`install.sh` 之前。要让它成为有效回滚点，得同时给 `config.py` 的该成员加
`ipv6: True`。

## 3. `/etc/systemd/system/dormant-probe.{service,timer}`（新增）

沉睡池探测（见 `dormant_probe.py` 头部说明）。回滚：
```bash
sudo systemctl disable --now dormant-probe.timer
sudo rm /etc/systemd/system/dormant-probe.{service,timer}
sudo systemctl daemon-reload
```

## 4. 已知取舍（不作为缺陷跟踪，但要知道）

- **`.dormant_state.json` / `dormant-outbounds.json` / `dormant_probe.py` 未入 git**
  （`.env` 也没入，`git ls-files` 只有 `.env.example`）。回滚靠手工。
  要不要入库由伙伴定。
- **探测失败原先无主动告警（10.28 晚已由 ExecStopPost 补上，见下）**：`Type=oneshot` 且无 `OnFailure=`，失败只在
  `journalctl -u dormant-probe.service` 里留一条 `Failed`。journal 里有逐条
  `ok/dead/error` 记录，不算无声，只是没人会主动翻。
- **daemon.log 的 `.N` 备份原来权限不一**（0600 / 0644 / 0664 混杂），
  已一次性 `chmod 600`。轮转是谁做的还没查清（不在 `daemon.py`，也不在
  `/etc/logrotate.d/`），下次轮转可能又放宽 —— 如果哪天看到 0644 的
  `daemon.log.N`，就是这里没根治。

---

## 2026-10-28 晚：三方审查的第三轮修改

### 1. `auto-switch-ip.service` 终于有沙箱了（9.2 UNSAFE → 7.3 MEDIUM）

原先它零沙箱，而它才是唯一真正"吃"订阅（URL / UA / 解析结果）并 `Popen`
子进程的地方 —— 把沙箱只加在探测服务上，等于把锁装在了侧门上。

加的：`ProtectSystem=strict`、`PrivateTmp=yes`、`ProtectKernel{Tunables,Modules,
ControlGroups,Clock,Hostname}`、`RestrictSUIDSGID`、`RestrictRealtime`、
`LockPersonality`、`UMask=0077`、`ReadWritePaths=/home/administrator`。

**两条不能加，都踩过：**

| 参数 | 后果 | 症状 |
|---|---|---|
| `NoNewPrivileges=yes` | `sudo` 直接不可用（setuid 被禁） | daemon 失去重启 WARP 的能力 |
| `CapabilityBoundingSet=` / `AmbientCapabilities=` | **sudo-rs panic** | `could not restore to saved user id: Os { code: 1, PermissionDenied }`，12 条 WARP 线全部拉不起来，日志里 178 条失败 |

第二个是实测踩出来的：加完 20 秒内 `daemon.log` 就开始刷 panic，`warp-wg-1/5/6`
全线重启失败。撤掉这两行后立刻恢复（12 条 `active`、`重启后出口=` 正常）。
教训一句话：**沙箱要一条一条加，每加一条看一眼对方还能不能干活。**

`ReadWritePaths` 之所以放开整条家目录：`ProtectSystem=strict` 会把 `/home` 也变
只读，而 daemon 需要在 `/home/administrator/.singbox_secret` 缺失时重建它，
否则 7890 的 clash 认证直接坏。`/etc`、`/usr`、`/boot` 仍然只读 —— 那才是这套
沙箱值钱的地方（挡住"订阅投毒 → 改 systemd 单元 / sudoers"）。

> 注意：`NOPASSWD: ALL` 还在时，主动提权那条路本来就通，这些沙箱只挡"意外写坏"
> 和"不提权的代码路径"。要真堵住，得先撤 `90-administrator-nopasswd`。

### 2. `daemon.log` 权限不再漂（0644 ↔ 0600）

根因就是没有显式 umask：日志文件被谁按默认 0022 重建一次就变 0644。单元里加
`UMask=0077` 之后固定 0600。

### 3. `dormant-probe.service` 4.3 OK

`UMask=0077`、`CapabilityBoundingSet=`、`AmbientCapabilities=`、`PrivateDevices=yes`、
`RestrictAddressFamilies=`、`SystemCallArchitectures=native`。

**`RestrictAddressFamilies` 里必须留 `AF_NETLINK`**：sing-box 要靠它枚举网卡，
少了这一项临时实例起不来，表现为 `✗ 临时实例起不来，不写任何状态（端口 x/y）`（旧版是"（或端口被占）"，已改）。

`ExecStopPost` 把 oneshot 的失败写进 `daemon.log`（原先只留在 journal 里，没人翻）。

### 4. 沉睡池脚本三个真 bug（都是审查抓的，都是我写的）

| 问题 | 后果 | 修法 |
|---|---|---|
| `%{http_code}` 在连不上/超时时输出 `"000"`（非空） | 代理端口关闭（`port-closed` → `error`）；临时实例已死 / DNS 全挂**目前分不开**（实例活着但出站坏时仍是 `dead http=000`，与节点真死同形），全部记成"节点死"；`errors` 阈值永不触发 | 判死前先探本地代理端口，端口没了记 `ERROR` |
| 无并发锁，而 `kill_orphans()` 会杀掉同前缀的**并发**实例 | 定时器与手动跑重叠时，后者杀掉前者的 sing-box，前者剩余节点全判死落盘 | `flock`，拿不到锁直接退出且不写状态 |
| `targets()` = `t not in pool or t in dead` | **进了池子的节点永不复测**：活着进池 → 之后死了 → 没人测 → `alive:true` 永远留着 → 死节点常驻池子。这正是沉睡池要治的病，只是慢一点复发 | 池内节点超过 `POOL_RECHECK_HOURS`（6h）也复测 |

另外：无历史记录的节点单次超时不再直接判死（加长超时重试一次）；写侧比对 `fp`，
tag 换指新节点时不继承旧 `strikes`；`save_state` 补目录 `fsync`。

### 5. 骤降护栏的口径换了三次，最后定在"池子成员 + 24h 峰值"

- 第一版数出站总数（135）→ 被 80 条常量稀释，永远不触发。
- 第二版数 selector 成员（36）→ 基线取自"已按 sleeping 过滤过的配置"，会随判死
  一路下移，护栏越守越松。
- 第三版数「订阅条数」→ 误伤：okz 那 62 条全是死节点，订阅挂掉时池子一个不少，
  却会拒收一整轮刷新。
- **当时以为的定版**：比池子实际成员数，基线取 24 小时峰值。
  这一段后来被推翻（见文末「护栏重写」）：上面的 36→18/36→5/36→0 是**直接用
  `daemon._read_sub_stats()` 的函数值算出来的**，不是线上日志 —— 线上那三行到
  当晚 19:31 才第一次真的落盘，而那时逻辑已经换掉了。

### 6. 还没做的（需要主人点头）

- 撤 `90-administrator-nopasswd`（我建议撤，改成只放行 `asip-unit`）。
- `.warp/wglines/line13~16/` 里还留着 4 条已停用线路的 WARP 私钥。
- 运行时状态（`.dormant_state.json` / `.pool_history.json` / `.dormant_probe.lock`）、`dormant_probe.py` 已加进 `.gitignore`（`.sub_stats.json` 已删除，不再存在）；
  **但 `dormant_probe.py` 与本文档本身仍是未跟踪文件、也不在 `.gitignore` 里** —— `git clean -fd` 会真的删掉它们（22KB 的探测脚本 + 这份台账），别当它们是安全的。
- `/tmp` 里还有今天摸出来的临时脚本和几份含凭据的 JSON 快照（`pq_*.json`）。
- 21 个已停用单元的 `[Install]` 段还在，`systemctl preset-all` 会把它们全部拉回来。

---

## 2026-10-28 深夜：护栏重写，以及我自己弄坏的八处

### 1. 「24 小时峰值」这个设计是错的，作废

上一节写的定版方案是"池子成员数 + 24h 峰值"，三路审查同时指出它是错的，而且
两路各自实跑复现了：

- **单向棘轮**：`pool_peak` 靠"距上次写 >24h 才清零"来滚动，可是 `ts` 每小时
  都被刷新，判断恒真 → 峰值实际是"自上次中断以来的历史最大"，只涨不落。
  实跑：pool 128 → 60 之后**每一轮**都被拒，逃生口只有手工删文件。
- **fallback 毒化**："只剩 <3 条就退回全部节点"那一轮，`pool_nodes` 会变成节点总数
  （今天 = 117）。峰值被抬到 117 之后，正常的 36 条每小时都被拒 —— 等于
  `rm .dormant_state.json` 一次就能把订阅刷新永久钉死。

**现在的做法**（`daemon.py`）：

- 统计写在**生成出来的临时配置**旁边（`<cfg>.tmp.stats.json`），只认"这一次真的
  生成出来的"；promote 之后由 `_drop_stats()` 删除，最终配置旁边没有这份文件。手工试跑、pytest 都不会再污染线上基线
  （原来 `.sub_stats.json` 的 mtime 18:58 就是一次试跑写的，线上配置是 18:15）。
- 基线 = `.pool_history.json` 里**每条样本按自己的 ts 判断**是否还在 24h 窗口内的
  最大值。窗口是真会滚的："旧样本 25h 前 + 新样本 17" → 基线回落到 17（第四轮复核时重跑过：`_pool_baseline()` 返回 17；这条是函数级验证，线上没有对应的日志行）。
- 样本只在 **promote 成功之后**追加；退回全部节点那种轮次不记。
- 连续 4 次被拦会喊一句"基线可能已过时"，但**不自动放宽** —— 放宽就等于没护栏。

### 2. 生成侧的告警以前根本进不了日志

`parse_subscription.py` 的"N 个节点暂离池子""⚠ 退回全部节点"都走 stderr，而 daemon
只在 rc≠0 时读 stderr。于是"78 条为什么不在池子里"这件事，在生产日志里**一次都没
出现过**（grep 全 0）。现在成功路径也捞：凡是 `⚠` 开头或含"暂离池子"的行都进
`daemon.log`，并且新加的"池子 N 条（判死摘出 M 条，订阅源供出 K 条）"把三个数一起报。

### 3. 锁必须放在两个实例都看得到的地方

`dormant_probe.py` 的 `flock` 原来锁 `/tmp/dormant-probe.lock`。而服务单元有
`PrivateTmp=yes` → 服务态下的 /tmp 是私有 tmpfs，锁文件在那里**永远没有第二个进程来
竞争**，等于没锁：定时器触发的探测和运维手动跑照样并发，各起一个临时 sing-box、
各自 PUT selector、最后写状态，谁后写谁赢。现在锁在项目目录
`.dormant_probe.lock`（跨命名空间可见）。锁文件打不开时（比如被 root 跑过一次、
属主变 root）会喊一句然后**不加锁继续跑**，而不是整轮不跑。

### 4. 临时实例起不来时终于有诊断，并且会换端口重试一次

`sing-box` 的 stdout/stderr 以前直接丢 `/dev/null`，所以 19:04 那次
`✗ 临时实例起不来（或端口被占）` 到底是被占、配置 FATAL、还是二进制没了，事后查不动。
现在输出写进 0600 的临时日志，失败时打尾部；并且**换一组端口再试一次**
（`free_port()` 交出去的端口有可能在 sing-box 绑上之前被内核当成出站源端口用掉）。

---

## 这一夜我自己弄坏的八处（都修好了，都留在这儿）

| # | 我干的事 | 症状 | 修法 |
|---|---|---|---|
| 1 | 给 daemon 单元加 `CapabilityBoundingSet=` | sudo-rs panic（`could not restore to saved user id`），12 条 WARP 线全拉不起来 | 撤掉，单元里写明为什么不能加 |
| 2 | 补丁把 `def _discard(path: str):` 写了两遍 | **daemon 语法错误，systemd 重启 12 次**；轮换从 19:28:12（SIGTERM）停到 19:30:05（横幅），**约 113 秒**，其中崩溃循环段 19:29:24–19:30:05 约 40 秒。sing-box 没停（`KillMode=process`），代理本身依旧可用 | 去重；此后每个补丁跑完先 `ast.parse` 再重启 |
| 3 | `ExecStopPost` 里写了 `printf "%s"` | systemd 把 `%s` 当"用户 shell"说明符，日志里变成 `/bin/bash` | 所有 `%` 写成 `%%` |
| 4 | 正则改 `make_config` 签名 | 生成 `stats_out=None): -> dict:` → `parse_subscription.py` 直接起不来（daemon 报"✗ 订阅解析失败 (rc=1)"；19:31:02 那句"无可用订阅源"来自一个**旁路进程**，不是常驻 daemon —— 同一进程 33 秒后又成功刷新过） | 手改回 `stats_out=None) -> dict:` |
| 5 | 替换区间跨过了变量声明 | `alive/errors/transitions` 被连带删除，探测 `NameError` | 补回 |
| 6 | 用 root 跑过一次探测 | 锁文件变 root:root 0600，administrator 的定时器打不开 | chown + 代码里加容错分支 |
| 7 | 给 `log()` 加"每 5 分钟重试建文件 handler"时，把 `global _logger` 写在使用之后 | **SyntaxError，daemon 20:09–20:23 崩了 94 次、约 14 分钟不可用**（代理没断，轮换停了） | 删掉重复的 `global`（第一次声明就够） |
| 8 | 审查脚本 import 了 daemon 并调 `log()` | 假事件写进生产 `daemon.log`，`grep` 会得出"拦了 3 次"的错结论 | 副作用闸：只有 systemd 进程（`INVOCATION_ID`）才写文件日志 |

**教训一句话**：这一夜里"改坏"的次数比"改好"的次数少不了多少，每一次都是同一个原因 ——
**改完没在自己身上先跑一遍就重启了服务**。规矩改成：补丁 → `py_compile` → 单跑一次
（dry-run / `--limit 1`）→ 才允许重启。

**而且 `ast.parse()` 不是语法检查。** 第 7 条那个 `global` 声明顺序的问题，
`ast.parse()` 照单全收（它只管括号和缩进），只有 `compile()` 才报
`name '_logger' is used prior to global declaration`。我用 ast 做了三次"语法 ok"的
确认，daemon 崩了 94 次。**以后一律 `python3 -m py_compile`。**

**顺带**：无限重启比"停住"更糟 —— 94 次重启把真问题埋进了滚动日志，还让人以为
服务还在跑。所以 `auto-switch-ip.service` 加了 `StartLimitIntervalSec=300` +
`StartLimitBurst=5`：5 次起不来就停住等人看。

---

## 回滚手册（2026-10-28 四轮之后补）

**快照是"基线"，不是"回滚源"**：`backups/20261028-1950/` 里是**这一刻在跑的**那份（5 个 .py：`daemon.py` / `parse_subscription.py` / `dormant_probe.py` / `warp_wg.py` / `config.py`；3 个 unit 含 `dormant-probe.timer`；`.gitignore`；`README.txt`；`sudoers.d.txt`；`md5sum.txt`）。把它 `cp` 回去等于复制自己，**什么都回滚不了**。

它有两个用处：① `md5sum -c md5sum.txt` 一眼看出"现在跑的还是不是这一版"；② 真要回到过去，用下面表里的 `.bak-*` / `.pre-*`。

**留底**：`backups/` 与所有 `.bak-*` 都在 `.gitignore` 里、且只在这一块盘上 —— `git clean -fdx` 或盘坏就一起没了。稳妥做法是把 `backups/` + `*.bak-*` + `*.pre-*` 打成一个 tar 放到仓库外（例：`tar czf ~/asip-backup-$(date +%F).tgz backups/ *.bak-* *.pre-*`）。想要"改之前"的样子用同目录下的 `.bak-*`：

| 要回到 | 文件 | 说明 |
|---|---|---|
| 护栏第一版（数出站总数，永远不触发） | `daemon.py.bak-stats` | "`_read_sub_stats()` 之前"的前像；它的护栏是 `old_n = _count_outbounds(...)`，**不是**第三轮那套"池子成员 + 峰值" |
| 第四轮改坏之前 | `daemon.py.pre-r4fix` | 护栏已重写、logger 还没包兜底 |
| 探测脚本上一版 | `dormant_probe.py.pre-p5` | 锁已在项目目录、还没有 HTTP 码与 error 落盘 |
| 单元文件 | `/etc/systemd/system/*.bak-*` | `auto-switch-ip.service.bak-sandbox`、`dormant-probe.service.bak-ts`（另有 `-caps` / `-stop` / `-round3` 几份中间态）。**`dormant-probe.timer` 没有 `.bak` 前像**（今天没改过它，只有快照副本） |

**`parse_subscription.py` 没有干净前像**（`.bak-stats3` 已含 `write_sub_stats`）。要退回去
只能 `git checkout`，而仓库 HEAD 停在 `84ca783`，比现在落后好几轮 —— 会连带丢掉第三轮。
所以真要整文件回退，先 `git diff > /tmp/parse.diff` 留底，再手工摘。

**顺序**：
1. 覆盖 .py → `python3 -c "import ast; ast.parse(open(f).read())"` 过语法
2. `sudo systemctl daemon-reload`（**改过 unit 就必须做这一步**，否则重启的还是旧参数）
3. `sudo systemctl restart dormant-probe.timer`；daemon 用 `kill <MainPID>` 让 systemd 拉起。改过 unit 之后顺手核一眼 `systemctl show dormant-probe.timer -p NextElapseUSecRealtime` —— `OnCalendar` + `Persistent=true` 会按新时间重算，别以为"重启了就等于按原计划"
4. 状态文件**不必删**（格式没变）。只有回滚到"锁在 /tmp"那一版时，项目目录里的
   `.dormant_probe.lock` 会变成孤儿，删掉即可
5. 回滚会丢掉：统计通道、24h 滚动窗口护栏（旧的是棘轮，会永久拒收）、临时实例失败
   尾部日志与换端口重试、真锁、`ExecStopPost` 时间戳、日志兜底与副作用闸
