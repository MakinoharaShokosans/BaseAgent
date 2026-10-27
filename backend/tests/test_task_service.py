"""任务服务：提交、快照、审批、续跑、取消、删除、投影。出处：docs/09_api/01_endpoints.md §3-4。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_runtime.approval import compute_approval_id
from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.memory import MemoryStore, SessionNotFound
from agent_runtime.observability import EventHub
from agent_runtime.runtime import (
    FullPermissionsNotConfirmedError,
    RunnerDeps,
    TaskNotFoundError,
    TaskNotResumableError,
    TaskQueueFullError,
    TaskRunner,
)
from agent_runtime.runtime.service import (
    ArtifactNotFoundError,
    TaskGoalError,
    TaskService,
    TaskStateConflictError,
)
from api_support import REDACTOR, World, resolve


class Rig:
    def __init__(self) -> None:
        self.world: World
        self.service: TaskService
        self.memory: MemoryStore
        self.hub: EventHub
        self.session = "s1"
        self.root: Path


@pytest.fixture
async def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    r = Rig()
    r.root = tmp_path / "proj"
    r.root.mkdir()
    async with (
        MemoryStore(tmp_path / "meta.db") as memory,
        CheckpointStore(tmp_path / "state.db") as checkpoints,
    ):
        hub = EventHub()
        world = World(checkpoints, hub)
        deps = RunnerDeps(
            checkpoints=checkpoints,
            build_runtime=world.build,
            is_idempotent=lambda name: False,
            make_canary=lambda session, task: f"canary-{session}-{task}",
        )
        runner = TaskRunner(deps, max_concurrent=1, new_task_id=lambda: next(world.ids))
        artifacts = tmp_path / "artifacts"
        service = TaskService(
            runner=runner,
            memory=memory,
            checkpoints=checkpoints,
            hub=hub,
            redactor=REDACTOR,
            resolve_level=resolve,
            goal_max_chars=50,
            artifacts_root=artifacts,
            traces_root=tmp_path / "traces",
        )
        runner.attach_observer(service.observer())
        await memory.create_workspace("w1", "proj", str(r.root))
        await memory.create_session("s1", "w1")
        r.world, r.service, r.memory, r.hub = world, service, memory, hub
        r.artifacts = artifacts  # type: ignore[attr-defined]
        yield r
        await runner.shutdown()


# ------------------------------------------------------------------ 提交


@pytest.mark.unit
async def test_submit_records_metadata_before_running_and_returns_a_view(rig: Rig) -> None:
    rig.world.gate = asyncio.Event()
    view = await rig.service.submit("s1", "  fix it  ")
    assert (view.task_id, view.status, view.task_goal) == ("task-1", "running", "fix it")
    assert (view.session_id, view.workspace_id, view.permission_level) == (
        "s1",
        "w1",
        "workspace_write",
    )
    row = await rig.memory.get_task("task-1")
    assert row is not None and row.task_goal == "fix it"
    await rig.world.entered.wait()
    assert (await rig.memory.get_task("task-1")).started_at is not None  # type: ignore[union-attr]
    rig.world.gate.set()
    await rig.service.runner.wait("task-1")


@pytest.mark.unit
@pytest.mark.parametrize("goal", ["", "   ", "x" * 51])
async def test_bad_goals_are_refused_without_leaving_anything(rig: Rig, goal: str) -> None:
    with pytest.raises(TaskGoalError):
        await rig.service.submit("s1", goal)
    assert await rig.memory.list_tasks("s1") == []


@pytest.mark.unit
async def test_unknown_session_and_unconfirmed_full_permissions(rig: Rig) -> None:
    with pytest.raises(SessionNotFound):
        await rig.service.submit("nope", "g")
    with pytest.raises(FullPermissionsNotConfirmedError):
        await rig.service.submit("s1", "g", permission_level="full_permissions")
    view = await rig.service.submit(
        "s1", "g", permission_level="full_permissions", confirm_full_permissions=True
    )
    assert view.permission_level == "full_permissions"
    await rig.service.runner.wait(view.task_id)


@pytest.mark.unit
async def test_queue_full_leaves_no_metadata_behind(rig: Rig) -> None:
    rig.world.gate = asyncio.Event()
    await rig.service.submit("s1", "a")
    with pytest.raises(TaskQueueFullError):
        await rig.service.submit("s1", "b")
    assert [t.task_id for t in await rig.memory.list_tasks("s1")] == ["task-1"]
    rig.world.gate.set()
    await rig.service.runner.wait("task-1")


# ------------------------------------------------------------------ 完成与快照


@pytest.mark.unit
async def test_finished_task_updates_metadata_and_events(rig: Rig) -> None:
    rig.world.artifact = None
    view = await rig.service.submit("s1", "do it")
    await rig.service.runner.wait(view.task_id)
    done = await rig.service.get(view.task_id)
    assert done.status == "succeeded" and done.finished_at is not None
    assert done.step_count == 2 and done.milestones == []  # 回合数；没有写过计划
    row = await rig.memory.get_task(view.task_id)
    assert row is not None and row.status == "succeeded" and row.step_count == 2
    events = await rig.hub.history(view.task_id)
    assert events[0].name == "task.started" and events[-1].name == "task.finished"
    assert events[-1].data["status"] == "succeeded"
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


@pytest.mark.unit
async def test_failed_task_reports_the_reason(rig: Rig) -> None:
    rig.world.fail_model = RuntimeError("boom")
    view = await rig.service.submit("s1", "g")
    await rig.service.runner.wait(view.task_id)
    failed = await rig.service.get(view.task_id)
    assert failed.status == "failed" and "boom" in failed.termination_reason
    names = [e.name for e in await rig.hub.history(view.task_id)]
    assert names[-2:] == ["task.error", "task.finished"]


@pytest.mark.unit
async def test_a_failed_task_can_be_resumed_and_event_seqs_continue(rig: Rig) -> None:
    """余额不足等模型错误：failed，可续跑；没有额度要对齐。"""
    rig.world.fail_model = RuntimeError("402 insufficient balance")
    view = await rig.service.submit("s1", "g")
    await rig.service.runner.wait(view.task_id)
    assert (await rig.service.get(view.task_id)).status == "failed"
    rig.world.fail_model = None
    resumed = await rig.service.resume(view.task_id)
    assert resumed.status == "running"
    await rig.service.runner.wait(view.task_id)
    assert (await rig.service.get(view.task_id)).status == "succeeded"
    seqs = [e.seq for e in await rig.hub.history(view.task_id)]
    assert seqs == list(range(1, len(seqs) + 1))  # 续跑后序号接着走


@pytest.mark.security
@pytest.mark.unit
async def test_a_security_termination_is_final_and_cannot_be_resumed(rig: Rig) -> None:
    rig.world.leak = True
    view = await rig.service.submit("s1", "g")
    await rig.service.runner.wait(view.task_id)
    done = await rig.service.get(view.task_id)
    assert done.status == "terminated" and done.should_terminate is True
    rig.world.leak = False
    with pytest.raises(TaskNotResumableError):
        await rig.service.resume(view.task_id)


@pytest.mark.unit
async def test_unknown_task(rig: Rig) -> None:
    for call in (rig.service.get, rig.service.cancel, rig.service.resume, rig.service.delete):
        with pytest.raises(TaskNotFoundError):
            await call("nope")


# ------------------------------------------------------------------ 审批


async def waiting(rig: Rig) -> str:
    rig.world.needs_approval = True
    view = await rig.service.submit("s1", "g")
    await rig.service.runner.wait(view.task_id)
    return view.task_id


@pytest.mark.unit
async def test_waiting_task_exposes_the_approval_request_and_persists_status(rig: Rig) -> None:
    tid = await waiting(rig)
    view = await rig.service.get(tid)
    assert view.status == "waiting_for_approval" and view.approval_request is not None
    assert view.approval_request["action_type"] == "network_egress"
    assert (await rig.memory.get_task(tid)).status == "waiting_for_approval"  # type: ignore[union-attr]


@pytest.mark.unit
async def test_approve_emits_before_the_task_continues_and_completes(rig: Rig) -> None:
    tid = await waiting(rig)
    approval_id = compute_approval_id(tid, 2, ("c1",))
    view = await rig.service.approve(tid, approval_id, "once")
    assert view.status == "running"
    await rig.service.runner.wait(tid)
    assert (await rig.service.get(tid)).status == "succeeded"
    names = [e.name for e in await rig.hub.history(tid)]
    assert names.index("task.waiting_for_approval") < names.index("task.approved")
    assert names.index("task.approved") < names.index("tool.result")


@pytest.mark.unit
async def test_a_mismatched_approval_id_is_a_conflict_and_changes_nothing(rig: Rig) -> None:
    tid = await waiting(rig)
    before = [e.name for e in await rig.hub.history(tid)]
    with pytest.raises(Exception) as caught:
        await rig.service.approve(tid, "appr_wrong", "once")
    assert type(caught.value).__name__ == "ApprovalMismatchError"
    assert [e.name for e in await rig.hub.history(tid)] == before
    assert (await rig.service.get(tid)).status == "waiting_for_approval"


@pytest.mark.unit
async def test_repeating_the_same_decision_is_idempotent_but_a_different_one_conflicts(
    rig: Rig,
) -> None:
    tid = await waiting(rig)
    approval_id = compute_approval_id(tid, 2, ("c1",))
    await rig.service.approve(tid, approval_id, "once")
    await rig.service.runner.wait(tid)
    again = await rig.service.approve(tid, approval_id, "once")
    assert again.status == "succeeded"
    with pytest.raises(TaskStateConflictError):
        await rig.service.reject(tid, approval_id, "不")


@pytest.mark.unit
async def test_approving_a_task_that_is_not_waiting_is_a_conflict(rig: Rig) -> None:
    view = await rig.service.submit("s1", "g")
    await rig.service.runner.wait(view.task_id)
    with pytest.raises(TaskStateConflictError):
        await rig.service.approve(view.task_id, "appr_x", "once")


@pytest.mark.unit
async def test_reject_continues_with_a_refusal_and_emits_rejected(rig: Rig) -> None:
    tid = await waiting(rig)
    approval_id = compute_approval_id(tid, 2, ("c1",))
    await rig.service.reject(tid, approval_id, "太危险\n了")
    await rig.service.runner.wait(tid)
    rejected = next(e for e in await rig.hub.history(tid) if e.name == "task.rejected")
    assert rejected.data["reason"] == "太危险 了"


# ------------------------------------------------------------------ 续跑与取消


@pytest.mark.unit
async def test_resume_rules_per_status(rig: Rig) -> None:
    ok = await rig.service.submit("s1", "g")
    await rig.service.runner.wait(ok.task_id)
    with pytest.raises(TaskNotResumableError):
        await rig.service.resume(ok.task_id)

    rig.world.needs_approval = True
    w = await rig.service.submit("s1", "g")
    await rig.service.runner.wait(w.task_id)
    with pytest.raises(TaskStateConflictError):
        await rig.service.resume(w.task_id)


@pytest.mark.unit
async def test_a_running_task_cannot_be_resumed_or_deleted_and_cancel_is_idempotent(
    rig: Rig,
) -> None:
    rig.world.gate = asyncio.Event()
    view = await rig.service.submit("s1", "g")
    await rig.world.entered.wait()
    from agent_runtime.runtime import TaskAlreadyRunningError

    with pytest.raises(TaskAlreadyRunningError):
        await rig.service.resume(view.task_id)
    with pytest.raises(TaskStateConflictError):
        await rig.service.delete(view.task_id)
    cancelled = await rig.service.cancel(view.task_id)
    assert cancelled.status == "cancelled"
    again = await rig.service.cancel(view.task_id)
    assert again.status == "cancelled"
    assert (await rig.memory.get_task(view.task_id)).status == "cancelled"  # type: ignore[union-attr]
    with pytest.raises(TaskNotResumableError):
        await rig.service.resume(view.task_id)
    last = (await rig.hub.history(view.task_id))[-1]
    assert last.name == "task.finished" and last.data["status"] == "cancelled"


# ------------------------------------------------------------------ 删除


@pytest.mark.unit
async def test_delete_removes_metadata_checkpoint_events_and_files(
    rig: Rig, tmp_path: Path
) -> None:
    view = await rig.service.submit("s1", "g")
    await rig.service.runner.wait(view.task_id)
    folder = rig.artifacts / view.task_id  # type: ignore[attr-defined]
    folder.mkdir(parents=True)
    (folder / "obs.log").write_text("x")
    await rig.service.delete(view.task_id)
    assert await rig.memory.get_task(view.task_id) is None
    assert await rig.service._checkpoints.load(view.task_id) is None  # pyright: ignore[reportPrivateUsage]
    assert await rig.hub.history(view.task_id) == []
    assert not folder.exists()
    with pytest.raises(TaskNotFoundError):
        await rig.service.get(view.task_id)


@pytest.mark.unit
async def test_a_deleted_waiting_task_is_gone_from_the_runner_too(rig: Rig) -> None:
    tid = await waiting(rig)
    await rig.service.delete(tid)
    assert rig.service.runner.list_tasks() == []


# ------------------------------------------------------------------ 列表、时间线、产物


@pytest.mark.unit
async def test_list_pages_newest_first(rig: Rig) -> None:
    clock = iter(range(100, 200))
    rig.service._clock = lambda: float(next(clock))  # pyright: ignore[reportPrivateUsage]
    for _ in range(3):
        view = await rig.service.submit("s1", "g")
        await rig.service.runner.wait(view.task_id)
    page = await rig.service.list("s1", limit=2)
    assert [v.task_id for v in page] == ["task-3", "task-2"]
    rest = await rig.service.list("s1", limit=2, before=page[-1].created_at)
    assert [v.task_id for v in rest] == ["task-1"]


@pytest.mark.unit
async def test_timeline_is_a_scrubbed_projection_of_the_messages(rig: Rig) -> None:
    view = await rig.service.submit("s1", "g")
    await rig.service.runner.wait(view.task_id)
    items = await rig.service.timeline(view.task_id)
    roles = [i["role"] for i in items]
    assert roles[0] == "user" and "tool" in roles
    tool = next(i for i in items if i["role"] == "tool")
    assert "canary-s1-task-1" not in str(tool["content"])
    call = next(i for i in items if "tool_calls" in i)
    assert call["tool_calls"][0]["args"]["api_key"] == "[REDACTED]"  # type: ignore[index]
    assert len(await rig.service.timeline(view.task_id, limit=1)) == 1


@pytest.mark.unit
async def test_artifacts_listed_and_served_only_from_inside_the_task_folder(
    rig: Rig, tmp_path: Path
) -> None:
    folder = rig.artifacts / "task-1"  # type: ignore[attr-defined]
    folder.mkdir(parents=True)
    inside = folder / "obs_1.log"
    inside.write_text("hello")
    rig.world.artifact = inside
    view = await rig.service.submit("s1", "g")
    await rig.service.runner.wait(view.task_id)
    listed = await rig.service.artifacts(view.task_id)
    assert [(a.artifact_id, a.size) for a in listed] == [("obs_1", 5)]
    assert await rig.service.artifact_file(view.task_id, "obs_1") == inside.resolve()
    with pytest.raises(ArtifactNotFoundError):
        await rig.service.artifact_file(view.task_id, "other")


@pytest.mark.security
async def test_an_artifact_path_outside_the_task_folder_is_never_served(
    rig: Rig, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    (rig.artifacts / "task-1").mkdir(parents=True)  # type: ignore[attr-defined]
    rig.world.artifact = secret
    view = await rig.service.submit("s1", "g")
    await rig.service.runner.wait(view.task_id)
    assert [a.size for a in await rig.service.artifacts(view.task_id)] == [0]
    with pytest.raises(ArtifactNotFoundError):
        await rig.service.artifact_file(view.task_id, "obs_1")
