#!/bin/bash
set -e
cd /home/administrator/auto-swicth-ip || exit 1
DOC=docs/warp-channel-allocation.md
cp -a "$DOC" "$DOC.bak-pre-h2revive-$(date +%Y%m%d-%H%M%S)"

python3 - <<'PY'
import re
p = "docs/warp-channel-allocation.md"
s = open(p, encoding="utf-8").read()

mark = "> **⚠️ 已被推翻（2026-11-02 16:00）**：本节结论「CF 的 MASQUE 只在 HTTP/3 / " \
       "TCP 路线客户端侧无解」是错的。真正挡路的是**机场中继按 SNI 掐断**——" \
       "`consumer-masque.cloudflareclient.com` 在 ClientHello 阶段就被 RST，而 " \
       "`-noservername` 握手正常。usque 的 `-s` 默认值恰是这个 SNI，所以" \
       "`--http2` 不传 `-s ''` 必然失败。另需用 RFC 7540 普通 CONNECT（服务器不发 " \
       "SETTINGS_ENABLE_CONNECT_PROTOCOL）。**H2 已复活，全量 44/65 可承载**，" \
       "详见文末「H2 路线复活」。\n\n"

targets = [
    "### H2 路线终局：CF 的 MASQUE 只在 HTTP/3，H2 是私有实现",
    "### 定性：这家机场的 TCP 中继是 SNI 路由器，TCP 路线客户端侧无解",
    "### 走 HTTP/2 over TCP 的那条路：通到最后一公里被 SNI 拦",
]
n = 0
for t in targets:
    i = s.find(t)
    if i < 0:
        print("MISS:", t)
        continue
    if mark in s[max(0, i-40):i+len(mark)+40]:
        print("ALREADY:", t)
        continue
    s = s[:i] + mark + s[i:]
    n += 1
    print("MARKED:", t)

open(p, "w", encoding="utf-8").write(s)
print("marked", n)
PY

# 追加新章节
cat docs/_h2sec.md >> "$DOC"
echo "=== 结果 ==="
wc -l "$DOC"
grep -c "已被推翻" "$DOC"
grep -n "^### H2 路线复活" "$DOC"
