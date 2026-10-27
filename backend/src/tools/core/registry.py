"""工具注册表：名字到实例的映射与查询，不含执行逻辑。设计文档：04_tools/01_tool_system.md §5。"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping

from agent_runtime.core import AgentTool, ToolSchema
from tools.core.errors import ToolRegistryError


class ToolRegistry:
    """构造期校验，想犯错也犯不了：

    - 空名、重名一律拒绝（重名通常意味着装配逻辑有 bug，不静默覆盖）。
    - 不可信工具默认拒绝；``allow_untrusted`` 为真时，若给了逐名授权清单，清单外的也拒绝。
    """

    def __init__(
        self,
        tools: Iterable[AgentTool],
        *,
        allow_untrusted: bool = False,
        untrusted_allowlist: Collection[str] | None = None,
    ) -> None:
        registered: dict[str, AgentTool] = {}
        for tool in tools:
            name = tool.name
            if not name:
                raise ToolRegistryError("工具名不能为空")
            if name in registered:
                raise ToolRegistryError(f"工具名重复：{name}")
            if tool.trust == "untrusted":
                if not allow_untrusted:
                    raise ToolRegistryError(f"不可信工具 {name} 不能进入这个注册表")
                if untrusted_allowlist is not None and name not in untrusted_allowlist:
                    raise ToolRegistryError(f"不可信工具 {name} 没有逐名授权")
            registered[name] = tool
        self._tools: Mapping[str, AgentTool] = registered

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def knows(self, name: str) -> bool:
        return name in self._tools

    def is_idempotent(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool is not None and tool.idempotent

    def schemas(self) -> tuple[ToolSchema, ...]:
        """按名称排序：工具 Schema 是请求前缀的一部分，顺序必须确定（§10.1）。"""
        return tuple(self._tools[name].schema() for name in sorted(self._tools))
