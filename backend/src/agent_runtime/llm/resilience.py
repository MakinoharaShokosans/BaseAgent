"""退避、熔断器、自适应并发。设计文档：docs/07_llm_gateway/01_gateway.md §5.2、§5.4、§7。

时钟、随机数都由调用方注入，测试里完全确定。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Literal


def backoff_delay(
    attempt: int,
    *,
    factor: float,
    max_sec: float,
    jitter: float,
    rng: Callable[[], float],
    retry_after: float | None,
    honor_retry_after: bool,
) -> float:
    """第 ``attempt`` 次失败（从 1 数）之后的等待秒数。

    ``factor ** attempt`` 指数增长，加上 ``jitter`` 比例的随机量，再封顶 ``max_sec``。
    服务商给了 ``Retry-After`` 就取两者较大者；这个值**不封顶**——封顶会让我们在窗口
    结束前就重试而再次被限流，是否值得等由调用方决定。
    """
    delay = min(factor**attempt * (1 + jitter * rng()), max_sec)
    if honor_retry_after and retry_after is not None:
        delay = max(delay, retry_after)
    return delay


def parse_retry_after(value: str | None) -> float | None:
    """只解析“秒数”形式；HTTP 日期形式按没有处理。"""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


BreakerState = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    """closed → 连续 N 次失败 → open（T 秒内拒绝）→ half_open（放行一次探测）。"""

    def __init__(self, *, open_after: int, open_sec: float, clock: Callable[[], float]) -> None:
        self._open_after = open_after
        self._open_sec = open_sec
        self._clock = clock
        self._failures = 0
        self._state: BreakerState = "closed"
        self.opened_at: float | None = None
        self._probe_started: float | None = None

    @property
    def state(self) -> BreakerState:
        return self._state

    def allow(self) -> bool:
        now = self._clock()
        if self._state == "closed":
            return True
        if self._state == "open":
            assert self.opened_at is not None
            if now - self.opened_at < self._open_sec:
                return False
            self._state, self._probe_started = "half_open", now
            return True
        # half_open：探测在途则挡住其余；探测迟迟没有结果（被取消等）时过一个窗口再放一次
        assert self._probe_started is not None
        if now - self._probe_started >= self._open_sec:
            self._probe_started = now
            return True
        return False

    def record_success(self) -> None:
        self._failures, self._state, self._probe_started = 0, "closed", None

    def record_failure(self) -> None:
        self._failures += 1
        if self._state != "closed" or self._failures >= self._open_after:
            self._state, self._probe_started = "open", None
            self.opened_at = self._clock()


class AdaptiveLimiter:
    """单端点并发上限。收到 429 减半（至少 1），连续成功“当前上限”次后放回一格。"""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit 必须至少为 1")
        self._max = limit
        self.limit = limit
        self.in_flight = 0
        self._streak = 0
        self._cond = asyncio.Condition()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._cond:
            await self._cond.wait_for(lambda: self.in_flight < self.limit)
            self.in_flight += 1
        try:
            yield
        finally:
            async with self._cond:
                self.in_flight -= 1
                self._cond.notify_all()

    def on_rate_limited(self) -> None:
        self.limit = max(1, self.limit // 2)
        self._streak = 0

    def on_success(self) -> None:
        if self.limit >= self._max:
            return
        self._streak += 1
        if self._streak >= self.limit:
            self.limit += 1
            self._streak = 0
