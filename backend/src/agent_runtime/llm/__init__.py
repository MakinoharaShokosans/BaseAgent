"""LLM 网关：分档、端点与能力声明、重试、降级、熔断、截止时间、用量。

目前未实现：``opaque`` 思考内容回传（03）。设计文档：docs/07_llm_gateway/
"""

from agent_runtime.llm.errors import (
    EndpointFailure,
    LLMBadRequest,
    LLMDeadlineExceeded,
    LLMError,
    LLMInvalidResponse,
    LLMUnavailableError,
)
from agent_runtime.llm.gateway import LLMGateway, api_key_for, default_client_factory
from agent_runtime.llm.preferred import PreferredEndpoint
from agent_runtime.llm.structured import (
    Degraded,
    Parsed,
    StructuredRecord,
    extract_json_object,
)
from agent_runtime.llm.tokens import estimate_tokens
from agent_runtime.llm.types import (
    CallRecord,
    GatewayTier,
    LLMRequest,
    LLMResponse,
    StructuredSpec,
    TokenUsage,
)

__all__ = [
    "CallRecord",
    "Degraded",
    "EndpointFailure",
    "GatewayTier",
    "LLMBadRequest",
    "LLMDeadlineExceeded",
    "LLMError",
    "LLMGateway",
    "LLMInvalidResponse",
    "LLMRequest",
    "LLMResponse",
    "LLMUnavailableError",
    "Parsed",
    "PreferredEndpoint",
    "StructuredRecord",
    "StructuredSpec",
    "TokenUsage",
    "api_key_for",
    "default_client_factory",
    "estimate_tokens",
    "extract_json_object",
]
