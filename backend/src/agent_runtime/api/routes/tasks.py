"""任务：提交、快照、审批、续跑、取消、事件流、时间线、产物。设计文档：docs/09_api/01 §3-4，02。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.api.errors import ApiError
from agent_runtime.api.schemas import (
    ApprovalRequestOut,
    ApproveBody,
    ArtifactOut,
    DecisionOut,
    MilestoneOut,
    RejectBody,
    TaskCreate,
    TaskOut,
)
from agent_runtime.observability import HEARTBEAT, Event
from agent_runtime.runtime.service import TaskView

router = APIRouter(tags=["tasks"])
Services = Annotated[ApiServices, Depends(services_of)]
TaskId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
SessionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


def task_out(v: TaskView) -> TaskOut:
    return TaskOut(
        task_id=v.task_id,
        session_id=v.session_id,
        workspace_id=v.workspace_id,
        task_goal=v.task_goal,
        status=v.status,
        permission_level=v.permission_level,
        approval_request=ApprovalRequestOut(**v.approval_request) if v.approval_request else None,  # type: ignore[arg-type]
        milestones=[MilestoneOut(**m) for m in v.milestones],  # type: ignore[arg-type]
        current_milestone_idx=v.current_milestone_idx,
        step_count=v.step_count,
        total_tokens=v.total_tokens,
        consecutive_errors=v.consecutive_errors,
        should_terminate=v.should_terminate,
        termination_reason=v.termination_reason,
        created_at=v.created_at,
        started_at=v.started_at,
        finished_at=v.finished_at,
        artifact_count=v.artifact_count,
    )


@router.post("/sessions/{session_id}/tasks", response_model=TaskOut, status_code=202)
async def submit_task(session_id: SessionId, body: TaskCreate, svc: Services) -> TaskOut:
    if len(body.task_goal) > svc.settings.server.task_goal_max_chars:
        raise ApiError(
            400,
            "VALIDATION_ERROR",
            f"task_goal 超过上限 {svc.settings.server.task_goal_max_chars} 字符",
        )
    view = await svc.tasks.submit(
        session_id,
        body.task_goal,
        parent_task_id=body.parent_task_id,
        permission_level=body.permission_level,
        confirm_full_permissions=body.confirm_full_permissions,
    )
    return task_out(view)


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: TaskId, svc: Services) -> TaskOut:
    return task_out(await svc.tasks.get(task_id))


@router.post("/tasks/{task_id}/approve", response_model=DecisionOut, status_code=202)
async def approve(task_id: TaskId, body: ApproveBody, svc: Services) -> DecisionOut:
    view = await svc.tasks.approve(task_id, body.approval_id, body.decision)
    return DecisionOut(task_id=task_id, status=view.status, message="已批准，任务继续执行")


@router.post("/tasks/{task_id}/reject", response_model=DecisionOut, status_code=202)
async def reject(task_id: TaskId, body: RejectBody, svc: Services) -> DecisionOut:
    view = await svc.tasks.reject(task_id, body.approval_id, body.reason)
    return DecisionOut(task_id=task_id, status=view.status, message="已拒绝，任务继续执行")


@router.post("/tasks/{task_id}/resume", response_model=DecisionOut, status_code=202)
async def resume(task_id: TaskId, svc: Services) -> DecisionOut:
    view = await svc.tasks.resume(task_id)
    return DecisionOut(task_id=task_id, status=view.status, message="已从检查点续跑")


@router.post("/tasks/{task_id}/cancel", response_model=DecisionOut)
async def cancel(task_id: TaskId, svc: Services) -> DecisionOut:
    view = await svc.tasks.cancel(task_id)
    return DecisionOut(task_id=task_id, status=view.status, message="任务已取消")


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: TaskId, svc: Services) -> Response:
    await svc.tasks.delete(task_id)
    return Response(status_code=204)


# ------------------------------------------------------------------ 事件流


def frame(event: Event) -> str:
    """SSE 帧。心跳不带 ``id``，不计入序号。"""
    body = event.to_json()
    if event.name == HEARTBEAT:
        return f"event: {HEARTBEAT}\ndata: {json.dumps({'task_id': event.task_id})}\n\n"
    return f"id: {event.seq}\nevent: {event.name}\ndata: {body}\n\n"


@router.get("/tasks/{task_id}/stream")
async def stream(
    task_id: TaskId,
    request: Request,
    svc: Services,
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    await svc.tasks.get(task_id)  # 不存在直接 404，而不是开一个永远空着的流
    header = last_event_id or ""
    cursor = after if after is not None else (int(header) if header.isdigit() else 0)
    heartbeat = float(svc.settings.server.sse_heartbeat_sec)

    async def body() -> AsyncIterator[str]:
        events = svc.hub.stream(task_id, cursor, heartbeat_sec=heartbeat)
        try:
            async for event in events:
                yield frame(event)
        finally:
            await events.aclose()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ 投影


@router.get("/tasks/{task_id}/timeline")
async def timeline(
    task_id: TaskId, svc: Services, limit: Annotated[int, Query(ge=1, le=1000)] = 200
) -> list[dict[str, object]]:
    return await svc.tasks.timeline(task_id, limit)


@router.get("/tasks/{task_id}/trace")
async def trace(task_id: TaskId, svc: Services) -> Response:
    """全量因果轨迹（NDJSON）。有轨迹文件就直接下载它；没有时退回到事件（同样已脱敏）。"""
    await svc.tasks.get(task_id)
    file = svc.tasks.trajectory_file(task_id)
    if file is not None:
        return FileResponse(
            file,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{task_id}.ndjson"'},
        )
    lines = [e.to_json() for e in await svc.hub.history(task_id, 0, 100_000)]
    return Response(
        "\n".join(lines) + ("\n" if lines else ""),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{task_id}.ndjson"'},
    )


@router.get("/tasks/{task_id}/llm_calls")
async def llm_calls(task_id: TaskId, svc: Services) -> list[dict[str, Any]]:
    await svc.tasks.get(task_id)
    return [e.to_dict() for e in await svc.hub.history(task_id, 0, 100_000) if e.name == "llm.call"]


@router.get("/tasks/{task_id}/spans")
async def spans(task_id: TaskId, svc: Services) -> list[dict[str, Any]]:
    """由事件推出的 span：每个阶段一个，工具调用按 ``call_id`` 与结果配对。"""
    await svc.tasks.get(task_id)
    events = await svc.hub.history(task_id, 0, 100_000)
    results = {e.data.get("call_id"): e for e in events if e.name == "tool.result"}
    out: list[dict[str, Any]] = []
    for e in events:
        if e.name == "node.finished":
            out.append({"kind": "stage", "name": e.data.get("node"), "seq": e.seq, "ts": e.ts})
        elif e.name == "tool.call":
            done = results.get(e.data.get("call_id"))
            out.append(
                {
                    "kind": "tool",
                    "name": e.data.get("tool"),
                    "seq": e.seq,
                    "ts": e.ts,
                    "ended_ts": done.ts if done else None,
                    "ok": done.data.get("ok") if done else None,
                }
            )
    return out


# ------------------------------------------------------------------ 产物


@router.get("/tasks/{task_id}/artifacts", response_model=list[ArtifactOut])
async def artifacts(task_id: TaskId, svc: Services) -> list[ArtifactOut]:
    return [
        ArtifactOut(artifact_id=a.artifact_id, size=a.size)
        for a in await svc.tasks.artifacts(task_id)
    ]


@router.get("/tasks/{task_id}/artifacts/{artifact_id}")
async def download_artifact(
    task_id: TaskId,
    artifact_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_.-]{1,128}$")],
    svc: Services,
) -> FileResponse:
    path = await svc.tasks.artifact_file(task_id, artifact_id)
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=f"{artifact_id}.log")
