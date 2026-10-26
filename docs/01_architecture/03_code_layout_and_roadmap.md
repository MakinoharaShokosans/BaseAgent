# 代码结构与实现路线

## 1. 依赖方向

模块按层组织，依赖只能**从上往下**：

```
        api                       HTTP 接入：路由、DTO、闸门、错误映射
         │
      runtime                     任务运行：注册表、主循环、检查点、审批
         │
 ┌───────┼────────┬───────────┬────────────┐
 ▼       ▼        ▼           ▼            ▼
subagents  context  llm      tools        guardrails
 │         │         │         │              │
 └─────────┴─────────┴────┬────┴──────────────┘
                          ▼
                        core                    纯契约：消息、状态、错误、工具协议
```

规则：

1. **`core` 零依赖**：只含数据类型与纯函数，不做 I/O，不依赖其他层。
2. **API 只依赖运行时的门面**：不直接碰网关、工具、记忆的内部。
3. **工具层不依赖运行时**：只依赖 `agent_runtime.core`（工具协议、错误）和 `agent_runtime.guardrails`（注入扫描等纯函数）。工具协议只在 `core` 定义一份，`tools/core` 不重复定义。旧版 `tools.builtin` 为了拿技能注册表而导入运行时模块，新版已消除：`load_skill` 只依赖两个只读协议，注册表在 `agent_runtime.skills`，由装配层接上。
4. **sidecar 服务完全独立**：`services/*` 不导入 Agent 运行时的任何东西，只通过 HTTP 对话，现有约束保持。
5. **子智能体依赖网关、工具、护栏**，不依赖主循环。主循环只通过工具注册表看到它们。
6. **护栏是纯函数集合**：预算、权限、循环检测、金丝雀、注入扫描、脱敏，都不依赖具体的模型或工具实现，可离线单测。
7. **观测是横切旁路**：各层可以依赖事件、日志、追踪的接口，但这些接口自己不依赖任何业务层。
8. **配置是共用底座**：`aegis_config` 是独立的顶层包，不依赖项目内任何其他部分，所有层都可以导入它。独立成包而不放进 `agent_runtime`，是因为 sidecar 服务（规则 4）不能依赖运行时，却同样要读自己那一节配置。

用一个依赖检查（如 `import-linter` 的契约）在测试里强制这些方向，违反即失败。

> 技术栈见 [技术选型](05_tech_stack.md)。仓库结构取 `backend/`、`frontend/`、`config/`、`docs/`，下面的 `AegisAgent/src/` 即指 `backend/src/`。

## 2. 目标目录

```
AegisAgent/src/
├── agent_runtime/
│   ├── core/            消息、状态、编解码、错误、工具协议、信封
│   ├── loop/            主循环、阶段、路由表、调度
│   │   ├── stages/      planner / budget_guard / executor / tool_runner / evaluator
│   │   └── routes.py
│   ├── checkpoint/      检查点存储、状态编解码、恢复扫描
│   ├── approval/        权限闸门、审批协议、白名单
│   ├── llm/             网关、端点池、熔断、重试、结构化输出、思考内容
│   ├── context/         提示词装配、观察值裁剪、窗口折叠与压缩
│   ├── memory/          工作区与会话记忆、存储、压缩器
│   ├── guardrails/      预算、账本、权限判定、循环检测、金丝雀、注入扫描、授权窗口
│   ├── subagents/       research/ 与 delegate/
│   ├── skills/          技能注册表
│   ├── observability/   事件、总线、轨迹、日志、追踪、脱敏
│   ├── prompts/         提示词模板
│   └── api/             FastAPI 应用、路由、DTO、闸门、任务注册表
├── tools/
│   ├── core/            注册表、派发器、sidecar 客户端、Schema 导出（协议在 agent_runtime.core）
│   └── builtin/         view_file / write_file / edit_file / list_dir / grep / read_artifact / bash / load_skill
├── mcps/                管理器、适配器、消毒、命名规范化
├── services/
│   ├── bash_shell/      沙箱
│   └── web_search/      检索与抓取
├── skills/              内置技能包
└── evaluation/
    └── agent_bench/     轨迹评测
```

`evaluation/rag_bench` 随 RAG 删除。

### 2.1 与现状的对应

