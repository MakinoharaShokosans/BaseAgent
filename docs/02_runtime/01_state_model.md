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
- 循环在每次提交检查点前调用 `assert_atomic_pairs(messages)`，违反即视为实现缺陷并终止任务，不写入检查点。
- 例外：最后一条带 `tool_calls` 的 `assistant` 允许暂无配对（其工具尚未执行），当且仅当任务处于"等待审批"（`pending_approval` 非空），或 `phase == "tool_runner"`（executor 刚提交、工具还没跑）。校验函数据此放宽最后一组。

## 2. AgentState 字段

沿用现有字段，新增运行时控制字段。写入归属表保证同一字段只有一个主写入者。

| 分组 | 字段 | 说明 | 主写入者 |
|---|---|---|---|
| 归属 | `workspace_id` `workspace_path` `session_id` | 全生命周期不变 | 初始化 |
| 任务 | `task_id` `task_goal` | 同上 | 初始化 |
| 规划 | `milestones` `current_milestone_idx` | 里程碑列表与当前指针 | planner；evaluator 改状态 |
| 上下文 | `messages` | 追加式对话 | planner / executor / tool_runner / budget_guard |
| 记忆 | `rolling_summary` `confirmed_facts` `failed_attempts` | 跨轮沉淀 | evaluator |
| 产物 | `artifacts` | `artifact_id -> 磁盘路径` | executor / tool_runner |
| 预算 | `step_count` `total_tokens` | 物理计量 | executor 加步数；各阶段加 Token |
| 防御 | `consecutive_errors` `fingerprint_history` | 瞬态护栏计数 | tool_runner |
| 安全 | `permission_level` `canary_token` | 任务基线权限与金丝雀 | 初始化 |
| 终止 | `should_terminate` `termination_reason` | 硬终止信号 | budget_guard / evaluator / 任一阶段在 LLM 不可用时 |
| **控制（新增）** | `phase` | 下一个要执行的阶段，检查点提交时写入 | 主循环 |
| **控制（新增）** | `pending_approval` | 待审批请求，`None` 表示未挂起 | tool_runner 抛出，主循环写入 |
| **控制（新增）** | `approval_decision` | 已到达、尚未被消费的审批决策 | 续跑入口写入，tool_runner 消费后清空 |
| **控制（新增）** | `dispatch_started` | 本批已开始派发的 `tool_call` id 列表，派发前提交，批次完成后清空，用于崩溃后识别"可能已部分执行"，见 [检查点](03_checkpoint_and_resume.md) §5 | tool_runner |
| **控制（新增）** | `schema_version` | 状态序列化版本 | 序列化层 |

两个设计取舍：

- **挂钟时间不入状态。** 它是 `PhysicalBudgetGuard` 实例的属性，进入状态会让回放时间不可复现。恢复后挂钟计时从恢复时刻重新开始，这是刻意的：否则进程重启本身就会耗尽时间预算。
- **`phase` 进入状态。** 旧版由 LangGraph 记录"下一个节点"，本版没有这层，必须自己存。放进状态而不是检查点表的独立列，是为了让状态本身足以说明"我停在哪"，便于测试和回放。

## 3. 更新语义

阶段函数返回**增量字典**，由 `apply_update(state, update)` 统一应用：

| 字段类型 | 语义 |
|---|---|
| `messages` | **追加**。增量里的消息按顺序接在末尾 |
| 其余列表与字典（`milestones`、`artifacts`、`fingerprint_history`、`confirmed_facts` 等） | **整体替换**。阶段需要返回完整的新值 |
| 标量 | 覆盖 |
| 未出现在增量里的字段 | 保持不变 |

`apply_update` 是纯函数，返回新状态对象，不原地修改。这让阶段函数可以放心读取传入的状态，而不用担心另一个写入者在期间改动它。

> 旧版 `add_messages` 还支持按 `id` 替换或删除消息。新版**不支持**：`Message` 没有 `id`，`state.messages` 只追加，压缩与折叠只发生在装配提示词时，不改写状态里的消息。（待核对项已按此定案；若阶段 5 的折叠实现发现必须改写，再回头扩展。）

