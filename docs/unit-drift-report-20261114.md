# 线上 systemd unit 与模板差异对照报告（T5/W1，只读对照，未改线上）

> 2026-11-14 | 数据来源：`deploy/gen-units.py --check --unit-dir /etc/systemd/system` 实跑（40 条差异）
> + 人工取证（drop-in、h2/wg 生成器模板行号、.bak 计数）。对照口径：**模板=生成器渲染产物**；
> 线上=/etc/systemd/system 现存文件。本报告只陈述事实与建议裁决，**不改线上任何文件**。

---

## 一、为何要做这份对照（逻辑链）

单元内容历史上以 heredoc 写死在 deploy/install.sh，而线上单元被手改后与模板各自演化——
「配置双主人」在 unit 层重演：改了线上没人知道模板是否还适用，跑了 install.sh 又会把手改
覆盖掉（install.sh 自己的注释承认「本机的 auto-switch-ip.service 不是 deploy/install.sh
生成的，是手工维护的旧版」，precheck/killmode 两个 drop-in 正是为绕开覆盖而生）。版本化
内核要能把「发布→回退」做实，前提是**单元有一个唯一真相源**，漂移能被机器指认。
本波已把 install.sh 族渲染收口到 `deploy/gen-units.py`（与旧 heredoc 逐字节等价，演练
`diff -r` 退出码 0），本报告收口后的第一次全量漂移清点。

## 二、unit 的四个来源（多源现状，机制图）

| 来源 | 产出 | 现状 |
|---|---|---|
| deploy/install.sh → **deploy/gen-units.py**（本波收口） | auto-switch-ip.service、asi-selfcheck.\\*、usque-warp\\*.service | 模板单一真相源已立 |
| deploy/wg-lines-install.sh（行 123-137 自带 StartLimitIntervalSec=0、fuser -k 预清理、LimitNOFILE） | warp-wg-\\*.service | 独立生成器，未收编（W4 治理项） |
| deploy/render_h2_lines.py（行 746-757 自带 StartLimitIntervalSec=0）→ h2-install.sh 安装 | usque-h2-\\*.service、asip-h2-singbox.service | 独立生成器，未收编（W4 治理项） |
| **手改 + drop-in** | 密钥直写、--interval、HOME=、Restart=no override、.bak 备份 ×16 | 本报告主对象 |

## 三、差异对照（逐条：模板值 → 线上值 → 性质 → 归类 → 建议裁决）

### 3.1 auto-switch-ip.service（主单元，手改最重）

| # | 项 | 模板（gen-units.py） | 线上 | 性质与影响 | 归类 | 建议裁决 |
|---|---|---|---|---|---|---|
| 1 | ExecStart | `/usr/bin/python3 …/daemon.py` | `/usr/bin/env python3 …/daemon.py --interval 10` | 两处手改：①解释器从钉死绝对路径变 `env python3`（PATH 依赖回归——模板注释明写 conda 场景重启后解释器漂移的事故）②`--interval 10` 是部署实配参数 | ①C 坏行为（回潮）②B 待决参数 | ①重生成时回归钉死解释器；②**不是「纠正回默认」**——按 A11 双钉先例，部署实配应显式入生成器参数/配置输入，两口径都保留，禁止静默丢掉 10 |
| 2 | Environment=**SINGBOX_SECRET=…**（43 字符） | 无（密钥走 ~/.singbox_secret 文件） | 直写 unit | 密钥的权威来源是 `config.get_singbox_secret()`（只读 ~/.singbox_secret，600 权限、mtime 轮换机制）；unit 直写绕过该机制：unit 文件 0644 可读、且 /etc/systemd/system 下 16 个 .bak 备份件同样含密钥。仓内（daemon.py/config.py/deploy//tools/）**无 `os.environ["SINGBOX_SECRET"]` 消费点**，疑似历史残留死配置 | C 已知坏行为（泄漏面） | 待人裁决后从 unit 移除、密钥归 ~/.singbox_secret 单一来源；执行属运维动作（涉及在跑服务），本波不动 |
| 3 | Environment=**NINE_ROUTER_PASSWORD=…**（明文口令） | 无 | 直写 unit | 同上；仓内无消费点（9router 侧是否读 env 未证） | C（泄漏面） | 同上；口令应入 9router 自身凭据管理，不入 asip 单元 |
| 4 | Environment=**TZ=Asia/Shanghai** | 有（注释：夜锁按 localtime 判窗，TZ 漂移=白天锁夜里不锁） | **缺失**（.env 也无 TZ） | 条件性风险：当前主机时区恰为 UTC+8 未触发；WSL 重建/容器化时夜锁整体偏 8h，恰是这套锁要修的事故形态，且只在特定时段可见 | C（护栏缺失） | 重生成单元时补回（模板已含）；或 .env 显式 TZ（二选一，勿双处） |
| 5 | KillMode=process（**A13 有意权衡**） | 模板本体含 | 本体缺，由 drop-in killmode.conf 补 | 语义等价（drop-in 叠加后生效），但因「怕被 --units-only 覆盖」而双处维护 | B（结构漂移） | 生成器已含 → 重生成后 drop-in 退场；A13 语义一字不动 |
| 6 | User/Group/EnvironmentFile=-…/.env | 模板本体含 | 本体缺，由 user.conf / align-sub.conf 两个 drop-in 补 | 同上 | B（结构漂移） | 同上，重生成后 drop-in 退场 |
| 7 | WantedBy | multi-user.target | default.target | 手改；系统级服务惯例是 multi-user.target，default.target 亦可工作但两处不一致 | B（口径不一） | 统一 multi-user.target |
| 8 | After= | network-online.target | `network-online.target sing-box.service` | sing-box.service 存在（inactive dead，56 节点池/7890，与 daemon 自拉的主 sing-box 不是一个实体）；仅启动排序，语义影响小 | B（待决） | 待决是否回收进模板；若该 unit 是历史遗留实体，随三张归属表（W4）一并裁决 |

