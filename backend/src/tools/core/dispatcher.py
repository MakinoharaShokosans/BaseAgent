"""并发派发器。设计文档：04_tools/01_tool_system.md §7。

不做权限判定、预算记账、观察值裁剪：那些属于 ``tool_runner``。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

from agent_runtime.core import AegisTool, ToolResult, Trust
from tools.core.registry import ToolRegistry

#: (tool_call_id, 工具名, 参数)
CallSpec = tuple[str, str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class DispatchedResult:
    tool_call_id: str
    tool_name: str
    result: ToolResult
    #: 生效的信任级：调用期覆盖 > 工具的静态声明 > 最保守值
    trust: Trust


class ToolDispatcher:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def knows(self, name: str) -> bool:
        return self._registry.knows(name)

    async def dispatch(self, calls: Sequence[CallSpec]) -> list[DispatchedResult]:
        """同一批并发执行，返回顺序与输入一致；任何调用都产生且只产生一个结果。"""
        return list(await asyncio.gather(*(self._one(*call) for call in calls)))

    async def _one(self, call_id: str, name: str, args: Mapping[str, Any]) -> DispatchedResult:
        tool = self._registry.get(name)
        if tool is None:
            # 副作用不可静态推理，保守地按不可信处理
            return DispatchedResult(
                call_id, name, ToolResult.failure(f"未知工具：{name}"), "untrusted"
            )
        began = time.monotonic()
        result = await self._invoke(tool, args)
        elapsed = round((time.monotonic() - began) * 1000)
        if not result.duration_ms:
            result = result.model_copy(update={"duration_ms": elapsed})
        return DispatchedResult(call_id, name, result, result.trust or tool.trust)

    async def _invoke(self, tool: AegisTool, args: Mapping[str, Any]) -> ToolResult:
        try:
            return await asyncio.wait_for(tool.invoke(args), timeout=tool.timeout_sec)
        except TimeoutError:
            return ToolResult.failure(f"工具 {tool.name} 执行超时（{tool.timeout_sec:g} 秒）")
        except Exception as exc:  # 工具实现者漏接的异常不得中断主循环
            logger.exception("工具 {} 抛出了未处理的异常", tool.name)
            return ToolResult.failure(f"工具 {tool.name} 执行出错：{type(exc).__name__}: {exc}")
