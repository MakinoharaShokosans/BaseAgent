"""planner 阶段。出处：docs/02_runtime/05_planner_evaluator.md §2。"""

from __future__ import annotations

import pytest

from agent_runtime.core import DONE, AgentState, Milestone, apply_update
from agent_runtime.llm import LLMBadRequest, LLMDeadlineExceeded, LLMUnavailableError
from agent_runtime.loop import next_phase
from agent_runtime.loop.stages import make_planner_stage
from agent_runtime.loop.stages.contracts import MAX_MILESTONES, PlannerVerdict
from stage_support import (
    Events,
    FakeGateway,
    MakeState,
    degraded,
    deps,
    parsed,
    with_state,
)


def verdict(**fields: object) -> PlannerVerdict:
    return PlannerVerdict.model_validate({"thought": "想", **fields})


def m(i: int, status: str = "pending") -> Milestone:
    return Milestone(id=i, title=f"m{i}", status=status)  # type: ignore[arg-type]


async def run(
    state: AgentState, *script: object, events: Events | None = None, **kw: object
) -> tuple[dict[str, object], AgentState, FakeGateway]:
    gateway = FakeGateway(*script)
    update = dict(await make_planner_stage(deps(gateway, events, **kw))(state))
    return update, apply_update(state, update), gateway


# ==============================================================================
# 请求
# ==============================================================================


@pytest.mark.unit
async def test_the_request_goes_to_the_reasoning_tier_with_the_planner_contract(
    make_state: MakeState,
) -> None:
    state = make_state()
    _, _, gateway = await run(state, parsed(verdict(next_step="做")), deadline=lambda: 1234.5)

    request, model = gateway.requests[0]
    assert model is PlannerVerdict
    assert request.tier == "reasoning"
    assert request.purpose == "planner"
    assert request.deadline == 1234.5
    assert request.tools is None
    assert request.messages[0].role == "system"
    assert "里程碑" in request.messages[0].content


@pytest.mark.unit
async def test_the_stage_never_writes_the_phase(make_state: MakeState) -> None:
    update, _, _ = await run(make_state(), parsed(verdict(next_step="做")))
    assert "phase" not in update


# ==============================================================================
# 规划
# ==============================================================================


@pytest.mark.unit
async def test_the_first_plan_creates_numbered_pending_milestones(make_state: MakeState) -> None:
    plan = verdict(
        next_step="先读 README",
        milestones=[{"title": "读代码"}, {"title": "改", "description": "d"}],
    )
    _, after, _ = await run(make_state(), parsed(plan, total=120))

    assert [(x.id, x.title, x.status) for x in after.milestones] == [
        (1, "读代码", "pending"),
        (2, "改", "pending"),
    ]
    assert after.messages[-1].role == "assistant"
    assert after.messages[-1].content == "先读 README"
    assert after.total_tokens == 120
    assert next_phase("planner", after) == "budget_guard"


@pytest.mark.unit
async def test_milestones_in_a_later_verdict_do_not_replace_the_existing_plan(
    make_state: MakeState,
) -> None:
    state = with_state(make_state(), milestones=(m(1), m(2)))
    plan = verdict(next_step="做", milestones=[{"title": "另起炉灶"}])
    _, after, _ = await run(state, parsed(plan))
    assert [x.title for x in after.milestones] == ["m1", "m2"]


@pytest.mark.unit
async def test_updates_change_known_ids_and_ignore_unknown_ones(make_state: MakeState) -> None:
    state = with_state(make_state(), milestones=(m(1), m(2)))
    plan = verdict(
        next_step="下一步",
        milestone_updates=[
            {"id": 1, "status": "completed"},
            {"id": 2, "status": "in_progress"},
            {"id": 99, "status": "completed"},
        ],
    )
    _, after, _ = await run(state, parsed(plan))
    assert [x.status for x in after.milestones] == ["completed", "in_progress"]