### 3.2 asi-selfcheck 族

| # | 项 | 模板 | 线上 | 性质 | 归类 | 裁决 |
|---|---|---|---|---|---|---|
| 9 | asi-selfcheck.service 多键 `Environment=HOME=…` | 无 | 手改补 | 自检脚本家目录按 uid 推（config 同款逻辑），HOME env 影响有限 | B（观察） | 待决是否回收进模板 |
| 10 | asi-selfcheck.timer WantedBy | **multi-user.target** | timers.target | **反向漂移：线上手改是修正、模板是缺陷**——timer 应挂 timers.target，挂 multi-user.target 语义错误 | C（模板缺陷） | 显式决策后**改生成器模板**（不是改线上）；注：本波收口保持逐字节等价未顺带修，等价性边界即在此 |

### 3.3 usque-warp\\*.service（9 个）

| # | 项 | 模板 | 线上 | 性质 | 归类 | 裁决 |
|---|---|---|---|---|---|---|
| 11 | 多键 `Environment=HOME=/home/administrator` | 无 | 手改补（9/9） | 一致性手改，用途待证（usque-launch 是否依赖 HOME 未证） | B（待决） | 待决回收进模板 |
| 12 | Description 里 tag 命名 | `WARP-CF4-B` 等（现行 members.py） | `WARP-CF-B`/`usque-warp-c` 等旧命名 | 纯文案；但证明**单元生成于旧 members 表后未随成员表重生成** | B（陈旧） | `install.sh --units-only` 重生成即刷新；tag 以 config 实配为准 |

### 3.4 10-precheck.conf（部署前关卡 drop-in）

| # | 项 | 仓库版 | 线上版 | 性质 | 裁决 |
|---|---|---|---|---|---|
| 13 | ExecStartPre 检查范围 | 6 个核心模块（daemon/dormant_probe/config/warp_wg/warp_blocks/parse_subscription） | **仅 daemon.py** | 线上落后仓库：仓库注释明写「只查主文件时，其他模块的未定义名照样掀翻服务」（2026-11-11 事故家族） | install.sh 每次下发即覆盖为 6 模块版（install.sh 现行为，无需人工）；**建议尽快跑一次 --units-only 拉平**（含 #1-#12 需先裁决） |

### 3.5 其他生成器族（warp-wg / usque-h2，未收编）

| # | 项 | 事实 | 归类 |
|---|---|---|---|
| 14 | warp-wg-1.service.d/override.conf `Restart=no` | 22 条 warp-wg 单元中**仅 1 号**有此 drop-in（手改）——行为劈叉：1 号线判脏不自动拉起、其余 21 条会 | 手改劈叉，建议随 W4 归属表裁决 |
| 15 | warp-wg-\\* `Environment=HOME=/root` | 生成器写 `HOME=$HOME_DIR`，/root 是以 root 运行 wg-lines-install.sh 的产物（remote-deploy 注释已知坑：ASIP_RUN_USER 必须显式传） | 调用上下文漂移（非手改）；收编时参数化 |
| 16 | StartLimitIntervalSec=0、fuser -k、LimitNOFILE | wg-lines-install.sh / render_h2_lines.py **生成器自带**（非手改），防 start-limit-hit 与端口占用 | 生成器内护栏，收编进 gen-units 时原样保留 |
| 17 | /etc/systemd/system 下 16 个 \\*.bak | 手改前随手备份的旧单元（含 #2 密钥副本） | 手改文化佐证；版本化快照应取代 .bak 习惯（**本波不清理**） |

## 四、结论与待裁决清单（按最根本排序）

1. **密钥直写（#2/#3）**：泄漏面 + 双主人，建议移除、密钥归 ~/.singbox_secret——涉在跑服务，待人批后执行；
2. **TZ 缺失（#4）**：条件性事故（夜锁偏 8h），重生成即修；
3. **--interval（#1②）**：按双钉原则参数化保留，不做「纠正」；
4. **模板缺陷 timer WantedBy（#10）**：改生成器不改线上；
5. **drop-in 退场（#5/#6）与 WantedBy 统一（#7）**：重生成自然完成；
6. **warp-wg-1 Restart=no 劈叉（#14）与 HOME=/root（#15）**：随 W4 三张归属表裁决；
7. **执行方式**：全部通过 `gen-units.py --write`（install.sh --units-only 路径）+ 人工确认后重启，禁止手改 unit 文件——这正是版本化内核存在的理由。

**本波已做**：模板单一真相源（gen-units.py）+ 机器对照通道（`--check`，退出码即漂移信号）。
**本波未做**（有意）：不动线上任何文件；不重生成线上单元；不清理 .bak。
