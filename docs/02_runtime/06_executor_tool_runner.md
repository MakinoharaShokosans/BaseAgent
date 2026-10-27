# executor 与 tool_runner

planner 与 evaluator 见 [05](05_planner_evaluator.md)。这里是另外两个真实阶段：executor 把指令变成工具调用（快速档），tool_runner 负责闸门、派发与观察值治理（无模型调用）。五个阶段至此齐全。

## 1. executor

`loop/stages/executor.py`，依赖 `ExecutorDeps(gateway, assemble, tools, deadline)`。网关只需满足 `invoke(request)`。

- 请求走**快速档**，带本任务的主工具集（`registry.schemas()`，按名排序；空集合发 `None` 而不是空数组）。阶段指令在 `prompts.py` 的 `EXECUTOR_INSTRUCTION`。
- 增量：`messages`（追加模型返回的那条 assistant 消息，**保留** `reasoning` 与 `produced_by`，思考回传靠它）、`step_count + 1`、`total_tokens`。**每轮必须加步数**：`budget_guard` 的终止保证依赖它，已有测试断言。
- 网关错误转终止增量，处置同 planner（[05 §1.1](05_planner_evaluator.md)）；`LLMInvalidResponse` 由网关内部重试，不会到这里。
- **金丝雀**：回复正文或任一工具调用的参数（含嵌套）里出现金丝雀，终止任务、只记 Token、**该条消息不进入状态**，因此其中的工具调用不会被执行。
- **格式错误的调用**：网关把参数不是合法 JSON 对象的调用的原文放在 `args["_raw"]`，并登记在 `malformed_calls`；被长度截断的调用参数可能恰好是合法 JSON 却不完整，网关同样登记。状态里没有地方存这份登记，所以 executor 把**所有**登记在案的调用的参数改写成 `{"_raw": <原文>}`（已是这个形态的不动）。这个形态是整条链上的约定：网关再次发给模型时把原文原样送回，tool_runner 据此拒绝执行。

## 2. tool_runner

`loop/stages/tool_runner.py`，依赖 `ToolRunnerDeps`：

| 字段 | 说明 |
|---|---|
| `dispatcher` | `tools.core.ToolDispatcher` |
| `policy` | `PermissionPolicy`，由 `[permissions]` 装配 |
| `limits` | `LoopLimits`：连续错误阈值、指纹阈值、窗口（至少 5，且不小于阈值） |
| `governor` | `ObservationGovernor` |
| `approval` | `ApprovalHooks(approval_id, check_binding)`：`loop` 与 `approval` 是同层模块、不能互相导入，所以由装配层注入真实函数（`runtime.APPROVAL_HOOKS`） |
| `commit` | 阶段中途那一次提交，检查点存储的 `commit` 满足它 |
| `allowlist` | 会话白名单（可变引用，由任务注册表持有，不入检查点） |
| `emit` / `dispatch_marker` / `envelope` | 事件回调、派发前标记开关（默认开）、不可信观察值的信封钩子 |

### 2.1 执行顺序（固定）

1. **权限闸门。** 逐个调用做 `check_permission`。越级且签名不在白名单的构成“越级集合”：
   - 无决策 → 抛 `ApprovalRequired`。此前**没有提交、没有执行、没有记账**。载荷：`call_ids` 只含越级调用（保持调用顺序）；`required_level` 取其中最高的（文档原说取第一个项，这里取最高，审批人看到的是真正需要的级别）；`command`、`reason`、`action_type` 来自第一个越级项；`related_actions` 至多 4 条；`approval_id` 确定性（`compute_approval_id`）。
   - 有决策 → 先 `check_binding`：越级集合必须与请求的 `call_ids` 吻合，且 `approval_id` 能由当前状态重新算出，否则报错，**不套用**。批准：本批越级调用可执行，`always` 把签名加入白名单。拒绝：只为越级调用合成失败观察值（“已被人工审核拒绝：<原因>，请改用合规方案…”，没有原因用“用户未批准该操作”），其余调用照常执行。
   - 没有越级调用时，残留的审批字段是过期的：一律清掉，**不授予任何东西**。
   - **不存在的工具名与参数无法使用的调用不参与权限判定**：它们根本不会被执行，为一个幻觉出来的工具名打扰人没有意义。
2. **指纹登记与死循环判定。** 本批所有调用的指纹追加到滑动窗口；最近 `identical_fingerprint_limit` 次完全一致即命中，**整批**（含批内不同的调用）拦截，不派发。
3. **派发前标记。** 对真正要派发的调用（排除被拒绝、被拦截、参数无法使用的）提交一次带 `dispatch_started` 的检查点；这次提交里的审批字段已清空，否则崩溃恢复会把任务当成“正挂起在审批上”而不处理。提交失败则整个阶段失败，**一个工具都不会执行**。
4. **并发派发。** `ToolDispatcher`。
5. **观察值治理与配对消息。** 每个调用一条 `tool` 消息，顺序与调用一致。失败原因在 `content` 为空时取 `error`。`ToolResult.failed`：`ok` 为假，或退出码非零（即使工具自称成功）。不可信观察值在**裁剪之后**过 `envelope`，警示语不会被截掉；可信的不包。
6. **连续错误与重规划通知。** 成功清零、失败按批内失败个数累加；达到阈值或命中指纹死循环时，在工具消息**之后**追加一条 `user` 角色的 `[系统护栏]` 通知（`system` 只能在最前面）。

