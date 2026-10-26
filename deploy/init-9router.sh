#!/bin/bash
# 9Router 的一次性初始化：让 asip 真正拿到它需要的三样东西。
#
#   ./deploy/init-9router.sh          # 幂等
#   NINE_ROUTER_URL=http://127.0.0.1:20127 ./deploy/init-9router.sh
#
# asip 依赖 9router 的三样，缺一样就退化成「看起来装好了、实际什么都没测到」：
#
#   1. CLI token        —— 9router 的 /api/* 默认全部 401。token 是
#                          sha256(machine-id + "9r-cli-auth" + auth/cli-secret)[:16]，
#                          cli-secret 首次使用时才生成，所以必须自己算。
#   2. proxyPool + 绑定 —— opencode 是 noAuth provider，运行时走**虚拟连接**注入；
#                          没有 providerStrategies.opencode.proxyPoolId 时
#                          resolveConnectionProxyConfig 拿到 null，出口是**直连**，
#                          流量根本不经过 sing-box。
#   3. enableObservability —— 默认 false，于是 requestDetails 一行都不写，
#                          asip 的 get_9router_error_rate() 永远读到 0/0，
#                          错误驱动换线等于没开。这条最隐蔽：HTTP 请求明明返回了
#                          429，asip 却显示「错误=0/0」。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${NINE_ROUTER_URL:-http://127.0.0.1:20127}"
POOL_NAME="${ASIP_POOL_NAME:-asip-singbox}"
# 与 config.py 的 singbox.mixed_port 一致：9router 的出口必须是 sing-box。
PROXY_URL="${ASIP_PROXY_URL:-http://127.0.0.1:7890}"
PYBIN="${PYTHON:-/usr/bin/python3}"

log() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

curl -s -o /dev/null -m 5 "$BASE/login" \
    || die "9router 不在 $BASE（先跑 deploy/install-9router.sh）"

# ---- 1. CLI token ----
# 与 src/shared/utils/machineId.js 的 getConsistentMachineId("9r-cli-auth") 同式。
# cli-secret 不存在时先生成并落盘，否则算出来的 token 每次都不一样。
TOKEN="$("$PYBIN" - "$HOME/.9router" <<'PY'
import hashlib, os, secrets, sys, pathlib
data = pathlib.Path(sys.argv[1])
raw = (data / "machine-id").read_text().strip()
sec_file = data / "auth" / "cli-secret"
sec = ""
if sec_file.exists():
    sec = sec_file.read_text().strip()
if not sec:
    sec_file.parent.mkdir(parents=True, exist_ok=True)
    sec = secrets.token_hex(32)
    sec_file.write_text(sec)
    sec_file.chmod(0o600)
print(hashlib.sha256((raw + "9r-cli-auth" + sec).encode()).hexdigest()[:16])
PY
)"
[ -n "$TOKEN" ] || die "算不出 CLI token（$HOME/.9router/machine-id 存在吗）"

api() {
    local method="$1" path="$2" body="${3:-}"
    if [ -n "$body" ]; then
        curl -sS -m 30 -X "$method" -H "x-9r-cli-token: $TOKEN" \
            -H 'Content-Type: application/json' -d "$body" "$BASE$path"
    else
        curl -sS -m 30 -X "$method" -H "x-9r-cli-token: $TOKEN" "$BASE$path"
    fi
}

code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H "x-9r-cli-token: $TOKEN" "$BASE/api/settings")"
[ "$code" = 200 ] || die "CLI token 被拒（HTTP $code）—— machine-id 与进程读到的不一致？"

# ---- 2. proxyPool ----
# 按名字复用，避免每跑一次多一条。
POOL_ID="$(api GET /api/proxy-pools | "$PYBIN" -c "
import json,sys
name = sys.argv[1]
for p in json.load(sys.stdin).get('proxyPools') or []:
    if p.get('name') == name:
        print(p['id']); break
" "$POOL_NAME")"

if [ -z "$POOL_ID" ]; then
    resp="$(api POST /api/proxy-pools "{\"name\":\"$POOL_NAME\",\"proxyUrl\":\"$PROXY_URL\",\"isActive\":true,\"type\":\"http\"}")"
    POOL_ID="$(printf '%s' "$resp" | "$PYBIN" -c "import json,sys; print(json.load(sys.stdin)['proxyPool']['id'])")" \
        || die "建 proxyPool 失败：$resp"
    log "✓ 建 proxyPool $POOL_NAME -> $PROXY_URL"
else
    # 地址可能变了（比如 sing-box 换了端口），顺手纠正。
    api PATCH "/api/proxy-pools/$POOL_ID" "{\"proxyUrl\":\"$PROXY_URL\",\"isActive\":true}" >/dev/null || true
    log "✓ 复用 proxyPool $POOL_NAME ($POOL_ID)"
fi

# ---- 3. 绑定 opencode + 打开 observability ----
# 一次 PATCH 写两个键：9router 的 updateSettings 是浅合并，分两次也能成，
# 但一次成功就不会出现「绑了 pool 却没开观测」的半吊子状态。
api PATCH /api/settings "$("$PYBIN" - "$POOL_ID" <<'PY'
import json, sys
print(json.dumps({
    "providerStrategies": {"opencode": {"proxyPoolId": sys.argv[1], "rotateStrategy": "none"}},
    "enableObservability": True,
}))
PY
)" >/dev/null || die "写 settings 失败"

api GET /api/settings | "$PYBIN" -c "
import json, sys
s = json.load(sys.stdin)
pool = ((s.get('providerStrategies') or {}).get('opencode') or {}).get('proxyPoolId')
enabled = s.get('enableObservability')
print(f'  opencode proxyPoolId = {pool}')
print(f'  enableObservability  = {enabled}')
assert pool, 'proxyPoolId 没写进去'
assert enabled is True, 'enableObservability 没打开'
" || die "写完校验不过"

log "✓ 9router 初始化完成（出口 $PROXY_URL，观测已开）"
log "  自测：curl -s -X POST $BASE/v1/chat/completions \\"
log "        -H 'Authorization: Bearer <api key>' -H 'Content-Type: application/json' \\"
log "        -d '{\"model\":\"opencode/deepseek-v4.1-flash-free\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"
log "  然后看 requestDetails 是否落行 —— asip 的全部判据都来自它"