### 3.1 实现细节（已落地于 `agent_runtime.core`）

- `AgentState` 是 `frozen` 的 dataclass，不是 TypedDict；序列类字段全是元组，`artifacts` 是字典且约定不原地修改。
- 初始化后不可更新的字段：`workspace_id`、`workspace_path`、`session_id`、`task_id`、`task_goal`、`permission_level`、`canary_token`、`schema_version`。更新它们直接报 `InvalidUpdateError`。
- `apply_update` 全有或全无：任一字段不合法则整份增量不生效；增量里的容器会被拷贝，之后对原对象的修改渗不进状态。未知字段、类型错误、负数计数器、非法阶段、非 `Message` 的消息都会被拒绝。
- `Message` 与 `ToolCall` 在构造时就校验角色约束（只有助手能带 `tool_calls` 与思考字段，只有 tool 消息带 `tool_call_id` 与 `name`），`args` 与 `reasoning_state` 深拷贝。
- `check_invariants(state)` 由循环在提交检查点前调用：原子对（`pending_approval` 非空时放宽最后一组）、`current_milestone_idx` 不超过里程碑数、`dispatch_started` 只含最近一次工具调用的 id。它**不**在 `apply_update` 里调用，因为一个阶段内部可以暂时处于未配对状态。
- 终止哨兵是字符串 `"done"`，`phase` 的合法值是五个阶段名加 `"done"`。

## 4. 里程碑与失败尝试

`Milestone` 与 `FailedAttempt` 仍为 pydantic 模型，是全工程唯一定义，`memory/` 等模块复用而不自持第二份。

```python
class Milestone(BaseModel):
    id: int
    title: str
    description: str = ""
    status: Literal["pending", "in_progress", "completed", "failed"] = "pending"

class FailedAttempt(BaseModel):
    action: str
    failure_reason: str
    conclusion: str
```

## 5. 序列化

检查点里的状态是一份 JSON，由 `state_codec` 负责编解码，取代 `JsonPlusSerializer` 的 msgpack 与模块白名单。

- `Message` 与 `ToolCall` 手写 `to_dict` / `from_dict`，字段显式，不使用 `pickle` 或按类型名反射构造。`to_dict` 只写非默认字段，字段顺序固定；`reasoning_state` 不排序键，编码、解码、再编码字节相同。
- `Milestone`、`FailedAttempt`、`PendingApproval`、`ApprovalDecision` 用 pydantic 的 `model_dump(mode="json")` 与严格 JSON 校验（JSON 数组可进元组字段，但 `"1"` 不会被悄悄当成整数）。
- 解码是**严格**的：未知 `role`、缺必需字段、未知字段、类型不符、`schema_version` 缺失或高于当前代码版本都直接报 `StateDecodeError`，不做猜测性修复。消息里可选字段（`reasoning` 等）缺失视为默认值，所以旧检查点仍可加载。解码只做结构校验，不检查原子对：该不变量由循环在写入前断言。
- `schema_version` 低于当前版本时，通过按版本注册的迁移函数逐级升级。第一版为 `1`，暂无迁移。

安全上的好处：检查点内容不再能触发任意类型实例化，数据库文件被篡改的最坏后果是解码失败。

## 6. 与 ExecutionContext 的关系

旧版另有 `ExecutionContext`，是任务在内存里的运行视图，额外持有 `step_history`，任务结束时落盘轨迹后丢弃。本版保留这一职责划分，但不再单独定义一份近似重复的 TypedDict：

- 持久化与恢复只认 `AgentState`。
- 轨迹由 `TrajectoryRecorder` 逐条追加到 `storage/traces/{task_id}.jsonl`，不经过状态。
- 任务结束时的收尾（把结论写回会话记忆、触发压缩）由收尾函数完成，读取终态 `AgentState` 即可，不需要第二份状态。
