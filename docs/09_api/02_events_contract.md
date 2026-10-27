# SSE 事件契约

运行时一侧的发出规则、缓冲、持久化见 [可观测性：事件与 SSE](../08_observability/01_events.md)。本文是**对客户端的契约**：怎么订阅、怎么重连、每种事件的载荷。

## 1. 订阅

```
GET /api/v1/tasks/{task_id}/stream
Accept: text/event-stream
Last-Event-ID: <seq>        重连时携带，可选
```

响应是标准 SSE：每个事件一帧，`id` 为 `seq`，`event` 为事件名，`data` 为 JSON。

```
id: 17
event: tool.call
data: {"task_id":"…","seq":17,"ts":1760000000.1,"event":"tool.call","tool":"bash","args":{…},"step":3}
```

### 1.1 行为

1. 先重放缓冲里 `seq` 大于游标的事件，再接实时。
2. 任务已结束的，重放完即关闭连接。
3. 空闲 `sse_heartbeat_sec`（默认 15 秒）发一次 `heartbeat`，不带 `id`。
4. 服务端在任务终态后关闭流；挂起等待审批时**不关闭**。

### 1.2 重连

客户端实现要求：

- 记录最后收到的 `seq`，断线后带 `Last-Event-ID` 重连。浏览器原生 `EventSource` 会自动处理，自定义 `fetch` 流式读取则要自己实现。
- **检测序号不连续**：收到的 `seq` 比上一个大于 1，说明丢了事件（慢消费者被丢弃），应主动断开并重连，或改用 `GET /tasks/{id}` 与 `/timeline` 重新同步。
- 收到 `task.error` 且 `code=STREAM_GAP`：游标已滑出缓冲，必须改用快照与时间线同步，再从最新 `seq` 重新订阅。
- 重连采用指数退避，上限 30 秒。

### 1.3 认证

浏览器的 `EventSource` 不能设置自定义请求头，所以令牌认证对 SSE 需要特殊处理，见 [03 §2](03_access_and_errors.md)。

## 2. 信封

所有带序号的事件：

| 字段 | 说明 |
|---|---|
| `event` | 事件名 |
| `task_id` | 任务 ID |
| `seq` | 任务内单调递增 |
| `ts` | 发出时间 |
| `v` | 事件模式版本（新增，缺省视为 1） |

客户端必须**忽略未知字段**，并对未知事件名降级为只显示名称和时间。

## 3. 任务事件

| 事件 | 载荷 | 说明 |
|---|---|---|
| `task.started` | `task_goal` | 开始执行 |
| `task.waiting_for_approval` | 审批请求，见 §6 | 任务已挂起，**不是终态** |
| `task.approved` / `task.rejected` | `approval_id`、`scope` / `reason` | 决策已提交，任务即将恢复 |
| `task.finished` | `status`、`termination_reason`、`step_count`、`total_tokens`、`delivery` | 终态，最后一个带序号的事件 |
| `task.error` | `code`、`message` | 不可恢复异常或流错误 |

`delivery` 是面向用户的交付结论，已脱敏金丝雀；若是 JSON 形式的结构化结论，服务端已渲染成 Markdown。

## 4. 阶段事件（旧实现的契约）

> 主循环收敛为纯 agent loop 后（[0006](../12_decisions/0006-pure-agent-loop.md)），没有阶段，下面的 `plan` 与 `node.finished` 被回合事件取代，见 §4.1。迁移期间服务端与前端按 §4.1 的映射同步修改，期间旧事件继续可用。

| 事件 | 载荷 |
|---|---|
| `plan` | `thought`、`directive`、`milestones[]`、`step` |
| `node.finished` | `node`、`stage`（阶段序号，不是信封的 `seq`）、`input_summary`、`output_summary`、`decision`、`step_count`、`total_tokens`、`should_terminate`，规划/评估时有 `milestones[]`，直接答复时有 `content` |

`node.finished.decision` 取值：`pass`、`call_tools`、`executed`、`reply`、`accepted`、`continue` 以及规划阶段的指令文本。

里程碑：`{ id, title, description?, status }`，`status` 为 `pending | in_progress | completed | failed`。

### 4.1 目标设计：回合事件

