"""任务收尾的沉淀：事实与失败尝试先进“近期新增”。设计文档：04_memory.md §5.2。

不改会话记忆段本身（摘要、已并入的事实），所以每个任务结束都不会让其后的缓存失效。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from agent_runtime.core import FailedAttempt
from agent_runtime.guardrails import scan_injection
from agent_runtime.memory.models import Fact, Failure, SessionMemory

Outcome = Literal["success", "terminated", "failed"]
MAX_FACT_CHARS: Final = 500


@dataclass(frozen=True, slots=True)
class DepositResult:
    memory: SessionMemory
    #: 近期新增超过上限，应提前并入
    needs_compaction: bool
    #: (内容, 原因)
    dropped: tuple[tuple[str, str], ...]


def _key(a: FailedAttempt) -> tuple[str, str, str]:
    return (a.action, a.failure_reason, a.conclusion)


def deposit(
    memory: SessionMemory,
    *,
    task_id: str,
    outcome: Outcome,
    facts: Sequence[str],
    failures: Sequence[FailedAttempt],
    tainted: bool,
    last_action_target: Mapping[str, Sequence[str]] | None,
    now: float,
    max_pending: int,
) -> DepositResult:
    """``tainted``：任务读过不可信内容，其事实标 ``untrusted``。失败任务只沉淀失败尝试。"""
    dropped: list[tuple[str, str]] = []
    known = {f.text for f in (*memory.facts, *memory.pending_facts)}
    new_facts: list[Fact] = []
    if outcome != "failed":
        for raw in facts:
            text = raw.strip()
            if not text:
                continue
            if len(text) > MAX_FACT_CHARS:
                dropped.append((text[:80], "超长"))
                continue
            if hits := scan_injection(text):
                dropped.append((text[:80], f"命中注入样态：{hits[0].label}"))
                continue
            if text in known:
                continue
            known.add(text)
            new_facts.append(
                Fact(text, task_id, "untrusted" if tainted else "trusted", "task", now)
            )

    seen = {_key(f.attempt) for f in (*memory.failures, *memory.pending_failures)}
    new_failures: list[Failure] = []
    for attempt in failures:
        if _key(attempt) in seen:
            continue
        joined = f"{attempt.action}\n{attempt.failure_reason}\n{attempt.conclusion}"
        if hits := scan_injection(joined):
            dropped.append((attempt.action[:80], f"命中注入样态：{hits[0].label}"))
            continue
        seen.add(_key(attempt))
        new_failures.append(Failure(attempt, task_id, now))

    target = memory.last_action_target
    if last_action_target:
        target = {k: tuple(v) for k, v in last_action_target.items()}
    updated = dataclasses.replace(
        memory,
        pending_facts=(*memory.pending_facts, *new_facts),
        pending_failures=(*memory.pending_failures, *new_failures),
        last_action_target=target,
    )
    return DepositResult(updated, len(updated.pending_facts) > max_pending, tuple(dropped))
