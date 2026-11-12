import fs from "node:fs";

const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

try {
  const res = await fetch(job.url, {
    method: job.method || "POST",
    headers: job.headers,
    body: job.body,
    proxy: job.proxy || undefined,
    signal: AbortSignal.timeout(job.timeoutMs || 30000),
  });
  const text = await res.text();
  const headers = {};
  res.headers.forEach((v, k) => { headers[k] = v; });
  console.log(JSON.stringify({
    status: res.status,
    headers,
    body: text.slice(0, 4000),
    bodyLen: text.length,
  }));
} catch (e) {
  console.log(JSON.stringify({ error: String((e && e.message) || e) }));
}
