# BaseAgent 文档

BaseAgent 是一个运行在本机的高可靠、安全可控的代码工程 Agent 架构底座。本文档集为系统的核心设计与实现规范。

## 目录

| 目录 | 内容 | 状态 |
|---|---|---|
| [00_overview](00_overview/) | 项目定位、范围、术语 | 已写初稿 |
| [01_architecture](01_architecture/) | 系统总览、任务生命周期、代码结构与实现路线、配置 | 已写初稿 |
| [02_runtime](02_runtime/) | 状态模型、主循环、检查点、人工审批、计划与草稿、工具执行 | 目标设计 |
| [03_context](03_context/) | 观察值治理、窗口与压缩、提示词装配、记忆、上下文继承 | 已写初稿 |
| [04_tools](04_tools/) | 工具系统、内置工具、Shell、MCP、Skills、web_search | 已写初稿 |
| [05_security](05_security/) | 权限分层、Shell 沙箱、信任隔离与注入防护 | 已写初稿 |
| [06_subagents](06_subagents/) | 动态委派（websearch 已移到 [04_tools](04_tools/07_websearch_tool.md)） | 已写初稿 |
| [07_llm_gateway](07_llm_gateway/) | 模型分档、降级、结构化输出、思考内容 | 已写初稿 |
| [08_observability](08_observability/) | 事件、轨迹与日志、追踪 | 已写初稿 |
| [09_api](09_api/) | HTTP API 与 SSE 事件契约 | 已写初稿 |
| [10_frontend](10_frontend/) | 控制台前端 | 已写初稿 |
| [11_testing](11_testing/) | 测试策略、测试矩阵、现有测试迁移 | 已写初稿 |
| [12_decisions](12_decisions/) | 架构决策记录（ADR） | 0001–0007（0001 已被 0006 取代） |

## 约定

- 文档描述的是代码的**当前真实行为**，与代码不一致时以代码为准并修正文档。
- 重大取舍写入 `12_decisions/`，一个决策一个文件，文件名 `NNNN-标题.md`。
