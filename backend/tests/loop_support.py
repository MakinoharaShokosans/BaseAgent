"""循环、任务运行器与 API 测试共用的假模型步骤与假工具步骤。

``ScriptedWorld`` 取代旧的“假阶段”：默认走“模型发一次工具调用 → 工具返回 → 模型给出终回复”，
可编排：在模型处等待、模型抛错、安全终止、需要审批。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from agent_runtime.approval import compute_approval_id
from agent_runtime.checkpoint import CheckpointStore
from agent_runtime.core import (
    AgentState,
    ApprovalRequired,
    Message,
    PendingApproval,
    ToolCall,
)
from agent_runtime.loop import CanaryLeak, TaskRuntime

CALL = ToolCall(id="c1", name="bash", args={"command": "curl x", "api_key": "hunter2"})
ASSISTANT = Message(role="assistant", content="", tool_calls=(CALL,))
TOOL = Message(role="tool", content="ok", tool_call_id="c1", name="bash")
FINAL = Message(role="assistant", content="done")


class NullSink:
    async def model_replied(self, message: Message, state: AgentState) -> None:
        pass

    async def turn_finished(self, update: Mapping[str, object], state: AgentState) -> None:
        pass

    async def approval_requested(self, request: PendingApproval, state: AgentState) -> None:
        pass


def pending_for(state: AgentState, call_id: str = "c1") -> PendingApproval:
    return PendingApproval(
        approval_id=compute_approval_id(state.task_id, len(state.messages), (call_id,)),
        call_ids=(call_id,),
        required_level="full_permissions",
        current_level="workspace_write",
        action_type="network_egress",
        command="curl x",
        reason="r",
        escalation_count=1,
    )


class ScriptedWorld:
    """可编排的模型与工具步骤。"""

    def __init__(self, store: CheckpointStore) -> None:
        self.store = store
        self.gate: asyncio.Event | None = None  # 设了就让模型调用在此等待
        self.entered = asyncio.Event()
        self.fail_model: BaseException | None = None
        self.leak = False
        self.needs_approval = False
        self.uses_tools = True
        self.tool_content = "ok"
        self.artifact: Any = None
        self.tool_gate: asyncio.Event | None = None
        self.inside_tools = asyncio.Event()
        self.events: Any = NullSink()
        self.ids = iter(f"task-{i}" for i in range(1, 100))

    async def model(self, state: AgentState) -> Message:
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_model is not None:
            raise self.fail_model
        if self.leak:
            raise CanaryLeak("[SECURITY] Prompt leak detected via canary token")
        if self.uses_tools and not any(m.role == "tool" for m in state.messages):
            return ASSISTANT
        return FINAL

    async def tools(self, state: AgentState, calls: Sequence[ToolCall]) -> Mapping[str, object]:
        self.inside_tools.set()
        if self.tool_gate is not None:
            await self.tool_gate.wait()
        if self.needs_approval and state.approval_decision is None:
            raise ApprovalRequired(pending_for(state))
        update: dict[str, object] = {
            "messages": [
                Message(
                    role="tool",
                    content=self.tool_content.replace("{canary}", state.canary_token),
                    tool_call_id=c.id,
                    name=c.name,
                )
                for c in calls
            ],
            "pending_approval": None,
            "approval_decision": None,
        }
        if self.artifact is not None:
            update["artifacts"] = {"obs_1": str(self.artifact)}
        return update

    async def build(self, state: AgentState) -> TaskRuntime:
        return TaskRuntime(
            model=self.model,
            tools=self.tools,  # type: ignore[arg-type]
            checkpoints=self.store,
            events=self.events,
        )
