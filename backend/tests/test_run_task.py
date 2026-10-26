"""run_task：新任务入口。出处：docs/01_architecture/02_task_lifecycle.md §2.1-2.2。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.core import AgentState, FailedAttempt, Message, Milestone, PendingApproval
from agent_runtime.loop import Stage, TaskRuntime
from agent_runtime.runtime import RunnerDeps, TaskAlreadyExistsError, TaskRequest, run_task

DONE_MS = Milestone(id=0, title="m", status="completed")


class NullSink:
    async def stage_committed(
        self, phase: str, update: Mapping[str, object], state: AgentState
    ) -> None:
        pass

    async def approval_requested(self, request: PendingApproval, state: AgentState) -> None:
        pass


def make_deps(store: CheckpointStore, **behaviours: Any) -> tuple[RunnerDeps, list[AgentState]]:
    """所有阶段默认返回空增量；planner 默认直接完成里程碑。"""
    seen: list[AgentState] = []

    def stage(name: str) -> Stage:
        async def run(state: AgentState) -> Mapping[str, object]:
            seen.append(state)
            behaviour = behaviours.get(name)
            if behaviour is None:
                return {"milestones": [DONE_MS]} if name == "planner" else {}
            if callable(behaviour):
                return await behaviour(state)  # type: ignore[no-any-return]
            return behaviour  # type: ignore[no-any-return]

        return run

    async def build(state: AgentState) -> TaskRuntime:
        names = ("planner", "budget_guard", "executor", "tool_runner", "evaluator")
        return TaskRuntime(
            stages={n: stage(n) for n in names},  # type: ignore[misc]
            checkpoints=store,
            events=NullSink(),  # type: ignore[arg-type]
        )

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
    assert first.phase == "planner"
    assert first.step_count == first.total_tokens == 0
    assert first.milestones == ()


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
async def test_session_memory_is_inherited(store: CheckpointStore) -> None:
    attempt = FailedAttempt(action="a", failure_reason="r", conclusion="c")
    deps, seen = make_deps(store)
    await run_task(
        deps,
        request(
            rolling_summary="so far", confirmed_facts=("uses pytest",), failed_attempts=(attempt,)
        ),
        "task-1",
    )
    assert seen[0].rolling_summary == "so far"
    assert seen[0].confirmed_facts == ("uses pytest",)
    assert seen[0].failed_attempts == (attempt,)


@pytest.mark.unit
async def test_the_canary_is_derived_per_session_and_task(store: CheckpointStore) -> None:
    deps, seen = make_deps(store)
    await run_task(deps, request(), "task-1")
    assert seen[0].canary_token == "canary-s1-task-1"


@pytest.mark.unit
async def test_the_initial_checkpoint_exists_before_the_first_stage_runs(
    store: CheckpointStore,
) -> None:
    """崩在第一个阶段里的任务也能找回，并由启动扫描发现。"""
    snapshots: list[AgentState | None] = []

    async def peek(state: AgentState) -> Mapping[str, object]:
        snapshots.append(await store.load("task-1"))
        return {"milestones": [DONE_MS]}

    deps, _ = make_deps(store, planner=peek)
    await run_task(deps, request(), "task-1")
    assert snapshots[0] is not None
    assert snapshots[0].phase == "planner"
    assert snapshots[0].messages[-1].content == "fix the bug"


@pytest.mark.unit
async def test_a_successful_run_is_stored_as_done(store: CheckpointStore) -> None:
    deps, _ = make_deps(store)
    outcome = await run_task(deps, request(), "task-1")
    assert outcome.status == "succeeded"
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
async def test_a_failing_stage_leaves_the_initial_checkpoint_resumable(
    store: CheckpointStore,
) -> None:
    async def boom(state: AgentState) -> Mapping[str, object]:
        raise RuntimeError("boom")

    deps, _ = make_deps(store, planner=boom)
    with pytest.raises(RuntimeError, match="boom"):
        await run_task(deps, request(), "task-1")
    stored = await store.load("task-1")
    assert stored is not None and stored.phase == "planner"
