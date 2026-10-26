"""记忆存储：SQLite（WAL）。设计文档：docs/03_context/04_memory.md §2。

长生命周期的单连接，写操作用锁串行化；会话记忆只通过 ``update_memory`` 做
读-改-写（同一事务内），所以并发的沉淀与压缩不会互相覆盖。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import aiosqlite

from agent_runtime.memory.errors import (
    MemoryError_,
    SessionNotFound,
    WatermarkRegressionError,
    WorkspaceNotFound,
    WorkspacePathConflict,
)
from agent_runtime.memory.models import SessionMemory, Turn, Workspace, WorkspaceMemory

_DB_MODE: Final = 0o600
_ROLES: Final = ("user", "assistant")
_LAYERS: Final = ("profile", "guidelines", "architecture", "pitfalls")

_MIGRATIONS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE workspaces (
        workspace_id TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        root_path    TEXT NOT NULL UNIQUE,
        description  TEXT NOT NULL DEFAULT '',
        created_at   REAL NOT NULL
    );
    CREATE TABLE workspace_memories (
        workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
        data_json    TEXT NOT NULL
    );
    CREATE TABLE sessions (
        session_id   TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
        created_at   REAL NOT NULL
    );
    CREATE TABLE session_turns (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        task_id     TEXT NOT NULL,
        role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content     TEXT NOT NULL,
        token_count INTEGER NOT NULL,
        created_at  REAL NOT NULL
    );
    CREATE INDEX idx_session_turns ON session_turns(session_id, id);
    CREATE TABLE session_memories (
        session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
        watermark  INTEGER NOT NULL,
        summary    TEXT NOT NULL,
        data_json  TEXT NOT NULL
    );
    """,
)


