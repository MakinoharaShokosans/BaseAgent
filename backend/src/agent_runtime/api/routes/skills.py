"""技能管理：目录、来源开关、逐技能开关、SKILL.md 正文。

技能内容仍然只在文件系统里；界面只改变 ``skills.policy`` 覆盖层，下次任务装配时生效。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.api.errors import ApiError
from agent_runtime.api.schemas import SkillSourceOut, SkillSourceToggleIn, SkillToggleIn
from agent_runtime.runtime.overrides import (
    SkillPolicy,
    load_skill_policy,
    save_skill_policy,
)

router = APIRouter(tags=["skills"])
Services = Annotated[ApiServices, Depends(services_of)]
SourceName = Annotated[str, Path(pattern=r"^(builtin|global|workspace)$")]
SkillName = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]


async def _catalog(svc: ApiServices) -> list[dict[str, Any]]:
    if svc.skill_catalog is not None:
        return await svc.skill_catalog()
    return svc.introspection.skills()


def _source_enabled(svc: ApiServices, policy: SkillPolicy, source: str) -> bool:
    cfg = svc.settings.skills
    default = {
        "builtin": cfg.allow_builtin,
        "global": cfg.allow_global,
        "workspace": cfg.allow_workspace,
    }[source]
    return policy.source_enabled(source, default)  # type: ignore[arg-type]


def _source_out(
    svc: ApiServices, policy: SkillPolicy, source: str, rows: list[dict[str, Any]]
) -> SkillSourceOut:
    count = sum(1 for row in rows if row.get("source") == source)
    return SkillSourceOut(
        source=source,  # type: ignore[arg-type]
        enabled=_source_enabled(svc, policy, source),
        count=count,
        trusted=source != "workspace",
    )


@router.get("/skills")
async def list_skills(svc: Services) -> list[dict[str, Any]]:
    return await _catalog(svc)


@router.get("/skills/sources", response_model=list[SkillSourceOut])
async def list_sources(svc: Services) -> list[SkillSourceOut]:
    policy = await load_skill_policy(svc.memory)
    rows = await _catalog(svc)
    return [_source_out(svc, policy, source, rows) for source in ("builtin", "global", "workspace")]


@router.get("/skills/{name}")
async def skill_body(
    name: SkillName,
    svc: Services,
    workspace_id: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9_-]{1,64}$")] = None,
) -> dict[str, Any]:
    if svc.skill_body is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "技能正文不可用")
    body = await svc.skill_body(name, workspace_id)
    if body is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", f"没有技能 {name}")
    return {
        "name": body.get("name", name),
        "content": body.get("body", ""),
        "source": body.get("source", ""),
        "workspace_id": body.get("workspace_id"),
        "helper_files": body.get("helper_files", {}),
    }


@router.put("/skills/{name}/enabled")
async def set_skill_enabled(name: SkillName, body: SkillToggleIn, svc: Services) -> dict[str, Any]:
    rows = await _catalog(svc)
    match = next((row for row in rows if row.get("name") == name), None)
    if match is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", f"没有技能 {name}")
    policy = await load_skill_policy(svc.memory)
    if body.enabled:
        policy.disabled.discard(name)
    else:
        policy.disabled.add(name)
    await save_skill_policy(svc.memory, policy)
    match = dict(match)
    match["enabled"] = body.enabled and _source_enabled(svc, policy, str(match.get("source")))
    return match


@router.put("/skills/sources/{source}", response_model=SkillSourceOut)
async def set_source_enabled(
    source: SourceName, body: SkillSourceToggleIn, svc: Services
) -> SkillSourceOut:
    policy = await load_skill_policy(svc.memory)
    policy.sources[source] = body.enabled
    await save_skill_policy(svc.memory, policy)
    return _source_out(svc, policy, source, await _catalog(svc))
