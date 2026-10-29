# 后端目录结构与快速定位指南 (Backend Directory Guide)

> **定位宗旨**：无需每次跨层检索或猜测代码位置。本指南按**“架构拓扑图”**、**“数据流转生命周期”**与**“功能需求场景”**三向索引，助你在 5 秒内精准定位到后端目标文件。

---

## 1. 架构拓扑与层级全景图 (Architecture Topology)

```
                     ┌──────────────────────────────────────────────┐
                     │          HTTP / SSE 客户端 (前端控制台)        │
                     └──────────────────────┬───────────────────────┘
                                            │
                                            ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. API 接入层 (agent_runtime.api)                                                          │
│    routes/ (tasks, sessions, workspaces, settings, prompts, skills, mcp, system, files)    │
│    app.py (FastAPI 入口, SPA 静态托管) ── security.py / token.py (认证鉴权)                  │
└───────────────────────────────────────────┬───────────────────────────────────────────────┘
                                            │
                                            ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│ 2. 运行时编排层 (agent_runtime.runtime)                                                    │
│    runner.py (任务主协程) ── service.py (TaskService) ── active.py (活跃任务集合)           │
│    events.py (运行时事件构建) ── memory_flow.py (记忆提取流)                                │
└───────────────────────────────────────────┬───────────────────────────────────────────────┘
                                            │
                      ┌─────────────────────┼─────────────────────┐
                      ▼                     ▼                     ▼
┌───────────────────────────┐ ┌───────────────────────────┐ ┌─────────────────────────────┐
│ 3. 循环内核               │ │ 4. 上下文与提示词工程     │ │ 5. 安全护栏与审批           │
│    (agent_runtime.loop)   │ │    (agent_runtime.context)│ │    (guardrails / approval)  │
│    kernel.py (ReAct 步进) │ │    prompt.py (系统提示词) │ │    permissions.py (三级权限)│
│    tools.py (工具执行绑定)│ │    observation.py (裁剪)  │ │    binding.py (审批决策绑定)│
│    scratch.py (规划草稿)  │ │    task_compaction.py(压减│ │    injection.py (注入防护)  │
└─────────────┬─────────────┘ └─────────────┬─────────────┘ └─────────────┬───────────────┘
              │                             │                             │
              └─────────────────────┬───────┴─────────────────────────────┘
                                    │
                      ┌─────────────┴─────────────┐
                      ▼                           ▼
┌───────────────────────────────────────────┐ ┌─────────────────────────────────────────────┐
│ 6. 模型网关 (agent_runtime.llm)           │ │ 7. 工具与 Sidecar (tools / services / mcps) │
│    gateway.py (多端点路由与熔断降级)      │ │    tools/builtin/ (shell, files, search...) │
│    wire.py (流式协议与 reasoning 提取)    │ │    services/bash_shell/ (独立执行沙箱)       │
│    structured.py (结构化输出与工具 Schema)│ │    mcps/ (MCP 管理器、协议转换与工具索引)   │
└───────────────────────────────────────────┘ └─────────────────────────────────────────────┘
                                    │
                                    ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│ 8. 持久化存储与总线 (memory / checkpoint / observability)                                 │
│    memory/store.py (SQLite 持久化: 任务/会话/记忆) ── checkpoint/store.py (状态快照)      │
│    observability/hub.py (EventHub 全局与单任务事件流) ── trajectory.py (因果日志 JSONL)   │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 按“改动需求”秒查索引表 (Task-to-File Lookup)

当你需要修改后端的某项具体功能时，查阅下表直达对应文件：

| 需求场景 | 目标文件 | 关键类 / 函数 / 逻辑点 |
|---|---|---|
| **新增/修改 API 接口路由** | [`src/agent_runtime/api/routes/`](src/agent_runtime/api/routes/) | 各子路由文件（`tasks.py`, `sessions.py`, `workspaces.py`, `settings.py`, `prompts.py`, `skills.py`, `mcp.py`） |
| **改动 SSE 事件推流 / 全局事件流** | [`src/agent_runtime/observability/hub.py`](src/agent_runtime/observability/hub.py)<br>[`src/agent_runtime/api/routes/system.py`](src/agent_runtime/api/routes/system.py)<br>[`src/agent_runtime/api/routes/tasks.py`](src/agent_runtime/api/routes/tasks.py) | `EventHub.publish`, `subscribe_global`, `/events` & `/tasks/{id}/stream` SSE 生成器 |
| **改动 ReAct 主循环 (思考→工具→观察)** | [`src/agent_runtime/loop/kernel.py`](src/agent_runtime/loop/kernel.py) | `step()`, 终止条件判断、异常捕获降级、思考链流转 |
| **改动大模型接入 (OpenAI/DeepSeek/Claude)** | [`src/agent_runtime/llm/gateway.py`](src/agent_runtime/llm/gateway.py)<br>[`src/agent_runtime/llm/wire.py`](src/agent_runtime/llm/wire.py) | `LlmGateway.chat()`, `chat_stream()`, `reasoning_content` 思维链解析 |
| **改动结构化输出 / 工具参数 Schema** | [`src/agent_runtime/llm/structured.py`](src/agent_runtime/llm/structured.py) | Tool definition 映射、JSON Schema 转换校验 |
| **改动内置工具 (View / Edit / Grep / Dir)** | [`src/tools/builtin/`](src/tools/builtin/) | [`files.py`](src/tools/builtin/files.py), [`search.py`](src/tools/builtin/search.py), [`artifacts.py`](src/tools/builtin/artifacts.py), [`assembly.py`](src/tools/builtin/assembly.py) |
| **改动 Shell 命令行执行工具** | [`src/tools/builtin/shell.py`](src/tools/builtin/shell.py)<br>[`src/services/bash_shell/`](src/services/bash_shell/) | `BashTool`, `services/bash_shell/runner.py` (独立沙箱子进程、超时与配额) |
| **改动人机协同审批 (HITL) 触发与判定** | [`src/agent_runtime/guardrails/permissions.py`](src/agent_runtime/guardrails/permissions.py)<br>[`src/agent_runtime/approval/binding.py`](src/agent_runtime/approval/binding.py) | `check_permission()`, `compute_approval_id()`, `ApprovalStore` |
| **改动系统提示词装配 / 动态注入** | [`src/agent_runtime/context/prompt.py`](src/agent_runtime/context/prompt.py)<br>[`src/agent_runtime/context/rules.py`](src/agent_runtime/context/rules.py) | `assemble_system_prompt()`, `.agentrules` 解析, 动态分区合并 |
| **改动观察值裁剪 / Token 预算压减** | [`src/agent_runtime/context/observation.py`](src/agent_runtime/context/observation.py)<br>[`src/agent_runtime/context/task_compaction.py`](src/agent_runtime/context/task_compaction.py) | `distill_observation()`, `compact_turns()`, Token 截断保留头尾 |
| **改动 SQLite 存储 / 数据库表结构** | [`src/agent_runtime/memory/store.py`](src/agent_runtime/memory/store.py) | `SQLiteStore`（workspaces, sessions, tasks, turns, memory 增删改查） |
| **改动记忆提炼与注入 (Memory Flow)** | [`src/agent_runtime/runtime/memory_flow.py`](src/agent_runtime/runtime/memory_flow.py)<br>[`src/agent_runtime/memory/compactor.py`](src/agent_runtime/memory/compactor.py) | 任务完成后的自动记忆沉淀与跨会话工作区记忆注入 |
| **改动 MCP 服务集成与工具协议** | [`src/mcps/manager.py`](src/mcps/manager.py)<br>[`src/mcps/adapter.py`](src/mcps/adapter.py) | `McpManager`, stdio / SSE 进程保活, MCP 协议转换为 Agent 工具 |
| **改动技能注册表与加载** | [`src/agent_runtime/skills/registry.py`](src/agent_runtime/skills/registry.py)<br>[`src/tools/builtin/skills.py`](src/tools/builtin/skills.py) | `SkillsRegistry`, SKILL.md 动态装配, `load_skill` 内置工具 |
| **改动子智能体受限派发 (Delegate)** | [`src/agent_runtime/subagents/delegate/`](src/agent_runtime/subagents/delegate/) | `runner.py`, `tool.py`, 独立生命周期管理与上下文收窄隔离 |
| **改动配置文件加载与环境变量** | [`src/baseagent_config/loader.py`](src/baseagent_config/loader.py)<br>[`src/baseagent_config/models.py`](src/baseagent_config/models.py) | `load_config()`, `Config` Pydantic 模型定义, `.env` 映射 |
| **改动网络搜索工具 / Sidecar** | [`src/agent_runtime/websearch/`](src/agent_runtime/websearch/)<br>[`src/services/web_search/`](src/services/web_search/) | 搜索客户端、独立爬虫与安全网络网关 |

---

## 3. 完整目录树与各包职责 (Package Map)

```
backend/src/
├── agent_runtime/                    # 【核心 Agent 运行时】
│   ├── api/                          # Web 接入层（FastAPI）
│   │   ├── app.py                    # FastAPI 实例创建、CORS、SPA 静态前端托管挂载
│   │   ├── deps.py                   # FastAPI 依赖注入（获取 store, runner, hub 等实例）
│   │   ├── errors.py                 # 全局异常捕获与标准化 JSON 错误响应转换
│   │   ├── schemas.py                # Pydantic 请求与响应 DTO（TaskCreate, SessionResponse 等）
│   │   ├── security.py / token.py    # Bearer 令牌鉴权与权限校验
│   │   ├── fsguard.py                # 路径越界安全防御
│   │   └── routes/                   # 各领域 REST 路由
│   │       ├── tasks.py              # 任务创建、取消、审批处理、单任务 SSE 事件推流
│   │       ├── sessions.py           # 会话增删改查、会话重命名、会话模糊搜索
│   │       ├── workspaces.py         # 工作区增删改查
│   │       ├── settings.py           # 模型切换、上下文限额覆盖
│   │       ├── prompts.py            # 系统提示词分片获取与覆盖
│   │       ├── skills.py             # 技能列表获取与开关切换
│   │       ├── mcp.py                # MCP 服务增删改查、状态探测
│   │       ├── system.py             # 健康检查、/events 全局广播 SSE 流
│   │       └── files.py              # 工作区文件目录树浏览与文本预览
│   │
│   ├── runtime/                      # 任务运行主流程
│   │   ├── runner.py                 # TaskRunner：驱动单任务从创建、启动到成功的协程编排
│   │   ├── service.py                # TaskService：提供对外统一接口
│   │   ├── events.py                 # 任务事件包装（tool.output, task.finished 等）
│   │   ├── active.py                 # 全局活跃中任务实例跟踪（取消、暂停句柄）
│   │   ├── memory_flow.py            # 任务生命周期中的记忆沉淀流动
│   │   ├── resume.py                 # 挂起恢复处理
│   │   └── overrides.py              # 动态覆盖运行时参数
│   │
│   ├── loop/                         # Agent 主循环内核（纯净无 HTTP 依赖）
│   │   ├── kernel.py                 # ReAct 循环主体：组装上下文 -> 调 LLM -> 调工具 -> 产出观察
│   │   ├── tools.py                  # 工具执行调度与超时拦截
│   │   ├── scratch.py                # 规划、草稿本与临时结论管理
│   │   ├── proxy.py                  # 代理包装
│   │   └── urls.py                   # 提取循环中出现的 URL
│   │
│   ├── context/                      # 上下文工程
│   │   ├── prompt.py                 # 组合静态/动态提示词、工具描述、系统规则
│   │   ├── observation.py            # 工具大体积输出裁剪（保留头尾，防止占爆上下文）
│   │   ├── task_compaction.py        # 历史回合压缩（折叠早前工具日志）
│   │   └── rules.py                  # 解析工作区根目录的 .agentrules 规则文件
│   │
│   ├── llm/                          # 大语言模型网关
│   │   ├── gateway.py                # 统一网关：多服务商路由、并发限制、重试与自适应熔断
│   │   ├── wire.py                   # 底层 HTTP 流式协议解析、思维链 (reasoning_content) 分流
│   │   ├── structured.py             # 强制模式与工具调用契约对齐
│   │   └── types.py                  # ModelRequest, ModelResponse 等数据类
│   │
│   ├── guardrails/                   # 安全护栏（纯函数，高安全性）
│   │   ├── permissions.py            # read_only / workspace_write / unrestricted 三级权限判定
│   │   ├── loops.py                  # 死循环与重复调用指纹检测
│   │   ├── injection.py              # 提示词注入攻击静态扫描
│   │   └── envelope.py               # 安全信封包装
│   │
│   ├── approval/                     # 人工协同审批 (Human-in-the-Loop)
│   │   ├── binding.py                # 生成防篡改的 approval_id 并绑定决策
│   │   └── errors.py                 # 审批异常
│   │
│   ├── memory/                       # 记忆系统与持久化底层
│   │   ├── store.py                  # SQLiteStore：核心本地数据库，存储会话、任务、消息、记忆
│   │   ├── compactor.py              # 记忆归纳与语义压缩
│   │   └── models.py                 # 数据库模型
│   │
│   ├── observability/                # 可观测性
│   │   ├── hub.py                    # EventHub：多播事件总线（任务级与跨任务全局广播）
│   │   ├── trajectory.py             # 结构化轨迹日志（JSONL），支持后续评测回放
│   │   └── redaction.py              # 敏感信息（API Key / 密码等）脱敏过滤
│   │
│   ├── skills/                       # 技能注册
│   │   └── registry.py               # 扫描指定目录下的 SKILL.md 并导出规范
│   │
│   ├── subagents/                    # 子智能体
│   │   └── delegate/                 # 委托子智能体执行受限任务（如研究、查资料）
│   │
│   └── websearch/                    # 运行时网络检索工具集成
│       └── runner.py                 # 搜索驱动与结果解析
│
├── baseagent_config/                 # 【配置中心】
│   ├── models.py                     # 全局 Pydantic 配置模型
│   ├── loader.py                     # YAML 与环境变量 (.env) 合并加载器
│   └── env.py                        # 环境变量覆盖提取
│
├── tools/                            # 【工具实现层】
│   ├── core/                         # 工具底座
│   │   ├── registry.py               # ToolRegistry：工具注册、命名空间与描述导出
│   │   ├── dispatcher.py             # 派发器：校验权限、拦截高危操作、调度执行
│   │   └── sidecar.py                # 与独立进程通信的 HTTP 客户端底座
│   │
│   └── builtin/                      # 原生内置工具集
│       ├── shell.py                  # bash 工具（向 bash_shell 服务发起执行请求）
│       ├── files.py                  # view_file, write_to_file, replace_file_content
│       ├── search.py                 # grep_search, find_by_name
│       ├── artifacts.py              # 交付物生成与记录
│       └── skills.py                 # load_skill 动态引入
│
├── services/                         # 【独立 Sidecar 微服务（独立子进程）】
│   ├── bash_shell/                   # 隔离沙箱终端服务（自带资源配额与日志截断）
│   │   ├── runner.py                 # 实际启动 subshell 执行命令
│   │   └── app.py                    # FastAPI 极简独立服务
│   └── web_search/                   # 独立网络搜索与抓取服务
│
├── mcps/                             # 【Model Context Protocol 支持】
│   ├── manager.py                    # McpManager：管理第三方 MCP Server 子进程/连接
│   └── adapter.py                    # 将 MCP 标准工具转为 Agent 原生工具注册
│
└── evaluation/                       # 【评测与基准测试】
    └── agent_bench/                  # 评测用例集、打分指标与报告生成
