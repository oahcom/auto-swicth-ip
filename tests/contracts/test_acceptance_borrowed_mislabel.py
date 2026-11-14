"""③组验收测试 · borrowed 恒误标（9router 三发循环跨端口置位误检）。

【三分法】C 已知坏行为：修「何时置位/取值来源」，不动「置位后怎么处理」
（后者是①组保真契约，见 test_fidelity_borrowed_skip.py）。
**禁止钉成契约**：本组语义是「误标消失」的验收判据，不是长期基线——
修复被接受并迁移完后应降级或改写。
【防什么事故】三发循环跨端口重试恒判 borrowed=1 → daemon 走 borrowed_skip
不标脏不换线，被限 IP 不进 24h 台账、后续请求反复撞墙（2026-11-14 实测
36/57 ERROR 429 全是 borrowed_skip）。
【live 复核结论（2026-11-14）】本坏行为已于当日 04:50 commit 723f0a27
（9router 仓）修复：换线环自记账改 `borrowed=false`（重试已 sessionRotate
重建凭据，body/session 属当前端口），重放路径改按 credsRebuilt 判借道。
审视文档「现行仍漏 40.2%」系修复前旧口径、未重验。因此本组当前应为绿
（修后绿）；仍留③组防回退（若有人把判据换回「与原始端口比对」即转红）。
"""
import os
import re
from pathlib import Path

import pytest

OPENCODE_JS = Path(os.environ.get(
    "ASIP_OPENCODE_JS",
    str(Path.home() / ".9router/open-sse/executors/opencode.js")))

pytestmark = pytest.mark.skipif(
    not OPENCODE_JS.exists(), reason="9router sidecar 源不可达（离线可跳过）")


def _read() -> str:
    return OPENCODE_JS.read_text(encoding="utf-8")


def test_retry_loop_accounting_not_marked_borrowed():
    """换线环自身的 429 记账不得再按「跨端口=借道」恒判 borrowed=1。

    修前红：旧判据 `borrowed: Boolean(targetProxyUrl) && targetProxyUrl !==
    requestProxyUrl`（requestProxyUrl 是 executeInner 开头的原始端口、循环内
    从不更新）恒真。修后绿：循环内记账常量 false 或不传 borrowed。
    """
    text = _read()
    # 反回退判据：不得再出现「与（从不更新的）原始端口比对」形态的置位
    stale = re.findall(
        r"borrowed\s*[:=][^\n]*requestProxyUrl", text)
    assert stale == [], f"疑似回退到旧误标判据: {stale}"
    # 修复形态：换线环自记账显式 false（三发循环自身的记账按本闸失败处理）
    assert re.search(r"const\s+borrowed\s*=\s*false", text), \
        "换线环自记账应显式 borrowed=false（723f0a27 修复形态）"


def test_replay_borrowed_requires_stale_credentials():
    """重放路径借道判据=「凭据未重建 且 打到非原端口」（credsRebuilt 语义）。
    """
    text = _read()
    assert "credsRebuilt" in text
    assert re.search(r"borrowed:\s*!credsRebuilt", text), \
        "重放 429 记账须按 credsRebuilt 判借道"
