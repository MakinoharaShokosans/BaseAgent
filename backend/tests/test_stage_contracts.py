"""planner / evaluator 的输出契约。出处：docs/02_runtime/05_planner_evaluator.md、
docs/07_llm_gateway/02_structured_output.md §3.2。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agent_runtime.loop.stages.contracts import (
    MAX_FACTS,
    MAX_MILESTONES,
    EvaluatorVerdict,
    PlannerVerdict,
)

# ==============================================================================
# PlannerVerdict
# ==============================================================================


@pytest.mark.unit
def test_a_minimal_planner_verdict_fills_defaults() -> None:
    verdict = PlannerVerdict.model_validate({"thought": "t"})
    assert verdict.next_step == ""
    assert verdict.milestones == []
    assert verdict.milestone_updates == []
    assert verdict.all_completed is False
    assert verdict.direct_response == ""


@pytest.mark.unit
def test_thought_is_required() -> None:
    with pytest.raises(ValidationError):
        PlannerVerdict.model_validate({"next_step": "读文件"})


@pytest.mark.unit
@pytest.mark.parametrize("name", ["next_step", "action", "next_action"])
def test_known_aliases_for_next_step_are_collected_in_the_model(name: str) -> None:
    assert PlannerVerdict.model_validate({"thought": "t", name: "做"}).next_step == "做"


@pytest.mark.unit
def test_unknown_fields_are_ignored() -> None:
    PlannerVerdict.model_validate({"thought": "t", "surprise": {"a": 1}})


@pytest.mark.unit
def test_harmless_deviations_are_tolerated() -> None:
    verdict = PlannerVerdict.model_validate({"thought": "t", "all_completed": "true"})
    assert verdict.all_completed is True


@pytest.mark.unit
def test_a_milestone_update_status_is_a_whitelist() -> None:
    with pytest.raises(ValidationError):
        PlannerVerdict.model_validate(
            {"thought": "t", "milestone_updates": [{"id": 1, "status": "pending; rm -rf /"}]}
        )
    with pytest.raises(ValidationError):  # pending 不能被“更新”回去
        PlannerVerdict.model_validate(
            {"thought": "t", "milestone_updates": [{"id": 1, "status": "pending"}]}
        )
    ok = PlannerVerdict.model_validate(
        {"thought": "t", "milestone_updates": [{"id": 1, "status": "completed"}]}
    )
    assert ok.milestone_updates[0].status == "completed"


@pytest.mark.unit
def test_milestone_ids_in_updates_must_be_integers() -> None:
    with pytest.raises(ValidationError):
        PlannerVerdict.model_validate(
            {"thought": "t", "milestone_updates": [{"id": "第一个", "status": "completed"}]}
        )


@pytest.mark.unit
def test_too_many_milestones_are_truncated_not_rejected() -> None:
    specs = [{"title": f"m{i}"} for i in range(MAX_MILESTONES + 5)]
    verdict = PlannerVerdict.model_validate({"thought": "t", "milestones": specs})
    assert len(verdict.milestones) == MAX_MILESTONES
    assert verdict.milestones[0].title == "m0"


@pytest.mark.unit
def test_overlong_titles_are_truncated() -> None:
    verdict = PlannerVerdict.model_validate(
        {"thought": "t", "milestones": [{"title": "题" * 500, "description": "述" * 5000}]}
    )
    assert len(verdict.milestones[0].title) <= 100
    assert len(verdict.milestones[0].description) <= 500


@pytest.mark.unit
def test_a_milestone_needs_a_title() -> None:
    with pytest.raises(ValidationError):
        PlannerVerdict.model_validate({"thought": "t", "milestones": [{"description": "d"}]})
    with pytest.raises(ValidationError):
        PlannerVerdict.model_validate({"thought": "t", "milestones": [{"title": "   "}]})


# ==============================================================================
# EvaluatorVerdict
# ==============================================================================


def evaluator(**fields: Any) -> EvaluatorVerdict:
    return EvaluatorVerdict.model_validate({"milestone_ok": True, **fields})


@pytest.mark.unit
def test_the_verdict_itself_is_required() -> None:
    """验收闸门：没有明确结论就不是结论。"""
    with pytest.raises(ValidationError):
        EvaluatorVerdict.model_validate({"summary": "做完了"})


@pytest.mark.unit
@pytest.mark.parametrize(("raw", "expected"), [(True, True), ("true", True), ("false", False)])
def test_the_verdict_accepts_boolean_like_values(raw: Any, expected: bool) -> None:
    assert evaluator(milestone_ok=raw).milestone_ok is expected


@pytest.mark.unit
def test_a_nonsense_verdict_is_rejected_not_guessed() -> None:
    with pytest.raises(ValidationError):
        evaluator(milestone_ok="也许")


@pytest.mark.unit
def test_a_single_fact_string_is_wrapped_into_a_list() -> None:
    assert evaluator(confirmed_facts="只有一条").confirmed_facts == ["只有一条"]


@pytest.mark.unit
def test_facts_and_summary_are_bounded_by_truncation() -> None:
    verdict = evaluator(
        summary="摘" * 5000,
        confirmed_facts=["事" * 1000] * (MAX_FACTS + 10),
        failed_attempts=[{"action": "a" * 900, "failure_reason": "r", "conclusion": "c"}],
    )
    assert len(verdict.summary) <= 500
    assert len(verdict.confirmed_facts) == MAX_FACTS
    assert all(len(f) <= 300 for f in verdict.confirmed_facts)
    assert len(verdict.failed_attempts[0].action) <= 300


@pytest.mark.unit
def test_blank_facts_are_dropped() -> None:
    assert evaluator(confirmed_facts=["有", "  ", ""]).confirmed_facts == ["有"]


@pytest.mark.unit
def test_failed_attempts_need_all_three_parts() -> None:
    with pytest.raises(ValidationError):
        evaluator(failed_attempts=[{"action": "a"}])
