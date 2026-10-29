# 配置

实际文件：[config/config.toml](../../config/config.toml) 与 [.env.example](../../.env.example)。本文说明配置的组织规则、哪些放环境变量、各节含义与校验。

## 1. 原则

1. **所有可调的东西都在配置里**，包括上下文窗口、各种阈值、模型名、端点、数量上限。代码里不出现魔法数字，也不出现模型名。
2. **密钥只在环境变量里**，配置文件只写变量名。配置文件可以入版本库，`.env` 绝不入库。
3. **按端点而不是按全局配置模型能力**：上下文窗口、最大输出、是否支持工具、是否返回用量、思考内容怎么处理，都是端点的属性。换模型只改配置。
4. **比例与绝对值同时存在**：窗口很大时（如 1M）比例阈值形同虚设，所以任务内折叠与压缩的触发线取 `min(窗口 × 比例, 绝对 Token)`。
5. **安全相关的上限在配置里，但安全红线不在**：监听必须是回环地址、不可信工具不能进主工具表、沙箱不继承完整环境，这些由代码强制，配置改不了。
6. **加载时强校验，失败即拒绝启动**，不带着不合法的配置运行。
7. **文件即规范**：配置文件里每一项都有注释，`config.toml` 本身是配置的参考文档。

## 2. 优先级

```
命令行参数  >  环境变量 BASEAGENT_<节>__<键>  >  config.toml  >  代码内置默认值
```

- 环境变量覆盖的写法：前缀 `BASEAGENT_`，层级分隔符 `__`（双下划线）。`BASEAGENT_SERVER__PORT=8010` 覆盖 `[server] port`；`BASEAGENT_RUNTIME__GUARDRAILS__MAX_STEPS=40` 覆盖 `[runtime.guardrails] max_steps`。
- 数组与表（如端点列表）不支持环境变量覆盖，只能改文件。
- 配置文件位置：`BASEAGENT_CONFIG` 指定，否则依次查找 `./config/config.toml` 与 `$BASEAGENT_HOME/config.toml`。
- 相对路径一律相对 `BASEAGENT_HOME` 解析，不相对当前目录，避免不同目录启动得到不同数据集。解析后必须仍在数据目录之内（`..` 逃逸被拒绝）；绝对路径原样使用。
- 数据目录本身：环境变量 `BASEAGENT_HOME` 优先于文件里的 `app.home`。文件里的 `app.home` 必须是绝对路径或以 `~` 开头；环境变量里给相对值时，在加载时按当前目录取绝对。开发时想用仓库内的目录，设 `BASEAGENT_HOME=./storage`。
- 只有以 `BASEAGENT_<节>__` 形式指向**存在的标量**的变量才是覆盖项。`BASEAGENT_HOME`、`BASEAGENT_CONFIG`、`BASEAGENT_API_TOKEN`、`BASEAGENT_PROXY_TARGET` 是普通变量，不是覆盖项。指向数组、表、整节、未知键的变量，加载时报错并带上变量名。
- 实现在 [`baseagent_config`](../../backend/src/baseagent_config/)。

## 3. 环境变量：什么该放，什么不该放

### 3.1 判断规则

放进环境变量的东西，满足下面任一条：

| 条件 | 例子 |
|---|---|
| 是**密钥或凭据**，泄露会造成损失 | 模型 API Key、第三方服务令牌 |
| 是**机器相关**的路径或位置，不应写进共享配置 | 数据目录、配置文件位置 |
| 在**测试、CI、容器**里需要临时固定 | 固定的 API 令牌 |

不放进环境变量的：业务参数、阈值、上限、模型名、端点地址、权限规则。它们是行为的一部分，要能随代码评审与版本管理。

### 3.2 变量清单

