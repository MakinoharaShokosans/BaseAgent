# 主循环

决策依据：[0006 主循环收敛为纯 agent loop](../12_decisions/0006-pure-agent-loop.md)。

## 1. 形状

循环只做一件事：调模型；模型要工具就执行并追加结果，再调模型；模型不再要工具就结束。下一步是什么由模型的回复决定，不由状态机或路由表决定。

循环**只依赖四样东西**：

| 依赖 | 作用 |
|---|---|
| 工具集 | 内置工具、MCP 工具、技能工具，统一的 `AgentTool` 协议，见 [工具系统](../04_tools/01_tool_system.md) |
| 上下文装配器 | `assemble()`：从只追加的 transcript 与草稿生成本次请求的消息，见 [提示词装配](../03_context/03_prompt_assembly.md) |
| 会话草稿 | 计划与笔记，见 [计划与草稿](05_plan_and_scratch.md) |
| 模型句柄 | 一个可调用对象，由模型网关实现 |

循环**不认识**：预算、权限、审批、检查点、事件、取消、压缩、信封。它们在循环外，经下面的接缝生效。

## 2. 内核

```python
async def run(ctx: LoopContext) -> None:
    pending = ctx.transcript.unanswered_calls()         # 续跑入口：尾部有未配对调用先处理
    while True:
        if not pending:
            reply = await ctx.model(ctx.assemble(), ctx.tools.schemas())
            ctx.transcript.append(reply)
            if not reply.tool_calls:
                await ctx.checkpoint()                   # 终回合提交
                return
            await ctx.checkpoint()                       # 派发前提交
            pending = reply.tool_calls
        results = await ctx.tools.run(pending)           # 唯一执行点；可能抛 ApprovalRequired
        ctx.transcript.extend(results)
        pending = ()
        await ctx.checkpoint()                           # 回合结束提交
```

- **回合** = 一次模型调用加它引出的全部工具结果。
- `ctx.checkpoint()` 和事件发射是循环声明的端口，由 Recorder 实现：先提交后发事件，提交失败即向上抛，不带着未落盘的进度继续跑。
- 循环**不捕获**任何异常。异常、取消、挂起全部向上传到 Supervisor。
- `ctx.tools.run` 保证为每个调用返回一条配对结果，无论成功、失败、被拒绝、被拦截还是超时。

## 3. 接缝：循环之外的东西怎么生效

| 层 | 职责 | 挂载点 |
|---|---|---|
| **Supervisor** | 任务生命周期、取消、挂起与续跑入口、部分交付、状态映射 | 包住 `run` |
| **模型网关** | 计量、降级端点、重试、金丝雀检测、思考内容回传 | 模型句柄内部，见 [网关](../07_llm_gateway/01_gateway.md) |
| **工具包装层** | 权限判定、审批挂起、重复指纹、连续错误、拦截上限 | `ctx.tools.run` 内，派发之前，见 [工具执行](06_tool_execution.md) |
| **工具后处理** | 不可信内容信封、观察值裁剪、产物登记、验证状态 | `ctx.tools.run` 内，派发之后 |
| **上下文装配器** | 折叠、压缩、技能索引、草稿回显、证据提醒 | `assemble()` |
| **Recorder** | 检查点、事件流、轨迹 | `checkpoint()` 与事件端口 |

## 4. 没有预算

循环、网关、Supervisor 都不设 Token、步数、挂钟上限，不统计"还剩多少额度"。服务商因余额不足、限流或鉴权失败拒绝请求时，网关把它当作普通的不可恢复错误抛出（`LLMUnavailableError` 等），任务按 `failed` 停止；充值后可以从最近的检查点续跑。

保留的刹车只有四道，都不是额度类：

1. 用户取消。
2. 工具包装层的重复指纹拦截；连续被拦截达到 `max_consecutive_blocks`（默认 5）时抛 `LoopAborted`，任务按 `failed` 停止。
3. 金丝雀泄露：网关抛 `CanaryLeak`，任务按 `terminated` 停止。
4. 单次模型调用的超时，在网关内，防止一次调用无限挂起。

**上下文窗口不是预算。** 装配器按分词估算当前提示词规模，据此折叠与压缩，见 [窗口与压缩](../03_context/02_window_and_compaction.md)。它只影响送给模型的视图，不会终止任务。

## 5. 退出与对外状态

| 出口 | 条件 | 外层状态 | 可续跑 |
|---|---|---|---|
| `run` 正常返回 | 模型不再调用工具 | `succeeded` | 否 |
| `ApprovalRequired` | 工具包装层要求审批 | `waiting_for_approval`（非终态） | 经决策续跑 |
| `CancelledError` | 用户取消 | `cancelled` | 否 |
| 模型错误 | 模型不可用、余额不足、鉴权失败、窗口溢出等 | `failed` | 是 |
| `LoopAborted` | 连续拦截达到上限 | `failed` | 是 |
| `CanaryLeak` | 模型输出含金丝雀 | `terminated`（安全终止） | 否 |

