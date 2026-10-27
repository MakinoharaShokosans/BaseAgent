"""会话压缩：水位线触发、对齐切片、提炼、合并、失败退避。设计文档：02_window_and_compaction.md §1。

提炼器由上层注入（它要调用模型）。压缩是增强项，绝不阻断主流程：任何失败都回退为上一版记忆，
水位线不推进，并按退避序列推迟下一次尝试。
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from loguru import logger

from agent_runtime.memory.merge import Extraction, MergeLimits, merge_extraction
from agent_runtime.memory.models import SessionMemory, Turn
from agent_runtime.memory.preferences import SessionPreferences
from agent_runtime.memory.slicing import split_for_compaction
from agent_runtime.memory.store import MemoryStore

Status = Literal["below_threshold", "backing_off", "compacted", "failed", "nothing_to_compact"]


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    token_limit: int
    high_watermark: float
    compaction_ratio: float
    keep_recent_turns: int
    backoff_sec: tuple[int, ...]
    limits: MergeLimits


@dataclass(frozen=True, slots=True)
class ExtractionRequest:
    memory: SessionMemory
    #: 待压缩的轮次；只为并入“近期新增”时为空
    turns: tuple[Turn, ...]
    limits: MergeLimits


class Extractor(Protocol):
    async def __call__(self, request: ExtractionRequest) -> Extraction | None: ...


@dataclass(frozen=True, slots=True)
class CompactionResult:
    status: Status
    cut_id: int = 0
    restored: tuple[str, ...] = ()
    dropped: tuple[tuple[str, str], ...] = ()
    #: 本次并入摘要的轮次数
    turns: int = 0


class SessionCompactor:
    def __init__(
        self,
        store: MemoryStore,
        extractor: Extractor | Callable[[ExtractionRequest], Awaitable[Extraction | None]],
        config: CompactionConfig,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._extract = extractor
        self._cfg = config
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}

    async def maybe_compact(self, session_id: str, *, force: bool = False) -> CompactionResult:
        """``force``：近期新增超限时提前并入，即使活跃轮次没到水位。"""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:  # 同一会话同一时刻只有一次压缩；后到的重新判断，通常已不需要
            return await self._run(session_id, force, manual=False)

    async def compact_now(self, session_id: str) -> CompactionResult:
        """用户手动触发：不看水位线与退避，除最近 ``keep_recent_turns`` 条外全部并入摘要。

        没有可并入的轮次、也没有待合并的近期新增时直接返回 ``nothing_to_compact``，不调用提炼器。
        """
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await self._run(session_id, True, manual=True)

    async def _run(self, session_id: str, force: bool, *, manual: bool) -> CompactionResult:
        cfg = self._cfg
        snapshot = await self._store.load_memory(session_id)
        now = self._clock()
        if now < snapshot.retry_after and not manual:  # 手动是用户明确的意图，不受退避约束
            return CompactionResult("backing_off")
        active = await self._store.turns_after(session_id, snapshot.watermark)
        total = sum(t.token_count for t in active)
        # 会话可以覆盖上下文上限（设置面板的滑动条）；没有覆盖时用配置值
        prefs = SessionPreferences.from_mapping(await self._store.get_session_settings(session_id))
        limit = prefs.context_token_limit or cfg.token_limit
        if total < limit * cfg.high_watermark and not force:
            return CompactionResult("below_threshold")

        # 手动：目标取到够不着，切片退到“允许范围内最后一个 assistant 节点”，即尽可能多地压
        target = sys.maxsize if manual else int(limit * cfg.compaction_ratio)
        head, _ = split_for_compaction(active, target, cfg.keep_recent_turns)
        if manual and not head and not (snapshot.pending_facts or snapshot.pending_failures):
            return CompactionResult("nothing_to_compact")
        request = ExtractionRequest(snapshot, tuple(head), cfg.limits)
        try:
            extraction = await self._extract(request)
        except Exception as exc:  # CancelledError 是 BaseException，不会被吞
            logger.warning("会话压缩失败（{}）：{!r}", session_id, exc)
            extraction = None
        if extraction is None:
            await self._record_failure(session_id, now)
            return CompactionResult("failed")

        valid = {t.task_id for t in head}
        valid |= {i.task_id for i in (*snapshot.facts, *snapshot.pending_facts)}
        valid |= {i.task_id for i in (*snapshot.failures, *snapshot.pending_failures)}
        merged = merge_extraction(
            snapshot, extraction, valid_task_ids=valid, limits=cfg.limits, now=now
        )
        cut = head[-1].id if head else snapshot.watermark

        def apply(current: SessionMemory) -> SessionMemory:
            # 提炼期间新沉淀的“近期新增”不在快照里，必须留下
            late_facts = tuple(f for f in current.pending_facts if f not in snapshot.pending_facts)
            late_failures = tuple(
                f for f in current.pending_failures if f not in snapshot.pending_failures
            )
            return dataclasses.replace(
                merged.memory,
                watermark=cut,  # 同会话串行；store 还会拒绝回退
                pending_facts=late_facts,
                pending_failures=late_failures,
                last_action_target=current.last_action_target,
            )

        await self._store.update_memory(session_id, apply)
        return CompactionResult("compacted", cut, merged.restored, merged.dropped, len(head))

    async def _record_failure(self, session_id: str, now: float) -> None:
        backoff = self._cfg.backoff_sec

        def apply(current: SessionMemory) -> SessionMemory:
            n = current.compaction_failures + 1
            wait = backoff[min(n - 1, len(backoff) - 1)] if backoff else 0
            return dataclasses.replace(current, compaction_failures=n, retry_after=now + wait)

        await self._store.update_memory(session_id, apply)
