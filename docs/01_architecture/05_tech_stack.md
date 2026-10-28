# 技术选型

本文确定新项目的技术栈：每一项选什么、为什么、考虑过什么替代、有什么风险。决策记录见 [0005](../12_decisions/0005-tech-stack.md)。

> **状态：已采纳**（5 项分叉均按推荐确定，见 §9）。依赖库的行为假设仍需按 §10 逐项验证。
>
> 版本号不写死在文档里：下限写在 `pyproject.toml` 与 `package.json`，实际版本由 `uv.lock` 和 `pnpm-lock.yaml` 固定。升级是显式动作，不自动跟随。

## 1. 选型原则

1. **能用标准库就不引依赖。** 配置用 `tomllib`，重试与熔断自己写。依赖越少，供应链风险和升级负担越小。
2. **只依赖有人维护、接口稳定的库。** 抓取类、非官方接口类的库放在可替换的窄接口后面。
3. **默认离线可用。** 运行时不去 CDN、不在首次使用时联网下载资源；测试默认不访问网络。
4. **外部服务藏在接口后面。** 模型 SDK、搜索提供者、追踪导出器都能被替换，核心不依赖具体厂商。
5. **与既有设计一致。** 文档里的并发、数据、安全设计已经定了前提：asyncio、pydantic、SQLite、OpenAI 兼容协议、本机单用户、Linux。选型服务于这些前提，而不是推翻它们。
6. **能复用就复用。** 旧项目的护栏、沙箱、测试、前端组件有价值，技术栈不应无谓地切断复用。

## 2. 运行环境

| 项 | 选择 | 说明 |
|---|---|---|
| 操作系统 | Linux（开发机为 Fedora） | 沙箱依赖 `setsid`、`rlimit`、进程组；不承诺 macOS 与 Windows |
| Python | **3.12** | 满足 `asyncio.timeout`、`TaskGroup`、`tomllib`；主流依赖的预编译包最齐全。开发机系统 Python 较新，不采用，由 `uv` 管理解释器，不依赖系统版本 |
| Node | 当前 LTS | 仅用于前端构建与测试 |
| 项目与解释器管理 | `uv` | 沿用；锁文件入库 |
| 前端包管理 | `pnpm` | 沿用 |

## 3. 后端

| 领域 | 选择 | 作用 | 理由 | 考虑过的替代 |
|---|---|---|---|---|
| Web 框架 | **FastAPI** + **uvicorn** | HTTP API、SSE | 异步原生、与 pydantic 一体、OpenAPI 自动生成可做契约快照测试 | Starlette 直用（少一层但失去自动文档）、Litestar（生态小） |
| SSE | `sse-starlette` | 事件流响应 | 沿用，处理心跳与断连 | 自写 `StreamingResponse`（可行，依赖更少，但要自己处理细节） |
| HTTP 客户端 | **httpx** | sidecar 调用、抓取 | 异步、连接池、统一超时 | aiohttp（无必要换） |
| 模型 SDK | 官方 **`openai`** SDK，**`max_retries=0`** | 调用 DeepSeek（OpenAI 兼容协议） | 类型完整、错误分类清楚、日后要流式也现成。重试交给自己的网关，否则 SDK 的重试会与之叠加 | 直接用 httpx 调用（控制力最强、依赖更少，但要自己维护请求与响应类型） |
| 数据模型 | **pydantic v2** | 状态、DTO、结构化输出契约 | 校验与序列化一体；结构化输出的契约直接由它导出 Schema | dataclass + 手写校验（失去 Schema 导出） |
| 配置 | **pydantic-settings**，TOML 来源 | 加载 `config.toml`，`BASEAGENT_` 前缀与 `__` 嵌套覆盖 | 与 [配置](04_configuration.md) 的优先级规则一一对应 | 自写加载器（可行，但要重复造校验） |
| 持久化 | **SQLite（WAL）** + **`aiosqlite`**，手写 SQL，小型版本化迁移器 | 元数据、检查点、事件 | 本机单用户，零运维；表很少，不值得 ORM | SQLAlchemy（过重）、SQLModel、直接 `sqlite3` 加线程池 |
| 重试与熔断 | **自己实现** | 网关弹性 | 需要与截止时间、抖动、`Retry-After`、熔断联动 | `tenacity`（不好表达截止时间与熔断，不再依赖） |
| 日志 | **loguru** | 结构化日志 | `contextualize` 与 `patcher` 可自动注入 `task_id`、`span_id` | stdlib `logging` + `structlog`（更标准，但迁移成本高，收益有限） |
| MCP | 官方 **`mcp`** SDK | 外部工具接入 | 官方实现 | 自写协议（不值得） |
| 分词估算 | **tiktoken**，仅用于估算 | 裁剪、压缩的局部决策 | 轻、快。用量统计以服务商返回为准 | 模型自带分词器（更准，更重）；字符粗算（无依赖，偏差大） |
| 网页正文 | **trafilatura** | 抓取后的正文提取 | 质量好，保留代码块 | readability 类库 |
| 搜索 | 可插拔提供者，见 §9 问题 3 | websearch 工具的检索 | | |
| 技能元数据 | **PyYAML**（`safe_load`） | 解析 `SKILL.md` 的 frontmatter | | |
| 追踪导出 | 自有 span 模型；`opentelemetry-*`、`langfuse` 为**可选依赖组** | 见 [追踪](../08_observability/03_tracing.md) | 已采纳 0004 | 直接依赖 OTel SDK（核心变重） |

