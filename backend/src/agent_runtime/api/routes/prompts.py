"""提示词模板：列出、读取、覆盖、恢复初始。

覆盖只写 ``app_settings``，不碰 ``prompts/*.md``；装配层每个任务读一次 ``PromptLibrary``，
因此改动对下一个任务生效。动态分区与固定安全指令不在这里，前端单独展示。
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.api.errors import ApiError
from agent_runtime.api.schemas import PromptDetailOut, PromptIn, PromptInfoOut
from agent_runtime.runtime.overrides import prompt_key

router = APIRouter(tags=["prompts"])
Services = Annotated[ApiServices, Depends(services_of)]
PromptName = Annotated[str, Path(pattern=r"^[A-Za-z0-9_]{1,64}$")]

MAX_CHARS: Final = 20_000
_TITLES: Final = {
    "system": "系统提示词",
    "compactor": "会话压缩",
    "task_compactor": "任务内压缩",
    "subagent": "子智能体",
    "subagent_report": "子智能体汇报",
    "websearch_plan": "联网搜索·规划",
    "websearch_extract": "联网搜索·提炼",
}
_USAGE: Final = {
    "system": "主 Agent",
    "compactor": "会话压缩",
    "task_compactor": "任务内压缩",
    "subagent": "子智能体",
    "subagent_report": "子智能体汇报",
    "websearch_plan": "联网搜索",
    "websearch_extract": "联网搜索",
}


def _known(svc: ApiServices, name: str) -> None:
    if name not in svc.prompts.names():
        raise ApiError(404, "RESOURCE_NOT_FOUND", f"没有模板 {name}")


def _detail(svc: ApiServices, name: str) -> PromptDetailOut:
    _known(svc, name)
    try:
        default = svc.prompts.default(name)
        content = svc.prompts.get(name)
    except KeyError as exc:
        raise ApiError(404, "RESOURCE_NOT_FOUND", f"没有模板 {name}") from exc
    return PromptDetailOut(
        name=name,
        content=content,
        default=default,
        customized=svc.prompts.customized(name),
        max_chars=MAX_CHARS,
    )


@router.get("/prompts", response_model=list[PromptInfoOut])
async def list_prompts(svc: Services) -> list[PromptInfoOut]:
    out: list[PromptInfoOut] = []
    for name in svc.prompts.names():
        try:
            size = len(svc.prompts.get(name))
        except KeyError:
            size = 0
        out.append(
            PromptInfoOut(
                name=name,
                title=_TITLES.get(name, name),
                usage=_USAGE.get(name, ""),
                size=size,
                customized=svc.prompts.customized(name),
                max_chars=MAX_CHARS,
            )
        )
    return out


@router.get("/prompts/{name}", response_model=PromptDetailOut)
async def get_prompt(name: PromptName, svc: Services) -> PromptDetailOut:
    return _detail(svc, name)


@router.put("/prompts/{name}", response_model=PromptDetailOut)
async def put_prompt(name: PromptName, body: PromptIn, svc: Services) -> PromptDetailOut:
    _known(svc, name)
    text = body.content.strip()
    if not text:
        raise ApiError(422, "VALIDATION_ERROR", "提示词不能为空；恢复初始请用 DELETE")
    if len(text) > MAX_CHARS:
        raise ApiError(422, "VALIDATION_ERROR", f"提示词超过上限 {MAX_CHARS} 字符")
    await svc.memory.set_app_setting(prompt_key(name), text)
    svc.prompts.set_override(name, text)
    return _detail(svc, name)


@router.delete("/prompts/{name}", response_model=PromptDetailOut)
async def reset_prompt(name: PromptName, svc: Services) -> PromptDetailOut:
    _known(svc, name)
    await svc.memory.set_app_setting(prompt_key(name), None)
    svc.prompts.set_override(name, None)
    return _detail(svc, name)