| 现状 | 去向 |
|---|---|
| `state.py`（含 LangGraph `add_messages`） | `core/state.py`，消息改为自有类型 |
| `workflow.py`（装配 + 图构建 + 驱动） | 拆成 `runtime` 门面、`loop/driver.py`、任务级装配 |
| `nodes/*`、`edges/*`、`routing.py` | `loop/stages/*`、`loop/routes.py`，逻辑保留，去掉图引擎依赖 |
| `checkpoint.py`（`AsyncSqliteSaver`） | `checkpoint/store.py`，自有表与编解码 |
| `llm/*` | `llm/*`，补熔断、截止时间、能力声明、结构化输出与思考内容 |
| `context.py`、`guardrails/observation_pruner.py`、`memory/*` | `context/*` 与 `memory/*` |
| `research/*`、`subagent/*` | `subagents/research`、`subagents/delegate` |
| `code_search/*`、`api/routes/rag.py`、`tools/builtin/rag_search.py`、`evaluation/rag_bench` | **删除** |
| `AegisRAG/` | **删除整个工程** |

## 3. 实现路线

分阶段，每一阶段结束时系统都应该是**可运行、可测试**的，不留"一半新一半旧"的状态。

### 阶段 -1：项目骨架（已完成）

目录结构、`pyproject.toml`（依赖、pytest、ruff、pyright、import-linter 契约）、`Makefile`、`.pre-commit-config.yaml`、前端脚手架（Vite、TypeScript、Tailwind、ESLint、Vitest、同源代理注入令牌）、参考配置，以及 51 个后端测试（41 个冒烟测试：包可导入、配置自洽、密钥不入库；10 个架构契约测试）。

**已验证**（联网安装依赖后，`make check` 退出码 0）：

| 检查 | 结果 |
|---|---|
| `ruff check` / `ruff format` | 通过 |
| `pyright`（核心包严格模式） | 0 错误 |
| `lint-imports` | 6 条契约成立（后加配置包契约后为 7 条） |
| 契约的有效性 | 专门的测试向临时副本注入 8 种违规（逐条打破对应契约）和 6 种合法依赖（不打破任何契约）。**空代码库上契约平凡成立，所以这组测试必须保留** |
| 前端 `tsc`、`eslint`、`vitest`、`prettier`、`vite build` | 通过 |
| ESLint 对 `dangerouslySetInnerHTML` 的禁用 | 已用临时违规文件验证会报错 |
| 开发代理的令牌注入 | 已用本地回显后端验证：请求头在服务端侧注入；页面与 JS 中不含令牌；轮换令牌无需重启前端；无令牌文件时不发送请求头；`Host` 被改写为后端地址 |

**发现并修复的问题：** ① 配置里 MCP 调用超时与工具派发超时同为 60 秒（冒烟测试发现），改为 50；② pnpm 12 默认拦截依赖的构建脚本，且不再读取 `package.json` 里的 `pnpm` 字段，需在 `pnpm-workspace.yaml` 里用 `allowBuilds` 只放行 `esbuild`；③ 若干行过长。

### 阶段 -0.5：配置加载器（已完成）

第一个垂直切片，测试先行：[`aegis_config`](../../backend/src/aegis_config/) 实现 [配置](04_configuration.md) 的全部加载规则。

| 内容 | 实现 |
|---|---|
| 模型 | `models.py`：20 节对应的 pydantic 模型，未知键报错、加载后不可变，字段默认值即“代码内置默认值”；比例、数量关系、回环、权限级别、正则可编译、MCP 敏感值须为 `env:` 引用等规则 |
| 来源与优先级 | `sources.py` + `loader.py`：显式传入（命令行）> `AEGIS_<节>__<键>` > `config.toml` > 默认值，由 pydantic-settings 合并（嵌套表按键合并，不整体替换） |
| 依赖环境的校验 | `loader.py`：文件发现、数据目录创建与可写、相对路径不得逃出数据目录、缺密钥的云端端点被跳过并告警、某档无可用端点则拒绝启动、信任开关告警 |
| 环境变量边界 | 指向数组、表、整节的变量被拒绝；拼写错误的变量被拒绝并带上变量名 |
| 一致性 | 反向测试：参考配置只保留 `models` 时，其余全靠代码默认值，结果与完整文件一致；代码里每个字段都在参考文件中出现 |

