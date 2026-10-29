# 检查点与续跑

## 1. 目标与非目标

目标：

- 进程崩溃、断电后，任务能从最近一次已提交的回合边界继续。
- 模型错误（含余额不足）导致任务 `failed` 后，可由用户触发 `resume`。
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
    status         TEXT NOT NULL,         -- 'running' | 'waiting' | 'finished'
    state_json     TEXT NOT NULL,         -- 完整 AgentState，见 state_codec
    schema_version INTEGER NOT NULL,
    updated_at     REAL NOT NULL
);
```

- `status` 与 `state_json` 里对应内容冗余（`waiting` ⇔ `pending_approval` 非空；`finished` ⇔ 任务已结束），列上保留是为了启动时不解码即可扫描出未完成任务（损坏的任务也能被列出）。它们在同一事务里写入；`load` 时核对 `task_id`、`status`、`schema_version` 与状态一致，不一致报 `CheckpointCorruptError`。
- 连接参数：`journal_mode=WAL`，`busy_timeout=5000`，`synchronous=FULL`（断电后已提交的检查点不能丢）。
- 库文件权限 0600，先以该权限建好空文件再让 SQLite 打开，WAL 的附属文件沿用它。
- 库结构的版本记在 `PRAGMA user_version`，迁移脚本按版本顺序执行；库版本高于程序支持的拒绝打开。
- 单行覆盖写，不追加历史。检查点体积由状态大小决定；对话过长时由上下文治理压缩装配视图，检查点里的 transcript 不瘦身。

## 3. 提交

```python
class CheckpointStore:
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def commit(self, state: AgentState, status: Status) -> int: ...   # 单事务 UPSERT，返回新的 seq
    async def load(self, task_id: str) -> AgentState | None: ...
    async def delete(self, task_id: str) -> bool: ...
    async def list_unfinished(self) -> list[UnfinishedTask]: ...   # task_id、status
```

提交点（由 Recorder 在循环的 `checkpoint()` 端口处执行，见 [主循环 §2](02_main_loop.md)）：

1. **派发前**：模型回复带工具调用，追加之后提交一次。
2. **回合结束**：工具结果追加之后提交一次。
3. **终回合**：模型回复不带工具调用，追加之后提交一次。
4. **挂起**：Supervisor 捕获 `ApprovalRequired`，写入 `pending_approval` 后提交，`status = 'waiting'`。
5. **终态**：任务结束（成功、失败、取消、安全终止）后提交，`status = 'finished'`。

规则：

- `commit` 失败（磁盘满、数据库被锁超时）视为任务失败，不允许"带着未持久化的进度继续跑"。
- `commit` 先调用 `check_invariants`：原子对等不变量不满足的状态不落盘，上一份检查点保持不变。
- 提交使用 `INSERT … ON CONFLICT(task_id) DO UPDATE … RETURNING seq`，`seq` 在 SQL 内自增，调用方不传序号。同一连接上的写操作由锁串行化；两个连接同时写同一任务也得到唯一递增的序号。

## 4. 加载与续跑

```python
async def resume_task(deps, task_id, decision=None):
    state = await deps.checkpoints.load(task_id)
    if state is None:
        raise TaskNotFoundError(task_id)
    state = check_resumable(state)                  # 已成功或已取消、安全终止的拒绝
    if decision is not None:
        state = bind_decision(state, decision)      # 校验后写入 approval_decision
    elif state.pending_approval is not None:
        raise ApprovalRequiredFirstError(task_id)
    state = repair_tail(state, deps.is_idempotent)  # 见 §5；挂起中的不处理
    await deps.checkpoints.commit(state, ...)       # 先落盘再运行
    rt = await prepare_task(deps, state)            # 重建任务级对象
    return await supervise(rt, state)
