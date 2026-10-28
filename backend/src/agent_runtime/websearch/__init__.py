"""websearch 工具：没有任何本地权限的子智能体读网页，主 Agent 只收到净化后的强类型报告。

设计文档：docs/04_tools/07_websearch_tool.md
"""

from agent_runtime.websearch.models import (
    CodeExample,
    Evidence,
    Finding,
    QueryPlan,
    RawReport,
    ReportLimits,
    VersionFact,
    WebSearchReport,
    WebSearchRequest,
    WebSearchSource,
    build_report,
)
from agent_runtime.websearch.render import render_failure, render_report
from agent_runtime.websearch.runner import (
    SearchClient,
    WebSearchLimits,
    WebSearchRunner,
)
from agent_runtime.websearch.search_client import SidecarSearchClient
from agent_runtime.websearch.tool import WebSearchTool

__all__ = [
    "CodeExample",
    "Evidence",
    "Finding",
    "QueryPlan",
    "RawReport",
    "ReportLimits",
    "SearchClient",
    "SidecarSearchClient",
    "VersionFact",
    "WebSearchLimits",
    "WebSearchReport",
    "WebSearchRequest",
    "WebSearchRunner",
    "WebSearchSource",
    "WebSearchTool",
    "build_report",
    "render_failure",
    "render_report",
]
