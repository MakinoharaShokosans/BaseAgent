"""事件中心：分配序号、持久化、热缓存、订阅分发。设计文档：docs/08_observability/01_events.md。

- ``seq`` 只在这里分配，每个任务内单调递增；进程重启或续跑后接着已有的最大值。
- 发事件永远不抛异常（旁路）；持久化失败只告警，事件仍进缓存与订阅者。
- 每个订阅者一个有界队列：满了就丢弃该订阅者的事件，并在它读完已有事件后收到
  ``stream.dropped`` 然后流结束，让它带着游标重连。任务从不被订阅者拖慢。
- 重放：缓冲覆盖就用缓冲，否则读事件库；两者都覆盖不到是游标缺口（``STREAM_GAP``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict, deque
from collections.abc import AsyncGenerator, Callable, Mapping
from typing import Final

from loguru import logger

from agent_runtime.observability.event_store import EventStore
from agent_runtime.observability.events import (
    HEARTBEAT,
    STREAM_DROPPED,
    TERMINAL_EVENTS,
    Event,
    sanitize_payload,
)

_PAGE: Final = 500
STREAM_GAP: Final = "STREAM_GAP"
GLOBAL_LIFECYCLE_EVENTS: Final = frozenset(
    {
        "task.started",
        "task.waiting_for_approval",
        "task.approved",
        "task.rejected",
        "task.finished",
        "task.error",
    }
)


class _Subscriber:
    __slots__ = ("lost_from", "lost_to", "queue")

    def __init__(self, size: int) -> None:
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=size)
        self.lost_from = 0
        self.lost_to = 0


class EventHub:
    def __init__(
        self,
        store: EventStore | None = None,
        *,
        buffer_events: int = 1000,
        subscriber_queue: int = 256,
        clip_chars: int = 300,
        max_buffered_tasks: int = 64,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._buffer_events = buffer_events
        self._queue_size = subscriber_queue
        self._clip = clip_chars
        self._max_tasks = max_buffered_tasks
        self._clock = clock
        self._seq: dict[str, int] = {}
        self._buffers: OrderedDict[str, deque[Event]] = OrderedDict()
        self._subs: dict[str, set[_Subscriber]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sinks: list[Callable[[Event], None]] = []
        self._global_subs: set[_Subscriber] = set()
        self._task_meta: dict[str, dict[str, str]] = {}

    def add_sink(self, sink: Callable[[Event], None]) -> None:
        """旁路：每个事件发布后同步调用（如轨迹写入）。sink 抛出的异常只告警。"""
        self._sinks.append(sink)

    # ------------------------------------------------------------------ 发出

    async def emit(
        self, task_id: str, name: str, data: Mapping[str, object] | None = None
    ) -> Event | None:
        """发出一个事件，返回已分配序号的事件；任何内部失败都只记录并返回 None。"""
        try:
            lock = self._locks.setdefault(task_id, asyncio.Lock())
            async with lock:
                seq = await self._next_seq(task_id)
                payload = sanitize_payload(dict(data or {}), self._clip)
                assert isinstance(payload, dict)
                event = Event(task_id, seq, self._clock(), name, payload)  # pyright: ignore[reportUnknownArgumentType]
                if self._store is not None:
                    try:
                        await self._store.append(event)
                    except Exception:
                        logger.warning("事件 {}#{} 未能持久化；仍在缓存与订阅中", task_id, seq)
                self._publish(event)
                return event
        except Exception:
            logger.exception("发出事件 {} 失败；任务继续", name)
            return None

    async def transient(self, task_id: str, name: str, data: Mapping[str, object]) -> None:
        """瞬态事件：不分配序号、不持久化、不进缓冲，只发给当前订阅者。

        流式增量与命令实时输出用；刷新后由 ``model.reply`` / ``tool.result`` 的权威内容恢复。
        """
        try:
            event = Event(task_id, 0, self._clock(), name, dict(data))
            self._publish(event, buffer=False)
        except Exception:
            logger.exception("发出瞬态事件 {} 失败；任务继续", name)

    async def _next_seq(self, task_id: str) -> int:
        current = self._seq.get(task_id)
        if current is None:
            current = 0
            if self._store is not None:
                try:
                    current = await self._store.max_seq(task_id)
                except Exception:
                    logger.warning("读取 {} 的最大序号失败；从缓冲推断", task_id)
        self._seq[task_id] = current + 1
        return current + 1

    def _publish(self, event: Event, *, buffer: bool = True) -> None:
        if buffer:
            for sink in self._sinks:
                try:
                    sink(event)
                except Exception:
                    logger.warning("事件旁路 {} 失败（已忽略）", getattr(sink, "__name__", "sink"))
        if buffer:
            buffered = self._buffers.get(event.task_id)
            if buffered is None:
                buffered = self._buffers[event.task_id] = deque(maxlen=self._buffer_events)
            buffered.append(event)
            self._buffers.move_to_end(event.task_id)
            self._evict_cold_tasks()
        for sub in self._subs.get(event.task_id, ()):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                if event.seq == 0:
                    continue  # 瞬态增量丢了就丢了，不触发重连
                if not sub.lost_from:
                    sub.lost_from = event.seq
                sub.lost_to = event.seq
        if event.name in GLOBAL_LIFECYCLE_EVENTS:
            if event.name == "task.started":
                s_id = str(event.data.get("session_id", ""))
                w_id = str(event.data.get("workspace_id", ""))
                if s_id or w_id:
                    self._task_meta[event.task_id] = {
                        "session_id": s_id,
                        "workspace_id": w_id,
                    }
            global_event = event
            meta = self._task_meta.get(event.task_id)
            if meta and (
                "session_id" not in event.data or "workspace_id" not in event.data
            ):
                enriched_data = dict(event.data)
                if "session_id" not in enriched_data and meta["session_id"]:
                    enriched_data["session_id"] = meta["session_id"]
                if "workspace_id" not in enriched_data and meta["workspace_id"]:
                    enriched_data["workspace_id"] = meta["workspace_id"]
                global_event = Event(
                    event.task_id,
                    event.seq,
                    event.ts,
                    event.name,
                    enriched_data,
                )
            for sub in list(self._global_subs):
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(global_event)

    def _evict_cold_tasks(self) -> None:
        while len(self._buffers) > self._max_tasks:
            victim = next((t for t in self._buffers if not self._subs.get(t)), None)
            if victim is None:
                return
            del self._buffers[victim]
            self._task_meta.pop(victim, None)

    # ------------------------------------------------------------------ 查询

    def last_seq(self, task_id: str) -> int:
        """已知的最大序号（进程内）；从未发过事件为 0。"""
        return self._seq.get(task_id, 0)

    async def history(self, task_id: str, after_seq: int = 0, limit: int = 1000) -> list[Event]:
        """按需读取历史（时间线、检视器用），不依赖热缓存。"""
        if self._store is not None:
            try:
                return await self._store.read(task_id, after_seq, limit)
            except Exception:
                logger.warning("读取事件库失败；退回缓冲")
        buffered = [e for e in self._buffers.get(task_id, ()) if e.seq > after_seq]
        return buffered[:limit]

    async def count(self, task_id: str, name: str) -> int:
        """某任务已发出的某类事件数（续跑后接着数阶段序号用）。"""
        total, cursor = 0, 0
        while True:
            page = await self.history(task_id, cursor, _PAGE)
            if not page:
                return total
            total += sum(1 for e in page if e.name == name)
            cursor = page[-1].seq

    async def forget(self, task_id: str) -> None:
        """任务被删除：事件、缓冲、序号一起清掉。"""
        self._buffers.pop(task_id, None)
        self._seq.pop(task_id, None)
        self._locks.pop(task_id, None)
        if self._store is not None:
            try:
                await self._store.delete_task(task_id)
            except Exception:
                logger.warning("删除 {} 的事件失败", task_id)

    # ------------------------------------------------------------------ 订阅

    async def stream(
        self, task_id: str, after_seq: int = 0, *, heartbeat_sec: float = 15.0
    ) -> AsyncGenerator[Event, None]:
        """先重放 ``seq > after_seq`` 的事件，再接实时。

        遇到 ``task.finished`` 就结束（挂起等待审批不结束）；游标缺口发 ``STREAM_GAP`` 后结束；
        慢消费者发 ``stream.dropped`` 后结束。心跳是 ``seq == 0`` 的传输事件。
        """
        sub = _Subscriber(self._queue_size)
        self._subs.setdefault(task_id, set()).add(sub)
        try:
            if task_id not in self._seq:
                await self._next_seq_probe(task_id)
            high = self._seq.get(task_id, 0)
            cursor = after_seq
            gap, replay = await self._replay(task_id, after_seq, high)
            if gap:
                yield self._transport(task_id, "task.error", {"code": STREAM_GAP, "message": gap})
                return
            last: Event | None = None
            for event in replay:
                yield event
                cursor, last = event.seq, event
            if cursor == high and high:
                tail = last or await self._event_at(task_id, high)
                if tail is not None and tail.name in TERMINAL_EVENTS:
                    return
            while True:
                if sub.queue.empty() and sub.lost_from:
                    yield self._transport(
                        task_id,
                        STREAM_DROPPED,
                        {"from_seq": sub.lost_from, "to_seq": sub.lost_to},
                    )
                    return
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat_sec)
                except TimeoutError:
                    yield self._transport(task_id, HEARTBEAT, {})
                    continue
                if event.seq == 0:
                    yield event  # 瞬态增量不推进游标
                    continue
                if event.seq <= cursor:
                    continue
                yield event
                cursor = event.seq
                if event.name in TERMINAL_EVENTS:
                    return
        finally:
            subs = self._subs.get(task_id)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    del self._subs[task_id]

    async def global_stream(self, *, heartbeat_sec: float = 15.0) -> AsyncGenerator[Event, None]:
        """全局事件流：持续广播所有任务的生命周期事件（started, finished, approval 等）。"""
        sub = _Subscriber(self._queue_size)
        self._global_subs.add(sub)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat_sec)
                except TimeoutError:
                    yield self._transport("", HEARTBEAT, {})
                    continue
                yield event
        finally:
            self._global_subs.discard(sub)

    async def _event_at(self, task_id: str, seq: int) -> Event | None:
        buffer = self._buffers.get(task_id)
        if buffer and buffer[-1].seq == seq:
            return buffer[-1]
        if self._store is not None:
            try:
                found = await self._store.read(task_id, seq - 1, 1)
            except Exception:
                return None
            return found[0] if found and found[0].seq == seq else None
        return None

    async def _next_seq_probe(self, task_id: str) -> None:
        if self._store is None:
            return
        try:
            existing = await self._store.max_seq(task_id)
        except Exception:
            return
        if existing:
            self._seq.setdefault(task_id, existing)

    def _transport(self, task_id: str, name: str, data: Mapping[str, object]) -> Event:
        return Event(task_id, 0, self._clock(), name, dict(data))

    async def _replay(self, task_id: str, after_seq: int, high: int) -> tuple[str, list[Event]]:
        """返回 (缺口说明, 事件)。缺口说明非空表示游标已滑出可重放的范围。"""
        if after_seq >= high:
            if after_seq > high:
                return "游标超前于服务端已知的最新序号", []
            return "", []
        buffered = [e for e in self._buffers.get(task_id, ()) if after_seq < e.seq <= high]
        if buffered and buffered[0].seq == after_seq + 1 and buffered[-1].seq == high:
            return "", buffered
        if self._store is not None:
            try:
                events: list[Event] = []
                cursor = after_seq
                while cursor < high:
                    page = await self._store.read(task_id, cursor, _PAGE)
                    page = [e for e in page if e.seq <= high]
                    if not page:
                        break
                    events.extend(page)
                    cursor = page[-1].seq
                if events and events[0].seq == after_seq + 1:
                    return "", events
                if events:
                    return f"序号 {after_seq + 1}..{events[0].seq - 1} 已被清理", []
            except Exception:
                logger.warning("事件库重放失败；尝试缓冲")
        if buffered and buffered[0].seq == after_seq + 1:
            return "", buffered
        return "游标早于可重放的最旧事件", []
