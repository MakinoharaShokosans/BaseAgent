"""事件中心与事件库。出处：docs/08_observability/01_events.md、docs/09_api/02_events_contract.md。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.observability import (
    Event,
    EventHub,
    EventStore,
    clip,
)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[EventStore]:
    async with EventStore(tmp_path / "events.db") as s:
        yield s


BACKGROUND: set[asyncio.Task[Any]] = set()


def spawn[T](coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    task = asyncio.create_task(coro)
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)
    return task


async def head(stream: AsyncIterator[Event]) -> Event:
    return await anext(stream)


async def collect(
    hub: EventHub, task: str, after: int = 0, n: int | None = None, **kw: float
) -> list[Event]:
    out: list[Event] = []
    async for ev in hub.stream(task, after, **kw):
        out.append(ev)
        if n is not None and len(out) >= n:
            break
    return out


# ------------------------------------------------------------------ 信封与整理


@pytest.mark.unit
def test_clip_flattens_whitespace_and_marks_the_cut() -> None:
    assert clip("a\n  b\tc", 10) == "a b c"
    assert clip("x" * 20, 5) == "xxxx…"


@pytest.mark.unit
async def test_seq_is_per_task_monotonic_and_the_envelope_is_complete() -> None:
    hub = EventHub(clock=lambda: 12.5)
    a1 = await hub.emit("a", "plan", {"x": 1})
    a2 = await hub.emit("a", "plan")
    b1 = await hub.emit("b", "plan")
    assert a1 and a2 and b1 and (a1.seq, a2.seq, b1.seq) == (1, 2, 1)
    assert a1.to_dict() == {"event": "plan", "task_id": "a", "seq": 1, "ts": 12.5, "v": 1, "x": 1}


@pytest.mark.unit
async def test_payload_cannot_forge_envelope_fields() -> None:
    hub = EventHub()
    ev = await hub.emit("a", "plan", {"seq": 99, "task_id": "other", "event": "x", "k": 1})
    assert ev is not None
    assert ev.to_dict()["seq"] == 1 and ev.to_dict()["task_id"] == "a"
    assert ev.to_dict()["event"] == "plan"


@pytest.mark.unit
async def test_summary_like_fields_are_clipped_even_when_nested() -> None:
    hub = EventHub(clip_chars=10)
    ev = await hub.emit(
        "a",
        "node.finished",
        {
            "output_summary": "x" * 50,
            "command": "y" * 50,
            "sub": {"reason": "z\n" * 30},
            "items": [{"excerpt": "w" * 40}],
        },
    )
    assert ev is not None
    d = ev.to_dict()
    assert len(d["output_summary"]) == 10  # type: ignore[arg-type]
    assert d["command"] == "y" * 50  # 命令不截断
    assert len(d["sub"]["reason"]) <= 10  # type: ignore[index]
    assert len(d["items"][0]["excerpt"]) == 10  # type: ignore[index]


# ------------------------------------------------------------------ 永不抛


@pytest.mark.unit
async def test_emit_never_raises_even_when_the_store_is_broken(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "never_opened.db")  # 未 open：每次写都会报错
    hub = EventHub(store)
    ev = await hub.emit("a", "plan")
    assert ev is not None and ev.seq == 1  # 仍进缓冲
    assert [e.seq for e in await collect(hub, "a", n=1)] == [1]


@pytest.mark.unit
async def test_unserialisable_payload_does_not_break_the_task() -> None:
    hub = EventHub()
    assert await hub.emit("a", "plan", {"x": object()}) is not None


# ------------------------------------------------------------------ 事件库


@pytest.mark.unit
async def test_events_survive_a_restart_and_seq_continues(tmp_path: Path) -> None:
    path = tmp_path / "e.db"
    async with EventStore(path) as s1:
        hub = EventHub(s1)
        await hub.emit("a", "task.started", {"task_goal": "g"})
        await hub.emit("a", "plan", {"thought": "t"})
    async with EventStore(path) as s2:
        hub2 = EventHub(s2)
        ev = await hub2.emit("a", "node.finished")
        assert ev is not None and ev.seq == 3
        history = await hub2.history("a")
        assert [e.name for e in history] == ["task.started", "plan", "node.finished"]
        assert history[0].data["task_goal"] == "g"


@pytest.mark.unit
async def test_forget_removes_events_buffers_and_seq(store: EventStore) -> None:
    hub = EventHub(store)
    await hub.emit("a", "plan")
    await hub.forget("a")
    assert await hub.history("a") == [] and hub.last_seq("a") == 0
    ev = await hub.emit("a", "plan")
    assert ev is not None and ev.seq == 1


@pytest.mark.unit
async def test_prune_by_age_and_by_count(store: EventStore) -> None:
    clock = {"t": 0.0}
    hub = EventHub(store, clock=lambda: clock["t"])
    for i, task in enumerate(["t1", "t2", "t3", "t4"]):
        clock["t"] = float(i * 10)
        await hub.emit(task, "task.started")
    assert await store.prune(older_than=15.0, keep_tasks=None) == 2  # t1(0) t2(10)
    assert await store.prune(older_than=None, keep_tasks=1) == 1  # 只留最近的 t4
    assert await store.max_seq("t4") == 1 and await store.max_seq("t3") == 0


# ------------------------------------------------------------------ 订阅


@pytest.mark.unit
async def test_replay_then_live_has_no_duplicates_and_no_gaps() -> None:
    hub = EventHub()
    for _ in range(3):
        await hub.emit("a", "plan")

    async def late() -> None:
        await asyncio.sleep(0)
        await hub.emit("a", "tool.call")
        await hub.emit("a", "task.finished", {"status": "succeeded"})

    spawn(late())
    got = await collect(hub, "a", after=1)
    assert [e.seq for e in got] == [2, 3, 4, 5]
    assert got[-1].name == "task.finished"


@pytest.mark.unit
async def test_a_finished_task_replays_then_closes() -> None:
    hub = EventHub()
    await hub.emit("a", "plan")
    await hub.emit("a", "task.finished", {})
    assert [e.name for e in await collect(hub, "a")] == ["plan", "task.finished"]


@pytest.mark.unit
async def test_cursor_at_the_end_of_a_finished_task_closes_immediately() -> None:
    hub = EventHub()
    await hub.emit("a", "task.finished", {})
    assert await collect(hub, "a", after=1) == []


@pytest.mark.unit
async def test_waiting_for_approval_does_not_close_the_stream() -> None:
    hub = EventHub()
    await hub.emit("a", "task.waiting_for_approval", {"approval_id": "x"})

    async def later() -> None:
        await asyncio.sleep(0.01)
        await hub.emit("a", "task.approved")
        await hub.emit("a", "task.finished", {})

    spawn(later())
    names = [e.name for e in await collect(hub, "a")]
    assert names == ["task.waiting_for_approval", "task.approved", "task.finished"]


@pytest.mark.unit
async def test_a_resumed_task_keeps_streaming_past_an_old_finished_event() -> None:
    hub = EventHub()
    await hub.emit("a", "task.finished", {"status": "terminated"})
    await hub.emit("a", "task.started")
    await hub.emit("a", "task.finished", {"status": "succeeded"})
    assert [e.seq for e in await collect(hub, "a", after=1)] == [2, 3]
    assert [e.seq for e in await collect(hub, "a")] == [1, 2, 3]  # 重放越过中间的终态


@pytest.mark.unit
async def test_heartbeat_has_no_seq_and_does_not_advance_the_cursor() -> None:
    hub = EventHub()
    await hub.emit("a", "plan")
    stream = hub.stream("a", 1, heartbeat_sec=0.01)
    beat = await anext(stream)
    assert beat.name == "heartbeat" and beat.is_transport and beat.seq == 0
    await hub.emit("a", "task.finished", {})
    rest = [e async for e in stream]
    assert [e.seq for e in rest if not e.is_transport] == [2]


@pytest.mark.unit
async def test_cursor_older_than_the_buffer_without_a_store_is_a_gap() -> None:
    hub = EventHub(buffer_events=3)
    for _ in range(10):
        await hub.emit("a", "plan")
    got = await collect(hub, "a", after=2)
    assert len(got) == 1 and got[0].name == "task.error" and got[0].data["code"] == "STREAM_GAP"


@pytest.mark.unit
async def test_the_store_covers_what_the_hot_buffer_has_dropped(store: EventStore) -> None:
    hub = EventHub(store, buffer_events=3)
    for _ in range(10):
        await hub.emit("a", "plan")
    await hub.emit("a", "task.finished", {})
    got = await collect(hub, "a", after=2)
    assert [e.seq for e in got] == list(range(3, 12))


@pytest.mark.unit
async def test_pruned_history_is_a_gap_not_a_silent_skip(store: EventStore) -> None:
    hub = EventHub(store, buffer_events=2)
    for _ in range(5):
        await hub.emit("a", "plan")
    await store._require().execute("DELETE FROM task_events WHERE seq <= 2")  # 模拟清理
    await store._require().commit()
    got = await collect(hub, "a", after=0)
    assert got[0].name == "task.error" and got[0].data["code"] == "STREAM_GAP"


@pytest.mark.unit
async def test_a_cursor_ahead_of_the_server_is_a_gap() -> None:
    hub = EventHub()
    await hub.emit("a", "plan")
    got = await collect(hub, "a", after=50)
    assert got[0].data["code"] == "STREAM_GAP"


@pytest.mark.unit
async def test_a_slow_consumer_never_blocks_emit_and_is_told_it_lost_events() -> None:
    hub = EventHub(subscriber_queue=2)
    stream = hub.stream("a", 0, heartbeat_sec=5)
    first = spawn(head(stream))
    await asyncio.sleep(0)  # 订阅者已登记
    await hub.emit("a", "plan")
    await first
    for _ in range(6):  # 消费者不读：队列 2，后 4 条被丢
        await asyncio.wait_for(hub.emit("a", "plan"), timeout=1)
    rest = [e async for e in stream]
    assert [e.seq for e in rest[:2]] == [2, 3]
    dropped = rest[-1]
    assert dropped.name == "stream.dropped"
    assert (dropped.data["from_seq"], dropped.data["to_seq"]) == (4, 7)
    assert len(rest) == 3


@pytest.mark.unit
async def test_a_dropped_consumer_can_resume_from_its_cursor_without_loss(
    store: EventStore,
) -> None:
    hub = EventHub(store, subscriber_queue=1)
    stream = hub.stream("a", 0)
    first = spawn(head(stream))
    await asyncio.sleep(0)
    for _ in range(5):
        await hub.emit("a", "plan")
    await hub.emit("a", "task.finished", {})
    seen = [await first]
    async for e in stream:
        if e.name == "stream.dropped":
            break
        seen.append(e)
    again = await collect(hub, "a", after=seen[-1].seq)
    assert [e.seq for e in seen + again] == list(range(1, 7))


@pytest.mark.unit
async def test_unsubscribing_releases_the_subscriber() -> None:
    hub = EventHub()
    stream = hub.stream("a", 0, heartbeat_sec=0.01)
    await anext(stream)  # 心跳
    await stream.aclose()
    assert hub._subs == {}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.unit
async def test_buffers_of_cold_tasks_are_bounded_but_subscribed_ones_stay() -> None:
    hub = EventHub(max_buffered_tasks=2)
    stream = hub.stream("keep", 0, heartbeat_sec=5)
    waiter = spawn(head(stream))
    await asyncio.sleep(0)
    await hub.emit("keep", "plan")
    for i in range(5):
        await hub.emit(f"t{i}", "plan")
    await waiter
    assert "keep" in hub._buffers and len(hub._buffers) <= 3  # pyright: ignore[reportPrivateUsage]
    await stream.aclose()
