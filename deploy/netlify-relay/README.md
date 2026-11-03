# Netlify 中继函数（asip 第三条出口 · 第二家平台）

一个 Functions v2 函数，按 9router 的 relay 协议转发请求。与 `deploy/vercel-relay/`
是同一份协议、同一套头，只是换了个部署平台。

| 头 | 语义 |
|---|---|
| `x-relay-target` | 目标 origin，如 `https://api.ipify.org` |
| `x-relay-path` | 目标路径，如 `/` |

## 部署

~~~bash
# A. 一条命令（打 Netlify API，自动建站点 + 传函数 + 登记台账）
NETLIFY_AUTH_TOKEN=xxx ./deploy/netlify-relay-deploy.py --count 2 --apply

# B. 手动
cd deploy/netlify-relay && netlify deploy --prod
~~~

## 自测

~~~bash
curl -s -H 'x-relay-target: https://api.ipify.org' -H 'x-relay-path: /' \
  https://<站点名>.netlify.app
~~~

回一个 IP = 通了。

## 与 Vercel 版的关键差别

| | Vercel | Netlify |
|---|---|---|
| 运行时 | Edge（Deno 风味） | Functions v2（Node 18+，返回 Response） |
| 流式 | `response.body` 直通 | `new Response(upstream.body)` 直通 |
| 路径来源 | `vercel.json` rewrites | `netlify.toml` redirect + `force = true` |
| 部署 API | 内联 files | 函数要先打成 zip |
| 出口段 | AWS | **也是 AWS** |

**最后一行是本方案最该记住的一句**：多接一家不等于多一个段。Vercel 和 Netlify 的出口
都在 AWS，接两家换的是服务商与地址池随机性，不是 ASN。真要跳出 AWS 得看 Cloudflare
Workers / Deno Deploy —— 9router 那两种 type 也认，但 asip 还没写部署脚本。

## 边界

- 开放中继：知道地址就能用。站点名别可猜，别写进公开文档。
- 函数有执行时长上限（v2 默认 10s，可在 `netlify.toml` 里调，上限随套餐）。
  这一条**比 Vercel 更紧**，长流式回答要实测。
