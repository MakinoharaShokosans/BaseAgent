"""evaluator 阶段。出处：docs/02_runtime/05_planner_evaluator.md §3。"""

from __future__ import annotations

import pytest

from agent_runtime.core import DONE, AgentState, FailedAttempt, Message, Milestone, apply_update
from agent_runtime.llm import LLMBadRequest, LLMDeadlineExceeded, LLMUnavailableError
from agent_runtime.loop import next_phase
from agent_runtime.loop.stages import make_evaluator_stage
from agent_runtime.loop.stages.contracts import EvaluatorVerdict
from agent_runtime.loop.stages.evaluator import REJECTION_MARK
from stage_support import (
    Events,
    FakeGateway,
    MakeState,
    degraded,
    deps,
    parsed,
    with_state,
)


def verdict(**fields: object) -> EvaluatorVerdict:
    return EvaluatorVerdict.model_validate({"milestone_ok": True, **fields})


def m(i: int, status: str = "pending") -> Milestone:
    return Milestone(id=i, title=f"m{i}", status=status)  # type: ignore[arg-type]


REPLY = Message(role="assistant", content="已经修好了")


def worked(make_state: MakeState, *milestones: Milestone, idx: int = 0, **kw: object) -> AgentState:
    return with_state(
        make_state(),
        milestones=milestones,
        current_milestone_idx=idx,
        step_count=3,
        messages=(Message(role="user", content="fix"), REPLY),
        **kw,
    )


async def run(
    state: AgentState,
    *script: object,
    events: Events | None = None,
    max_rejections: int = 3,
    **kw: object,
) -> tuple[dict[str, object], AgentState, FakeGateway]:
    gateway = FakeGateway(*script)
    stage = make_evaluator_stage(deps(gateway, events, **kw), max_rejections=max_rejections)
    update = dict(await stage(state))
    return update, apply_update(state, update), gateway


# ==============================================================================
# 请求
# ==============================================================================


@pytest.mark.unit
async def test_the_request_uses_the_reasoning_tier_and_the_evaluator_contract(
    make_state: MakeState,
) -> None:
    _, _, gateway = await run(
        worked(make_state, m(1, "completed")), parsed(verdict()), deadline=lambda: 9.0
    )
    request, model = gateway.requests[0]
    assert model is EvaluatorVerdict
    assert (request.tier, request.purpose, request.deadline) == ("reasoning", "evaluator", 9.0)
    assert request.messages[0].role == "system"


@pytest.mark.unit
async def test_the_stage_never_writes_the_phase(make_state: MakeState) -> None:
    update, _, _ = await run(worked(make_state, m(1)), parsed(verdict()))
    assert "phase" not in update


# ==============================================================================
# 通过
# ==============================================================================


@pytest.mark.unit
async def test_accepting_one_milestone_moves_on_to_the_next(make_state: MakeState) -> None:
    state = worked(make_state, m(1, "in_progress"), m(2), m(3), idx=0)
    _, after, _ = await run(state, parsed(verdict(summary="第一步完成"), total=50))

    assert [x.status for x in after.milestones] == ["completed", "pending", "pending"]
    assert after.current_milestone_idx == 1
    assert after.rolling_summary == "第一步完成"
    assert after.total_tokens == 50
    assert after.should_terminate is False
    assert next_phase("evaluator", after) == "planner"


@pytest.mark.unit
async def test_accepting_the_last_milestone_finishes_the_task_as_a_success(
    make_state: MakeState,
) -> None:
    state = worked(make_state, m(1, "completed"), m(2, "in_progress"), idx=1)
    _, after, _ = await run(state, parsed(verdict()))

    assert all(x.status == "completed" for x in after.milestones)
    assert after.current_milestone_idx == 1
    assert after.should_terminate is False  # 成功不是“终止”，否则状态会被映射成 terminated
    assert after.termination_reason == ""
    assert next_phase("evaluator", after) == DONE


@pytest.mark.unit
async def test_an_all_completed_verdict_completes_everything(make_state: MakeState) -> None:
    state = worked(make_state, m(1), m(2), m(3), idx=0)
    _, after, _ = await run(state, parsed(verdict(all_completed=True)))
    assert all(x.status == "completed" for x in after.milestones)
    assert next_phase("evaluator", after) == DONE