验证：167 个后端测试全过（新增 116 个）；`lint-imports` 7 条契约成立，新契约“配置包零项目内依赖”同样有注入违规的测试；对校验规则做了变异检查（去掉回环校验和迟滞校验后，19 个测试按预期失败，恢复后全过）。

**发现并明确的歧义：** ① `app.home` 的注释曾说开发时可设为仓库内的 `./storage`，与“相对路径相对数据目录解析”矛盾——文件里的 `app.home` 现要求绝对路径或 `~`，开发时用环境变量 `AEGIS_HOME`（相对值按当前目录取绝对）；② 禁用根里的 `/` 只禁止把根目录本身当工作区，其余禁用根连同其下路径都禁止，这样 `browse_roots = ["/"]`（整盘浏览）才不与之冲突。

### 阶段 0：安全与观测的低成本修复

不依赖架构改造，可以在现有代码上先做，收益立竿见影：

| 事项 | 文档 |
|---|---|
| 子进程使用最小环境变量；监督器传白名单环境 | [沙箱 §5.1](../05_security/02_shell_sandbox.md) |
| 轨迹写入 `tool_args`（脱敏） | [轨迹 §1.2](../08_observability/02_trajectory_and_logs.md) |
| `web_search` 增加地址校验与响应大小上限 | [web_search §4](../04_tools/06_web_search_service.md) |
| 信封正文转义 | [信任 §4.4](../05_security/03_trust_and_injection.md) |
| 研究子智能体接入预算账本，并限制调用次数 | [账本 §7](../06_subagents/03_budget_ledger.md) |
| 修复账本重复扣减 | 同上 |
| API 令牌认证、工作区路径校验、目录浏览收紧 | [API 接入控制](../09_api/03_access_and_errors.md) |
| 沙箱拒绝原因传达给模型 | [Shell 工具 §1.2](../04_tools/03_shell_tool.md) |
| 前端错误信封解析、SSE 重连与序号检测、去掉 URL 中的令牌 | [前端 §7、§8](../10_frontend/01_architecture.md) |

### 阶段 1：去掉 RAG

纯删除，风险低。顺序：前端 RAG 面板与状态 → API 路由与 DTO → `rag_search` 与代码检索子智能体 → 配置与启动器 → `AegisRAG` 目录 → 文档与提示词措辞。完成后跑全部现有测试。

### 阶段 2：消息类型与状态（已完成）

新项目没有旧代码要替换，所以原计划里“全工程替换 `langchain_core.messages` 导入、仍在 LangGraph 上运行”两步不再适用，只剩类型本身。已在 `agent_runtime.core` 实现，测试先行，详见 [状态模型 §3.1](../02_runtime/01_state_model.md)。

| 内容 | 位置 |
|---|---|
| `Message`、`ToolCall`（含 `reasoning`、`reasoning_state`、`produced_by`），构造时校验角色约束 | `core/messages.py` |
| 原子对断言 `assert_atomic_pairs`（含等待审批时放宽最后一组） | 同上 |
| `AgentState`（不可变）、`apply_update`、`new_state`、`check_invariants`；`Milestone`、`FailedAttempt`、`PendingApproval`、`ApprovalDecision` | `core/state.py` |
| JSON 编解码：严格、`reasoning_state` 字节级无损、`schema_version` 与迁移表机制 | `core/codec.py` |
| 错误类型 | `core/errors.py` |

验证：93 个新测试，含 Hypothesis 往返性质测试（任意消息序列经 JSON 往返相等；任意 `reasoning_state` 载荷编码两次字节相同）和篡改检查点的安全测试；`pyright` 严格模式 0 错误（`core` 在严格名单内）；`lint-imports` 7 条契约成立。变异检查：去掉原子对的缺失检查、把消息追加改成覆盖，各有测试按预期失败，恢复后全过。

**与计划的偏差：** ① `AgentState` 用不可变 dataclass 而非 TypedDict，文档里循环伪代码的 `state["phase"]` 在实现时写成 `state.phase`；② `core` 与配置包不能互相依赖，所以权限级别的取值在两边各有一份，有测试保证一致。

### 阶段 3：自研主循环与检查点

