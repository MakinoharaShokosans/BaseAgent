"""注册表与派发器。工具协议（``AegisTool``、``ToolResult``）在 agent_runtime.core。

设计文档：docs/04_tools/01_tool_system.md
"""

from tools.core.dispatcher import CallSpec, DispatchedResult, ToolDispatcher
from tools.core.errors import DependencyUnavailableError, ToolRegistryError
from tools.core.registry import ToolRegistry
from tools.core.sidecar import SidecarClient, SidecarReply

__all__ = [
    "CallSpec",
    "DependencyUnavailableError",
    "DispatchedResult",
    "SidecarClient",
    "SidecarReply",
    "ToolDispatcher",
    "ToolRegistry",
    "ToolRegistryError",
]
