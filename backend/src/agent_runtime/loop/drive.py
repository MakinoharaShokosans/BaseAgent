"""drive：主循环。设计文档：docs/02_runtime/02_main_loop.md §3。"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal

from loguru import logger

from agent_runtime.core import (
    DONE,
    AgentState,
    ApprovalRequired,
    InvalidUpdateError,
    PendingApproval,
    apply_update,
    check_invariants,
)
from agent_runtime.loop.routes import next_phase
from agent_runtime.loop.runtime import TaskRuntime

OutcomeStatus = Literal["succeeded", "terminated", "waiting_for_approval"]


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """``drive`` 的返回。

    ``failed`` 与 ``cancelled`` 不在此：它们以异常形式传出，由任务运行器处理。
    """

    state: AgentState
    approval_request: PendingApproval | None = None

    @property
    def status(self) -> OutcomeStatus:
        if self.approval_request is not None:
            return "waiting_for_approval"
        return "terminated" if self.state.should_terminate else "succeeded"


async def drive(rt: TaskRuntime, state: AgentState) -> TaskOutcome:
    """从 ``state.phase`` 起运行到终止或挂起。

    1. 先提交检查点，后发事件：客户端看到的进度不会比可恢复的进度更远。
    2. ``ApprovalRequired`` 不是错误：该阶段的局部结果一概不应用，记录待审批请求、提交、返回。
    3. 其他异常（含取消）向上传播，状态停留在上一次提交点。
    4. 事件发送失败不影响任务：状态已经落盘。
    """
    while state.phase != DONE:
        phase = state.phase
        try:
            update = await rt.stages[phase](state)
        except ApprovalRequired as request:
            state = apply_update(state, {"pending_approval": request.payload, "phase": phase})
            check_invariants(state)
            await rt.checkpoints.commit(state)
            await _notify(rt.events.approval_requested(request.payload, state))
            return TaskOutcome(state, request.payload)

        if "phase" in update:
            raise InvalidUpdateError("phase：阶段函数不得写入，由主循环路由决定")
        state = apply_update(state, update)
        state = apply_update(state, {"phase": next_phase(phase, state)})
        check_invariants(state)

        await rt.checkpoints.commit(state)  # 提交点
        await _notify(rt.events.stage_committed(phase, update, state))
    return TaskOutcome(state)


async def _notify(event: Awaitable[None]) -> None:
    try:
        await event
    except Exception:
        logger.exception("事件发送失败；检查点已提交，任务继续")
