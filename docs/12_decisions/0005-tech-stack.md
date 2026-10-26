# 0005 技术栈

- 状态：**已采纳**（5 项分叉均按推荐确定，见下）
- 影响：[01_architecture/05_tech_stack.md](../01_architecture/05_tech_stack.md)、`pyproject.toml`、`package.json`

## 背景

去掉 LangGraph 与 RAG 后，需要确定新项目的技术栈。既有设计已经设定了前提：asyncio、pydantic、SQLite、OpenAI 兼容协议（DeepSeek）、本机单用户、Linux。

## 决定

保持 Python 与 React/TypeScript 的主栈，精简依赖：

- **后端**：Python 3.12（`uv` 管理）、FastAPI + uvicorn、httpx、官方 `openai` SDK（`max_retries=0`）、pydantic v2 与 pydantic-settings、SQLite（WAL）+ aiosqlite 手写 SQL、loguru、官方 `mcp` SDK、tiktoken（仅估算，预置词表）、trafilatura、PyYAML。
- **自己实现**：重试与熔断、SSE 管理器（前端）、小型迁移器。
- **前端**：React 19、TypeScript、Vite、Tailwind 3.4、Zustand、TanStack Query、react-markdown 加 rehype-sanitize、CodeMirror 6、Vitest、Playwright。
- **工程化**：ruff、pyright、pytest 加 Hypothesis 加 respx、import-linter、pre-commit、单仓库 `backend/` `frontend/` `config/` `docs/`。
- **移除**：langgraph*、langchain-core、tenacity、numpy、tabulate、RAG 全家桶；`langfuse` 降为可选依赖组；前端的 `@monaco-editor/react`、`@microsoft/fetch-event-source`。

## 5 项分叉的结论

| # | 问题 | 结论 |
|---|---|---|
| 1 | 后端语言 | Python 3.12 |
| 2 | 代码编辑器 | CodeMirror 6 |
| 3 | 联网检索提供者 | 接口可插拔，开发用 DuckDuckGo，正式用有 API 的服务 |
| 4 | 沙箱加固时机 | 首版 `rlimit` 加最小环境，第二阶段 `bwrap` 与 cgroup |
| 5 | 生产形态 | 后端静态托管前端，单进程分发 |

## 后果

- 依赖面显著缩小，升级与供应链风险降低。
- 重试、熔断、SSE 管理器需要自己维护，配套测试必须到位。
- 旧项目的护栏、沙箱、测试与前端组件可以大量复用，迁移成本主要在引擎层。
- 第 1 项已确定为 Python；若日后要换语言，本决定与大部分设计文档都需要重估。
- 第 3 项只确定了“接口可插拔，开发用 DuckDuckGo，正式用有 API 的服务”，**具体选哪家服务商仍待选定**（涉及账号与费用），不阻塞开发。
- 第 4 项意味着首版沙箱仍是进程组加 `rlimit` 加最小环境，**没有网络隔离**，文档里的相应边界声明继续有效。
- 文中所有依赖库行为假设需要经 [技术验证项](../01_architecture/05_tech_stack.md) 逐一验证。
