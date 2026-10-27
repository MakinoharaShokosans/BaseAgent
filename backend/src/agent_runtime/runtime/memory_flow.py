"""任务前后的记忆流程：开始时装入，收尾时写流水、沉淀、必要时压缩。

设计文档：docs/03_context/04_memory.md §6，02_window_and_compaction.md §1.2-1.3。
记忆写入失败不使已完成的任务变成失败：``finish`` 不抛异常，问题记在报告里。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from loguru import logger

from agent_runtime.context import MemoryEntry, PromptAssets
from agent_runtime.core import AgentState, FailedAttempt, Message
from agent_runtime.memory import (
    CompactionResult,
    MemoryStore,
    Outcome,
    SessionCompactor,
    Turn,
    WorkspaceMemory,
    deposit,
)

_REDACTED: Final = "[SECURITY_REDACTED]"
_SECURITY_PREFIX: Final = "[SECURITY]"
_WRITE_TOOLS: Final = frozenset({"write_file", "edit_file"})


@dataclass(frozen=True, slots=True)
class SessionContext:
    """任务开始时从记忆装入的一切。"""

    history: tuple[Message, ...]
    rolling_summary: str
    confirmed_facts: tuple[str, ...]
    failed_attempts: tuple[FailedAttempt, ...]
    #: 只有记忆相关的字段有意义（``system`` 为空）；用 :meth:`apply_to` 并入真正的资产
    assets: PromptAssets

    def apply_to(self, base: PromptAssets) -> PromptAssets:
        import dataclasses

        a = self.assets
        return dataclasses.replace(
            base,
            workspace_memory=a.workspace_memory,
            session_summary=a.session_summary,
            session_facts=a.session_facts,
            session_failures=a.session_failures,
            recent_facts=a.recent_facts,
            recent_failures=a.recent_failures,
        )


@dataclass(frozen=True, slots=True)
class FinishReport:
    error: str | None = None
    compaction: str | None = None
    dropped: tuple[tuple[str, str], ...] = ()


def outcome_of(state: AgentState) -> Outcome:
    if state.should_terminate:
        return "failed" if state.termination_reason.startswith(_SECURITY_PREFIX) else "terminated"
    return "success"


def task_was_tainted(messages: Sequence[Message]) -> bool:
    """任务读过不可信内容（观察值带 ``trust="untrusted"`` 信封）：由此得出的事实不能算可信。"""
    return any(m.role == "tool" and 'trust="untrusted"' in m.content for m in messages)


def touched_files(messages: Sequence[Message]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for m in messages:
        for call in m.tool_calls:
            path = call.args.get("path")
            if call.name in _WRITE_TOOLS and isinstance(path, str) and path:
                seen[path] = None
    return tuple(seen)


def _message(turn: Turn) -> Message:
    return Message(role="user" if turn.role == "user" else "assistant", content=turn.content)


def _workspace_entries(memory: WorkspaceMemory) -> tuple[MemoryEntry, ...]:
    return tuple(
        MemoryEntry(text, "user", "trusted")
        for layer in (memory.profile, memory.guidelines, memory.architecture, memory.pitfalls)
        for text in layer
    )


class MemoryFlow:
    def __init__(
        self,
        store: MemoryStore,
        compactor: SessionCompactor,
        *,
        active_window_turns: int,
        max_pending: int,
        persist_task_facts: bool,
        count_tokens: Callable[[str], int],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._compactor = compactor
        self._window = active_window_turns
        self._max_pending = max_pending
        self._persist = persist_task_facts
        self._count = count_tokens
        self._clock = clock

    async def prepare(self, session_id: str) -> SessionContext:
        memory = await self._store.load_memory(session_id)
        turns = await self._store.turns_after(session_id, memory.watermark)
        if len(turns) < self._window:  # 保底：摘要质量不佳时也不至于完全失去最近的上下文
            turns = await self._store.recent_turns(session_id, self._window)
        workspace_id = await self._workspace_of(session_id)
        user_memory = await self._store.get_workspace_memory(workspace_id)
        workspace = await self._store.get_workspace(workspace_id)

        def entries(items: Sequence[object]) -> tuple[MemoryEntry, ...]:
            return tuple(MemoryEntry(f.text, f.source, f.trust) for f in items)  # type: ignore[attr-defined]

        assets = PromptAssets(
            system="",
            workspace_name=workspace.name,
            workspace_description=workspace.description,
            workspace_memory=_workspace_entries(user_memory),
            session_summary=(
                MemoryEntry(memory.summary, "compressor", "untrusted") if memory.summary else None
            ),
            session_facts=entries(memory.facts),
            session_failures=tuple(f.attempt for f in memory.failures),
            recent_facts=entries(memory.pending_facts),
            recent_failures=tuple(f.attempt for f in memory.pending_failures),
        )
        return SessionContext(
            history=tuple(_message(t) for t in turns),
            rolling_summary=memory.summary,
            confirmed_facts=tuple(f.text for f in (*memory.facts, *memory.pending_facts)),
            failed_attempts=tuple(f.attempt for f in (*memory.failures, *memory.pending_failures)),
            assets=assets,
        )

    async def compact(self, session_id: str) -> CompactionResult:
        """手动压缩（用户点按钮）；与收尾自动压缩共用同一把会话锁。"""
        return await self._compactor.compact_now(session_id)

    async def _workspace_of(self, session_id: str) -> str:
        return await self._store.workspace_id_of(session_id)

    async def finish(self, session_id: str, state: AgentState, *, delivery: str) -> FinishReport:
        try:
            return await self._finish(session_id, state, delivery)
        except Exception as exc:  # 记忆写入失败不使已完成的任务变成失败
            logger.error("任务 {} 收尾写记忆失败：{!r}", state.task_id, exc)
            return FinishReport(error=f"{type(exc).__name__}: {exc}")

    async def _finish(self, session_id: str, state: AgentState, delivery: str) -> FinishReport:
        goal = self._redact(state.task_goal, state)
        answer = self._redact(delivery, state)
        await self._store.append_turn(session_id, state.task_id, "user", goal, self._count(goal))
        await self._store.append_turn(
            session_id, state.task_id, "assistant", answer, self._count(answer)
        )

        outcome = outcome_of(state)
        files = touched_files(state.messages)
        result_flags: list[bool] = []
        dropped: list[tuple[str, str]] = []

        def apply(current):  # type: ignore[no-untyped-def]
            out = deposit(
                current,
                task_id=state.task_id,
                outcome=outcome,
                facts=state.confirmed_facts if self._persist else (),
                failures=state.failed_attempts if self._persist else (),
                tainted=task_was_tainted(state.messages),
                last_action_target={"files": files} if files else None,
                now=self._clock(),
                max_pending=self._max_pending,
            )
            result_flags.append(out.needs_compaction)
            dropped.extend(out.dropped)
            return out.memory

        await self._store.update_memory(session_id, apply)
        compaction = await self._compactor.maybe_compact(session_id, force=any(result_flags))
        return FinishReport(compaction=compaction.status, dropped=(*dropped, *compaction.dropped))

    @staticmethod
    def _redact(text: str, state: AgentState) -> str:
        token = state.canary_token
        return text.replace(token, _REDACTED) if token else text