| 事件 | 载荷 | 取代 |
|---|---|---|
| `model.reply` | `turn`、`content`、`tool_calls[]`（只含 id 与工具名）、`reasoning?`（截断） | — |
| `plan.updated` | `plan[]`：`{ id, title, status }`，`status` 为 `pending \| in_progress \| completed` | `plan` |
| `turn.finished` | `turn`（回合序号，不是信封的 `seq`）、`tool_count`、`failed_count`、`tokens`（统计值） | `node.finished` |

`task.finished` 的 `step_count` 改为回合数 `turns`，`total_tokens` 保留为统计值，二者都不对应任何上限。`should_terminate` 与 `decision` 字段取消，终态由 `task.finished.status` 表达。

> `input_summary` 与 `output_summary` 现状是硬编码的中文模板文案，不是由实际内容生成的。它们对用户有展示价值，但不应被客户端当作机器可读字段解析。新版保留文案，机器可读信息放在独立字段（`decision`、`milestones`）。

## 5. 工具与模型事件

| 事件 | 载荷 |
|---|---|
| `tool.call` | `tool`、`args`（已脱敏）、`step` |
| `tool.result` | `tool`、`result`（前 500 字符）、`step`、`ok`（新增）、`artifact_id`（新增） |
| `llm.call` | `id`、`node`、`step`、`tier`、`model`、`tokens`、`duration_ms`、`attempts`、`finish_reason`、`error?`、`blob_ref`；**完整 `messages` 与响应不再内联**，见下 |

**`llm.call` 的变化。** 现状事件内联了完整的提示词和响应，体积巨大。新版事件只带摘要，完整内容通过 `GET /tasks/{id}/llm_calls/{call_id}` 按需拉取。

## 6. 审批请求载荷

`task.waiting_for_approval` 与 `TaskOut.approval_request` 同构：

```json
{
  "approval_id": "appr_…",
  "call_ids": ["call_1"],
  "required_level": "full_permissions",
  "current_level": "workspace_write",
  "action_type": "network_egress",
  "command": "curl https://…",
  "reason": "命中网络外联模式；所需级别高于当前级别",
  "escalation_count": 2,
  "related_actions": ["pip install requests"]
}
```

`action_type` 取值：`read`、`workspace_write`、`network_egress`、`global_env`、`privileged`、`unknown`。这是稳定枚举，前端据此映射图标与文案。`command` 展示完整命令（已脱敏），**不要截断到用户无法判断的长度**。

## 7. 研究与子智能体事件

| 事件 | 载荷 |
|---|---|
| `research.started` | `topic`、`questions`、`max_sources` |
| `research.round` | `round`、`queries[]`、`fetched`、`evidence` |
| `research.finished` | `rounds`、`sources`、`findings`、`version_facts`、`tokens`、`elapsed_sec` |
| `subagent.started` | `role`、`depth`、`assigned_tools[]` |
| `subagent.step` | `step`、`tools[]` |
| `subagent.tool` | `step`、`tool`、`ok`、`duration_ms`、`untrusted`、`summary`（≤300 字符） |
| `subagent.blocked` | `step`、`tool`、`required_level`、`current_level`、`action_type`、`reason` |
| `subagent.finished` | `role`、`status`、`steps_used`、`tool_calls`、`tokens`、`findings`、`verified_findings`、`elapsed_sec` |

`subagent.blocked` 值得前端突出显示：它解释了子任务为什么做不成某件事。研究事件**永远不含页面正文**。

`code_search.*` 随 RAG 移除。

## 8. 传输事件

| 事件 | 说明 |
|---|---|
| `heartbeat` | `{ task_id }`，不带 `id`，不计入序号 |
| `stream.dropped`（新增） | 慢消费者被丢弃事件后的通知：`from_seq`、`to_seq`；收到后应重连 |

## 9. 典型序列

一次带审批的工具任务（目标设计）：

```
task.started
model.reply                        ← 回合 1：带工具调用
plan.updated                       ← 若模型调用了 update_plan
tool.call (bash)
task.waiting_for_approval          ← 挂起，客户端展示审批卡片
   … 用户批准 …
task.approved
tool.result (bash)
turn.finished
… 若干回合 …
model.reply                        ← 最终回复，无工具调用
turn.finished
task.finished (succeeded)
```

