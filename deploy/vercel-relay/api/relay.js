// Vercel 中继函数（Edge runtime）。
//
// 协议与 9router 自带的那份一致，但**多一道头过滤**，原因是实测出来的：
// Vercel 会在入站请求上注入 Forwarded / X-Forwarded-For / X-Vercel-Proxied-For，
// 里面是**客户端真实 IP**；还有 X-Vercel-Oidc-Token（部署令牌）。照抄 9router 的
// 实现（只删两个 relay 头）会把这些原样发给上游 ——
//   1) 上游看到的是你的真实 IP，中继等于白接；
//   2) api.ipify.org 这类服务直接读 Forwarded，探针就再也测不到 Vercel 的出口；
//   3) 部署令牌泄漏给第三方。
//
// 2026-10-29 实测证据（部署后 curl httpbin 回显）：
//   "Forwarded": "for=67.159.48.147;..."      <- 本机出口
//   "X-Vercel-Proxied-For": "67.159.48.147"
//   "origin": "67.159.48.147,13.212.60.86"    <- 后者才是 Vercel 的出口
//
// ── x-relay-timing（2026-10-29 加）───────────────────────────────────
// 在外部量「经中继要多花多少」量不准：本机网络抖动 1–2.5s，比要测的那点开销大一个量级。
// 所以让函数自己报账：
//   x-relay-timing: 1     -> 正常转发，响应头 x-relay-timing 里给 fetch_ms / 到返回耗时
//   x-relay-timing: full  -> 不转发，把上游读完，回一段 JSON（fetch_ms / 首块 / 总耗时 / 字节数）
// full 是给 asip vercel bench 用的：它测的是「函数 -> 上游」那一段，跟本地网络无关。
export const config = { runtime: "edge" };

// 逐跳 / 由前置代理注入的头：中继必须剥掉，不能带给上游。
const STRIP_EXACT = new Set([
  "forwarded",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-port",
  "x-forwarded-proto",
  "x-real-ip",
  "true-client-ip",
  "via",
  "cdn-loop",
  "logs-url",
  "x-invocation-id",
  "x-middleware-subrequest",
  "x-ja4-digest",
  // 中继自己用的头
  "x-relay-target",
  "x-relay-path",
  "x-relay-timing",
  // 由 fetch 自己算
  "host",
  "content-length",
  "connection",
  "transfer-encoding",
]);

function shouldStrip(name) {
  // x-vercel-* 一律剥：SSO 令牌、部署 URL、地域回显、OIDC token 都在这个前缀下。
  return STRIP_EXACT.has(name) || name.startsWith("x-vercel-");
}

// 从 x-vercel-id（形如 "sin1::abc-123-xyz"）里取边缘区域，给 timing 用。
function edgeRegion(req) {
  const raw = req.headers.get("x-vercel-id") || "";
  const m = /^([a-z]{3}\d)::/.exec(raw);
  return m ? m[1] : "";
}

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

export default async function handler(req) {
  const target = req.headers.get("x-relay-target");
  const relayPath = req.headers.get("x-relay-path") || "/";
  const timing = (req.headers.get("x-relay-timing") || "").toLowerCase();

  if (!target) {
    return json({
      error: "Missing x-relay-target header",
      hint: "这是 asip 的中继端点，不是普通网页",
    }, 400);
  }

  const targetUrl = target.replace(/\/$/, "") + relayPath;
  const t0 = Date.now();

  const clean = {};
  for (const [k, v] of req.headers.entries()) {
    if (!shouldStrip(k)) clean[k] = v;
  }

  const hasBody = req.method !== "GET" && req.method !== "HEAD";

  try {
    const upstream = await fetch(targetUrl, {
      method: req.method,
      headers: clean,
      body: hasBody ? req.body : undefined,
      duplex: "half",
      redirect: "manual",
    });
    const fetchMs = Date.now() - t0;

    // full：只测量，不转发 —— 把上游读完，回一段账。
    if (timing === "full") {
      const started = Date.now();
      let firstChunkMs = null;
      let bytes = 0;
      const reader = upstream.body ? upstream.body.getReader() : null;
      while (reader) {
        const { done, value } = await reader.read();
        if (done) break;
        if (firstChunkMs === null) firstChunkMs = Date.now() - t0;
        bytes += value ? value.byteLength : 0;
        if (Date.now() - started > 20000 || bytes > 8 * 1024 * 1024) break;
      }
      return json({
        region: edgeRegion(req),
        runtime: "edge",
        status: upstream.status,
        fetch_ms: fetchMs,
        first_chunk_ms: firstChunkMs,
        total_ms: Date.now() - t0,
        bytes,
      });
    }

    // 响应头也要过一遍：Vercel 的缓存/调试头会泄漏部署信息，且会干扰流式。
    const respHeaders = new Headers();
    for (const [k, v] of upstream.headers.entries()) {
      const lower = k.toLowerCase();
      if (lower.startsWith("x-vercel-") || lower === "content-encoding" || lower === "content-length") {
        continue;
      }
      respHeaders.set(k, v);
    }
    if (timing === "1") {
      respHeaders.set("x-relay-timing", JSON.stringify({
        region: edgeRegion(req),
        runtime: "edge",
        fetch_ms: fetchMs,
        return_ms: Date.now() - t0,
      }));
    }

    return new Response(upstream.body, {
      status: upstream.status,
      headers: respHeaders,
    });
  } catch (err) {
    return json({
      error: "relay_upstream_failed",
      message: String((err && err.message) || err),
      fetch_ms: Date.now() - t0,
    }, 502);
  }
}
