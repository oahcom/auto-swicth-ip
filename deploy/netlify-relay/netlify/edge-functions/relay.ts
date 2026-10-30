// Netlify 中继 · Edge Function（Deno），协议与 Vercel 那两份完全一致。
//
// 为什么单独写这一份：Netlify Functions（Node）跑在 AWS 的一个固定区域，
// 而 Edge Functions 跑在 Netlify 自己的全球边缘网上 —— 出口 IP 和 ASN 是另一套。
// 对我们来说这正好是「换段」的第二个来源：Vercel 给的是 AWS，Netlify Edge 给的是它自己的边。
//
// 头过滤与 Vercel 那两份同源：Forwarded / X-Forwarded-* / x-nf-* / x-relay-* 一律剥掉。
// 不剥的话上游看到的是**客户端真实 IP**，中继等于白接（这个坑在 Vercel 上真踩过）。
//
// timing 语义也照抄：x-relay-timing: 1 回报耗时；full 只测量、读完上游、回一段 JSON。
import type { Config, Context } from "https://edge.netlify.com";

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
  "x-relay-target",
  "x-relay-path",
  "x-relay-timing",
  "host",
  "content-length",
  "connection",
  "transfer-encoding",
]);

function shouldStrip(name: string): boolean {
  return STRIP_EXACT.has(name) || name.startsWith("x-nf-") || name.startsWith("x-vercel-");
}

function json(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

export default async function handler(req: Request, context: Context): Promise<Response> {
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

  const clean: Record<string, string> = {};
  for (const [k, v] of req.headers.entries()) {
    if (!shouldStrip(k.toLowerCase())) clean[k] = v;
  }

  const hasBody = req.method !== "GET" && req.method !== "HEAD";

  try {
    const upstream = await fetch(targetUrl, {
      method: req.method,
      headers: clean,
      body: hasBody ? req.body : undefined,
      redirect: "manual",
      // @ts-ignore Deno 认这个字段，TS 类型还没跟上
      duplex: "half",
    });
    const fetchMs = Date.now() - t0;

    if (timing === "full") {
      let firstChunkMs: number | null = null;
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
        region: context.deploy?.context || "",   // Netlify 不给区域名，这里留空更诚实
        runtime: "netlify-edge",
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
        runtime: "netlify-edge",
        fetch_ms: fetchMs,
        return_ms: Date.now() - t0,
      }));
    }
    return new Response(upstream.body, { status: upstream.status, headers: respHeaders });
  } catch (err) {
    return json({
      error: "relay_upstream_failed",
      message: String((err as Error)?.message || err),
      fetch_ms: Date.now() - t0,
    }, 502);
  }
}

export const config: Config = { path: "/*" };