### 3.1 需要特别说明的几项

**`openai` SDK 的使用方式。** 创建客户端时设 `max_retries=0`（默认会自动重试 2 次）。DeepSeek 特有的字段，如 `reasoning_content` 和缓存用量，不在 SDK 的标准类型里，要通过响应对象的额外字段读取；`thinking` 开关通过 `extra_body` 发送。这些细节全部封装在网关内，不外泄。**这一点要在上线前验证**，见 [DeepSeek §9](../07_llm_gateway/04_deepseek.md)。

**tiktoken 需要预置词表。** 它首次使用时会联网下载词表文件。要在安装或构建阶段预置缓存目录（`TIKTOKEN_CACHE_DIR`），保证离线环境、CI、测试都不触网。DeepSeek 的分词器与它不同，所以估算值要乘配置里的安全系数。

**SQLite 的使用约定。** 每个数据库文件一个长生命周期连接；写操作串行化；WAL 模式，`busy_timeout` 设置；迁移器按版本号顺序执行并幂等，记录在 `schema_version` 表中。旧版的“每次操作新开连接”不再沿用。

**自己写重试的范围。** 只有网关一处需要：分类（可重试与不可重试）、带抖动与上限的指数退避、`Retry-After`、截止时间、熔断。约一百行，配套测试用假时钟。

## 4. 依赖清单

### 4.1 核心运行依赖

| 包 | 用途 |
|---|---|
| fastapi、uvicorn[standard] | API 与服务器 |
| sse-starlette | SSE |
| httpx | HTTP 客户端 |
| openai | 模型 SDK |
| pydantic、pydantic-settings | 模型与配置 |
| aiosqlite | 异步 SQLite |
| loguru | 日志 |
| mcp | MCP 客户端 |
| tiktoken | 分词估算 |
| pyyaml | 技能元数据 |

### 4.2 按服务分组的依赖

sidecar 与主进程共享代码库，但各自的依赖应当能单独安装：

| 分组 | 包 |
|---|---|
| `web-search` | trafilatura、搜索提供者的包 |
| `tracing-otlp` | opentelemetry-sdk、opentelemetry-exporter-otlp |
| `tracing-langfuse` | langfuse |

### 4.3 开发依赖

| 包 | 用途 |
|---|---|
| pytest、pytest-asyncio、pytest-cov | 测试 |
| hypothesis | 属性测试与模糊测试 |
| respx | 模拟 httpx 请求，用于我们自己用 `httpx` 发请求的客户端测试（如 sidecar 调用）。**拦不住 `openai` 3.x**，见 §10 |
| ruff | 检查与格式化 |
| pyright | 类型检查，核心包用严格模式 |
| import-linter | 强制模块依赖方向，见 [代码结构 §1](03_code_layout_and_roadmap.md) |
| hatchling | 构建后端 |

