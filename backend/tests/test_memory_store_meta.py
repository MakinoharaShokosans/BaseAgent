"""记忆库的列表、分页与任务元数据。出处：docs/09_api/01_endpoints.md §1-3。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.memory import (
    MemoryError_,
    MemoryStore,
    SessionNotFound,
    TaskRecord,
    WorkspaceNotFound,
)


@pytest.fixture
async def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = MemoryStore(tmp_path / "meta.db")
    await s.open()
    yield s
    await s.close()


async def make(store: MemoryStore, tmp_path: Path, name: str = "p1") -> str:
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    await store.create_workspace(f"w_{name}", name, str(root))
    return f"w_{name}"


def task(tid: str, sid: str = "s1", wid: str = "w_p1", at: float = 1.0, **kw: object) -> TaskRecord:
    return TaskRecord(tid, sid, wid, "目标", "running", "workspace_write", at, **kw)  # type: ignore[arg-type]


@pytest.mark.unit
async def test_workspaces_list_update_and_lookup_by_path(
    store: MemoryStore, tmp_path: Path
) -> None:
    a = await make(store, tmp_path, "p1")
    await make(store, tmp_path, "p2")
    assert [w.name for w in await store.list_workspaces()] == ["p1", "p2"]
    assert (await store.get_workspace(a)).created_at > 0
    found = await store.workspace_by_path(str(tmp_path / "p1"))
    assert found is not None and found.workspace_id == a
    assert await store.workspace_by_path(str(tmp_path / "nope")) is None
    changed = await store.update_workspace(a, name="新名", description="d")
    assert (changed.name, changed.description) == ("新名", "d")
    kept = await store.update_workspace(a, description="只改描述")
    assert kept.name == "新名" and kept.description == "只改描述"
    with pytest.raises(WorkspaceNotFound):
        await store.update_workspace("w_x", name="x")


@pytest.mark.unit
async def test_sessions_list_newest_first_and_get(store: MemoryStore, tmp_path: Path) -> None:
    wid = await make(store, tmp_path)
    await store.create_session("s1", wid)
    await store.create_session("s2", wid)
    assert {s.session_id for s in await store.list_sessions(wid)} == {"s1", "s2"}
    assert (await store.get_session("s1")).workspace_id == wid
    with pytest.raises(SessionNotFound):
        await store.get_session("nope")
    with pytest.raises(WorkspaceNotFound):
        await store.list_sessions("nope")


@pytest.mark.unit
async def test_turns_page_walks_backwards_without_overlap(
    store: MemoryStore, tmp_path: Path
) -> None:
    wid = await make(store, tmp_path)
    await store.create_session("s1", wid)
    ids = [
        await store.append_turn("s1", "t", "user" if i % 2 == 0 else "assistant", f"m{i}", 1)
        for i in range(7)
    ]
    newest = await store.turns_page("s1", 3)
    assert [t.id for t in newest] == ids[4:]
    older = await store.turns_page("s1", 3, before_id=newest[0].id)
    assert [t.id for t in older] == ids[1:4]
    oldest = await store.turns_page("s1", 3, before_id=older[0].id)
    assert [t.id for t in oldest] == ids[:1]
    assert await store.turns_page("s1", 3, before_id=oldest[0].id) == []
    with pytest.raises(SessionNotFound):
        await store.turns_page("nope", 3)


@pytest.mark.unit
async def test_task_record_roundtrip_and_update(store: MemoryStore, tmp_path: Path) -> None:
    wid = await make(store, tmp_path)
    await store.create_session("s1", wid)
    await store.record_task(task("t1", parent_task_id="t0", started_at=2.0))
    got = await store.get_task("t1")
    assert got == task("t1", parent_task_id="t0", started_at=2.0)
    await store.update_task(
        "t1", status="succeeded", finished_at=9.0, step_count=4, total_tokens=70
    )
    after = await store.get_task("t1")
    assert after is not None
    assert (after.status, after.finished_at, after.step_count, after.total_tokens) == (
        "succeeded",
        9.0,
        4,
        70,
    )
    assert await store.get_task("none") is None


@pytest.mark.unit
async def test_task_update_rejects_fields_that_must_not_change(
    store: MemoryStore, tmp_path: Path
) -> None:
    wid = await make(store, tmp_path)
    await store.create_session("s1", wid)
    await store.record_task(task("t1"))
    for bad in ("task_goal", "session_id", "permission_level", "created_at", "task_id"):
        with pytest.raises(MemoryError_):
            await store.update_task("t1", **{bad: "x"})


@pytest.mark.unit
async def test_task_needs_an_existing_session_and_a_unique_id(
    store: MemoryStore, tmp_path: Path
) -> None:
    wid = await make(store, tmp_path)
    with pytest.raises(SessionNotFound):
        await store.record_task(task("t1"))
    await store.create_session("s1", wid)
    await store.record_task(task("t1"))
    with pytest.raises(MemoryError_):
        await store.record_task(task("t1"))


@pytest.mark.unit
async def test_tasks_list_newest_first_with_cursor(store: MemoryStore, tmp_path: Path) -> None:
    wid = await make(store, tmp_path)
    await store.create_session("s1", wid)
    await store.create_session("s2", wid)
    for i in range(5):
        await store.record_task(task(f"t{i}", at=float(i + 1)))
    await store.record_task(task("other", sid="s2", at=99.0))
    page = await store.list_tasks("s1", limit=2)
    assert [t.task_id for t in page] == ["t4", "t3"]
    nxt = await store.list_tasks("s1", limit=2, before=page[-1].created_at)
    assert [t.task_id for t in nxt] == ["t2", "t1"]
    assert [t.task_id for t in await store.list_tasks("s2")] == ["other"]
    with pytest.raises(SessionNotFound):
        await store.list_tasks("none")


@pytest.mark.unit
async def test_deleting_a_session_or_workspace_cascades_to_tasks(
    store: MemoryStore, tmp_path: Path
) -> None:
    wid = await make(store, tmp_path)
    await store.create_session("s1", wid)
    await store.create_session("s2", wid)
    await store.record_task(task("a", sid="s1"))
    await store.record_task(task("b", sid="s2"))
    assert await store.task_ids_of_session("s1") == ["a"]
    assert sorted(await store.task_ids_of_workspace(wid)) == ["a", "b"]
    await store.delete_session("s1")
    assert await store.get_task("a") is None and await store.get_task("b") is not None
    await store.delete_workspace(wid)
    assert await store.get_task("b") is None
    assert (await store.count_rows())["tasks"] == 0


@pytest.mark.unit
async def test_delete_task_removes_only_that_task(store: MemoryStore, tmp_path: Path) -> None:
    wid = await make(store, tmp_path)
    await store.create_session("s1", wid)
    await store.record_task(task("a"))
    await store.record_task(task("b"))
    await store.delete_task("a")
    assert await store.get_task("a") is None and await store.get_task("b") is not None
