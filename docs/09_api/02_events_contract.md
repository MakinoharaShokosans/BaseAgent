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

## 4. 阶段事件

| 事件 | 载荷 |
|---|---|
| `plan` | `thought`、`directive`、`milestones[]`、`step` |
| `node.finished` | `node`、`seq`（阶段序号）、`input_summary`、`output_summary`、`decision`、`step_count`、`total_tokens`、`should_terminate`，规划/评估时有 `milestones[]`，直接答复时有 `content` |

`node.finished.decision` 取值：`pass`、`call_tools`、`executed`、`reply`、`accepted`、`continue` 以及规划阶段的指令文本。

里程碑：`{ id, title, description?, status }`，`status` 为 `pending | in_progress | completed | failed`。

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
| `subagent.started` | `role`、`depth`、`assigned_tools[]`、`token_budget`、`max_steps` |
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

一次带审批的工具任务：

```
task.started
plan
node.finished (planner)
node.finished (budget_guard)
tool.call (bash)
node.finished (executor)
task.waiting_for_approval          ← 挂起，客户端展示审批卡片
   … 用户批准 …
task.approved
tool.result (bash)
node.finished (tool_runner)
… 若干轮 …
node.finished (evaluator)
task.finished (succeeded)
```

注意审批后事件的 `seq` 接着挂起前的继续，不会重置，即使中间经历过进程重启。

## 10. 客户端实现清单

- [ ] 保存最后的 `seq`，重连带 `Last-Event-ID`。
- [ ] 检测不连续并重连。
- [ ] 处理 `STREAM_GAP`：改用快照加时间线同步。
- [ ] 忽略未知字段和未知事件。
- [ ] `task.waiting_for_approval` 不当作终态。
- [ ] 以 `task.finished` 作为流的结束标志，之后不再重连。
- [ ] 不依赖事件条数估计进度，用里程碑和阶段序号。
- [ ] 展示 `command` 时不二次截断。

## 11. 测试要点

- 服务端：重放加实时的衔接无重复无遗漏；游标缺口；心跳；终态后关流；挂起期间不关流；慢消费者通知。
- 契约：每种事件的必需字段；`v` 缺省为 1；新增字段不破坏旧客户端（用旧版事件快照回放）。
- 脱敏：`tool.call` 的 `args` 里没有疑似密钥。
- 客户端（前端测试）：断线重连、不连续检测、乱序容忍。
