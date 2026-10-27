"""崩溃恢复：补齐尾部未配对的调用。出处：docs/02_runtime/03_checkpoint_and_resume.md §5。"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_runtime.checkpoint import UNKNOWN_RESULT, mark_cancelled, repair_tail
from agent_runtime.core import (
    AgentState,
    Message,
    PendingApproval,
    ToolCall,
    apply_update,
    check_invariants,
    unanswered_calls,
)

MakeState = Callable[..., AgentState]

IDEMPOTENT = {"view_file", "grep", "mcp__docs__search"}


def is_idempotent(name: str) -> bool:
    return name in IDEMPOTENT


def calls(*specs: tuple[str, str]) -> Message:
    return Message(
        role="assistant",
        content="",
        tool_calls=tuple(ToolCall(id=i, name=n, args={}) for i, n in specs),
    )


def crashed(make_state: MakeState, assistant: Message) -> AgentState:
    """模型回复（带工具调用）已提交，工具结果还没有。"""
    return apply_update(make_state(), {"messages": [Message(role="user", content="go"), assistant]})


def tool_messages(state: AgentState) -> dict[str, Message]:
    return {m.tool_call_id: m for m in state.messages if m.role == "tool" and m.tool_call_id}


@pytest.mark.unit
def test_nothing_to_repair_when_the_tail_is_paired(make_state: MakeState) -> None:
    state = apply_update(
        make_state(),
        {
            "messages": [
                Message(role="user", content="go"),
                calls(("c1", "bash")),
                Message(role="tool", content="ok", tool_call_id="c1", name="bash"),
            ]
        },
    )
    result = repair_tail(state, is_idempotent)
    assert result.action == "none" and result.state is state


@pytest.mark.unit
def test_a_suspended_task_is_left_alone(make_state: MakeState) -> None:
    pending = PendingApproval(
        approval_id="a",
        call_ids=("c1",),
        required_level="full_permissions",
        current_level="workspace_write",
        action_type="network",
        command="curl",
        reason="r",
        escalation_count=1,
    )
    state = apply_update(crashed(make_state, calls(("c1", "bash"))), {"pending_approval": pending})
    assert repair_tail(state, is_idempotent).action == "none"


@pytest.mark.unit
def test_all_idempotent_means_drop_the_reply_and_ask_again(make_state: MakeState) -> None:
    state = crashed(make_state, calls(("c1", "view_file"), ("c2", "grep")))
    result = repair_tail(state, is_idempotent)
    assert result.action == "retry"
    assert [m.role for m in result.state.messages] == ["user"]


@pytest.mark.unit
def test_one_non_idempotent_call_synthesizes_unknown_for_the_whole_batch(
    make_state: MakeState,
) -> None:
    state = crashed(make_state, calls(("c1", "view_file"), ("c2", "bash")))
    result = repair_tail(state, is_idempotent)
    assert result.action == "synthesized"
    replies = tool_messages(result.state)
    assert set(replies) == {"c1", "c2"}
    assert all(m.content == UNKNOWN_RESULT for m in replies.values())
    assert result.synthesized_ids == ("c1", "c2")
    check_invariants(result.state)


@pytest.mark.unit
def test_unknown_tools_are_treated_as_non_idempotent(make_state: MakeState) -> None:
    state = crashed(make_state, calls(("c1", "mcp__github__create_issue")))
    assert repair_tail(state, is_idempotent).action == "synthesized"


@pytest.mark.unit
def test_the_mcp_proxy_is_judged_by_its_target(make_state: MakeState) -> None:
    def proxied(target: str) -> Message:
        call = ToolCall(id="c1", name="mcp_call", args={"name": target, "arguments": {}})
        return Message(role="assistant", content="", tool_calls=(call,))

    assert repair_tail(crashed(make_state, proxied("mcp__docs__search")), is_idempotent).action == (
        "retry"
    )
    assert repair_tail(crashed(make_state, proxied("mcp__x__write")), is_idempotent).action == (
        "synthesized"
    )


@pytest.mark.unit
def test_repair_is_idempotent_and_does_not_mutate_its_input(make_state: MakeState) -> None:
    state = crashed(make_state, calls(("c1", "bash")))
    before = state.messages
    once = repair_tail(state, is_idempotent).state
    assert state.messages == before
    assert repair_tail(once, is_idempotent).action == "none"


@pytest.mark.unit
@given(st.lists(st.tuples(st.sampled_from(["view_file", "grep", "bash", "edit_file"])), max_size=4))
def test_any_repaired_state_is_consistent(specs: list[tuple[str]]) -> None:
    from agent_runtime.core import new_state

    state = new_state(
        workspace_id="w",
        workspace_path="/p",
        session_id="s",
        task_id="t",
        task_goal="g",
        permission_level="read_only",
        canary_token="c",
    )
    batch = calls(*((f"c{i}", name) for i, (name,) in enumerate(specs)))
    state = apply_update(state, {"messages": [Message(role="user", content="go"), batch]})
    repaired = repair_tail(state, is_idempotent).state
    check_invariants(repaired)
    assert unanswered_calls(repaired.messages) == ()


# ==============================================================================
# 取消
# ==============================================================================


@pytest.mark.unit
def test_cancel_fills_in_the_missing_results_and_clears_pending(make_state: MakeState) -> None:
    state = crashed(make_state, calls(("c1", "bash"), ("c2", "bash")))
    cancelled = mark_cancelled(state)
    replies = tool_messages(cancelled)
    assert set(replies) == {"c1", "c2"}
    assert all("取消" in m.content for m in replies.values())
    assert cancelled.pending_approval is None and cancelled.approval_decision is None
    check_invariants(cancelled)


@pytest.mark.unit
def test_cancel_only_fills_in_the_missing_results(make_state: MakeState) -> None:
    done = Message(role="tool", content="real", tool_call_id="c1", name="bash")
    state = apply_update(
        crashed(make_state, calls(("c1", "bash"), ("c2", "bash"))), {"messages": [done]}
    )
    replies = tool_messages(mark_cancelled(state))
    assert replies["c1"].content == "real"
    assert "取消" in replies["c2"].content


@pytest.mark.unit
def test_cancel_with_a_paired_tail_changes_nothing(make_state: MakeState) -> None:
    state = apply_update(make_state(), {"messages": [Message(role="user", content="go")]})
    assert mark_cancelled(state).messages == state.messages
