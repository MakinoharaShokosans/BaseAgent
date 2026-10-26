# 工具系统

## 1. 要解决的问题

模型只会输出"调用某个工具，参数是这些"。运行时必须保证：

1. 无论工具怎样失败，主循环都拿到一个**结构化、可判定**的结果，而不是异常或一段需要猜的自由文本。
2. 来自不可信来源的工具，**从构造层面**就进不了特权工具表，而不是靠提示词约定。
3. 一批工具调用可以并发执行，互不拖累，并且返回顺序与调用顺序一致，便于配对。
4. 新增一个工具只加一个文件，不触碰框架代码。

## 2. 分层

```
tools/
├── core/      框架机制：协议、Schema 导出、注册表、派发器、sidecar HTTP 客户端
└── builtin/   具体工具实现 + 装配入口 build_builtin_tools
```

`core` 不依赖任何具体工具；`builtin` 只依赖 `core`。改派发策略不会碰到工具，加工具不会碰到框架。

## 3. 工具协议

所有工具（内置、MCP 转译、子智能体包装）继承同一个抽象基类。

```python
class AegisTool(ABC):
    name: str                      # 全局唯一，注入模型 function calling 的 name
    description: str               # 给模型看的能力描述，直接影响调用准确率
    parameters: dict               # JSON Schema
    timeout_sec: float = 60.0      # 单次调用硬超时
    trust: Literal["trusted", "untrusted"] = "trusted"
    idempotent: bool = False       # 新增，见 §3.2

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult: ...
```

### 3.1 失败约定

- **业务失败不抛异常**，返回 `ToolResult.failure(message)`。文件不存在、命令退出码非零、参数缺失都属于业务失败，它们的价值是让模型自我修正。
- 只有**框架级错误**才抛 `AgentError` 的子类（例如装配错误）。
- 派发器兜底：即使工具实现者漏接了异常，也会被降级为失败观察值，不会中断主循环。

### 3.2 幂等属性（新增）

`idempotent: bool` 声明"重复执行是否安全且结果等价"。默认 `False`，需要工具作者显式声明为 `True`。

用途：[检查点](../02_runtime/03_checkpoint_and_resume.md) 的崩溃恢复。若任务在 `tool_runner` 派发期间中断，恢复时对 `idempotent` 工具可直接重试，其余工具改为合成"执行结果未知"的观察值，交给模型核验。

| 工具 | idempotent | 说明 |
|---|---|---|
| `view_file` | 是 | 纯读取 |
| `load_skill` | 是 | 纯读取 |
| `write_file` | 否 | 虽然覆盖写入重复执行结果相同，但模型可能依赖"写之前文件的状态"来决策，保守处理 |
| `bash` | 否 | 副作用不可静态推理 |
| MCP 转译工具 | 否 | 同上 |
| 子智能体类工具 | 否 | 有 Token 消耗和副作用 |

> 待定：`write_file` 是否应标为幂等。覆盖写入的"同一内容写两次"确实等价，但目前保守处理，等实现时依据实际恢复体验再定。

## 4. 结果契约

```python
class ToolResult(BaseModel):
    ok: bool
    content: str = ""              # 供模型阅读的观察值（已蒸馏）
    exit_code: int | None = None   # 进程类工具的退出码
    artifact_id: str | None = None
    artifact_path: str | None = None
    is_truncated: bool = False
    duration_ms: int = 0
    error: str | None = None       # 失败原因摘要
    meta: dict = {}                # 工具特有的结构化附加信息
    trust: Literal["trusted", "untrusted"] | None = None  # 调用期信任级覆盖
```

编排层只读这些字段做判定，不解析 `content` 的自由文本：

| 判定 | 依据 |
|---|---|
| 是否计为失败（连续错误计数） | 先看 `ok is False`；再看 `exit_code` 非零；两者都没有时才回退到关键字启发式 |
| 是否有离线产物 | `artifact_path` |
| 是否被截断 | `is_truncated` |
| 观察值是否要加不可信信封 | 生效的信任级，见 §6 |

## 5. 注册表

注册表只做"名字到实例"的映射与查询，不含执行逻辑，也不感知状态机。

**构造期校验**（想犯错也犯不了）：

| 情形 | 行为 |
|---|---|
| 工具名为空 | 拒绝 |
| 工具名重复 | 拒绝，不静默覆盖（重名通常意味着装配逻辑有 bug） |
| 注册 `untrusted` 工具，但注册表 `allow_untrusted=False`（默认） | 拒绝 |
| `allow_untrusted=True` 且给了逐名授权清单，工具名不在清单里 | 拒绝 |
| `allow_untrusted=True` 且清单为 `None` | 放行，用于隔离区 |

