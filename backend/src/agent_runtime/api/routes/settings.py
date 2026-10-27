"""用户可调的设置：会话级（模型偏好、上下文上限）与全局（系统提示词）。

设置在下一个任务开始时生效。设计文档：docs/09_api/01_endpoints.md §2.1。
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.api.errors import ApiError
from agent_runtime.api.schemas import (
    AppSettingsIn,
    AppSettingsOut,
    RangeOut,
    SessionSettingsIn,
    SessionSettingsOut,
)
from agent_runtime.memory import MIN_CONTEXT_TOKEN_LIMIT, SYSTEM_PROMPT_KEY, SessionPreferences

router = APIRouter(tags=["settings"])
Services = Annotated[ApiServices, Depends(services_of)]
SessionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]

SYSTEM_PROMPT_MAX_CHARS: Final = 20_000
_LIMIT_STEP: Final = 1000


def _endpoints(svc: ApiServices) -> dict[str, int]:
    """端点名 → 上下文窗口；同名端点出现在多个档位时取较小的窗口。"""
    out: dict[str, int] = {}
    for item in svc.introspection.models():
        name, window = item.get("name"), item.get("context_window")
        if isinstance(name, str) and isinstance(window, int) and window > 0:
            out[name] = min(window, out.get(name, window))
    return out


def _limit_range(svc: ApiServices) -> RangeOut:
    """上限取最小窗口：会话压缩阈值超过任一端点的窗口都没有意义。"""
    windows = _endpoints(svc).values()
    default = svc.settings.context.session.token_limit
    top = min(windows) if windows else max(default, MIN_CONTEXT_TOKEN_LIMIT)
    return RangeOut(
        min=MIN_CONTEXT_TOKEN_LIMIT, max=max(top, MIN_CONTEXT_TOKEN_LIMIT), step=_LIMIT_STEP
    )


def _session_out(svc: ApiServices, prefs: SessionPreferences) -> SessionSettingsOut:
    return SessionSettingsOut(
        model=prefs.model,
        context_token_limit=prefs.context_token_limit,
        defaults={"context_token_limit": svc.settings.context.session.token_limit},
        bounds={"context_token_limit": _limit_range(svc)},
    )


@router.get("/sessions/{session_id}/settings", response_model=SessionSettingsOut)
async def get_session_settings(session_id: SessionId, svc: Services) -> SessionSettingsOut:
    data = await svc.memory.get_session_settings(session_id)
    return _session_out(svc, SessionPreferences.from_mapping(data))


@router.put("/sessions/{session_id}/settings", response_model=SessionSettingsOut)
async def put_session_settings(
    session_id: SessionId, body: SessionSettingsIn, svc: Services
) -> SessionSettingsOut:
    await svc.memory.get_session(session_id)
    if body.model is not None and body.model not in _endpoints(svc):
        raise ApiError(422, "VALIDATION_ERROR", f"未知的模型端点：{body.model}")
    limit = body.context_token_limit
    if limit is not None:
        bounds = _limit_range(svc)
        if not bounds.min <= limit <= bounds.max:
            raise ApiError(
                422,
                "VALIDATION_ERROR",
                f"context_token_limit 须在 {bounds.min} 到 {bounds.max} 之间",
            )
    prefs = SessionPreferences(model=body.model, context_token_limit=limit)
    await svc.memory.set_session_settings(session_id, prefs.to_mapping())
    return _session_out(svc, prefs)


def _app_out(svc: ApiServices, override: str | None) -> AppSettingsOut:
    return AppSettingsOut(
        system_prompt=override,
        system_prompt_default=svc.prompts.get("system"),
        system_prompt_max_chars=SYSTEM_PROMPT_MAX_CHARS,
    )


@router.get("/settings", response_model=AppSettingsOut)
async def get_app_settings(svc: Services) -> AppSettingsOut:
    return _app_out(svc, await svc.memory.get_app_setting(SYSTEM_PROMPT_KEY))


@router.put("/settings", response_model=AppSettingsOut)
async def put_app_settings(body: AppSettingsIn, svc: Services) -> AppSettingsOut:
    text = body.system_prompt.strip() if body.system_prompt is not None else ""
    if len(text) > SYSTEM_PROMPT_MAX_CHARS:
        raise ApiError(
            422, "VALIDATION_ERROR", f"系统提示词超过上限 {SYSTEM_PROMPT_MAX_CHARS} 字符"
        )
    override = text or None  # 空白视同恢复默认
    await svc.memory.set_app_setting(SYSTEM_PROMPT_KEY, override)
    return _app_out(svc, override)
