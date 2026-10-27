# 预算账本

## 1. 要解决的问题

工具层原本的隐含契约是"工具是叶子、无状态、便宜"。子智能体破坏了这三条：它内部要跑模型循环，会消耗**父任务看不见的 Token**。如果不显式补偿，物理预算的熔断就被绕过：子任务可以烧掉远超父任务上限的额度，而父任务的计量增量为零。

账本解决的是一个会计问题：**子智能体的消耗如何准入、记账、对账，使"父任务已用 + 子级消耗"始终不超过父任务上限。**

## 2. 两个对象，职责正交

| 对象 | 持有者 | 用途 |
|---|---|---|
| `ChildBudget` | 子智能体（被执行方） | **自我约束**：步数之外的 Token 与挂钟 |
| `BudgetLedger` | 父级（授权方） | **准入与结算**：唯一的记账权 |

### 2.1 ChildBudget

```python
ChildBudget(max_tokens, max_wall_time_sec)
  .add_tokens(n)    # 负数按 0 计，防止异常值反而"充值"
  .remaining        # 剩余额度
  .exhausted        # Token 或挂钟任一耗尽
  .reason()         # 耗尽原因，用于 warnings
```

`max_tokens` 来自账本**实际授予**的额度，不是配置里的名义上限。这是"切片"语义的落点：同一份配置，余额不同，授予额不同。研究子智能体与动态子智能体共用同一实现，避免每个子智能体各写一份预算算术，口径一旦分叉，对账就不可信。

### 2.2 BudgetLedger

```python
BudgetLedger(max_total_tokens, max_spawns=4, min_grant_tokens=1000)
  .sync_parent_usage(tokens)           # 推送父任务已用量
  .reserve(amount, label) -> (Reservation | None, reason)
  .settle(reservation, actual_tokens) -> refund
  .available / .consumed / .in_flight / .spawn_count / .snapshot()
```

## 3. 三条硬纪律

1. **账本归父级持有，被执行方只能报告，不能记账。** 子智能体的用量由框架从模型响应里直接读出，交给 `settle`。**绝不采信子智能体在输出文本里自述的用量**，那是可以被注入内容伪造的。
2. **先预留、后执行、再结算，而不是事后累加。** 事后累加在并发下不成立：父任务在子智能体返回之前不知道它花了多少，N 个并发子任务各自按上限开销，第一次对账时就已经超支。
3. **预留是原子的。** `reserve` 内部**不含任何 `await`**，在单线程事件循环里天然原子。一旦引入 `await`，并发派发就会超额分配。

## 4. 守恒式

```
父任务已用 + 子级已结算 + 在途预留 ≤ 父任务上限

available = max_total_tokens − parent_used − consumed − in_flight
```

## 5. 生命周期

```
sync_parent_usage(total_tokens)
        │
        ▼
reserve(amount, label)
  ├─ 派发次数 ≥ max_spawns            → 拒绝
  ├─ available < min_grant_tokens     → 拒绝
  ├─ granted = min(申请, available)
  ├─ granted < min_grant_tokens       → 拒绝
  └─ 通过：in_flight += granted；spawn_count += 1
        │
        ▼
子智能体运行（ChildBudget 自我约束）
        │
        ▼
settle(reservation, actual)       ← finally，所有退出路径
  ├─ in_flight −= 预留额
  ├─ consumed  += 实际消耗
  └─ 实际 > 预留？记警告，仍按实际记账（宁可多记，不可少记）
```

要点：

- **收窄而非拒绝**：余额只剩一半时，给一半比整体失败更有用，但仍受 `min_grant_tokens` 兜底。
- **`settle` 幂等**：重复调用无副作用，`Reservation.settled` 标记保证。
- **结算在父级代码里**：超时会取消子智能体所在的任务，所以调用点必须在未受取消保护的父级 `finally` 中，且该块不含 `await`，保证被取消时也能同步完成。
- **多退少补**：`refund = max(0, 预留 − 实际)`。

## 6. 与物理预算的衔接

账本**不写 `AgentState`**。衔接点在 `tool_runner`：

1. 派发前 `sync_parent_usage(state.total_tokens)`，记下 `consumed_before`。
2. 派发后 `child_tokens = ledger.consumed − consumed_before`。
3. 以**增量**并入：`total_tokens = base_tokens + child_tokens`。

用增量而非绝对值，是因为账本随任务重建，而 `total_tokens` 已被检查点持久化，增量语义在续跑后天然正确。这样就复用了既有的 `PhysicalBudgetGuard`，不必新增预算口径。冲销必须晚于审批闸门，见 [主循环 §5](../02_runtime/02_main_loop.md)。

## 7. 已发现的问题

