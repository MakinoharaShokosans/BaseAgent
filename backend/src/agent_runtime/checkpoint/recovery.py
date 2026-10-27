"""崩溃恢复与取消：补齐尾部未配对的工具调用。

回合内有两个提交点：模型回复带工具调用时先提交，工具结果追加后再提交。崩溃落在两者之间时，
检查点尾部是一条带未配对 ``tool_calls`` 的助手消息，工具可能已部分或全部执行。无法区分
“已派发”与“未派发”，所以一律按“可能已执行”处理：全部幂等则丢弃这条消息重新来过，
否则为每个调用合成“执行结果未知”，交给模型核验。把“至少一次”收敛成“至多一次加显式的未知结果”。

设计文档：docs/02_runtime/03_checkpoint_and_resume.md §5。纯函数，不做 I/O。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from agent_runtime.core import AgentState, Message, ToolCall, apply_update, unanswered_calls

RecoveryAction = Literal["none", "retry", "synthesized"]

UNKNOWN_RESULT: Final = (
    "执行结果未知：任务在执行期间中断，工具可能已部分或全部生效，请先核验当前状态再决定是否重试"
)
CANCELLED_REASON: Final = "用户取消"
NOT_EXECUTED_CANCELLED: Final = "已取消：任务被用户取消，本调用没有执行"


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    state: AgentState
    action: RecoveryAction
    #: 获得合成消息的调用 id（action 为 synthesized 时是该批的全部调用）
    synthesized_ids: tuple[str, ...] = ()


def repair_tail(state: AgentState, is_idempotent: Callable[[str], bool]) -> RecoveryResult:
    """补齐尾部未配对的调用。挂起在审批上的任务不处理（工具包装层会重新进入同一批）。

    整批一起处理，不留一半重放一半合成的混合状态。幂等性按调用的**目标工具**判断。
    """
    if state.pending_approval is not None:
        return RecoveryResult(state, "none")
    pending = unanswered_calls(state.messages)
    if not pending:
        return RecoveryResult(state, "none")

    last = _last_assistant_index(state)
    if all(is_idempotent(_target(c)) for c in pending) and not _has_answers(state, last):
        # 丢弃这条助手消息，等同未发生，重新调模型
        return RecoveryResult(apply_update_messages(state, state.messages[:last]), "retry")

    replies = [
        Message(role="tool", content=UNKNOWN_RESULT, tool_call_id=c.id, name=c.name)
        for c in pending
    ]
    return RecoveryResult(
        apply_update(state, {"messages": replies}), "synthesized", tuple(c.id for c in pending)
    )


def mark_cancelled(state: AgentState) -> AgentState:
    """取消：为未完成的调用补“已取消”结果以保持原子对；清掉待审批请求与已绑定决策。

    终态由检查点的 ``outcome`` 列表达，不在状态里。
    """
    replies = [
        Message(role="tool", content=NOT_EXECUTED_CANCELLED, tool_call_id=c.id, name=c.name)
        for c in unanswered_calls(state.messages)
    ]
    return apply_update(
        state, {"messages": replies, "pending_approval": None, "approval_decision": None}
    )


def apply_update_messages(state: AgentState, messages: tuple[Message, ...]) -> AgentState:
    """整体换掉 transcript（只用于丢弃未配对的尾部）。"""
    import dataclasses

    return dataclasses.replace(state, messages=tuple(messages))


def _target(call: ToolCall) -> str:
    """``mcp_call`` 是代理，幂等性看它转发的目标工具。"""
    if call.name == "mcp_call":
        name = call.args.get("name")
        if isinstance(name, str):
            return name
    return call.name


def _last_assistant_index(state: AgentState) -> int:
    for index in range(len(state.messages) - 1, -1, -1):
        if state.messages[index].role == "assistant":
            return index
    return 0


def _has_answers(state: AgentState, last: int) -> bool:
    """尾部那批调用里是否已有部分结果（有则不能整条丢弃）。"""
    return any(m.role == "tool" for m in state.messages[last + 1 :])
