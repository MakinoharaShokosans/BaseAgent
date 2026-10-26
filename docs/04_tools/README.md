# 工具系统

工具是模型作用于外部世界的唯一通道。本目录描述工具的契约、注册、派发与信任模型，以及各类具体工具与外部集成。

## 文档

| 文档 | 内容 | 状态 |
|---|---|---|
| [01_tool_system.md](01_tool_system.md) | 工具协议、结果契约、注册表、派发器、Schema 导出、sidecar 客户端 | 已写初稿 |
| [02_builtin_tools.md](02_builtin_tools.md) | `view_file`、`write_file`、`read_artifact`（新增）、`edit_file`（建议）、去掉 RAG 后的代码定位 | 已写初稿 |
| [03_shell_tool.md](03_shell_tool.md) | `bash` 工具行为，以及写给模型的命令使用规范 | 已写初稿 |
| [04_mcp.md](04_mcp.md) | MCP 接入：懒连接、消毒、命名空间、故障隔离 | 已写初稿 |
| [05_skills.md](05_skills.md) | 技能包：来源信任、渐进式披露、元数据治理 | 已写初稿 |
| [06_web_search_service.md](06_web_search_service.md) | `web_search` sidecar：检索、抓取、清洗，及 SSRF 问题 | 已写初稿 |

> 权限分层、信任隔离与注入防护见 [05_security](../05_security/)，子智能体见 [06_subagents](../06_subagents/)。工具系统只定义"工具是什么、怎么被调用"，不定义"谁有权调用"。

## 工具全景

| 工具 | 来源 | 信任 | 权限 | 说明 |
|---|---|---|---|---|
| `view_file` | 内置 | trusted | read_only | |
| `write_file` | 内置 | trusted | workspace_write | |
| `edit_file` | 内置（建议新增） | trusted | workspace_write | 局部精确替换 |
| `list_dir`、`grep` | 内置（建议新增） | trusted | read_only | 结构化、不经 shell |
| `read_artifact` | 内置（新增） | trusted | read_only | 取回被裁剪的输出 |
| `bash` | 内置，经 `bash_shell` sidecar | trusted | 按命令分类 | |
| `load_skill` | 内置 | trusted | read_only | |
| `delegate_research` | 研究隔离区入口 | trusted 接口，输出不可信 | read_only | 内部使用 `web_search` |
| `spawn_subagent` | 动态委派 | 输出覆盖为 untrusted | read_only（动作本身） | |
| `mcp__*` | MCP 适配器 | untrusted | 未知工具，默认 full_permissions | |
| `web_search` | 仅研究隔离区 | untrusted | — | 不在主工具表 |

## 本目录发现的主要问题

| # | 问题 | 位置 | 级别 |
|---|---|---|---|
| 1 | `web_search` 抓取无内网地址过滤（SSRF），重定向不受限 | [06 §4](06_web_search_service.md) | **高** |
| 2 | MCP 连接的建立与释放可能不在同一个任务，关闭时可能出错或残留子进程 | [04 §8](04_mcp.md) | 高（待核实） |
| 3 | MCP 工具名消毒与模型接口的函数名约束不一致，一个非法名字会让整个请求 400 | [04 §8](04_mcp.md) | 高 |
| 4 | 沙箱的拒绝原因没有到达模型 | [03 §1.2](03_shell_tool.md) | 中 |
| 5 | `write_file` 非原子、无备份，覆盖会丢失用户的未提交修改 | [02 §3](02_builtin_tools.md) | 中 |
| 6 | 被裁剪的输出无法取回（`read_artifact` 缺失） | [02 §4](02_builtin_tools.md) | 中 |
| 7 | 工作区技能可顶替同名可信技能；技能正文未扫描 | [05 §8](05_skills.md) | 中 |
| 8 | `view_file` 整文件读入内存、不识别二进制 | [02 §2](02_builtin_tools.md) | 低 |
| 9 | 失败的 MCP 服务器永久短路；`npx -y` 无版本固定 | [04 §8](04_mcp.md) | 中 |

## 去掉 RAG 的影响汇总

- 移除 `rag_search`，系统提示词、研究报告模板、子智能体协议里的"先用 `rag_search` 核实"改为"先用 `grep`、`view_file` 核实"。
- 代码定位依赖 `bash` 中的 `rg`/`grep`，建议补充结构化的 `list_dir` 与 `grep` 工具，见 [02 §6](02_builtin_tools.md)。
- `services.rag_url`、RAG 健康探测、前端的 RAG 面板与状态指示一并删除。