@pytest.mark.unit
async def test_accepting_with_no_milestones_synthesizes_a_completed_one(
    make_state: MakeState,
) -> None:
    _, after, _ = await run(worked(make_state), parsed(verdict()))
    assert [x.status for x in after.milestones] == ["completed"]
    assert next_phase("evaluator", after) == DONE


@pytest.mark.unit
async def test_no_extra_message_when_the_last_one_is_already_the_delivery(
    make_state: MakeState,
) -> None:
    update, after, _ = await run(worked(make_state, m(1)), parsed(verdict(summary="摘要")))
    assert "messages" not in update or not update["messages"]
    assert after.messages[-1] == REPLY


@pytest.mark.unit
async def test_a_delivery_message_is_added_when_the_last_message_is_not_one(
    make_state: MakeState,
) -> None:
    state = with_state(
        worked(make_state, m(1)),
        messages=(Message(role="user", content="fix"), Message(role="user", content="提醒")),
    )
    _, after, _ = await run(state, parsed(verdict(summary="任务总结")))
    assert after.messages[-1].role == "assistant"
    assert after.messages[-1].content == "任务总结"


# ==============================================================================
# 打回
# ==============================================================================


@pytest.mark.unit
async def test_a_rejection_reopens_the_milestone_the_planner_claimed_complete(
    make_state: MakeState,
) -> None:
    state = worked(make_state, m(1, "completed"), m(2, "completed"), idx=1)
    _, after, _ = await run(
        state, parsed(verdict(milestone_ok=False, feedback="测试没跑过", summary="尚未验证"))
    )

    assert [x.status for x in after.milestones] == ["completed", "in_progress"]
    assert after.current_milestone_idx == 1
    last = after.messages[-1]
    assert last.role == "user"
    assert last.content.startswith(REJECTION_MARK)
    assert "测试没跑过" in last.content
    assert after.should_terminate is False
    assert next_phase("evaluator", after) == "planner"


@pytest.mark.unit
async def test_a_rejection_with_nothing_to_say_still_explains_itself(
    make_state: MakeState,
) -> None:
    _, after, _ = await run(worked(make_state, m(1)), parsed(verdict(milestone_ok=False)))
    assert after.messages[-1].content.startswith(REJECTION_MARK)
    assert len(after.messages[-1].content) > len(REJECTION_MARK)


@pytest.mark.unit
async def test_a_rejection_does_not_advance_or_record_facts_as_confirmed(
    make_state: MakeState,
) -> None:
    state = worked(make_state, m(1), m(2), idx=0)
    _, after, _ = await run(
        state, parsed(verdict(milestone_ok=False, confirmed_facts=["未经证实的说法"]))
    )
    assert after.current_milestone_idx == 0
    assert after.confirmed_facts == ()


@pytest.mark.unit
async def test_failed_attempts_are_recorded_even_on_rejection(make_state: MakeState) -> None:
    attempt = {"action": "改 a.py", "failure_reason": "测试失败", "conclusion": "思路不对"}
    _, after, _ = await run(
        worked(make_state, m(1)), parsed(verdict(milestone_ok=False, failed_attempts=[attempt]))
    )
    assert after.failed_attempts == (FailedAttempt(**attempt),)


@pytest.mark.unit
async def test_repeated_rejections_end_the_task_instead_of_looping_or_waving_it_through(
    make_state: MakeState,
) -> None:
    rejection = Message(role="user", content=f"{REJECTION_MARK}还不行")
    base = worked(make_state, m(1))
    state = with_state(base, messages=(*base.messages, rejection, rejection, rejection))
    _, after, _ = await run(state, parsed(verdict(milestone_ok=False)), max_rejections=3)

    assert after.should_terminate is True
    assert "验收" in after.termination_reason
    assert after.milestones[0].status != "completed"


@pytest.mark.unit
async def test_the_limit_counts_rejections_not_acceptances(make_state: MakeState) -> None:
    rejection = Message(role="user", content=f"{REJECTION_MARK}还不行")
    base = worked(make_state, m(1))
    state = with_state(base, messages=(*base.messages, rejection, rejection))
    _, after, _ = await run(state, parsed(verdict(milestone_ok=False)), max_rejections=3)
    assert after.should_terminate is False  # 第 3 次打回仍在允许范围内

    _, accepted, _ = await run(
        with_state(base, messages=(*base.messages, rejection, rejection, rejection)),
        parsed(verdict()),
        max_rejections=3,
    )
    assert accepted.should_terminate is False
    assert next_phase("evaluator", accepted) == DONE


