"""路由表：下一个阶段完全由当前阶段结束后的状态决定，全是纯函数。

设计文档：docs/02_runtime/02_main_loop.md §2。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from agent_runtime.core import DONE, AgentState, NextPhase, Phase

Router = Callable[[AgentState], NextPhase]

#: 文档状态图里画出的边；测试用它断言路由函数不会走到图外
ALLOWED_TRANSITIONS: Final[Mapping[Phase, frozenset[NextPhase]]] = {
    "planner": frozenset({"budget_guard", "evaluator", DONE}),
    "budget_guard": frozenset({"executor", DONE}),
    "executor": frozenset({"tool_runner", "evaluator", DONE}),
    "tool_runner": frozenset({"planner", "budget_guard", DONE}),
    "evaluator": frozenset({"planner", DONE}),
}


def all_milestones_completed(state: AgentState) -> bool:
    """空里程碑视为未完成：单步任务不能因为“没有计划”就跳过执行。"""
    return bool(state.milestones) and all(m.status == "completed" for m in state.milestones)


def route_after_planner(state: AgentState) -> NextPhase:
    if state.should_terminate:
        return DONE
    if all_milestones_completed(state):
        # 没做过任何一步，说明 planner 已直接答复，无需复核
        return DONE if state.step_count == 0 else "evaluator"
    return "budget_guard"


def route_after_budget_guard(state: AgentState) -> NextPhase:
    return DONE if state.should_terminate else "executor"


def route_after_executor(state: AgentState) -> NextPhase:
    if state.should_terminate:
        return DONE
    last = state.messages[-1] if state.messages else None
    if last is not None and last.role == "assistant" and last.tool_calls:
        return "tool_runner"
    return "evaluator"  # 没有工具调用：给出了自然语言结论，交给 evaluator 验收


def route_after_tool_runner(state: AgentState) -> NextPhase:
    """默认直接继续执行；tool_runner 标记了需要重规划才回 planner。"""
    if state.should_terminate:
        return DONE
    return "planner" if state.replan_needed else "budget_guard"


def route_after_evaluator(state: AgentState) -> NextPhase:
    if state.should_terminate or all_milestones_completed(state):
        return DONE
    return "planner"


ROUTES: Final[Mapping[Phase, Router]] = {
    "planner": route_after_planner,
    "budget_guard": route_after_budget_guard,
    "executor": route_after_executor,
    "tool_runner": route_after_tool_runner,
    "evaluator": route_after_evaluator,
}


def next_phase(phase: Phase, state: AgentState) -> NextPhase:
    return ROUTES[phase](state)
