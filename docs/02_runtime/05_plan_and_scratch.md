# 计划与草稿

草稿是**单个任务（一次对话）内的临时记忆**：模型通过工具写，装配器每次回显给模型，不参与控制流。它取代旧的 planner、里程碑和 evaluator 沉淀的事实。

## 1. 数据

存放在 `AgentState`，随检查点持久化：

| 字段 | 类型 | 上限 |
|---|---|---|
| `plan` | `tuple[PlanItem]`，`PlanItem(id, title, status)`，`status ∈ pending / in_progress / completed` | 12 条；标题 ≤ 100 字 |
| `facts` | `tuple[str]` | 20 条；每条 ≤ 300 字 |
| `failed_attempts` | `tuple[FailedAttempt]`，字段 `action / failure_reason / conclusion` | 10 条；每个字段 ≤ 300 字 |

`FailedAttempt` 是全工程唯一定义，记忆模块复用。会话继承来的摘要、事实、失败尝试**不进状态**，只在任务启动时冻结进提示词资产，因此任务内的写入不会覆盖继承内容，也不会让资产段的缓存失效。

## 2. `update_plan`

```
update_plan(items: [{id, title, status}])
```

- **整体替换**当前计划，不做增量。模型每次给出完整列表。
- 内置工具，权限级别 `read_only`，信任级别 `trusted`。
- 校验：条数 ≤ 12，`id` 唯一，`status` 在白名单内；标题超长截断。违规返回失败结果并说明原因，计划不变。
- 空列表表示清空计划。
- 成功后发 `plan.updated` 事件。
- 计划对控制流**没有任何影响**：全部条目 `completed` 不会结束任务，没有计划也不会阻止任务结束。

## 3. `note`

```
note(kind="fact", text)
note(kind="failed_attempt", action, failure_reason, conclusion)
```

- 追加一条笔记。与已有条目逐字相同则忽略并提示"已存在"。
- 条数达到上限返回失败结果，提示模型合并或放弃，不静默淘汰。
- 内置工具，`read_only`、`trusted`。
- 笔记是模型自己的陈述，不是经过独立验证的事实。进入会话记忆时的把关见 §6。

## 4. 回显

装配器在**请求尾部**渲染一段 `<scratch>`，与最新的 user 消息同位置，不写进历史：

```
<scratch>
计划：
  [x] 1 定位失败用例
  [>] 2 修复 parse() 的边界处理
  [ ] 3 运行全部测试
本任务笔记：
  事实：…
  失败尝试：…
</scratch>
```

- 只渲染任务内新增的笔记；资产里已有的不重复。
- 为空时整段省略，不留空标题。
- 放在尾部，所以草稿每次变化只影响尾部，前缀缓存不受影响。
- 草稿永不被折叠或压缩。

## 5. 证据提醒

工具后处理维护 `verification`，来自工具结果，不来自模型自述：

| 值 | 含义 |
|---|---|
| `none` | 本任务还没有修改过文件 |
| `unchecked` | 修改文件之后，还没运行过命令 |
| `failed` | 修改文件之后，最近一条命令失败（退出码非零） |
| `passed` | 修改文件之后，最近一条命令成功 |

装配器按值在 `<scratch>` 里追加一行事实陈述：

- `unchecked`：`你修改过文件，此后尚未运行任何命令验证。`
- `failed`：`你修改文件后，最近一条命令失败（退出码 N）：<命令前 80 字符>。`
- `none`、`passed`：不显示。

提醒只陈述事实，不下指令，也不拦截结束：模型可以无视它，因为一个无关的非零退出码不应把任务卡死。这是对旧 evaluator"硬证据压过模型判断"的有意放弃，见 [0006](../12_decisions/0006-pure-agent-loop.md)。

## 6. 任务结束时的沉淀

任务以 `succeeded` 或 `failed` / `terminated` 结束后，`facts` 与 `failed_attempts` 交给记忆沉淀（[记忆 §5.2](../03_context/04_memory.md)）：

- 成功任务沉淀事实与失败尝试；失败任务只沉淀失败尝试。
- 条目来源标 `task_note`；读取过不可信内容的任务（出现过带 `trust="untrusted"` 信封的观察值）的事实标 `untrusted`。
- 沉淀入口照常过长度、注入样态扫描与去重。
- `plan` 不沉淀。任务的结论由交付文本和 transcript 承担。

## 7. 不变量

1. 计划与笔记不影响控制流。
2. 上限在写入时强制，不靠渲染时截断。
3. 草稿工具不产生外部副作用，可安全重试。
4. 继承来的记忆不进 `facts` / `failed_attempts`。
5. `verification` 只由工具结果推出。

## 8. 测试要点

- `update_plan`：整体替换、超限、重复 id、非法状态、清空。
- `note`：去重、达到上限、两种 kind 的必填字段。
- 回显：空草稿不出现；只含任务内新增；变化只影响尾部（逐条比较前缀）。
- `verification` 四态转换：改文件 → `unchecked`；跑命令失败 → `failed`；成功 → `passed`；再改文件回到 `unchecked`。
- 沉淀：失败任务只沉淀失败尝试；不可信任务的事实标 `untrusted`。

## 9. 实现状态

**已实施。** `loop/scratch.py` 提供 `update_plan`、`note` 与纯函数 `apply_scratch`；它们在工具包装层就地处理（改状态，不经派发器），注册表里的 `UpdatePlanTool`、`NoteTool` 只提供 Schema。上限在写入时强制（`check_invariants` 在提交时再断言一次）。回显由 `PromptAssembler._scratch` 渲染（`prompt.goal_echo_chars` 控制目标行长度）。`verification` 由包装层更新，回显里的"最近一条命令失败"不带退出码（状态里没有它）。`plan.updated` 事件已发，同时仍发旧的 `plan` 事件。笔记沉淀到会话记忆走原有的 `deposit`（失败任务的事实不沉淀）。
