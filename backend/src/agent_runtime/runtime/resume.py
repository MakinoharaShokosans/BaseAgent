"""resume_task：从检查点续跑。设计文档：docs/02_runtime/03_checkpoint_and_resume.md §4。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from agent_runtime.approval import bind_decision, parse_decision
from agent_runtime.approval.errors import NotWaitingForApprovalError
from agent_runtime.checkpoint import CheckpointStore, recover_interrupted_dispatch
from agent_runtime.core import DONE, AgentState, ApprovalDecision, apply_update
from agent_runtime.loop import TaskOutcome, TaskRuntime, drive
from agent_runtime.runtime.active import ActiveTasks
from agent_runtime.runtime.errors import (
    ApprovalRequiredFirstError,
    TaskAlreadyRunningError,
    TaskNotFoundError,
    TaskNotResumableError,
)


@dataclass(frozen=True, slots=True)
class ResumeDeps:
    checkpoints: CheckpointStore
    #: 重建任务级运行时（预算守卫、裁剪器、账本、事件总线等都不入检查点，每次续跑重建）
    build_runtime: Callable[[AgentState], Awaitable[TaskRuntime]]
    #: 工具名 -> 重复执行是否安全。未知工具应返回 False
    is_idempotent: Callable[[str], bool]
    active: ActiveTasks


async def resume_task(
    deps: ResumeDeps,
    task_id: str,
    decision: ApprovalDecision | Mapping[str, object] | None = None,
) -> TaskOutcome:
    """从最近一次提交的检查点续跑。

    - 已成功完成的任务不可续跑；因预算熔断（``should_terminate``）而终止的任务回到 planner，
      清除终止标记，``step_count`` 与 ``total_tokens`` 沿用，所以可能立刻再次熔断。
    - 挂起在审批上的任务必须带决策，且决策绑定校验通过；校验失败时任务保持挂起、原样不动。
    - 决策绑定后、阶段运行前先落盘，之后即使崩溃，用户的批准也不会丢。
    - 派发期间崩溃的任务先做恢复，结果同样先落盘。
    - 同一任务已有运行实例时直接拒绝。
    """
    if not deps.active.try_acquire(task_id):
        raise TaskAlreadyRunningError(f"任务 {task_id} 已在运行")
    try:
        state = await prepare_resume(deps, task_id, decision)
        runtime = await deps.build_runtime(state)
        return await drive(runtime, state)
    finally:
        deps.active.release(task_id)


async def prepare_resume(
    deps: ResumeDeps,
    task_id: str,
    decision: ApprovalDecision | Mapping[str, object] | None = None,
) -> AgentState:
    """续跑的前半段：加载、校验、绑定决策、恢复派发中断，有变化就先提交。

    不占运行位、不运行阶段。任务运行器用它在**调用方的请求里**同步暴露校验错误，
    再把后半段（重建运行时并 ``drive``）放到后台。
    """
    loaded = await deps.checkpoints.load(task_id)
    if loaded is None:
        raise TaskNotFoundError(f"没有任务 {task_id} 的检查点")
    state = _prepare(loaded, decision, deps.is_idempotent)
    if state != loaded:
        await deps.checkpoints.commit(state)
    return state


def _prepare(
    state: AgentState,
    decision: ApprovalDecision | Mapping[str, object] | None,
    is_idempotent: Callable[[str], bool],
) -> AgentState:
    if state.phase == DONE:
        if not state.should_terminate:
            raise TaskNotResumableError(f"任务 {state.task_id} 已成功完成，不可续跑")
        if decision is not None:
            raise NotWaitingForApprovalError("任务当前不在等待审批")
        return apply_update(
            state, {"phase": "planner", "should_terminate": False, "termination_reason": ""}
        )

    if decision is not None:
        parsed = decision if isinstance(decision, ApprovalDecision) else parse_decision(decision)
        state = bind_decision(state, parsed)
    elif state.pending_approval is not None and state.approval_decision is None:
        raise ApprovalRequiredFirstError(f"任务 {state.task_id} 正在等待审批，续跑必须带决策")

    return recover_interrupted_dispatch(state, is_idempotent).state