**第一块（已完成）：检查点存储。** `agent_runtime.checkpoint` 的 `CheckpointStore`（`commit` / `load` / `delete` / `list_unfinished`）与 `scan_for_recovery`，设计见 [检查点 §2-6](../02_runtime/03_checkpoint_and_resume.md)。30 个测试，含强杀进程后数据仍在、两个连接并发提交、篡改与损坏检测、不变量不满足时不落盘；变异检查（去掉不变量断言、`seq` 不自增、去掉列与状态一致性核对）各有测试失败。

**第二块（已完成）：主循环骨架。** `agent_runtime.loop` 的路由表、`TaskRuntime`、`drive`，以及 `core` 里的 `ApprovalRequired` 信号，用脚本化的假阶段函数测试，细节见 [主循环 §3.1](../02_runtime/02_main_loop.md)。47 个测试，覆盖：完整一轮的阶段顺序与每阶段一次检查点；先提交后发事件；提交失败时不发事件也不继续；事件通道故障不影响任务；挂起（不应用该阶段任何结果、事件在提交之后、停在原阶段）与批准后继续；其他异常与取消时状态停在上一次提交点；原子对违规不写检查点；与真实 `CheckpointStore` 联用的“崩溃后换新连接继续”。变异检查（事件挪到提交前、事件异常不再被吞、空里程碑视为完成）均有测试失败。

**发现并修正：** 文档伪代码的原子对断言会误报每一次 executor 提交，已改为 `phase == "tool_runner"` 时放宽最后一组（同步改了 `core.check_invariants`）。

**第三块（已完成）：崩溃恢复与续跑。** `checkpoint.recover_interrupted_dispatch`（合成“执行结果未知”）、`approval` 的决策解析与绑定校验（含确定性 `approval_id`）、新增的 `agent_runtime.runtime` 门面包里的 `resume_task` 与 `ActiveTasks`，细节见 [检查点 §4.1、§5](../02_runtime/03_checkpoint_and_resume.md)。55 个测试，含 Hypothesis 性质（任意批次、任意已派发子集、任意幂等划分，恢复后状态都满足不变量且每个调用恰有一条结果）；变异检查（一律重试、不提交准备后的状态、不重新计算 approval_id、不释放占位）均有测试失败。

**结构调整：** `loop`、`checkpoint`、`approval` 是互相独立的同层模块，不能彼此导入，所以组合它们的 `resume_task` 放进新包 `agent_runtime.runtime`，在三者之上、`api` 之下；依赖方向契约相应更新（分层加一层，工具层/核心/观测的禁止清单加入它），并各有注入违规的测试。

**第四块（已完成）：`budget_guard` 与物理预算守卫。** 纯判定 `evaluate_budget`、带挂钟与“只告警一次”的 `PhysicalBudgetGuard`、阶段 `make_budget_guard_stage`，细节见 [主循环 §5.1](../02_runtime/02_main_loop.md)。40 个测试，含：每个维度在比例线与上限处的边界、使用量增加时判定只会变严（Hypothesis）、永远不收敛的任务在步数/Token 上限处必然终止且告警先于熔断、续跑后挂钟重置而步数与 Token 沿用。变异检查（熔断改成 `>`、告警每次都发、去掉软告警、挂钟从 0 起算）均有测试失败。

**第五块（已完成）：新任务入口与任务运行器。** `create_task` / `run_task`、`TaskRunner`（后台运行、状态映射、并发名额、取消、关停、启动接管）、配置装配，细节见 [任务生命周期 §2.4.1](02_task_lifecycle.md)。62 个测试，含：名额满不留痕迹、挂起不占名额、续跑校验错误同步抛出且不占名额、取消落在 executor 与 tool_runner 之间时补齐配对并持久化、取消后启动扫描不再发现它、关停不写终态、新进程接管上次中断与挂起的任务并可续跑、已存在的 `task_id` 不被覆盖。变异检查（挂起占名额、去掉提交闸门、覆盖已有任务、取消不补配对、取消不落盘）均有测试失败。