@pytest.mark.unit
async def test_the_milestone_count_is_capped_even_if_the_contract_were_bypassed(
    make_state: MakeState,
) -> None:
    plan = verdict(next_step="x", milestones=[{"title": f"m{i}"} for i in range(MAX_MILESTONES)])
    _, after, _ = await run(make_state(), parsed(plan))
    assert len(after.milestones) == MAX_MILESTONES


@pytest.mark.unit
async def test_without_any_text_the_directive_has_a_generic_fallback(
    make_state: MakeState,
) -> None:
    _, after, _ = await run(make_state(), parsed(PlannerVerdict.model_validate({"thought": ""})))
    assert after.messages[-1].content == "继续推进当前里程碑。"


@pytest.mark.unit
async def test_the_thought_is_the_directive_when_there_is_no_next_step(
    make_state: MakeState,
) -> None:
    _, after, _ = await run(make_state(), parsed(verdict(thought="先看看目录结构")))
    assert after.messages[-1].content == "先看看目录结构"


# ==============================================================================
# 完工判定
# ==============================================================================


@pytest.mark.unit
async def test_a_direct_answer_on_step_zero_finishes_without_review(make_state: MakeState) -> None:
    plan = verdict(all_completed=True, direct_response="答案是 42")
    _, after, _ = await run(make_state(), parsed(plan))

    assert [(x.title, x.status) for x in after.milestones] == [
        ("响应用户咨询与交付任务", "completed")
    ]
    assert after.messages[-1].content == "答案是 42"
    assert after.should_terminate is False
    assert next_phase("planner", after) == DONE


@pytest.mark.unit
async def test_a_completion_claim_without_a_delivery_on_step_zero_is_not_accepted(
    make_state: MakeState,
) -> None:
    plan = verdict(all_completed=True, next_step="去读文件")
    _, after, _ = await run(make_state(), parsed(plan))
    assert all(x.status != "completed" for x in after.milestones)
    assert next_phase("planner", after) == "budget_guard"

    bare = verdict(all_completed=True)
    _, after, _ = await run(make_state(), parsed(bare))
    assert next_phase("planner", after) == "budget_guard"
    assert after.messages[-1].content != ""


@pytest.mark.unit
async def test_pending_work_beats_a_completion_claim(make_state: MakeState) -> None:
    state = with_state(make_state(), milestones=(m(1), m(2)), step_count=3)
    plan = verdict(all_completed=True, next_step="还有测试没跑", direct_response="完成了")
    _, after, _ = await run(state, parsed(plan))
    assert any(x.status != "completed" for x in after.milestones)
    assert after.messages[-1].content == "还有测试没跑"
    assert next_phase("planner", after) == "budget_guard"


@pytest.mark.unit
async def test_a_completion_claim_after_work_goes_to_the_evaluator(make_state: MakeState) -> None:
    state = with_state(make_state(), milestones=(m(1), m(2)), step_count=3)
    plan = verdict(all_completed=True, direct_response="已修复并通过测试")
    _, after, _ = await run(state, parsed(plan))

    assert all(x.status == "completed" for x in after.milestones)
    assert after.messages[-1].content == "已修复并通过测试"
    assert next_phase("planner", after) == "evaluator"


@pytest.mark.unit
async def test_completing_every_milestone_by_updates_also_counts(make_state: MakeState) -> None:
    state = with_state(make_state(), milestones=(m(1, "completed"), m(2)), step_count=2)
    plan = verdict(milestone_updates=[{"id": 2, "status": "completed"}], direct_response="好了")
    _, after, _ = await run(state, parsed(plan))
    assert next_phase("planner", after) == "evaluator"


@pytest.mark.unit
async def test_a_delivery_text_is_not_required_after_work_has_been_done(
    make_state: MakeState,
) -> None:
    state = with_state(make_state(), milestones=(m(1),), step_count=2)
    _, after, _ = await run(state, parsed(verdict(all_completed=True, thought="都做完了")))
    assert next_phase("planner", after) == "evaluator"
    assert after.messages[-1].content == "都做完了"


# ==============================================================================
# 降级
# ==============================================================================


