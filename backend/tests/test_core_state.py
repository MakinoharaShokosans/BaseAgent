"""AgentState 与 apply_update 的更新语义。出处：docs/02_runtime/01_state_model.md §2-4。"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from agent_runtime.core import (
    MAX_FACT_CHARS,
    MAX_FACTS,
    MAX_PLAN_ITEMS,
    AgentState,
    AtomicPairError,
    FailedAttempt,
    InvalidStateError,
    InvalidUpdateError,
    Message,
    PendingApproval,
    PlanItem,
    ToolCall,
    apply_update,
    check_invariants,
    new_state,
    unanswered_calls,
)


def make_state(**overrides: Any) -> AgentState:
    base: dict[str, Any] = {
        "workspace_id": "w1",
        "workspace_path": "/work/proj",
        "session_id": "s1",
        "task_id": "t1",
        "task_goal": "fix the bug",
        "permission_level": "workspace_write",
        "canary_token": "canary-abc",
    }
    return new_state(**{**base, **overrides})


USER = Message(role="user", content="go")
CALL = ToolCall(id="c1", name="view_file", args={"path": "a.py"})
ASSISTANT = Message(role="assistant", content="", tool_calls=(CALL,))
TOOL = Message(role="tool", content="ok", tool_call_id="c1", name="view_file")


@pytest.mark.unit
def test_a_new_state_has_no_workflow_fields() -> None:
    names = {f.name for f in dataclasses.fields(AgentState)}
    assert not names & {"phase", "milestones", "step_count", "total_tokens", "should_terminate"}
    state = make_state()
    assert (state.plan, state.facts, state.failed_attempts) == ((), (), ())
    assert (state.consecutive_blocks, state.verification) == (0, "none")
    assert state.schema_version == 4
    assert (state.stream_summary, state.stream_cut) == ("", 0)


@pytest.mark.unit
def test_messages_append_and_everything_else_replaces() -> None:
    state = apply_update(make_state(), {"messages": [USER], "facts": ["a"]})
    state = apply_update(state, {"messages": [ASSISTANT], "facts": ["b"]})
    assert [m.role for m in state.messages] == ["user", "assistant"]
    assert state.facts == ("b",)


@pytest.mark.unit
def test_update_is_all_or_nothing_and_does_not_mutate() -> None:
    state = make_state()
    with pytest.raises(InvalidUpdateError):
        apply_update(state, {"facts": ["ok"], "consecutive_errors": -1})
    assert state.facts == ()


@pytest.mark.unit
@pytest.mark.parametrize("field", ["task_id", "task_goal", "permission_level", "canary_token"])
def test_fixed_fields_cannot_be_updated(field: str) -> None:
    with pytest.raises(InvalidUpdateError, match="不可更新"):
        apply_update(make_state(), {field: "x"})


@pytest.mark.unit
@pytest.mark.parametrize("field", ["phase", "milestones", "step_count", "dispatch_started"])
def test_removed_fields_are_unknown(field: str) -> None:
    with pytest.raises(InvalidUpdateError, match="未知"):
        apply_update(make_state(), {field: 1})


@pytest.mark.unit
def test_the_last_tool_call_group_may_be_unanswered() -> None:
    """回合进行中、或挂起在审批上：最后一组允许暂无配对。"""
    check_invariants(apply_update(make_state(), {"messages": [USER, ASSISTANT]}))


@pytest.mark.unit
def test_an_unanswered_group_in_the_middle_is_a_violation() -> None:
    state = apply_update(make_state(), {"messages": [USER, ASSISTANT, USER]})
    with pytest.raises(AtomicPairError):
        check_invariants(state)


@pytest.mark.unit
def test_unanswered_calls_reads_the_tail_only() -> None:
    assert unanswered_calls([USER, ASSISTANT]) == (CALL,)
    assert unanswered_calls([USER, ASSISTANT, TOOL]) == ()
    assert unanswered_calls([USER]) == ()


@pytest.mark.unit
def test_plan_limits_are_enforced_on_commit() -> None:
    items = [PlanItem(id=i, title="t") for i in range(MAX_PLAN_ITEMS + 1)]
    with pytest.raises(InvalidStateError, match="plan"):
        check_invariants(apply_update(make_state(), {"plan": items}))
    dup = [PlanItem(id=1, title="a"), PlanItem(id=1, title="b")]
    with pytest.raises(InvalidStateError, match="id"):
        check_invariants(apply_update(make_state(), {"plan": dup}))


@pytest.mark.unit
def test_note_limits_are_enforced_on_commit() -> None:
    too_many = apply_update(make_state(), {"facts": [str(i) for i in range(MAX_FACTS + 1)]})
    with pytest.raises(InvalidStateError, match="facts"):
        check_invariants(too_many)
    too_long = apply_update(make_state(), {"facts": ["x" * (MAX_FACT_CHARS + 1)]})
    with pytest.raises(InvalidStateError, match="字符"):
        check_invariants(too_long)


@pytest.mark.unit
def test_typed_fields_reject_the_wrong_types() -> None:
    state = make_state()
    for update in (
        {"plan": ["x"]},
        {"facts": "abc"},
        {"failed_attempts": [FailedAttempt]},
        {"pending_approval": "x"},
        {"verification": "maybe"},
    ):
        with pytest.raises(InvalidUpdateError):
            apply_update(state, update)


@pytest.mark.unit
def test_pending_approval_round_trips_through_update() -> None:
    pending = PendingApproval(
        approval_id="a",
        call_ids=("c1",),
        required_level="full_permissions",
        current_level="read_only",
        action_type="network",
        command="curl x",
        reason="r",
        escalation_count=1,
    )
    state = apply_update(make_state(), {"messages": [USER, ASSISTANT], "pending_approval": pending})
    assert state.pending_approval == pending
    check_invariants(state)
