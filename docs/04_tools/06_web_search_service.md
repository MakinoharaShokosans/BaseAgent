# web_search 服务

`web_search` 是独立进程的 sidecar（`127.0.0.1:8003`），负责检索、抓取、清洗，供websearch 工具使用。主 Agent 不直接调用它，见 [websearch 工具](07_websearch_tool.md)。

## 1. 职责

```
POST /api/v1/search/query
  检索 → 去重 → （可选）并发抓取 Top-N → 正文清洗 → 超长落盘 → 汇总
POST /api/v1/search/fetch
  抓取给定的页面（先用 query 的 fetch=false 看摘要，再只抓挑中的）→ 正文清洗 → 超长落盘 → 汇总
GET  /api/v1/health
```

| 步骤 | 实现 |
|---|---|
| 检索 | 可插拔的搜索提供者，默认 `searxng` 实例池（免 Key、免额外进程、原生 JSON，支持公网或自建节点自动故障转移），备选 `ddgs` 库与 `ddg` 网页抓取 |
| 去重 | URL 指纹加内容指纹 |
| 抓取 | 共享的异步 HTTP 客户端，信号量限制并发，跟随重定向 |
| 清洗 | `trafilatura` 提取正文，输出保留代码块的 Markdown；放入线程池，不阻塞事件循环 |
| 落盘 | 正文超过 Token 阈值则全文写入 `{artifacts_dir}/{task_id}/web_{hash}.md`，响应只回头部预览与句柄 |

## 2. 请求与响应

请求：

| 字段 | 说明 |
|---|---|
| `query` | 检索词，必填，去首尾空白 |
| `max_results` | 候选条数，不得超过配置上限 |
| `fetch` | 是否抓取正文 |
| `task_id` | 产物归属，只允许 `[A-Za-z0-9_-]{1,64}`，防路径穿越 |

`/search/fetch` 的请求：`items`（1–10 项，每项 `url`，可带 `title`、`snippet`）与 `task_id`；地址校验、并发限制、清洗和落盘与 `query` 完全一致，响应是同样的 `results`。

`query` 响应里的 `error` 非空表示搜索源出了问题（例如 `搜索被拦截：…`），此时 `results` 为空；与“搜到 0 条”（`error` 为空）不同。

每条结果：`title`、`url`、`snippet`、`status`、`published_at`、`preview`、`token_estimate`、`is_truncated`、`artifact_path`、`error`、`fingerprint`。

状态：

| 状态 | 含义 |
|---|---|
| `OK` | 抓取并提取到正文 |
| `BLOCKED` | 命中 WAF（403/429/503），**不重试、不对抗**，降级为搜索摘要 |
| `ERROR` | 网络错误、其他 HTTP 错误 |
| `EMPTY` | 页面可达但提取不到正文（常见于纯 JS 渲染页） |
| `SNIPPET_ONLY` | 未发起抓取，仅有搜索摘要 |

总超时 `asyncio.timeout(total_timeout_sec)` 覆盖搜索加抓取加清洗，超时返回 504。

## 3. 配置

```toml
[web_search]
host = "127.0.0.1"
port = 8003
provider = "searxng"
searxng_instances = [
    "https://search.lumy.live",
    "https://searx.tiekoetter.com",
    "https://searx.be",
]
searxng_language = "zh-CN"
ddgs_backends = ["yahoo", "bing", "google"]   # 依次试，取第一个有结果的
allow_fake_ip = false         # 本机代理使用 fake-ip DNS（Clash 等）时设 true，放行 198.18.0.0/15
max_results = 8
fetch_concurrency = 5
request_timeout_sec = 6
total_timeout_sec = 30
max_content_tokens = 1500     # 超过则落盘，只回预览
preview_chars = 200
user_agent = "Mozilla/5.0 (compatible; BaseAgentBot/0.1)"
artifacts_dir = "storage/artifacts"
```

