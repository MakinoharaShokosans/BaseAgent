# BaseAgent 文档

自研 Agent Runtime 版本（无 LangGraph、无 RAG）的全新文档集。旧文档仍在 `documents/`，仅作迁移参考，不再维护。

## 目录

| 目录 | 内容 | 状态 |
|---|---|---|
| [00_overview](00_overview/) | 项目定位、范围、术语 | 已写初稿 |
| [01_architecture](01_architecture/) | 系统总览、任务生命周期、代码结构与实现路线、配置 | 已写初稿 |
| [02_runtime](02_runtime/) | 状态模型、主循环、检查点、人工审批 | 已写初稿 |
| [03_context](03_context/) | 观察值治理、窗口与压缩、提示词装配、记忆 | 已写初稿 |
| [04_tools](04_tools/) | 工具系统、内置工具、Shell、MCP、Skills、web_search | 已写初稿 |
| [05_security](05_security/) | 权限分层、Shell 沙箱、信任隔离与注入防护 | 已写初稿 |
| [06_subagents](06_subagents/) | 研究隔离区、动态委派与预算账本 | 已写初稿 |
| [07_llm_gateway](07_llm_gateway/) | 模型分档、降级、结构化输出、思考内容 | 已写初稿 |
| [08_observability](08_observability/) | 事件、轨迹与日志、追踪 | 已写初稿 |
| [09_api](09_api/) | HTTP API 与 SSE 事件契约 | 已写初稿 |
| [10_frontend](10_frontend/) | 控制台前端 | 已写初稿 |
| [11_testing](11_testing/) | 测试策略、测试矩阵、现有测试迁移 | 已写初稿 |
| [12_decisions](12_decisions/) | 架构决策记录（ADR） | 0001–0005（均已采纳） |

## 约定

- 文档描述的是代码的**当前真实行为**，与代码不一致时以代码为准并修正文档。
- 重大取舍写入 `12_decisions/`，一个决策一个文件，文件名 `NNNN-标题.md`。
