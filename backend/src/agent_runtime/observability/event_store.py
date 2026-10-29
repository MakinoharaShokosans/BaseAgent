"""事件持久化：``task_events`` 表。设计文档：docs/08_observability/01_events.md §6。

旁路存储：写失败由调用方告警，不影响任务。与检查点库分开（写入频率差异大）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import aiosqlite

from agent_runtime.observability.events import Event

_DB_MODE: Final = 0o600

_MIGRATIONS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE task_events (
        task_id  TEXT NOT NULL,
        seq      INTEGER NOT NULL,
        ts       REAL NOT NULL,
        event    TEXT NOT NULL,
        payload  TEXT NOT NULL,
        v        INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (task_id, seq)
    );
    CREATE INDEX idx_task_events_ts ON task_events(ts);
    """,
)


class EventStoreError(RuntimeError):
    pass


class EventStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        if self._conn is not None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(self._path, os.O_CREAT | os.O_WRONLY, _DB_MODE))
            conn = await aiosqlite.connect(self._path)
        except (OSError, sqlite3.Error) as exc:
            raise EventStoreError(f"无法打开事件库 {self._path}：{exc}") from exc
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA synchronous=NORMAL")
            row = await (await conn.execute("PRAGMA user_version")).fetchone()
            version = int(row[0]) if row else 0
            if version > len(_MIGRATIONS):
                raise EventStoreError(f"事件库版本 {version} 高于当前程序支持的 {len(_MIGRATIONS)}")
            for target in range(version + 1, len(_MIGRATIONS) + 1):
                await conn.executescript(
                    f"BEGIN;{_MIGRATIONS[target - 1]}PRAGMA user_version={target};COMMIT;"
                )
        except BaseException:
            await conn.close()
            raise
        self._conn = conn

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise EventStoreError("事件库尚未 open")
        return self._conn

    # ------------------------------------------------------------------ 读写

    async def append(self, event: Event) -> None:
        conn = self._require()
        payload = json.dumps(dict(event.data), ensure_ascii=False, default=str)
        async with self._lock:
            await conn.execute(
                "INSERT OR REPLACE INTO task_events (task_id, seq, ts, event, payload, v)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (event.task_id, event.seq, event.ts, event.name, payload, event.v),
            )
            await conn.commit()

    async def max_seq(self, task_id: str) -> int:
        cursor = await self._require().execute(
            "SELECT COALESCE(MAX(seq), 0) FROM task_events WHERE task_id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def read(self, task_id: str, after_seq: int = 0, limit: int = 1000) -> list[Event]:
        cursor = await self._require().execute(
            "SELECT seq, ts, event, payload, v FROM task_events"
            " WHERE task_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (task_id, after_seq, limit),
        )
        rows = await cursor.fetchall()
        return [
            Event(task_id, int(seq), float(ts), str(name), json.loads(payload), int(v))
            for seq, ts, name, payload, v in rows
        ]

    async def delete_task(self, task_id: str) -> None:
        conn = self._require()
        async with self._lock:
            await conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            await conn.commit()

    async def prune(self, *, older_than: float | None, keep_tasks: int | None) -> int:
        """按保留策略清理：早于 ``older_than`` 的任务整体删除，只保留最近 ``keep_tasks`` 个任务。

        “任务的时间”取它最后一条事件的时间。返回删除的任务数。0/None 表示该维度不限。
        """
        conn = self._require()
        async with self._lock:
            rows = await (
                await conn.execute(
                    "SELECT task_id, MAX(ts) AS last FROM task_events GROUP BY task_id"
                    " ORDER BY last DESC"
                )
            ).fetchall()
            doomed: list[str] = []
            for rank, (task_id, last) in enumerate(rows):
                too_old = older_than is not None and float(last) < older_than
                over = keep_tasks is not None and rank >= keep_tasks
                if too_old or over:
                    doomed.append(str(task_id))
            for task_id in doomed:
                await conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            await conn.commit()
        return len(doomed)
