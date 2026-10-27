"""给每次调用加上"首选端点"：会话里用户选定的模型。

只在调用方没有指定 ``prefer_endpoint`` 时填入（结构化输出的修复重试要回到产生坏输出的端点，
那个优先级更高）。首选端点失败时网关照常按配置顺序降级，所以选择不会让任务因单点不可用而失败。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from pydantic import BaseModel

from agent_runtime.llm.structured import Degraded, Parsed
from agent_runtime.llm.types import LLMRequest, LLMResponse


class _Gateway(Protocol):
    async def invoke(self, request: LLMRequest) -> LLMResponse: ...

    async def invoke_structured[T: BaseModel](
        self, request: LLMRequest, model: type[T]
    ) -> Parsed[T] | Degraded: ...


class PreferredEndpoint:
    def __init__(self, inner: _Gateway, endpoint: str) -> None:
        self._inner = inner
        self._endpoint = endpoint

    def _with(self, request: LLMRequest) -> LLMRequest:
        if request.prefer_endpoint is not None:
            return request
        return replace(request, prefer_endpoint=self._endpoint)

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        return await self._inner.invoke(self._with(request))

    async def invoke_structured[T: BaseModel](
        self, request: LLMRequest, model: type[T]
    ) -> Parsed[T] | Degraded:
        return await self._inner.invoke_structured(self._with(request), model)
