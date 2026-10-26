"""planner / evaluator 接真实的 LLMGateway（模拟网络）：契约、路径与降级计数端到端。"""

from __future__ import annotations

import pytest

from agent_runtime.core import Message, Milestone, apply_update
from agent_runtime.loop import next_phase
from agent_runtime.loop.stages import ModelStageDeps, make_evaluator_stage, make_planner_stage
from agent_runtime.loop.stages.evaluator import REJECTION_MARK
from stage_support import MakeState, assemble, with_state
from test_llm_gateway import World
from test_llm_structured_gateway import ep, reply


def _deps(gateway: object) -> ModelStageDeps:
    return ModelStageDeps(gateway=gateway, assemble=assemble)  # type: ignore[arg-type]


@pytest.mark.integration
async def test_the_planner_plans_through_the_real_gateway(make_state: MakeState) -> None:
    world = World()
    gateway = world.gateway(reasoning=(ep("r", json_object=True),))
    world.script["r"] = [
        reply({"thought": "t", "next_step": "读 README", "milestones": [{"title": "读"}]})
    ]

    state = make_state()
    update = await make_planner_stage(_deps(gateway))(state)
    after = apply_update(state, update)

    assert [m.title for m in after.milestones] == ["读"]
    assert after.messages[-1].content == "读 README"
    assert after.total_tokens == 15
    assert world.body()["response_format"] == {"type": "json_object"}
    assert "next_step" in world.body()["messages"][0]["content"]


@pytest.mark.integration
async def test_an_evaluator_whose_output_stays_unreadable_rejects_and_is_counted(
    make_state: MakeState,
) -> None:
    world = World()
    gateway = world.gateway(reasoning=(ep("r", json_object=True),))
    world.script["r"] = [reply("验收通过"), reply("确实通过了")]

    state = with_state(
        make_state(),
        milestones=(Milestone(id=1, title="m", status="completed"),),
        step_count=2,
        messages=(Message(role="user", content="fix"), Message(role="assistant", content="好了")),
    )
    update = await make_evaluator_stage(_deps(gateway))(state)
    after = apply_update(state, update)

    assert after.milestones[0].status == "in_progress"
    assert after.messages[-1].content.startswith(REJECTION_MARK)
    assert next_phase("evaluator", after) == "planner"
    assert gateway.structured_counts == {("evaluator", "degraded"): 1}