增量：`messages`、`consecutive_errors`、`fingerprint_history`、`artifacts`，以及 `dispatch_started = ()`、`pending_approval = None`、`approval_decision = None`。不写 `phase` 与 `step_count`。

### 2.2 什么算失败（计入连续错误）

| 情形 | 计入 |
|---|---|
| 工具 `ok = false`、退出码非零、超时、抛异常、未知工具 | 是 |
| 被指纹拦截 | 是 |
| 参数无法使用（格式错误或被截断） | 是，这是模型的错 |
| **人工拒绝审批** | **否**：不是工具失败，把它算进去会在用户拒绝几次后触发“工具一直失败”的通知，误导模型 |

### 2.3 崩溃与取消

- 取消发生在派发期间：`CancelledError` 向上传播，状态停在标记那次提交，由 `mark_cancelled` 或恢复补齐配对。
- 标记是恢复所读的字段：幂等工具重试，其余合成“执行结果未知”（`checkpoint.recover_interrupted_dispatch`，已用真实阶段产出的状态测试）。

## 3. 工具层（`tools.core`）与协议（`agent_runtime.core`）

- 协议在 `core`：`AgentTool`（`name`、`description`、`parameters`、`timeout_sec`、`trust`、`idempotent`、`invoke`）、`ToolResult`（冻结的 pydantic 模型，`failure()` 与 `failed`）。
- `ToolRegistry`：构造期校验空名、重名（不覆盖）、不可信工具（默认拒绝；`allow_untrusted` 时若给了逐名清单，清单外也拒绝）。`schemas()` **按名称排序**，保证请求前缀稳定；`is_idempotent` 对未知工具返回假。
- `ToolDispatcher`：`asyncio.gather` 并发，返回顺序与输入一致；每个调用独立硬超时；超时、异常、未知工具都变成失败结果；信任级 = 结果的调用期覆盖 > 工具的静态声明 > 未知工具取 `untrusted`；`CancelledError` 不吞。

## 4. 护栏与上下文模块

| 模块 | 内容 |
|---|---|
| `guardrails.permissions` | `PermissionPolicy`（配置快照）、`check_permission`（纯函数）、`action_signature` |
| `guardrails.loops` | `compute_fingerprint`、`register_fingerprints`、`is_fingerprint_loop`、`update_consecutive_errors`、`build_replan_notice`、`LoopLimits` |
| `context.observation` | `ObservationLimits`、`ObservationGovernor`、`ObservationStore` |

与旧版相比的取舍：

- 签名与指纹用 sha256（旧版 md5），长度不变。
- 非法的权限正则被跳过并记录，不影响其余规则；已知防不住的命令（解释器间接执行、脚本、`eval`）有测试固定“当前被判为只读”，防止被误当作已覆盖。
- 观察值裁剪：行数不超过 `head + tail` 时文档原说“原样返回”，但几行巨长的输出（如 base64）会因此整条放行；实现里再按长度封顶每行（至少 80 字符），省略处写明。句柄在同一步内重名时加 `_2`、`_3`；工具名与任务 id 净化后才进文件路径，不能逃出产物目录；落盘失败只留摘要。

## 5. 实现状态与未做

已落地：executor、tool_runner、`tools.core`（协议在 core）、权限判定、指纹与连续错误、观察值裁剪与落盘、配置装配（`permission_policy_from_settings`、`loop_limits_from_settings`、`observation_limits_from_settings`、`APPROVAL_HOOKS`）。

**未做：**

- **内置工具**已补上（见 [内置工具 §11](../04_tools/02_builtin_tools.md)），`read_artifact` 使被裁剪的部分可取回。`ToolRunnerDeps` 因此多了两处：`bind`（有调用要处理时先把状态推给工具侧的任务绑定对象，发生在任何提交与执行之前）；以及工具自带的产物（`artifact_id` + `artifact_path`）登记进 `state.artifacts`，重名加后缀、被截断时在观察值末尾追加 `[完整输出: artifact://…]`。
- **子智能体预算账本**的父级同步与派发后冲销：等子智能体。
- **信封实现**（[信任 §4](../05_security/03_trust_and_injection.md)）：`envelope` 钩子已在，真实的信封函数没做；目前注册表默认拒绝不可信工具，所以尚无不可信观察值可包。
- **注入扫描**、**轨迹记录**（审批决定、拦截、通知现在只发 `guard.*` 事件）。
- 工具参数的 JSON Schema 校验（[工具系统 §14](../04_tools/01_tool_system.md)）。
- 审批的 `check_binding` 只绑定 call id 与步数，**不绑定参数**：状态里的 `tool_calls` 不会被重新生成，所以正常路径下被批准的就是被执行的；但检查点库被篡改时，同一 id 换参数会沿用旧批准。是否把参数哈希也纳入 `approval_id`，待定。
