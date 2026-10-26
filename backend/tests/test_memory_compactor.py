"""会话压缩：触发、滞回、退避、失败不丢记忆、并发。出处：02_window_and_compaction.md §1、§2。"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from agent_runtime.memory import Fact, MemoryStore, SessionMemory
from agent_runtime.memory.compactor import CompactionConfig, ExtractionRequest, SessionCompactor
from agent_runtime.memory.merge import ExtractedFact, Extraction, MergeLimits

CONFIG = CompactionConfig(
    token_limit=100,
    high_watermark=0.8,
    compaction_ratio=0.4,
    keep_recent_turns=2,
    backoff_sec=(30, 120, 600),
    limits=MergeLimits(
        max_facts=10, max_failures=5, summary_max_chars=100, require_provenance=True
    ),
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Extractor:
    def __init__(self, result: Extraction | BaseException | None = None) -> None:
        self.result = result if result is not None else Extraction("新摘要", (), (), ())
        self.requests: list[ExtractionRequest] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def __call__(self, request: ExtractionRequest) -> Extraction | None:
        self.requests.append(request)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
async def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    (tmp_path / "ws").mkdir()
    s = MemoryStore(tmp_path / "m.db")
    await s.open()
    await s.create_workspace("w", "p", str(tmp_path / "ws"))
    await s.create_session("s", "w")
    yield s
    await s.close()


async def fill(store: MemoryStore, pairs: int, tokens: int = 10) -> None:
    for i in range(pairs):
        await store.append_turn("s", f"t{i}", "user", f"q{i}", tokens)
        await store.append_turn("s", f"t{i}", "assistant", f"a{i}", tokens)


def compactor(
    store: MemoryStore,
    extractor: Extractor,
    clock: Clock | None = None,
    config: CompactionConfig = CONFIG,
) -> SessionCompactor:
    return SessionCompactor(store, extractor, config, clock=clock or Clock())


@pytest.mark.unit
async def test_below_the_high_watermark_nothing_happens(store: MemoryStore) -> None:
    await fill(store, 3)  # 60 < 80
    ex = Extractor()
    result = await compactor(store, ex).maybe_compact("s")
    assert result.status == "below_threshold" and ex.requests == []
    assert await store.load_memory("s") == SessionMemory()


@pytest.mark.unit
async def test_reaching_the_watermark_compacts_down_to_about_the_target(store: MemoryStore) -> None:
    await fill(store, 4)  # 80 == 高水位
    ex = Extractor()
    result = await compactor(store, ex).maybe_compact("s")
    mem = await store.load_memory("s")
    assert result.status == "compacted" and result.cut_id == mem.watermark == 4
    assert mem.summary == "新摘要"
    active = await store.turns_after("s", mem.watermark)
    assert sum(t.token_count for t in active) <= 50  # 压到高水位以下很多，不会立刻再次触发
    assert [t.content for t in ex.requests[0].turns] == ["q0", "a0", "q1", "a1"]


@pytest.mark.unit
async def test_the_original_turns_are_never_deleted(store: MemoryStore) -> None:
    await fill(store, 4)
    await compactor(store, Extractor()).maybe_compact("s")
    assert (await store.count_rows())["turns"] == 8


@pytest.mark.unit
async def test_the_watermark_only_moves_forward_across_many_tasks(store: MemoryStore) -> None:
    ex = Extractor()
    c = compactor(store, ex)
    marks = []
    for _ in range(30):  # 每个任务 10 个 Token，远小于上限：与真实的比例一致
        await fill(store, 1, tokens=5)
        await c.maybe_compact("s")
        marks.append((await store.load_memory("s")).watermark)
    assert marks == sorted(marks) and marks[-1] > marks[0]
    active = await store.turns_after("s", marks[-1])
    assert sum(t.token_count for t in active) < 80  # 活跃部分始终被控制在高水位下


@pytest.mark.unit
async def test_the_extractor_sees_the_existing_memory_and_the_pending_additions(
    store: MemoryStore,
) -> None:
    await fill(store, 4)
    pending = Fact("P", "t1", "trusted", "task", 1.0)
    await store.update_memory(
        "s", lambda m: dataclasses.replace(m, summary="旧摘要", pending_facts=(pending,))
    )
    ex = Extractor()
    await compactor(store, ex).maybe_compact("s")
    req = ex.requests[0]
    assert req.memory.summary == "旧摘要" and req.memory.pending_facts == (pending,)
    assert req.limits == CONFIG.limits


@pytest.mark.unit
async def test_facts_with_a_source_among_the_compacted_tasks_are_accepted_others_dropped(
    store: MemoryStore,
) -> None:
    await fill(store, 4)
    ex = Extractor(Extraction(None, (ExtractedFact("真", "t0"), ExtractedFact("编", "tX")), (), ()))
    await compactor(store, ex).maybe_compact("s")
    assert [f.text for f in (await store.load_memory("s")).facts] == ["真"]


# ----------------------------------------------------------------------- 失败


@pytest.mark.unit
@pytest.mark.parametrize("result", [None, RuntimeError("模型挂了"), TimeoutError()])
async def test_a_failed_extraction_changes_neither_memory_nor_watermark(
    store: MemoryStore, result: object
) -> None:
    await fill(store, 4)
    before = SessionMemory(summary="旧", facts=(Fact("F", "t0", "trusted", "task", 1.0),))
    await store.update_memory("s", lambda _: before)
    ex = Extractor()
    ex.result = result  # type: ignore[assignment]
    clock = Clock()
    res = await compactor(store, ex, clock).maybe_compact("s")
    after = await store.load_memory("s")
    assert res.status == "failed"
    assert (after.summary, after.facts, after.watermark) == ("旧", before.facts, 0)
    assert after.compaction_failures == 1 and after.retry_after == clock.now + 30


@pytest.mark.unit
async def test_cancellation_is_not_swallowed(store: MemoryStore) -> None:
    await fill(store, 4)
    ex = Extractor(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await compactor(store, ex).maybe_compact("s")


@pytest.mark.unit
async def test_failures_back_off_exponentially_and_success_resets(store: MemoryStore) -> None:
    await fill(store, 4)
    clock = Clock()
    ex = Extractor()
    ex.result = None  # type: ignore[assignment]
    c = compactor(store, ex, clock)
    waits = []
    for _ in range(5):
        clock.now = (await store.load_memory("s")).retry_after or clock.now
        await c.maybe_compact("s")
        m = await store.load_memory("s")
        waits.append(m.retry_after - clock.now)
    assert waits == [30, 120, 600, 600, 600]

    clock.now = m.retry_after
    ex.result = Extraction("好了", (), (), ())
    assert (await c.maybe_compact("s")).status == "compacted"
    m = await store.load_memory("s")
    assert (m.compaction_failures, m.retry_after) == (0, 0.0)


@pytest.mark.unit
async def test_during_backoff_the_extractor_is_not_called_again(store: MemoryStore) -> None:
    await fill(store, 4)
    clock = Clock()
    ex = Extractor()
    ex.result = None  # type: ignore[assignment]
    c = compactor(store, ex, clock)
    await c.maybe_compact("s")
    clock.now += 10
    assert (await c.maybe_compact("s")).status == "backing_off"
    assert len(ex.requests) == 1
    clock.now += 100
    await c.maybe_compact("s")
    assert len(ex.requests) == 2


# ----------------------------------------------------------------------- 强制并入与并发


@pytest.mark.unit
async def test_forcing_merges_pending_even_below_the_threshold_without_moving_the_watermark(
    store: MemoryStore,
) -> None:
    await fill(store, 1)
    pending = Fact("P", "t0", "trusted", "task", 1.0)
    await store.update_memory("s", lambda m: dataclasses.replace(m, pending_facts=(pending,)))
    ex = Extractor(Extraction("合并后", (ExtractedFact("P", "t0"),), (), ()))
    res = await compactor(store, ex).maybe_compact("s", force=True)
    m = await store.load_memory("s")
    assert res.status == "compacted" and m.watermark == 0
    assert m.pending_facts == () and [f.text for f in m.facts] == ["P"] and m.summary == "合并后"
    assert ex.requests[0].turns == ()


@pytest.mark.unit
async def test_something_deposited_while_the_model_was_thinking_is_not_lost(
    store: MemoryStore,
) -> None:
    await fill(store, 4)
    old = Fact("旧", "t0", "trusted", "task", 1.0)
    await store.update_memory("s", lambda m: dataclasses.replace(m, pending_facts=(old,)))
    ex = Extractor()
    ex.gate = asyncio.Event()
    task = asyncio.create_task(compactor(store, ex).maybe_compact("s"))
    await ex.started.wait()
    late = Fact("迟到", "t9", "trusted", "task", 2.0)
    await store.update_memory(
        "s", lambda m: dataclasses.replace(m, pending_facts=(*m.pending_facts, late))
    )
    ex.gate.set()
    await task
    m = await store.load_memory("s")
    assert m.pending_facts == (late,)
    assert "旧" in [f.text for f in m.facts]


@pytest.mark.unit
async def test_concurrent_requests_for_one_session_run_one_extraction(store: MemoryStore) -> None:
    await fill(store, 4)
    ex = Extractor()
    ex.gate = asyncio.Event()
    c = compactor(store, ex)
    first = asyncio.create_task(c.maybe_compact("s"))
    await ex.started.wait()
    second = asyncio.create_task(c.maybe_compact("s"))
    await asyncio.sleep(0.01)
    ex.gate.set()
    results = await asyncio.gather(first, second)
    assert len(ex.requests) == 1
    assert sorted(r.status for r in results) == ["below_threshold", "compacted"]


@pytest.mark.unit
async def test_different_sessions_do_not_block_each_other(store: MemoryStore) -> None:
    await store.create_session("s2", "w")
    ex = Extractor()
    ex.gate = asyncio.Event()
    c = compactor(store, ex)
    await fill(store, 4)
    for i in range(4):
        await store.append_turn("s2", f"u{i}", "user", "q", 10)
        await store.append_turn("s2", f"u{i}", "assistant", "a", 10)
    a = asyncio.create_task(c.maybe_compact("s"))
    b = asyncio.create_task(c.maybe_compact("s2"))
    await asyncio.sleep(0.05)
    assert len(ex.requests) == 2
    ex.gate.set()
    await asyncio.gather(a, b)


@pytest.mark.unit
async def test_a_source_among_the_existing_items_counts_as_real_even_if_not_in_the_cut(
    store: MemoryStore,
) -> None:
    await fill(store, 4)
    earlier = Fact("旧", "t_old", "trusted", "task", 1.0)
    await store.update_memory("s", lambda m: dataclasses.replace(m, pending_facts=(earlier,)))
    ex = Extractor(Extraction(None, (ExtractedFact("由旧任务得出", "t_old"),), (), ()))
    await compactor(store, ex).maybe_compact("s")
    assert "由旧任务得出" in [f.text for f in (await store.load_memory("s")).facts]
