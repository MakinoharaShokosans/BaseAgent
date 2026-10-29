# 0005 技术栈选型规范

- 状态：**已采纳**
- 关联模块：[01_architecture/05_tech_stack.md](../01_architecture/05_tech_stack.md)、`pyproject.toml`、`package.json`

## 背景

在系统架构选型中，需要确定整个工程的技术栈。核心设计前提：asyncio 异步并发、pydantic 类型校验、SQLite 本地存储、OpenAI 兼容接口协议（DeepSeek 等）、本机单用户部署、面向 Linux 环境。

## 决定

保持 Python 与 React/TypeScript 的主栈，坚持高内聚、低外部依赖的选型原则：

- **后端**：Python 3.12（`uv` 管理）、FastAPI + uvicorn、httpx、官方 `openai` SDK（`max_retries=0`）、pydantic v2 与 pydantic-settings、SQLite（WAL）+ aiosqlite 手写 SQL、loguru、官方 `mcp` SDK、tiktoken（仅估算，预置词表）、trafilatura、PyYAML。
- **自研核心组件**：确定性重试与熔断机制、前端 SSE 实时流管理器、轻量级数据库迁移机制。
- **前端**：React 19、TypeScript、Vite、Tailwind CSS、Zustand、TanStack Query、react-markdown 加 rehype-sanitize、CodeMirror 6、Vitest、Playwright。
- **工程化**：ruff、pyright、pytest 加 Hypothesis 加 respx、import-linter、pre-commit、单仓库目录结构（`backend/`、`frontend/`、`config/`、`docs/`）。
- **精简原则**：不引入重量级编排框架与向量数据库全家桶，自研纯粹智能体主循环；`langfuse` 作为可选导出插件；自研轻量 SSE 管理器与编辑器选型。

## 关键技术分叉结论

| # | 决策项 | 结论与选型 | 决策依据 |
|---|---|---|---|
| 1 | 后端主语言 | Python 3.12 | 丰富的 AI 生态与工具集成能力，兼顾开发效率与异步并发 |
| 2 | 代码编辑器 | CodeMirror 6 | 轻量、模块化、无复杂 WebWorker 依赖，内存与加载开销小 |
| 3 | 联网检索提供者 | 接口可插拔抽象 | 开发与测试默认支持 DuckDuckGo，生产环境支持标准搜索 API |
| 4 | 沙箱隔离机制 | 分阶段演进 | 首版基于进程组、`rlimit` 与受限环境变量，后续平滑支持 `bwrap` 与 cgroup |
| 5 | 部署交付形态 | 单进程静态托管 | 后端 FastAPI 托管前端构建产物，极简单进程打包分发 |

## 后果

- 依赖面显著缩小，升级与第三方供应链风险降低。
- 关键重试、熔断与 SSE 状态管理由自研代码承担，配套测试覆盖率严格受控。
- 架构自包含性极大提升，无任何云端外部强制绑定。
