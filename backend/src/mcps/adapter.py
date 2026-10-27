"""把远端工具伪装成本地工具。信任级恒为 ``untrusted``：返回值会被加不可信信封与警示语。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from agent_runtime.core import AegisTool, ToolResult

if TYPE_CHECKING:
    from mcps.manager import McpManager


class McpTool(AegisTool):
    trust = "untrusted"

    def __init__(
        self,
        manager: McpManager,
        *,
        server: str,
        tool: str,
        exposed: str,
        description: str,
        parameters: Mapping[str, Any],
        idempotent: bool,
        timeout_sec: float,
    ) -> None:
        self._manager = manager
        self.server, self.remote_name = server, tool
        self.name = exposed
        self.description = f"[MCP:{server}] {description}".strip()
        self.parameters = parameters
        self.idempotent = idempotent
        self.timeout_sec = timeout_sec

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        return await self._manager.call(self.server, self.remote_name, args)
