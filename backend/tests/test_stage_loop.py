"""planner 与 evaluator 接进 drive：假 executor / tool_runner，假网关给剧本。

出处：docs/02_runtime/05_planner_evaluator.md §4。
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from agent_runtime.core import AgentState, Message, Phase
from agent_runtime.guardrails import BudgetLimits, PhysicalBudgetGuard
from agent_runtime.loop import Stage, TaskRuntime, drive
from agent_runtime.loop.stages import (
    make_budget_guard_stage,
    make_evaluator_stage,
    make_planner_stage,
)
from agent_runtime.loop.stages.contracts import EvaluatorVerdict, PlannerVerdict
from stage_support import FakeGateway, MakeState, deps, parsed


class Store:
    def __init__(self) -> None:
        self.committed: list[AgentState] = []

    async def commit(self, state: AgentState) -> int:
        self.committed.append(state)
        return len(self.committed)


class Sink:
    async def stage_committed(
        self, phase: Phase, update: Mapping[str, object], state: AgentState
    ) -> None: ...

    async def approval_requested(self, request: object, state: AgentState) -> None: ...


def plan(**fields: object) -> PlannerVerdict:
    return PlannerVerdict.model_validate({"thought": "t", **fields})


def judge(ok: bool, **fields: object) -> EvaluatorVerdict:
    return EvaluatorVerdict.model_validate({"milestone_ok": ok, **fields})


def runtime(planner_script: list[object], evaluator_script: list[object]) -> TaskRuntime:
    async def executor(state: AgentState) -> Mapping[str, object]:
        # 假 executor：做一步，直接给出自然语言结论（不调工具）
        return {
            "step_count": state.step_count + 1,
            "messages": [Message(role="assistant", content="我做完了")],
        }

    async def tool_runner(state: AgentState) -> Mapping[str, object]:
        return {}

    guard = PhysicalBudgetGuard(
        BudgetLimits(
            max_steps=10, max_total_tokens=10_000, max_wall_time_sec=60, warning_ratio=0.9
        ),
        clock=lambda: 0.0,
    )
    stages: dict[Phase, Stage] = {
        "planner": make_planner_stage(deps(FakeGateway(*planner_script))),
        "budget_guard": make_budget_guard_stage(guard),
        "executor": executor,
        "tool_runner": tool_runner,
        "evaluator": make_evaluator_stage(deps(FakeGateway(*evaluator_script))),
    }
    return TaskRuntime(stages=stages, checkpoints=Store(), events=Sink())


@pytest.mark.integration
async def test_plan_work_accept_succeeds(make_state: MakeState) -> None:
    rt = runtime(
        [parsed(plan(next_step="做", milestones=[{"title": "唯一的里程碑"}]))],
        [parsed(judge(True, summary="完成"))],
    )
    outcome = await drive(rt, make_state())

    assert outcome.status == "succeeded"
    assert outcome.state.step_count == 1
    assert [m.status for m in outcome.state.milestones] == ["completed"]
    assert outcome.state.total_tokens == 200


@pytest.mark.integration
async def test_a_direct_answer_finishes_at_the_planner(make_state: MakeState) -> None:
    rt = runtime([parsed(plan(all_completed=True, direct_response="42"))], [])
    outcome = await drive(rt, make_state())
    assert outcome.status == "succeeded"
    assert outcome.state.step_count == 0
    assert outcome.state.messages[-1].content == "42"


@pytest.mark.integration
async def test_a_rejection_sends_the_task_back_to_the_planner_for_another_round(
    make_state: MakeState,
) -> None:
    rt = runtime(
        [
            parsed(plan(next_step="做", milestones=[{"title": "m"}])),
            parsed(plan(next_step="补上测试")),
        ],
        [parsed(judge(False, feedback="没有测试")), parsed(judge(True))],
    )
    outcome = await drive(rt, make_state())

    assert outcome.status == "succeeded"
    assert outcome.state.step_count == 2
    contents = [m.content for m in outcome.state.messages]
    assert any("没有测试" in c for c in contents)
    assert "补上测试" in contents


@pytest.mark.integration
async def test_a_reviewer_that_never_agrees_is_stopped_by_the_rejection_limit(
    make_state: MakeState,
) -> None:
    rt = runtime(
        [parsed(plan(next_step="做", milestones=[{"title": "m"}]))],
        [parsed(judge(False))],
    )
    outcome = await drive(rt, make_state())
    assert outcome.status == "terminated"
    assert "验收" in outcome.state.termination_reason


@pytest.mark.integration
async def test_a_planner_that_insists_on_being_done_cannot_loop_past_the_evaluator_limit(
    make_state: MakeState,
) -> None:
    """planner → evaluator → planner 这条回路不经过 budget_guard，上限只能靠打回次数。"""
    rt = runtime(
        [
            parsed(plan(next_step="做", milestones=[{"title": "m"}])),
            parsed(plan(all_completed=True, direct_response="好了")),
        ],
        [parsed(judge(False, feedback="不行"))],
    )
    outcome = await drive(rt, make_state())
    assert outcome.status == "terminated"
