# 接入 DeepSeek

本项目选用 DeepSeek 作为模型服务。本文把它的接口特性映射到网关的设计上：怎么配置两档模型、思考模式怎么处理、缓存怎么利用、哪些地方需要特别注意。

> **来源与时效。** 下面的事实来自 DeepSeek 官方 API 文档（`api-docs.deepseek.com`）在撰写时的页面内容，经抓取摘要后整理，**不是来自实际调用**。模型名、版本、参数语义都可能变化。因此：模型名与参数只出现在配置里，不进代码；每一条标"待验证"的结论，上线前必须用真实密钥跑一遍 §9 的验证清单。

## 1. 文档里与本项目相关的事实

| 项 | 文档所述 |
|---|---|
| 协议 | OpenAI 兼容，基址 `https://api.deepseek.com`；也提供 Anthropic 格式入口（本项目不用） |
| 模型 | 撰写时列出 `deepseek-flash` 与 `deepseek-v4-pro`；旧名称 `deepseek-v4-flash` 等已退役但仍被接受（映射到 flash）。文档页面**没有**提到 `deepseek-chat`、`deepseek-reasoner`，不要沿用旧名称 |
| 上下文与输出 | 表中为 1M 上下文、最长 384K 输出；是否对两个模型都成立，页面没有写清 |
| 能力 | JSON 输出、工具调用、思考与非思考两种模式，均支持；`deepseek-flash` 支持视觉，`deepseek-v4-pro` 不支持 |
| 思考模式 | **默认开启**，默认强度 `high` |
| 开关 | 请求体字段 `thinking: {"type": "enabled" \| "disabled"}`；通过 OpenAI SDK 时放在 `extra_body` 里 |
| 强度 | `reasoning_effort`：`low` / `high` / `max`；`minimal`、`low` 映射为 `low`，`medium`、`high`、`xhigh` 映射为 `high`，`max`、`ultra` 映射为 `max` |
| 思考内容字段 | 响应消息上的 `reasoning_content`，与 `content` 同级 |
| 思考模式下的参数 | `temperature`、`presence_penalty`、`frequency_penalty` **不报错但无效** |
| 回传规则 | 见 §4，**这是本文最重要的一条** |
| JSON 模式 | 只有 `response_format: {"type": "json_object"}`；提示词里**必须出现 "json" 字样**并最好带示例；偶尔返回空内容；`max_tokens` 要足够大否则 JSON 被截断；**文档未提 `json_schema`** |
| 缓存 | 默认开启、全自动；用量里给出 `prompt_cache_hit_tokens` 与 `prompt_cache_miss_tokens`，见 §6 |

## 2. 两档模型的配置

分档是策略问题，具体用哪个模型、开不开思考，都在配置里：

```toml
[models.reasoning]
temperature = 0.0                 # 思考模式下无效，保留无妨

[[models.reasoning.endpoints]]
name              = "deepseek-pro"
base_url          = "https://api.deepseek.com"
model             = "deepseek-v4-pro"      # 以官方文档当前列出的名称为准
api_key_env       = "DEEPSEEK_API_KEY"
timeout_sec       = 180
context_window    = 1000000
max_output_tokens = 16384
reasoning_effort  = "high"

[models.reasoning.endpoints.capabilities]
tools              = true
parallel_tools     = true               # 待验证
json_object        = true
json_schema        = false              # 文档未提及，按不支持处理
usage_in_response  = true
thinking_control   = "deepseek"         # 见 §3
reasoning_field    = "reasoning_content"
reasoning_passback = "never"            # 规划与评估不带工具，不需要回传，见 §4
thinking_with_tools = false             # 带工具的请求关闭思考，见 §4

[models.fast]
temperature = 0.2

[[models.fast.endpoints]]
name              = "deepseek-flash"
base_url          = "https://api.deepseek.com"
model             = "deepseek-flash"
api_key_env       = "DEEPSEEK_API_KEY"
timeout_sec       = 90
context_window    = 1000000
max_output_tokens = 8192

[models.fast.endpoints.capabilities]
tools               = true
parallel_tools      = true
json_object         = true
json_schema         = false
usage_in_response   = true
thinking_control    = "deepseek"
reasoning_field     = "reasoning_content"
reasoning_passback  = "never"
thinking_with_tools = false
```

