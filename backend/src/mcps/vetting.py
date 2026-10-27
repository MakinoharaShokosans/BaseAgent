"""工具定义的消毒：描述会进入工具 Schema，模型把它当权威说明，所以可以被硬拒。

设计文档：docs/04_tools/04_mcp.md §6。拒绝的原因进入审计列表，用户能看到某个工具为什么没出现。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from agent_runtime.guardrails import scan_injection

_NAME: Final = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_MIN_DESCRIPTION: Final = 8
_MAX_SCHEMA_NODES: Final = 2000
_TRUNCATED: Final = "…"


@dataclass(frozen=True, slots=True)
class Vetted:
    name: str
    description: str
    schema: Mapping[str, Any]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Rejection:
    tool: str
    reason: str


@dataclass(slots=True)
class _Walk:
    nodes: int = 0
    texts: list[str] = field(default_factory=list[str])


def _descriptions(node: Any, walk: _Walk) -> None:
    """收集 schema 里所有 ``description``/``title`` 文本：参数说明同样是投毒面。"""
    walk.nodes += 1
    if walk.nodes > _MAX_SCHEMA_NODES:
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in ("description", "title") and isinstance(value, str):
                walk.texts.append(value)
            else:
                _descriptions(value, walk)
    elif isinstance(node, list):
        for item in node:
            _descriptions(item, walk)


def _schema_problem(schema: Any) -> str | None:
    if not isinstance(schema, Mapping):
        return "inputSchema 不是对象"
    if schema.get("type") != "object":
        return "inputSchema.type 不是 object"
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return "inputSchema.properties 不是对象"
    walk = _Walk()
    _descriptions(schema, walk)
    if walk.nodes > _MAX_SCHEMA_NODES:
        return "inputSchema 过于庞大"
    return None


def vet_tool(
    name: str,
    description: str | None,
    schema: Any,
    *,
    max_description_chars: int,
    reject_on_injection: bool,
) -> Vetted | Rejection:
    if not isinstance(name, str) or not _NAME.match(name):
        return Rejection(str(name)[:64], "工具名不合法")
    problem = _schema_problem(schema)
    if problem:
        return Rejection(name, problem)
    text = description or ""
    warnings: list[str] = []
    if len(text) > max_description_chars:
        text = text[: max_description_chars - len(_TRUNCATED)] + _TRUNCATED
        warnings.append("描述过长，已截断")
    elif len(text.strip()) < _MIN_DESCRIPTION:
        warnings.append("描述过短")
    walk = _Walk()
    _descriptions(schema, walk)
    hits = scan_injection("\n".join([text, *walk.texts]))
    if hits:
        labels = "、".join(sorted({h.label for h in hits}))
        if reject_on_injection:
            return Rejection(name, f"描述含疑似注入样态（{labels}）")
        warnings.append(f"描述含疑似注入样态（{labels}）")
    return Vetted(name, text, dict(schema), tuple(warnings))
