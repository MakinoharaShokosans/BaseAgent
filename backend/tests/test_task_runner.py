"""任务运行器：后台运行、状态映射、并发名额、取消、启动接管。

出处：docs/01_architecture/02_task_lifecycle.md §2.1、§2.5、§4；docs/02_runtime/02_main_loop.md §4。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.approval import compute_approval_id
from agent_runtime.checkpoint import CheckpointStore, scan_for_recovery
from agent_runtime.core import AgentState, check_invariants
from agent_runtime.runtime import (
    ApprovalRequiredFirstError,
    RunnerDeps,
    TaskNotFoundError,
    TaskQueueFullError,
    TaskRequest,
    TaskRunner,
    TaskSnapshot,
)
from loop_support import ScriptedWorld


class Observer:
    def __init__(self, explode: bool = False) -> None:
        self.started: list[str] = []
        self.finished: list[TaskSnapshot] = []
        self._explode = explode

    async def task_started(self, task_id: str, state: AgentState) -> None:
        self.started.append(task_id)

    async def task_finished(self, snapshot: TaskSnapshot) -> None:
        self.finished.append(snapshot)
        if self._explode:
            raise RuntimeError("teardown failed")


class World(ScriptedWorld):
    def deps(self) -> RunnerDeps:
        return RunnerDeps(
            checkpoints=self.store,
            build_runtime=self.build,
            is_idempotent=lambda name: False,
            make_canary=lambda session, task: f"canary-{session}-{task}",
        )

    def runner(self, max_concurrent: int = 1, observer: Observer | None = None) -> TaskRunner:
        return TaskRunner(
            self.deps(),
            max_concurrent=max_concurrent,
            observer=observer,
            new_task_id=lambda: next(self.ids),
        )


def request(**kw: Any) -> TaskRequest:
    base: dict[str, Any] = {
        "workspace_id": "w1",
        "workspace_path": "/work/proj",
        "session_id": "s1",
        "goal": "fix the bug",
        "permission_level": "workspace_write",
    }
    return TaskRequest(**{**base, **kw})


def approve(task_id: str, position: int = 2) -> dict[str, Any]:
    return {"approval_id": compute_approval_id(task_id, position, ("c1",)), "approved": True}


@pytest.fixture
async def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    async with CheckpointStore(tmp_path / "state.db") as store:
        yield World(store)


# ==============================================================================
# 提交与状态映射
# ==============================================================================


@pytest.mark.unit
async def test_submit_returns_the_task_id_immediately_and_runs_in_the_background(
    world: World,
) -> None:
    world.gate = asyncio.Event()
    runner = world.runner()
    task_id = await runner.submit(request())
    assert task_id == "task-1"
    assert runner.snapshot(task_id).status == "running"

    world.gate.set()
    snapshot = await runner.wait(task_id)
    assert snapshot.status == "succeeded"


@pytest.mark.unit
async def test_succeeded(world: World) -> None:
    runner = world.runner()
    snapshot = await runner.wait(await runner.submit(request()))
    assert snapshot.status == "succeeded"
    assert snapshot.reason == ""
    assert snapshot.state is not None and snapshot.state.messages[-1].content == "done"
    meta = await world.store.load_status(snapshot.task_id)
    assert meta is not None and (meta.status, meta.outcome) == ("finished", "succeeded")
    assert await world.store.load(snapshot.task_id) == snapshot.state


@pytest.mark.unit
async def test_terminated_carries_the_reason(world: World) -> None:
    world.leak = True
    runner = world.runner()
    snapshot = await runner.wait(await runner.submit(request()))
    assert snapshot.status == "terminated"
    assert "canary" in snapshot.reason
    meta = await world.store.load_status(snapshot.task_id)
    assert meta is not None and meta.outcome == "terminated"


@pytest.mark.unit
async def test_an_exception_becomes_failed_and_stays_resumable(world: World) -> None:
    world.fail_model = RuntimeError("LLM 不可用")
    runner = world.runner()
    task_id = await runner.submit(request())
    snapshot = await runner.wait(task_id)

    assert snapshot.status == "failed"
    assert "LLM 不可用" in snapshot.reason
    stored = await world.store.load(task_id)
    assert stored is not None and stored.messages[-1].role == "user"  # 停在上一次提交点
    meta = await world.store.load_status(task_id)
    assert meta is not None and meta.status == "running"

    world.fail_model = None
    await runner.resume(task_id)
    assert (await runner.wait(task_id)).status == "succeeded"


@pytest.mark.unit
async def test_a_waiting_task_is_not_terminal(world: World) -> None:
    world.needs_approval = True
    observer = Observer()
    runner = world.runner(observer=observer)
    task_id = await runner.submit(request())
    snapshot = await runner.wait(task_id)

    assert snapshot.status == "waiting_for_approval"
    assert snapshot.state is not None and snapshot.state.pending_approval is not None
    assert observer.finished == []  # 挂起不是结束，不发 finished


@pytest.mark.unit
async def test_unknown_task(world: World) -> None:
    runner = world.runner()
    with pytest.raises(TaskNotFoundError):
        runner.snapshot("nope")
    with pytest.raises(TaskNotFoundError):
        await runner.cancel("nope")


@pytest.mark.unit
async def test_observer_sees_start_and_one_finish(world: World) -> None:
    observer = Observer()
    runner = world.runner(observer=observer)
    task_id = await runner.submit(request())
    await runner.wait(task_id)
    assert observer.started == [task_id]
    assert [s.status for s in observer.finished] == ["succeeded"]


@pytest.mark.unit
async def test_a_failing_teardown_does_not_change_the_outcome(world: World) -> None:
    """收尾中任何一步失败都不使已完成的任务变成失败。"""
    observer = Observer(explode=True)
    runner = world.runner(observer=observer)
    snapshot = await runner.wait(await runner.submit(request()))
    assert snapshot.status == "succeeded"
    assert len(observer.finished) == 1


# ==============================================================================
# 并发名额
# ==============================================================================


@pytest.mark.unit
async def test_a_full_runner_refuses_new_tasks_without_registering_them(world: World) -> None:
    world.gate = asyncio.Event()
    runner = world.runner(max_concurrent=1)
    first = await runner.submit(request())
    with pytest.raises(TaskQueueFullError):
        await runner.submit(request())
    assert [s.task_id for s in runner.list_tasks()] == [first]
    assert await world.store.load("task-2") is None  # 没有留下半个任务

    world.gate.set()
    await runner.wait(first)
    second = await runner.submit(request())  # 名额回来了
    assert (await runner.wait(second)).status == "succeeded"


@pytest.mark.unit
async def test_slots_are_counted_not_just_one(world: World) -> None:
    world.gate = asyncio.Event()
    runner = world.runner(max_concurrent=2)
    await runner.submit(request())
    await runner.submit(request())
    with pytest.raises(TaskQueueFullError):
        await runner.submit(request())
    world.gate.set()
    await runner.wait_all()


@pytest.mark.unit
async def test_a_waiting_task_gives_its_slot_back(world: World) -> None:
    world.needs_approval = True
    runner = world.runner(max_concurrent=1)
    first = await runner.submit(request())
    assert (await runner.wait(first)).status == "waiting_for_approval"
    second = await runner.submit(request())  # 不会被挂起的任务占着名额挡住
    assert (await runner.wait(second)).status == "waiting_for_approval"


@pytest.mark.unit
async def test_resuming_needs_a_free_slot_and_leaves_the_task_waiting_otherwise(
    world: World,
) -> None:
    world.needs_approval = True
    runner = world.runner(max_concurrent=1)
    waiting = await runner.submit(request())
    await runner.wait(waiting)

    world.needs_approval = False
    world.gate = asyncio.Event()
    busy = await runner.submit(request())
    await world.entered.wait()

    with pytest.raises(TaskQueueFullError):
        await runner.resume(waiting, approve(waiting))
    assert runner.snapshot(waiting).status == "waiting_for_approval"

    world.gate.set()
    await runner.wait(busy)
    await runner.resume(waiting, approve(waiting))
    assert (await runner.wait(waiting)).status == "succeeded"


# ==============================================================================
# 续跑
# ==============================================================================


@pytest.mark.unit
async def test_approval_flow_through_the_runner(world: World) -> None:
    world.needs_approval = True
    runner = world.runner()
    task_id = await runner.submit(request())
    await runner.wait(task_id)

    await runner.resume(task_id, approve(task_id))
    assert runner.snapshot(task_id).status in ("running", "succeeded")
    assert (await runner.wait(task_id)).status == "succeeded"


@pytest.mark.unit
async def test_resume_validation_errors_are_raised_to_the_caller_and_change_nothing(
    world: World,
) -> None:
    world.needs_approval = True
    runner = world.runner()
    task_id = await runner.submit(request())
    await runner.wait(task_id)

    with pytest.raises(ApprovalRequiredFirstError):
        await runner.resume(task_id)
    with pytest.raises(ValueError, match="approved"):
        await runner.resume(task_id, {"approval_id": "x", "approved": "yes"})
    assert runner.snapshot(task_id).status == "waiting_for_approval"
    # 失败的续跑没有占着名额
    other = await runner.submit(request())
    assert (await runner.wait(other)).status == "waiting_for_approval"


@pytest.mark.unit
async def test_resuming_a_running_task_is_refused(world: World) -> None:
    world.gate = asyncio.Event()
    runner = world.runner(max_concurrent=2)
    task_id = await runner.submit(request())
    from agent_runtime.runtime import TaskAlreadyRunningError

    with pytest.raises(TaskAlreadyRunningError):
        await runner.resume(task_id)
    world.gate.set()
    await runner.wait(task_id)


# ==============================================================================
# 取消
# ==============================================================================


@pytest.mark.unit
async def test_cancelling_a_running_task(world: World) -> None:
    world.gate = asyncio.Event()
    observer = Observer()
    runner = world.runner(observer=observer)
    task_id = await runner.submit(request())
    await world.entered.wait()

    assert await runner.cancel(task_id) is True
    snapshot = runner.snapshot(task_id)
    assert snapshot.status == "cancelled"
    assert [s.status for s in observer.finished] == ["cancelled"]

    stored = await world.store.load(task_id)
    assert stored is not None
    meta = await world.store.load_status(task_id)  # 取消被持久化，重启后不会被当成“进程中断”
    assert meta is not None and meta.status == "finished" and meta.outcome == "cancelled"
    assert (await scan_for_recovery(world.store)).interrupted == ()


@pytest.mark.unit
async def test_cancelling_between_tool_calls_keeps_the_pairs_whole(world: World) -> None:
    """取消落在模型回复提交之后、工具执行期间：未配对的调用要被合成“已取消”结果。"""
    world.tool_gate = asyncio.Event()
    runner = world.runner()
    task_id = await runner.submit(request())
    await world.inside_tools.wait()
    await runner.cancel(task_id)

    stored = await world.store.load(task_id)
    assert stored is not None
    check_invariants(stored)
    assert stored.messages[-1].role == "tool"
    assert "取消" in stored.messages[-1].content


@pytest.mark.unit
async def test_cancelling_a_waiting_task_clears_the_request(world: World) -> None:
    world.needs_approval = True
    runner = world.runner()
    task_id = await runner.submit(request())
    await runner.wait(task_id)

    assert await runner.cancel(task_id) is True
    assert runner.snapshot(task_id).status == "cancelled"
    stored = await world.store.load(task_id)
    assert stored is not None
    assert stored.pending_approval is None
    meta = await world.store.load_status(task_id)
    assert meta is not None and meta.outcome == "cancelled"
    check_invariants(stored)
    report = await scan_for_recovery(world.store)
    assert report.waiting_for_approval == () and report.interrupted == ()


@pytest.mark.unit
async def test_cancelling_a_finished_task_does_nothing(world: World) -> None:
    runner = world.runner()
    task_id = await runner.submit(request())
    await runner.wait(task_id)
    assert await runner.cancel(task_id) is False
    assert runner.snapshot(task_id).status == "succeeded"


@pytest.mark.unit
async def test_cancelling_frees_the_slot(world: World) -> None:
    world.gate = asyncio.Event()
    runner = world.runner(max_concurrent=1)
    task_id = await runner.submit(request())
    await world.entered.wait()
    await runner.cancel(task_id)
    world.gate.set()
    again = await runner.submit(request())
    assert (await runner.wait(again)).status == "succeeded"


# ==============================================================================
# 关停与启动接管
# ==============================================================================


@pytest.mark.unit
async def test_shutdown_stops_tasks_but_leaves_them_resumable(world: World) -> None:
    world.gate = asyncio.Event()
    runner = world.runner()
    task_id = await runner.submit(request())
    await world.entered.wait()

    await runner.shutdown()
    stored = await world.store.load(task_id)
    assert stored is not None
    meta = await world.store.load_status(task_id)
    assert meta is not None and meta.status == "running"  # 不是用户取消：不写终态，下次启动可以续跑
    assert [u.task_id for u in await world.store.list_unfinished()] == [task_id]


@pytest.mark.unit
async def test_startup_adopts_interrupted_and_waiting_tasks(tmp_path: Path) -> None:
    async with CheckpointStore(tmp_path / "state.db") as store:
        # 上一个“进程”：一个跑到一半崩了，一个挂在审批上
        before = World(store)
        before.fail_model = RuntimeError("process died")
        crashed_runner = before.runner()
        crashed = await crashed_runner.submit(request())
        await crashed_runner.wait_all()
        before.needs_approval = True
        before.fail_model = None
        parked_runner = before.runner()
        parked = await parked_runner.submit(request())
        await parked_runner.wait(parked)

        # 新进程
        after = World(store)
        runner = after.runner()
        report = await scan_for_recovery(store)
        await runner.adopt(report)

        assert runner.snapshot(crashed).status == "failed"
        assert "进程中断" in runner.snapshot(crashed).reason
        assert runner.snapshot(parked).status == "waiting_for_approval"

        await runner.resume(crashed)  # 失败的任务可以显式续跑
        assert (await runner.wait(crashed)).status == "succeeded"
