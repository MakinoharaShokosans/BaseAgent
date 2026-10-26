"""把提炼结果合并进会话记忆：带出处、不静默丢失、受上限约束。设计文档：窗口与压缩 §1.5.1。

纯函数。提炼由模型完成，模型的输出是不可信的：没有真实出处的事实丢弃，命中注入样态的丢弃，
旧条目没被列入 ``superseded`` 就原样补回，超过上限淘汰最旧的。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection
from dataclasses import dataclass

from agent_runtime.core import FailedAttempt
from agent_runtime.guardrails import scan_injection
from agent_runtime.memory.models import Fact, Failure, SessionMemory


@dataclass(frozen=True, slots=True)
class ExtractedFact:
    text: str
    task_id: str


@dataclass(frozen=True, slots=True)
class ExtractedFailure:
    action: str
    failure_reason: str
    conclusion: str
    task_id: str


@dataclass(frozen=True, slots=True)
class Superseded:
    #: 被推翻或并入别处的旧事实原文，或旧失败尝试的 action
    text: str
    reason: str


@dataclass(frozen=True, slots=True)
class Extraction:
    summary: str | None
    facts: tuple[ExtractedFact, ...]
    failures: tuple[ExtractedFailure, ...]
    superseded: tuple[Superseded, ...]


@dataclass(frozen=True, slots=True)
class MergeLimits:
    max_facts: int
    max_failures: int
    summary_max_chars: int
    require_provenance: bool


@dataclass(frozen=True, slots=True)
class MergeResult:
    memory: SessionMemory
    #: 模型漏掉又未声明被取代、因此被补回的旧条目
    restored: tuple[str, ...]
    dropped: tuple[tuple[str, str], ...]
    summary_kept_old: bool


def merge_extraction(
    snapshot: SessionMemory,
    extraction: Extraction,
    *,
    valid_task_ids: Collection[str],
    limits: MergeLimits,
    now: float,
) -> MergeResult:
    dropped: list[tuple[str, str]] = []
    restored: list[str] = []
    superseded = {s.text for s in extraction.superseded}

    def acceptable(text: str, task_id: str) -> bool:
        if limits.require_provenance and task_id not in valid_task_ids:
            dropped.append((text[:80], "没有真实出处"))
            return False
        if hits := scan_injection(text):
            dropped.append((text[:80], f"命中注入样态：{hits[0].label}"))
            return False
        return True

    # 事实
    old_facts = {f.text: f for f in (*snapshot.facts, *snapshot.pending_facts)}
    facts: dict[str, Fact] = {}
    for item in extraction.facts:
        text = item.text.strip()
        if not text or text in facts:
            continue
        if text in old_facts:
            facts[text] = old_facts[text]  # 继承出处与信任
        elif acceptable(text, item.task_id):
            facts[text] = Fact(text, item.task_id, "untrusted", "compressor", now)
    for text, old in old_facts.items():
        if text not in facts and text not in superseded:
            facts[text] = old
            restored.append(text)
    kept_facts = _newest(list(facts.values()), limits.max_facts, lambda f: f.created_at, dropped)

    # 失败尝试
    old_failures = {f.attempt: f for f in (*snapshot.failures, *snapshot.pending_failures)}
    failures: dict[FailedAttempt, Failure] = {}
    for item in extraction.failures:
        attempt = FailedAttempt(
            action=item.action, failure_reason=item.failure_reason, conclusion=item.conclusion
        )
        if attempt in failures:
            continue
        if attempt in old_failures:
            failures[attempt] = old_failures[attempt]
        elif acceptable(f"{item.action}\n{item.failure_reason}\n{item.conclusion}", item.task_id):
            failures[attempt] = Failure(attempt, item.task_id, now)
    for attempt, old in old_failures.items():
        if attempt not in failures and attempt.action not in superseded:
            failures[attempt] = old
            restored.append(attempt.action)
    kept_failures = _newest(
        list(failures.values()), limits.max_failures, lambda f: f.created_at, dropped
    )

    # 摘要
    summary, kept_old = snapshot.summary, True
    candidate = (extraction.summary or "").strip()
    if candidate:
        if hits := scan_injection(candidate):
            dropped.append((candidate[:80], f"摘要命中注入样态：{hits[0].label}"))
        else:
            summary, kept_old = candidate[: limits.summary_max_chars], False

    merged = dataclasses.replace(
        snapshot,
        summary=summary,
        facts=tuple(kept_facts),
        failures=tuple(kept_failures),
        pending_facts=(),
        pending_failures=(),
        compaction_failures=0,
        retry_after=0.0,
    )
    return MergeResult(merged, tuple(restored), tuple(dropped), kept_old)


def _newest[T](
    items: list[T], cap: int, created: Callable[[T], float], dropped: list[tuple[str, str]]
) -> list[T]:
    if len(items) <= cap:
        return items
    ordered = sorted(range(len(items)), key=lambda i: created(items[i]))
    for i in ordered[: len(items) - cap]:
        dropped.append((str(items[i])[:80], "超出上限，淘汰最旧"))
    cut = set(ordered[: len(items) - cap])
    return [x for i, x in enumerate(items) if i not in cut]
