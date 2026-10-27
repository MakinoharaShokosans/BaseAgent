# 状态模型

## 1. 消息类型

自定义不可变消息，取代 `langchain_core.messages`。

```python
Role = Literal["system", "user", "assistant", "tool"]

@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    args: Mapping[str, Any]

@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()   # 仅 assistant 使用
    tool_call_id: str | None = None         # 仅 tool 使用，指向被响应的 ToolCall.id
    name: str | None = None                 # 仅 tool 使用，工具名
    reasoning: str | None = None            # 仅 assistant 使用：明文思考内容，供展示与审计
    reasoning_state: Mapping | None = None  # 仅 assistant 使用：不透明载荷，仅用于回传，逐字节原样保存
    produced_by: str | None = None          # 仅 assistant 使用：产生该消息的端点，判断能否回传
```

后三个字段的含义、回传规则与安全约束见 [思考内容](../07_llm_gateway/03_reasoning_content.md)。它们不进入会话记忆、交付结论或其他模型的上下文。

与 OpenAI 兼容的对话格式一一对应，`LLMGateway` 转换时不再需要按类型分支。

### 原子对不变量

`assistant` 消息带 `tool_calls` 时，其后必须紧跟与每个 `ToolCall.id` 一一对应的 `tool` 消息，数量相等、不缺不多。否则模型端点会返回 400。

- 无论工具被批准、被拒绝、被循环检测拦截还是执行失败，都必须生成配对的 `tool` 消息。
- 检查点提交前调用 `assert_atomic_pairs(messages)`，违反即视为实现缺陷，不写入检查点。
- 例外：**最后一组**允许暂无配对（模型回复已提交、工具尚未完成，即回合进行中；或任务挂起在审批上）。续跑时按 [主循环 §7](02_main_loop.md) 补齐。

## 2. AgentState 字段

沿用现有字段，新增运行时控制字段。写入归属表保证同一字段只有一个主写入者。

| 分组 | 字段 | 说明 | 主写入者 |
|---|---|---|---|
| 归属 | `workspace_id` `workspace_path` `session_id` | 全生命周期不变 | 初始化 |
| 任务 | `task_id` `task_goal` | 同上 | 初始化 |
| 对话 | `messages` | 追加式 transcript | 循环 |
| 草稿 | `plan` `facts` `failed_attempts` | 任务内临时记忆，见 [计划与草稿](05_plan_and_scratch.md) | `update_plan` / `note` 工具 |
| 产物 | `artifacts` | `artifact_id -> 磁盘路径` | 工具后处理 |
| 验证 | `verification` | `none`/`unchecked`/`failed`/`passed`：最近一次修改文件之后命令的验证情况，来自工具结果而不是模型自述 | 工具后处理 |
| 防御 | `consecutive_errors` `fingerprint_history` `consecutive_blocks` | 瞬态护栏计数 | 工具包装层 |
| 安全 | `permission_level` `canary_token` | 任务基线权限与金丝雀 | 初始化 |
| 控制 | `pending_approval` | 待审批请求，`None` 表示未挂起 | Supervisor（捕获 `ApprovalRequired` 后写入） |
| 控制 | `approval_decision` | 已到达、尚未被消费的审批决策 | 续跑入口写入，工具包装层消费后清空 |
| 控制 | `schema_version` | 状态序列化版本（目标 3） | 序列化层 |

状态里**没有**：`phase`（没有阶段）、里程碑、步数与 Token 计数（没有预算）、终止标志（停止原因是 `run` 的出口，由 Supervisor 记录）、重规划与打回计数、派发前标记（尾部未配对调用即其信号）、会话继承来的摘要与记忆（只在提示词资产里，任务内只读）。

设计取舍：

- **用量与挂钟时间不入状态。** 没有预算，就没有需要持久化的额度计数；用量由网关计量，进入事件与轨迹，Recorder 汇总展示。
- **继承记忆不入状态。** 会话摘要、历史事实与失败尝试在任务启动时装进冻结的提示词资产（[提示词装配](../03_context/03_prompt_assembly.md)），任务内的笔记另存于 `facts` / `failed_attempts`，两者互不覆盖。

## 3. 更新语义

状态更新用**增量字典**表达，由 `apply_update(state, update)` 统一应用：

| 字段类型 | 语义 |
|---|---|
| `messages` | **追加**。增量里的消息按顺序接在末尾 |
| 其余列表与字典（`plan`、`facts`、`artifacts`、`fingerprint_history` 等） | **整体替换**。写入方需要给出完整的新值 |
| 标量 | 覆盖 |
| 未出现在增量里的字段 | 保持不变 |

