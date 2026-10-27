# 网关：分档、降级与重试

## 1. 要解决的问题

模型调用是整个系统里最不可靠的一环：会超时、限流、返回 5xx、返回畸形内容，不同服务商的行为还各不相同。如果让每个阶段自己处理：

- 重试与降级逻辑会被复制到五六个地方，各自演化。
- 任何一处换模型、换端点都要改一片代码。
- 观测、安全检查无处统一挂载。

所以**所有模型调用只有一个入口**：`LLMGateway`。调用方只说"我要哪一档模型、给这些消息和工具"，不关心有几个端点、密钥在哪、失败怎么处理。

去掉 LangChain 之后，网关是唯一和模型 SDK 接触的地方，自定义的 `Message` 在这里转换成线上协议格式，响应在这里转换回来。

## 2. 两档模型

| 档 | 用途 | 默认温度 |
|---|---|---|
| `reasoning` | 规划、复核、验收；需要判断和推理的环节 | 0.0 |
| `fast` | 工具调度、记忆压缩、子智能体；需要便宜和快的环节 | 0.2 |

分档是策略问题，不是模型名问题。阶段代码只写档位，具体用哪个模型由配置决定。这一原则在新版里要贯彻到底，包括：

- 记忆压缩**必须**走网关的 `fast` 档，而不是自己取第一个端点直接调用（现状绕过了网关，见 [窗口与压缩 §2](../03_context/02_window_and_compaction.md)）。
- 子智能体通过各自配置的 `model_tier` 取档，不持有端点。
- 阶段代码里不得出现具体模型名。现状里规划和执行阶段为了事件展示硬编码了 `"gpt-5.6-terra"`、`"fast-model"` 之类的回退字符串，应当改为由网关返回实际命中的端点。

## 3. 接口

```python
@dataclass(frozen=True)
class LLMRequest:
    tier: Literal["reasoning", "fast"]
    messages: Sequence[Message]
    tools: Sequence[ToolSchema] | None = None
    response_schema: type[BaseModel] | None = None   # 结构化输出，见 02
    max_output_tokens: int | None = None
    reasoning_effort: Literal["low", "medium", "high", "max"] | None = None   # 见 03，各家映射见 04
    canary: str | None = None                        # 金丝雀 token，见 §12.1
    purpose: str = ""                                # 观测标签：main / compactor / research …

@dataclass
class LLMResponse:
    message: Message                  # role=assistant，含 content / tool_calls / reasoning（见 03）
    parsed: BaseModel | None          # 结构化输出校验后的对象
    usage: Usage                      # prompt / completion / reasoning / total / 缓存命中与未命中，及是否为估算
    endpoint: str                     # 实际命中的端点名
    model: str                        # 实际命中的模型
    finish_reason: str
    attempts: int                     # 含重试与降级的总尝试次数
    duration_ms: int

async def invoke(self, request: LLMRequest) -> LLMResponse
```

和现状相比：返回完整的助手消息而不是零散字段；用量是结构化的并带"是否估算"标记；请求里显式带 `purpose` 与 `canary`。

## 4. 端点

### 4.1 配置

每档有一个**有序**的端点列表，顺序即降级顺序。端点除了地址和密钥，还要声明**能力**，因为"OpenAI 兼容"只是线路格式兼容，能力并不兼容：

```toml
[[models.reasoning.endpoints]]
name            = "primary"
base_url        = "https://api.example.com/v1"
model           = "model-a"
api_key_env     = "REASONING_KEY"
timeout_sec     = 180
context_window  = 128000          # 任务内上下文折叠的依据，见 03_context
max_output_tokens = 8192
temperature     = 0.0             # 缺省继承档位；设为 false 表示不发送该参数
reasoning_effort = "medium"       # 缺省不发送

[models.reasoning.endpoints.capabilities]
tools             = true
parallel_tools    = true
json_object       = true
json_schema       = false
usage_in_response = true
reasoning_field   = "reasoning_content"   # 见 03；空表示该模型不返回思考内容
reasoning_passback = "never"              # never | tool_turns | opaque | all_when_tools
thinking_control   = "none"               # none | deepseek，见 04
thinking_with_tools = true                # 为否时，带 tools 的请求显式关闭思考
```

能力字段的作用：

