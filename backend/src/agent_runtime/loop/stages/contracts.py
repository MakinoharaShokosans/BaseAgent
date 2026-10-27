"""planner / evaluator 的输出契约：各自场景的唯一权威定义。

设计文档：docs/02_runtime/05_planner_evaluator.md §1；容忍与截断规则见
docs/07_llm_gateway/02_structured_output.md §3.2。过长的文本截断而不是拒绝；枚举字段用字面量白名单。
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import AfterValidator, AliasChoices, BaseModel, BeforeValidator, Field

MAX_MILESTONES: Final = 12
MAX_FACTS: Final = 20
MAX_FAILED_ATTEMPTS: Final = 10

_TITLE: Final = 100
_DESCRIPTION: Final = 500
_THOUGHT: Final = 2000
_DIRECTIVE: Final = 2000
_DELIVERY: Final = 20000
_SUMMARY: Final = 500
_FEEDBACK: Final = 500
_ITEM: Final = 300


def _clip(limit: int) -> AfterValidator:
    return AfterValidator(lambda text: text.strip()[:limit])


def _non_blank(text: str) -> str:
    if not text.strip():
        raise ValueError("不能为空")
    return text


def _wrap_single(value: object) -> object:
    return [value] if isinstance(value, str) else value


def _clean_facts(items: list[str]) -> list[str]:
    cleaned = [item.strip()[:_ITEM] for item in items if item.strip()]
    return cleaned[:MAX_FACTS]


# ==============================================================================
# planner
# ==============================================================================


class MilestoneSpec(BaseModel):
    title: Annotated[str, AfterValidator(_non_blank), _clip(_TITLE)]
    description: Annotated[str, _clip(_DESCRIPTION)] = ""


class MilestoneUpdate(BaseModel):
    id: int
    #: pending 不在白名单里：规划只能推进状态，不能把进度倒回去
    status: Literal["in_progress", "completed", "failed"]


class PlannerVerdict(BaseModel):
    thought: Annotated[str, _clip(_THOUGHT)]
    #: 别名 action / next_action 是旧版提示词遗留的漂移，计划在提示词全部切换后删除
    next_step: Annotated[str, _clip(_DIRECTIVE)] = Field(
        default="", validation_alias=AliasChoices("next_step", "action", "next_action")
    )
    #: 只在首次规划（状态里还没有里程碑）时采用
    milestones: Annotated[
        list[MilestoneSpec], AfterValidator(lambda items: items[:MAX_MILESTONES])
    ] = []
    milestone_updates: list[MilestoneUpdate] = []
    all_completed: bool = False
    #: 面向用户的交付文本；宣布完成时使用
    direct_response: Annotated[str, _clip(_DELIVERY)] = ""
    #: 客观上无法继续（操作被拒绝、缺少必要信息、环境限制）：体面地结束，不要反复绕
    blocked: bool = False
    blocked_reason: Annotated[str, _clip(_FEEDBACK)] = ""


# ==============================================================================
# evaluator
# ==============================================================================


class FailedAttemptSpec(BaseModel):
    action: Annotated[str, _clip(_ITEM)]
    failure_reason: Annotated[str, _clip(_ITEM)]
    conclusion: Annotated[str, _clip(_ITEM)]


class EvaluatorVerdict(BaseModel):
    #: 没有默认值：缺少明确结论就不是结论，走修复重试，仍缺则按未达成处理
    milestone_ok: bool
    all_completed: bool = False
    summary: Annotated[str, _clip(_SUMMARY)] = ""
    #: 未通过时写给 planner 看的原因
    feedback: Annotated[str, _clip(_FEEDBACK)] = ""
    confirmed_facts: Annotated[
        list[str], BeforeValidator(_wrap_single), AfterValidator(_clean_facts)
    ] = []
    failed_attempts: Annotated[
        list[FailedAttemptSpec], AfterValidator(lambda items: items[:MAX_FAILED_ATTEMPTS])
    ] = []
    #: 执行者已说明受阻且原因成立（如操作被人工拒绝）：别再打回，体面地结束
    blocked: bool = False
    blocked_reason: Annotated[str, _clip(_FEEDBACK)] = ""
