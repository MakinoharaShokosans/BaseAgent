"""使用模型的阶段（planner、evaluator）的共同部分：依赖、调用与失败处置。

设计文档：docs/02_runtime/05_planner_evaluator.md §1.3。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from loguru import logger
from pydantic import BaseModel

from agent_runtime.core import AgentState, Message
from agent_runtime.guardrails import detect_canary_leak
from agent_runtime.llm import (
    Degraded,
    LLMBadRequest,
    LLMDeadlineExceeded,
    LLMRequest,
    LLMResponse,
    LLMUnavailableError,
    Parsed,
)
from agent_runtime.llm.types import Effort

CANARY_REASON: Final = "[SECURITY] Prompt leak detected via canary token"

#: 提示词装配：(状态, 阶段指令) -> 发给模型的消息。真实装配器在阶段 5（提示词装配文档）
Assembler = Callable[[AgentState, str], Sequence[Message]]
#: 向事件流发事件。失败只记日志
Emit = Callable[[str, Mapping[str, object]], Awaitable[None]]


class StructuredInvoker(Protocol):
    """``LLMGateway`` 满足它；测试用假实现。"""

    async def invoke_structured[T: BaseModel](
        self, request: LLMRequest, model: type[T]
    ) -> Parsed[T] | Degraded: ...


@dataclass(frozen=True, slots=True)
class ModelStageDeps:
    gateway: StructuredInvoker
    assemble: Assembler
    #: 本次调用的绝对截止时间，与网关同一时基：``clock() + guard.remaining_wall_sec()``
    deadline: Callable[[], float | None] = lambda: None
    emit: Emit | None = None
    reasoning_effort: Effort | None = None
    #: 附在阶段指令尾部的进度提示（已用步数与 Token）。指令在上下文尾部，不影响前缀缓存
    hint: Callable[[AgentState], str] = lambda state: ""


@dataclass(frozen=True, slots=True)
class Stop:
    """调用没有得到可用结果，阶段应当直接返回这份增量（终止任务）。"""

    update: Mapping[str, object]


def terminate(reason: str, **extra: object) -> dict[str, object]:
    return {"should_terminate": True, "termination_reason": reason, **extra}


async def guarded[T](purpose: str, call: Awaitable[T]) -> T | Stop:
    """等一次网关调用。网关错误转成终止增量，不向上抛：
    “LLM 全链路不可用”是预期内的失败（见 02_main_loop §3）。
    """
    try:
        return await call
    except LLMUnavailableError as exc:
        logger.error("[{}] LLM 全链路不可用：{}", purpose, exc)
        return Stop(terminate(f"LLM 不可用: {exc}"))
    except LLMDeadlineExceeded:
        return Stop(terminate("预算耗尽：截止时间不足以再发起一次模型调用"))
    except LLMBadRequest as exc:
        if exc.context_overflow:
            return Stop(terminate("上下文超出模型窗口，请求被拒绝"))
        return Stop(terminate(f"请求被模型端点拒绝: {exc}"))


async def ask[T: BaseModel](
    deps: ModelStageDeps, state: AgentState, *, purpose: str, instruction: str, model: type[T]
) -> Parsed[T] | Degraded | Stop:
    """走推理档调用一次结构化输出。"""
    request = LLMRequest(
        tier="reasoning",
        messages=tuple(deps.assemble(state, _with_hint(instruction, deps.hint(state)))),
        purpose=purpose,
        deadline=deps.deadline(),
        reasoning_effort=deps.reasoning_effort,
    )
    return await guarded(purpose, deps.gateway.invoke_structured(request, model))


def _with_hint(instruction: str, hint: str) -> str:
    return f"{instruction}\n{hint}" if hint else instruction


def response_leaked(response: LLMResponse, token: str, *extra: str) -> bool:
    """模型的原始输出（正文与工具参数，及额外文本）里是否带出了金丝雀。"""
    message = response.message
    parts: list[object] = [message.content, [c.args for c in message.tool_calls], *extra]
    return detect_canary_leak(parts, token)


def leaked(result: Parsed[BaseModel] | Degraded, token: str) -> bool:
    extra = (result.raw_text,) if isinstance(result, Degraded) else ()
    return response_leaked(result.response, token, *extra)


async def report_degraded(deps: ModelStageDeps, stage: str, result: Degraded) -> None:
    logger.warning("[{}] 结构化输出降级：{}", stage, result.reason)
    if deps.emit is None:
        return
    try:
        await deps.emit(
            "structured_output.degraded",
            {
                "stage": stage,
                "reason": result.reason,
                "problems": list(result.problems),
                "rounds": result.rounds,
            },
        )
    except Exception:
        logger.exception("事件发送失败（已忽略）")