| 能力 | 用途 |
|---|---|
| `tools`、`parallel_tools` | 不支持时不发送 `tools`，或不发送并行调用开关 |
| `json_object`、`json_schema` | 决定结构化输出走哪条路径，见 [结构化输出](02_structured_output.md) |
| `usage_in_response` | 为否时用量由本地估算，见 §8 |
| `thinking_control` | 决定怎样发送思考开关。`deepseek` 时总是在 `extra_body` 显式发送 `thinking`，因为该服务默认开启思考，不发送不等于关闭，见 [DeepSeek](04_deepseek.md) |
| `thinking_with_tools` | 为否时，请求带 `tools` 就显式关闭思考，用于规避"带工具必须回传全部历史思考内容"的服务商，见 [DeepSeek §4](04_deepseek.md) |
| `merge_consecutive_user` | 为真时请求构造把相邻的 `user` 消息合并成一条（段间用标题分隔，字节序稳定），用于要求角色严格交替的端点，见 [提示词装配 §2.1](../03_context/03_prompt_assembly.md) |
| `reasoning_*` | 见 [思考内容](03_reasoning_content.md) |
| `temperature = false` | 有些推理模型拒绝采样温度参数，发了会 400 |

### 4.2 解析与密钥

- 配置里只写环境变量名，密钥来自 `.env`，`config.toml` 入版本库。
- 本地端点（`localhost`、`127.0.0.1`）密钥缺失时回填 `EMPTY`；**云端端点密钥缺失则跳过该端点并告警**，因为带着空密钥请求只会 401，白白消耗一次重试与超时。
- 一个档位解析后没有任何可用端点，启动时就应当报清楚的错误，而不是等到第一次调用。
- 端点池在进程启动时一次解析，轮换密钥需要重启。提供 `reload()` 可以不重启。

## 5. 重试与降级

两级策略：**端点内重试**，再**跨端点降级**。

### 5.1 哪些错误重试

| 错误 | 处理 |
|---|---|
| 连接失败、超时、429、5xx | 端点内指数退避重试，次数用尽后降级到下一个端点 |
| 400（请求本身有问题，例如超出上下文长度、参数不被接受） | **不重试**。降级到下一个端点也往往无济于事，但上下文超长这类错误在下一个端点（窗口更大）可能成功，所以仍尝试下一个，同时记录错误类别 |
| 401、403（鉴权） | 不重试，降级，并标记该端点"鉴权失败"，避免后续每次调用都先撞一次 |
| 响应体畸形（无 `choices`、空内容且无工具调用） | 视为瞬时故障，重试一次 |

默认：每个端点最多 3 次尝试，退避从 2 秒起指数增长。

### 5.2 退避

- **加抖动**：在计算出的等待时间上加随机比例，避免多个并发调用同时重试造成的惊群。
- **设上限**：单次等待不超过 30 秒。现状只有指数增长，没有上限。
- **尊重 `Retry-After`**：服务商返回了就用它，取它和计算值中较大者。

### 5.3 降级

第一个端点耗尽后透明切换到下一个，**保持相同的 `messages` 与 `tools`**，保证语义一致。例外是思考内容的透传状态，因为那和具体模型绑定，切换时必须处理，见 [思考内容 §6](03_reasoning_content.md)。

全部端点耗尽才抛 `LLMUnavailableError`，携带每个端点的失败摘要，而不只是最后一个。

### 5.4 熔断器（新增）

现状没有跨调用的记忆：主端点宕机期间，**每一次**调用都要先在主端点上完整走一遍"超时加三次退避重试"，才降级到备用端点。一个 180 秒超时的端点，单次调用就可能白等十分钟以上。

新增每端点的熔断状态：

```
closed ──连续 N 次耗尽重试──► open（在 T 秒内直接跳过该端点）
   ▲                              │ T 秒后
   └──── 探测成功 ◄──── half-open（放行一次探测请求）
```

默认 N = 2，T = 60 秒。open 状态下仍记录被跳过的事件。**如果所有端点都处于 open，仍然尝试最近开启的那个**，不能因为熔断而完全拒绝服务。

## 6. 超时

一次调用 = 端点数 × 重试次数 × 单次超时 + 退避，没有上界的话，网络挂起会让任务无限等待。这是**防挂起**，不是额度控制：