由此得到两类注册表：

| 注册表 | `allow_untrusted` | 用途 |
|---|---|---|
| 主工具表 | 仅在有 MCP 工具时为真，且带逐名授权清单 | 主 Agent 的特权工具表 |
| 隔离区工具表 | 真，清单为 `None` | 研究子智能体，唯一能装 `web_search` 的地方 |

主工具表即使被误加入 `web_search`，也会在构造期抛 `ToolExecutionError`。"主 Agent 不接触原始外部内容"因此是代码强制的不变量。

另提供：`to_openai_tools()`（导出 function 定义）和 `get_capabilities_summary()`（给 planner 的能力清单，MCP 工具会带标记）。

## 6. 信任级的解析

每次调用的"生效信任级"按以下优先级解析：

1. **调用期覆盖**：`ToolResult.trust` 非空时采用。
2. **工具类静态声明**：`tool.trust`。
3. **最保守值** `untrusted`：工具不存在时。

允许调用期覆盖，是因为有些工具的输出信任级取决于本次入参。典型是动态子智能体：它被授予了哪些工具，决定了它的输出能不能被当作可信。静态类属性表达不了这种依赖。

派发器把解析结果放进 `DispatchedResult.trust`，编排层只读这一个字段，不自行重复解析。不可信的观察值由编排层统一加信封，不依赖各个工具自觉，协议细节见 [05_security](../05_security/)。

## 7. 派发器

```python
class DispatchedResult:
    tool_call_id: str
    tool_name: str
    result: ToolResult
    trust: Literal["trusted", "untrusted"]

async def dispatch(calls: Sequence[tuple[str, str, Mapping]]) -> list[DispatchedResult]
```

- 同一批调用用 `asyncio.gather` 并发，总耗时约等于最慢的一个，而不是求和。
- **返回顺序与输入一致**，便于与 `tool_calls` 配对生成 `tool` 消息。
- **每个调用独立硬超时**（`asyncio.wait_for(..., tool.timeout_sec)`），一个卡死不影响其他。
- 超时、未预期异常都降级为失败观察值，附 `duration_ms`。
- 未知工具名返回失败观察值，信任级取 `untrusted`。副作用不可静态推理，所以保守处理。

派发器不做权限判定、不做预算记账、不做观察值裁剪。这些属于 `tool_runner`，见 [主循环 §5](../02_runtime/02_main_loop.md)。

## 8. Schema 导出

工具转 OpenAI 兼容 function 定义：

```json
{ "type": "function",
  "function": { "name": "...", "description": "...", "parameters": { ... } } }
```

没有 `name` 直接抛 `ValueError`，早失败优于生成非法 Schema。内置工具与 MCP 转译工具共用同一套转换，避免两边格式分叉。

## 9. Sidecar HTTP 客户端

调用 `bash_shell` 和 `web_search` 的统一客户端：

- 一个 `httpx.AsyncClient`，延迟创建、共享连接池；一轮内并发调用多个 sidecar 不会重复握手。
- 所有失败统一映射为 `DependencyUnavailableError`：网络异常、超时、非预期状态码、返回体不是合法 JSON。调用方只需处理这一种异常。
- 工具层把它转成 `ToolResult.failure`，所以 sidecar 挂掉时模型看到的是一条可读的失败观察值。
- 任务结束时由装配层逐个 `aclose()`。

> 去掉 RAG 后，客户端只剩 shell 与 web 两个目标；`services.rag_url` 配置和对应健康探测随之删除。

## 10. 内置工具装配

`build_builtin_tools(...)` 在任务启动时按需装配**可信**工具：

| 工具 | 条件 |
|---|---|
| `bash` | 传入了 shell 客户端 |
| `view_file`、`write_file` | 始终 |
| `load_skill` | 传入了技能注册表 |

原则：缺少某个依赖时，**对应工具不注册**，模型根本看不到它，也就不会误调用一个不存在的能力。

装配函数**刻意不含 `web_search`**。外部信息的唯一入口是研究子智能体，见 [06_subagents](../06_subagents/)。

### 10.1 工具集在会话内保持稳定

工具 Schema 是请求的 `tools` 字段，多数模型服务把它排在提示词最前面，属于前缀缓存的一部分。工具集一变，其后整段缓存全部失效。所以：

