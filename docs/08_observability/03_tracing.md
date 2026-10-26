# 追踪

追踪回答的是"这个任务的调用树长什么样、时间花在哪"：哪个阶段调用了几次模型、哪次重试耗时最长、子智能体内部做了什么。它是事件、轨迹、日志之上的**结构化视图**，用 span 把它们关联起来。

这也是 [概览](../00_overview/README.md) 里的待决事项"去掉 LangChain 回调后，外部追踪如何接入"的答案。

## 1. 现状

`observability/langfuse_tracer.py` 提供 `build_langfuse_handler`，返回一个 LangChain 的 `CallbackHandler`，设计上挂到 LangGraph 的执行上。但读代码发现：

- **全工程没有任何调用点。** 这个函数从未被调用，Langfuse 追踪实际上从没接入过。
- 它依赖 LangChain 的回调体系；去掉 LangChain 之后，这条路径本身就失效了。
- `pyproject.toml` 里却声明了 `langfuse>=4.15.2` 依赖，白白拉进一批包。
- 文件顶部写着"Langfuse SDK 在大版本之间回调挂载方式多次破坏性变更"，说明这是个高耦合、易碎的集成点。

结论：现有实现是一份未接入的死代码，没有迁移价值。

## 2. 目标

1. **本地优先：** 不依赖任何外部服务也能得到完整的调用树。
2. **可选导出：** 需要时可把同一棵树导出到外部平台（Langfuse、任意 OpenTelemetry 后端），不改业务代码。
3. **旁路：** 导出失败、后端不可达、SDK 版本变化，都不得影响任务。
4. **不重复造事实：** 追踪使用的是事件与轨迹里已有的信息，只是多加了关联键和层级。

## 3. 内部 span 模型

```python
@dataclass
class Span:
    span_id: str
    parent_span_id: str | None
    trace_id: str            # = task_id
    name: str                # "task" | "stage:planner" | "llm" | "tool:bash" | "subagent:CodeInvestigator" …
    kind: Literal["task", "stage", "llm", "tool", "subagent", "research", "guard"]
    start_ts: float
    end_ts: float | None
    status: Literal["ok", "error", "cancelled"]
    attributes: dict[str, Any]       # 摘要级信息，已脱敏
```

调用树：

```
task（trace_id = task_id）
├─ stage:planner
│   └─ llm（tier、model、usage、attempts）
├─ stage:budget_guard
├─ stage:executor
│   └─ llm
├─ stage:tool_runner
│   ├─ tool:bash
│   ├─ tool:spawn_subagent
│   │   └─ subagent:CodeInvestigator
│   │       ├─ llm
│   │       ├─ tool:view_file
│   │       └─ llm（收尾提炼）
│   └─ guard:approval（挂起与恢复）
└─ stage:evaluator
    └─ llm
```

### 3.1 谁创建 span

| 位置 | span |
|---|---|
| 任务运行器 | `task` |
| 主循环 | 每个阶段一个 `stage:*` |
| LLM 网关 | 每次调用一个 `llm`，重试与降级作为该 span 上的事件或子 span |
| 工具派发器 | 每个工具调用一个 `tool:*` |
| 子智能体与研究运行器 | `subagent:*` / `research`，其内部的 `llm` 和 `tool` 是它的子 span |
| 护栏 | 审批挂起、熔断等作为 `guard:*` |

通过 `contextvars` 保存当前 span，创建子 span 时自动取当前的作为父。异步任务之间（`asyncio.gather` 并发派发）自动继承，不需要手工传递。

### 3.2 与现有记录的关系

- `span_id` 与 `parent_span_id` 写入轨迹每一条记录、事件信封和日志上下文，三者因此可以按 span 关联。
- 追踪**不是第四份数据**，它是在已有记录上加的关联键，加上 span 的起止时间与状态。起止时间由 span 上下文管理器自动记录。
- span 持久化：与轨迹一起写入 `storage/traces/{task_id}.spans.jsonl`，每个 span 结束时一行。进程崩溃时未结束的 span 缺失，续跑后新的 span 作为同一 trace 的兄弟节点。

### 3.3 续跑

崩溃或审批挂起后续跑，`trace_id` 不变（仍是 `task_id`），新的执行段在 `task` 下以一个 `resume` span 开始，表明这是第几次恢复。挂起等待审批的时间不计入任何执行 span，而是作为 `guard:approval` span 的持续时间，这样耗时分析不被人工等待扭曲。

