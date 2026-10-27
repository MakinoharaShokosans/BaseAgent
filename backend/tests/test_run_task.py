"""run_task：新任务入口。出处：docs/01_architecture/02_task_lifecycle.md §2.1-2.2。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.core import AgentState, Message
from agent_runtime.loop import TaskRuntime
from agent_runtime.runtime import RunnerDeps, TaskAlreadyExistsError, TaskRequest, run_task
from loop_support import FINAL, NullSink


def make_deps(store: CheckpointStore, model: Any = None) -> tuple[RunnerDeps, list[AgentState]]:
    """模型默认直接给出终回复；``model`` 可换成自己的步骤。"""
    seen: list[AgentState] = []

    async def default_model(state: AgentState) -> Message:
        seen.append(state)
        return FINAL

    async def tools(state: AgentState, calls: Any) -> Mapping[str, object]:
        return {}

    async def build(state: AgentState) -> TaskRuntime:
        step = model or default_model

        async def recording(current: AgentState) -> Message:
            if model is not None:
                seen.append(current)
            return await step(current)

        return TaskRuntime(model=recording, tools=tools, checkpoints=store, events=NullSink())  # type: ignore[arg-type]

    deps = RunnerDeps(
        checkpoints=store,
        build_runtime=build,
        is_idempotent=lambda name: False,
        make_canary=lambda session_id, task_id: f"canary-{session_id}-{task_id}",
    )
    return deps, seen


def request(**kw: Any) -> TaskRequest:
    base: dict[str, Any] = {
        "workspace_id": "w1",
        "workspace_path": "/work/proj",
        "session_id": "s1",
        "goal": "fix the bug",
        "permission_level": "workspace_write",
    }
    return TaskRequest(**{**base, **kw})


@pytest.fixture
async def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    async with CheckpointStore(tmp_path / "state.db") as opened:
        yield opened


@pytest.mark.unit
async def test_initial_state_is_built_from_the_request(store: CheckpointStore) -> None:
    deps, seen = make_deps(store)
    await run_task(deps, request(), "task-1")

    first = seen[0]
    assert first.task_id == "task-1"
    assert (first.workspace_id, first.workspace_path, first.session_id) == (
        "w1",
        "/work/proj",
        "s1",
    )
    assert first.task_goal == "fix the bug"
    assert first.permission_level == "workspace_write"
    assert first.plan == () and first.facts == () and first.failed_attempts == ()


@pytest.mark.unit
async def test_the_goal_is_the_latest_user_message_after_the_history(
    store: CheckpointStore,
) -> None:
    history = [
        Message(role="user", content="earlier ask"),
        Message(role="assistant", content="earlier reply"),
    ]
    deps, seen = make_deps(store)
    await run_task(deps, request(history=history), "task-1")
    assert list(seen[0].messages) == [*history, Message(role="user", content="fix the bug")]


@pytest.mark.unit
def test_inherited_memory_is_not_part_of_the_request() -> None:
    """继承来的记忆只进冻结的提示词资产，不进任务状态（决策 0006）。"""
    fields = set(TaskRequest.__dataclass_fields__)
    assert not fields & {"rolling_summary", "confirmed_facts", "failed_attempts"}


@pytest.mark.unit
async def test_the_canary_is_derived_per_session_and_task(store: CheckpointStore) -> None:
    deps, seen = make_deps(store)
    await run_task(deps, request(), "task-1")
    assert seen[0].canary_token == "canary-s1-task-1"


@pytest.mark.unit
async def test_the_initial_checkpoint_exists_before_the_first_model_call(
    store: CheckpointStore,
) -> None:
    """崩在第一次模型调用里的任务也能找回，并由启动扫描发现。"""
    snapshots: list[AgentState | None] = []

    async def peek(state: AgentState) -> Message:
        snapshots.append(await store.load("task-1"))
        return FINAL

    deps, _ = make_deps(store, peek)
    await run_task(deps, request(), "task-1")
    assert snapshots[0] is not None
    assert snapshots[0].messages[-1].content == "fix the bug"


@pytest.mark.unit
async def test_a_successful_run_ends_with_the_final_reply(store: CheckpointStore) -> None:
    deps, _ = make_deps(store)
    outcome = await run_task(deps, request(), "task-1")
    assert outcome.status == "succeeded"
    assert outcome.state.messages[-1] == FINAL
    assert await store.load("task-1") == outcome.state


@pytest.mark.unit
async def test_an_existing_task_id_is_refused_and_left_untouched(store: CheckpointStore) -> None:
    deps, _ = make_deps(store)
    first = await run_task(deps, request(goal="original"), "task-1")
    with pytest.raises(TaskAlreadyExistsError, match="task-1"):
        await run_task(deps, request(goal="overwrite!"), "task-1")
    assert await store.load("task-1") == first.state


@pytest.mark.unit
async def test_empty_goal_and_bad_level_are_rejected_before_anything_is_written(
    store: CheckpointStore,
) -> None:
    deps, seen = make_deps(store)
    with pytest.raises(ValueError, match="goal"):
        await run_task(deps, request(goal="   "), "task-1")
    with pytest.raises(ValueError, match="permission_level"):
        await run_task(deps, request(permission_level="root"), "task-2")
    assert seen == []
    assert await store.load("task-1") is None
    assert await store.load("task-2") is None


@pytest.mark.unit
async def test_a_failing_model_call_leaves_the_initial_checkpoint_resumable(
    store: CheckpointStore,
) -> None:
    async def boom(state: AgentState) -> Message:
        raise RuntimeError("boom")

    deps, _ = make_deps(store, boom)
    with pytest.raises(RuntimeError, match="boom"):
        await run_task(deps, request(), "task-1")
    stored = await store.load("task-1")
    assert stored is not None and stored.messages[-1].content == "fix the bug"
    meta = await store.load_status("task-1")
    assert meta is not None and meta.status == "running"
