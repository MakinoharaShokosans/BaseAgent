"""会话压缩的提炼器：快速档、结构化输出，经网关调用。设计文档：02_window_and_compaction §1.5.1。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, Field

from agent_runtime.context import PromptLibrary
from agent_runtime.core import Message
from agent_runtime.guardrails import wrap
from agent_runtime.llm import LLMRequest
from agent_runtime.llm.structured import Degraded, Parsed
from agent_runtime.memory import (
    ExtractedFact,
    ExtractedFailure,
    Extraction,
    ExtractionRequest,
    Superseded,
)


class FactItem(BaseModel):
    text: str
    task_id: str = ""


class FailureItem(BaseModel):
    action: str
    failure_reason: str
    conclusion: str
    task_id: str = ""


class SupersededItem(BaseModel):
    text: str
    reason: str = ""


class CompactionOutput(BaseModel):
    summary: str = Field(default="", description="更新后的整体背景与进展")
    facts: list[FactItem] = Field(default_factory=list[FactItem])
    failed_attempts: list[FailureItem] = Field(default_factory=list[FailureItem])
    superseded: list[SupersededItem] = Field(default_factory=list[SupersededItem])


def _lines(items: Sequence[str]) -> str:
    return "\n".join(items) if items else "（无）"


def render_input(request: ExtractionRequest) -> str:
    m, limits = request.memory, request.limits
    parts = [
        wrap("session_memory", m.summary or "（无）", kind="existing_summary"),
        wrap(
            "session_memory",
            _lines([f"- [task={f.task_id}] {f.text}" for f in m.facts]),
            kind="existing_facts",
        ),
        wrap(
            "session_memory",
            _lines(
                [
                    f"- [task={f.task_id}] 做法：{f.attempt.action}；"
                    f"失败原因：{f.attempt.failure_reason}；结论：{f.attempt.conclusion}"
                    for f in m.failures
                ]
            ),
            kind="existing_failed_attempts",
        ),
        wrap(
            "session_memory",
            _lines(
                [f"- [task={f.task_id}] {f.text}" for f in m.pending_facts]
                + [
                    f"- [task={f.task_id}] 失败尝试 做法：{f.attempt.action}；"
                    f"原因：{f.attempt.failure_reason}；结论：{f.attempt.conclusion}"
                    for f in m.pending_failures
                ]
            ),
            kind="recent_additions",
        ),
        wrap(
            "session_memory",
            _lines(
                [
                    f"[task={t.task_id}] {'用户' if t.role == 'user' else '助手'}：{t.content}"
                    for t in request.turns
                ]
            ),
            kind="conversation_to_compact",
        ),
        f"上限：事实不超过 {limits.max_facts} 条，失败尝试不超过 {limits.max_failures} 条，"
        f"摘要不超过 {limits.summary_max_chars} 字。",
    ]
    return "\n\n".join(parts)


class StructuredInvoker(Protocol):
    """``LLMGateway`` 满足它；测试用假实现。"""

    async def invoke_structured[T: BaseModel](
        self, request: LLMRequest, model: type[T]
    ) -> Parsed[T] | Degraded: ...


class GatewayExtractor:
    def __init__(self, gateway: StructuredInvoker, library: PromptLibrary) -> None:
        self._gateway = gateway
        self._system = library.get("compactor")

    async def __call__(self, request: ExtractionRequest) -> Extraction | None:
        llm_request = LLMRequest(
            tier="fast",
            messages=(
                Message(role="system", content=self._system),
                Message(role="user", content=render_input(request)),
            ),
            purpose="compactor",
        )
        result = await self._gateway.invoke_structured(llm_request, CompactionOutput)
        if not isinstance(result, Parsed):
            return None  # 降级：保留旧记忆，由压缩器记失败并退避
        out = result.value
        return Extraction(
            summary=out.summary or None,
            facts=tuple(ExtractedFact(f.text, f.task_id) for f in out.facts),
            failures=tuple(
                ExtractedFailure(f.action, f.failure_reason, f.conclusion, f.task_id)
                for f in out.failed_attempts
            ),
            superseded=tuple(Superseded(s.text, s.reason) for s in out.superseded),
        )