- 每个档位配置 `call_timeout_sec`（一次调用从开始到放弃的总耗时上限，含重试、降级与退避）。
- 网关在**每次尝试之前**检查剩余时间；单次尝试的超时取 `min(端点超时, 剩余时间)`；剩余不足一次最小尝试时间就直接放弃，抛 `LLMCallTimeout`，而不是开始一次注定被打断的请求。
- `LLMCallTimeout` 与"所有端点不可用"区分开，但任务层处理相同：按 `failed` 停止，可续跑。

没有任务级的挂钟或 Token 截止时间：网关不接收来自任务的截止时间参数。

## 7. 并发与限流

规划、执行、子智能体、研究、压缩都会调用网关，子智能体和研究还可能并发。

- 每个端点一个信号量（默认并发 4，可配置），防止自己把自己打成 429。
- 收到 429 时，除了退避，把该端点的有效并发临时减半，恢复期内逐步放回。
- 同一任务内的并发由同批工具与子智能体的数量间接决定，网关层只负责端点保护。

## 8. 用量

用量统计以**服务商返回的数**为准，用于观测、成本估算与上下文规模校准，**不做额度控制**。

| 情形 | 处理 |
|---|---|
| 响应带 `usage` | 取 `prompt_tokens`、`completion_tokens`、`total_tokens`，及推理 Token 明细（若有） |
| 响应不带 `usage`（部分本地与兼容服务） | **用本地分词估算**，并在 `Usage.estimated = True` 标记 |

现状在不带 `usage` 时直接记为 0，统计会严重失真。必须修复：估算值偏差再大，也比 0 好；并且应当保守，估算时取高。

用量里同时保留缓存字段 `cache_hit_tokens` 与 `cache_miss_tokens`（服务商提供时，如 DeepSeek 的 `prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`），用于观测前缀缓存是否真的生效、分开估算成本，以及验证 [装配规则](../03_context/03_prompt_assembly.md) 的稳定性。

用量里单独保留 `reasoning_tokens`（含在 `completion_tokens` 里，仅用于观测，不重复计费）。

## 9. 工具调用的规范化

线上返回的工具调用形态不统一，网关负责收敛为 `ToolCall(id, name, args)`：

| 问题 | 处理 |
|---|---|
| `arguments` 是 JSON 字符串、字典或空 | 统一解析为字典 |
| `arguments` 不是合法 JSON | 保留原文到 `args["_raw"]` 并**标记该调用格式错误** |
| 缺失 `id` 或 `id` 重复（部分本地模型） | 生成唯一 id（`call_<序号>`），保证与后续 `tool` 消息一对一配对 |
| 工具名不在本次提供的 Schema 里 | 保留，交给派发器按"未知工具"处理 |
| `finish_reason == "length"` 且存在工具调用 | **参数很可能被截断**。不执行，作为格式错误回给模型，并提示缩短输出 |

格式错误的调用不应被静默当作普通参数执行。工具执行对它生成失败观察值，告诉模型"参数不是合法 JSON，请重新发起调用"，计入连续错误。

## 10. 请求构造

`Message` 到线上格式的转换集中在网关：

- 带 `tool_calls` 的助手消息，内容为空时发送 `null` 而不是空字符串，兼容较严格的服务端。
- `tool` 消息带 `tool_call_id`。
- 系统消息只放在最前面一条。
- 显式设置 `max_tokens`（取请求值或端点配置），不依赖服务商默认值，默认值可能很小，导致输出被截断。
- 不发送端点不支持的参数（`temperature`、`parallel_tool_calls`、`reasoning_effort`、`response_format` 等），由能力声明控制。

## 11. 客户端生命周期

现状在**每次尝试**里新建一个 `AsyncOpenAI` 客户端，从不关闭，等同于每次调用新建一个 HTTP 连接池，既浪费握手，又会泄漏连接。

新版：每个端点持有一个长生命周期的客户端，随网关创建，进程退出时统一关闭。

## 12. 观测

网关是观测的天然落点。每次调用（含每次重试与降级）产生一条记录：

```
purpose, tier, endpoint, model, attempt, outcome(ok|retry|fallback|error),
usage, duration_ms, finish_reason, error_class
```

