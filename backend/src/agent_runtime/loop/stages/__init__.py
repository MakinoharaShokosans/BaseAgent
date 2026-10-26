"""五个阶段：planner / budget_guard / executor / tool_runner / evaluator。

全部已实现。设计文档：docs/02_runtime/02_main_loop.md、05_planner_evaluator.md
"""

from agent_runtime.loop.stages.budget_guard import make_budget_guard_stage
from agent_runtime.loop.stages.common import ModelStageDeps
from agent_runtime.loop.stages.evaluator import make_evaluator_stage
from agent_runtime.loop.stages.executor import ExecutorDeps, make_executor_stage
from agent_runtime.loop.stages.planner import make_planner_stage
from agent_runtime.loop.stages.tool_runner import (
    ApprovalHooks,
    ToolRunnerDeps,
    make_tool_runner_stage,
)

__all__ = [
    "ApprovalHooks",
    "ExecutorDeps",
    "ModelStageDeps",
    "ToolRunnerDeps",
    "make_budget_guard_stage",
    "make_evaluator_stage",
    "make_executor_stage",
    "make_planner_stage",
    "make_tool_runner_stage",
]