### 4.4 移除的旧依赖

| 包 | 原因 |
|---|---|
| langgraph、langgraph-checkpoint-sqlite | 自研循环与检查点 |
| langchain-core | 自定义消息类型；原为传递依赖 |
| langfuse | 降为可选依赖组 |
| tenacity | 自己实现重试 |
| numpy、tabulate | 旧评测框架的开发依赖，新评测不需要 |
| 向量库、嵌入、重排序全家桶 | RAG 移除 |

## 5. 前端

| 领域 | 选择 | 理由 | 考虑过的替代 |
|---|---|---|---|
| 框架 | **React 19** + **TypeScript** | 沿用，旧组件可复用 | |
| 构建 | **Vite** | 沿用；开发服务器做同源代理，令牌在服务端侧注入 | |
| 样式 | **Tailwind CSS 3.4** | 沿用，避免无谓迁移；后续升级是独立事项 | Tailwind 4（新，等稳定后再评估） |
| 状态 | **Zustand** | 沿用；事件归约写成纯函数 | Redux Toolkit（过重） |
| 服务端状态 | **TanStack Query** | 沿用 | |
| SSE | **自写的 fetch 流式管理器** | 需要序号缺口检测、`STREAM_GAP` 处理、连接状态展示、可控的重连，现有 `@microsoft/fetch-event-source` 提供不了 | 继续用该库（维护不活跃，且这些逻辑仍要自己加） |
| Markdown | `react-markdown` + `remark-gfm` + `rehype-highlight` + **`rehype-sanitize`** | 不启用原始 HTML，再加一道清理 | |
| 代码编辑与查看 | **CodeMirror 6** | 轻量、可完全本地打包 | **Monaco**：`@monaco-editor/react` 默认从 CDN 加载本体，违反“默认离线”；要保留就必须改为本地资源，且体积大。见 §9 问题 2 |
| 图标与工具 | lucide-react、clsx、tailwind-merge | 沿用 | |
| 面板布局 | react-resizable-panels | 沿用 | |
| 单元与组件测试 | **Vitest** + Testing Library + jsdom | 沿用 | |
| E2E | **Playwright**，数量控制在个位数 | 覆盖审批、断线重连、错误展示 | Cypress |
| 代码质量 | ESLint + Prettier + `tsc --noEmit` | | |

移除：`@monaco-editor/react`、`@microsoft/fetch-event-source`；去掉所有 RAG 相关代码。

## 6. 工程化

| 项 | 选择 |
|---|---|
| 仓库结构 | 单仓库：`backend/`、`frontend/`、`config/`、`docs/`。取代旧的 `AegisAgent` 等命名 |
| Python 包布局 | `backend/src/` 下按 [目标目录](03_code_layout_and_roadmap.md) 组织；`pyproject.toml` 在 `backend/` |
| 任务入口 | 一份 `Makefile` 或 `justfile`：`make test`、`make lint`、`make dev` 等，包装 `uv` 与 `pnpm` 命令 |
| 提交前检查 | pre-commit：ruff、格式、密钥扫描（防止把密钥提交） |
| CI（每次提交） | lint、类型检查、单元与集成测试、依赖方向检查、前端测试 |
| CI（每日） | E2E、慢测试 |
| 手动 | 真实模型评测，带预算保护 |
| 版本与发布 | 语义化版本；变更记录；锁文件入库 |
| 许可证 | 沿用现有 LICENSE，需要你确认；新增依赖前检查许可证兼容性 |

## 7. 沙箱的演进

沙箱是整套安全设计里最薄弱的机制，技术上分两步走。

| 阶段 | 手段 | 状态 |
|---|---|---|
| 首版 | 进程组、`RLIMIT_AS`/`FSIZE`/`CPU`、超时两段式硬杀、内存池、最小环境变量 | 沿用并补环境隔离 |
| 加固（可选，接口先留好） | **`bubblewrap`**：用户命名空间隔离文件系统视图、可断网；**`systemd-run --user --scope`**：cgroup 限制内存、CPU、**进程数（`pids.max`）** | 第二阶段 |

