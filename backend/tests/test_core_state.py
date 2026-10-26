"""AgentState 与 apply_update 的更新语义。出处：docs/02_runtime/01_state_model.md §2-4。"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from agent_runtime.core import (
    DONE,
    AgentState,
    AtomicPairError,
    FailedAttempt,
    InvalidStateError,
    InvalidUpdateError,
    Message,
    Milestone,
    PendingApproval,
    ToolCall,
    apply_update,
    check_invariants,
    new_state,
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


def approval(**kw: Any) -> PendingApproval:
    base: dict[str, Any] = {
        "approval_id": "appr_1",
        "call_ids": ["c1"],
        "required_level": "full_permissions",
        "current_level": "workspace_write",
        "action_type": "network",
        "command": "curl x",
        "reason": "network",
        "escalation_count": 1,
    }
    return PendingApproval(**{**base, **kw})


# ==============================================================================
# 初始状态
# ==============================================================================


@pytest.mark.unit
def test_new_state_defaults() -> None:
    state = make_state()
    assert state.phase == "planner"
    assert state.messages == ()
    assert state.milestones == ()
    assert state.step_count == state.total_tokens == state.consecutive_errors == 0
    assert state.should_terminate is False
    assert state.pending_approval is None
    assert state.approval_decision is None
    assert state.dispatch_started == ()
    assert state.schema_version == 1


@pytest.mark.unit
def test_new_state_can_inherit_session_memory() -> None:
    state = make_state(rolling_summary="before", confirmed_facts=["f1"], failed_attempts=[])
    assert state.rolling_summary == "before"
    assert state.confirmed_facts == ("f1",)


@pytest.mark.unit
@pytest.mark.parametrize("field", ["task_id", "task_goal", "workspace_id", "canary_token"])
def test_required_identity_fields_cannot_be_empty(field: str) -> None:
    with pytest.raises(InvalidUpdateError, match=field):
        make_state(**{field: ""})


@pytest.mark.unit
def test_permission_level_must_be_known() -> None:
    with pytest.raises(InvalidUpdateError, match="permission_level"):
        make_state(permission_level="root")


@pytest.mark.unit
def test_state_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_state().step_count = 1  # type: ignore[misc]


# ==============================================================================
# 更新语义
# ==============================================================================


@pytest.mark.unit
def test_messages_are_appended_in_order() -> None:
    first = apply_update(make_state(), {"messages": [USER]})
    second = apply_update(first, {"messages": [ASSISTANT, TOOL]})
    assert second.messages == (USER, ASSISTANT, TOOL)


@pytest.mark.unit
def test_apply_update_is_pure() -> None:
    state = make_state()
    new = apply_update(state, {"messages": [USER], "step_count": 3})
    assert new is not state
    assert state.messages == ()
    assert state.step_count == 0


@pytest.mark.unit
def test_other_lists_are_replaced_not_appended() -> None:
    m1 = Milestone(id=1, title="a")
    m2 = Milestone(id=2, title="b")
    state = apply_update(make_state(), {"milestones": [m1], "confirmed_facts": ["x", "y"]})
    state = apply_update(state, {"milestones": [m2], "confirmed_facts": ["z"]})
    assert state.milestones == (m2,)
    assert state.confirmed_facts == ("z",)


@pytest.mark.unit
def test_dict_fields_are_replaced() -> None:
    state = apply_update(make_state(), {"artifacts": {"a1": "/p/1"}})
    state = apply_update(state, {"artifacts": {"a2": "/p/2"}})
    assert dict(state.artifacts) == {"a2": "/p/2"}


@pytest.mark.unit
def test_scalars_overwrite_and_absent_fields_stay() -> None:
    state = apply_update(make_state(rolling_summary="keep"), {"step_count": 2, "phase": "executor"})
    assert (state.step_count, state.phase, state.rolling_summary) == (2, "executor", "keep")


@pytest.mark.unit
def test_empty_update_returns_an_equal_state() -> None:
    state = make_state()
    assert apply_update(state, {}) == state


@pytest.mark.unit
def test_later_mutation_of_the_update_payload_cannot_reach_the_state() -> None:
    facts = ["a"]
    artifacts = {"k": "v"}
    new = apply_update(make_state(), {"confirmed_facts": facts, "artifacts": artifacts})
    facts.append("b")
    artifacts["k2"] = "v2"
    assert new.confirmed_facts == ("a",)
    assert dict(new.artifacts) == {"k": "v"}


@pytest.mark.unit
def test_unknown_field_is_rejected() -> None:
    with pytest.raises(InvalidUpdateError, match="nonsense"):
        apply_update(make_state(), {"nonsense": 1})


@pytest.mark.unit
@pytest.mark.parametrize(
    "field",
    [
        "workspace_id",
        "workspace_path",
        "session_id",
        "task_id",
        "task_goal",
        "permission_level",
        "canary_token",
        "schema_version",
    ],
)
def test_fields_fixed_at_initialisation_cannot_be_updated(field: str) -> None:
    with pytest.raises(InvalidUpdateError, match=field):
        apply_update(make_state(), {field: "x"})


@pytest.mark.unit
def test_messages_update_must_be_a_sequence_of_messages() -> None:
    state = make_state()
    for bad in (USER, [{"role": "user", "content": "x"}], "text", None):
        with pytest.raises(InvalidUpdateError, match="messages"):
            apply_update(state, {"messages": bad})


@pytest.mark.unit
def test_rejected_update_is_all_or_nothing() -> None:
    state = make_state()
    with pytest.raises(InvalidUpdateError):
        apply_update(state, {"messages": [USER], "step_count": -1})
    assert state.messages == ()


@pytest.mark.unit
@pytest.mark.parametrize("field", ["step_count", "total_tokens", "consecutive_errors"])
def test_counters_cannot_be_negative_or_non_integer(field: str) -> None:
    with pytest.raises(InvalidUpdateError, match=field):
        apply_update(make_state(), {field: -1})
    with pytest.raises(InvalidUpdateError, match=field):
        apply_update(make_state(), {field: 1.5})
    with pytest.raises(InvalidUpdateError, match=field):
        apply_update(make_state(), {field: True})


@pytest.mark.unit
def test_phase_must_be_a_known_phase_or_done() -> None:
    for phase in ("planner", "budget_guard", "executor", "tool_runner", "evaluator", DONE):
        assert apply_update(make_state(), {"phase": phase}).phase == phase
    with pytest.raises(InvalidUpdateError, match="phase"):
        apply_update(make_state(), {"phase": "thinking"})


@pytest.mark.unit
def test_typed_fields_reject_wrong_types() -> None:
    state = make_state()
    with pytest.raises(InvalidUpdateError, match="milestones"):
        apply_update(state, {"milestones": [{"id": 1}]})
    with pytest.raises(InvalidUpdateError, match="failed_attempts"):
        apply_update(state, {"failed_attempts": ["x"]})
    with pytest.raises(InvalidUpdateError, match="pending_approval"):
        apply_update(state, {"pending_approval": {"approval_id": "x"}})
    with pytest.raises(InvalidUpdateError, match="should_terminate"):
        apply_update(state, {"should_terminate": "yes"})


@pytest.mark.unit
def test_approval_pending_and_cleared() -> None:
    state = apply_update(make_state(), {"pending_approval": approval()})
    assert state.pending_approval is not None
    assert state.pending_approval.approval_id == "appr_1"
    cleared = apply_update(state, {"pending_approval": None, "approval_decision": None})
    assert cleared.pending_approval is None


@pytest.mark.unit
def test_milestone_and_failed_attempt_models() -> None:
    assert Milestone(id=1, title="t").status == "pending"
    with pytest.raises(ValueError, match="status"):
        Milestone(id=1, title="t", status="done")  # type: ignore[arg-type]
    attempt = FailedAttempt(action="a", failure_reason="r", conclusion="c")
    assert attempt.action == "a"


# ==============================================================================
# 状态不变量
# ==============================================================================


@pytest.mark.unit
def test_invariants_accept_a_consistent_state() -> None:
    state = apply_update(make_state(), {"messages": [USER, ASSISTANT, TOOL]})
    check_invariants(state)


@pytest.mark.unit
def test_invariants_reject_unpaired_tool_calls() -> None:
    state = apply_update(make_state(), {"messages": [USER, ASSISTANT]})
    with pytest.raises(AtomicPairError):
        check_invariants(state)


@pytest.mark.unit
def test_pending_approval_relaxes_the_last_group() -> None:
    state = apply_update(
        make_state(), {"messages": [USER, ASSISTANT], "pending_approval": approval()}
    )
    check_invariants(state)


@pytest.mark.unit
def test_milestone_pointer_must_stay_in_range() -> None:
    m = Milestone(id=1, title="a")
    ok = apply_update(make_state(), {"milestones": [m], "current_milestone_idx": 1})
    check_invariants(ok)  # 等于长度表示全部完成
    bad = apply_update(make_state(), {"milestones": [m], "current_milestone_idx": 2})
    with pytest.raises(InvalidStateError, match="current_milestone_idx"):
        check_invariants(bad)


@pytest.mark.unit
def test_dispatch_started_must_refer_to_the_last_assistant_calls() -> None:
    good = apply_update(make_state(), {"messages": [USER, ASSISTANT], "dispatch_started": ["c1"]})
    check_invariants(dataclasses.replace(good, pending_approval=approval()))
    stray = apply_update(
        make_state(), {"messages": [USER, ASSISTANT, TOOL], "dispatch_started": ["zz"]}
    )
    with pytest.raises(InvalidStateError, match="dispatch_started"):
        check_invariants(stray)


@pytest.mark.unit
def test_permission_levels_agree_with_the_configuration_schema() -> None:
    """core 不能依赖配置包，所以两边各有一份取值；这里保证它们不漂移。"""
    from typing import get_args

    from aegis_config.models import PermissionLevel as ConfigLevel
    from agent_runtime.core.state import PERMISSION_LEVELS

    assert set(PERMISSION_LEVELS) == set(get_args(ConfigLevel))


@pytest.mark.unit
def test_calls_awaiting_tool_runner_are_a_legal_tail() -> None:
    """executor 提交时，工具调用已在状态里而结果尚未产出；此时 phase 是 tool_runner。"""
    waiting = apply_update(make_state(), {"messages": [USER, ASSISTANT], "phase": "tool_runner"})
    check_invariants(waiting)
    elsewhere = apply_update(make_state(), {"messages": [USER, ASSISTANT], "phase": "planner"})
    with pytest.raises(AtomicPairError):
        check_invariants(elsewhere)