| 变量 | 必需 | 含义 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 是 | 模型密钥。推理档与快速档共用；由 `api_key_env` 引用 |
| `BASEAGENT_HOME` | 否 | 数据目录。数据库、轨迹、产物、日志、令牌、金丝雀盐都在其下 |
| `BASEAGENT_CONFIG` | 否 | 配置文件路径 |
| `BASEAGENT_API_TOKEN` | 否 | 固定 API 令牌，优先于令牌文件。仅测试或容器需要 |
| `GITHUB_TOKEN` 等 | 否 | MCP 服务器凭据，配置中以 `"env:VAR"` 引用，只在启用该服务器时需要 |
| `OTLP_HEADERS` | 否 | OTLP 导出的认证头，仅开启远程追踪时 |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 否 | 仅开启 Langfuse 导出时 |
| `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` | 否 | 标准代理变量；同时影响模型调用与 `web_search`。务必把回环地址放进 `NO_PROXY` |
| `BASEAGENT_<节>__<键>` | 否 | 覆盖任意标量配置项 |
| `BASEAGENT_PROXY_TARGET` | 否 | 前端开发服务器代理的后端地址，无 `VITE_` 前缀，不进打包产物 |

### 3.3 有意不放进环境变量的

| 项 | 为什么在文件里 |
|---|---|
| 上下文窗口、最大输出、阈值、上限 | 是行为参数，需要进版本库与评审 |
| 模型名、端点地址、降级顺序 | 同上；换模型是一次可审计的变更 |
| 监听地址与端口 | 在文件里并有回环校验；环境变量可覆盖端口，但不能覆盖成非回环地址 |
| 命令黑名单与权限分类正则 | 安全策略，必须可评审 |
| 技能与规则文件的开关 | 安全相关，默认关闭，开启要有记录 |

### 3.4 关于前端

Vite 约定以 `VITE_` 开头的变量会被**打包进浏览器代码**。所以前端环境里不能放任何密钥，也不要用 `VITE_` 变量传令牌。令牌由开发服务器的同源代理在服务端侧注入，见 [API 接入控制 §3](../09_api/03_access_and_errors.md)。

## 4. 运行时自动生成的文件

位于 `BASEAGENT_HOME` 下，权限 0600（目录 0700），**不入库**：

| 文件 | 内容 | 说明 |
|---|---|---|
| `api_token` | 256 位随机访问令牌 | 首次启动生成；删除并重启即轮换 |
| `canary_salt` | 金丝雀派生用的随机盐 | 首次启动生成；动态注入提示词以检测泄露 |

## 5. 各节速查

| 节 | 作用 | 详见 |
|---|---|---|
| `[app]` `[logging]` `[storage]` | 数据目录、日志、存储路径与保留策略 | [系统总览](01_system_overview.md)、[轨迹与日志](../08_observability/02_trajectory_and_logs.md) |
| `[server]` `[server.auth]` `[server.filesystem]` `[server.workspace]` | API、令牌、文件系统浏览边界、工作区路径校验 | [API 接入控制](../09_api/03_access_and_errors.md) |
| `[runtime.guardrails]` `[runtime.loop]` `[runtime.checkpoint]` `[runtime.approval]` | 死循环拦截（指纹窗口、摆动、`max_consecutive_blocks`）、检查点、审批 | [主循环](../02_runtime/02_main_loop.md)、[检查点](../02_runtime/03_checkpoint_and_resume.md)、[审批](../02_runtime/04_approval.md) |
| `[models.*]` | 两档模型、端点、能力声明 | [网关](../07_llm_gateway/01_gateway.md)、[DeepSeek](../07_llm_gateway/04_deepseek.md) |
| `[llm.*]` | 重试、熔断、并发、单次调用总耗时上限、用量估算、结构化输出 | [网关](../07_llm_gateway/01_gateway.md)、[结构化输出](../07_llm_gateway/02_structured_output.md) |
| `[context.*]` | 会话与任务级窗口、观察值裁剪、记忆、项目规则、提示词装配 | [03_context](../03_context/) |
| `[tools.*]` | 文件工具与派发 | [内置工具](../04_tools/02_builtin_tools.md) |
| `[services]` `[bash_shell]` `[web_search]` | sidecar 寻址、沙箱、检索与抓取安全 | [Shell 沙箱](../05_security/02_shell_sandbox.md)、[web_search](../04_tools/06_web_search_service.md) |
| `[websearch]` | websearch 工具 | [07_websearch_tool](../04_tools/07_websearch_tool.md) |
| `[subagent]` | 动态委派 | [06_subagents](../06_subagents/) |
| `[skills]` `[permissions]` `[mcp]` | 技能、权限分级、MCP | [Skills](../04_tools/05_skills.md)、[权限分层](../05_security/01_permission_tiers.md)、[MCP](../04_tools/04_mcp.md) |
| `[events]` `[redaction]` | 事件、脱敏 | [08_observability](../08_observability/) |
| `[security]` | 金丝雀盐文件 | [信任与注入](../05_security/03_trust_and_injection.md) |

