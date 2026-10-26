"""检查点存储：每个任务只保留最新一份，单事务 UPSERT，WAL 模式。

设计文档：docs/02_runtime/03_checkpoint_and_resume.md §2-3。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import aiosqlite

from agent_runtime.checkpoint.errors import CheckpointCorruptError, CheckpointError
from agent_runtime.core import (
    DONE,
    AgentState,
    StateDecodeError,
    check_invariants,
    dumps_state,
    loads_state,
)

_BUSY_TIMEOUT_MS: Final = 5000
_DB_MODE: Final = 0o600

#: 按版本顺序执行的迁移脚本；版本号记在 ``PRAGMA user_version``。只追加，不改已发布的版本。
_MIGRATIONS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE task_checkpoints (
        task_id        TEXT PRIMARY KEY,
        seq            INTEGER NOT NULL,
        phase          TEXT NOT NULL,
        waiting        INTEGER NOT NULL DEFAULT 0,
        state_json     TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        updated_at     REAL NOT NULL
    );
    CREATE INDEX idx_task_checkpoints_phase ON task_checkpoints(phase);
    """,
)

_UPSERT: Final = """
INSERT INTO task_checkpoints (task_id, seq, phase, waiting, state_json, schema_version, updated_at)
VALUES (?, 1, ?, ?, ?, ?, ?)
ON CONFLICT(task_id) DO UPDATE SET
    seq = seq + 1,
    phase = excluded.phase,
    waiting = excluded.waiting,
    state_json = excluded.state_json,
    schema_version = excluded.schema_version,
    updated_at = excluded.updated_at
RETURNING seq
"""


@dataclass(frozen=True, slots=True)
class UnfinishedTask:
    """启动扫描的一条记录。只来自列，不解码状态。"""

    task_id: str
    phase: str
    waiting_for_approval: bool


class CheckpointStore:
    """长生命周期的单连接；写操作用锁串行化。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 生命周期

    async def open(self) -> None:
        if self._conn is not None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # 先以 0600 建好空文件，WAL 的附属文件会沿用它的权限
            os.close(os.open(self._path, os.O_CREAT | os.O_WRONLY, _DB_MODE))
            conn = await aiosqlite.connect(self._path)
        except (OSError, sqlite3.Error) as exc:
            raise CheckpointError(f"无法打开检查点库 {self._path}：{exc}") from exc
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            # 断电后已提交的检查点不能丢，所以用 FULL
            await conn.execute("PRAGMA synchronous=FULL")
            await self._migrate(conn)
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

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        try:
            cursor = await conn.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            version = int(row[0]) if row else 0
            if version > len(_MIGRATIONS):
                raise CheckpointError(
                    f"检查点库版本 {version} 高于当前程序支持的 {len(_MIGRATIONS)}，请升级程序"
                )
            for target in range(version + 1, len(_MIGRATIONS) + 1):
                await conn.executescript(
                    f"BEGIN;{_MIGRATIONS[target - 1]}PRAGMA user_version={target};COMMIT;"
                )
        except sqlite3.Error as exc:
            raise CheckpointError(f"检查点库初始化失败：{exc}") from exc

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise CheckpointError("检查点库尚未 open")
        return self._conn

    # ------------------------------------------------------------------ 读写

    async def commit(self, state: AgentState) -> int:
        """提交检查点，返回新的提交序号（每个任务从 1 起，在 SQL 内自增）。

        先断言不变量：不合法的状态不落盘。失败即抛 :class:`CheckpointError`。
        """
        conn = self._require()
        check_invariants(state)
        params = (
            state.task_id,
            state.phase,
            int(state.pending_approval is not None),
            dumps_state(state),
            state.schema_version,
            time.time(),
        )
        async with self._lock:
            try:
                cursor = await conn.execute(_UPSERT, params)
                row = await cursor.fetchone()
                await conn.commit()
            except sqlite3.Error as exc:
                await conn.rollback()
                raise CheckpointError(f"提交检查点失败（任务 {state.task_id}）：{exc}") from exc
        assert row is not None
        return int(row[0])

    async def load(self, task_id: str) -> AgentState | None:
        conn = self._require()
        try:
            cursor = await conn.execute(
                "SELECT task_id, phase, schema_version, state_json "
                "FROM task_checkpoints WHERE task_id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()
        except sqlite3.Error as exc:
            raise CheckpointError(f"读取检查点失败（任务 {task_id}）：{exc}") from exc
        if row is None:
            return None

        row_task, row_phase, row_version, state_json = row
        try:
            state = loads_state(state_json)
        except StateDecodeError as exc:
            raise CheckpointCorruptError(f"任务 {task_id} 的检查点无法解码：{exc}") from exc
        for column, in_column, in_state in (
            ("task_id", row_task, state.task_id),
            ("phase", row_phase, state.phase),
            ("schema_version", row_version, state.schema_version),
        ):
            if in_column != in_state:
                raise CheckpointCorruptError(
                    f"任务 {task_id} 的检查点前后不一致：{column} 列为 {in_column!r}，"
                    f"状态里为 {in_state!r}"
                )
        return state

    async def delete(self, task_id: str) -> bool:
        conn = self._require()
        async with self._lock:
            try:
                cursor = await conn.execute(
                    "DELETE FROM task_checkpoints WHERE task_id = ?", (task_id,)
                )
                await conn.commit()
            except sqlite3.Error as exc:
                await conn.rollback()
                raise CheckpointError(f"删除检查点失败（任务 {task_id}）：{exc}") from exc
        return cursor.rowcount > 0

    async def list_unfinished(self) -> list[UnfinishedTask]:
        """列出 phase 不是终止的任务。只读列，不解码状态，所以损坏的任务也能被列出。"""
        conn = self._require()
        try:
            cursor = await conn.execute(
                "SELECT task_id, phase, waiting FROM task_checkpoints "
                "WHERE phase != ? ORDER BY updated_at, task_id",
                (DONE,),
            )
            rows = await cursor.fetchall()
        except sqlite3.Error as exc:
            raise CheckpointError(f"扫描检查点失败：{exc}") from exc
        return [UnfinishedTask(str(r[0]), str(r[1]), bool(r[2])) for r in rows]