- 主 Agent 的工具集**在一个会话内应当固定**。装配发生在任务启动时，不要在任务中途增减工具。
- MCP 工具的出现与消失（服务器连接失败、恢复后重试、`listChanged`）会改变工具集。变化只在**任务边界**生效，并把它视为一个新的缓存起点，不要频繁切换。
- 子智能体按任务授予不同工具，它们有**独立的上下文与请求**，工具集不同不影响主 Agent 的缓存。
- 工具的**顺序**也是前缀的一部分：注册表导出 Schema 时按名称排序，保证确定。
- 工具描述文本不得包含随任务变化的内容（路径、时间、计数）。

对应的测试：同一会话内两次导出的 `tools` 序列化逐字节相同；MCP 工具集变化只在下一个任务生效。

## 11. 文件工具的路径安全

`view_file` 与 `write_file` 共用 `_resolve_within_root`：先把路径 `resolve()` 成绝对路径，再要求它等于工作区根或位于其子树内，否则抛 `PermissionError` 并以失败观察值返回。`resolve()` 会展开符号链接与 `..`，所以符号链接逃逸与 `../../` 都会被拦住。

- `view_file`：单次最多返回 400 行，带行号，超出时提示递增 `start_line` 继续读；以 `errors="replace"` 解码，二进制文件不会让工具崩溃。
- `write_file`：单次写入上限 1,000,000 字节。

这是路径沙箱的**第一道**；命令执行的沙箱由 `bash_shell` 独立承担，见 [05_security](../05_security/)。

## 12. 不变量

1. 任何工具调用都产生且只产生一个 `ToolResult`，不向主循环抛异常。
2. 特权工具表中不存在 `untrusted` 工具，除非逐名授权。
3. 派发结果的顺序与调用顺序一致，数量相等。
4. 每个调用都有硬超时。
5. 工具实现不做权限判定。越权拦截只发生在 `tool_runner`，避免权限逻辑散落。

## 13. 测试要点

- 注册表：空名、重名、不可信工具在默认注册表被拒、清单外的不可信工具被拒、清单内放行。
- 派发器：并发耗时接近最大值而非总和；顺序一致；超时降级；工具抛异常降级；未知工具降级且信任级为 `untrusted`；调用期信任覆盖优先于静态声明。
- 文件工具：`..` 逃逸、绝对路径逃逸、符号链接逃逸均被拒；400 行截断提示；超大写入被拒。
- 客户端：连接失败、超时、非预期状态码、非 JSON 响应，均映射为 `DependencyUnavailableError`。
- 装配：缺少 shell 客户端时没有 `bash`；缺少技能注册表时没有 `load_skill`；主工具表加入 `web_search` 抛错。

## 13.1 实现状态

协议（`AegisTool`、`ToolResult`）在 `agent_runtime.core`，注册表与派发器在 `tools.core`，行为与本文一致（注册表构造期校验、按名排序导出、并发派发且顺序一致、独立硬超时、失败降级、信任级解析），见 [executor 与 tool_runner §3](../02_runtime/06_executor_tool_runner.md)。`ToolResult.failed` 是连续错误计数的依据（`ok` 为假或退出码非零；没有再回退到关键字启发式，因为 `ok` 总是有值）。内置工具与 sidecar 客户端已实现，见 [内置工具 §11](02_builtin_tools.md)：`build_builtin_tools` 缺依赖则不注册（无 shell 绑定就没有 `bash`，无技能目录就没有 `load_skill`），`read_artifact` 与两个文件工具始终在；同配置两次装配导出的 Schema 逐字节相同。`SidecarClient` 在 `tools.core`：延迟创建并共享连接池，`aclose()` 可重复调用且关闭后可再用，网络错误、超时、非预期状态码、非 JSON 对象一律是 `DependencyUnavailableError`；它还允许调用方声明额外接受的状态码（`bash` 用它接收 403 与 422），这样策略拒绝的原因不会丢。
**未做**：参数 Schema 校验、`edit_file`、`list_dir`、`grep`。

## 14. 待定

- `idempotent` 对 `write_file` 的取值，见 §3.2。
- 工具参数是否在派发前按 JSON Schema 校验。目前依赖各工具自行容错（如 `bash` 对 `timeout_sec` 的转换），统一校验能让错误更早、更一致，但会增加一层依赖。
- MCP 工具的 `idempotent` 是否允许由配置显式声明，而不是一律保守。