```

顺序与失败语义：

1. 登记运行占位，已有同一任务的实例则拒绝（`TaskAlreadyRunningError`）；无论成功、失败、被取消、被拒绝，占位都在 `finally` 里释放。
2. 加载检查点；不存在 `TaskNotFoundError`；损坏 `CheckpointCorruptError`。
3. **可续跑判定**：`succeeded`、`cancelled`、`terminated`（安全终止）不可续跑，报 `TaskNotResumableError`；`failed` 与挂起可以。
4. **决策**：给了决策则严格解析（`approved` 必须是真布尔值，`scope` 缺省 `once`，未知键拒绝）并绑定（任务须在等待审批、`approval_id` 一致、没有未消费的旧决策）；没给决策而任务挂起且尚无已绑定的决策 → `ApprovalRequiredFirstError`；任务没有挂起却给了决策 → `NotWaitingForApprovalError`。任一失败，任务原样保持挂起，不写库。
5. 补齐尾部（§5）。
6. 若状态与加载时不同，**先提交**再运行：决策绑定后、补齐后立刻落盘，之后崩溃也不丢用户的批准。已绑定决策的任务再次续跑可以不带决策。
7. 重建任务级对象，包住 `run`。

续跑要点：

- **任务级对象全部重建**：裁剪器、记录器、上下文装配器、工具注册表、事件总线。它们不入检查点。
- **没有预算要对齐。** `failed` 任务（包括余额不足）续跑时直接再调模型，不需要额外清零或放宽任何上限。
- **会话级审批白名单**（"总是允许"）由任务注册表持有，跨 `resume` 存活，不入检查点。进程重启后白名单丢失，下一次相同操作会重新询问，这是偏安全的默认。
- 挂起中的任务续跑：工具包装层从检查点重新进入同一批调用，**重新做同一次纯判定**，得到相同的越级集合后消费决策。

## 5. 崩溃恢复语义

检查点位于回合内的三个点（§3），崩溃落在两点之间时：

| 崩溃位置 | 检查点尾部 | 重做后果 | 处理 |
|---|---|---|---|
| 模型调用中 | 上一回合的工具结果 | 重新调用模型，可能得到不同回复 | 可接受；Token 不重复计费的问题不存在（用量按网关事件计，不进状态） |
| 派发前提交之后、结果提交之前 | assistant 带未配对的 `tool_calls` | 工具可能已部分或全部执行 | 补齐，见下 |
| 回合结束提交之后 | 完整配对 | 无 | 直接调模型 |

**补齐（`repair_tail`，纯函数，幂等）**：尾部存在未配对调用且不是挂起时，对该批调用：

| 情形 | 处理 |
|---|---|
| 全部声明 `idempotent` | 丢弃这条 assistant 消息，等同未发生，重新调模型 |
| 其余 | 为**每个**调用合成 tool 消息："执行结果未知：任务在执行期间中断，工具可能已部分或全部生效，请先核验当前状态再决定是否重试"；然后继续调模型 |

整批一起处理，不留一半重放一半合成的混合状态。未知工具（如第三方 MCP）按非幂等。无法区分"已派发"与"未派发"，所以一律按"可能已执行"处理，代价是个别从未执行的调用会被标为结果未知，由模型核验即可。

这把"至少一次"收敛成"至多一次加显式的未知结果"，由模型基于文件系统的真实状态判断，而不是盲目重放。

## 6. 启动时的扫描

进程启动时调用 `list_unfinished()`：

- `status = 'running'`：说明上次进程在任务运行中退出。任务**不自动续跑**，在注册表里标记为 `failed`，原因"进程中断"，等待用户显式 `resume`。自动续跑可能在用户不知情时再次执行命令，不符合最小惊讶原则。
- `status = 'waiting'`：恢复为 `waiting_for_approval`，前端重新连上后可继续审批。

## 7. 并发与隔离

- 单机默认 `max_concurrent_tasks = 1`，同一 `task_id` 同一时刻只有一个 `run` 在运行，由任务注册表保证。
- 检查点库只被运行时进程写入，sidecar 不访问。
- 同一任务的 `resume` 在已有运行实例时直接拒绝，不排队。

## 8. 实现要点

- `CheckpointStore.commit(state, *, outcome="", reason="")` 单事务执行 UPSERT；`outcome` 非空时标记 `status = 'finished'`，否则按是否挂起写 `waiting` 或 `running`。
- 表结构包含 `outcome` 与 `reason` 列记录终态原因，读取通过 `load_status`。
- 未配对调用的尾部修复在 `checkpoint/recovery.py`，`mcp_call` 按目标工具判断幂等。
- `resume_task` 对已处于 `finished` 状态的任务按 `outcome` 抛出 `TaskNotResumableError`。

