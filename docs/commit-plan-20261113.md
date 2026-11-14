# 提交拆分计划（2026-11-13 18:2x，供逐块审 diff）

背景：工作区混着三路改动——**台账生命周期**（ledger-dev）、**配置漂移集成**（drift-dev + integrator）、**并行 WIP**（probe 传 ip / gate 退避，责任人 oahcom）。其中台账的 `daemon.py` 改动已被并行提交（`566ba06` 起）**整包收编进历史**，本计划只覆盖**仍在工作区**的部分。

混杂文件（同一文件里两方改动交错）必须 **hunk 级拆分**再暂存，方法：

```
git diff -U3 <file> > p.patch     # 按 @@ 块切成两份（ours / theirs）
git apply --cached ours.patch     # 只暂存归属本笔的 hunk
```

---

## 第一笔【紧急】修 HEAD 自洽：台账补入库

| 文件 | 内容 | 说明 |
|---|---|---|
| `asip/ledger_policy.py` | 释放时刻/判定/续命的单一事实源（untracked） | **HEAD 的 daemon.py 已 import 它**——不补入，stash/新检出即 ImportError |
| `test_ledger_lifecycle.py` | T1–T8 + 不变量共 10 例 | 配套回归网 |
| `tools/channel_report.py` | `blocked_ip_summary` 委托 `ledger_policy` | 对外输出格式不变 |
| `config.py`（**拆 hunk**） | 仅 `ledger_retention_sec` / `ledger_cleanup_mode` 两键（含注释） | 同文件里 `proxy_gate_backoff_*` 6 键是并行方的，**不进本笔** |

提交信息建议：`fix(asip): 台账单一事实源补入库——asip/ledger_policy.py 曾被 566ba06 起的提交引用但漏 add`

## 第二笔 配置漂移集成

| 文件 / hunk | 内容 |
|---|---|
| `daemon.py` @@ -10724 | `reload_singbox_config` docstring 改 INC-017 口径（D-2） |
| `daemon.py` @@ -12928 | `_slot_members_drift` 委托 `compare_members`（D-1 收口） |
| `daemon.py` @@ -15383..-15440（5 个 hunk） | 自检分类七 `selector_drift` / `selector_unverifiable` |
| `daemon.py` @@ -56（**拆行**） | `from asip import config_projection`——同块混有并行方的 `gate_rotation_policy` import，只取本行 |
| `asip/config_projection.py` | 三面纯比对单一事实源（untracked） |
| `tools/asip_audit.py` | `--doc-drift` / `--runtime-drift`（519 行新增） |
| `test_config_projection.py` / `test_config_drift_inspect.py` / `test_asip_audit_doc_drift.py` | 9 + 5 + 15 例（untracked） |
| `test_config_consistency.py` | autouse 隔离 fixture（监督 F-3 已接受） |
| `CLAUDE.md`（**拆 hunk**）+ `docs/subsystems/switching.md` | `proactive_after_dsv4_ok` 150→300 文档修复（CLAUDE.md 里并行方另有改动） |

提交信息建议：`feat(asip): 配置漂移收口——config_projection 单一事实源 + 自检分类七 + doc-drift 关卡`

## 第三笔 文档与测试桩对齐

| 文件 / hunk | 内容 |
|---|---|
| `docs/glossary.md`（**拆 hunk**） | 「六之三 配置六面」+ `expires_at` 行；同文件并行方加了「渠道轮换」一节，**不进本笔** |
| `docs/established-facts.md`（**拆 hunk**） | 第 32/33 条；同文件并行方的 30/31 条是他们的 |
| `docs/design-20261113-ledger-lifecycle.md` / `-config-drift.md` / `patch-20261113-drift-daemon-integration.md` / `review-20261113-impl-3x6.md` / `runbook/ledger-cleanup-switch.md` | 设计、集成规格、监督报告、runbook（均 untracked） |
| `test_daemon_unit.py`（**拆 hunk**） | 3 处 probe 桩签名对齐（4663/4681/4704）；同文件并行方有 probe-ip 桩改动，**不进本笔** |
| `test_warp_refresh.py` | 9 处 probe 桩签名对齐（本文件改动全是我们对齐的，可整文件进） |

提交信息建议：`docs+test(asip): 三方六维设计/监督文档 + probe 桩签名对齐`

## 不动（并行 WIP，留给其责任人提交）

`probe_warp.py`、`test_probe_warp.py`、`conftest.py`、`deploy/asip-cli.sh`、`test_asip_cli_gate.py`、`tools/trace_req.py`、`test_trace_req.py`、`asip/gate_rotation_policy.py`、`test_gate_backoff.py`、`test_gate_rotation_policy.py`、`docs/cf-repair-and-per-ip-usage-20261112.md`、`docs/opencode-429-limit-model-20261112.md`；以及混杂文件里属于他们的 hunk（daemon.py 的 -7797/-12416/-14419×4、config.py 的 `proxy_gate_backoff_*`、CLAUDE.md/glossary/established-facts/test_daemon_unit 里标注「并行方」的部分）。

---

## 提交顺序与依赖

第一笔独立且最急（修 HEAD 自洽）→ 第二笔依赖第一笔（`config_projection` 与 `ledger_policy` 同属 asip 包）→ 第三笔收尾。三笔提交前后各跑一次全量 pytest（当前基线 **1319 passed / 0 failed**）。