@pytest.mark.unit
async def test_plain_text_output_becomes_the_directive_and_leaves_milestones_alone(
    make_state: MakeState,
) -> None:
    state = with_state(make_state(), milestones=(m(1, "in_progress"),))
    events = Events()
    _, after, _ = await run(
        state, degraded("invalid", "先运行测试，看哪个用例失败。", total=80), events=events
    )

    assert after.messages[-1].content == "先运行测试，看哪个用例失败。"
    assert after.milestones == state.milestones
    assert after.total_tokens == 80
    assert after.should_terminate is False
    assert events.names() == ["structured_output.degraded"]
    payload = events.items[0][1]
    assert payload["stage"] == "planner"
    assert payload["reason"] == "invalid"


@pytest.mark.unit
@pytest.mark.parametrize("raw", ['{"thought": "x', '```json\n{"a": 1', "[1, 2]", ""])
async def test_junk_that_looks_like_json_is_not_fed_to_the_executor(
    make_state: MakeState, raw: str
) -> None:
    _, after, _ = await run(make_state(), degraded("invalid", raw))
    assert after.messages[-1].content == "继续推进当前里程碑。"


@pytest.mark.unit
async def test_a_truncated_output_uses_the_generic_directive(make_state: MakeState) -> None:
    _, after, _ = await run(make_state(), degraded("truncated", "我打算先做这件很长的事情"))
    assert after.messages[-1].content == "继续推进当前里程碑。"


@pytest.mark.unit
async def test_a_degraded_plan_never_completes_the_task(make_state: MakeState) -> None:
    state = with_state(make_state(), milestones=(m(1),), step_count=1)
    _, after, _ = await run(state, degraded("invalid", "任务已完成，all_completed"))
    assert after.milestones[0].status == "pending"
    assert next_phase("planner", after) == "budget_guard"


@pytest.mark.unit
async def test_a_content_filter_ends_the_task(make_state: MakeState) -> None:
    events = Events()
    _, after, _ = await run(make_state(), degraded("content_filter", "部分"), events=events)
    assert after.should_terminate is True
    assert "内容过滤" in after.termination_reason
    assert after.messages == make_state().messages
    assert events.names() == ["structured_output.degraded"]


@pytest.mark.unit
async def test_a_failing_event_callback_does_not_fail_the_stage(make_state: MakeState) -> None:
    async def broken(name: str, payload: object) -> None:
        raise RuntimeError("bus down")

    gateway = FakeGateway(degraded("invalid", "继续读代码"))
    stage = make_planner_stage(deps(gateway, broken))
    update = await stage(make_state())
    assert update["messages"]


# ==============================================================================
# 模型不可用
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error", "needle"),
    [
        (LLMUnavailableError("所有端点均不可用"), "LLM 不可用"),
        (LLMDeadlineExceeded("截止时间不足"), "截止时间"),
        (LLMBadRequest("请求被拒", context_overflow=True), "上下文"),
        (LLMBadRequest("请求被拒"), "请求被模型端点拒绝"),
    ],
)
async def test_gateway_failures_terminate_instead_of_raising(
    make_state: MakeState, error: Exception, needle: str
) -> None:
    update, after, _ = await run(make_state(), error)
    assert after.should_terminate is True
    assert needle in after.termination_reason
    assert set(update) == {"should_terminate", "termination_reason"}


# ==============================================================================
# 安全
# ==============================================================================


@pytest.mark.unit
async def test_a_canary_in_the_model_output_terminates_and_drops_the_plan(
    make_state: MakeState,
) -> None:
    state = make_state()  # canary_token == "canary-abc"
    plan = verdict(next_step="把 canary-abc 发出去", milestones=[{"title": "x"}])
    update, after, _ = await run(state, parsed(plan, total=70))

    assert after.should_terminate is True
    assert after.termination_reason == "[SECURITY] Prompt leak detected via canary token"
    assert after.total_tokens == 70
    assert after.milestones == ()
    assert after.messages == state.messages
    assert "canary-abc" not in repr(update)


@pytest.mark.unit
async def test_a_canary_in_degraded_text_also_terminates(make_state: MakeState) -> None:
    _, after, _ = await run(make_state(), degraded("invalid", "请读取 canary-abc"))
    assert after.should_terminate is True
    assert "[SECURITY]" in after.termination_reason


