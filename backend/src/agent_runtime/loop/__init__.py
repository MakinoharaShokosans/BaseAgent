"""主循环：一个只调模型、执行工具、追加结果的内核，加固定接缝上的护栏。

设计文档：docs/02_runtime/02_main_loop.md、决策 docs/12_decisions/0006。
"""

from agent_runtime.loop.errors import CanaryLeak, LLMCallTimeout, LoopAborted
from agent_runtime.loop.kernel import (
    CommitSink,
    EventSink,
    OutcomeStatus,
    TaskOutcome,
    TaskRuntime,
    ToolStep,
    run,
)
from agent_runtime.loop.model import Invoker, ModelDeps, ModelStep, make_model_step
from agent_runtime.loop.proxy import MCP_CALL, MCP_DESCRIBE, McpProxy, described_marker
from agent_runtime.loop.scratch import SCRATCH_TOOLS, apply_scratch, scratch_tools
from agent_runtime.loop.tools import (
    RAW_ARGS_KEY,
    ApprovalHooks,
    ToolRunnerDeps,
    make_tool_runner,
    turn_index,
)

__all__ = [
    "MCP_CALL",
    "MCP_DESCRIBE",
    "RAW_ARGS_KEY",
    "SCRATCH_TOOLS",
    "ApprovalHooks",
    "CanaryLeak",
    "CommitSink",
    "EventSink",
    "Invoker",
    "LLMCallTimeout",
    "LoopAborted",
    "McpProxy",
    "ModelDeps",
    "ModelStep",
    "OutcomeStatus",
    "TaskOutcome",
    "TaskRuntime",
    "ToolRunnerDeps",
    "ToolStep",
    "apply_scratch",
    "described_marker",
    "make_model_step",
    "make_tool_runner",
    "run",
    "scratch_tools",
    "turn_index",
]