class MemoryStore:
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
            os.close(os.open(self._path, os.O_CREAT | os.O_WRONLY, _DB_MODE))
            conn = await aiosqlite.connect(self._path)
        except (OSError, sqlite3.Error) as exc:
            raise MemoryError_(f"无法打开记忆库 {self._path}：{exc}") from exc
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
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

    @staticmethod
    async def _migrate(conn: aiosqlite.Connection) -> None:
        try:
            row = await (await conn.execute("PRAGMA user_version")).fetchone()
            version = int(row[0]) if row else 0
            if version > len(_MIGRATIONS):
                raise MemoryError_(
                    f"记忆库版本 {version} 高于当前程序支持的 {len(_MIGRATIONS)}，请升级程序"
                )
            for target in range(version + 1, len(_MIGRATIONS) + 1):
                await conn.executescript(
                    f"BEGIN;{_MIGRATIONS[target - 1]}PRAGMA user_version={target};COMMIT;"
                )
        except sqlite3.Error as exc:
            raise MemoryError_(f"记忆库初始化失败：{exc}") from exc

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise MemoryError_("记忆库尚未 open")
        return self._conn

    # ------------------------------------------------------------------ 工作区

    async def create_workspace(
        self, workspace_id: str, name: str, root_path: str, description: str = ""
    ) -> None:
        root = Path(root_path).resolve()  # noqa: ASYNC240
        if not root.is_dir():
            raise MemoryError_(f"工作区根路径必须是已存在的目录：{root_path}")
        conn = self._require()
        async with self._lock:
            row = await (
                await conn.execute(
                    "SELECT workspace_id FROM workspaces WHERE root_path = ?", (str(root),)
                )
            ).fetchone()
            if row is not None:
                raise WorkspacePathConflict(f"路径 {root} 已属于工作区 {row[0]}")
            try:
                await conn.execute(
                    "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?)",
                    (workspace_id, name, str(root), description, time.time()),
                )
                await conn.commit()
            except sqlite3.IntegrityError as exc:
                await conn.rollback()
                raise MemoryError_(f"工作区 {workspace_id} 已存在") from exc

    async def workspace_exists(self, workspace_id: str) -> bool:
        row = await (
            await self._require().execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            )
        ).fetchone()
        return row is not None

    async def get_workspace(self, workspace_id: str) -> Workspace:
        row = await (
            await self._require().execute(
                "SELECT workspace_id, name, root_path, description FROM workspaces "
                "WHERE workspace_id = ?",
                (workspace_id,),
            )
        ).fetchone()
        if row is None:
            raise WorkspaceNotFound(f"工作区 {workspace_id} 不存在")
        return Workspace(*row)

    async def delete_workspace(self, workspace_id: str) -> None:
        await self._write("DELETE FROM workspaces WHERE workspace_id = ?", (workspace_id,))

    async def get_workspace_memory(self, workspace_id: str) -> WorkspaceMemory:
        await self.get_workspace(workspace_id)
        row = await (
            await self._require().execute(
                "SELECT data_json FROM workspace_memories WHERE workspace_id = ?", (workspace_id,)
            )
        ).fetchone()
        if row is None:
            return WorkspaceMemory()
        data = json.loads(row[0])
        return WorkspaceMemory(**{k: tuple(data.get(k, ())) for k in _LAYERS})

    async def set_workspace_memory(self, workspace_id: str, memory: WorkspaceMemory) -> None:
        """工作区记忆只能由用户的明确动作写入：调用方是 HTTP 接口，压缩与沉淀从不调用它。"""
        await self.get_workspace(workspace_id)
        data = json.dumps({k: list(getattr(memory, k)) for k in _LAYERS}, ensure_ascii=False)
        await self._write(
            "INSERT INTO workspace_memories VALUES (?, ?) "
            "ON CONFLICT(workspace_id) DO UPDATE SET data_json = excluded.data_json",
            (workspace_id, data),
        )

    # ------------------------------------------------------------------ 会话与流水

    async def create_session(self, session_id: str, workspace_id: str) -> None:
        await self.get_workspace(workspace_id)
        await self._write(
            "INSERT INTO sessions VALUES (?, ?, ?)", (session_id, workspace_id, time.time())
        )

    async def session_exists(self, session_id: str) -> bool:
        row = await (
            await self._require().execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            )
        ).fetchone()
        return row is not None

    async def _need_session(self, session_id: str) -> None:
        if not await self.session_exists(session_id):
            raise SessionNotFound(f"会话 {session_id} 不存在")

    async def workspace_id_of(self, session_id: str) -> str:
        row = await (
            await self._require().execute(
                "SELECT workspace_id FROM sessions WHERE session_id = ?", (session_id,)
            )
        ).fetchone()
        if row is None:
            raise SessionNotFound(f"会话 {session_id} 不存在")
        return str(row[0])

    async def delete_session(self, session_id: str) -> None:
        await self._write("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    async def append_turn(
        self, session_id: str, task_id: str, role: str, content: str, token_count: int
    ) -> int:
        if role not in _ROLES:
            raise MemoryError_(f"流水只记录 user 与 assistant，不接受 {role!r}")
        await self._need_session(session_id)
        conn = self._require()
        async with self._lock:
            cursor = await conn.execute(
                "INSERT INTO session_turns "
                "(session_id, task_id, role, content, token_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, task_id, role, content, token_count, time.time()),
            )
            await conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def turns_after(self, session_id: str, watermark: int) -> list[Turn]:
        await self._need_session(session_id)
        return await self._turns(
            "SELECT id, session_id, task_id, role, content, token_count, created_at "
            "FROM session_turns WHERE session_id = ? AND id > ? ORDER BY id",
            (session_id, watermark),
        )

    async def recent_turns(self, session_id: str, count: int) -> list[Turn]:
        await self._need_session(session_id)
        if count <= 0:
            return []
        turns = await self._turns(
            "SELECT id, session_id, task_id, role, content, token_count, created_at "
            "FROM session_turns WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, count),
        )
        return turns[::-1]

    async def _turns(self, sql: str, params: tuple[object, ...]) -> list[Turn]:
        rows = await (await self._require().execute(sql, params)).fetchall()
        return [Turn(*row) for row in rows]

    # ------------------------------------------------------------------ 会话记忆

    async def load_memory(self, session_id: str) -> SessionMemory:
        await self._need_session(session_id)
        return await self._load(session_id)

    async def _load(self, session_id: str) -> SessionMemory:
        row = await (
            await self._require().execute(
                "SELECT watermark, summary, data_json FROM session_memories WHERE session_id = ?",
                (session_id,),
            )
        ).fetchone()
        if row is None:
            return SessionMemory()
        return SessionMemory.from_json_dict(row[0], row[1], json.loads(row[2]))

    async def update_memory(
        self, session_id: str, change: Callable[[SessionMemory], SessionMemory]
    ) -> SessionMemory:
        """同一事务内的读-改-写。``change`` 抛异常则什么都不改；水位线不得回退或越过现有轮次。"""
        await self._need_session(session_id)
        conn = self._require()
        async with self._lock:
            current = await self._load(session_id)
            new = change(current)
            if new.watermark < current.watermark:
                raise WatermarkRegressionError(
                    f"水位线不得回退：{current.watermark} -> {new.watermark}"
                )
            top = await (
                await conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM session_turns WHERE session_id = ?",
                    (session_id,),
                )
            ).fetchone()
            if top is not None and new.watermark > top[0]:
                raise MemoryError_(f"水位线 {new.watermark} 超过了现有轮次（最大 {top[0]}）")
            await conn.execute(
                "INSERT INTO session_memories VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET watermark = excluded.watermark, "
                "summary = excluded.summary, data_json = excluded.data_json",
                (
                    session_id,
                    new.watermark,
                    new.summary,
                    json.dumps(new.to_json_dict(), ensure_ascii=False),
                ),
            )
            await conn.commit()
        return new

    # ------------------------------------------------------------------ 辅助

    async def count_rows(self) -> dict[str, int]:
        conn = self._require()
        out: dict[str, int] = {}
        for key, table in (
            ("workspaces", "workspaces"),
            ("sessions", "sessions"),
            ("turns", "session_turns"),
            ("memories", "session_memories"),
        ):
            row = await (await conn.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            out[key] = int(row[0]) if row else 0
        return out

    async def _write(self, sql: str, params: tuple[object, ...]) -> None:
        conn = self._require()
        async with self._lock:
            try:
                await conn.execute(sql, params)
                await conn.commit()
            except sqlite3.Error as exc:
                await conn.rollback()
                raise MemoryError_(f"记忆库写入失败：{exc}") from exc
