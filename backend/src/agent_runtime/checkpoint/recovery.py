"""派发期间崩溃的恢复：把“至少一次”收敛成“至多一次加显式的未知结果”。

``tool_runner`` 在开始派发之前多提交一次带 ``dispatch_started`` 的检查点。恢复时若发现
该标记，说明派发可能已部分发生：对有副作用的工具不重放，而是合成“执行结果未知”的 tool
消息，交给模型基于文件系统的真实状态判断；全是幂等工具则直接重做该阶段。

设计文档：docs/02_runtime/03_checkpoint_and_resume.md §5。纯函数，不做 I/O。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from agent_runtime.core import (
    DONE,
    AgentState,
    InvalidStateError,
    Message,
    ToolCall,
    apply_update,
)

RecoveryAction = Literal["none", "retry", "synthesized"]

UNKNOWN_RESULT: Final = (
    "执行结果未知：任务在执行期间中断，工具可能已部分或全部生效，请先核验当前状态再决定是否重试"
)
NOT_EXECUTED: Final = "未执行：任务在批量派发期间中断，本调用没有产生结果；如仍需要，请重新发起"


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    state: AgentState
    action: RecoveryAction
    #: 获得合成消息的调用 id（action 为 synthesized 时是该批的全部调用）
    synthesized_ids: tuple[str, ...] = ()


def recover_interrupted_dispatch(
    state: AgentState, is_idempotent: Callable[[str], bool]
) -> RecoveryResult:
    """处理 ``phase == "tool_runner"`` 且 ``dispatch_started`` 非空的检查点。

    - 其余情形（含正挂起在审批上）原样返回，``action == "none"``。
    - 已派发的调用全是幂等的：清标记，阶段原样重做（``retry``）。
    - 否则：给该批**每个**调用合成 tool 消息（已派发且非幂等的写“结果未知”，其余写“未执行”），
      清标记并转入 planner（``synthesized``）。整批都合成，是为了不破坏原子对，也避免
      一半重放、一半合成的混合状态。
    """
    if (
        state.phase != "tool_runner"
        or not state.dispatch_started
        or state.pending_approval is not None
    ):
        return RecoveryResult(state, "none")

    batch = _last_batch(state)
    started = set(state.dispatch_started)
    if all(is_idempotent(call.name) for call in batch if call.id in started):
        return RecoveryResult(apply_update(state, {"dispatch_started": ()}), "retry")

    replies = [
        Message(
            role="tool",
            content=UNKNOWN_RESULT
            if call.id in started and not is_idempotent(call.name)
            else NOT_EXECUTED,
            tool_call_id=call.id,
            name=call.name,
        )
        for call in batch
    ]
    recovered = apply_update(
        state, {"messages": replies, "dispatch_started": (), "phase": "planner"}
    )
    return RecoveryResult(recovered, "synthesized", tuple(call.id for call in batch))


CANCELLED_REASON: Final = "用户取消"
NOT_EXECUTED_CANCELLED: Final = "未执行：任务已被用户取消，本调用没有执行"


def mark_cancelled(state: AgentState) -> AgentState:
    """把状态写成“用户取消”的终态。纯函数。

    取消可能落在任何位置：最后一组工具调用若还没有结果（刚提交、或正挂起在审批上），
    补上“未执行”的 tool 消息保持原子对；清掉待审批请求、已绑定决策和派发标记。
    终态用 ``should_terminate`` 表达，所以重启后不会被当成“进程中断”，也不会被当成
    “等待审批”。已经结束的状态原样返回。
    """
    if state.phase == DONE:
        return state
    replies: list[Message] = []
    last = next((m for m in reversed(state.messages) if m.role == "assistant"), None)
    if last is not None and last.tool_calls:
        index = len(state.messages) - 1 - state.messages[::-1].index(last)
        answered = {m.tool_call_id for m in state.messages[index + 1 :] if m.role == "tool"}
        replies = [
            Message(role="tool", content=NOT_EXECUTED_CANCELLED, tool_call_id=c.id, name=c.name)
            for c in last.tool_calls
            if c.id not in answered
        ]
    return apply_update(
        state,
        {
            "messages": replies,
            "pending_approval": None,
            "approval_decision": None,
            "dispatch_started": (),
            "should_terminate": True,
            "termination_reason": CANCELLED_REASON,
            "phase": DONE,
        },
    )


def _last_batch(state: AgentState) -> tuple[ToolCall, ...]:
    last = state.messages[-1] if state.messages else None
    if last is None or last.role != "assistant" or not last.tool_calls:
        raise InvalidStateError("dispatch_started 非空，但最后一条消息不是带工具调用的助手消息")
    return last.tool_calls
