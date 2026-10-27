"""把循环与任务生命周期翻译成对外的事件。设计文档：docs/09_api/02_events_contract.md §3-6。

``TaskEvents`` 满足循环的 ``EventSink``（只在检查点提交之后被调用）和工具包装层的 ``Emit``。
工具是否成功这类“提交前才知道”的信息，由包装层经 ``tool.observed`` 递过来，这里只记下，
等到该回合提交后再随 ``tool.result`` 一起发出，所以事件永远不会比可恢复的进度更远。

回合事件（``model.reply``、``plan.updated``、``turn.finished``）是目标契约；迁移期间同时发出旧的
``plan`` 与 ``node.finished``，前端改完后再去掉（见 docs/09_api/02_events_contract.md §4.1）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Final

from agent_runtime.core import (
    AgentState,
    Message,
    PendingApproval,
    current_task_id,
)
from agent_runtime.llm import CallRecord
from agent_runtime.loop.tools import REJECTED_TEMPLATE
from agent_runtime.observability import EventHub, Redactor, clip
from agent_runtime.runtime.runner import TaskSnapshot

_REPLY_CLIP: Final = 2000
_REASONING_CLIP: Final = 500


class TaskEvents:
    """一个任务一个。``canary`` 与其他需要精确抹掉的字符串在构造时给出。"""

    def __init__(
        self,
        hub: EventHub,
        task_id: str,
        redactor: Redactor,
        *,
        canary: str = "",
        tool_result_clip: int = 500,
        turn_offset: int = 0,
        usage: Callable[[], int] = lambda: 0,
    ) -> None:
        self._hub = hub
        self._task_id = task_id
        # 金丝雀在这里并入：事件的出口不依赖装配层记得传
        self._redactor = redactor.with_secrets([canary] if canary else [])
        self._result_clip = tool_result_clip
        self._turn = turn_offset
        self._usage = usage
        self._observed: dict[str, Mapping[str, object]] = {}

    # ------------------------------------------------------------------ 工具包装层的 Emit

    async def emit(self, name: str, data: Mapping[str, object]) -> None:
        if name == "tool.observed":
            self._observed[str(data.get("call_id", ""))] = dict(data)
            return
        await self._hub.emit(self._task_id, name, self._scrub(data))

    # ------------------------------------------------------------------ EventSink

    async def model_replied(self, message: Message, state: AgentState) -> None:
        turn = self._turn + 1
        payload: dict[str, object] = {
            "turn": turn,
            "content": clip(message.content, _REPLY_CLIP),
            "tool_calls": [{"id": c.id, "name": c.name} for c in message.tool_calls],
        }
        if message.reasoning:
            payload["reasoning"] = clip(message.reasoning, _REASONING_CLIP)
        await self._hub.emit(self._task_id, "model.reply", self._scrub(payload))
        for call in message.tool_calls:
            args, _ = self._redactor.args(call.args)
            await self._hub.emit(
                self._task_id,
                "tool.call",
                {"tool": call.name, "args": args, "step": turn, "call_id": call.id},
            )
        if not message.tool_calls:
            self._turn = turn
            await self._legacy_node(
                turn, "agent", "reply", message.content, content=message.content
            )

    async def turn_finished(self, update: Mapping[str, object], state: AgentState) -> None:
        turn = self._turn + 1
        self._turn = turn
        new = [m for m in _list_of(update.get("messages")) if m.role == "tool"]
        failed = 0
        for message in new:
            failed += not await self._tool_result(message, turn)
        if "plan" in update:
            await self._plan_updated(state)
        await self._hub.emit(
            self._task_id,
            "turn.finished",
            {
                "turn": turn,
                "tool_count": len(new),
                "failed_count": failed,
                "tokens": self._usage(),
            },
        )
        await self._legacy_node(turn, "tool_runner", "executed", new[-1].content if new else "")

    async def approval_requested(self, request: PendingApproval, state: AgentState) -> None:
        await self._hub.emit(
            self._task_id, "task.waiting_for_approval", approval_payload(request, self._redactor)
        )

    # ------------------------------------------------------------------ 内部

    async def _plan_updated(self, state: AgentState) -> None:
        plan = [{"id": i.id, "title": i.title, "status": i.status} for i in state.plan]
        await self._hub.emit(self._task_id, "plan.updated", self._scrub({"plan": plan}))
        await self._hub.emit(
            self._task_id,
            "plan",
            self._scrub(
                {
                    "thought": "",
                    "directive": "",
                    "milestones": plan_as_milestones(state),
                    "step": self._turn,
                }
            ),
        )

    async def _legacy_node(
        self, turn: int, node: str, decision: str, text: str, **extra: object
    ) -> None:
        payload: dict[str, object] = {
            "node": node,
            "stage": turn,
            "input_summary": "任务目标与当前进展" if node == "agent" else "待执行的工具调用",
            "output_summary": self._redactor.text(clip(text, 300)),
            "decision": decision,
            "step_count": turn,
            "total_tokens": self._usage(),
            "should_terminate": False,
            **extra,
        }
        await self._hub.emit(self._task_id, "node.finished", self._scrub(payload))

    async def _tool_result(self, message: Message, turn: int) -> bool:
        observed = self._observed.pop(message.tool_call_id or "", {})
        ok = bool(observed.get("ok", True))
        await self._hub.emit(
            self._task_id,
            "tool.result",
            {
                "tool": message.name or "",
                "result": self._redactor.text(message.content)[: self._result_clip],
                "step": turn,
                "ok": ok,
                "artifact_id": str(observed.get("artifact_id", "")),
                "call_id": message.tool_call_id or "",
            },
        )
        return ok

    def _scrub(self, data: Mapping[str, object]) -> dict[str, object]:
        cleaned = self._redactor.value(data)
        assert isinstance(cleaned, dict)
        return {str(k): v for k, v in cleaned.items()}  # pyright: ignore[reportUnknownVariableType]


def approval_payload(request: PendingApproval, redactor: Redactor) -> dict[str, object]:
    """审批请求的对外形状：``task.waiting_for_approval`` 与 ``TaskOut.approval_request`` 同构。"""
    return {
        "approval_id": request.approval_id,
        "call_ids": list(request.call_ids),
        "required_level": request.required_level,
        "current_level": request.current_level,
        "action_type": request.action_type,
        "command": redactor.text(request.command),  # 不截断：用户要看清整条命令
        "reason": redactor.text(request.reason),
        "escalation_count": request.escalation_count,
        "related_actions": [redactor.text(a) for a in request.related_actions],
    }


# ==============================================================================
# 模型调用
# ==============================================================================


class LlmCallEvents:
    """网关的调用记录（同步回调）到 ``llm.call`` 事件。

    记录不含消息正文，所以事件只带摘要，也就不会把提示词或观察值带进事件流。回调是同步的，
    发事件是异步的：这里排队后台发出，``drain`` 在任务收尾前等它们发完，保证
    ``task.finished`` 仍是最后一个事件。
    """

    def __init__(self, hub: EventHub) -> None:
        self._hub = hub
        self._pending: set[asyncio.Task[object]] = set()
        self._tokens: dict[str, int] = {}

    def tokens_of(self, task_id: str) -> int:
        """本进程内该任务的累计 Token（统计值，不对应任何上限）。"""
        return self._tokens.get(task_id, 0)

    def __call__(self, record: CallRecord) -> None:
        task_id = current_task_id.get()
        if task_id is None:
            return
        tokens = record.usage.total_tokens if record.usage is not None else 0
        self._tokens[task_id] = self._tokens.get(task_id, 0) + tokens
        payload: dict[str, object] = {
            "node": record.purpose,
            "tier": record.tier,
            "endpoint": record.endpoint,
            "model": record.model,
            "attempts": record.attempt,
            "outcome": record.outcome,
            "duration_ms": record.duration_ms,
            "tokens": tokens,
            "finish_reason": record.finish_reason or "",
        }
        usage = record.usage
        if usage is not None:
            # 缓存命中率与各阶段的开销都从这里算出来；服务商没给的字段不写
            payload["prompt_tokens"] = usage.prompt_tokens
            payload["completion_tokens"] = usage.completion_tokens
            payload["reasoning_tokens"] = usage.reasoning_tokens
            if usage.cache_hit_tokens is not None:
                payload["cache_hit_tokens"] = usage.cache_hit_tokens
            if usage.cache_miss_tokens is not None:
                payload["cache_miss_tokens"] = usage.cache_miss_tokens
        if record.error_class:
            payload["error"] = record.error_class
        try:
            task = asyncio.get_running_loop().create_task(
                self._hub.emit(task_id, "llm.call", payload)
            )
        except RuntimeError:  # 没有运行中的事件循环：旁路，放弃
            return
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        while self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)


# ==============================================================================
# 生命周期
# ==============================================================================

StateLoader = Callable[[str], Awaitable[AgentState | None]]


class LifecycleEvents:
    """``TaskObserver``：``task.started`` / ``task.error`` / ``task.finished``。"""

    def __init__(
        self,
        hub: EventHub,
        redactor: Redactor,
        load_state: StateLoader,
        *,
        drain: Callable[[], Awaitable[None]] | None = None,
        usage: Callable[[str], int] = lambda task_id: 0,
    ) -> None:
        self._hub = hub
        self._redactor = redactor
        self._load = load_state
        self._drain = drain
        self._usage = usage

    async def task_started(self, task_id: str, state: AgentState) -> None:
        await self._hub.emit(
            task_id, "task.started", {"task_goal": self._redactor.text(state.task_goal)}
        )

    async def task_finished(self, snapshot: TaskSnapshot) -> None:
        if self._drain is not None:
            await self._drain()  # 还在路上的 llm.call 先发完，task.finished 才是最后一个
        state = snapshot.state or await self._load(snapshot.task_id)
        redactor = self._redactor.with_secrets([state.canary_token] if state else [])
        if snapshot.status == "failed":
            await self._hub.emit(
                snapshot.task_id,
                "task.error",
                {"code": "TASK_FAILED", "message": redactor.text(snapshot.reason)},
            )
        await self._hub.emit(
            snapshot.task_id,
            "task.finished",
            {
                "status": snapshot.status,
                "termination_reason": redactor.text(snapshot.reason),
                "step_count": turns_of(state),
                "turns": turns_of(state),
                "total_tokens": self._usage(snapshot.task_id),
                "delivery": redactor.text(delivery_of(state, snapshot.status, snapshot.reason))
                if state
                else "",
            },
        )


_SECURITY_PREFIX = "[SECURITY]"
_EDIT_TOOLS = frozenset({"write_file", "edit_file"})
_PARTIAL_LIST_LIMIT = 12
#: 工具包装层合成的“没有执行”的观察值前缀（被人工拒绝、被循环护栏拦截、参数不可用）
_NOT_RUN_PREFIXES = (REJECTED_TEMPLATE.split("{")[0], "该调用与最近的调用重复", "未执行")


def turns_of(state: AgentState | None) -> int:
    """回合数：模型回复的条数（统计值）。"""
    return sum(1 for m in state.messages if m.role == "assistant") if state else 0


def plan_as_milestones(state: AgentState) -> list[dict[str, object]]:
    """旧事件契约里的里程碑形状。"""
    return [
        {"id": i.id, "title": i.title, "description": "", "status": i.status} for i in state.plan
    ]


def delivery_of(state: AgentState | None, status: str = "succeeded", reason: str = "") -> str:
    """面向用户的交付：正常结束取最后一条不带工具调用的 assistant 消息；被终止或取消的任务
    带着已完成的部分交付（最后一条 assistant 消息多半只是一条内部指令，不是交付）。"""
    if state is None:
        return ""
    if status in ("terminated", "cancelled"):
        return partial_delivery(state, reason)
    for message in reversed(state.messages):
        if message.role == "assistant" and message.content and not message.tool_calls:
            return message.content
    return ""


def partial_delivery(state: AgentState, reason: str = "") -> str:
    """任务被终止或取消时的交付：全部由代码根据状态生成，不额外调用模型。
    安全类终止不展开任何内容。"""
    reason = reason or "任务被终止"
    if reason.startswith(_SECURITY_PREFIX):
        return "任务因安全原因被终止，没有交付内容。"
    lines = [f"任务没有完成：{reason}"]
    done = [i.title for i in state.plan if i.status == "completed"]
    todo = [i.title for i in state.plan if i.status != "completed"]
    if done:
        lines += ["", "已完成：", *(f"- {t}" for t in done[:_PARTIAL_LIST_LIMIT])]
    if todo:
        lines += ["", "未完成：", *(f"- {t}" for t in todo[:_PARTIAL_LIST_LIMIT])]
    touched = _touched_files(state)
    if touched:
        lines += ["", "已修改的文件：" + "、".join(touched[:_PARTIAL_LIST_LIMIT])]
    if state.facts:
        lines += ["", "已确认的事实：", *(f"- {f}" for f in state.facts[:_PARTIAL_LIST_LIMIT])]
    lines += ["", "可以在同一会话里继续提问，我会从这些成果接着做。"]
    return "\n".join(lines)


def _touched_files(state: AgentState) -> list[str]:
    """改过的文件：只算真正执行了的调用。被拒绝、被拦截、参数不可用的调用没有动过文件。"""
    not_run = {
        m.tool_call_id: m.content.startswith(_NOT_RUN_PREFIXES)
        for m in state.messages
        if m.role == "tool" and m.tool_call_id
    }
    seen: dict[str, None] = {}
    for message in state.messages:
        for call in message.tool_calls:
            path = call.args.get("path")
            ran = not not_run.get(call.id, False)
            if call.name in _EDIT_TOOLS and isinstance(path, str) and path and ran:
                seen[path] = None
    return list(seen)


# ==============================================================================
# 纯辅助
# ==============================================================================


def _list_of(raw: object) -> list[Message]:
    return [m for m in raw if isinstance(m, Message)] if isinstance(raw, list | tuple) else []