关于进程数限制：之前我提到的 `RLIMIT_NPROC` **不适合**。它限制的是同一用户 ID 的**全部进程总数**，会和你桌面上其他进程相互影响，既可能误伤，也限制不住。cgroup 的 `pids.max` 才是针对一条命令及其子进程的精确上限。

Fedora 带 SELinux，开启 `bwrap` 或 cgroup 时要核对策略是否放行。沙箱升级之前，文档里的“不是对抗恶意代码的隔离环境”这条边界声明继续有效。

## 8. 明确不选

| 不选 | 原因 |
|---|---|
| LangChain / LangGraph / LlamaIndex 等编排框架 | 核心诉求就是自研循环 |
| 向量数据库与嵌入模型 | RAG 移除 |
| ORM | 表少，手写 SQL 更清楚 |
| Redis、消息队列、Celery | 单机单进程，不需要 |
| Docker 作为强依赖 | 本机直接运行；容器化是可选的分发方式，不是前提 |
| GraphQL / WebSocket | REST 加 SSE 已满足；审批与取消走普通 HTTP |
| 前端 SSR 框架（Next.js 等） | 本机控制台，不需要 |
| 多语言后端 | 单一语言降低维护成本 |

## 9. 5 项分叉的结论

全部按推荐确定。下表保留“若选另一边的影响”，供日后重新评估时参考。

| # | 问题 | 结论 | 若选另一边的影响 |
|---|---|---|---|
| 1 | **后端语言**：保持 Python？ | **Python 3.12** | 换 TypeScript 或 Go 要重估 MCP 生态、网页正文提取库、旧代码复用，工作量显著增加；整套文档里的并发与数据设计也要改写 |
| 2 | **代码编辑器**：CodeMirror 6 还是 Monaco？ | **CodeMirror 6** | 选 Monaco 需要把本体改为本地打包，体积大，配置繁琐 |
| 3 | **联网检索提供者** | 接口可插拔；开发用 DuckDuckGo（`duckduckgo-search` 已更名为 `ddgs`，需核对）；正式用一个有 API 的服务（自部署 SearXNG，或 Brave、Tavily 等，需要账号和费用） | 只用 DDG 是抓取式的非官方接口，易被限流或失效；用付费接口需要你提供账号与预算 |
| 4 | **沙箱加固时机** | 首版保持 `rlimit` 加最小环境，`bwrap` 与 cgroup 作为第二阶段 | 首版就上会增加复杂度与 SELinux 调试成本，但安全性更高 |
| 5 | **生产形态** | 前端构建后由后端静态托管，单进程分发 | 只提供开发服务器则前后端分开启动，令牌注入与同源要靠开发代理，不适合分发 |

## 10. 锁定的版本与已发现的偏差

骨架安装时由 `uv.lock` 与 `pnpm-lock.yaml` 解析出的版本（节选）：Python 3.12.14、FastAPI 0.162、pydantic 2.14、pydantic-settings 2.16、aiosqlite 0.22、tiktoken 0.14、Vite 6.4、vitest 5.0、pnpm 12.9。

**两个依赖的主版本比文档作者的经验更新，需要特别核对：**

- `openai` 解析为 **3.37**。本文对 SDK 行为的描述（默认自动重试 2 次、响应对象的额外字段访问方式、`extra_body`）依据的是较早的主版本，**必须对 3.x 逐条验证**，见下面的验证项 1。
- `mcp` 解析为 **2.3**。关于 `anyio` 任务约束与会话生命周期的假设同样需要对 2.x 验证。

**验证结果（离线，`backend/tests/sdk/`，32 项全过）：**

