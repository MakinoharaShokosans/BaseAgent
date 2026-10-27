"""MCP 接入：管理器、适配器、消毒、命名规范化。

设计文档：docs/04_tools/04_mcp.md
"""

from mcps.adapter import McpTool
from mcps.manager import McpManager, resolve_env, unpinned_package
from mcps.naming import assign_names
from mcps.vetting import Rejection, Vetted, vet_tool

__all__ = [
    "McpManager",
    "McpTool",
    "Rejection",
    "Vetted",
    "assign_names",
    "resolve_env",
    "unpinned_package",
    "vet_tool",
]
