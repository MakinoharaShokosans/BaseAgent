"""网关的请求、响应与观测记录。设计文档：docs/07_llm_gateway/01_gateway.md §3。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Self

from pydantic import BaseModel

from agent_runtime.core import Message, ToolSchema
from baseagent_config.models import Endpoint

Tier = Literal["reasoning", "fast"]
Effort = Literal["low", "medium", "high", "max"]
Outcome = Literal["ok", "retry", "fallback", "error"]
StructuredPath = Literal["json_schema", "json_object", "tool", "prompt"]


@dataclass(frozen=True, slots=True)
class StructuredSpec:
    """要求模型输出符合某个 Pydantic 模型的 JSON。设计文档见 02_structured_output.md。"""

    name: str
    #: ``model_json_schema()`` 的原样输出；发给严格模式前由 ``strict_json_schema`` 简化
    schema: Mapping[str, Any]

    @classmethod
    def for_model(cls, model: type[BaseModel]) -> Self:
        return cls(name=model.__name__, schema=model.model_json_schema())


@dataclass(frozen=True, slots=True)
class LLMRequest:
    tier: Tier
    messages: Sequence[Message]
    tools: Sequence[ToolSchema] | None = None
    max_output_tokens: int | None = None
    reasoning_effort: Effort | None = None
    #: 绝对时间，与网关所用时钟同一时基；见 ``PhysicalBudgetGuard.remaining_wall_sec``
    deadline: float | None = None
    #: 观测标签：planner / executor / compactor …
    purpose: str = ""
    structured: StructuredSpec | None = None
    #: 优先尝试的端点（修复重试要回到产生坏输出的那个端点）；它失败时仍会按顺序降级
    prefer_endpoint: str | None = None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    #: 含在 completion_tokens 里，仅用于观测，不重复计费
    reasoning_tokens: int = 0
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    #: 为真表示服务商没给用量，是本地估算的（宁高勿低）
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class LLMResponse:
    message: Message
    usage: TokenUsage
    endpoint: str
    model: str
    finish_reason: str
    #: 含重试与降级的总尝试次数
    attempts: int
    duration_ms: int
    #: 格式错误或可能被截断的工具调用：id -> 原因。不得被当作普通参数执行
    malformed_calls: Mapping[str, str] = field(default_factory=dict[str, str])
    #: 本次请求实际走的结构化路径；非结构化请求为 None
    structured_path: StructuredPath | None = None


@dataclass(frozen=True, slots=True)
class CallRecord:
    """每次尝试一条，供轨迹与 ``llm.call`` 事件使用。不含密钥与消息正文。"""

    purpose: str
    tier: str
    endpoint: str
    model: str
    attempt: int
    outcome: Outcome
    duration_ms: int
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    error_class: str | None = None


@dataclass(frozen=True, slots=True)
class GatewayTier:
    temperature: float
    #: 有序，顺序即降级顺序
    endpoints: tuple[Endpoint, ...]
