# 检查点与续跑

## 1. 目标与非目标

目标：

- 进程崩溃、断电后，任务能从最近一次已提交的阶段边界继续。
- 预算熔断或失败后，可由用户触发 `resume`。
- 等待审批期间，任务不占用执行资源，进程重启也不丢失挂起状态。

非目标：

- 不做历史回放与时间旅行调试。轨迹审计由 `storage/traces/{task_id}.jsonl` 承担。
- 不保证工具副作用的恰好一次，见 §5。

## 2. 存储

SQLite（WAL 模式）和 `aiosqlite`，路径由配置 `storage.checkpoint_db` 给出（相对数据目录）。每个任务只保留**最新**一份检查点。

```sql
CREATE TABLE IF NOT EXISTS task_checkpoints (
    task_id        TEXT PRIMARY KEY,
    seq            INTEGER NOT NULL,      -- 提交序号，单调递增
    phase          TEXT NOT NULL,         -- 下一个要执行的阶段，或 'done'
    waiting        INTEGER NOT NULL DEFAULT 0,  -- pending_approval 是否非空
    state_json     TEXT NOT NULL,         -- 完整 AgentState，见 state_codec
    schema_version INTEGER NOT NULL,
    updated_at     REAL NOT NULL
);
```

- `phase`、`waiting` 与 `state_json` 里对应内容冗余，列上保留是为了启动时不解码即可扫描出未完成任务（损坏的任务也能被列出）。它们在同一事务里写入；`load` 时核对 `task_id`、`phase`、`schema_version` 三列与状态一致，不一致报 `CheckpointCorruptError`。
- 连接参数：`journal_mode=WAL`，`busy_timeout=5000`，`synchronous=FULL`（断电后已提交的检查点不能丢）。
- 库文件权限 0600，先以该权限建好空文件再让 SQLite 打开，WAL 的附属文件沿用它。
- 库结构的版本记在 `PRAGMA user_version`，迁移脚本按版本顺序执行；库版本高于程序支持的拒绝打开。
- 单行覆盖写，不追加历史。检查点体积由状态大小决定，不随步数增长；对话过长时由上下文治理压缩，而不是靠检查点瘦身。

## 3. 提交

```python
class CheckpointStore:
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def commit(self, state: AgentState) -> int: ...      # 单事务 UPSERT，返回新的 seq
    async def load(self, task_id: str) -> AgentState | None: ...
    async def delete(self, task_id: str) -> bool: ...
    async def list_unfinished(self) -> list[UnfinishedTask]: ...  # task_id、phase、waiting_for_approval
```

提交规则：

1. 每个阶段应用增量并计算出下一阶段之后提交一次（见 [主循环 §3](02_main_loop.md)）。
2. 挂起审批时提交一次，`pending_approval` 非空，`phase` 停在 `tool_runner`。
3. 任务到达 `DONE` 时提交终态，`phase = 'DONE'`。
4. `commit` 失败（磁盘满、数据库被锁超时）视为任务失败，不允许"带着未持久化的进度继续跑"。
5. `commit` 先调用 `check_invariants`：原子对等不变量不满足的状态不落盘，上一份检查点保持不变。

提交使用 `INSERT … ON CONFLICT(task_id) DO UPDATE … RETURNING seq`，`seq` 在 SQL 内自增，调用方不传序号，避免并发时序号冲突。同一连接上的写操作由锁串行化；两个连接同时写同一任务也得到唯一递增的序号（已测）。

## 4. 加载与续跑

> 以下是设计意图；已实现的 `resume_task`（`agent_runtime.runtime`）的精确行为见 §4.1。

```python
async def resume_task(deps, task_id, decision=None):
    state = await deps.checkpoints.load(task_id)
    if state is None:
        raise TaskNotFound(task_id)
    if decision is not None:
        state = bind_decision(state, decision)   # 校验后写入 approval_decision
    elif state["pending_approval"] is not None:
        raise ApprovalRequiredFirst(task_id)     # 有挂起却没给决策
    rt = await prepare_task(deps, state)          # 重建任务级运行时
    return await drive(rt, state)
```

续跑要点：

- **任务级对象全部重建**：预算守卫（挂钟起点重置）、裁剪器、记录器、上下文装配器、工具注册表、事件总线、子智能体账本。它们不入检查点。
- **账本以增量对齐**：`total_tokens` 已持久化，账本新建后通过 `sync_parent_usage(total_tokens)` 对齐父级已用量，子智能体消耗只以增量并入，因此重建不会重复计费。
- **会话级审批白名单**（"总是允许"）由任务注册表持有，跨 `resume` 存活，不入检查点。进程重启后白名单丢失，下一次相同操作会重新询问，这是偏安全的默认。
- **终态不可续跑**：`phase == 'DONE'` 且状态为 `succeeded` 时 `resume` 返回错误。`terminated` 与 `failed` 允许续跑，续跑后 `should_terminate` 在入口处清零，预算按重建的守卫重新计时，但 `step_count` 与 `total_tokens` 沿用，因此仍可能立刻再次熔断。是否需要在续跑时放宽上限，留作待定事项。

### 4.1 实现细节（已落地）

`resume_task(deps, task_id, decision=None)`，`deps` 含检查点库、重建任务级运行时的回调、`is_idempotent(工具名)`、`ActiveTasks`。顺序：