- 写入轨迹，并通过事件总线发出 `llm.call` 事件。
- **事件由网关发出，阶段代码不再各自复制一份**。现状规划、执行、评估三个阶段各有一段几乎相同的 60 行事件代码，包括失败路径。
- 事件里的完整消息体用于前端的调用检视器。它包含系统提示词（含金丝雀）与全部观察值，仅在本机回环传输，但需要在 [08_observability](../08_observability/) 里写明保留策略。

## 12.1 金丝雀检测

（**实现位置：目前检测在循环的模型接缝 `loop/model.py`，网关与 `LLMRequest.canary` 尚未做，行为一致。**）网关在返回响应之前扫描模型的原始输出：正文、全部工具调用的参数（递归扫字符串、字典键与值、列表）、明文思考内容。命中就抛 `CanaryLeak`，**该条响应不返回给调用方**，因此其中的工具调用不会被执行、不会进入 transcript。金丝雀 token 由调用方在请求上带入（`LLMRequest.canary`），为空永不命中。检测函数是 `guardrails.detect_canary_leak`。

## 13. 错误

| 异常 | 含义 | 任务层处理 |
|---|---|---|
| `LLMUnavailableError` | 所有端点耗尽 | 任务终止，状态 `failed`，原因"LLM 不可用" |
| `LLMCallTimeout` | 一次调用超过总耗时上限 | 任务 `failed`，可续跑 |
| `CanaryLeak` | 模型输出含金丝雀 | 任务 `terminated`（安全终止），不可续跑，见下 |
| `LLMBadRequest` | 请求本身被拒（如上下文超长）且降级也无效 | 触发上下文折叠后重试一次；仍失败则终止 |
| `LLMInvalidResponse` | 响应无法解析 | 已内部重试过，仍失败则作为不可用处理 |

`LLMBadRequest` 里的"上下文超长"是个重要的联动点：它是触发任务内压缩的**被动信号**，补足 [窗口与压缩 §3](../03_context/02_window_and_compaction.md) 中基于估算的主动触发。

## 14. 不变量

1. 全工程只有网关接触模型 SDK。
2. 阶段代码不出现具体模型名与端点。
3. 降级时 `messages` 与 `tools` 不变（思考内容的透传状态除外）。
4. 任何一次调用的总耗时不超过调用方给定的截止时间。
5. 每次调用都有用量记录，不带 `usage` 时用估算且带标记。
6. 工具调用的 id 非空且唯一。
7. 密钥不进入日志与事件。

## 15. 测试要点

- 重试：429、5xx、超时各自按预期重试；400 与鉴权错误不重试；退避带抖动且有上限；`Retry-After` 被采纳。
- 降级：主端点失败切到备用；全部失败抛异常，异常里含所有端点的摘要；降级后请求内容一致。
- 熔断：连续失败后进入 open，期间直接跳过；半开探测成功后恢复；全部 open 时仍尝试一个。
- 截止时间：剩余时间不足时不发起新尝试；单次超时被截短。
- 用量：缺 `usage` 时估算并标记；推理 Token 明细解析。
- 工具调用：非法 JSON、缺失 id、重复 id、`length` 截断。
- 并发：信号量限制端点并发；429 后并发降低。
- 请求构造：不支持的参数不被发送；`temperature=false`。
- 安全：错误信息与日志不含密钥。

## 16.1 实现细节（已落地于 `agent_runtime.llm`）

已实现：端点池、能力过滤、请求构造与响应解析、重试与退避、降级、熔断、截止时间、单端点并发保护、用量与估算、工具调用规范化、每次尝试的观测记录、思考内容回传的 `never` / `tool_turns` / `all_when_tools`。结构化输出见 [02 §13](02_structured_output.md)。**未实现**：`opaque` 回传（构造网关时直接抛 `NotImplementedError`，因为载荷在线上的放置位置因服务商而异）、`reload()` 重新解析密钥（目前只清屏蔽与熔断状态）。

