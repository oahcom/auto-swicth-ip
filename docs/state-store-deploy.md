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