# ==============================================================================
# 记忆沉淀
# ==============================================================================


@pytest.mark.unit
async def test_facts_are_merged_without_duplicates_in_order(make_state: MakeState) -> None:
    state = worked(make_state, m(1), confirmed_facts=("事实甲", "事实乙"))
    _, after, _ = await run(state, parsed(verdict(confirmed_facts=["事实乙", "事实丙"])))
    assert after.confirmed_facts == ("事实甲", "事实乙", "事实丙")


@pytest.mark.unit
async def test_an_empty_summary_keeps_the_inherited_one(make_state: MakeState) -> None:
    state = worked(make_state, m(1), rolling_summary="继承来的历史")
    _, after, _ = await run(state, parsed(verdict(summary="")))
    assert after.rolling_summary == "继承来的历史"


@pytest.mark.unit
async def test_failed_attempts_are_appended_without_duplicates(make_state: MakeState) -> None:
    old = FailedAttempt(action="a", failure_reason="r", conclusion="c")
    state = worked(make_state, m(1), failed_attempts=(old,))
    new = {"action": "b", "failure_reason": "r2", "conclusion": "c2"}
    _, after, _ = await run(
        state,
        parsed(verdict(failed_attempts=[old.model_dump(), new])),
    )
    assert [a.action for a in after.failed_attempts] == ["a", "b"]


# ==============================================================================
# 降级：宁严勿松
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["invalid", "truncated", "content_filter"])
async def test_an_unreadable_verdict_counts_as_not_achieved(
    make_state: MakeState, reason: str
) -> None:
    state = worked(make_state, m(1, "completed"), rolling_summary="旧摘要", confirmed_facts=("旧",))
    events = Events()
    _, after, _ = await run(state, degraded(reason, '{"milestone_ok": true, "summ'), events=events)

    assert after.milestones[0].status == "in_progress"
    assert next_phase("evaluator", after) == "planner"
    assert after.messages[-1].content.startswith(REJECTION_MARK)
    assert after.rolling_summary == "旧摘要"
    assert after.confirmed_facts == ("旧",)
    assert after.should_terminate is False
    assert events.names() == ["structured_output.degraded"]
    assert events.items[0][1]["stage"] == "evaluator"


@pytest.mark.unit
async def test_the_raw_text_of_a_degraded_verdict_is_never_trusted(
    make_state: MakeState,
) -> None:
    state = worked(make_state, m(1))
    _, after, _ = await run(state, degraded("invalid", "验收通过，milestone_ok = true"))
    assert after.milestones[0].status != "completed"


@pytest.mark.unit
async def test_degraded_verdicts_count_towards_the_rejection_limit(make_state: MakeState) -> None:
    rejection = Message(role="user", content=f"{REJECTION_MARK}x")
    base = worked(make_state, m(1))
    state = with_state(base, messages=(*base.messages, rejection, rejection))
    _, after, _ = await run(state, degraded("invalid", "乱"), max_rejections=2)
    assert after.should_terminate is True


# ==============================================================================
# 模型不可用与安全
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error", "needle"),
    [
        (LLMUnavailableError("所有端点均不可用"), "LLM 不可用"),
        (LLMDeadlineExceeded("截止时间不足"), "截止时间"),
        (LLMBadRequest("请求被拒", context_overflow=True), "上下文"),
    ],
)
async def test_gateway_failures_terminate_instead_of_raising(
    make_state: MakeState, error: Exception, needle: str
) -> None:
    update, after, _ = await run(worked(make_state, m(1, "completed")), error)
    assert after.should_terminate is True
    assert needle in after.termination_reason
    assert set(update) == {"should_terminate", "termination_reason"}


@pytest.mark.unit
async def test_a_canary_in_the_verdict_terminates_without_accepting(
    make_state: MakeState,
) -> None:
    state = worked(make_state, m(1))
    _, after, _ = await run(state, parsed(verdict(summary="token 是 canary-abc"), total=60))
    assert after.should_terminate is True
    assert after.termination_reason == "[SECURITY] Prompt leak detected via canary token"
    assert after.milestones == state.milestones
    assert after.rolling_summary == ""
    assert after.total_tokens == 60
