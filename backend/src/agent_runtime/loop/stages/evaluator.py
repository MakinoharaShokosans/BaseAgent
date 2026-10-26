"""evaluator 阶段：独立验收里程碑并沉淀事实（推理档，结构化输出）。

设计文档：docs/02_runtime/05_planner_evaluator.md §3。让“干活的”自己宣布完工等于没有验收，
所以验收由独立的一次模型调用完成，并且**宁严勿松**：任何无法确认的情形都按未达成处理。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from agent_runtime.core import AgentState, FailedAttempt, Message, Milestone
from agent_runtime.llm import Degraded
from agent_runtime.loop.runtime import Stage
from agent_runtime.loop.stages.common import (
    CANARY_REASON,
    ModelStageDeps,
    Stop,
    ask,
    leaked,
    report_degraded,
    terminate,
)
from agent_runtime.loop.stages.contracts import EvaluatorVerdict
from agent_runtime.loop.stages.planner import DIRECT_ANSWER_MILESTONE
from agent_runtime.loop.stages.prompts import EVALUATOR_INSTRUCTION

#: 打回消息的前缀；同时是“已打回几次”的计数依据，不入状态也不增加字段
REJECTION_MARK: Final = "【验收未通过】"
DEFAULT_MAX_REJECTIONS: Final = 3

_DEFAULT_FEEDBACK: Final = "当前目标尚未达成，请继续推进，并给出可验证的证据。"
_UNREADABLE_FEEDBACK: Final = "验收结论无法解析，按未达成处理。请继续推进，并给出可验证的证据。"
_DEFAULT_DELIVERY: Final = "验收完成。"


def make_evaluator_stage(
    deps: ModelStageDeps, *, max_rejections: int = DEFAULT_MAX_REJECTIONS
) -> Stage:
    """``max_rejections``：累计打回超过它就终止任务。planner → evaluator → planner 这条回路
    不经过 budget_guard，上限只能由这里给出；到上限时终止而不是放行。
    """

    async def evaluator(state: AgentState) -> Mapping[str, object]:
        result = await ask(
            deps,
            state,
            purpose="evaluator",
            instruction=EVALUATOR_INSTRUCTION,
            model=EvaluatorVerdict,
        )
        if isinstance(result, Stop):
            return result.update

        tokens = state.total_tokens + result.usage.total_tokens
        if leaked(result, state.canary_token):
            return terminate(CANARY_REASON, total_tokens=tokens)

        verdict: EvaluatorVerdict | None = None
        if isinstance(result, Degraded):
            await report_degraded(deps, "evaluator", result)
        else:
            verdict = result.value

        if verdict is not None and verdict.milestone_ok:
            return _accept(state, verdict, tokens)

        rejections = _count_rejections(state) + 1
        if rejections > max_rejections:
            return terminate(
                f"验收连续 {rejections} 次未通过，已超过上限 {max_rejections}",
                total_tokens=tokens,
            )
        return _reject(state, verdict, tokens)

    return evaluator


def _count_rejections(state: AgentState) -> int:
    return sum(
        1 for m in state.messages if m.role == "user" and m.content.startswith(REJECTION_MARK)
    )


def _accept(state: AgentState, verdict: EvaluatorVerdict, tokens: int) -> dict[str, object]:
    milestones = state.milestones
    idx = state.current_milestone_idx
    if not milestones:
        milestones = (Milestone(id=1, title=DIRECT_ANSWER_MILESTONE, status="completed"),)
    elif verdict.all_completed:
        milestones = tuple(m.model_copy(update={"status": "completed"}) for m in milestones)
    else:
        milestones = _set_status(milestones, idx, "completed")
    next_idx = min(idx + 1, len(milestones) - 1)
    done = all(m.status == "completed" for m in milestones)

    update: dict[str, object] = {
        "milestones": milestones,
        "current_milestone_idx": next_idx,
        "confirmed_facts": _merge(state.confirmed_facts, verdict.confirmed_facts),
        "failed_attempts": _merge_attempts(state, verdict),
        "total_tokens": tokens,
    }
    if verdict.summary:
        update["rolling_summary"] = verdict.summary
    if done and not _has_delivery(state):
        update["messages"] = [
            Message(role="assistant", content=verdict.summary or _DEFAULT_DELIVERY)
        ]
    return update


def _reject(state: AgentState, verdict: EvaluatorVerdict | None, tokens: int) -> dict[str, object]:
    if verdict is None:
        feedback = _UNREADABLE_FEEDBACK
    else:
        feedback = verdict.feedback or verdict.summary or _DEFAULT_FEEDBACK

    update: dict[str, object] = {
        # 把被主张完成的里程碑重新打开：否则路由会因为“全部完成”而放行
        "milestones": _set_status(state.milestones, state.current_milestone_idx, "in_progress"),
        "messages": [Message(role="user", content=f"{REJECTION_MARK}{feedback}")],
        "total_tokens": tokens,
    }
    if verdict is not None:
        if verdict.summary:
            update["rolling_summary"] = verdict.summary
        update["failed_attempts"] = _merge_attempts(state, verdict)
    return update


def _set_status(
    milestones: tuple[Milestone, ...], index: int, status: str
) -> tuple[Milestone, ...]:
    if not 0 <= index < len(milestones):
        return milestones
    target = milestones[index]
    return tuple(m.model_copy(update={"status": status}) if m is target else m for m in milestones)


def _has_delivery(state: AgentState) -> bool:
    last = state.messages[-1] if state.messages else None
    return (
        last is not None and last.role == "assistant" and bool(last.content) and not last.tool_calls
    )


def _merge(existing: tuple[str, ...], new: list[str]) -> tuple[str, ...]:
    merged = list(existing)
    merged += [fact for fact in new if fact not in merged]
    return tuple(merged)


def _merge_attempts(state: AgentState, verdict: EvaluatorVerdict) -> tuple[FailedAttempt, ...]:
    merged = list(state.failed_attempts)
    for spec in verdict.failed_attempts:
        attempt = FailedAttempt(**spec.model_dump())
        if attempt not in merged:
            merged.append(attempt)
    return tuple(merged)