要点：

- 两档共用同一个密钥变量，**一个密钥、两个模型**。降级链可以是同档内先 pro 后 flash，也可以跨家，由配置顺序决定。
- 推理档对应规划与评估，用能力更强的模型；快速档对应工具调度、压缩、子智能体，用 flash。
- `max_output_tokens` 要显式设置（文档提到最长 384K，但默认值未知），结构化输出被截断是 JSON 失败的头号原因。
- 密钥只在 `.env`，配置里只写变量名，沿用现有约定。

## 3. 思考模式的控制

网关根据 `thinking_control` 决定怎么发送开关：

| 取值 | 行为 |
|---|---|
| `none`（默认） | 不发送任何思考相关字段 |
| `deepseek` | 在 `extra_body` 中发送 `thinking: {"type": ...}`；`reasoning_effort` 作为标准参数发送 |

请求里的 `reasoning_effort` 映射到 DeepSeek 的三档：`low → low`，`medium → high`，`high → high`，另允许 `max`。因为 DeepSeek **默认开启思考**，不发送字段并不等于关闭，所以网关对该方言**总是显式发送** `thinking` 字段，以免依赖默认值。

## 4. 思考内容与工具调用：回传规则

文档所述：

| 请求 | 要求 |
|---|---|
| **带 `tools`** | 此前**所有轮次**的 `reasoning_content` 必须回传（连无工具调用的轮次也包括），**缺少会返回 400** |
| **不带 `tools`** | 回传是可选的；带了也被忽略，不会拼进上下文，上一轮的思考不会被带入下一轮 |

这与 [思考内容](03_reasoning_content.md) 原先的三种回传策略（`never` / `tool_turns` / `opaque`）都不吻合：DeepSeek 在带工具时要求回传**所有**历史助手消息的思考内容。如果只是默认 `never`，开着思考发工具请求，**每一次都会 400**。

### 4.1 本项目的做法：带工具的请求不开思考

我们的调用天然分成两类：

| 调用 | 是否带工具 | 档位 |
|---|---|---|
| 规划、评估 | 否（强制 JSON） | 推理档 |
| 执行阶段、子智能体 | 是 | 快速档 |
| 记忆压缩、研究提炼 | 否（强制 JSON） | 快速档 |

策略：

- **不带工具的调用**：思考开启（推理档）或按需，`reasoning_content` 不需要回传，被忽略，`never` 即可。规划与评估正是最需要思考的地方。
- **带工具的调用**：`thinking_with_tools = false`，网关在 `tools` 非空时发送 `thinking: {"type": "disabled"}`。没有思考内容，也就没有回传问题。执行阶段只是把规划阶段已经想好的指令落成具体的工具调用，不需要再长时间思考，这样还省 Token 和时延。

这个做法一并解决了几个麻烦：

1. 不会因为缺少 `reasoning_content` 而 400。
2. 会话历史里只有"目标加交付"的问答，没有思考内容，**不会**与"所有历史轮次都要回传"相冲突，因为带工具的请求根本不开思考。
3. 不用把大段思考内容存进检查点、不用管降级时跨家的连续性。

### 4.2 如果将来要在工具调用中开启思考

需要新增回传策略 `all_when_tools`：

- 请求带工具时，**每一条**历史助手消息都回传其 `reasoning_content`，包括会话历史里的交付消息。
- 这要求持久化**每个任务的思考内容**，会话流水里的交付消息也要带上它，这与"思考内容不进入会话记忆"的原则冲突，需要重新评估。
- 缺失时的处理：旧数据没有思考内容，要能退化为关闭思考，而不是 400。

