"""子智能体执行循环：步数与单次运行超时封顶的朴素异步循环，没有 Token 额度。

设计文档：06_subagents/01_dynamic_delegation.md §4。越级即失败，绝不挂起（也不参考会话白名单）；
子级的中间观察值只在这里，不进主状态、不进检查点、不进事件。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from loguru import logger
from pydantic import BaseModel

from agent_runtime.core import Message, ToolCall
from agent_runtime.guardrails import (
    PermissionPolicy,
    check_permission,
    detect_canary_leak,
    observation_envelope,
)
from agent_runtime.llm import Degraded, LLMRequest, LLMResponse, Parsed
from agent_runtime.subagents.delegate.report import (
    AccessSet,
    DelegateLimits,
    RawSubagentReport,
    SubagentReport,
    build_report,
)
from agent_runtime.subagents.usage import ChildUsage
from tools.core import ToolDispatcher, ToolRegistry

Emit = Callable[[str, Mapping[str, object]], Awaitable[None]]
_BLOCKED = (
    "需要的权限没有授予，且你不能发起审批：{reason}。"
    "请换一种不需要该权限的做法，或在结论里如实说明做不到。"
)


class SubagentGateway(Protocol):
    async def invoke(self, request: LLMRequest) -> LLMResponse: ...

    async def invoke_structured[T: BaseModel](
        self, request: LLMRequest, model: type[T]
    ) -> Parsed[T] | Degraded: ...


@dataclass(frozen=True, slots=True)
class RunnerLimits:
    max_steps: int
    max_observation_chars: int
    report: DelegateLimits


class SubagentRunner:
    def __init__(
        self,
        gateway: SubagentGateway,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        level: str,
        limits: RunnerLimits,
        *,
        system_prompt: str,
        extract_prompt: str,
        tier: str = "fast",
        canary: str = "",
        depth: int = 1,
        emit: Emit | None = None,
    ) -> None:
        self._gateway = gateway
        self._registry = registry
        self._dispatcher = ToolDispatcher(registry)
        self._policy = policy
        self._level = level
        self._limits = limits
        self._system = system_prompt
        self._extract = extract_prompt
        self._tier = tier
        self._canary = canary
        self._depth = depth
        self._emit = emit

    async def run(self, role: str, goal: str, budget: ChildUsage, max_steps: int) -> SubagentReport:
        started = time.monotonic()
        access = AccessSet()
        warnings: list[str] = []
        steps = calls = 0
        messages: list[Message] = [
            Message(role="system", content=self._system.replace("{role}", role)),
            Message(role="user", content=goal),
        ]
        await self._say(
            "subagent.started",
            {
                "role": role,
                "depth": self._depth,
                "assigned_tools": sorted(s.name for s in self._registry.schemas()),
                "max_steps": max_steps,
            },
        )
        try:
            schemas = self._registry.schemas()
            while steps < min(max_steps, self._limits.max_steps):
                if budget.expired:
                    warnings.append(budget.reason())
                    break
                response = await self._model(messages, schemas, budget)
                if response is None:
                    warnings.append("模型调用失败，提前收尾")
                    break
                steps += 1
                budget.add_tokens(response.usage.total_tokens)  # 用量取自响应，不信自述
                if self._canary and detect_canary_leak(response.message.content, self._canary):
                    warnings.append("检测到提示词泄露样态，已终止子智能体")
                    break
                message = response.message
                messages.append(message)
                await self._say(
                    "subagent.step", {"step": steps, "tools": [c.name for c in message.tool_calls]}
                )
                if not message.tool_calls:
                    break
                results = await self._run_calls(message.tool_calls, access, role, steps)
                calls += len(message.tool_calls)
                messages += results
            else:
                warnings.append(f"达到步数上限 {max_steps}")
            raw = await self._finalise(messages, budget, warnings)
        except Exception as exc:  # 任何内部异常都降级，不让主循环看到
            logger.warning("子智能体失败：{}", type(exc).__name__)
            warnings.append(f"子智能体出错：{type(exc).__name__}")
            raw = RawSubagentReport()
        report = build_report(
            raw,
            access=access,
            limits=self._limits.report,
            steps_used=steps,
            tool_calls=calls,
            total_tokens=budget.used,
            elapsed_sec=time.monotonic() - started,
            extra_warnings=tuple(warnings),
        )
        await self._say(
            "subagent.finished",
            {
                "role": role,
                "status": report.status,
                "steps_used": report.steps_used,
                "tool_calls": report.tool_calls,
                "tokens": report.total_tokens,
                "findings": len(report.findings),
                "verified_findings": sum(1 for f in report.findings if f.verified),
                "elapsed_sec": report.elapsed_sec,
            },
        )
        return report

    # ------------------------------------------------------------------ 步骤

    async def _model(
        self, messages: Sequence[Message], schemas: Sequence[object], budget: ChildUsage
    ) -> LLMResponse | None:
        request = LLMRequest(
            tier=self._tier,  # type: ignore[arg-type]
            messages=tuple(messages),
            tools=tuple(schemas),  # type: ignore[arg-type]
            purpose="subagent",
        )
        try:
            return await asyncio.wait_for(
                self._gateway.invoke(request), max(budget.remaining_wall_sec, 0.1)
            )
        except Exception as exc:
            logger.warning("子智能体的模型调用失败：{}", type(exc).__name__)
            return None

    async def _run_calls(
        self, calls: Sequence[ToolCall], access: AccessSet, role: str, step: int
    ) -> list[Message]:
        allowed: list[ToolCall] = []
        out: dict[str, Message] = {}
        for call in calls:
            decision = check_permission(self._policy, self._level, call.name, call.args)
            if not self._registry.knows(call.name):
                out[call.id] = _tool_message(call, f"没有工具 {call.name}：你只能使用被分配的工具")
            elif not decision.allowed:
                out[call.id] = _tool_message(call, _BLOCKED.format(reason=decision.reason))
                await self._say(
                    "subagent.blocked",
                    {
                        "step": step,
                        "tool": call.name,
                        "required_level": decision.required_level,
                        "current_level": decision.current_level,
                        "action_type": decision.action_type,
                        "reason": decision.reason,
                    },
                )
            else:
                allowed.append(call)
        dispatched = await self._dispatcher.dispatch([(c.id, c.name, c.args) for c in allowed])
        for item in dispatched:
            call = next(c for c in allowed if c.id == item.tool_call_id)
            result = item.result
            access.record(call.args, result.meta, ok=not result.failed)
            text = (result.content or result.error or "")[: self._limits.max_observation_chars]
            if item.trust == "untrusted":
                text = observation_envelope(call.name, item.trust, text)
            out[call.id] = _tool_message(call, text)
            await self._say(
                "subagent.tool",
                {
                    "step": step,
                    "tool": call.name,
                    "ok": not result.failed,
                    "duration_ms": result.duration_ms,
                    "untrusted": item.trust == "untrusted",
                    "summary": _digest(result.failed, result.content or result.error or ""),
                },
            )
        return [out[c.id] for c in calls]

    async def _finalise(
        self, messages: Sequence[Message], budget: ChildUsage, warnings: list[str]
    ) -> RawSubagentReport:
        if budget.expired:
            warnings.append("单次运行已超时，未能提炼结论")
            return RawSubagentReport()
        request = LLMRequest(
            tier=self._tier,  # type: ignore[arg-type]
            messages=(*messages, Message(role="user", content=self._extract)),
            purpose="subagent_report",
        )
        try:
            result = await asyncio.wait_for(
                self._gateway.invoke_structured(request, RawSubagentReport),
                max(budget.remaining_wall_sec, 0.1),
            )
        except Exception:
            warnings.append("收尾提炼失败")
            return RawSubagentReport()
        budget.add_tokens(result.usage.total_tokens)
        if isinstance(result, Degraded):
            warnings.append("提炼结果无法解析，已丢弃")
            return RawSubagentReport()
        return result.value

    async def _say(self, name: str, data: Mapping[str, object]) -> None:
        if self._emit is None:
            return
        try:
            await self._emit(name, data)
        except Exception:
            logger.warning("子智能体事件发送失败（已忽略）")


def _tool_message(call: ToolCall, text: str) -> Message:
    return Message(role="tool", content=text, tool_call_id=call.id, name=call.name)


def _digest(failed: bool, output: str) -> str:
    """事件里只放内容无关的摘要（成败与规模）；失败时带一小段原因，成功的输出一个字都不带。"""
    size = f"{len(output)} 字符"
    return f"失败，{size}：{output[:120]}" if failed else f"成功，{size}"
