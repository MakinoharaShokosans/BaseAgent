"""budget_guard 阶段：硬熔断检查与软告警，无模型调用。

设计文档：docs/02_runtime/02_main_loop.md §1、§5。
"""

from __future__ import annotations

from collections.abc import Mapping

from agent_runtime.core import AgentState, Message
from agent_runtime.guardrails import PhysicalBudgetGuard
from agent_runtime.loop.runtime import Stage


def make_budget_guard_stage(guard: PhysicalBudgetGuard) -> Stage:
    async def budget_guard(state: AgentState) -> Mapping[str, object]:
        verdict, usage = guard.assess(state.step_count, state.total_tokens)
        if verdict.exhausted:
            return {
                "should_terminate": True,
                "termination_reason": verdict.reason(guard.limits, usage),
            }
        if verdict.warnings:
            text = verdict.warning_text(guard.limits, usage)
            return {"messages": [Message(role="user", content=text)]}
        return {}

    return budget_guard
