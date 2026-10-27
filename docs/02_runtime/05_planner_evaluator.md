# planner 与 evaluator

两个都走推理档、都用[结构化输出](../07_llm_gateway/02_structured_output.md)，依赖通过 `ModelStageDeps` 注入。planner 提出主张（做什么、做完了没有），evaluator 独立复核；**让干活的自己宣布完工等于没有验收**。

## 1. 契约与共同规则

契约模型在 `loop/stages/contracts.py`，是两个场景的唯一权威定义。过长的文本截断而不是拒绝，枚举字段用字面量白名单。

| 模型 | 字段 | 说明 |
|---|---|---|
| `PlannerVerdict` | `thought`（必填） | 想法；没有 `next_step` 时当指令 |
| | `next_step` | 交给 executor 的自然语言指令。别名 `action` / `next_action` 收口在模型里，带“计划删除”注释 |
| | `milestones` | 只在首次规划（状态里还没有里程碑）时采用；至多 12 个，标题 ≤ 100 字，描述 ≤ 500 字 |
| | `milestone_updates` | `{id, status}`，状态白名单 `in_progress / completed / failed`：**不含 `pending`**，规划只能推进进度不能倒退 |
| | `all_completed` / `direct_response` | 宣布完成与面向用户的交付文本 |
| `EvaluatorVerdict` | `milestone_ok`（**必填、无默认**） | 缺少明确结论就不是结论：走修复重试，仍缺则按未达成处理 |
| | `all_completed` | 是否全部达成 |
| | `summary` / `feedback` | 进展摘要（≤ 500 字）；未通过时写给 planner 的原因 |
| | `confirmed_facts` | 单个字符串自动包成列表；去空白、每条 ≤ 300 字、至多 20 条 |
| | `failed_attempts` | `{action, failure_reason, conclusion}` 三项必填，至多 10 条 |

### 1.1 共同的失败处置

两个阶段对模型调用的失败做同样的处理（`loop/stages/common.py` 的 `ask`），不抛异常，返回终止增量——“LLM 全链路不可用”属于预期内的失败：

| 情形 | 增量 |
|---|---|
| `LLMUnavailableError` | `should_terminate`，原因 `LLM 不可用: …` |
| `LLMDeadlineExceeded` | `should_terminate`，原因“预算耗尽：截止时间不足…” |
| `LLMBadRequest`（上下文超长） | `should_terminate`，原因“上下文超出模型窗口…”；阶段 5 的折叠接入后，这里改为触发折叠 |
| `LLMBadRequest`（其他） | `should_terminate`，原因“请求被模型端点拒绝” |

增量里只有 `should_terminate` 与 `termination_reason`：没有拿到响应就没有 Token 可记。

### 1.2 金丝雀

模型的原始输出（正文、工具参数，降级时含原文）里出现金丝雀，就终止任务，`termination_reason = "[SECURITY] Prompt leak detected via canary token"`，**丢弃本次的全部结论**，只记 Token。检测函数是 `guardrails.detect_canary_leak`（递归扫字符串、字典键与值、列表；空 token 永不命中）。

### 1.3 依赖

`ModelStageDeps(gateway, assemble, deadline, emit, reasoning_effort)`：

- `gateway`：满足 `invoke_structured` 即可，生产里是 `LLMGateway`。
- `assemble(state, instruction) -> messages`：提示词装配。真实装配器属于阶段 5（[提示词装配](../03_context/03_prompt_assembly.md)），目前由调用方注入。阶段指令在 `loop/stages/prompts.py`，只写语义与判断标准，字段表由契约导出的 Schema 提供。
- `deadline()`：本次调用的绝对截止时间，`clock() + guard.remaining_wall_sec()`。
- `emit(name, payload)`：发事件，失败只记日志。目前发出 `structured_output.degraded`（`stage`、`reason`、`problems`、`rounds`）；事件总线接入后由它承接。

## 2. planner

### 2.1 增量

只返回 `messages`（追加**一条** assistant 指令）、`milestones`、`total_tokens`。不写 `phase`。

- **首次规划**：状态里没有里程碑时，采用 `milestones`，编号按顺序重排为 1..n，状态 `pending`。之后的结论里再带 `milestones` 一律忽略，不能另起炉灶。
- **更新**：按 id 套用 `milestone_updates`，未知 id 忽略，同一 id 以最后一条为准。

### 2.2 完工判定

```
claimed = all_completed 或（有里程碑且全部 completed）
done    = claimed 且 next_step 为空            ← 还有待办指令，以未完成为准
done    = false  若 step_count == 0 且没有 direct_response   ← 没动手就宣布完成，必须带着答案
```

- **完成**：全部里程碑标为 `completed`（一个都没有就合成一个“响应用户咨询与交付任务”）；消息内容取 `direct_response`，其次 `thought`，其次“任务已达成。”。路由随后走 evaluator（做过事）或 `DONE`（未做事的直接答复）。
- **未完成**：消息内容取 `next_step`，其次 `thought`，其次“继续推进当前里程碑。”。**如果状态更新恰好把里程碑全标成了完成但本轮没有被接受，重新打开最后一个**，否则路由会直接放行。

旧版一堆“`status` / `is_completed` / `milestone_ok` 各种取值都当作通过”的别名与优先级，随契约一并去掉：判定只看 `all_completed`、`next_step` 与里程碑状态。

### 2.3 降级

| 原因 | 处置 |
|---|---|
| `invalid` | 原文当作指令，**但**以 `{`、`[`、围栏开头的残片不喂给 executor，改用通用指令；原文截到 4000 字 |
| `truncated` | 通用指令 |
| `content_filter` | 终止，“模型输出被内容过滤拦截” |

