"""planner / evaluator 阶段测试共用的假网关与构造器。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from agent_runtime.core import AgentState, Message, apply_update
from agent_runtime.llm import (
    Degraded,
    LLMRequest,
    LLMResponse,
    Parsed,
    TokenUsage,
)
from agent_runtime.loop.stages.common import Emit, ModelStageDeps

MakeState = Callable[..., AgentState]


def response(content: str = "{}", total: int = 100, finish: str = "stop") -> LLMResponse:
    usage = TokenUsage(prompt_tokens=total - 10, completion_tokens=10, total_tokens=total)
    return LLMResponse(
        message=Message(role="assistant", content=content, produced_by="r"),
        usage=usage,
        endpoint="r",
        model="m",
        finish_reason=finish,
        attempts=1,
        duration_ms=1,
        structured_path="json_object",
    )


def parsed(value: BaseModel, total: int = 100, content: str | None = None) -> Parsed[Any]:
    text = content if content is not None else json.dumps(value.model_dump(), ensure_ascii=False)
    resp = response(text, total)
    return Parsed(value=value, response=resp, usage=resp.usage, rounds=1)


def degraded(reason: str, raw: str = "", total: int = 100) -> Degraded:
    resp = response(raw, total)
    return Degraded(
        reason=reason,  # type: ignore[arg-type]
        raw_text=raw,
        problems=("缺少字段 `thought`",),
        response=resp,
        usage=resp.usage,
        rounds=2,
    )


class FakeGateway:
    """按顺序给出剧本里的结果或抛出异常。"""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[tuple[LLMRequest, type[BaseModel]]] = []

    async def invoke_structured(self, request: LLMRequest, model: type[BaseModel]) -> Any:
        self.requests.append((request, model))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, Mapping[str, object]]] = []

    async def __call__(self, name: str, payload: Mapping[str, object]) -> None:
        self.items.append((name, payload))

    def names(self) -> list[str]:
        return [n for n, _ in self.items]


def assemble(state: AgentState, instruction: str) -> Sequence[Message]:
    return (Message(role="system", content=instruction), *state.messages)


def deps(gateway: FakeGateway, events: Emit | None = None, **kw: Any) -> ModelStageDeps:
    return ModelStageDeps(
        gateway=gateway,
        assemble=assemble,
        emit=events,
        **kw,
    )


def with_state(state: AgentState, **fields: Any) -> AgentState:
    return apply_update(state, fields)


# ==============================================================================
# executor / tool_runner
# ==============================================================================


def call_response(
    content: str = "",
    calls: Sequence[tuple[str, str, Mapping[str, Any]]] = (),
    *,
    total: int = 50,
    malformed: Mapping[str, str] | None = None,
    reasoning: str | None = None,
) -> LLMResponse:
    """一次带（或不带）工具调用的模型响应。``calls``：(id, 工具名, 参数)。"""
    from agent_runtime.core import ToolCall

    usage = TokenUsage(prompt_tokens=total - 5, completion_tokens=5, total_tokens=total)
    return LLMResponse(
        message=Message(
            role="assistant",
            content=content,
            tool_calls=tuple(ToolCall(id=i, name=n, args=a) for i, n, a in calls),
            reasoning=reasoning,
            produced_by="f",
        ),
        usage=usage,
        endpoint="f",
        model="fast-model",
        finish_reason="tool_calls" if calls else "stop",
        attempts=1,
        duration_ms=1,
        malformed_calls=dict(malformed or {}),
    )


class FakeLLM:
    """满足 ``Invoker``：按顺序给出响应或抛出异常。"""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[LLMRequest] = []

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item
