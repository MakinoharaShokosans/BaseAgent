"""记忆存储。出处：docs/03_context/04_memory.md §2-4、§6、§8。"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import stat
from pathlib import Path

import pytest

from agent_runtime.core import FailedAttempt
from agent_runtime.memory import (
    Fact,
    Failure,
    MemoryError_,
    MemoryStore,
    SessionMemory,
    SessionNotFound,
    WatermarkRegressionError,
    WorkspaceMemory,
    WorkspaceNotFound,
    WorkspacePathConflict,
)


@pytest.fixture
async def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = MemoryStore(tmp_path / "meta.db")
    await s.open()
    yield s
    await s.close()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "proj").mkdir()
    return tmp_path / "proj"


async def session(store: MemoryStore, root: Path, sid: str = "s1", wid: str = "w1") -> str:
    if not await store.workspace_exists(wid):
        await store.create_workspace(wid, "proj", str(root), "描述")
    await store.create_session(sid, wid)
    return sid


def fact(text: str = "f", task: str = "t1", **kw: object) -> Fact:
    return Fact(text=text, task_id=task, trust="trusted", source="task", created_at=1.0, **kw)  # type: ignore[arg-type]


# ==============================================================================
# 库本身
# ==============================================================================


@pytest.mark.unit
async def test_the_database_is_private_wal_and_reopening_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "d" / "meta.db"
    s = MemoryStore(path)
    await s.open()
    await s.open()
    await s.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = MemoryStore(path)
    await again.open()
    await again.close()
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.unit
async def test_a_database_from_a_newer_program_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "meta.db"
    s = MemoryStore(path)
    await s.open()
    await s.close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=999")
    conn.commit()
    conn.close()
    with pytest.raises(MemoryError_, match="999"):
        await MemoryStore(path).open()


@pytest.mark.unit
async def test_using_a_store_that_was_never_opened_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(MemoryError_):
        await MemoryStore(tmp_path / "x.db").workspace_exists("w")


# ==============================================================================
# 工作区与会话
# ==============================================================================


@pytest.mark.unit
async def test_a_workspace_round_trips(store: MemoryStore, root: Path) -> None:
    await store.create_workspace("w1", "proj", str(root), "描述")
    ws = await store.get_workspace("w1")
    assert (ws.name, ws.root_path, ws.description) == ("proj", str(root.resolve()), "描述")  # noqa: ASYNC240


@pytest.mark.unit
async def test_a_missing_workspace_is_an_error_never_a_made_up_default(store: MemoryStore) -> None:
    with pytest.raises(WorkspaceNotFound):
        await store.get_workspace("ghost")


@pytest.mark.unit
async def test_the_root_must_be_an_existing_directory(store: MemoryStore, tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")
    for bad in (tmp_path / "nope", tmp_path / "file"):
        with pytest.raises(MemoryError_, match="目录"):
            await store.create_workspace("w", "n", str(bad))


@pytest.mark.unit
async def test_one_path_cannot_back_two_workspaces(store: MemoryStore, root: Path) -> None:
    await store.create_workspace("w1", "a", str(root))
    with pytest.raises(WorkspacePathConflict):
        await store.create_workspace("w2", "b", str(root / ".." / "proj"))
    with pytest.raises(MemoryError_):
        await store.create_workspace("w1", "c", str(root.parent))  # 同 id


@pytest.mark.unit
async def test_a_session_needs_an_existing_workspace(store: MemoryStore) -> None:
    with pytest.raises(WorkspaceNotFound):
        await store.create_session("s1", "ghost")


@pytest.mark.unit
async def test_unknown_sessions_are_errors_on_every_read_and_write(store: MemoryStore) -> None:
    with pytest.raises(SessionNotFound):
        await store.load_memory("ghost")
    with pytest.raises(SessionNotFound):
        await store.append_turn("ghost", "t", "user", "x", 1)
    with pytest.raises(SessionNotFound):
        await store.turns_after("ghost", 0)


# ==============================================================================
# 流水
# ==============================================================================


@pytest.mark.unit
async def test_turns_are_numbered_in_order_and_read_after_the_watermark(
    store: MemoryStore, root: Path
) -> None:
    sid = await session(store, root)
    ids = [
        await store.append_turn(sid, "t1", r, f"c{i}", 5)
        for i, r in enumerate(["user", "assistant"] * 3)
    ]
    assert ids == sorted(ids) and len(set(ids)) == 6
    after = await store.turns_after(sid, ids[1])
    assert [t.content for t in after] == ["c2", "c3", "c4", "c5"]
    assert after[0].task_id == "t1" and after[0].token_count == 5 and after[0].role == "user"
    recent = await store.recent_turns(sid, 2)
    assert [t.content for t in recent] == ["c4", "c5"]
    assert await store.recent_turns(sid, 0) == []


@pytest.mark.unit
async def test_only_user_and_assistant_turns_are_recorded(store: MemoryStore, root: Path) -> None:
    sid = await session(store, root)
    for role in ("tool", "system", ""):
        with pytest.raises(MemoryError_):
            await store.append_turn(sid, "t", role, "x", 1)


@pytest.mark.unit
async def test_turns_of_different_sessions_do_not_mix(store: MemoryStore, root: Path) -> None:
    a = await session(store, root, "a")
    b = await session(store, root, "b")
    await store.append_turn(a, "t", "user", "A", 1)
    await store.append_turn(b, "t", "user", "B", 1)
    assert [t.content for t in await store.turns_after(a, 0)] == ["A"]


# ==============================================================================
# 会话记忆
# ==============================================================================


@pytest.mark.unit
async def test_a_fresh_session_has_the_empty_memory(store: MemoryStore, root: Path) -> None:
    sid = await session(store, root)
    assert await store.load_memory(sid) == SessionMemory()


@pytest.mark.unit
async def test_memory_round_trips_with_provenance(store: MemoryStore, root: Path) -> None:
    sid = await session(store, root)
    mem = SessionMemory(
        watermark=0,
        summary="摘要",
        facts=(fact("事实"),),
        failures=(
            Failure(
                attempt=FailedAttempt(action="a", failure_reason="r", conclusion="c"),
                task_id="t9",
                created_at=2.0,
            ),
        ),
        pending_facts=(fact("新事实", "t2"),),
        last_action_target={"files": ("a.py",), "symbols": ("f",)},
        compaction_failures=2,
        retry_after=99.5,
    )
    await store.update_memory(sid, lambda _: mem)
    assert await store.load_memory(sid) == mem


@pytest.mark.unit
async def test_the_watermark_never_moves_backwards(store: MemoryStore, root: Path) -> None:
    sid = await session(store, root)
    tid = await store.append_turn(sid, "t", "user", "x", 1)
    await store.update_memory(sid, lambda m: dataclasses.replace(m, watermark=tid))
    with pytest.raises(WatermarkRegressionError):
        await store.update_memory(sid, lambda m: dataclasses.replace(m, watermark=tid - 1))
    assert (await store.load_memory(sid)).watermark == tid


@pytest.mark.unit
async def test_a_watermark_beyond_the_existing_turns_is_refused(
    store: MemoryStore, root: Path
) -> None:
    sid = await session(store, root)
    with pytest.raises(MemoryError_):
        await store.update_memory(sid, lambda m: dataclasses.replace(m, watermark=50))


@pytest.mark.unit
async def test_concurrent_read_modify_write_loses_nothing(store: MemoryStore, root: Path) -> None:
    sid = await session(store, root)

    async def add(i: int) -> None:
        await store.update_memory(
            sid, lambda m: dataclasses.replace(m, pending_facts=(*m.pending_facts, fact(f"f{i}")))
        )

    await asyncio.gather(*(add(i) for i in range(25)))
    assert len((await store.load_memory(sid)).pending_facts) == 25


@pytest.mark.unit
async def test_a_failing_update_function_changes_nothing(store: MemoryStore, root: Path) -> None:
    sid = await session(store, root)

    def boom(m: SessionMemory) -> SessionMemory:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await store.update_memory(sid, boom)
    assert await store.load_memory(sid) == SessionMemory()


# ==============================================================================
# 工作区记忆（只有用户动作能写）与级联
# ==============================================================================


@pytest.mark.unit
async def test_workspace_memory_is_written_only_by_the_explicit_setter(
    store: MemoryStore, root: Path
) -> None:
    await session(store, root)
    assert await store.get_workspace_memory("w1") == WorkspaceMemory()
    mem = WorkspaceMemory(
        profile=("称呼我小王",), guidelines=("先写测试",), architecture=(), pitfalls=("别用 X",)
    )
    await store.set_workspace_memory("w1", mem)
    assert await store.get_workspace_memory("w1") == mem
    await store.set_workspace_memory("w1", WorkspaceMemory())
    assert await store.get_workspace_memory("w1") == WorkspaceMemory()
    with pytest.raises(WorkspaceNotFound):
        await store.set_workspace_memory("ghost", mem)


@pytest.mark.unit
async def test_deleting_a_session_removes_its_turns_and_memory(
    store: MemoryStore, root: Path
) -> None:
    sid = await session(store, root)
    await store.append_turn(sid, "t", "user", "x", 1)
    await store.update_memory(sid, lambda m: dataclasses.replace(m, summary="s"))
    await store.delete_session(sid)
    assert not await store.session_exists(sid)
    assert await store.count_rows() == {
        "sessions": 0,
        "turns": 0,
        "memories": 0,
        "workspaces": 1,
        "tasks": 0,
    }


@pytest.mark.unit
async def test_deleting_a_workspace_cascades_everything_beneath_it(
    store: MemoryStore, root: Path
) -> None:
    for sid in ("a", "b"):
        await session(store, root, sid)
        await store.append_turn(sid, "t", "assistant", "x", 1)
        await store.update_memory(sid, lambda m: dataclasses.replace(m, summary="s"))
    await store.set_workspace_memory("w1", WorkspaceMemory(profile=("p",)))
    await store.delete_workspace("w1")
    assert await store.count_rows() == {
        "sessions": 0,
        "turns": 0,
        "memories": 0,
        "workspaces": 0,
        "tasks": 0,
    }
    with pytest.raises(WorkspaceNotFound):
        await store.get_workspace_memory("w1")
