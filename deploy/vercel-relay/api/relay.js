// Vercel 中继函数（Edge runtime）。
//
// 协议与 9router 自带的那份完全一致，不能改：
//   x-relay-target  目标 origin，如 https://api.opencode.ai
//   x-relay-path    目标路径，如 /zen/v1/chat/completions
// 响应体直通（response.body），不做缓冲 —— SSE 全靠这一条。
//
// 为什么必须 Edge 而不是 Node：Node 函数会把 body 收全再发，流式回答会被攒成
// 一整块（首字节延迟 = 整段时长）。Edge 用 Web Streams 直通。
export const config = { runtime: "edge" };

export default async function handler(req) {
  const target = req.headers.get("x-relay-target");
  const relayPath = req.headers.get("x-relay-path") || "/";

  if (!target) {
    return new Response(
      JSON.stringify({
        error: "Missing x-relay-target header",
        hint: "这是 asip 的中继端点，不是普通网页",
      }),
      { status: 400, headers: { "content-type": "application/json" } }
    );
  }

  const targetUrl = target.replace(/\/$/, "") + relayPath;

  const rawHeaders = {};
  for (const [k, v] of req.headers.entries()) rawHeaders[k] = v;
  delete rawHeaders["x-relay-target"];
  delete rawHeaders["x-relay-path"];
  delete rawHeaders["host"];
  delete rawHeaders["content-length"];

  const hasBody = req.method !== "GET" && req.method !== "HEAD";

  try {
    const response = await fetch(targetUrl, {
      method: req.method,
      headers: rawHeaders,
      body: hasBody ? req.body : undefined,
      duplex: "half",
      redirect: "manual",
    });

    return new Response(response.body, {
      status: response.status,
      headers: response.headers,
    });
  } catch (err) {
    // 把上游失败如实回给调用方：9router 会把 502 当出口故障，
    // 而不是当成「中继自己坏了」—— 两者后续动作不同。
    return new Response(
      JSON.stringify({ error: "relay_upstream_failed", message: String(err && err.message || err) }),
      { status: 502, headers: { "content-type": "application/json" } }
    );
  }
}