- **接口**：`LLMGateway.invoke(LLMRequest) -> LLMResponse`；用 `LLMGateway.from_loaded(loaded_settings)` 装配，只使用缺密钥的云端端点已被剔除后的可用端点。时钟、睡眠、随机数、客户端工厂都可注入，所以全部行为可用 `httpx2.MockTransport` 加假时钟离线、确定地测试。
- **SDK**：`max_retries=0`，重试与降级只由网关掌控；`openai` 3.x 底层是 `httpx2`（见 [技术选型 §10](../01_architecture/05_tech_stack.md)），网关只捕获 SDK 的 `APIStatusError` / `APIConnectionError`。每个端点一个长生命周期客户端，`aclose()` 统一关闭。
- **失败分类**：429、408/425、5xx、连接失败、超时 → 瞬时，端点内退避重试；401/402/403 → 鉴权/额度，不重试、降级并**屏蔽该端点**（402 是 DeepSeek 的余额不足，SDK 没有专门子类）；其余 4xx → 请求本身有问题，不重试但仍尝试下一个端点；畸形响应 → 只重试一次。
- **退避**：第 n 次失败后等 `factor^n × (1 + jitter × 随机)`，封顶 `backoff_max_sec`；`Retry-After`（只解析秒数形式）取较大者，**不封顶**；若它超过封顶值，认为等不到，**直接放弃该端点**而不是干等。
- **熔断**：只统计瞬时故障的耗尽；请求本身有问题、鉴权失败不算端点不健康。打开后到点放行**一次**探测，探测迟迟没有结果（被取消）时过一个窗口再放一次。全部打开时仍尝试最近打开的那个。熔断状态是“按需”判定的：只在真要尝试该端点时才消耗探测名额。
- **总耗时上限**：旧实现的 `deadline`（`clock() + guard.remaining_wall_sec()`）随预算守卫一起取消，改为档位配置的 `call_timeout_sec`；每次尝试前若剩余时间小于 `min_attempt_sec` 抛 `LLMCallTimeout`；单次超时取 `min(端点超时, 剩余)`；退避之后若剩余不足一次最小尝试，**不睡**，直接抛出。
- **并发**：每端点一个 `AdaptiveLimiter`；429 减半（至少 1），连续成功“当前上限”次后放回一格。
- **用量**：服务商给了就用（含缓存命中/未命中、推理 Token 明细）；没给、或端点声明不返回，就**本地估算并标记 `estimated`**，乘以 `estimate_safety_factor`；`estimate_when_missing = false` 时记 0。估算不依赖分词词表：中日韩字符一字一 token，其余每 3 字符一 token（偏高）；等 tiktoken 离线缓存验证后可替换。
- **工具调用**：参数解析为字典；不是合法 JSON 对象的保留原文于 `args["_raw"]`，并登记在 `LLMResponse.malformed_calls`（id → 原因），**不得被当作普通参数执行**；缺失、为空、重复的 id 生成 `call_<序号>`；`finish_reason == "length"` 时所有调用都标为可能被截断。再次发给模型时，格式错误的调用把原文原样送回。
- **思考**：`thinking_control = deepseek` 时总是显式发送 `thinking`；`thinking_with_tools = false` 且带工具时发送 `disabled`，此时也不发 `reasoning_effort`。强度映射 `low→low`、`medium/high→high`、`max→max`，请求值优先于端点默认值。
- **脱敏**：错误摘要与观测记录里的服务端错误文本经过脱敏（`sk-…`、`Bearer …`，以及 `from_loaded` 里已知的密钥原文）。观测记录不含消息正文。
- **对外语义**：`LLMUnavailableError`（带每个端点的失败摘要）→ 任务 `failed`；`LLMCallTimeout` → 任务 `failed`；`LLMBadRequest.context_overflow` → 上下文超长的被动信号。三者互不为子类。

## 16. 已发现的问题汇总

| # | 问题 | 级别 |
|---|---|---|
| 1 | 响应不带 `usage` 时记为 0，用量统计对这类端点失真 | 高 |
| 2 | 一次调用的总耗时无上界，网络挂起会让任务无限等待 | 高 |
| 3 | 每次尝试新建客户端且不关闭，连接泄漏 | 中 |
| 4 | 无熔断，主端点宕机时每次调用都付出完整重试代价 | 中 |
| 5 | 记忆压缩绕过网关，无降级无重试 | 中 |
| 6 | 工具参数非法 JSON 时静默当作普通参数；`length` 截断未处理 | 中 |
| 7 | 退避无抖动无上限，不读 `Retry-After` | 低 |
| 8 | 温度等参数无法按端点关闭，推理模型可能因此 400 | 低 |
| 9 | 三个阶段复制粘贴了事件发射与失败处理代码，并硬编码模型名 | 低 |
