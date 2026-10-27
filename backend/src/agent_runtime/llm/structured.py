"""结构化输出：路径选择、JSON 抽取、校验、修复提示（纯函数）。

设计文档：docs/07_llm_gateway/02_structured_output.md。网关的编排（重试、计数）在 gateway.py。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ValidationError

from agent_runtime.llm.types import (
    LLMRequest,
    LLMResponse,
    StructuredPath,
    StructuredSpec,
    TokenUsage,
)
from baseagent_config.models import Capabilities

#: 路径 3 里承载结果的工具名（见 §3.1）
EMIT_TOOL: Final = "emit_result"

DegradeReason = Literal["invalid", "truncated", "content_filter"]
StructuredOutcome = Literal["ok", "repaired", "degraded"]

_ECHO_LIMIT_TRUNCATED: Final = 2000

# 严格模式（strict）不接受的校验类关键字。校验由 Pydantic 在收到输出后做，这里只管让服务端放行
_UNSUPPORTED_KEYWORDS: Final = frozenset(
    {
        "title",
        "default",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "pattern",
        "format",
    }
)
_NAME_TABLES: Final = frozenset({"properties", "$defs", "definitions"})


# ==============================================================================
# 路径
# ==============================================================================


def choose_path(caps: Capabilities, *, prefer_schema: bool) -> StructuredPath:
    """按端点能力选路径（§3.1）。``prefer_schema`` 为假且有 ``json_object`` 时不用 json_schema。"""
    if caps.json_schema and (prefer_schema or not caps.json_object):
        return "json_schema"
    if caps.json_object:
        return "json_object"
    if caps.tools:
        return "tool"
    return "prompt"


def path_for(
    caps: Capabilities, request: LLMRequest, *, prefer_schema: bool
) -> StructuredPath | None:
    return None if request.structured is None else choose_path(caps, prefer_schema=prefer_schema)


def needs_schema_in_prompt(path: StructuredPath | None) -> bool:
    """路径 2 与 4 服务端不知道 Schema，要写进提示词。"""
    return path in ("json_object", "prompt")


def schema_prompt(spec: StructuredSpec) -> str:
    # 必含 "JSON" 字样：DeepSeek 的 json_object 模式要求提示词里出现 json（§3.1）
    return (
        "请只输出一个 JSON 对象，不要输出 Markdown 围栏或任何解释文字。"
        "该对象必须符合下面的 JSON Schema：\n"
        + json.dumps(spec.schema, ensure_ascii=False, separators=(",", ":"))
    )


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """把 Pydantic 导出的 Schema 简化成服务端严格模式接受的形态（返回新对象）。

    所有对象封闭（``additionalProperties: false``）且全部字段必填，去掉严格模式不支持的校验关键字。
    """
    result = _strict(schema, names=False)
    assert isinstance(result, dict)
    return result  # type: ignore[return-value]


def _strict(node: Any, *, names: bool) -> Any:
    if isinstance(node, list):
        return [_strict(item, names=False) for item in node]  # type: ignore[union-attr]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():  # type: ignore[union-attr]
        if not names and key in _UNSUPPORTED_KEYWORDS:
            continue
        out[key] = _strict(value, names=not names and key in _NAME_TABLES)
    if not names and isinstance(out.get("properties"), dict):
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])  # type: ignore[arg-type]
    return out


# ==============================================================================
# 抽取
# ==============================================================================


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里取出 JSON 对象；找不到完整的就返回 None，**不修补残缺 JSON**。

    多个顶层对象取最后一个（推理内容泄漏进正文时，最终答案在最后）。
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        whole = json.loads(stripped)
    except RecursionError:
        return None
    except ValueError:
        pass
    else:
        return whole if isinstance(whole, dict) else None  # type: ignore[return-value]

    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    pos = 0
    while (start := stripped.find("{", pos)) >= 0:
        try:
            value, end = decoder.raw_decode(stripped, start)
        except json.JSONDecodeError as exc:
            if exc.pos >= len(stripped) or exc.msg.startswith("Unterminated string"):
                return None  # 到文末都没闭合：被截断了，不能拿里面的片段凑数
            pos = start + 1  # 正文里的普通花括号，如 {x}
            continue
        except RecursionError:
            return None
        pos = end
        if _inside_array(stripped, start):
            continue  # [{...}] 是数组，不是对象
        found = value
    return found


def _inside_array(text: str, start: int) -> bool:
    before = text[:start].rstrip()
    return bool(before) and before[-1] in "[,"


# ==============================================================================
# 校验与修复
# ==============================================================================


def describe_validation_error(exc: ValidationError, limit: int = 20) -> list[str]:
    """给模型看的错误清单。只写路径与原因，不回显输入值：输入可能复述了不可信内容。"""
    lines: list[str] = []
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    for error in errors:
        path = _path(error["loc"])
        if error["type"] == "missing":
            lines.append(f"缺少字段 `{path}`")
        else:
            lines.append(f"`{path or '（整体）'}`：{error['msg']}")
    if len(lines) > limit:
        extra = len(lines) - limit
        lines = [*lines[:limit], f"另有 {extra} 处错误未列出"]
    return lines


def _path(loc: Sequence[int | str]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else part)
    return out


def repair_prompt(reason: DegradeReason, problems: Sequence[str]) -> str:
    if reason == "truncated":
        return (
            "上一次输出因超过长度上限被截断，无法解析。"
            "请精简内容、缩短输出，重新给出完整的 JSON 对象，只输出 JSON。"
        )
    listing = "\n".join(f"- {p}" for p in problems)
    return f"上一次输出未通过校验：\n{listing}\n请只输出修正后的 JSON。"


def echo(text: str, reason: DegradeReason) -> str:
    """放进修复请求的“上一次输出”。截断的输出往往很长，只留开头。"""
    if not text:
        return "（空）"
    if reason == "truncated" and len(text) > _ECHO_LIMIT_TRUNCATED:
        return text[:_ECHO_LIMIT_TRUNCATED] + "…（已截断）"
    return text


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: DegradeReason
    raw_text: str
    problems: tuple[str, ...]


def judge[T: BaseModel](response: LLMResponse, model: type[T]) -> T | Rejected:
    """判定一次响应：通过校验的模型对象，或拒收原因。截断的输出不解析，即使看上去完整。"""
    text, payload, problem = _payload(response)
    finish = response.finish_reason
    if finish == "content_filter":
        return Rejected("content_filter", text, ("被服务端内容过滤拦截",))
    if finish == "length":
        return Rejected("truncated", text, ("输出因长度上限被截断",))
    if problem is not None:
        return Rejected("invalid", text, (problem,))
    assert payload is not None
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        return Rejected("invalid", text, tuple(describe_validation_error(exc)))


def _payload(response: LLMResponse) -> tuple[str, dict[str, Any] | None, str | None]:
    """(原始文本, 抽出的对象, 抽取失败的原因)。"""
    message = response.message
    if response.structured_path == "tool":
        for call in message.tool_calls:
            if call.name != EMIT_TOOL:
                continue
            if call.id in response.malformed_calls:
                raw = call.args.get("_raw")
                return (
                    raw if isinstance(raw, str) else "",
                    None,
                    f"工具参数无法解析：{response.malformed_calls[call.id]}",
                )
            return json.dumps(dict(call.args), ensure_ascii=False), dict(call.args), None
    text = message.content
    payload = extract_json_object(text)
    if payload is None:
        return text, None, "输出里找不到完整的 JSON 对象（需要一个以 { 开头、以 } 结尾的对象）"
    return text, payload, None


# ==============================================================================
# 结果
# ==============================================================================


@dataclass(frozen=True, slots=True)
class Parsed[T: BaseModel]:
    """通过校验的结果。"""

    value: T
    #: 最后一次调用的响应
    response: LLMResponse
    #: 各轮（含修复）用量之和
    usage: TokenUsage
    rounds: int

    @property
    def repaired(self) -> bool:
        return self.rounds > 1


@dataclass(frozen=True, slots=True)
class Degraded:
    """校验（含修复重试）仍未通过。降级方式由各场景在自己的契约里声明。"""

    reason: DegradeReason
    raw_text: str
    problems: tuple[str, ...]
    response: LLMResponse
    usage: TokenUsage
    rounds: int


@dataclass(frozen=True, slots=True)
class StructuredRecord:
    """每次结构化调用一条，供 ``structured_output.*`` 事件与格式失败率统计使用。"""

    purpose: str
    path: StructuredPath | None
    outcome: StructuredOutcome
    rounds: int
    reason: DegradeReason | None = None


def add_usage(total: TokenUsage | None, usage: TokenUsage) -> TokenUsage:
    if total is None:
        return usage
    return TokenUsage(
        prompt_tokens=total.prompt_tokens + usage.prompt_tokens,
        completion_tokens=total.completion_tokens + usage.completion_tokens,
        total_tokens=total.total_tokens + usage.total_tokens,
        reasoning_tokens=total.reasoning_tokens + usage.reasoning_tokens,
        cache_hit_tokens=_sum(total.cache_hit_tokens, usage.cache_hit_tokens),
        cache_miss_tokens=_sum(total.cache_miss_tokens, usage.cache_miss_tokens),
        estimated=total.estimated or usage.estimated,
    )


def _sum(a: int | None, b: int | None) -> int | None:
    return None if a is None and b is None else (a or 0) + (b or 0)