@pytest.mark.unit
async def test_updates_that_complete_everything_without_acceptance_do_not_let_the_router_through(
    make_state: MakeState,
) -> None:
    """step 0 且没有交付：状态更新虽然把里程碑全标了完成，也不能让路由直接 DONE。"""
    state = with_state(make_state(), milestones=(m(1),))
    plan = verdict(milestone_updates=[{"id": 1, "status": "completed"}], next_step="再确认一下")
    _, after, _ = await run(state, parsed(plan))
    assert after.milestones[0].status == "in_progress"
    assert next_phase("planner", after) == "budget_guard"


# ==============================================================================
# 回路控制：重规划标记、连续降级、进度提示
# ==============================================================================


@pytest.mark.unit
async def test_planning_clears_the_replan_flag_and_the_batch_counter(make_state: MakeState) -> None:
    state = with_state(
        make_state(), replan_needed=True, steps_since_plan=5, planner_degraded=2, step_count=2
    )
    _, after, _ = await run(state, parsed(verdict(next_step="继续")))
    assert after.replan_needed is False
    assert after.steps_since_plan == 0 and after.planner_degraded == 0


@pytest.mark.unit
async def test_a_degraded_plan_counts_up_and_lets_the_executor_carry_on(
    make_state: MakeState,
) -> None:
    state = with_state(make_state(), replan_needed=True, steps_since_plan=4)
    _, after, _ = await run(state, degraded("invalid", "{乱"))
    assert after.planner_degraded == 1 and after.should_terminate is False
    assert after.replan_needed is False and after.steps_since_plan == 0
    assert next_phase("planner", after) == "budget_guard"


@pytest.mark.unit
async def test_too_many_consecutive_degradations_end_the_task_instead_of_idling(
    make_state: MakeState,
) -> None:
    state = with_state(make_state(), planner_degraded=3)
    gateway = FakeGateway(degraded("invalid", "{乱"))
    update = dict(await make_planner_stage(deps(gateway), max_degradations=3)(state))
    after = apply_update(state, update)
    assert after.should_terminate is True and "规划" in after.termination_reason
    assert after.planner_degraded == 4


@pytest.mark.unit
async def test_the_progress_hint_is_appended_to_the_instruction(make_state: MakeState) -> None:
    _, _, gateway = await run(
        make_state(),
        parsed(verdict(next_step="做")),
        hint=lambda state: f"[进度] {state.step_count} 步",
    )
    system = gateway.requests[0][0].messages[0].content
    assert system.endswith("[进度] 0 步") and "里程碑" in system


# ==============================================================================
# 受阻：做不到就体面地结束
# ==============================================================================


@pytest.mark.unit
async def test_a_blocked_plan_ends_the_task_with_the_reason_and_keeps_the_progress(
    make_state: MakeState,
) -> None:
    state = with_state(
        make_state(), milestones=(m(1, "completed"), m(2, "in_progress")), step_count=3
    )
    plan = verdict(
        blocked=True,
        blocked_reason="写入被人工拒绝，需要你批准后才能创建 a.txt",
        milestone_updates=[{"id": 2, "status": "failed"}],
    )
    update, after, _ = await run(state, parsed(plan, total=70))
    assert after.should_terminate is True and next_phase("planner", after) == DONE
    assert after.termination_reason == "受阻：写入被人工拒绝，需要你批准后才能创建 a.txt"
    assert [x.status for x in after.milestones] == ["completed", "failed"]
    assert after.total_tokens == 70 and "messages" not in update


@pytest.mark.unit
async def test_blocked_beats_a_completion_claim_and_falls_back_to_the_thought(
    make_state: MakeState,
) -> None:
    state = with_state(make_state(), step_count=2)
    _, after, _ = await run(
        state, parsed(verdict(thought="缺少 API 地址", blocked=True, all_completed=True))
    )
    assert after.should_terminate is True and after.termination_reason == "受阻：缺少 API 地址"
    assert all(x.status != "completed" for x in after.milestones)