**阶段 4 之后（已完成）：planner 与 evaluator。** `agent_runtime.loop.stages` 的契约、两个阶段与 `guardrails.canary`，细节见 [planner 与 evaluator](../02_runtime/05_planner_evaluator.md)。97 个测试：契约（别名、截断、白名单、必填结论）、planner 的规划与完工判定（待办压过完成声明、零步无交付不算完成、状态更新不得放行路由）与降级、evaluator 的通过/打回/记忆沉淀/降级按未达成/打回上限、网关失败转终止、金丝雀、接进 `drive` 的端到端、以及接真实网关（模拟网络）的两个冒烟。变异检查 13 项（含上述每条规则与金丝雀）均有测试失败，其中“不重开里程碑”最初存活，补测试后被抓到。**成功不再置 `should_terminate`**（旧版会让状态被映射成 `terminated`）；**打回上限到了就终止而不是放行**（旧版放行与“宁严勿松”矛盾）。

**executor 与 tool_runner（已完成）。** 细节见 [executor 与 tool_runner](../02_runtime/06_executor_tool_runner.md)。157 个测试：工具协议、注册表与派发器（并发、顺序、超时、信任级）；权限判定（名单、bash 分类、大小写与连接符、已知防不住的固定、非法正则、签名）；指纹与连续错误（含 Hypothesis）；观察值治理（阈值边界、JSON 轮廓、关键行、巨长行、豁免、落盘失败、路径净化）；executor（快速档请求、步数、格式错误调用的标记、金丝雀、网关失败）；tool_runner（挂起前无副作用、确定性请求、批准/拒绝/`always`、绑定校验、过期审批、派发前标记先于任何工具、整批拦截、连续错误通知、信封）；接进 `drive` 的端到端（审批挂起后续跑、拒绝、标记可被崩溃恢复读取、重复调用被截断）。变异检查 34 项（不写标记、标记里带悬空审批、白名单失效、不核对绑定、不拦指纹、人工拒绝计为失败、幻觉工具打扰人、格式错误的调用被执行、可信内容也包信封、executor 不加步数、不查金丝雀、权限比较边界、签名依赖键序、观察值阈值与豁免与路径、派发无超时、注册表重名与不可信放行等）均有测试失败。

五个阶段至此齐全，但缺内置工具、提示词装配器、事件总线与 HTTP 层、任务元数据表，所以还不能端到端跑一个真实任务。

**内置工具（已完成）。** 细节见 [内置工具 §11](../04_tools/02_builtin_tools.md)、[Shell 工具 §8](../04_tools/03_shell_tool.md)、[Skills §14](../04_tools/05_skills.md)。204 个测试（含 `tool_runner` 的绑定与产物登记）：路径沙箱（`..`、绝对路径、符号链接进出、不存在的目标、NUL）；`view_file`（行范围、续读提示、二进制、大文件、编码与 CRLF）；`write_file`（原子性、权限位、字节上限、覆盖前备份且备份失败不写）；`read_artifact`（只读已登记、不接受路径、根目录之外拒绝、`grep`、来源信任）；sidecar 客户端（五类失败统一映射、连接池共享与重开）；`bash`（响应契约、超时夹取、排队不计失败、403 与 422 带规则与原因）；技能注册表（来源门控、不得顶替、符号链接、冻结、治理、清单）；`load_skill`（信封转义）；注入扫描；装配（缺依赖不注册、Schema 逐字节稳定）；接进 `tool_runner` 的闭环（截断后的命令日志经 `read_artifact` 取回）。变异检查 41 项（含路径沙箱、备份、原子写、二进制与大文件判定、产物根目录与信任推断、响应契约、超时夹取、排队、策略拒绝的规则、sidecar 状态码、符号链接、不得顶替、正文扫描范围、信封转义、绑定与登记）均有测试失败，其中 3 项最初存活（拒绝规则未进 `meta`、嵌套在辅助目录里的符号链接、产物可信名单为空），补测试后被抓到。**反向依赖已消除**：`load_skill` 只依赖两个只读协议，注册表在 `agent_runtime.skills`。

**提示词装配器（已完成）。** 细节见 [提示词装配 §10](../03_context/03_prompt_assembly.md)。125 个测试（含信封 80 个）：段序与角色映射、空段不留标题、金丝雀只在系统层且不进检视、记忆信封的来源与信任、构造顺序无关、任务内只追加（除尾部外逐条相同）、同会话两任务共享整个前缀、不同阶段只差尾部、某段渲染失败只跳过该段、`describe` 与真实请求同源、规则文件（默认关、未启用不读、符号链接出界、截断、按修改时间失效）、提示词库（路径花招、缓存与重载）、接进 planner 的真实请求。变异检查 25 项均有测试失败，其中 5 项最初存活（规则状态闸门、失败尝试的信任标记、已并入摘要的去重、已并入失败的去重、提示词库名字校验），补测试后被抓到。