所以首版**不做**，保持 `thinking_with_tools = false`。这是一个明确的取舍，不是遗漏。

### 4.3 对 0003 的影响

0003"思考内容默认不回传，按端点显式开启"的**决定本身不变**，DeepSeek 恰好印证了"默认不回传"之外还需要"带工具时能关闭思考"这个控制。需要补的是：能力声明里增加 `thinking_control` 与 `thinking_with_tools`，回传策略列表预留 `all_when_tools`。见 [思考内容](03_reasoning_content.md) 与 [0003](../12_decisions/0003-reasoning-passback-default.md)。

## 5. 结构化输出

按文档，只有 `json_object` 模式，所以走 [结构化输出](02_structured_output.md) 的**路径 2**：`json_object` 加提示词中附 Schema。

额外要求：

1. **提示词里必须出现 "json" 字样**，并附期望格式的示例。由 Pydantic 模型导出的字段说明里要包含这个词，网关构造请求时检查，缺失则在末尾补一句"请只输出 JSON"。
2. **偶尔返回空内容**，视为瞬时故障重试一次（网关本就有"空响应重试"）。
3. **`max_tokens` 足够大**，否则 JSON 被截断。截断时按既定做法修复重试，不修补残缺 JSON。
4. 如果以后需要更强的约束，可以用"以工具调用承载"的**路径 3**。但那要带 `tools`，会触发 §4 的规则，只能在思考关闭时使用。

## 6. 缓存

DeepSeek 的上下文缓存是**自动**的，文档要点：

- 默认开启，无需改代码。
- 命中的前提是请求的前缀与已缓存的"前缀单元"**完整匹配**；只重叠一部分、后面接不同内容，不算命中。
- 缓存单元在三处持久化：用户输入结尾、模型输出结尾、长内容的固定间隔处。
- **第一次出现的前缀基本不命中**，要等它被持久化后（通常第二次匹配的请求才开始命中）。构建需要几秒。
- 不再使用的缓存几小时到几天内自动清除。
- **尽力而为，不保证命中率。**
- 只缓存输入前缀，输出仍然重新计算。

这与 [提示词装配 §5](../03_context/03_prompt_assembly.md) 的缓存规则一致，而且有两点具体的收获：

1. 缓存点正好落在"用户输入结尾"和"模型输出结尾"，我们的任务内往返是**追加式**的，每一步新增的内容接在上一步的输出之后，天然适合。
2. 用量里有**真实的命中数字**，不需要再估算。

网关的 `Usage` 增加两个字段：`cache_hit_tokens`（来自 `prompt_cache_hit_tokens`）与 `cache_miss_tokens`。用途：

- 观测：事件与轨迹里记录每次调用的命中率，调试装配顺序是否真的稳定。
- 评测：同一任务不同装配顺序下的命中率对比，验证 [装配规则 R1 到 R6](../03_context/03_prompt_assembly.md)。
- 成本：命中部分与未命中部分的计价不同，成本估算需要分开。
- 回归：固定前缀逐字节稳定的测试，加一条"第二次相同前缀的请求命中率明显升高"的集成检查。

## 7. 上下文窗口与成本

DeepSeek 标称 1M 上下文。窗口很大带来一个后果：按"窗口的 60%、80%"设置的折叠与压缩阈值，几乎永远不会触发，任务内上下文会一直增长，成本也随之增长（缓存命中的部分便宜，但不是免费）。

所以阈值要同时有**绝对上限**：

```
折叠触发线 = min(窗口 × 0.6, task_context_soft_tokens)     例如 soft = 100k
压缩触发线 = min(窗口 × 0.8, task_context_hard_tokens)     例如 hard = 160k
```

具体数值依据实际成本和你的任务长度来定，写在配置里。会话级的 `session_token_limit`（默认 32000）本来就是绝对值，不受影响。

## 8. 其他需要注意的

