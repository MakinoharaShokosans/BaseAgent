# 工具系统

工具是模型作用于外部世界的唯一通道。本目录描述工具的契约、注册、派发与信任模型，以及各类具体工具与外部集成。

## 文档

| 文档 | 内容 | 状态 |
|---|---|---|
| [01_tool_system.md](01_tool_system.md) | 工具协议、结果契约、注册表、派发器、Schema 导出、sidecar 客户端 | 已写初稿 |
| [02_builtin_tools.md](02_builtin_tools.md) | `view_file`、`write_file`、`read_artifact`、`edit_file` 与代码定位方案 | 规范 |
| [03_shell_tool.md](03_shell_tool.md) | `bash` 工具行为，以及写给模型的命令使用规范 | 规范 |
| [04_mcp.md](04_mcp.md) | MCP 接入：懒连接、消毒、命名空间、故障隔离 | 规范 |
| [05_skills.md](05_skills.md) | 技能包：来源信任、渐进式披露、元数据治理 | 规范 |
| [06_web_search_service.md](06_web_search_service.md) | `web_search` sidecar：检索、抓取、清洗，及 SSRF 防护 | 规范 |

> 权限分层、信任隔离与注入防护见 [05_security](../05_security/)，子智能体见 [06_subagents](../06_subagents/)。工具系统只定义"工具是什么、怎么被调用"，不定义"谁有权调用"。

## 工具全景

| 工具 | 来源 | 信任 | 权限 | 说明 |
|---|---|---|---|---|
| `view_file` | 内置 | trusted | read_only | 文件读取 |
| `write_file` | 内置 | trusted | workspace_write | 文件覆盖写 |
| `edit_file` | 内置 | trusted | workspace_write | 局部精确替换 |
| `list_dir`、`grep` | 内置 | trusted | read_only | 结构化搜索与目录浏览 |
| `read_artifact` | 内置 | trusted | read_only | 取回被裁剪的输出 |
| `bash` | 内置，经 `bash_shell` sidecar | trusted | 按命令分类 | 受限沙箱执行 |
| `load_skill` | 内置 | trusted | read_only | 加载技能 SOP |
| `websearch` | websearch 工具入口 | trusted 接口，输出不可信 | read_only | 内部使用 `web_search` |
| `spawn_subagent` | 动态委派 | 输出覆盖为 untrusted | read_only（动作本身） | 委派受限子智能体 |
| `mcp__*` | MCP 适配器 | untrusted | 默认 full_permissions | 外部协议扩展工具 |
| `web_search` | 独立 Sidecar | untrusted | — | 仅供 websearch 内部调用 |