**记忆与会话压缩（已完成）。** 细节见 [记忆 §11](../03_context/04_memory.md)、[窗口与压缩 §8](../03_context/02_window_and_compaction.md)。99 个测试：存储（迁移与版本、级联、水位线单调、并发读-改-写不丢、不存在即报错、路径冲突）；切片（对齐、保底、Hypothesis）；沉淀（失败任务只存失败、不可信标注、去重、注入丢弃、超限触发）；合并（出处、不静默丢失、上限、摘要）；压缩器（触发、滞回、退避序列、失败不丢记忆、提炼期间的新沉淀不丢、并发只提炼一次）；运行时流程（装入、金丝雀脱敏、收尾不抛）；网关提炼器。变异检查 42 项均有测试失败，其中 4 项最初存活（失败尝试未声明取代的补回、既有条目的出处也算真实；另有两处等价的防御代码被简化掉）。

**任务级观察值折叠（已完成）。** 细节见 [窗口与压缩 §8.1](../03_context/02_window_and_compaction.md)。30 个测试：不到触发线不动、触发线含边界、基础部分计入、最近 K 步完整、结构与配对不变、存根确定性与内容、没有离线副本/豁免工具/不更短的不折叠、成批与单调（含 Hypothesis）、装配器里只折任务内往返且不改状态、折叠之间请求只在末尾增长。变异检查 24 项均有测试失败，其中 7 项最初存活：触发边界、取错句柄、纯文本指令算一步、基础规模、传错流，补测试后被抓到；另两处是等价的防御代码，已删掉。

仍缺：任务内压缩（级别 3，默认关）、事件总线与 HTTP 层、任务元数据表、`bash_shell` 与 `web_search` 两个 sidecar 服务端、`edit_file` / `list_dir` / `grep`。


1. 路由函数去掉 `END`，改用哨兵；写 `apply_update`。
2. 写 `drive` 循环与 `CheckpointStore`。
3. 审批改为抛 `ApprovalRequired` 加落盘挂起，`tool_runner` 的闸门在阶段开头。
4. 派发前标记、崩溃恢复、启动扫描。
5. 删除 `langgraph*` 依赖，重写依赖图引擎的测试。

### 阶段 4：网关与结构化输出

**第一块（已完成）：网关核心。** `agent_runtime.llm` 与 `core.ToolSchema`，细节见 [网关 §16.1](../07_llm_gateway/01_gateway.md)。134 个测试，全部用 `httpx2.MockTransport` 与假时钟离线运行：请求构造（DeepSeek 思考开关与强度、回传策略、合并连续 user）、响应解析（工具调用规范化、截断、用量与估算）、退避/熔断/自适应并发的纯逻辑、以及网关端到端（重试、降级、熔断与探测、截止时间、鉴权屏蔽、能力过滤、客户端复用、脱敏、从配置装配）。变异检查（熔断不记失败、鉴权不屏蔽、不脱敏、不查截止时间、429 不减半、带工具仍开思考、回传不看产生者、去掉抖动）均有测试失败。

**第二块（已完成）：结构化输出。** `agent_runtime.llm.structured` 与 `LLMGateway.invoke_structured`，细节见 [结构化输出 §13](../07_llm_gateway/02_structured_output.md)。66 个测试：JSON 抽取（含 Hypothesis 往返）、校验错误描述（不回显输入）、严格 Schema 简化器、四条请求路径的选择与线上形态、修复重试（带错误、回到同一端点、可配次数）、截断（不解析、加倍上限）、内容过滤、降级结果与计数。变异检查（截断也解析、截断片段凑数、不封闭对象、回显输入、忽略 `prefer_schema`、修复不回原端点、多修复一次、内容过滤也重试）均有测试失败。

尚未做：各场景的 Pydantic 契约模型与降级处置（随 planner / evaluator 等真实阶段落地）、提示词与模型字段一致性检查、`opaque` 回传、密钥重新解析的 `reload()`、`LLMBadRequest` 触发折叠的联动（属上下文阶段）。

