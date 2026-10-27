"""planner 阶段：宏观规划与重规划（推理档，结构化输出）。

设计文档：docs/02_runtime/05_planner_evaluator.md §2。planner 不调用工具：它只产出
“要做什么”的指令与里程碑状态，具体的工具调用交给 executor。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from agent_runtime.core import AgentState, Message, Milestone
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
from agent_runtime.loop.stages.contracts import MAX_MILESTONES, PlannerVerdict
from agent_runtime.loop.stages.prompts import PLANNER_INSTRUCTION

#: 受阻而结束的终止原因前缀（planner 与 evaluator 共用）
BLOCKED_PREFIX: Final = "受阻："
GENERIC_DIRECTIVE: Final = "继续推进当前里程碑。"
DIRECT_ANSWER_MILESTONE: Final = "响应用户咨询与交付任务"
_FALLBACK_DELIVERY: Final = "任务已达成。"
_RAW_DIRECTIVE_LIMIT: Final = 4000


def make_planner_stage(deps: ModelStageDeps, *, max_degradations: int = 3) -> Stage:
    """``max_degradations``：planner 连续降级超过它就终止任务，不让空转一直烧到预算用完。"""

    async def planner(state: AgentState) -> Mapping[str, object]:
        result = await ask(
            deps,
            state,
            purpose="planner",
            instruction=PLANNER_INSTRUCTION,
            model=PlannerVerdict,
        )
        if isinstance(result, Stop):
            return result.update

        tokens = state.total_tokens + result.usage.total_tokens
        if leaked(result, state.canary_token):
            return terminate(CANARY_REASON, total_tokens=tokens)

        if isinstance(result, Degraded):
            await report_degraded(deps, "planner", result)
            if result.reason == "content_filter":
                return terminate("模型输出被内容过滤拦截", total_tokens=tokens)
            streak = state.planner_degraded + 1
            if streak > max_degradations:
                return terminate(
                    f"规划连续 {streak} 次没有得到可用的结论，已超过上限 {max_degradations}",
                    total_tokens=tokens,
                    planner_degraded=streak,
                )
            # 继续推进比卡住更有价值；里程碑保持不变，也绝不据此宣布完成
            message = Message(role="assistant", content=_degraded_directive(result))
            return {
                "messages": [message],
                "total_tokens": tokens,
                "planner_degraded": streak,
                "replan_needed": False,
                "steps_since_plan": 0,
            }

        return plan_update(state, result.value, tokens)

    return planner


def plan_update(state: AgentState, verdict: PlannerVerdict, tokens: int) -> dict[str, object]:
    """把一份通过校验的规划结论折成状态增量（纯函数）。"""
    milestones = _apply_milestones(state.milestones, verdict)
    if verdict.blocked:
        reason = verdict.blocked_reason or verdict.thought or "无法继续推进"
        return {
            "milestones": milestones,
            "total_tokens": tokens,
            "should_terminate": True,
            "termination_reason": f"{BLOCKED_PREFIX}{reason}",
            "replan_needed": False,
            "steps_since_plan": 0,
            "planner_degraded": 0,
        }
    next_step = verdict.next_step.strip()
    delivery = verdict.direct_response.strip()

    claimed = verdict.all_completed or (bool(milestones) and _all_completed(milestones))
    # 还有指令要执行，说明没做完：以未完成为准
    done = claimed and not next_step
    # 还没动手就宣布完成，必须带着答案，否则无物可交付
    if done and state.step_count == 0 and not delivery:
        done = False

    if done:
        if milestones:
            milestones = tuple(m.model_copy(update={"status": "completed"}) for m in milestones)
        else:
            milestones = (Milestone(id=1, title=DIRECT_ANSWER_MILESTONE, status="completed"),)
        content = delivery or verdict.thought or _FALLBACK_DELIVERY
    else:
        if milestones and _all_completed(milestones):
            # 状态更新把它们全标完成了，但本轮并未被接受为完成：重新打开最后一个，防止路由放行
            last = milestones[-1]
            milestones = (*milestones[:-1], last.model_copy(update={"status": "in_progress"}))
        content = next_step or verdict.thought or GENERIC_DIRECTIVE

    return {
        "messages": [Message(role="assistant", content=content)],
        "milestones": milestones,
        "total_tokens": tokens,
        "replan_needed": False,
        "steps_since_plan": 0,
        "planner_degraded": 0,
    }


def _apply_milestones(
    existing: tuple[Milestone, ...], verdict: PlannerVerdict
) -> tuple[Milestone, ...]:
    if not existing:
        return tuple(
            Milestone(id=i, title=spec.title, description=spec.description)
            for i, spec in enumerate(verdict.milestones[:MAX_MILESTONES], start=1)
        )
    status_by_id = {u.id: u.status for u in verdict.milestone_updates}
    return tuple(
        m.model_copy(update={"status": status_by_id[m.id]}) if m.id in status_by_id else m
        for m in existing
    )


def _all_completed(milestones: tuple[Milestone, ...]) -> bool:
    return all(m.status == "completed" for m in milestones)


def _degraded_directive(result: Degraded) -> str:
    """降级时把原文当作指令；但像 JSON 的残片、被截断的输出不能原样喂给 executor。"""
    raw = result.raw_text.strip()
    if result.reason == "invalid" and raw and not raw.startswith(("{", "[", "```")):
        return raw[:_RAW_DIRECTIVE_LIMIT]
    return GENERIC_DIRECTIVE
