# 运行时设计

Agent Runtime 负责把一个任务目标推进到交付：维护状态，驱动主循环，在每个阶段边界持久化，并在需要人工审批时挂起、之后续跑。它不依赖任何图编排框架，核心就是一个显式的异步循环加一张 SQLite 表。

## 文档

| 文档 | 内容 |
|---|---|
| [01_state_model.md](01_state_model.md) | 消息类型、`AgentState` 字段与写入归属、更新语义、序列化 |
| [02_main_loop.md](02_main_loop.md) | 五个阶段、路由表、循环骨架、终止与状态映射、事件发出 |
| [03_checkpoint_and_resume.md](03_checkpoint_and_resume.md) | 检查点表结构、提交时机、崩溃恢复语义、续跑入口 |
| [04_approval.md](04_approval.md) | 权限闸门、挂起与恢复协议、审批决策的作用域 |
| [05_planner_evaluator.md](05_planner_evaluator.md) | planner 与 evaluator 的契约、完工判定、打回上限与降级处置 |
| [06_executor_tool_runner.md](06_executor_tool_runner.md) | executor 与 tool_runner：闸门、派发前标记、观察值治理、指纹与连续错误 |

决策依据见 [12_decisions](../12_decisions/)：

- [0001 主循环保留五阶段](../12_decisions/0001-main-loop-shape.md)
- [0002 审批采用落盘挂起](../12_decisions/0002-approval-suspension.md)

> 以上两项决策均已采纳，本目录的设计以它们为前提。若日后推翻其中任何一项，需同步修订本目录。

## 设计原则

1. **状态是唯一事实来源。** 循环只读写 `AgentState`，进程内的其他对象（预算守卫、裁剪器、账本）都是任务级辅助，不承载需要跨进程恢复的事实。
2. **阶段函数是"状态进、增量出"。** 阶段不直接改状态，由循环统一应用增量并提交检查点，保证"应用增量"与"落盘"之间没有别的写入者。
3. **提交点即恢复点。** 只有在检查点提交成功之后，增量才算生效。恢复总是从最近一次提交的阶段边界重新进入下一个阶段。
4. **挂起不是阻塞。** 等待审批时任务退出循环并释放执行资源，之后由决策到达重新拉起，不在内存里持有一个悬空的协程。
5. **护栏是确定性代码。** 预算、重复指纹、连续错误都由代码强制，模型只能被告知，不能决定是否生效。

## 与旧版的对应

| 旧版（LangGraph） | 本版 |
|---|---|
| `StateGraph` + `add_conditional_edges` | 路由表 + `while` 循环 |
| `add_messages` Reducer | 状态应用函数里对 `messages` 的追加语义 |
| `AsyncSqliteSaver` | 自有 `task_checkpoints` 表 |
| `interrupt()` / `Command(resume=…)` | `ApprovalRequired` 异常 + 状态中的 `pending_approval` / `approval_decision` |
| `astream(stream_mode="updates")` 事后解析增量 | 循环在阶段边界直接发出事件 |