监听地址只允许回环，与 `bash_shell` 同一条安全红线。

## 4. 已发现的问题

这是websearch 工具里**直接接触外部网络**的一环，下面几项的重要性高于其位置：

| # | 问题 | 级别 |
|---|---|---|
| 1 | **没有对目标地址做内网过滤（SSRF）。** 抓取只检查协议是 `http://` 或 `https://`，并且 `follow_redirects=True`。搜索结果里一个 URL，或者一次重定向，可以指向 `127.0.0.1`、私网地址、云厂商元数据地址（`169.254.169.254`）。抓取到的内容会作为"证据"进入websearch 工具的上下文，再被提炼回主 Agent。本机上还跑着 Agent API、两个 sidecar，它们的 GET 接口都在回环上 | **高** |
| 2 | **响应体没有大小上限。** 直接取 `response.text`，把整个响应读入内存。一个指向超大文件的链接可以耗尽内存，或者让 `trafilatura` 长时间占用线程 | 中 |
| 3 | **不检查内容类型。** 二进制、PDF、压缩包也会被当作 HTML 解码并送去提取 | 低 |
| 4 | **重定向次数与跳转目标无限制。** 重定向链可以很长，也可以从公网跳到内网 | 中（与 1 同源） |
| 5 | **User-Agent 轮换。** 配置里支持 UA 池。它不是安全问题，但"伪装成不同浏览器"与"WAF 命中即放弃、不对抗"的设计立场略有张力，需要在文档里说明只为分散请求特征 | 低 |

### 4.1 SSRF 的修复设计

在抓取层增加一个统一的地址校验，**每一次请求以及每一次重定向跳转**都要过：

1. 仅允许 `http` 与 `https`；端口限制为 80、443 及配置白名单。
2. 解析主机名，**对解析出的每个 IP 地址**检查，拒绝：回环（`127.0.0.0/8`、`::1`）、私网（`10/8`、`172.16/12`、`192.168/16`、`fc00::/7`）、链路本地（`169.254/16`、`fe80::/10`）、未指定地址、组播、保留段。
3. **使用校验过的 IP 去连接**（或在连接后复核对端地址），防止 DNS 重绑定：校验时解析到公网地址，连接时再次解析到内网地址。
4. 重定向手动跟随，每跳重新校验，最多 5 跳。
5. 拒绝带用户信息的 URL（`http://user:pass@host`）。
6. 被拒绝的结果以 `status=ERROR`、`error="目标地址不被允许"` 返回，不抛异常，并计数记录。

## 5. 新版改动

1. 上面的地址校验。
2. 流式读取响应，累计超过 `max_download_bytes`（默认 2 MB）即停止并标记为截断；按 `Content-Type` 过滤，只处理 `text/html`、`text/plain`、`application/xhtml+xml`。
3. 抓取并发由信号量控制已有，补充**每主机并发上限**，避免对同一站点集中请求。
4. 搜索提供者接口保持可插拔；新增提供者时必须声明是否需要密钥与是否可能返回内网链接。
5. 产物落盘路径沿用 `task_id` 白名单，补充对 `hash` 部分的校验。
6. 响应里增加 `fetched_ip` 之类的诊断字段（仅调试用，不进入主 Agent 上下文）。

## 6. 与上下文的关系

- 页面正文是**不可信数据**。`web_search` 不做任何信任判断，信任隔离由websearch 工具负责。
- 响应里的 `preview` 和 `snippet` 进入子智能体的证据块，是提示词注入的入口，所以证据必须用信封包裹，见 [websearch 工具 §8](07_websearch_tool.md)。
- 产物句柄 `artifact_path` 指向 sidecar 落盘位置。研究报告里只保留文件名作为 `artifact_id`，主 Agent 通常不需要读取原文。

## 7. 不变量

