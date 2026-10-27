"""动态委派：受限的子劳动力，能力衰减而非能力授予。

设计文档：docs/06_subagents/02_dynamic_delegation.md
"""

from agent_runtime.subagents.delegate.render import render_report
from agent_runtime.subagents.delegate.report import (
    AccessSet,
    Citation,
    DelegateLimits,
    RawSubagentReport,
    SubagentFinding,
    SubagentReport,
    build_report,
)
from agent_runtime.subagents.delegate.runner import RunnerLimits, SubagentRunner
from agent_runtime.subagents.delegate.tool import SpawnSubagentTool

__all__ = [
    "AccessSet",
    "Citation",
    "DelegateLimits",
    "RawSubagentReport",
    "RunnerLimits",
    "SpawnSubagentTool",
    "SubagentFinding",
    "SubagentReport",
    "SubagentRunner",
    "build_report",
    "render_report",
]
