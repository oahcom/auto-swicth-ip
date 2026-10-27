import fs from "node:fs";

const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

const RETRY_HEADERS = [
  "retry-after",
  "x-ratelimit-reset-after",
  "reset-after",
  "x-retry-after",
  "x-ratelimit-reset",
];

try {
  const res = await fetch(job.url, {
    method: job.method || "POST",
    headers: job.headers,
    body: job.body,
    proxy: job.proxy || undefined,
    signal: AbortSignal.timeout(job.timeoutMs || 20000),
  });
  const reader = res.body.getReader();
  const { value } = await reader.read();
  const head = new TextDecoder().decode(value || new Uint8Array()).slice(0, 1000);
  reader.cancel().catch(() => {});
  const retry = {};
  for (const name of RETRY_HEADERS) {
    const v = res.headers.get(name);
    if (v != null) retry[name] = v;
  }
  console.log(JSON.stringify({ status: res.status, head, retry }));
} catch (e) {
  console.log(JSON.stringify({ error: String(e?.message || e) }));
}
