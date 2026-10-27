"""研究隔离区：没有任何本地权限的子智能体读网页，主 Agent 只收到净化后的强类型报告。

设计文档：docs/06_subagents/01_research_isolation.md
"""

from agent_runtime.subagents.research.models import (
    CodeExample,
    Evidence,
    Finding,
    QueryPlan,
    RawReport,
    ReportLimits,
    ResearchReport,
    ResearchRequest,
    ResearchSource,
    VersionFact,
    build_report,
)
from agent_runtime.subagents.research.render import render_failure, render_report
from agent_runtime.subagents.research.runner import (
    ResearchLimits,
    ResearchRunner,
    SearchClient,
)
from agent_runtime.subagents.research.search_client import SidecarSearchClient
from agent_runtime.subagents.research.tool import DelegateResearchTool

__all__ = [
    "CodeExample",
    "DelegateResearchTool",
    "Evidence",
    "Finding",
    "QueryPlan",
    "RawReport",
    "ReportLimits",
    "ResearchLimits",
    "ResearchReport",
    "ResearchRequest",
    "ResearchRunner",
    "ResearchSource",
    "SearchClient",
    "SidecarSearchClient",
    "VersionFact",
    "build_report",
    "render_failure",
    "render_report",
]