1. 只监听回环地址。
2. 只抓取公网地址；每次请求与每次重定向都经地址校验。
3. 单个响应的读取量有上限。
4. 命中 WAF 即降级，不对抗。
5. 落盘路径不会逃出产物根目录。
6. 任何失败都以状态表达，不抛业务异常。

## 8. 测试要点

- 地址校验：回环、私网、链路本地、元数据地址、IPv6 变体、十进制与十六进制写法的 IP、带用户信息的 URL。
- DNS 重绑定：第一次解析公网、第二次解析内网的主机。
- 重定向：公网跳内网、过长的链、跳到非 http 协议。
- 大小：超大响应在上限处停止，不耗尽内存。
- 内容类型：二进制与 PDF 被跳过。
- WAF：403/429/503 返回 `BLOCKED` 且不重试。
- 超时：总超时返回 504，不泄漏未完成的任务。
- 去重：同一 URL 的不同写法归一；内容相同的页面合并。

## 9. 待定

- 是否允许用户配置域名白名单或黑名单（例如只允许官方文档站点）。研究场景下白名单会显著提升安全性，但限制了检索面。
- 是否缓存抓取结果，减少重复请求并降低对目标站点的压力。
- 是否支持 `robots.txt`。目前不检查。

## 10. 实现状态

代码在 `backend/src/services/web_search/`（`netguard`、`extract`、`providers`、`app`），`python -m services.web_search` 启动，或由 `baseagent start` 拉起。

- **SSRF（§4 第 1、4 项已修）**：`check_url` 对**每个解析结果**判断（任何一个落在回环、私网、链路本地、`100.64/10`、保留段、组播、未指定地址，或 IPv4 映射的 IPv6 写法，整个主机名都被拒绝）；十进制、十六进制、八进制的 IP 字面量先交给标准库规范化；只允许 `http`/`https`、端口白名单、拒绝带用户信息的 URL。
- **DNS 重绑定**：校验过的 IP 被「钉」进连接 URL（`Host` 头和 SNI 仍是原主机名），解析只发生一次，连接时不会再二次解析。
- **重定向**手动跟随，每一跳重新校验，上限 `max_redirects`；跳到内网、跳到非 http 协议、过长的链都变成 `ERROR`。
- **响应体上限（§4 第 2 项已修）**：流式读取，到 `max_download_bytes` 就停并标记截断；按 `Content-Type` 过滤，只处理配置的类型。
- **WAF**：403/429/503 返回 `BLOCKED`，不重试。
- **清洗**：有 `trafilatura` 用它，没有就用标准库的回退实现（去脚本、样式、导航、页脚，保留标题、列表、代码块）；放入线程池。
- **去重**：URL 归一（大小写、片段、默认端口、尾部斜杠）后去重；内容指纹相同的页面只留第一个。
- **落盘**：超过 `max_content_tokens` 写 `{artifacts_dir}/{task_id}/web_{hash}.md`，响应只回预览和路径；`task_id` 只允许 `[A-Za-z0-9_-]{1,64}`。
- **每主机并发上限（§5 第 3 项）**与全局抓取并发都用信号量。总超时 504；搜索提供者出错以 `error` 字段表达而不是抛业务异常。
- **提供者**：`searxng`（默认，采用公网或自建 SearXNG 实例池，免 Key、免额外本地进程、原生 JSON API，支持按 `searxng_instances` 顺序自动故障转移；兼顾多引擎聚合与抗封锁能力）、`ddgs`（用 `ddgs` 库，按 `ddgs_backends` 的顺序依次试，第一个有结果的为准）和 `ddg`（直接解析 `html.duckduckgo.com`，常被反爬验证页拦截，仅作备选）。搜索源拒绝请求（反爬、限流、全部后端/实例失败）时接口返回 `error: "搜索被拦截：…"`，与“搜到 0 条”区分，模型据此知道不是换措辞能解决的。**未做**：域名白/黑名单的执行（配置项已定义）、抓取缓存、`robots.txt`、`fetched_ip` 诊断字段。