```

---

## 4. 任务完整执行生命周期 (Request-to-Result Flow)

理解一次请求在后端经历的完整时序：

```mermaid
sequenceDiagram
    participant Client as 前端/客户端
    participant API as API 路由 (routes/tasks.py)
    participant Runner as 运行时 (runtime/runner.py)
    participant Loop as 循环内核 (loop/kernel.py)
    participant LLM as 模型网关 (llm/gateway.py)
    participant Guard as 安全护栏 (guardrails)
    participant Tools as 工具派发 (tools/core)
    participant Hub as 事件总线 (observability/hub.py)

    Client->>API: POST /api/v1/sessions/{id}/tasks (提交目标)
    API->>Runner: 启动后台任务 run_task()
    API-->>Client: 返回 Task DTO (queued)
    Client->>API: GET /api/v1/tasks/{id}/stream (建立 SSE 监听)
    
    loop ReAct 循环 (直到完成或需要审批)
        Runner->>Loop: 执行单步 step()
        Loop->>LLM: 发送上下文 (提示词+历史+观察)
        LLM-->>Loop: 返回推理与 ToolCall
        Loop->>Guard: 权限与循环检测
        alt 越级操作需要审批
            Guard-->>Runner: 触发 WaitingForApproval
            Runner->>Hub: 广播 task.waiting_for_approval
            Hub-->>Client: SSE 推送审批通知 (挂起等待)
            Client->>API: POST /api/v1/tasks/{id}/approve
            API->>Runner: 恢复执行
        end
        Loop->>Tools: 调用工具 execute()
        Tools-->>Hub: 广播实时 tool.output (增量推流)
        Tools-->>Loop: 返回执行结果 (Observation)
        Loop->>Loop: 裁剪超长输出 (distill_observation)
    end
    
    Runner->>Hub: 广播 task.finished (succeeded / failed)
    Runner->>Runner: 异步提取并沉淀会话记忆 (memory_flow.py)
    Hub-->>Client: SSE 结束
```

---

## 5. 后端自动化验证命令 (Dev & Test Commands)

在对后端代码进行修改后，**无需启动浏览器**，直接在终端执行以下标准化命令闭环验证：

```bash
# 1. 静态风格与语法检查 (Ruff)
uv run ruff check

# 2. 核心轻量测试套件 (快速闭环，数秒内完成)
uv run pytest tests/test_api_routes.py tests/test_global_events.py tests/test_loop_kernel.py

# 3. 运行全量离线单测 (覆盖运行时/工具/MCP/API，不包含耗时的外部真模型测试)
uv run pytest

# 4. 架构分层依赖合规检查 (确保未发生自底向上的跨层反向依赖)
uv run lint-imports
```
