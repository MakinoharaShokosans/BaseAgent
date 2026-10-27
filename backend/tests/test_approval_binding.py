"""审批决策的解析与绑定校验。出处：docs/02_runtime/04_approval.md §3-4。"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agent_runtime.approval import (
    ApprovalBindingError,
    ApprovalMismatchError,
    InvalidDecisionError,
    NotWaitingForApprovalError,
    bind_decision,
    check_binding,
    compute_approval_id,
    parse_decision,
)
from agent_runtime.core import (
    AgentState,
    ApprovalDecision,
    Message,
    PendingApproval,
    ToolCall,
    apply_update,
)

MakeState = Callable[..., AgentState]


def pending_for(task_id: str, step: int, call_ids: tuple[str, ...]) -> PendingApproval:
    return PendingApproval(
        approval_id=compute_approval_id(task_id, step, call_ids),
        call_ids=call_ids,
        required_level="full_permissions",
        current_level="workspace_write",
        action_type="network",
        command="curl x",
        reason="r",
        escalation_count=len(call_ids),
    )


def waiting(make_state: MakeState, call_ids: tuple[str, ...] = ("c1", "c2")) -> AgentState:
    calls = tuple(ToolCall(id=i, name="bash", args={}) for i in call_ids)
    return apply_update(
        make_state(),
        {
            "messages": [Message(role="assistant", content="", tool_calls=calls)],
            "pending_approval": pending_for("t1", 1, call_ids),
        },
    )


# ==============================================================================
# approval_id
# ==============================================================================


@pytest.mark.unit
def test_approval_id_is_deterministic_and_order_insensitive() -> None:
    a = compute_approval_id("t1", 3, ("c1", "c2"))
    assert a == compute_approval_id("t1", 3, ("c1", "c2"))
    assert a == compute_approval_id("t1", 3, ("c2", "c1"))
    assert a.startswith("appr_")
    assert len(a) == len("appr_") + 16


@pytest.mark.unit
def test_approval_id_changes_with_every_input() -> None:
    base = compute_approval_id("t1", 3, ("c1", "c2"))
    assert compute_approval_id("t2", 3, ("c1", "c2")) != base
    assert compute_approval_id("t1", 4, ("c1", "c2")) != base
    assert compute_approval_id("t1", 3, ("c1",)) != base
    assert compute_approval_id("t1", 3, ("c1", "c3")) != base


@pytest.mark.unit
def test_approval_id_has_no_boundary_ambiguity() -> None:
    assert compute_approval_id("t", 1, ("ab", "c")) != compute_approval_id("t", 1, ("a", "bc"))


# ==============================================================================
# 解析
# ==============================================================================


@pytest.mark.unit
def test_parse_approval_defaults_to_once() -> None:
    decision = parse_decision({"approval_id": "appr_1", "approved": True})
    assert decision == ApprovalDecision(approval_id="appr_1", approved=True, scope="once")


@pytest.mark.unit
def test_parse_always_and_rejection() -> None:
    assert (
        parse_decision({"approval_id": "a", "approved": True, "scope": "always"}).scope == "always"
    )
    rejected = parse_decision({"approval_id": "a", "approved": False, "reason": "不允许联网"})
    assert (rejected.approved, rejected.reason) == (False, "不允许联网")
    assert parse_decision({"approval_id": "a", "approved": False}).reason == ""


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        {"approval_id": "a", "approved": "yes"},
        {"approval_id": "a", "approved": 1},
        {"approval_id": "a"},
        {"approved": True},
        {"approval_id": "", "approved": True},
        {"approval_id": "a", "approved": True, "scope": "forever"},
        {"approval_id": "a", "approved": True, "surprise": 1},
        {"approval_id": 7, "approved": True},
        "approve",
        None,
    ],
)
def test_malformed_decisions_are_rejected(raw: object) -> None:
    with pytest.raises(InvalidDecisionError):
        parse_decision(raw)  # type: ignore[arg-type]


# ==============================================================================
# bind_decision
# ==============================================================================


@pytest.mark.unit
def test_bind_attaches_the_decision_and_keeps_the_request(make_state: MakeState) -> None:
    state = waiting(make_state)
    decision = ApprovalDecision(approval_id=state.pending_approval.approval_id, approved=True)  # type: ignore[union-attr]
    bound = bind_decision(state, decision)
    assert bound.approval_decision == decision
    assert bound.pending_approval == state.pending_approval  # 由 tool_runner 消费时一并清空
    assert state.approval_decision is None  # 纯函数


@pytest.mark.unit
def test_bind_refuses_when_nothing_is_pending(make_state: MakeState) -> None:
    with pytest.raises(NotWaitingForApprovalError):
        bind_decision(make_state(), ApprovalDecision(approval_id="appr_1", approved=True))


@pytest.mark.unit
def test_bind_refuses_a_decision_for_another_request(make_state: MakeState) -> None:
    with pytest.raises(ApprovalMismatchError, match="approval_id"):
        bind_decision(
            waiting(make_state), ApprovalDecision(approval_id="appr_other", approved=True)
        )


@pytest.mark.unit
def test_second_decision_before_consumption_is_refused(make_state: MakeState) -> None:
    state = waiting(make_state)
    approval_id = state.pending_approval.approval_id  # type: ignore[union-attr]
    first = bind_decision(state, ApprovalDecision(approval_id=approval_id, approved=True))
    with pytest.raises(ApprovalBindingError, match="已有"):
        bind_decision(first, ApprovalDecision(approval_id=approval_id, approved=False))


@pytest.mark.unit
def test_binding_errors_share_a_base_class() -> None:
    assert issubclass(NotWaitingForApprovalError, ApprovalBindingError)
    assert issubclass(ApprovalMismatchError, ApprovalBindingError)
    assert issubclass(InvalidDecisionError, ApprovalBindingError)


# ==============================================================================
# check_binding：tool_runner 重新判定后的核对
# ==============================================================================


@pytest.mark.unit
def test_check_binding_accepts_the_same_escalations(make_state: MakeState) -> None:
    state = waiting(make_state)
    decision = ApprovalDecision(approval_id=state.pending_approval.approval_id, approved=True)  # type: ignore[union-attr]
    check_binding(apply_update(state, {"approval_decision": decision}), ("c2", "c1"))


@pytest.mark.unit
def test_check_binding_rejects_a_different_set_of_escalations(make_state: MakeState) -> None:
    """重启后重新判定得到的越级集合与当初请求不同：不能套用旧决策。"""
    state = waiting(make_state)
    decision = ApprovalDecision(approval_id=state.pending_approval.approval_id, approved=True)  # type: ignore[union-attr]
    bound = apply_update(state, {"approval_decision": decision})
    with pytest.raises(ApprovalMismatchError, match="call_ids"):
        check_binding(bound, ("c1",))
    with pytest.raises(ApprovalMismatchError, match="call_ids"):
        check_binding(bound, ("c1", "c2", "c3"))


@pytest.mark.security
@pytest.mark.unit
def test_check_binding_rejects_a_forged_request_id(make_state: MakeState) -> None:
    """approval_id 必须能由任务、步数与调用集合重新算出，被改写的请求不被信任。"""
    state = waiting(make_state)
    forged = state.pending_approval.model_copy(update={"approval_id": "appr_forged"})  # type: ignore[union-attr]
    bound = apply_update(
        state,
        {
            "pending_approval": forged,
            "approval_decision": ApprovalDecision(approval_id="appr_forged", approved=True),
        },
    )
    with pytest.raises(ApprovalMismatchError, match="approval_id"):
        check_binding(bound, ("c1", "c2"))


@pytest.mark.unit
def test_check_binding_needs_both_a_request_and_a_decision(make_state: MakeState) -> None:
    with pytest.raises(NotWaitingForApprovalError):
        check_binding(make_state(), ("c1",))
    with pytest.raises(ApprovalBindingError, match="决策"):
        check_binding(waiting(make_state), ("c1", "c2"))