| 项 | 结论 |
|---|---|
| `max_retries=0` 生效；默认客户端自己重试 2 次（共 3 次请求） | 成立。网关必须显式关掉 |
| `extra_body` 合并进请求体顶层 | 成立 |
| 响应 `message.reasoning_content`、`usage.prompt_cache_hit_tokens` / `miss` 可读 | 成立，读法：`getattr(obj, name, None)`，全部在 `model_extra` 里；缺失时不报错 |
| 流式 delta 带 `reasoning_content`；`stream_options.include_usage` 在末尾 chunk 给用量 | 成立 |
| assistant 消息里的自定义 `reasoning_content` 原样发出 | 成立（回传策略可行） |
| 429 的 `Retry-After` 可从 `exc.response.headers` 取；400/401/422/5xx 各有异常类；402 无专门子类，只能用 `APIStatusError.status_code` | 成立 |
| 每请求 `timeout=` 传到底层 | 成立 |
| **偏差：** `openai` 3.x 的底层 HTTP 库是独立的 **`httpx2`**，不是 `httpx` | 影响：① `respx` 拦截不了 openai 的请求，测试改用 `httpx2.MockTransport`；② 传入自带 `http_client` 必须是 `httpx2.AsyncClient`；③ 超时、连接错误等底层异常是 httpx2 的类型，但 SDK 已包装成 `APIConnectionError` / `APITimeoutError`，网关只应捕获 SDK 的异常；④ 项目自己的 `httpx` 依赖与它并存，互不影响 |
| **偏差（mcp）：** `mcp.server.fastmcp` 在 2.x 已移除，改为 `from mcp.server.mcpserver import MCPServer` | 写 MCP 测试服务器时需用新名 |
| **偏差（mcp）：** `call_tool(read_timeout_seconds=)` 是 `float` 秒，不再是 `timedelta`；超时抛 `MCPError`，会话之后仍可用 | 已验证 |
| **偏差（mcp）：** 参数校验失败不抛异常，返回 `is_error=True` 的结果；`call_tool` 的返回类型还包含 `InputRequiredResult` 等新分支 | 工具适配器要按结果的 `is_error` 判断，并处理非 `CallToolResult` 的分支（首版可视为不支持并报错） |
| mcp：`stdio_client` 不把父进程环境传给子进程 | 成立，密钥不会漏给第三方服务器 |
| mcp：不同任务里建立/释放 | 报 `RuntimeError`（文档风险属实）；专用后台任务方案有效 |

另外：pnpm 12 默认拦截依赖的构建脚本，配置写在 `frontend/pnpm-workspace.yaml` 的 `allowBuilds`，只放行 `esbuild`。

## 11. 上线前的技术验证项

选型里有几处依赖库行为的假设，要用小脚本验证，不要凭文档：

1. ~~`openai` SDK 能读到 `reasoning_content` 与缓存用量字段；`extra_body` 能发出 `thinking`；`max_retries=0` 生效。~~ **离线部分已验证**，见 §10；仍需用真实密钥确认服务端行为。
2. ~~`pydantic-settings` 的 TOML 来源与 `BASEAGENT_` 前缀、`__` 嵌套覆盖行为符合 [配置](04_configuration.md)，未知键能报错。~~ **已验证并落地**（阶段 -0.5 的配置加载器及其测试）。
3. `tiktoken` 在预置缓存目录后完全离线可用。
4. ~~`mcp` SDK 在“专用后台任务”模式下建立与关闭连接无残留子进程。~~ **已验证**（用本地 stdio 服务器），见 §10。接真实的第三方服务器（npx 类）时仍要再确认进程树被回收。
5. `trafilatura` 对常见技术文档页面的提取质量，以及对恶意或超大页面的行为。
6. `aiosqlite` 在 WAL、单连接、并发读写下的表现与锁等待。
7. CodeMirror 6 满足查看与简单编辑需求，包体积可接受。
8. 自写 SSE 管理器在断网、重启后端、慢消费者三种情形下的行为。

## 12. 待定

- **检索服务商**：第 3 项只定了“接口可插拔、开发用 DuckDuckGo、正式用有 API 的服务”，具体选 SearXNG、Brave 还是 Tavily 还未选定，取决于账号与预算。不阻塞开发，正式使用前定。

- 是否引入 `orjson` 提升 JSON 序列化性能。目前瓶颈不在这里，暂不引入。
- 类型检查是否对全部包启用严格模式，还是仅核心包。倾向核心包严格、其余基础模式，逐步收紧。
- 是否使用 `uv` 的 workspace 把 sidecar 拆成独立包。目前同一个包内按子目录组织即可。
