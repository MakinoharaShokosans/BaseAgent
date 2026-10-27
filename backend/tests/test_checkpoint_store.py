"""检查点存储：提交、读取最新、启动扫描。出处：docs/02_runtime/03_checkpoint_and_resume.md。"""

from __future__ import annotations

import asyncio
import sqlite3
import stat
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_runtime.checkpoint import (
    CheckpointCorruptError,
    CheckpointError,
    CheckpointStore,
    scan_for_recovery,
)
from agent_runtime.core import (
    AgentState,
    AtomicPairError,
    Message,
    PendingApproval,
    ToolCall,
    apply_update,
)

MakeState = Callable[..., AgentState]

CALL = ToolCall(id="c1", name="view_file", args={"path": "a"})
ASSISTANT = Message(role="assistant", content="", tool_calls=(CALL,))
TOOL = Message(role="tool", content="ok", tool_call_id="c1", name="view_file")
PENDING = PendingApproval(
    approval_id="appr_1",
    call_ids=("c1",),
    required_level="full_permissions",
    current_level="workspace_write",
    action_type="network",
    command="curl x",
    reason="r",
    escalation_count=1,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "checkpoints" / "state.db"


@pytest.fixture
async def store(db_path: Path):  # type: ignore[no-untyped-def]
    async with CheckpointStore(db_path) as opened:
        yield opened


def _is_file(path: Path) -> bool:
    return path.is_file()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _run_python(script: str) -> None:
    subprocess.run([sys.executable, "-c", script], check=True, timeout=60)


def _raw(db_path: Path, sql: str, *args: object) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(sql, args).fetchall()
        connection.commit()
        return rows
    finally:
        connection.close()


# ==============================================================================
# 打开与文件
# ==============================================================================


@pytest.mark.unit
async def test_open_creates_directory_and_private_database(db_path: Path) -> None:
    async with CheckpointStore(db_path):
        assert _is_file(db_path)
    assert _mode(db_path) == 0o600


@pytest.mark.unit
async def test_database_uses_wal_with_busy_timeout(db_path: Path) -> None:
    async with CheckpointStore(db_path):
        assert _raw(db_path, "PRAGMA journal_mode") == [("wal",)]


@pytest.mark.unit
async def test_use_before_open_is_an_error(db_path: Path, make_state: MakeState) -> None:
    closed = CheckpointStore(db_path)
    with pytest.raises(CheckpointError, match="open"):
        await closed.commit(make_state())
    with pytest.raises(CheckpointError, match="open"):
        await closed.load("t1")


@pytest.mark.unit
async def test_open_twice_and_close_twice_are_harmless(db_path: Path) -> None:
    store = CheckpointStore(db_path)
    await store.open()
    await store.open()
    await store.close()
    await store.close()


@pytest.mark.unit
async def test_database_from_a_newer_version_is_refused(db_path: Path) -> None:
    async with CheckpointStore(db_path):
        pass
    _raw(db_path, "PRAGMA user_version = 99")
    with pytest.raises(CheckpointError, match="版本"):
        await CheckpointStore(db_path).open()


@pytest.mark.unit
async def test_reopen_keeps_existing_data(db_path: Path, make_state: MakeState) -> None:
    async with CheckpointStore(db_path) as first:
        await first.commit(make_state())
    async with CheckpointStore(db_path) as second:
        assert await second.load("t1") == make_state()


# ==============================================================================
# 提交与读取
# ==============================================================================


@pytest.mark.unit
async def test_load_unknown_task_returns_none(store: CheckpointStore) -> None:
    assert await store.load("nope") is None


@pytest.mark.unit
async def test_commit_then_load_round_trips(store: CheckpointStore, make_state: MakeState) -> None:
    state = apply_update(
        make_state(),
        {
            "messages": [Message(role="user", content="hi"), ASSISTANT, TOOL],
            "consecutive_errors": 2,
        },
    )
    await store.commit(state)
    assert await store.load("t1") == state


@pytest.mark.unit
async def test_only_the_latest_checkpoint_is_kept(
    store: CheckpointStore, make_state: MakeState, db_path: Path
) -> None:
    state = make_state()
    for step in range(1, 4):
        state = apply_update(state, {"consecutive_errors": step})
        await store.commit(state)
    assert (await store.load("t1")) == state
    assert _raw(db_path, "SELECT count(*) FROM task_checkpoints") == [(1,)]


@pytest.mark.unit
async def test_seq_increments_inside_sql(store: CheckpointStore, make_state: MakeState) -> None:
    state = make_state()
    assert [await store.commit(state) for _ in range(3)] == [1, 2, 3]
    other = make_state("t2")
    assert await store.commit(other) == 1  # 每个任务各自计数


@pytest.mark.unit
async def test_tasks_are_isolated(store: CheckpointStore, make_state: MakeState) -> None:
    a = apply_update(make_state("a"), {"consecutive_errors": 1})
    b = apply_update(make_state("b"), {"consecutive_errors": 9})
    await store.commit(a)
    await store.commit(b)
    assert (await store.load("a")) == a
    assert (await store.load("b")) == b


@pytest.mark.unit
async def test_status_column_follows_the_commit(
    store: CheckpointStore, make_state: MakeState, db_path: Path
) -> None:
    running = make_state()
    await store.commit(running)
    assert _raw(db_path, "SELECT status, outcome FROM task_checkpoints") == [("running", "")]
    waiting = apply_update(running, {"messages": [ASSISTANT], "pending_approval": PENDING})
    await store.commit(waiting)
    assert _raw(db_path, "SELECT status FROM task_checkpoints") == [("waiting",)]
    await store.commit(running, outcome="cancelled", reason="用户取消")
    stored = await store.load_status("t1")
    assert stored is not None
    assert (stored.status, stored.outcome, stored.reason) == ("finished", "cancelled", "用户取消")


@pytest.mark.unit
async def test_the_workflow_era_table_is_kept_aside(db_path: Path) -> None:
    """旧的 phase 表原样留存（只读），新表另建。"""
    async with CheckpointStore(db_path):
        pass
    tables = {r[0] for r in _raw(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"task_checkpoints", "task_checkpoints_legacy"} <= tables


@pytest.mark.unit
async def test_reasoning_state_survives_the_database(
    store: CheckpointStore, make_state: MakeState
) -> None:
    payload = {"z": 1, "a": {"终": "端"}, "sig": "AB+/=="}
    message = Message(role="assistant", content="x", reasoning_state=payload, produced_by="ep")
    await store.commit(apply_update(make_state(), {"messages": [message]}))
    loaded = await store.load("t1")
    assert loaded is not None
    restored = loaded.messages[0].reasoning_state
    assert restored == payload
    assert list(restored or {}) == ["z", "a", "sig"]


@pytest.mark.unit
async def test_delete_removes_the_task(store: CheckpointStore, make_state: MakeState) -> None:
    await store.commit(make_state())
    assert await store.delete("t1") is True
    assert await store.load("t1") is None
    assert await store.delete("t1") is False


# ==============================================================================
# 不变量：不合法的状态不落盘
# ==============================================================================


@pytest.mark.unit
async def test_an_unpaired_group_in_the_middle_is_not_persisted(
    store: CheckpointStore, make_state: MakeState
) -> None:
    good = apply_update(make_state(), {"consecutive_errors": 1})
    await store.commit(good)
    broken = apply_update(good, {"messages": [ASSISTANT, Message(role="user", content="x")]})
    with pytest.raises(AtomicPairError):
        await store.commit(broken)
    assert await store.load("t1") == good


@pytest.mark.unit
async def test_pending_approval_allows_an_unpaired_tail(
    store: CheckpointStore, make_state: MakeState
) -> None:
    state = apply_update(make_state(), {"messages": [ASSISTANT], "pending_approval": PENDING})
    await store.commit(state)
    assert await store.load("t1") == state


# ==============================================================================
# 损坏与篡改
# ==============================================================================


@pytest.mark.security
@pytest.mark.unit
async def test_garbage_state_json_is_reported_as_corrupt(
    store: CheckpointStore, make_state: MakeState, db_path: Path
) -> None:
    await store.commit(make_state())
    _raw(db_path, "UPDATE task_checkpoints SET state_json = ?", "{not json")
    with pytest.raises(CheckpointCorruptError, match="t1"):
        await store.load("t1")


@pytest.mark.security
@pytest.mark.unit
async def test_status_column_disagreeing_with_state_is_corrupt(
    store: CheckpointStore, make_state: MakeState, db_path: Path
) -> None:
    await store.commit(make_state())
    _raw(db_path, "UPDATE task_checkpoints SET status = 'waiting'")
    with pytest.raises(CheckpointCorruptError, match="status"):
        await store.load("t1")


@pytest.mark.security
@pytest.mark.unit
async def test_row_for_a_different_task_is_corrupt(
    store: CheckpointStore, make_state: MakeState, db_path: Path
) -> None:
    await store.commit(make_state("t1"))
    _raw(db_path, "UPDATE task_checkpoints SET task_id = 'other'")
    with pytest.raises(CheckpointCorruptError, match="task_id"):
        await store.load("other")


@pytest.mark.unit
async def test_corrupt_row_does_not_break_other_tasks(
    store: CheckpointStore, make_state: MakeState, db_path: Path
) -> None:
    await store.commit(make_state("bad"))
    await store.commit(make_state("good"))
    _raw(db_path, "UPDATE task_checkpoints SET state_json = 'x' WHERE task_id = 'bad'")
    assert await store.load("good") is not None


# ==============================================================================
# 并发
# ==============================================================================


@pytest.mark.unit
async def test_concurrent_commits_on_one_connection(
    store: CheckpointStore, make_state: MakeState
) -> None:
    states = [make_state(f"t{i}") for i in range(30)]
    await asyncio.gather(*(store.commit(s) for s in states))
    for s in states:
        assert await store.load(s.task_id) == s


@pytest.mark.unit
async def test_concurrent_commits_to_one_task_get_unique_increasing_seq(
    store: CheckpointStore, make_state: MakeState
) -> None:
    seqs = await asyncio.gather(*(store.commit(make_state()) for _ in range(20)))
    assert sorted(seqs) == list(range(1, 21))


@pytest.mark.integration
async def test_two_connections_do_not_clash(db_path: Path, make_state: MakeState) -> None:
    async with CheckpointStore(db_path) as a, CheckpointStore(db_path) as b:
        seqs = await asyncio.gather(*(s.commit(make_state()) for _ in range(15) for s in (a, b)))
    assert sorted(seqs) == list(range(1, 31))


# ==============================================================================
# 崩溃
# ==============================================================================


@pytest.mark.integration
async def test_committed_state_survives_a_hard_process_kill(
    db_path: Path, make_state: MakeState
) -> None:
    """提交返回之后进程被强杀（不执行任何清理），数据仍在。"""
    script = textwrap.dedent(
        f"""
        import asyncio, os
        from pathlib import Path
        from agent_runtime.checkpoint import CheckpointStore
        from agent_runtime.core import new_state

        async def main():
            store = CheckpointStore(Path({str(db_path)!r}))
            await store.open()
            state = new_state(workspace_id="w1", workspace_path="/work/proj", session_id="s1",
                              task_id="t1", task_goal="fix the bug",
                              permission_level="workspace_write", canary_token="canary-abc")
            await store.commit(state)
            os._exit(0)   # 不关闭连接，不做任何清理

        asyncio.run(main())
        """
    )
    _run_python(script)
    async with CheckpointStore(db_path) as reopened:
        assert await reopened.load("t1") == make_state()


# ==============================================================================
# 启动扫描
# ==============================================================================


@pytest.mark.unit
async def test_list_unfinished_skips_finished_tasks(
    store: CheckpointStore, make_state: MakeState
) -> None:
    await store.commit(make_state("running"))
    await store.commit(make_state("finished"), outcome="succeeded")
    unfinished = await store.list_unfinished()
    assert [(u.task_id, u.status) for u in unfinished] == [("running", "running")]


@pytest.mark.unit
async def test_list_unfinished_flags_tasks_waiting_for_approval(
    store: CheckpointStore, make_state: MakeState
) -> None:
    waiting = apply_update(
        make_state("waiting"), {"messages": [ASSISTANT], "pending_approval": PENDING}
    )
    await store.commit(waiting)
    await store.commit(make_state("running"))
    flags = {u.task_id: u.waiting_for_approval for u in await store.list_unfinished()}
    assert flags == {"waiting": True, "running": False}


@pytest.mark.unit
async def test_waiting_flag_clears_when_the_approval_is_consumed(
    store: CheckpointStore, make_state: MakeState
) -> None:
    waiting = apply_update(make_state(), {"messages": [ASSISTANT], "pending_approval": PENDING})
    await store.commit(waiting)
    await store.commit(apply_update(waiting, {"pending_approval": None, "messages": [TOOL]}))
    assert [u.waiting_for_approval for u in await store.list_unfinished()] == [False]


@pytest.mark.unit
async def test_scan_for_recovery_classifies_tasks(
    store: CheckpointStore, make_state: MakeState
) -> None:
    waiting = apply_update(make_state("w"), {"messages": [ASSISTANT], "pending_approval": PENDING})
    await store.commit(waiting)
    await store.commit(make_state("r1"))
    await store.commit(make_state("r2"))
    await store.commit(make_state("d"), outcome="succeeded")

    report = await scan_for_recovery(store)
    assert report.waiting_for_approval == ("w",)
    assert sorted(report.interrupted) == ["r1", "r2"]


@pytest.mark.unit
async def test_scan_on_empty_database(store: CheckpointStore) -> None:
    report = await scan_for_recovery(store)
    assert report.interrupted == () and report.waiting_for_approval == ()


@pytest.mark.unit
async def test_scan_does_not_decode_state(
    store: CheckpointStore, make_state: MakeState, db_path: Path
) -> None:
    """扫描只看列，不解码状态：损坏的任务也能被列出来，让用户知道有它。"""
    await store.commit(make_state())
    _raw(db_path, "UPDATE task_checkpoints SET state_json = 'garbage'")
    report = await scan_for_recovery(store)
    assert report.interrupted == ("t1",)