### 5.1 与上下文窗口相关的配置

| 项 | 位置 | 说明 |
|---|---|---|
| 模型窗口 | `models.<档>.endpoints[].context_window` | 每个端点各自声明 |
| 最大输出 | `models.<档>.endpoints[].max_output_tokens` | 显式设置，避免 JSON 被默认值截断 |
| 会话活跃对话窗口 | `context.session.token_limit` | 默认 32000 |
| 会话压缩触发/回落 | `context.session.high_watermark` / `compaction_ratio` | 0.80 触发，一次压到 0.40 |
| 任务内折叠触发 | `context.task.soft_ratio` / `soft_tokens` | 取 `min(窗口 × 比例, 绝对值)` |
| 任务内压缩触发 | `context.task.hard_ratio` / `hard_tokens` | 同上；`compaction_enabled`（默认开）可关 |
| 观察值裁剪阈值 | `context.observation.max_tokens` | 默认 1500 |

## 6. 校验

加载时检查，任一失败拒绝启动并说明原因：

| 类别 | 规则 |
|---|---|
| 安全红线 | `server.host`、`bash_shell.host`、`web_search.host` 必须是回环地址；`web_search.block_private_addresses` 等布尔项只接受真正的布尔值 |
| 路径 | `app.home` 可创建且可写（新建时权限 0700）；所有相对路径解析后落在 `app.home` 之下；`browse_roots` 不得落在 `forbidden_roots` 之内 |
| 模型 | 每档至少有一个端点；端点的 `context_window`、`max_output_tokens`、`timeout_sec` 为正；`api_key_env` 对应变量缺失的**云端**端点被跳过并告警，某档一个都不剩则拒绝启动 |
| 比例 | `high_watermark` 大于 `compaction_ratio`；`soft_ratio` 小于 `hard_ratio`，二者在 (0,1) 内 |
| 数量关系 | `context.task.soft_tokens` 小于 `hard_tokens`；`mcp.call_timeout_sec` 小于工具派发超时；`bash_shell.timeout_sec` 不大于 `max_timeout_sec` |
| 权限 | 级别取值合法；分类正则能编译（不合法的启动时报错，而不是运行时静默跳过） |
| 子智能体 | `max_depth` 为非负；`denied_tools` 必须包含 `spawn_subagent`；`model_tier` 必须是已存在的档 |
| MCP | `stdio` 服务器必须给 `command`，`sse` 必须给 `url`；敏感环境变量必须写成 `"env:变量名"` |
| 模型（补充） | 同一档内端点不得重名；`max_output_tokens` 必须小于 `context_window` |
| 技能 | 开启 `allow_workspace` 时给出警告 |
| 规则文件 | 开启 `context.rules.allow_workspace` 时给出警告 |
| 未知键 | 未知配置项报错（拼写错误不能被静默忽略） |

校验结果同样显示在启动看板里。

## 7. 待定设计

- 配置热加载。目前所有配置在启动时读取，改了要重启。模型端点和日志级别是最可能需要热加载的，但会带来状态一致性问题，首版不做。
- 多配置文件叠加（例如 `config.local.toml` 覆盖共享配置）。目前用环境变量覆盖已够用；若覆盖项变多再引入。
- 配置的 JSON Schema 导出，用于编辑器补全与校验。
- `max_total_tokens` 的默认值是否随所选模型与价格调整。当前默认值为 200000。
- `context.task.soft_tokens` 与 `hard_tokens` 的默认值（100000 与 160000）依据实测的成本与延迟调整。
