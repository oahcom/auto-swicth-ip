# Vercel 中继函数（asip 第三条出口）

一个 Edge 函数，按 9router 的 relay 协议转发请求：

| 头 | 语义 |
|---|---|
| `x-relay-target` | 目标 origin，如 `https://api.ipify.org` |
| `x-relay-path` | 目标路径，如 `/` 或 `/zen/v1/chat/completions` |

上游看到的源地址是 **Vercel 的出口**，不是本机。

## 部署

两种都行：

~~~bash
# A. 一条命令（asip 自己打 Vercel API，不需要 GUI）
VERCEL_TOKEN=xxx ./deploy/vercel-relay-deploy.py --count 3 --apply

# B. 手动
cd deploy/vercel-relay && vercel --prod
~~~

## 自测

~~~bash
curl -s -H 'x-relay-target: https://api.ipify.org' -H 'x-relay-path: /' \
  https://<你的项目>.vercel.app
~~~

回一个 IP = 通了。回 401 = 项目开了 SSO 保护（部署脚本会自动关，手动部署要在
Settings → Deployment Protection 里关掉）。

## 边界

- 这是一个**开放中继**：知道地址的人都能用。项目名不要可猜，别写进公开文档。
- 函数有执行时长上限（Edge 约 30s 量级）。长流式回答可能被截断，见任务书 R2。
- 只转发 HTTP(S)。没有 CONNECT，不能当浏览器代理。