| 项 | 说明 |
|---|---|
| 采样参数 | 思考模式下 `temperature` 无效。对推理档不必再强调温度 0；对快速档（思考关闭）温度仍然生效 |
| 用量字段 | 文档给出了缓存字段，推理 Token 的明细字段是否提供，未在抓取的页面中确认，待验证；没有就只按总量 |
| 并行工具调用 | 文档页面没有明确说明，待验证；不支持时 `parallel_tools = false`，网关按串行处理 |
| 限流 | 文档摘要没有给出限流数值。网关的并发信号量与 429 退避保持默认，运行后依据实际观察调整 |
| 工具调用的参数 | 仍按网关的规范化处理：非法 JSON 标记为格式错误，`length` 截断不执行 |
| 错误码 | 400（含缺少 `reasoning_content`）不重试；429、5xx、超时重试 |
| 旧模型名 | 退役名称虽仍被接受，行为可能与新名称不同。配置里只用文档当前列出的名称 |
| 视觉 | `deepseek-flash` 支持视觉，本项目不使用 |
| Anthropic 格式 | 本项目不使用 |

## 9. 上线前验证清单

每一项都是一个小的集成脚本或测试，用真实密钥手动触发（默认不在 CI 中运行）：

1. 连通性：两档各发一个最简单的请求，确认模型名、基址、鉴权。
2. **用量字段**：确认 `usage` 里有 `total_tokens`、`prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`，以及是否有推理 Token 明细。
3. **思考开关**：`thinking: disabled` 的请求，响应里没有 `reasoning_content`；`enabled` 的请求有。
4. **带工具、思考关闭**：多轮工具调用不报 400；历史里没有 `reasoning_content` 的助手消息不引发错误。
5. **带工具、思考开启且不回传**：确认确实返回 400（验证文档所述，也确认我们的默认是对的）。
6. **不带工具、思考开启**：回传与不回传都不报错；`reasoning_content` 被提取。
7. **JSON 模式**：提示词含 "json" 时正常；去掉后的行为；空内容出现的频率；`max_tokens` 过小时的截断表现。
8. **并行工具调用**：一次请求能否返回多个 `tool_calls`。
9. **缓存**：同一前缀连续两次请求，第二次的 `prompt_cache_hit_tokens` 明显大于零；在前缀中间改一个字节，命中数明显下降。
10. **限流**：并发 4 路请求观察是否 429。
11. **长上下文表现**：把上下文喂到几十万 Token，观察延迟与成本，用来定 §7 的绝对阈值。
12. **记忆段放 `user` 侧**（装配 §2.1 的待验证项）：画像与准则是否被遵循；连续 `user` 消息是否被接受。

验证结果记入 `12_decisions` 或一份"已验证端点清单"，格式：日期、模型名、各项通过与否、观察到的数值。

## 10. 对其他文档的影响

| 文档 | 调整 |
|---|---|
| [网关](01_gateway.md) | 能力声明增加 `thinking_control`、`thinking_with_tools`；`Usage` 增加缓存字段；`reasoning_effort` 增加 `max` |
| [思考内容](03_reasoning_content.md) | 回传策略预留 `all_when_tools`；增加 DeepSeek 的例子与 §4 的结论 |
| [结构化输出](02_structured_output.md) | `json_object` 要求提示词含 "json"，空内容重试 |
| [窗口与压缩](../03_context/02_window_and_compaction.md) | 阈值同时有绝对上限 |
| [0003](../12_decisions/0003-reasoning-passback-default.md) | 补充 DeepSeek 的约束与能力声明 |

## 11. 待定

- 推理档要不要在规划阶段使用 `max` 强度。成本和时延会明显上升，倾向 `high`，由评测决定。
- 是否需要为快速档在特定任务（例如复杂的代码重构）临时开启思考。如果要，就必须实现 `all_when_tools`，见 §4.2。
- 是否保留跨家降级（DeepSeek 不可用时回退到另一家）。如果保留，另一家的能力声明与回传策略要分别验证。