能力声明、熔断、截止时间、用量估算、工具调用规范化；Pydantic 契约与修复重试；思考内容的取、存、回传策略；压缩器改走网关。

### 阶段 5：上下文与记忆

`read_artifact`；旧观察值折叠；摘要字段拆分；任务事实沉淀与记忆上限；记忆的信任渲染；项目规则的门控与槽位；提示词按"固定前缀、对话记忆、数据层、最新需求、任务内往返、阶段指令"重排并满足缓存规则。

### 阶段 6：观测与 API 契约

脱敏函数；事件持久化与 `llm.call` 外置；日志上下文绑定；span 与导出器；任务列表接口与相关 API 改进；事件版本字段。

### 阶段 7：工具与技能完善

`edit_file`、`list_dir`、`grep`；`write_file` 原子写入与备份；MCP 生命周期任务、名称规范化、退避重试；技能同名保护与正文扫描。

### 阶段 8：评测与文档收口

用真实模型跑代表性任务集，统计格式失败率、重试率、预算命中率；对照每份文档的"测试要点"查缺补漏；更新 README 与示例。

## 4. 每阶段的验收

| 阶段 | 验收标准 |
|---|---|
| 0 | 每项修复有一个失败在前、通过在后的回归测试 |
| 1 | 全仓库搜索不再出现 `rag`（除历史文档），现有测试全绿，启动无 8001 依赖 |
| 2 | 仍在 LangGraph 上运行的情况下，测试全绿；消息类型编解码往返相等 |
| 3 | `grep -r langgraph` 为空；崩溃恢复集成测试通过；审批跨进程重启可恢复 |
| 4 | 假模型驱动的网关测试覆盖重试、降级、熔断、截止时间；结构化输出各场景降级正确 |
| 5 | 长任务（超过 25 步）上下文规模受控；任务事实被沉淀；记忆上限生效 |
| 6 | 重启后时间线与调用检视器仍可用；追踪导出失败不影响任务 |
| 7 | 每个新工具覆盖路径逃逸与失败路径；MCP 在不同任务内发起与关闭无残留进程 |
| 8 | 评测报告可复现，指标与文档里的阈值对得上 |

## 5. 风险

| 风险 | 缓解 |
|---|---|
| 自研循环遗漏 LangGraph 提供的隐含语义（并发写合并、重试、递归上限） | 阶段 2 先把类型换掉并保持在 LangGraph 上运行；阶段 3 对照旧行为写等价性测试，包括使用相同的假模型脚本回放 |
| 检查点格式变更使旧任务无法续跑 | 不迁移旧库，使用新库文件名，旧文件保留；在文档与启动看板里说明 |
| 同时做太多变更难以定位问题 | 严格按阶段推进，每阶段一个可回退的提交序列 |
| 文档与实现脱节 | 每阶段结束核对对应文档的"测试要点"与"不变量"，不一致则以实现为准并修文档，或以文档为准并修实现，二选一，不留悬空 |
| 评测依赖真实模型，成本与稳定性 | 单元与集成测试全部用假模型；真实模型的测试保持默认跳过，手动触发 |

## 6. 开放问题汇总

下列问题散落在各文档的"待定"里，实现前需要逐个决定。已经定下来的在 [12_decisions](../12_decisions/) 中有记录。

| 问题 | 所在文档 |
|---|---|
| 任务内压缩（级别 3）是否做 | [窗口与压缩 §7](../03_context/02_window_and_compaction.md) |
| 审批是否设过期时间；`always` 签名粒度 | [审批 §10](../02_runtime/04_approval.md) |
| `write_file` 是否幂等 | [工具系统 §3.2](../04_tools/01_tool_system.md) |
| 记忆段放 `user` 侧已定，需用真实模型验证效果与端点兼容性 | [提示词装配 §9](../03_context/03_prompt_assembly.md) |
| 记忆是否检索式注入 | [记忆 §10](../03_context/04_memory.md) |
| 追踪是否直接采用 OpenTelemetry SDK | [追踪 §9](../08_observability/03_tracing.md) |
| MCP 是否做工具级授权 | [MCP §10](../04_tools/04_mcp.md) |
| 是否提供后台进程能力 | [Shell 工具 §7](../04_tools/03_shell_tool.md) |
