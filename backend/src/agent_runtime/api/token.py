"""本地令牌：生成、读取、比较、失败限速。设计文档：docs/09_api/03 §3。

令牌不写日志、不进事件、不进错误体；文件 0600、目录 0700；比较用常量时间。
"""

from __future__ import annotations

import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

_TOKEN_BYTES: Final = 32  # 256 位


def provision_token(path: Path, *, env: Mapping[str, str] | None = None, env_name: str = "") -> str:
    """优先用环境变量里的令牌；否则读文件；文件不存在就生成。删除文件并重启即轮换。"""
    if env_name and env is not None and (value := env.get(env_name, "").strip()):
        return value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            path.chmod(0o600)
            return existing
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


def tokens_match(expected: str, presented: str) -> bool:
    """常量时间比较；长度不同也走完整比较，不靠提前返回泄漏长度。"""
    return hmac.compare_digest(expected.encode(), presented.encode())


class FailureLimiter:
    """同一来源在窗口内连续失败达到阈值后，短暂拒绝（成功不清零窗口，窗口自然滑出）。"""

    def __init__(
        self, limit: int, window_sec: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit
        self._window = window_sec
        self._clock = clock
        self._failures: defaultdict[str, deque[float]] = defaultdict(deque)

    def blocked(self, source: str) -> bool:
        return len(self._recent(source)) >= self._limit

    def record_failure(self, source: str) -> None:
        self._recent(source).append(self._clock())

    def _recent(self, source: str) -> deque[float]:
        queue = self._failures[source]
        horizon = self._clock() - self._window
        while queue and queue[0] <= horizon:
            queue.popleft()
        if not queue:
            self._failures.pop(source, None)
            return self._failures[source]
        return queue