三者共同点：里程碑不变，**绝不据此宣布完成**，发 `structured_output.degraded`，Token 照记。

## 3. evaluator

### 3.1 判定

- `milestone_ok` 为真：**通过**。把当前里程碑标为完成（`all_completed` 为真则全部）；指针进到下一个（到末尾停在末尾）；合并事实（保序去重）；追加失败尝试（去重）；摘要非空才覆盖。若此时全部完成，且最后一条消息还不是 assistant 的正文交付，补一条 assistant 消息（摘要或“验收完成。”）。
- 否则（含降级）：**打回**。
  - 把当前里程碑重新打开为 `in_progress`——planner 若主张“全部完成”才会来到这里，不重开的话路由会因“全部完成”而放行。
  - 追加一条 `user` 角色消息，以 `【验收未通过】` 开头，内容是 `feedback`，其次 `summary`，其次默认提示。
  - **不**记事实（未经证实），失败尝试与摘要照记。

### 3.2 成功不是终止

通过后**不设 `should_terminate`**：全部完成由路由送往 `DONE`，状态映射成 `succeeded`。旧版把“验收通过”写成 `should_terminate = true`，在新版会被映射成 `terminated`，这一点必须区别对待（已测）。

### 3.3 打回上限

planner → evaluator → planner 这条回路**不经过 budget_guard**（步数不增加），物理预算管不到它。上限由打回次数给出：累计打回（统计消息里以 `【验收未通过】` 开头的 `user` 消息，不新增状态字段）超过 `max_rejections`（默认 3）就**终止**任务，原因“验收连续 N 次未通过…”。

> 旧版在“已连续打回 ≥ 2 次且已有直接答复”时**放行**，这与“宁严勿松”矛盾，也让评估闸门形同虚设；新版到上限即终止，不放行。上限暂为阶段参数，没有进配置；真有调节需求时再加。

### 3.4 降级：按未达成处理

`invalid` / `truncated` / `content_filter` 一律打回，反馈为“验收结论无法解析，按未达成处理”。**降级文本里即使写着“验收通过”也不采信**（已测）。降级同样计入打回次数，所以一个持续输出垃圾的评估者最终会终止任务而不是无限循环。

## 4. 与主循环的接口

- 两个阶段都满足 `Stage` 签名，只读状态、返回增量，不写 `phase`。
- 路由未改：`route_after_planner` / `route_after_evaluator` 仍只读状态。本文的所有“完成”“重开”语义最终都落在里程碑状态上，由路由据此决定下一跳。
- 接入 `drive` 的端到端测试：规划→执行→验收通过；直接答复；打回后补做再通过；评估者永不同意（打回上限终止）；planner 坚持宣布完成而 evaluator 永不同意（同样由打回上限终止）。

## 5. 实现状态

已落地：`agent_runtime.loop.stages` 的 `contracts`、`common`、`prompts`、`planner`、`evaluator`，以及 `guardrails.canary`。

尚未做：

- 真实的提示词装配器与阶段指令的打磨（阶段 5），目前的指令文本是最小可用版。
- 提示词与模型字段一致性检查（[结构化输出 §8](../07_llm_gateway/02_structured_output.md)）。
- 轨迹记录（`TrajectoryRecorder`）与 `llm.call` 事件：等观测层。
- 研究、子智能体的契约（`ResearchRaw`、`SubagentRaw`）与压缩器的 `CompactionResult`。
- 对真实模型的验证：指令是否真能让模型遵守“拿不准就判未达成”、`all_completed` 与 `next_step` 的搭配，需要接真实端点后看格式失败率与打回率。

## 6. 回路改动（实现状态补充）

- **planner 不再每步都跑。** 见 [主循环 §2](02_main_loop.md)：默认由 executor 连续执行，planner 只在首次规划、定期复核（`replan_interval`）、连续失败/循环/被拒时运行。planner 连续降级超过 `max_planner_degradations`（默认 3）终止任务，不让空转烧到预算用完；一次成功的规划清零计数。
- **指令尾部带进度提示**（`progress_hint`）：已执行步数与 Token 百分比，三个阶段都有。它在上下文尾部，不影响前缀缓存。
- **evaluator 拿到代码侧证据。** 指令里附一句由 `state.verification` 得出的事实（没改过文件 / 改后没运行过命令 / 最近命令失败 / 最近命令成功）。**硬证据压过模型的判断**：改完文件后最近一条命令失败时，即使模型判通过也按未达成处理（反馈里说明原因）。这条硬拦只触发一次，之后要么修复并重跑（会更新 `verification`），要么由模型结合说明再判，免得无关的非零退出码把任务卡死。
- **“做不到”有出口：`blocked`。** planner 与 evaluator 的输出都带 `blocked` / `blocked_reason`：关键操作被人工拒绝、缺少只有用户知道的信息、环境不允许时，不再一轮轮打回直到验收上限，而是带着已完成的部分结束（`termination_reason` 以“受阻：”开头，交付见 [主循环 §5](02_main_loop.md) 的部分交付）。指令明确要求不要换着花样绕过被拒绝的操作，也不要编造缺失的信息。evaluator 在模型同时判了通过时忽略 `blocked`。依据：基线评测里“只读权限下写文件”这个用例，模型已经正确停手，却因为没有这个出口被打回 4 次、白烧 5.3 万 Token。
- **打回次数是显式状态 `rejections`**，上限 `max_rejections`（默认 3）。到上限终止，并带着已完成的部分交付，而不是留一条内部指令当交付。