| # | 问题 | 级别 |
|---|---|---|
| 1 | **研究子智能体完全绕过账本。** `delegate_research` 用自己的 `ChildBudget`，不预留、不结算，消耗也不并入 `total_tokens`；它没有单任务调用次数上限，配额不受 `max_spawns` 约束。这正是本文开头所说的"绕过熔断"，详见 [研究隔离区 §8](01_research_isolation.md) | **高** |
| 2 | **第二次派发起，子级消耗被重复扣减。** 第一次派发后，`tool_runner` 把 `child_tokens` 并入 `total_tokens`。下一次派发前 `sync_parent_usage` 又把这个已含子级消耗的 `total_tokens` 当作"父任务自己的用量"，而账本里的 `consumed` 也还保留着同一笔，于是 `available` 里这笔子级消耗被减了两次。结果是余额被系统性低估，后续派发更早被拒或被收窄 | 中 |
| 3 | **派发次数在续跑后重置。** `spawn_count` 只在账本里，账本随任务重建 | 低 |
| 4 | **没有账本快照的持久化。** `snapshot()` 只用于日志与事件，崩溃后无法对账在途预留 | 低 |

### 7.1 第 2 项的修复方案

让 `parent_used` 的口径明确为"**不含子级**的父任务用量"。在 `tool_runner` 推送时扣掉已并入的部分：

```python
ledger.sync_parent_usage(state_total_tokens - ledger.merged_into_state)
```

其中 `merged_into_state` 是账本记录的"已由 `tool_runner` 冲销进状态"的子级消耗。冲销完成时调用 `ledger.mark_merged(child_tokens)`。这样 `available` 的三项始终互斥。

更简单的做法是取消 `parent_used` 和 `consumed` 的分开记账：`available = max_total_tokens − state_total_tokens − in_flight`，因为一旦冲销进状态，`state_total_tokens` 已经含有子级的已结算部分。这样账本只需追踪**在途预留**。代价是在一次派发的并发批内，已结算的子级消耗要等冲销才体现在 `state_total_tokens` 里。同批内的并发预留靠 `in_flight` 覆盖，已结算部分靠本批内增量，处理起来更繁琐。因此倾向第一种。

## 8. 新版需要统一的事

1. 研究子智能体接入账本：派发前预留，`finally` 结算，消耗并入 `total_tokens`。同时给 `delegate_research` 加每任务调用次数上限（可复用 `max_spawns` 的思路，单独配置）。
2. 修复重复扣减，并加回归测试。
3. 任何新增的、内部会调用模型的工具，必须按同一模式接入，否则预算就会再出漏洞。可以在工具协议里加一个声明（例如 `consumes_tokens = True`），让注册表在装配时检查这类工具是否带了账本。
4. 把账本状态（`spawn_count`、已冲销额度）放入检查点，使续跑后保持一致。

## 9. 不变量

1. 任意时刻：`父任务已用 + 子级已结算 + 在途预留 ≤ 上限`。
2. 子级的消耗只能来自框架读取的真实用量，不信自述。
3. 预留原子，结算幂等，结算覆盖所有退出路径。
4. 账本不写状态，只通过 `tool_runner` 的增量冲销影响预算。
5. 任何会消耗模型额度的工具，都必须经账本准入。

## 10. 测试要点

- 守恒：多种申请额、余额、并发组合下，守恒式始终成立。
- 并发：同批多个预留，授予总量不超过余额。
- 收窄：余额小于申请额时授予余额；小于最小额度时拒绝。
- 次数：达到 `max_spawns` 后拒绝。
- 结算：成功、失败、超时被取消、异常四条路径都结算；重复结算无副作用；实际超出预留时按实际记账并告警。
- 非法输入：负数 Token、`None`、`0` 申请额。
- 对账：第一次派发后再派发，`available` 不被重复扣减（第 2 项回归）。
- 续跑：账本重建后与状态对齐，不重复计费。
- 研究接入：连续调用研究工具，父任务 `total_tokens` 随之增长，达到上限时熔断。

## 11. 待定

- 子级用量的计量精度：当前以模型端点返回的 `total_tokens` 为准，若端点不返回用量，需要退回本地分词估算，口径会偏差。
- 预算耗尽时对在途子智能体的处理：当前由子级自己的预算先咬合再收尾。是否需要父级强制取消。
- 是否给每次派发加成本归因（哪个角色花了多少），用于调优 `max_spawns` 和默认额度。

## 12. 实现状态

`BudgetLedger`、`ChildBudget`、`Reservation` 在 `agent_runtime.guardrails.ledger`（纯算术）；`ToolRunnerDeps.ledger` 接上后，`tool_runner` 在派发前 `sync_parent_usage(state.total_tokens)`、派发后把本批的增量并入 `total_tokens` 并 `mark_merged`。

- **§7 第 2 项（重复扣减）已修**，用的是 §7.1 的第一种方案：`parent_used = 状态里的 total_tokens − 已并入的子级消耗`，三项互斥；有回归测试（两次连续派发后 `available` 不被重复扣减）。
- **第 1 项（研究绕过账本）已修**，见 [研究隔离区 §12](01_research_isolation.md)。
- **守恒式**有 Hypothesis 性质测试；预留无 `await`；结算幂等；负数用量不会反向「充值」；实际超出预留时按实际记账并告警。
- **第 3、4 项未做**：账本不入检查点，续跑后重建（`spawn_count` 重置）。
