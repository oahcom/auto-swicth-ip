# 状态库部署清单（P3 / T2）

库是**回滚点**，放错文件系统等于回滚点不可信。以下四条是硬约束，缺一条不得上线。

## 1. 库位置：必须是 Linux 本地盘

```
db_path = <repo>/run/state.sqlite3      # 默认，ext4
```

- 禁止 `/mnt/c/**`、任何 9p / drvfs / virtiofs / nfs / cifs 挂载。原因：SQLite WAL
  依赖 `fcntl` 字节锁与进程间共享内存（`-shm`），9p 不保证锁语义，崩溃后 WAL
  可能无法回放 → 台账回滚点本身损坏。
- 打开时自检（`asip/state_store/store.py::StoreError`）：从 `/proc/mounts` 取最
  长前缀挂载点判类型，命中远端类型直接拒开，不降级、不静默改用 DELETE 日志。
- 上线前取证：

```
stat -f -c '%T' run          # 期望 ext2/ext3（ext4 家族）
awk '$2=="/"{print $1,$3}' /proc/mounts
```

实测：`run/` → `ext2/ext3`，根挂载 `/dev/sdd ext4`。

## 2. 建库只走迁移器，daemon 不建库

```
python3 deploy/asip-release.py migrate up      # 应用 001_state_store_baseline
python3 deploy/asip-release.py migrate status  # 查已应用版本
```

- schema 版本单调递增，迁移实现登记在 `deploy/migrations/NNN_*.py` + `asip/state_store/schema.py::MIGRATIONS`。
- 库里有更新版本时报 `StoreError` 拒开（防止旧代码按新库解释、静默丢列）。
- daemon 若发现库不存在：影子模式视为「未启用」返回 None，**绝不自己建库**——
  否则 schema 版本变成「谁先启动谁定」。

## 3. 影子开关（默认关）

```
ASIP_STATE_SHADOW=1     # 开双写+周期 diff+差异告警（观察窗用）
ASIP_STATE_JOURNAL=0    # 关事件流水入表，回退 JSONL（默认开）
ASIP_STATE_DB=...       # 覆盖库路径（默认 run/state.sqlite3）
```

影子差异必须为 0 并持续一个观察窗，才允许进切读（T2c）。

## 4. 回滚演练（切读前必须真跑一次）

```
python3 deploy/state-rollback-drill.py    # 导出→冻结→回读→篡改检出
python3 deploy/state-shadow-drill.py      # 载入器 vs 库 逐行对账
```

演练只用临时库，不碰生产。冻结用 `deploy/state-projection.py` 的侧车 sha256。

## 5. 目录与权限

- `run/` 700、`run/state.sqlite3` 600（库里有出口地址与封禁证据，与台账同密级）。
- 台账保留/归档以库为准：热工作集留表内，冷归档走后台低优先车道，不得与
  `/rotate` 抢 IO，不得排进换线时间线。

## 6. IP 台账增量 WAL（2026-11-27）

`.warp_ip_used.json` 的热路径不再整表替换。权威状态由旧格式 JSON 快照与
`.warp_ip_used.json.wal` 组成：每次 upsert/delete 追加一条 JSONL delta，文件
`fsync` 成功后才返回；启动先读快照再按顺序 replay。完整损坏行 fail-closed，
仅允许截断未完成的最后一行。

默认每 1024 条 delta 或 WAL 达 4 MiB 时 compact：写临时快照 → fsync 文件 →
rename → fsync 目录 → truncate+fsync WAL。若在 rename 后、truncate 前崩溃，旧 WAL
会被再次 replay；upsert/delete 幂等，因此不会丢失或复活烧毁地址。

- `deploy/ledger-wal.py status`：只读查看快照/WAL 大小与 pending 行数；
- `deploy/ledger-wal.py compact`：要求 `auto-switch-ip.service` 已停止，将快照+WAL
  合并为旧代码可读的 JSON；
- `state-projection export` 在 WAL 非空或 daemon 仍可能追加时拒绝导出；
- 回滚到不支持 WAL 的代码前必须停止 daemon、compact 并确认 `wal_bytes=0`。
  `asip-release.py rollback` 和 `remote-deploy.sh` 都有 fail-closed 检查。

SQLite shadow（若显式开启）首次启动时全量 seed，后续使用 `apply_delta` 只镜像变化行；
`replace_rows` 不再位于每次 IP 记账热路径。WAL+JSON 是当前 IP 台账 source of truth，
SQLite 仅是可重建影子；它的短暂落后不会覆盖 WAL 中已 fsync 的变更。

### 6.1 10 分钟等频基准（2026-11-27）

使用 `benchmarks/bench_ledger_wal.py` 在 `/tmp` 临时目录跑合成 47,665 行台账，
按 4 updates/sec 连续 600 秒，共 2,400 次单行更新；full-rewrite 基线按现场
快照 16,912,942 bytes/次计算。基准结束前先 replay 比对内存态，再执行一次最终
compact 验证旧 JSON 回滚投影；最终临时目录已删除。

| 指标 | 结果 |
|---|---:|
| full JSON rewrite 基线 | 40,591,060,800 bytes（约 40.59 GB） |
| WAL delta append 总量 | 872,302 bytes |
| 周期 compact | 2 次，共 29,301,020 bytes |
| 稳态逻辑写入（delta + 周期 compact） | 30,173,322 bytes，较基线减少 **99.9257%** |
| 加上最终回滚投影 compact | 44,824,493 bytes，较基线减少 **99.8896%** |
| append 延迟 | p50 0.194 ms / p95 0.339 ms |
| 周期 compact 延迟 | p50 397.3 ms / p95 405.0 ms（2 次样本） |
| 最终 replay / JSON projection | 均通过；compact 后 WAL=0 |

基准窗口底层设备总写入约 5.12 GiB，但该值包含同时运行的生产服务和其他主机负载，
不能归因给基准进程；性能比较使用 WAL/快照文件的精确逻辑写入字节数。基准脚本
保留为可复现工具，正式验收仍需在生产变更窗口观察真实 WAL 与设备写入趋势。
