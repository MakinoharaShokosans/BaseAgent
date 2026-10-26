"""executor 阶段：把规划者的指令变成具体的工具调用（快速档）。

设计文档：docs/02_runtime/02_main_loop.md §1-2。每轮 ``step_count`` 加一：``budget_guard`` 的
终止保证依赖这一点（见该文档 §5.1）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from agent_runtime.core import AgentState, Message, ToolCall, ToolSchema
from agent_runtime.llm import LLMRequest, LLMResponse
from agent_runtime.loop.runtime import Stage
from agent_runtime.loop.stages.common import (
    CANARY_REASON,
    Assembler,
    Stop,
    guarded,
    response_leaked,
    terminate,
)
from agent_runtime.loop.stages.prompts import EXECUTOR_INSTRUCTION

#: 参数无法使用的调用的标记：``args`` 只含这一个键，值是模型给出的原文。
#: 网关再次发给模型时原样送回（见 llm/wire.py），tool_runner 据此不执行。
RAW_ARGS_KEY: Final = "_raw"


class Invoker(Protocol):
    """``LLMGateway`` 满足它；测试用假实现。"""

    async def invoke(self, request: LLMRequest) -> LLMResponse: ...


@dataclass(frozen=True, slots=True)
class ExecutorDeps:
    gateway: Invoker
    assemble: Assembler
    #: 本任务的主工具集：会话内应保持固定（前缀缓存），按名称排序
    tools: Sequence[ToolSchema]
    deadline: Callable[[], float | None] = lambda: None


def make_executor_stage(deps: ExecutorDeps) -> Stage:
    async def executor(state: AgentState) -> Mapping[str, object]:
        request = LLMRequest(
            tier="fast",
            messages=tuple(deps.assemble(state, EXECUTOR_INSTRUCTION)),
            tools=tuple(deps.tools) or None,
            purpose="executor",
            deadline=deps.deadline(),
        )
        result = await guarded("executor", deps.gateway.invoke(request))
        if isinstance(result, Stop):
            return result.update

        tokens = state.total_tokens + result.usage.total_tokens
        if response_leaked(result, state.canary_token):
            # 带金丝雀的调用不得进入状态，更不会被执行
            return terminate(CANARY_REASON, total_tokens=tokens)

        message = _unusable_calls_marked(result)
        return {
            "messages": [message],
            "step_count": state.step_count + 1,
            "total_tokens": tokens,
        }

    return executor


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
