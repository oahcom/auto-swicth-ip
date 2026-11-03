// Vercel 中继 · Node 运行时（不是 Edge）。
//
// 为什么留这一份：**Edge 不能选区域**。实测同一份 Edge 代码加 --region iad1 装上去，
// 出口还是新加坡；换成 Node 运行时 + vercel.json 的 functions.regions，出口就真的搬到
// iad1 了 —— 七个区七个 IP，连 ASN 都换了一个（AS14618 vs AS16509）。
//
// 代价：Node 函数有 maxDuration（Hobby 默认 10s，最高 60s），实测 67.8s 被服务端掐断。
// Edge 没有区域但能流 75 秒以上。要长回答走 Edge，要换段走 Node。
//
// ── x-relay-timing（与 Edge 那份同一套语义）─────────────────────────────
//   x-relay-timing: 1     -> 正常转发，响应头里报 fetch_ms
//   x-relay-timing: full  -> 不转发，读完上游，回 JSON 账本
// 两条腿必须报同一个口径，否则 bench 出来的数没法跟 Edge 比。
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";

const STRIP = new Set([
  "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-port",
  "x-forwarded-proto", "x-real-ip", "true-client-ip", "via", "cdn-loop",
  "x-relay-target", "x-relay-path", "x-relay-timing", "host", "content-length",
  "connection", "transfer-encoding", "accept-encoding",
]);

function shouldStrip(k) {
  return STRIP.has(k) || k.startsWith("x-vercel-");
}

function sendJson(res, obj, status) {
  res.statusCode = status || 200;
  res.setHeader("content-type", "application/json");
  res.setHeader("cache-control", "no-store");
  res.end(JSON.stringify(obj));
}

export default async function handler(req, res) {
  const target = req.headers["x-relay-target"];
  const relayPath = req.headers["x-relay-path"] || "/";
  const timing = String(req.headers["x-relay-timing"] || "").toLowerCase();

  if (!target) {
    sendJson(res, {
      error: "Missing x-relay-target header",
      hint: "这是 asip 的中继端点，不是普通网页",
    }, 400);
    return;
  }

  const targetUrl = String(target).replace(/\/$/, "") + relayPath;
  const t0 = Date.now();

  const headers = {};
  for (const [k, v] of Object.entries(req.headers)) {
    if (!shouldStrip(k.toLowerCase())) headers[k] = v;
  }
  headers["accept-encoding"] = "identity"; // 不压缩，免得流被攒成一整块

  const init = { method: req.method, headers, redirect: "manual" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = Readable.toWeb(req);
    init.duplex = "half";
  }

  try {
    const upstream = await fetch(targetUrl, init);
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
      sendJson(res, {
        region: process.env.VERCEL_REGION || "",
        runtime: "node",
        status: upstream.status,
        fetch_ms: fetchMs,
        first_chunk_ms: firstChunkMs,
        total_ms: Date.now() - t0,
        bytes,
      });
      return;
    }

    res.statusCode = upstream.status;
    for (const [k, v] of upstream.headers.entries()) {
      const lower = k.toLowerCase();
      if (lower.startsWith("x-vercel-")) continue;
      if (lower === "content-encoding" || lower === "content-length" || lower === "transfer-encoding") continue;
      res.setHeader(k, v);
    }
    if (timing === "1") {
      res.setHeader("x-relay-timing", JSON.stringify({
        region: process.env.VERCEL_REGION || "",
        runtime: "node",
        fetch_ms: fetchMs,
        return_ms: Date.now() - t0,
      }));
    }
    if (!upstream.body) { res.end(); return; }
    await pipeline(Readable.fromWeb(upstream.body), res);
  } catch (err) {
    if (!res.headersSent) {
      sendJson(res, {
        error: "relay_upstream_failed",
        message: String((err && err.message) || err),
        fetch_ms: Date.now() - t0,
      }, 502);
    } else {
      res.end();
    }
  }
}