1. 登记运行占位，已有同一任务的实例则拒绝（`TaskAlreadyRunningError`）；无论成功、失败、被取消、被拒绝，占位都在 `finally` 里释放。
2. 加载检查点；不存在 `TaskNotFoundError`；损坏 `CheckpointCorruptError`。
3. 终态判定：`phase == done` 且 `should_terminate` 为假（成功完成）→ `TaskNotResumableError`；`should_terminate` 为真（预算熔断等）→ 回到 **planner**，清除终止标记与原因，`step_count`、`total_tokens` 沿用。此时给了决策 → 拒绝（不在等待审批）。
4. 决策：给了决策则严格解析（`approved` 必须是真布尔值，`scope` 缺省 `once`，未知键拒绝）并绑定（任务须在等待审批、`approval_id` 一致、没有未消费的旧决策）；没给决策而任务挂起且尚无已绑定的决策 → `ApprovalRequiredFirstError`；任务没有挂起却给了决策 → `NotWaitingForApprovalError`。任一失败，任务原样保持挂起，不写库。
5. 派发中断恢复（§5）。
6. 若状态与加载时不同，**先提交**再运行：决策绑定后、恢复合成后立刻落盘，之后崩溃也不丢用户的批准。已绑定决策的任务再次续跑可以不带决策。
7. 重建运行时（用恢复后的状态，账本据此对齐已用量），`drive`。

`tool_runner` 消费决策时用 `check_binding(state, 重新判定出的越级调用 id)` 核对：越级集合必须与请求的 `call_ids` 一致，且 `approval_id` 必须能由 `task_id`、`step_count`、`call_ids` 重新算出（`compute_approval_id`，sha256 取前 16 位十六进制）；被改写的请求不被信任。

## 5. 崩溃恢复语义

检查点位于阶段边界，崩溃落在阶段中间时，该阶段整体重做。各阶段的重做影响不同：

| 崩溃位置 | 重做后果 | 处理 |
|---|---|---|
| `planner` / `evaluator` / `executor` 的 LLM 调用中 | 重新调用模型，可能得到不同输出 | 可接受。Token 只在提交时计入，不会重复计数 |
| `budget_guard` | 纯判定，无副作用 | 无需处理 |
| `tool_runner` 派发之前 | 纯判定，无副作用 | 无需处理 |
| **`tool_runner` 派发期间或之后、提交之前** | 工具可能被执行两次，例如重复写文件、重复运行命令 | 见下 |

### 派发前标记

为避免静默重复执行有副作用的工具，`tool_runner` 在**开始派发之前**多提交一次轻量检查点，写入 `dispatch_started = <该批 tool_call id 列表>`。恢复时：

- 若 `phase == 'tool_runner'` 且 `dispatch_started` 非空，说明派发可能已部分发生。
- 此时**不重新执行**，而是为该批每个 `tool_call` 合成一条 `tool` 消息：`执行结果未知：任务在执行期间中断，工具可能已部分或全部生效，请先核验当前状态再决定是否重试`。随后清空标记并进入 `planner`。
- 只读工具（`view_file`、`load_skill` 等）可以直接重试，不必合成。可重试性由工具声明的 `idempotent` 属性决定。

这样把"至少一次"收敛成"至多一次加显式的未知结果"，由模型基于文件系统的真实状态判断，而不是盲目重放。

**实现（`checkpoint.recover_interrupted_dispatch`，纯函数）：**

| 情形 | 处理 |
|---|---|
| 无标记、不在 `tool_runner`、或正挂起在审批上 | 不处理 |
| 已派发的调用**全是幂等**的 | 清标记，阶段原样重做（`retry`），不合成任何消息 |
| 其余 | 给该批**每个**调用合成 tool 消息：已派发且非幂等的写“执行结果未知…请先核验当前状态再决定是否重试”，其余（未派发的、已派发但幂等的）写“未执行…如仍需要请重新发起”；清标记，转入 planner |

整批都合成，是为了不破坏原子对，也避免一半重放一半合成的混合状态。未知工具（如第三方 MCP）按非幂等处理。恢复本身是幂等的：对恢复后的状态再恢复一次，什么也不做。

`dispatch_started` 是控制字段，需加入 [状态模型](01_state_model.md) 的控制字段表。目前文档尚未列入，实现前需补齐。

## 6. 启动时的扫描

进程启动时调用 `list_unfinished()`：

- `phase` 非 `done` 且无 `pending_approval`：说明上次进程在任务运行中退出。任务**不自动续跑**，在注册表里标记为 `failed`，原因"进程中断"，等待用户显式 `resume`。自动续跑可能在用户不知情时再次执行命令，不符合最小惊讶原则。
- `pending_approval` 非空：恢复为 `waiting_for_approval`，前端重新连上后可继续审批。

## 7. 并发与隔离

- 单机默认 `max_concurrent_tasks = 1`，同一 `task_id` 同一时刻只有一个 `drive` 在运行，由任务注册表保证。
- 检查点库只被运行时进程写入，sidecar 不访问。
- 同一任务的 `resume` 在已有运行实例时直接拒绝，不排队。

## 8. 迁移步骤

1. 实现 `CheckpointStore` 与 `state_codec`，单测覆盖往返、版本拒绝、篡改拒绝。**（`CheckpointStore` 与启动扫描已完成，编解码见阶段 2。）**
2. 旧版 `aegis_state.db`（LangGraph 格式）**不迁移**。新版使用新的库文件名，旧文件保留不动，由用户自行清理。
3. 移除 `langgraph-checkpoint-sqlite` 依赖。
