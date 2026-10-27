"""``mcp_call`` 代理的解析。设计文档：docs/04_tools/04_mcp.md §5.1、决策 0007。

代理只在工具包装层的第一步被解析成对目标工具的调用，之后权限、审批、指纹、幂等、信封、
超时全部按目标处理；``mcp_call`` 自身不是权限主体。纯函数，只读 transcript。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Final

from agent_runtime.core import AgentState, ToolCall

MCP_CALL: Final = "mcp_call"
MCP_DESCRIBE: Final = "mcp_describe"
PROXY_TOOLS: Final = frozenset({MCP_CALL, MCP_DESCRIBE})


@dataclass(frozen=True, slots=True)
class Resolved:
    """解析后的调用。``error`` 非空表示不派发，直接合成这条失败结果。"""

    name: str
    args: Mapping[str, Any]
    error: str = ""


def described_marker(name: str) -> str:
    """``mcp_describe`` 的结果里每个工具定义的标记；``mcp_call`` 的前置条件据此判断。"""
    return f'<mcp_tool name="{name}"'


class McpProxy:
    def __init__(self, deferred: Collection[str], pinned: Collection[str]) -> None:
        self._deferred = frozenset(deferred)
        self._pinned = frozenset(pinned)

    @property
    def deferred(self) -> frozenset[str]:
        return self._deferred

    def resolve(self, state: AgentState, call: ToolCall) -> Resolved:
        if call.name != MCP_CALL:
            return Resolved(call.name, call.args)
        target = call.args.get("name")
        arguments = call.args.get("arguments", {})
        if not isinstance(target, str) or not target:
            return Resolved(call.name, call.args, "mcp_call 需要 name（字符串）")
        if not isinstance(arguments, Mapping):
            return Resolved(target, {}, "mcp_call 的 arguments 必须是对象")
        if target in self._pinned:
            return Resolved(target, {}, f"{target} 已在工具列表里，请直接调用，不要经 mcp_call")
        if target not in self._deferred:
            return Resolved(target, {}, f"索引里没有工具 {target}；请核对索引中的名字")
        if not _described(state, target):
            return Resolved(
                target, {}, f"本任务还没有取过 {target} 的参数定义，请先 mcp_describe 再调用"
            )
        return Resolved(target, dict(arguments))  # pyright: ignore[reportUnknownArgumentType]


def _described(state: AgentState, target: str) -> bool:
    marker = described_marker(target)
    return any(
        m.role == "tool" and m.name == MCP_DESCRIBE and marker in m.content for m in state.messages
    )
