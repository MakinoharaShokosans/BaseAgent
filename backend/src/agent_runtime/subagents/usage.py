"""子级一次运行的统计与挂钟计时。没有 Token 额度：用量只是统计值，不对应任何上限。

挂钟只是单次子任务的硬超时（与每个工具调用的超时同类），不是预算。
设计文档：docs/06_subagents/README.md。
"""

from __future__ import annotations

import time
from collections.abc import Callable


class ChildUsage:
    def __init__(self, wall_time_sec: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.wall_time_sec = wall_time_sec
        self._clock = clock
        self._started = clock()
        self.used = 0

    def add_tokens(self, n: int) -> None:
        self.used += max(int(n), 0)  # 负数按 0 计

    @property
    def elapsed_sec(self) -> float:
        return self._clock() - self._started

    @property
    def remaining_wall_sec(self) -> float:
        return max(self.wall_time_sec - self.elapsed_sec, 0.0)

    @property
    def expired(self) -> bool:
        return self.remaining_wall_sec <= 0

    def reason(self) -> str:
        return f"单次运行的超时已到（{self.wall_time_sec:g} 秒）"
