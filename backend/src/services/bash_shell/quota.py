"""全局内存池与并发槽位。设计文档：docs/05_security/02_shell_sandbox.md §3.3。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Grant:
    mb: int


class Queued(Exception):
    def __init__(self, estimated_wait_sec: float) -> None:
        super().__init__("配额已满")
        self.estimated_wait_sec = estimated_wait_sec


class GlobalMemoryBudget:
    def __init__(
        self, total_mb: int, max_concurrent: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._total = total_mb
        self._slots = max_concurrent
        self._free = total_mb
        self._active = 0
        self._waiting = 0
        self._cond = asyncio.Condition()
        self._durations: deque[float] = deque(maxlen=20)
        self._clock = clock

    @property
    def free_mb(self) -> int:
        return self._free

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return self._waiting

    def _fits(self, mb: int) -> bool:
        return self._active < self._slots and self._free >= mb

    def estimate_wait(self, mb: int) -> float:
        """内存缺口比例 × 近期平均耗时 × 排队深度。"""
        average = sum(self._durations) / len(self._durations) if self._durations else 5.0
        deficit = max(mb - self._free, 0) / max(self._total, 1)
        slots_full = 1.0 if self._active >= self._slots else 0.0
        return round(max(deficit, slots_full, 0.05) * average * (self._waiting + 1), 1)

    async def acquire(self, mb: int, *, wait_sec: float = 0.0) -> Grant:
        """``wait_sec <= 0``：池满立即抛 :class:`Queued`；否则等到有空位或超时（再抛 Queued）。"""
        mb = min(mb, self._total)  # 一个请求永远不会比整个池还大
        async with self._cond:
            if self._fits(mb):
                return self._take(mb)
            if wait_sec <= 0:
                raise Queued(self.estimate_wait(mb))
            self._waiting += 1
            try:
                await asyncio.wait_for(self._cond.wait_for(lambda: self._fits(mb)), wait_sec)
            except TimeoutError:
                raise Queued(self.estimate_wait(mb)) from None
            finally:
                self._waiting -= 1
            return self._take(mb)

    def _take(self, mb: int) -> Grant:
        self._free -= mb
        self._active += 1
        return Grant(mb)

    async def release(self, grant: Grant, *, duration_sec: float | None = None) -> None:
        async with self._cond:
            self._free = min(self._free + grant.mb, self._total)
            self._active = max(self._active - 1, 0)
            if duration_sec is not None:
                self._durations.append(duration_sec)
            self._cond.notify_all()
