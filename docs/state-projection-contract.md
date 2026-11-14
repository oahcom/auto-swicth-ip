# 状态投影工具接口约定（T5/W1 预留，P3 接线用）

> 2026-11-14 | 工具：`deploy/state-projection.py`；配套：`deploy/asip-release.py`（版本/迁移骨架）
> 性质：**接口先行、接线后至**——W1 把回滚契约的「形状」钉死并真实演练，P3 落地 SQLite
> 后在此加数据源，封套不变。对应《实施方案规划》阶段 2 验收：「回滚演练通过（导出投影→
> 冻结→回读一致）」。

---

## 一、逻辑链（为何是这个形状）

阶段 2「状态权威切读」的不可逆点是**停止读 JSON**。回退路径=「库导出 JSON 投影→冻结→
回读」：切读出问题时，把库的内容投影成 JSON（旧代码读得懂的形态）、**冻结**成不可变
回滚点、再**回读**校验一致性。三步各自防一种事故：

- **导出投影**防「切读后旧形态丢失」：投影是库在某时刻的完整自证快照（逐条 sha256）；
- **冻结**防「回滚点自己被改」：侧车哈希 + 去写位，回滚点不可变才叫回滚点（与
  asip-release 的快照树同一语义）；
- **回读**防「回了个坏的」：还原前先验侧车与逐条哈希，目标已存在拒写（防覆盖活数据），
  条目名防路径穿越。

W1 用现有 JSON 状态文件当数据源把三步走通；P3 的 SQLite 是**新数据源**，不是新契约。

## 二、调用约定（CLI）

```
# 1) 导出投影（只读状态根；原子落盘：临时文件+rename）
python3 deploy/state-projection.py export --state-root <状态根> --out <投影.json>
    [--include GLOB ...] [--no-default]

# 2) 冻结（自证→侧车 .sha256→去写位）
python3 deploy/state-projection.py freeze <投影.json>

# 3) 回读（先验后写）
python3 deploy/state-projection.py readback <投影.json> --into <目标目录> [--force]
python3 deploy/state-projection.py readback <投影.json> --verify-only   # 只验不写
```

退出码：0 成功；1 校验失败/目标冲突；2 用法错误。
**P3 程序化调用**同语义（可直接 import 三个函数 `cmd_export/cmd_freeze/cmd_readback`
或复刻 CLI）；新增 `--source sqlite:<db>` 时**只增数据源、不动封套**。

## 三、投影封套 schema（asip-state-projection/1）

```
{
  "schema": "asip-state-projection/1",
  "created_at": "<UTC ISO8601>",
  "source": {"type": "json-files", "root": "<绝对路径>"},
  "includes": [<生效的 glob 列表>],
  "entries": [
    {"name": "<状态根下相对路径，/ 分隔>",
     "sha256": "<data 的哈希>",
     "size": <字节数>,
     "mtime": <float>,
     "data": "<原文文本（JSONL 存原文）>"}
  ]
}
```

字段约定：
- `entries` 按 name 升序——**稳定有序**，同一状态两次投影可逐条 diff；
- `sha256/size` 以 `data` 的 UTF-8（surrogateescape）字节计——封套自证，与侧车互补；
- `source.type` 预留 `"sqlite"`（P3）：届时 entries 的 name=表名/查询名，data=表内容
  的 JSON 序列化文本，哈希语义不变；`source` 里带库文件哈希与 schema 版本。

## 四、默认状态文件集（glob 按路径段匹配）

`.bad_nodes.json`、`.monitor_state.json`、`.egress_log.jsonl`、`.warp_ip_used.json`、
`.warp_ip_cycles.json`、`.warp_ip_history.jsonl`、`.warp_line_state.json`、
`.warp_proxy_fallback.json`、`.opencode_gate_ledger.jsonl`、`.warp/*.json`（直接子级）、
`.warp/h2/*.json`——与 .gitignore「运行时状态文件」段同口径。

**已知边界（P3 接线时可收窄）**：
1. `.warp/h2/singbox.json` 是派生产物且内嵌 Clash secret，被 `.warp/h2/*.json` 命中——
   投影文件以 0600 落盘、冻结后去写位；P3 可改为白名单只留 `.warp/h2/channels.json`；
2. 大台账不裁剪（.warp_ip_used.json ~14.9MB、.warp_line_state.json ~2.9MB、
   .warp_ip_history.jsonl ~3.8MB）——投影体积 ~22MB 属预期，回滚点完整性优先于体积；
3. 设备凭据（`.warp/*/config.json`、`wglines/*/singbox.json`）**不进投影**（glob 路径段
   匹配已排除）；密钥域不归状态回滚管。

## 五、与版本化/迁移骨架的接口

- `deploy/asip-release.py migrate`：`deploy/migrations/NNN_名字.py` 定义 `up()/down()`，
  应用台账在 `$ASIP_RELEASE_ROOT/migrations.json`，逐个应用逐个记账（崩溃断点可续）。
  **P3 的 SQLite schema 迁移登记在此**：一次 schema 迁移=一个 migration 文件；
- `asip-release.py rollback --with-migrations`：代码回滚时连同迁移态回退到该发布时刻
  （manifest 记录 `migrations_applied`）；状态内容的回退走本投影工具，两者职责分开：
  **版本域（代码）归 asip-release，状态域（数据）归 state-projection**，互不覆盖；
- 顺序建议（P3 切读前）：`export`（切读前状态）→`freeze`（回滚点）→ 切读 → 出问题
  `readback --into` 到隔离目录核对 → 人工确认后替换状态根（先停写者）。

## 六、演练记录（2026-11-14 真实执行，只读真实状态根 + 回读落演练目录）

- export：23 条目 / 原始字节 22,564,406（含三大台账）；凭据文件 0 纳入；
- freeze：侧车 sha256=e1939aa5f8b5…、文件去写位（-r--------）；
- readback --verify-only：23 条目校验通过；readback 落盘 23 个条目，`find -name config.json`=0；
- 防覆盖闸：目标已存在拒写（退出码 1，--force 才覆盖）；
- 篡改检测：冻结后追加 1 字节 → 侧车哈希不符、回读拒绝（退出码 1，目标目录未动）。
