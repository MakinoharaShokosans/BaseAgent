"""模型接缝：把状态变成一次模型调用，返回助手消息。

没有阶段指令，没有预算截止时间；只有两道护栏：金丝雀检测（模型输出里出现金丝雀即
``CanaryLeak``）和单次调用超时。模型错误（含余额不足）原样向上传播，任务以 ``failed`` 结束。
设计文档：docs/02_runtime/02_main_loop.md §2、§4。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from agent_runtime.core import AgentState, Message, ToolCall, ToolSchema
from agent_runtime.guardrails import detect_canary_leak
from agent_runtime.llm import LLMRequest, LLMResponse
from agent_runtime.llm.types import Effort, Tier
from agent_runtime.loop.errors import CanaryLeak, LLMCallTimeout
from agent_runtime.loop.tools import RAW_ARGS_KEY

ModelStep = Callable[[AgentState], Awaitable[Message]]


class Invoker(Protocol):
    """``LLMGateway`` 满足它；测试用假实现。"""

    async def invoke(self, request: LLMRequest) -> LLMResponse: ...


@dataclass(frozen=True, slots=True)
class ModelDeps:
    gateway: Invoker
    assemble: Callable[[AgentState], Sequence[Message]]
    #: 本任务的主工具集：会话内应保持固定（前缀缓存），按名称排序
    tools: Sequence[ToolSchema]
    tier: Tier = "reasoning"
    #: 单次模型调用的超时秒数；None 表示不限
    call_timeout_sec: float | None = None
    reasoning_effort: Effort | None = None
    #: 流式增量；None 表示等整条响应
    on_delta: Callable[[str, str], Awaitable[None]] | None = None


def make_model_step(deps: ModelDeps) -> ModelStep:
    async def model(state: AgentState) -> Message:
        request = LLMRequest(
            tier=deps.tier,
            messages=tuple(deps.assemble(state)),
            tools=tuple(deps.tools) or None,
            purpose="agent",
            reasoning_effort=deps.reasoning_effort,
            stream=deps.on_delta is not None,
            on_delta=deps.on_delta,
        )
        try:
            async with asyncio.timeout(deps.call_timeout_sec):
                response = await deps.gateway.invoke(request)
        except TimeoutError as exc:
            raise LLMCallTimeout(f"模型调用超过 {deps.call_timeout_sec:g} 秒") from exc

        message = response.message
        if detect_canary_leak(
            [message.content, [c.args for c in message.tool_calls]], state.canary_token
        ):
            raise CanaryLeak()  # 带金丝雀的回复不得进入状态，更不会被执行
        return _unusable_calls_marked(response)

    return model


def _unusable_calls_marked(response: LLMResponse) -> Message:
    """格式错误或可能被截断的调用不能当作普通参数执行：把参数换成原文标记。

    被长度截断时参数可能恰好是合法 JSON，却不完整，所以也一并标记。
    """
    message = response.message
    if not response.malformed_calls:
        return message
    calls = tuple(
        _as_raw(call) if call.id in response.malformed_calls else call
        for call in message.tool_calls
    )
    return Message(
        role="assistant",
        content=message.content,
        tool_calls=calls,
        reasoning=message.reasoning,
        reasoning_state=message.reasoning_state,
        produced_by=message.produced_by,
    )


def _as_raw(call: ToolCall) -> ToolCall:
    if set(call.args) == {RAW_ARGS_KEY} and isinstance(call.args[RAW_ARGS_KEY], str):
        return call
    return ToolCall(
        id=call.id,
        name=call.name,
        args={RAW_ARGS_KEY: json.dumps(dict(call.args), ensure_ascii=False)},
    )
