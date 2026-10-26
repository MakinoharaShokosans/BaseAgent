"""路由表：下一阶段完全由当前阶段结束后的状态决定。

出处：docs/02_runtime/02_main_loop.md §2。
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_runtime.core import (
    DONE,
    AgentState,
    Message,
    Milestone,
    Phase,
    ToolCall,
    apply_update,
    new_state,
)
from agent_runtime.loop import ALLOWED_TRANSITIONS, ROUTES, next_phase

PHASES: tuple[str, ...] = get_args(Phase)


def state_with(**update: Any) -> AgentState:
    base = new_state(
        workspace_id="w",
        workspace_path="/p",
        session_id="s",
        task_id="t",
        task_goal="g",
        permission_level="read_only",
        canary_token="c",
    )
    return apply_update(base, update)


def milestones(*statuses: str) -> list[Milestone]:
    return [Milestone(id=i, title=f"m{i}", status=s) for i, s in enumerate(statuses)]  # type: ignore[arg-type]


CALL = ToolCall(id="c1", name="view_file", args={})
WITH_CALLS = Message(role="assistant", content="", tool_calls=(CALL,))
TEXT_ONLY = Message(role="assistant", content="all done")


@pytest.mark.unit
def test_routes_cover_exactly_the_five_phases() -> None:
    assert set(ROUTES) == set(PHASES)
    assert set(ALLOWED_TRANSITIONS) == set(PHASES)


# ------------------------------------------------------------------------------
# planner
# ------------------------------------------------------------------------------


@pytest.mark.unit
def test_planner_with_unfinished_milestones_goes_to_budget_guard() -> None:
    state = state_with(milestones=milestones("completed", "pending"))
    assert next_phase("planner", state) == "budget_guard"


@pytest.mark.unit
def test_empty_milestones_count_as_unfinished() -> None:
    """单步任务不能因为“没有计划”就跳过执行。"""
    assert next_phase("planner", state_with()) == "budget_guard"


@pytest.mark.unit
def test_planner_that_answered_directly_goes_straight_to_done() -> None:
    state = state_with(milestones=milestones("completed"), step_count=0)
    assert next_phase("planner", state) == DONE


@pytest.mark.unit
def test_all_complete_after_work_goes_to_evaluator() -> None:
    state = state_with(milestones=milestones("completed"), step_count=3)
    assert next_phase("planner", state) == "evaluator"


@pytest.mark.unit
def test_failed_milestone_is_not_complete() -> None:
    state = state_with(milestones=milestones("completed", "failed"), step_count=2)
    assert next_phase("planner", state) == "budget_guard"


# ------------------------------------------------------------------------------
# 其余阶段
# ------------------------------------------------------------------------------


@pytest.mark.unit
def test_budget_guard_continues_unless_terminated() -> None:
    assert next_phase("budget_guard", state_with()) == "executor"


@pytest.mark.unit
def test_executor_with_tool_calls_goes_to_tool_runner() -> None:
    assert next_phase("executor", state_with(messages=[WITH_CALLS])) == "tool_runner"


@pytest.mark.unit
def test_executor_with_a_text_answer_goes_to_evaluator() -> None:
    assert next_phase("executor", state_with(messages=[TEXT_ONLY])) == "evaluator"
    assert next_phase("executor", state_with()) == "evaluator"


@pytest.mark.unit
def test_executor_only_looks_at_the_last_message() -> None:
    old_calls_then_text = [
        WITH_CALLS,
        Message(role="tool", content="x", tool_call_id="c1", name="t"),
        TEXT_ONLY,
    ]
    assert next_phase("executor", state_with(messages=old_calls_then_text)) == "evaluator"


@pytest.mark.unit
def test_tool_runner_returns_to_planner() -> None:
    assert next_phase("tool_runner", state_with()) == "planner"


@pytest.mark.unit
def test_evaluator_delivers_when_every_milestone_is_complete() -> None:
    assert next_phase("evaluator", state_with(milestones=milestones("completed"))) == DONE


@pytest.mark.unit
def test_evaluator_loops_back_when_not_converged() -> None:
    assert (
        next_phase("evaluator", state_with(milestones=milestones("completed", "pending")))
        == "planner"
    )
    assert next_phase("evaluator", state_with()) == "planner"


# ------------------------------------------------------------------------------
# 硬终止
# ------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("phase", PHASES)
def test_hard_termination_always_routes_to_done(phase: str) -> None:
    state = state_with(should_terminate=True, termination_reason="budget", messages=[WITH_CALLS])
    assert next_phase(phase, state) == DONE  # type: ignore[arg-type]


# ------------------------------------------------------------------------------
# 性质：只会走文档里画出的边
# ------------------------------------------------------------------------------


@st.composite
def arbitrary_states(draw: st.DrawFn) -> AgentState:
    statuses = draw(
        st.lists(st.sampled_from(["pending", "in_progress", "completed", "failed"]), max_size=4)
    )
    tail = draw(st.sampled_from([None, WITH_CALLS, TEXT_ONLY]))
    return state_with(
        milestones=milestones(*statuses),
        step_count=draw(st.integers(0, 5)),
        should_terminate=draw(st.booleans()),
        messages=[] if tail is None else [tail],
    )


@pytest.mark.unit
@given(arbitrary_states(), st.sampled_from(PHASES))
def test_routes_only_follow_documented_edges(state: AgentState, phase: str) -> None:
    assert next_phase(phase, state) in ALLOWED_TRANSITIONS[phase]  # type: ignore[index]


@pytest.mark.unit
@given(arbitrary_states(), st.sampled_from(PHASES))
def test_routes_are_pure(state: AgentState, phase: str) -> None:
    assert next_phase(phase, state) == next_phase(phase, state)  # type: ignore[arg-type]
