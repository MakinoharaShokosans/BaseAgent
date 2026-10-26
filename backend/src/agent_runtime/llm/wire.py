"""内部消息 ⇄ 线上格式的转换（纯函数）。设计文档：docs/07_llm_gateway/01_gateway.md §9-10。

全工程只有网关这一层知道线上协议的样子。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from aegis_config.models import Endpoint
from agent_runtime.core import Message, ToolCall, ToolSchema
from agent_runtime.llm.errors import LLMInvalidResponse
from agent_runtime.llm.structured import (
    EMIT_TOOL,
    needs_schema_in_prompt,
    path_for,
    schema_prompt,
    strict_json_schema,
)
from agent_runtime.llm.tokens import estimate_completion_tokens, estimate_prompt_tokens
from agent_runtime.llm.types import LLMRequest, TokenUsage

#: DeepSeek 的三档强度（见 docs/07_llm_gateway/04_deepseek.md §3）
_DEEPSEEK_EFFORT: Final = {"low": "low", "medium": "high", "high": "high", "max": "max"}


# ==============================================================================
# 请求
# ==============================================================================


def build_request_kwargs(
    endpoint: Endpoint, temperature: float, request: LLMRequest, *, prefer_schema: bool = True
) -> dict[str, Any]:
    """生成 ``chat.completions.create`` 的参数。能力声明决定哪些字段会被发送。"""
    caps = endpoint.capabilities
    if caps.reasoning_passback == "opaque":
        raise NotImplementedError(
            "reasoning_passback = opaque 尚未实现：载荷在线上的放置位置因服务商而异"
        )

    path = path_for(caps, request, prefer_schema=prefer_schema)
    spec = request.structured
    tools: Sequence[ToolSchema] = request.tools or ()
    if path == "tool":
        assert spec is not None
        tools = (ToolSchema(EMIT_TOOL, f"提交 {spec.name} 结果", dict(spec.schema)),)
    has_tools = bool(tools)
    kwargs: dict[str, Any] = {
        "model": endpoint.model,
        "messages": _messages(
            endpoint,
            request,
            schema_prompt(spec) if spec is not None and needs_schema_in_prompt(path) else None,
        ),
        # 显式设置，不依赖服务商默认值：默认值可能很小，导致输出被截断
        "max_tokens": min(
            request.max_output_tokens or endpoint.max_output_tokens, endpoint.max_output_tokens
        ),
        "temperature": temperature,
    }
    if has_tools and caps.tools:
        kwargs["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": dict(t.parameters),
                },
            }
            for t in tools
        ]
    if path == "tool":
        kwargs["tool_choice"] = {"type": "function", "function": {"name": EMIT_TOOL}}
    elif path == "json_object":
        kwargs["response_format"] = {"type": "json_object"}
    elif path == "json_schema":
        assert spec is not None
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": spec.name,
                "schema": strict_json_schema(dict(spec.schema)),
                "strict": True,
            },
        }

    effort = request.reasoning_effort or endpoint.reasoning_effort
    if caps.thinking_control == "deepseek":
        # DeepSeek 默认开启思考：不发送字段不等于关闭，所以总是显式发送
        thinking_on = caps.thinking_with_tools or not has_tools
        kwargs["extra_body"] = {"thinking": {"type": "enabled" if thinking_on else "disabled"}}
        if thinking_on and effort is not None:
            kwargs["reasoning_effort"] = _DEEPSEEK_EFFORT[effort]
    elif effort is not None:
        kwargs["reasoning_effort"] = effort
    return kwargs


def _messages(
    endpoint: Endpoint, request: LLMRequest, extra_system: str | None
) -> list[dict[str, Any]]:
    caps = endpoint.capabilities
    source = list(request.messages)
    if extra_system:
        # 追加到最前面的 system 消息：Schema 对同一场景恒定，不破坏前缀缓存
        if source and source[0].role == "system":
            source[0] = replace(source[0], content=f"{source[0].content}\n\n{extra_system}")
        else:
            source.insert(0, Message(role="system", content=extra_system))
    last_user = max((i for i, m in enumerate(source) if m.role == "user"), default=-1)
    out: list[dict[str, Any]] = []
    seen_non_system = False

    for index, message in enumerate(source):
        if message.role == "system":
            if seen_non_system:
                raise ValueError("system 消息只能放在最前面")
        else:
            seen_non_system = True

        wire = _message(message)
        if message.role == "assistant" and _passes_back(
            endpoint, request, message, index > last_user
        ):
            assert caps.reasoning_field is not None and message.reasoning is not None
            wire[caps.reasoning_field] = message.reasoning

        if (
            caps.merge_consecutive_user
            and wire["role"] == "user"
            and out
            and out[-1]["role"] == "user"
        ):
            out[-1]["content"] = f"{out[-1]['content']}\n\n{wire['content']}"
        else:
            out.append(wire)
    return out


def _message(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            # 带工具调用且无正文时发送 null，兼容较严格的服务端
            "content": message.content or None,
            "tool_calls": [_wire_call(c) for c in message.tool_calls],
        }
    return {"role": message.role, "content": message.content}


def _wire_call(call: ToolCall) -> dict[str, Any]:
    # 此前被判为格式错误的调用，把原文原样送回去，不要重新编码成另一种样子
    if set(call.args) == {"_raw"} and isinstance(call.args["_raw"], str):
        arguments = call.args["_raw"]
    else:
        arguments = json.dumps(call.args, ensure_ascii=False)
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": arguments},
    }


def _passes_back(endpoint: Endpoint, request: LLMRequest, message: Message, in_tail: bool) -> bool:
    """思考内容回传。只回传给产生它的同一端点；端点没有声明字段名则无从回传。"""
    caps = endpoint.capabilities
    if (
        caps.reasoning_field is None
        or message.reasoning is None
        or message.produced_by != endpoint.name
    ):
        return False
    match caps.reasoning_passback:
        case "tool_turns":
            return in_tail
        case "all_when_tools":
            return bool(request.tools)
        case _:
            return False


# ==============================================================================
# 响应
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ParsedCompletion:
    message: Message
    usage: TokenUsage
    finish_reason: str
    malformed_calls: dict[str, str]


def parse_completion(
    endpoint: Endpoint,
    completion: Any,
    request: LLMRequest,
    *,
    safety_factor: float,
    estimate_when_missing: bool = True,
) -> ParsedCompletion:
    choices = getattr(completion, "choices", None)
    if not choices:
        raise LLMInvalidResponse("响应里没有 choices")
    choice = choices[0]
    raw = choice.message
    finish = str(choice.finish_reason or "")

    calls, malformed = _tool_calls(raw.tool_calls or [], truncated=finish == "length")
    content = raw.content or ""
    if not content and not calls:
        raise LLMInvalidResponse("响应既没有内容也没有工具调用")

    field = endpoint.capabilities.reasoning_field
    reasoning = getattr(raw, field, None) if field else None
    message = Message(
        role="assistant",
        content=content,
        tool_calls=calls,
        reasoning=reasoning if isinstance(reasoning, str) and reasoning else None,
        produced_by=endpoint.name,
    )
    usage = _usage(endpoint, completion, request, message, safety_factor, estimate_when_missing)
    return ParsedCompletion(message, usage, finish, malformed)


def _tool_calls(raw_calls: Any, *, truncated: bool) -> tuple[tuple[ToolCall, ...], dict[str, str]]:
    calls: list[ToolCall] = []
    malformed: dict[str, str] = {}
    used: set[str] = set()
    counter = 0

    for raw in raw_calls:
        call_id = getattr(raw, "id", None)
        if not call_id or call_id in used:
            counter += 1
            while f"call_{counter}" in used:
                counter += 1
            call_id = f"call_{counter}"
        used.add(call_id)

        args, problem = _arguments(raw.function.arguments)
        calls.append(ToolCall(id=call_id, name=raw.function.name, args=args))
        if problem:
            malformed[call_id] = problem

    if truncated:
        for call in calls:
            malformed[call.id] = "输出因长度上限被截断，参数很可能不完整；请缩短输出后重新发起调用"
    return tuple(calls), malformed


def _arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    if isinstance(raw, dict):
        return dict(raw), None  # type: ignore[arg-type]
    text = str(raw)
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"_raw": text}, "参数不是合法的 JSON，请重新发起调用"
    if not isinstance(parsed, dict):
        return {"_raw": text}, "参数不是 JSON 对象，请重新发起调用"
    return parsed, None  # type: ignore[return-value]


def _usage(
    endpoint: Endpoint,
    completion: Any,
    request: LLMRequest,
    message: Message,
    safety_factor: float,
    estimate_when_missing: bool,
) -> TokenUsage:
    usage = getattr(completion, "usage", None)
    if usage is not None and endpoint.capabilities.usage_in_response:
        prompt = int(usage.prompt_tokens or 0)
        completion_tokens = int(usage.completion_tokens or 0)
        details = getattr(usage, "completion_tokens_details", None)
        return TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion_tokens,
            total_tokens=int(usage.total_tokens or prompt + completion_tokens),
            reasoning_tokens=int(getattr(details, "reasoning_tokens", 0) or 0),
            cache_hit_tokens=getattr(usage, "prompt_cache_hit_tokens", None),
            cache_miss_tokens=getattr(usage, "prompt_cache_miss_tokens", None),
        )
    if not estimate_when_missing:
        return TokenUsage(0, 0, 0)
    prompt = _high(estimate_prompt_tokens(request.messages, request.tools), safety_factor)
    produced = _high(estimate_completion_tokens(message), safety_factor)
    return TokenUsage(prompt, produced, prompt + produced, estimated=True)


def _high(tokens: int, factor: float) -> int:
    return int(tokens * factor + 0.999999)
