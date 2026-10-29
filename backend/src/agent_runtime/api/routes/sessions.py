"""会话、对话流水与会话记忆。设计文档：docs/09_api/01_endpoints.md §2。"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.api.errors import ApiError
from agent_runtime.api.routes.tasks import task_out
from agent_runtime.api.schemas import (
    CompactionOut,
    FactOut,
    FailureOut,
    ForkRequest,
    SessionMemoryOut,
    SessionOut,
    SessionUpdate,
    TaskOut,
    TurnOut,
)
from agent_runtime.memory import Fact, Failure, SessionInfo
from agent_runtime.memory.errors import MemoryError_, SessionNotFound
from agent_runtime.runtime.service import TaskStateConflictError

router = APIRouter(tags=["sessions"])
Services = Annotated[ApiServices, Depends(services_of)]
SessionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


def session_out(info: SessionInfo) -> SessionOut:
    return SessionOut(
        session_id=info.session_id,
        workspace_id=info.workspace_id,
        created_at=info.created_at,
        title=info.title,
        pinned=info.pinned,
        archived=info.archived,
        updated_at=info.updated_at,
    )


def _fact(f: Fact) -> FactOut:
    return FactOut(
        text=f.text, task_id=f.task_id, trust=f.trust, source=f.source, created_at=f.created_at
    )


def _failure(f: Failure) -> FailureOut:
    return FailureOut(
        action=f.attempt.action,
        failure_reason=f.attempt.failure_reason,
        conclusion=f.attempt.conclusion,
        task_id=f.task_id,
        created_at=f.created_at,
    )


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(session_id: SessionId, svc: Services) -> SessionOut:
    return session_out(await svc.memory.get_session(session_id))


@router.patch("/sessions/{session_id}", response_model=SessionOut)
async def update_session(session_id: SessionId, body: SessionUpdate, svc: Services) -> SessionOut:
    fields = body.model_fields_set
    if "title" in fields and body.title is not None and len(body.title) > 120:
        raise ApiError(422, "VALIDATION_ERROR", "标题不能超过 120 个字符")
    info = await svc.memory.update_session(
        session_id,
        title=body.title if "title" in fields else None,
        pinned=body.pinned if "pinned" in fields else None,
        archived=body.archived if "archived" in fields else None,
    )
    return session_out(info)


@router.post("/sessions/{session_id}/title", response_model=SessionOut)
async def generate_title(session_id: SessionId, svc: Services) -> SessionOut:
    """按首轮对话生成标题；已有标题时直接返回，生成失败保留空标题由前端兜底。"""
    info = await svc.memory.get_session(session_id)
    if info.title:
        return session_out(info)
    if svc.title_generator is None:
        return session_out(info)
    title = await svc.title_generator(session_id)
    if title:
        info = await svc.memory.update_session(session_id, title=title)
    return session_out(info)


@router.post("/sessions/{session_id}/fork", response_model=SessionOut, status_code=201)
async def fork_session(
    session_id: SessionId, svc: Services, body: ForkRequest | None = None
) -> SessionOut:
    """复制会话的对话与记忆为新会话；``from_turn_id`` 给出时只复制到该轮（含）为止。"""
    upto = body.from_turn_id if body else None
    new_id = "sess_" + uuid.uuid4().hex[:12]
    try:
        await svc.memory.fork_session(session_id, new_id, upto_turn_id=upto)
    except MemoryError_ as exc:
        if isinstance(exc, SessionNotFound):
            raise
        raise ApiError(404, "TURN_NOT_FOUND", "该轮次不属于这个会话") from exc
    return session_out(await svc.memory.get_session(new_id))


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: SessionId, svc: Services) -> Response:
    await svc.memory.get_session(session_id)
    task_ids = await svc.memory.task_ids_of_session(session_id)
    for task_id in task_ids:
        if (await svc.tasks.get(task_id)).status == "running":
            raise TaskStateConflictError("有任务正在运行，先取消再删除")
    await svc.memory.delete_session(session_id)
    for task_id in task_ids:
        await svc.tasks.forget_task(task_id)
    return Response(status_code=204)


@router.get("/sessions/{session_id}/turns", response_model=list[TurnOut])
async def list_turns(
    session_id: SessionId,
    svc: Services,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[TurnOut]:
    turns = await svc.memory.turns_page(session_id, limit, before_id)
    return [
        TurnOut(
            id=t.id,
            task_id=t.task_id,
            role=t.role,
            content=svc.redactor.text(t.content),
            created_at=t.created_at,
        )
        for t in turns
    ]


async def _memory_out(svc: ApiServices, session_id: str) -> SessionMemoryOut:
    m = await svc.memory.load_memory(session_id)
    return SessionMemoryOut(
        summary=m.summary,
        watermark=m.watermark,
        facts=[_fact(f) for f in m.facts],
        failures=[_failure(f) for f in m.failures],
        pending_facts=[_fact(f) for f in m.pending_facts],
        pending_failures=[_failure(f) for f in m.pending_failures],
    )


@router.get("/sessions/{session_id}/memory", response_model=SessionMemoryOut)
async def session_memory(session_id: SessionId, svc: Services) -> SessionMemoryOut:
    await svc.memory.get_session(session_id)
    return await _memory_out(svc, session_id)


@router.post("/sessions/{session_id}/memory/compact", response_model=CompactionOut)
async def compact_memory(session_id: SessionId, svc: Services) -> CompactionOut:
    """手动压缩：除最近几轮外并入摘要。有任务在跑时拒绝，免得与它的收尾写记忆交错。"""
    await svc.memory.get_session(session_id)
    if svc.compact_session is None:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "记忆压缩未装配")
    for task_id in await svc.memory.task_ids_of_session(session_id):
        if (await svc.tasks.get(task_id)).status == "running":
            raise TaskStateConflictError("有任务正在运行，等它结束后再压缩")
    result = await svc.compact_session(session_id)
    if result.status == "failed":
        raise ApiError(502, "COMPACTION_FAILED", "压缩失败，记忆保持不变，稍后再试")
    return CompactionOut(
        status="compacted" if result.status == "compacted" else "nothing_to_compact",
        turns=result.turns,
        memory=await _memory_out(svc, session_id),
    )


@router.get("/sessions/{session_id}/context")
async def session_context(session_id: SessionId, svc: Services) -> dict[str, Any]:
    await svc.memory.get_session(session_id)
    if svc.context_inspector is None:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "上下文检视未装配")
    return dict(await svc.context_inspector(session_id))


@router.get("/sessions/{session_id}/context/segments/{segment}")
async def context_segment(
    session_id: SessionId,
    segment: Annotated[str, Path(pattern=r"^[a-z_]{1,32}$")],
    svc: Services,
) -> dict[str, Any]:
    await svc.memory.get_session(session_id)
    if svc.context_segment is None:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "上下文检视未装配")
    data = await svc.context_segment(session_id, segment)
    if data is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", f"没有上下文分段 {segment}")
    return dict(data)


@router.get("/sessions/{session_id}/tasks", response_model=list[TaskOut])
async def list_tasks(
    session_id: SessionId,
    svc: Services,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[float | None, Query()] = None,
) -> list[TaskOut]:
    return [task_out(v) for v in await svc.tasks.list(session_id, limit, before)]