`apply_update` 是纯函数，返回新状态对象，不原地修改。这让工具后处理等写入方可以放心读取传入的状态，而不用担心另一个写入者在期间改动它。

> 旧版 `add_messages` 还支持按 `id` 替换或删除消息。新版**不支持**：`Message` 没有 `id`，`state.messages` 只追加，压缩与折叠只发生在装配提示词时，不改写状态里的消息。（待核对项已按此定案；若阶段 5 的折叠实现发现必须改写，再回头扩展。）

### 3.1 实现细节（已落地于 `agent_runtime.core`）

- `AgentState` 是 `frozen` 的 dataclass，不是 TypedDict；序列类字段全是元组，`artifacts` 是字典且约定不原地修改。
- 初始化后不可更新的字段：`workspace_id`、`workspace_path`、`session_id`、`task_id`、`task_goal`、`permission_level`、`canary_token`、`schema_version`。更新它们直接报 `InvalidUpdateError`。
- `apply_update` 全有或全无：任一字段不合法则整份增量不生效；增量里的容器会被拷贝，之后对原对象的修改渗不进状态。未知字段、类型错误、负数计数器、非 `Message` 的消息都会被拒绝。
- `Message` 与 `ToolCall` 在构造时就校验角色约束（只有助手能带 `tool_calls` 与思考字段，只有 tool 消息带 `tool_call_id` 与 `name`），`args` 与 `reasoning_state` 深拷贝。
- `check_invariants(state)` 由 Recorder 在提交检查点前调用：原子对（最后一组放宽）、`plan` 条数与 `id` 唯一、笔记条数与长度上限。它**不**在 `apply_update` 里调用，因为一个回合内部可以暂时处于未配对状态。

## 4. 计划条目与失败尝试

`PlanItem` 与 `FailedAttempt` 仍为 pydantic 模型，是全工程唯一定义，`memory/` 等模块复用而不自持第二份。

```python
class PlanItem(BaseModel):
    id: int
    title: str
    status: Literal["pending", "in_progress", "completed"] = "pending"

class FailedAttempt(BaseModel):
    action: str
    failure_reason: str
    conclusion: str
```

## 5. 序列化

检查点里的状态是一份 JSON，由 `state_codec` 负责编解码，取代 `JsonPlusSerializer` 的 msgpack 与模块白名单。

- `Message` 与 `ToolCall` 手写 `to_dict` / `from_dict`，字段显式，不使用 `pickle` 或按类型名反射构造。`to_dict` 只写非默认字段，字段顺序固定；`reasoning_state` 不排序键，编码、解码、再编码字节相同。
- `PlanItem`、`FailedAttempt`、`PendingApproval`、`ApprovalDecision` 用 pydantic 的 `model_dump(mode="json")` 与严格 JSON 校验（JSON 数组可进元组字段，但 `"1"` 不会被悄悄当成整数）。
- 解码是**严格**的：未知 `role`、缺必需字段、未知字段、类型不符、`schema_version` 缺失或高于当前代码版本都直接报 `StateDecodeError`，不做猜测性修复。消息里可选字段（`reasoning` 等）缺失视为默认值，所以旧检查点仍可加载。解码只做结构校验，不检查原子对：该不变量由循环在写入前断言。
- `schema_version` 低于当前版本时，通过按版本注册的迁移函数逐级升级。v1、v2 是五阶段工作流的状态，与 v3 不兼容，**不迁移**：`MINIMUM_SCHEMA_VERSION = 3`，旧状态解码时报错；检查点库里的旧表改名为 `task_checkpoints_legacy`，只读保留。

安全上的好处：检查点内容不再能触发任意类型实例化，数据库文件被篡改的最坏后果是解码失败。

## 6. 与 ExecutionContext 的关系

旧版另有 `ExecutionContext`，是任务在内存里的运行视图，额外持有 `step_history`，任务结束时落盘轨迹后丢弃。本版保留这一职责划分，但不再单独定义一份近似重复的 TypedDict：

- 持久化与恢复只认 `AgentState`。
- 轨迹由 `TrajectoryRecorder` 逐条追加到 `storage/traces/{task_id}.jsonl`，不经过状态。
- 任务结束时的收尾（把结论写回会话记忆、触发压缩）由收尾函数完成，读取终态 `AgentState` 即可，不需要第二份状态。
