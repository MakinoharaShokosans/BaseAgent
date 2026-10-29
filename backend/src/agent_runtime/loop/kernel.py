"""循环内核：调模型；有工具调用就执行并把结果追加进 transcript，再调模型；没有则结束。

设计文档：docs/02_runtime/02_main_loop.md。内核不含预算、阶段、路由或验收；一切护栏与记录
都挂在固定接缝上（模型接缝、工具包装层、Recorder）。

回合有两个提交点：模型回复追加之后、工具结果追加之后。先提交，后发事件：客户端看到的
进度不会比可恢复的进度更远。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from loguru import logger

from agent_runtime.context.task_compaction import Compaction, CompactStep
from agent_runtime.core import (
    AgentState,
    ApprovalRequired,
    Message,
    PendingApproval,
    ToolCall,
    apply_update,
    check_invariants,
    unanswered_calls,
)
from agent_runtime.llm import LLMBadRequest
from agent_runtime.loop.errors import CanaryLeak, LoopAborted
from agent_runtime.loop.model import ModelStep

OutcomeStatus = Literal["succeeded", "terminated", "waiting_for_approval"]


#: 工具包装层：状态与本批调用进，状态增量出。挂起时抛 ``ApprovalRequired``
ToolStep = Callable[[AgentState, tuple[ToolCall, ...]], Awaitable[Mapping[str, object]]]


class CommitSink(Protocol):
    """检查点存储对循环的接口。``agent_runtime.checkpoint.CheckpointStore`` 满足它。"""

    async def commit(self, state: AgentState) -> int: ...


class EventSink(Protocol):
    """循环向外发出的事件。只在对应的检查点提交成功之后调用。"""

    async def model_replied(self, message: Message, state: AgentState) -> None: ...

    async def turn_finished(self, update: Mapping[str, object], state: AgentState) -> None: ...

    async def approval_requested(self, request: PendingApproval, state: AgentState) -> None: ...

    async def context_compacted(self, result: Compaction, state: AgentState) -> None: ...


@dataclass(frozen=True, slots=True)
class TaskRuntime:
    model: ModelStep
    tools: ToolStep
    checkpoints: CommitSink
    events: EventSink
    #: 连续被拦截的批次数达到它即 ``LoopAborted``；0 不检查
    max_consecutive_blocks: int = 5
    #: 任务内压缩：每次调模型前问一次要不要压；窗口溢出时强制压一次再重试。None 表示不压缩
    compact: CompactStep | None = None


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """``run`` 的返回。

    ``failed`` 与 ``cancelled`` 不在此：它们以异常形式传出，由任务运行器处理。
    """

    state: AgentState
    approval_request: PendingApproval | None = None
    #: 非空表示安全终止（金丝雀泄露），不可续跑
    termination_reason: str = ""

    @property
    def status(self) -> OutcomeStatus:
        if self.approval_request is not None:
            return "waiting_for_approval"
        return "terminated" if self.termination_reason else "succeeded"


async def run(rt: TaskRuntime, state: AgentState) -> TaskOutcome:
    """从检查点尾部起运行到结束、挂起或异常。

    - 尾部有未配对的调用（续跑，或挂起后的审批）：先执行它们，再调模型。
    - ``ApprovalRequired`` 不是错误：本批的局部结果一概不应用，记录待审批请求、提交、返回。
    - 其他异常（含取消、模型错误、``LoopAborted``）向上传播，状态停留在上一次提交点。
    - 事件发送失败不影响任务：状态已经落盘。
    """
    pending = unanswered_calls(state.messages)
    recovered = False  # 本次溢出恢复过了吗：恢复后再溢出就不再试，原样失败
    while True:
        if not pending:
            state = await _compacted(rt, state, force=False)
            try:
                reply = await rt.model(state)
            except CanaryLeak as leak:
                return TaskOutcome(state, termination_reason=leak.reason)
            except LLMBadRequest as overflow:
                if not overflow.context_overflow or recovered or rt.compact is None:
                    raise
                recovered = True
                shrunk = await _compacted(rt, state, force=True)
                if shrunk is state:
                    raise  # 已经没有可压的了
                state = shrunk
                continue
            recovered = False
            state = apply_update(state, {"messages": [reply]})
            check_invariants(state)
            await rt.checkpoints.commit(state)
            await _notify(rt.events.model_replied(reply, state))
            if not reply.tool_calls:
                return TaskOutcome(state)
            pending = reply.tool_calls

        try:
            update = await rt.tools(state, pending)
        except ApprovalRequired as request:
            state = apply_update(state, {"pending_approval": request.payload})
            check_invariants(state)
            await rt.checkpoints.commit(state)
            await _notify(rt.events.approval_requested(request.payload, state))
            return TaskOutcome(state, request.payload)

        state = apply_update(state, update)
        check_invariants(state)
        await rt.checkpoints.commit(state)
        await _notify(rt.events.turn_finished(update, state))
        pending = ()
        if rt.max_consecutive_blocks and state.consecutive_blocks >= rt.max_consecutive_blocks:
            raise LoopAborted(
                f"同一批调用连续 {state.consecutive_blocks} 次被重复拦截，模型在原地打转"
            )


async def _compacted(rt: TaskRuntime, state: AgentState, *, force: bool) -> AgentState:
    """需要时做一次任务内压缩并提交；没有压缩就原样返回同一个状态对象。压缩从不让任务失败。"""
    if rt.compact is None:
        return state
    try:
        result = await rt.compact(state, force)
    except Exception:
        logger.exception("任务内压缩出错；本回合不压缩，任务继续")
        return state
    if result is None:
        return state
    state = apply_update(state, result.update)
    check_invariants(state)
    await rt.checkpoints.commit(state)
    await _notify(rt.events.context_compacted(result, state))
    return state


async def _notify(event: Awaitable[None]) -> None:
    try:
        await event
    except Exception:
        logger.exception("事件发送失败；检查点已提交，任务继续")
