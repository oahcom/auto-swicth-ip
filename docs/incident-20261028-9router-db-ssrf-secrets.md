# 2026-10-28 9router 三起事故记录

记录人：opencode（无子代理，全部结论带实测命令）
范围：`~/.9router`（分支 `master-oa`）；涉及提交 `e157a608`、`472d524d`
关联：`~/.hermes/profiles/sister/reports/third-party-audit-20261028-r1.md`

## 事故一：库被精简库顶替，9 条 providerConnections 丢失

**现象**：09:34 重启后 `9router1/remote` 返回 `404 No active credentials for provider: 9router1`，`/v1/models` 从 858 条掉到 81 条。

**证据**

| 表 | 事故中现库(192KB) | `db/backups/schema-2-to-16-.../data.sqlite`(70MB, 09:04:11) | `db/forensics-20261028-0910/live-data.sqlite`(112MB, 09:10) |
|---|---|---|---|
| providerConnections | 0 | 8 | 9 |
| providerNodes | 0 | 3 | 4 |
| proxyPools | 3 | 7 | 6 |
| apiKeys | 1 | 1 | 2 |
| combos | 1 | 5 | 6 |
| kv | 4 | 26 | 27 |

时间线：09:00 旧进程仍在正常服务（`logs/start-20261028-0841.log` 有 combo 流量）→ 09:04:11 `db/data.sqlite` 被换成精简库，同秒写入 70MB 备份与 `auth/cli-secret`、`jwt-secret` → 09:10 旧 inode 被复制到 `db/forensics-20261028-0910/` → 09:34 按正常流程重启，进程加载精简库 → 连接消失。

**根因**：未确定。精简库的构成（保留 apiKeys/customModels/combos/活跃 pool，唯独没有 providerConnections/providerNodes）与「剔除运行时数据+凭据」的导出脚本高度一致，但仓库里没有该脚本残留，所以只记「高度疑似」。

**恢复**（已执行并验证）：
```bash
kill $(ss -ltnp | grep ':20127' | grep -oP 'pid=\K[0-9]+' | head -1)
cp db/data.sqlite db/data.sqlite.partial-20261028-0935
rm -f db/data.sqlite-wal db/data.sqlite-shm
cp /tmp/snap.sqlite db/data.sqlite      # 由 forensics 快照 checkpoint 而来
./restart_nine_router.sh
```
验证：`pragma integrity_check=ok`、`providerConnections=9`、`/v1/models` 200 且含 `9router1/remote`、`9router1/remote` 200（1.298s）。

## 事故二：SSRF 修复第一版是假修

**第一版 `e157a608`**：把 `169.254.0.0/16`、`fe80::/10`、`metadata.google.internal` 保留在代理路径上，理由是「这类地址只能靠代理挡住」。

**证伪过程**：把 aihub 连接的 `providerSpecificData.baseUrl` 临时改成 `http://169.254.169.254:80/v1`，调用 `aihub/deepseek-v4-flash`：

```
curl --max-time 45 ... → HTTP=000 t=45.0s
日志: [ProxyFetch] Proxy failed, falling back to direct: fetch failed
```

`open-sse/utils/proxyFetch.js` 的 catch 里有无条件回落 `return fetchWithTlsFallback(url, options, null)`，代理连不上就直连重试 —— 元数据依然可读，等于没修。

**第二版 `472d524d`**：在 `proxyAwareFetch` 入口硬拒（503），不代理也不直连。复测同一连接：

```
HTTP=503 t=9.0s
{"error":{"message":"[...] [502]: [ProxyFetch] refused link-local/metadata target: 169.254.169.254"}}
日志无 falling back to direct
```
回归：`9router1/remote` 200（4.198s）；`192.168.1.97`/`10.x`/`nas.lan` 仍直连（41 条用例）；探针用的 baseUrl 已还原为 `https://aihub365.top/api/proxy/v1`。

## 事故三：密钥进入已推送的历史

`auth/cli-secret`、`jwt-secret`、`machine-id`、`model-catalog*.json` 在 `55f31496`（已推送）里被跟踪，`4bdbb691` 才 `git rm --cached` + `.gitignore`。远端历史仍含明文：
- `jwt-secret` 签 dashboard 会话 cookie；
- `auth/cli-secret` + `machine-id` 派生 CLI token（`sha256(machine-id + '9r-cli-auth' + cli-secret)`）。

处置待定：① 只轮换（删三个文件后重启，应用重新生成；dashboard 会话与旧 CLI token 失效，agent 流量走 apiKeys 不受影响）；② `filter-repo` 改历史后 force-push（仓库有 `zeusire/master` 合并记录，会影响其他克隆）。

## 三条教训

**L1「保留代理」不等于「阻断」**。只要下层有 `fallback to direct`，任何「必须走代理」的策略都是纸糊的。安全策略要在**入口拒绝**，不能靠路由偏好。判据必须是端到端行为（日志/HTTP/耗时），不是代码阅读。

**L2 进程重启不校验库完整性**。`providerConnections=0` 也能正常启动，只在请求时 404，属于静默降级。另外部分单测直接读写 `$HOME/.9router/db/data.sqlite`（未设 `DATA_DIR`），测试与生产库耦合，是这类事故的高危放大器。

**L3 时间戳不能用来自证部署顺序**。同一 pid 的启动时刻两次读到 `08:40:45` / `08:39:31`，其 stdout 日志 birth 是 `08:41:13`，BUILD_ID mtime `08:40:48` —— 四个读数无法同时成立，原因未查明。作为对照，当前进程内核启动 `09:37:40` 与日志 birth `09:37:40.869` 一致，说明不是整体性时钟漂移。部署判据改用内容+行为：chunk 里有本次新增字符串，且运行时观测到新行为。

## 复现命令

```bash
# 库状态对比
sqlite3 db/data.sqlite "select count(*) from providerConnections;"
sqlite3 db/forensics-20261028-0910/live-data.sqlite "select count(*) from providerConnections;"

# 链路本地是否被拒（需要一条连接，把 baseUrl 临时指过去）
curl -s -o /tmp/x.json -w "%{http_code} %{time_total}\n" --max-time 30 \
  -X POST http://127.0.0.1:20127/v1/chat/completions \
  -H "Content-Type: application/json" -H "Authorization: Bearer 9router-local" \
  -d '{"model":"aihub/deepseek-v4-flash","messages":[{"role":"user","content":"hi"}]}'
# 期望：503 且错误体含 refused link-local/metadata target；日志无 falling back to direct

# 单测口径（不要直接比「failed」数字，先确认口径）
cd tests && npx --no-install vitest run unit --exclude "**/*.live.test.js"
```