## 4. 导出

```python
class SpanExporter(Protocol):
    async def export(self, spans: Sequence[Span]) -> None: ...
    async def shutdown(self) -> None: ...
```

| 导出器 | 说明 | 默认 |
|---|---|---|
| `JsonlExporter` | 写本地 `.spans.jsonl` | 开 |
| `OtlpExporter` | 通过 OpenTelemetry 协议导出到任意兼容后端（Jaeger、Tempo、Langfuse 的 OTLP 入口等） | 关 |
| `LangfuseExporter` | 直接使用 Langfuse SDK，映射 `llm` 为 generation，其余为 span | 关，可选，不装依赖则不可用 |

规则：

- 导出在**后台队列**里批量进行，队列有界，满了丢弃最旧的并计数，绝不阻塞主流程。
- 导出器的异常全部吞掉并记录警告；连续失败后暂停一段时间再重试。
- 导出器是插件：未安装对应依赖就不可用，核心不依赖它们。`pyproject.toml` 把 `langfuse`、`opentelemetry-*` 放到可选依赖组，不进入默认依赖。

### 4.1 属性与隐私

导出到外部平台意味着数据**离开本机**。规则：

1. 默认关闭所有远程导出，必须在配置里显式开启并配置目的地。
2. 导出的 `attributes` 只含**摘要级**信息：名称、耗时、Token、状态、模型名、错误类别。
3. 提示词与完整响应、工具入参、命令、观察值**默认不导出**。需要时通过独立开关 `tracing.export_content = true` 开启，开启时过脱敏函数，并在界面上显著提示"内容将发送到 X"。
4. 研究隔离区的页面正文**永不导出**。
5. 配置里的密钥只引用环境变量名，不存明文。

## 5. 配置

```toml
[tracing]
enabled = true
local_spans = true            # 写本地 .spans.jsonl
export_content = false        # 提示词与载荷是否导出（远程时务必谨慎）

[tracing.otlp]
enabled = false
endpoint = "http://127.0.0.1:4318"
headers_env = "OTLP_HEADERS"  # 引用环境变量，不存明文

[tracing.langfuse]
enabled = false
host = "http://localhost:3000"
public_key_env = "LANGFUSE_PUBLIC_KEY"
secret_key_env = "LANGFUSE_SECRET_KEY"
```

## 6. 前端用途

调用检视器与时间线目前基于事件和轨迹各自拼装。引入 span 之后：

- 时间线按 span 树展示，能折叠子智能体；
- 耗时瀑布图直接由 `start_ts` / `end_ts` 绘制；
- 点击一个 `llm` span 可以打开该次调用的完整消息体（来自外置的大块内容）。

## 7. 不变量

1. 追踪是旁路：任何追踪相关的异常不影响任务。
2. 远程导出默认关闭，内容导出默认关闭。
3. 研究隔离区的页面正文不出现在任何 span 中。
4. span 的父子关系完整：每个非根 span 都有存在的父。
5. 挂起等待审批的时间不计入执行耗时。
6. 核心不依赖任何导出器的第三方包。

## 8. 测试要点

- span 树：并发工具调用与并发子智能体的父子关系正确；异步任务边界之间上下文继承。
- 续跑：`trace_id` 不变，出现 `resume` span；挂起时间归入 `guard:approval`。
- 导出：队列满丢弃并计数；导出器抛异常不影响任务；未安装可选依赖时不可用但不报错。
- 隐私：默认配置下没有任何数据离开本机；`export_content=false` 时属性里没有提示词与入参；脱敏生效。
- 关联：轨迹、事件、日志里的 `span_id` 能互相对上。
- 迁移：移除 `langfuse` 默认依赖后，核心功能与测试不受影响。

## 9. 待定

- 是否直接采用 OpenTelemetry SDK 作为内部 span 实现，而不是自己写一套。优点是生态和语义约定现成；缺点是核心引入较重的依赖。倾向自有轻量模型加 OTLP 导出器，两者映射简单。
- `llm` span 是否按 OpenTelemetry 的生成式 AI 语义约定命名属性。该约定仍在演进，先对齐主要字段（模型、Token 用量），其余保持自有。
- 采样：目前每个任务全量记录。任务量大时是否需要采样。单用户本机场景不需要。
