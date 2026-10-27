"""草稿工具：``update_plan`` 与 ``note``。设计文档：docs/02_runtime/05_plan_and_scratch.md。

它们只改状态里的草稿字段，没有外部副作用，所以由工具包装层就地处理，不经派发器。
注册表里的 ``ScratchTool`` 只提供对模型暴露的 Schema 和权限名，``invoke`` 从不被调用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from agent_runtime.core import (
    MAX_FACT_CHARS,
    MAX_FACTS,
    MAX_FAILED_ATTEMPTS,
    MAX_PLAN_ITEMS,
    AgentTool,
    FailedAttempt,
    PlanItem,
    ToolResult,
)

UPDATE_PLAN: Final = "update_plan"
NOTE: Final = "note"
SCRATCH_TOOLS: Final = frozenset({UPDATE_PLAN, NOTE})

MAX_TITLE_CHARS: Final = 100
_STATUSES: Final = ("pending", "in_progress", "completed")


@dataclass(frozen=True, slots=True)
class Scratch:
    plan: tuple[PlanItem, ...] = ()
    facts: tuple[str, ...] = ()
    failed_attempts: tuple[FailedAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class ScratchResult:
    scratch: Scratch
    text: str
    ok: bool
    #: 草稿是否变了（决定是否发 ``plan.updated``）
    plan_changed: bool = False


class _ScratchTool(AgentTool):
    idempotent = True
    timeout_sec = 5.0

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:  # pragma: no cover
        return ToolResult.failure(f"{self.name} 由工具包装层处理，不应被派发")


class UpdatePlanTool(_ScratchTool):
    name = UPDATE_PLAN
    description = (
        "整体替换当前任务的计划（草稿，不影响任务是否结束）。每次给出完整列表；空列表表示清空。"
        f"最多 {MAX_PLAN_ITEMS} 条。状态：pending / in_progress / completed。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "title": {"type": "string"},
                        "status": {"type": "string", "enum": list(_STATUSES)},
                    },
                    "required": ["title"],
                },
            }
        },
        "required": ["items"],
    }


class NoteTool(_ScratchTool):
    name = NOTE
    description = (
        "记一条本任务内的笔记。kind=fact 记已确认的事实（text）；"
        "kind=failed_attempt 记失败的做法（action、failure_reason、conclusion）。"
        "笔记是你自己的陈述，不会被当作经过验证的事实。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["fact", "failed_attempt"]},
            "text": {"type": "string"},
            "action": {"type": "string"},
            "failure_reason": {"type": "string"},
            "conclusion": {"type": "string"},
        },
        "required": ["kind"],
    }


def scratch_tools() -> list[AgentTool]:
    return [UpdatePlanTool(), NoteTool()]


def apply_scratch(scratch: Scratch, name: str, args: Mapping[str, Any]) -> ScratchResult:
    """纯函数：失败时草稿原样返回，并说明原因。"""
    if name == UPDATE_PLAN:
        return _update_plan(scratch, args)
    if name == NOTE:
        return _note(scratch, args)
    return ScratchResult(scratch, f"未知的草稿工具：{name}", False)


def _fail(scratch: Scratch, why: str) -> ScratchResult:
    return ScratchResult(scratch, f"失败：{why}", False)


def _update_plan(scratch: Scratch, args: Mapping[str, Any]) -> ScratchResult:
    raw = args.get("items")
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return _fail(scratch, "items 必须是列表")
    items = cast("Sequence[object]", raw)
    if len(items) > MAX_PLAN_ITEMS:
        return _fail(scratch, f"计划最多 {MAX_PLAN_ITEMS} 条，收到 {len(items)} 条；请合并步骤")
    plan: list[PlanItem] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            return _fail(scratch, f"items[{index - 1}] 必须是对象")
        entry = cast("Mapping[str, Any]", item)
        title = entry.get("title")
        status = entry.get("status", "pending")
        ident = entry.get("id", index)
        if not isinstance(title, str) or not title.strip():
            return _fail(scratch, f"items[{index - 1}].title 必须是非空字符串")
        if status not in _STATUSES:
            return _fail(scratch, f"items[{index - 1}].status 必须是 {'/'.join(_STATUSES)}")
        if isinstance(ident, bool) or not isinstance(ident, int):
            return _fail(scratch, f"items[{index - 1}].id 必须是整数")
        plan.append(PlanItem(id=ident, title=title.strip()[:MAX_TITLE_CHARS], status=status))
    ids = [p.id for p in plan]
    if len(set(ids)) != len(ids):
        return _fail(scratch, "计划里的 id 重复")
    new = tuple(plan)
    return ScratchResult(
        Scratch(new, scratch.facts, scratch.failed_attempts),
        f"计划已更新（{len(new)} 条）。" if new else "计划已清空。",
        True,
        plan_changed=new != scratch.plan,
    )


def _note(scratch: Scratch, args: Mapping[str, Any]) -> ScratchResult:
    kind = args.get("kind")
    if kind == "fact":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return _fail(scratch, "kind=fact 需要非空的 text")
        text = text.strip()
        if len(text) > MAX_FACT_CHARS:
            return _fail(scratch, f"单条事实不超过 {MAX_FACT_CHARS} 字符，请精简")
        if text in scratch.facts:
            return ScratchResult(scratch, "已存在，未重复记录。", True)
        if len(scratch.facts) >= MAX_FACTS:
            return _fail(scratch, f"事实笔记已达上限 {MAX_FACTS} 条；请合并或放弃，不会自动淘汰")
        return ScratchResult(
            Scratch(scratch.plan, (*scratch.facts, text), scratch.failed_attempts),
            "已记录。",
            True,
        )
    if kind == "failed_attempt":
        fields: dict[str, str] = {}
        for key in ("action", "failure_reason", "conclusion"):
            value = args.get(key)
            if not isinstance(value, str) or not value.strip():
                return _fail(scratch, f"kind=failed_attempt 需要非空的 {key}")
            if len(value.strip()) > MAX_FACT_CHARS:
                return _fail(scratch, f"{key} 不超过 {MAX_FACT_CHARS} 字符，请精简")
            fields[key] = value.strip()
        attempt = FailedAttempt(**fields)
        if attempt in scratch.failed_attempts:
            return ScratchResult(scratch, "已存在，未重复记录。", True)
        if len(scratch.failed_attempts) >= MAX_FAILED_ATTEMPTS:
            return _fail(scratch, f"失败尝试笔记已达上限 {MAX_FAILED_ATTEMPTS} 条；请合并或放弃")
        return ScratchResult(
            Scratch(scratch.plan, scratch.facts, (*scratch.failed_attempts, attempt)),
            "已记录。",
            True,
        )
    return _fail(scratch, "kind 必须是 fact 或 failed_attempt")