- 工具自身的异常从不逃出 `ctx.tools.run`，一律变成失败的工具结果交给模型。
- `failed` 与 `terminated` 的任务由 Supervisor 用代码生成**部分交付**：计划条目的完成情况、已修改的文件、笔记里的事实与失败尝试、最近一次进展；安全类终止不展开内容。正常结束的交付就是模型最后一条回复。
- "做不到"没有专门出口：被拒绝、缺信息时模型自己说明卡在哪并结束，这条回复就是交付。工具包装层返回的拒绝结果明确写明"不要换着花样绕过被拒绝的操作，也不要编造缺失的信息"。

## 6. 取消与原子对

`ctx.tools.run` 被取消时，在重新抛出 `CancelledError` 之前，为每个尚无结果的调用追加一条"已取消，未执行或结果未知"的工具结果并提交（此段不受取消影响）。这保证 transcript 的原子对不变量在任何出口都成立：

> `assistant` 带 `tool_calls` 时，其后必须有与每个调用 id 一一对应的 `tool` 消息。唯一例外是**最后一组**：回合进行中（已提交的回复尚无结果）或任务挂起在审批上。

模型端点对违反此约束的请求返回 400，所以这是正确性问题，不是风格问题。

## 7. 续跑

续跑只看 transcript 的尾部，不依赖任何阶段字段：

| 尾部形态 | 含义 | 处理 |
|---|---|---|
| 最后一条是不带工具调用的 assistant | 已结束 | 拒绝续跑 |
| 最后一条是 tool 消息，或用户消息 | 回合之间 | 直接 `run`（调模型） |
| 末组 assistant 的调用未配对，`pending_approval` 非空 | 挂起在审批上 | 绑定决策后 `run`，工具包装层重新做同一次纯判定并消费决策 |
| 末组 assistant 的调用未配对，无挂起 | 崩溃于派发期间 | 先补齐，再 `run` |

补齐规则（纯函数，幂等）：

- 调用的工具声明了 `idempotent`：丢弃，重新执行。
- 其余：合成结果"执行结果未知：任务在执行期间中断，工具可能已部分或全部生效，请先核验当前状态再决定是否重试"。
- 整批一起处理，不留一半重放一半合成的状态。未知工具（如第三方 MCP）按非幂等。

派发前提交保证崩溃后不会丢失"本回合发出过哪些调用"，从而不会让模型重新发出同一批有副作用的调用。检查点细节见 [检查点与续跑](03_checkpoint_and_resume.md)。

## 8. 事件

循环通过事件端口发出下列事件，提交之后才发；名称与载荷以 [事件契约](../09_api/02_events_contract.md) 为准。

| 事件 | 时机 |
|---|---|
| `model.reply` | 模型回复追加之后 |
| `tool.call` / `tool.result` | 每个调用开始 / 每条结果追加之后 |
| `plan.updated` | `update_plan` 执行之后 |
| `turn.finished` | 回合结束提交之后 |
| `task.waiting_for_approval` | 挂起检查点提交之后 |
| `task.finished` | Supervisor 在终态提交之后 |

事件发送失败只记日志，不影响任务。

## 9. 入口

```python
async def run_task(deps, req) -> TaskOutcome:             # 新任务
async def resume_task(deps, task_id, decision=None)       # 续跑 / 提交审批决策
```

共同步骤：装配任务级对象（装配器资产、工具集、网关句柄、Recorder）→ 取得状态（新建或从检查点加载）→ Supervisor 包住 `run` → 非挂起则收尾（回写会话记忆，见 [记忆](../03_context/04_memory.md)）。

## 10. 不变量

1. 循环不读不写任何额度、权限、检查点相关的东西。
2. 每次提交的 transcript 满足原子对不变量（最后一组按 §6 放宽）。
3. 先提交后发事件。
4. 工具异常不逃出 `ctx.tools.run`。
5. transcript 只追加；折叠与压缩只发生在 `assemble()`。
6. 同一任务同一时刻只有一个 `run`。

## 11. 测试要点

- 脚本化模型驱动：纯文本回复即结束；多轮工具往返；一回合多个并发调用。
- 原子对：成功、失败、拒绝、拦截、超时、取消六种路径都产生配对结果。
- 取消：派发期间取消后，transcript 配对完整，检查点可续跑。
- 崩溃恢复：派发前提交后在派发期间杀进程，续跑时幂等工具重试、非幂等工具得到"结果未知"。
- 余额不足 / 鉴权失败：网关抛错，状态为 `failed`，检查点保留，可续跑。
- 审批：挂起前无副作用；决策绑定；拒绝后模型收到拒绝结果并继续。
- 拦截上限：连续拦截达到上限抛 `LoopAborted`。
- 循环内核不导入 `guardrails.budget`、`checkpoint`、`approval`（依赖方向由 `import-linter` 约束）。

## 12. 实现状态

**设计已定，代码未迁移。** 代码里仍是旧的五阶段实现，迁移清单见 [0006](../12_decisions/0006-pure-agent-loop.md)。
