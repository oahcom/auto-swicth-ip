// Netlify 中继函数（Functions v2，Node 18+，支持流式响应）。
//
// 协议与 Vercel 那份逐字节等价；**头过滤也必须是同一套** —— Netlify 同样会在入站
// 请求上注入 X-Forwarded-For / X-Nf-Client-Connection-Ip 之类的头，不剥就等于把
// 客户端真实 IP 连同"我是中继"一起告诉上游。2026-10-29 在 Vercel 上实测过这件事。
//
// 别写成 v1 的 handler（callback 风格）：v1 会收全响应体再发，流式回答会被攒成一块。
//
// x-relay-timing（与 Vercel 两份同口径）：
//   1    -> 正常转发，响应头里报 fetch_ms
//   full -> 不转发，读完上游，回 JSON 账本（fetch_ms / 首块 / 总耗时 / 字节）
const STRIP_EXACT = new Set([
  "forwarded",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-port",
  "x-forwarded-proto",
  "x-real-ip",
  "true-client-ip",
  "client-ip",
  "via",
  "cdn-loop",
  "x-nf-client-connection-ip",
  "x-nf-request-id",
  "x-relay-target",
  "x-relay-path",
  "x-relay-timing",
  "host",
  "content-length",
  "connection",
]);

function shouldStrip(name) {
  return STRIP_EXACT.has(name) || name.startsWith("x-nf-") || name.startsWith("x-vercel-");
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
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

    if (timing === "full") {
      let firstChunkMs = null;
      let bytes = 0;
      if (upstream.body) {
        const reader = upstream.body.getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          if (firstChunkMs === null) firstChunkMs = Date.now() - t0;
          bytes += value ? value.byteLength : 0;
          if (bytes > 8 * 1024 * 1024) break;
        }
      }
      return json({
        region: process.env.AWS_REGION || process.env.NETLIFY_REGION || "",
        runtime: "netlify-node",
        status: upstream.status,
        fetch_ms: fetchMs,
        first_chunk_ms: firstChunkMs,
        total_ms: Date.now() - t0,
        bytes,
      });
    }

    const respHeaders = new Headers();
    for (const [k, v] of upstream.headers.entries()) {
      const lower = k.toLowerCase();
      if (lower.startsWith("x-nf-") || lower.startsWith("x-vercel-")) continue;
      if (lower === "content-encoding" || lower === "content-length") continue;
      respHeaders.set(k, v);
    }
    if (timing === "1") {
      respHeaders.set("x-relay-timing", JSON.stringify({
        runtime: "netlify-node",
        fetch_ms: fetchMs,
        return_ms: Date.now() - t0,
      }));
    }

    return new Response(upstream.body, { status: upstream.status, headers: respHeaders });
  } catch (err) {
    return json({
      error: "relay_upstream_failed",
      message: String((err && err.message) || err),
      fetch_ms: Date.now() - t0,
    }, 502);
  }
}