旧实现的序列是 `plan`、`node.finished (planner)`、`node.finished (budget_guard)`、`tool.call`、`node.finished (executor)` … `node.finished (evaluator)`，迁移前仍以它为准。

注意审批后事件的 `seq` 接着挂起前的继续，不会重置，即使中间经历过进程重启。

## 10. 客户端实现清单

- [ ] 保存最后的 `seq`，重连带 `Last-Event-ID`。
- [ ] 检测不连续并重连。
- [ ] 处理 `STREAM_GAP`：改用快照加时间线同步。
- [ ] 忽略未知字段和未知事件。
- [ ] `task.waiting_for_approval` 不当作终态。
- [ ] 以 `task.finished` 作为流的结束标志，之后不再重连。
- [ ] 不依赖事件条数估计进度，用计划条目和回合序号。
- [ ] 展示 `command` 时不二次截断。

## 11. 测试要点

- 服务端：重放加实时的衔接无重复无遗漏；游标缺口；心跳；终态后关流；挂起期间不关流；慢消费者通知。
- 契约：每种事件的必需字段；`v` 缺省为 1；新增字段不破坏旧客户端（用旧版事件快照回放）。
- 脱敏：`tool.call` 的 `args` 里没有疑似密钥。
- 客户端（前端测试）：断线重连、不连续检测、乱序容忍。

## 12. 实现状态

事件由 `agent_runtime.observability.EventHub` 分配序号、持久化并分发，由 `agent_runtime.runtime.events.TaskEvents` 把循环翻译成本文的事件。与上文的差异：

- **`node.finished` 的阶段序号叫 `stage`**，不是 `seq`：信封的 `seq` 是事件序号，同名的载荷字段会被信封覆盖。客户端用 `stage` 显示进度。
- **`stream.dropped` 之后服务端关闭该订阅**：慢消费者读完已有事件后收到它，然后流结束；客户端带着游标重连，重放从事件库补齐，不会丢。
- **游标缺口**有三种：早于可重放的最旧事件、事件库已按保留策略清理了中间一段、游标超前于服务端已知的最新序号；都以一条 `task.error`（`code=STREAM_GAP`）结束流。
- **挂起不结束流；`task.finished` 结束流**，但已结束的任务可能被续跑，所以从头订阅会越过中间的终态事件，只在“终态就是最后一个事件”时才关。
- **`task.error`** 在不可恢复异常时发（`code=TASK_FAILED`），随后总有一条 `task.finished`。
- **`tool.result`** 带 `call_id`、`ok`、`artifact_id`；`ok` 来自工具包装层经内部的 `tool.observed` 递来的结果，该信号本身不对外发布，随阶段提交后的 `tool.result` 一起发出。
- **`tool.call` 的 `args`、`tool.result` 的 `result`、审批的 `command` 与 `reason`** 都过统一脱敏，金丝雀由 `TaskEvents` 自己并入；审批的 `command` 不截断，`reason` 等摘要类字段限长 300。
- **`llm.call`** 由网关的调用记录经 `LlmCallEvents` 发出（每次尝试一条，含重试与降级）：`node`（purpose）、`tier`、`endpoint`、`model`、`attempts`、`outcome`、`duration_ms`、`tokens`、`finish_reason`、`error?`，以及服务商给了才有的用量拆分：`prompt_tokens`、`completion_tokens`、`reasoning_tokens`、`cache_hit_tokens`、`cache_miss_tokens`（缓存命中率与各阶段开销由它们算出，评测框架读取）。记录本身不含消息正文，所以事件只有摘要，也没有 `blob_ref`；完整提示词的外置与按需拉取尚未做。任务收尾前会等在途的 `llm.call` 发完，`task.finished` 仍是最后一个事件。
- **回合事件已发出**（`model.reply`、`plan.updated`、`turn.finished`），**同时仍发旧的 `plan` 与 `node.finished`**（`node` 取 `agent` / `tool_runner`，`should_terminate` 恒为 `false`）：前端 `taskReducer` 还在消费旧事件，改完后去掉。`task.finished` 的 `step_count` 与新增的 `turns` 都是回合数，`total_tokens` 是统计值（`LlmCallEvents` 按任务累计）。`GET /system/limits` 只剩 `max_concurrent_tasks`。
