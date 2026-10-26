"""任务级运行时：循环所需的依赖。阶段函数自己的依赖（网关、工具、守卫等）通过闭包注入。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol, get_args

from agent_runtime.core import AgentState, PendingApproval, Phase

#: 阶段函数：状态进（只读），增量字典出。规则见 docs/02_runtime/01_state_model.md §3
Stage = Callable[[AgentState], Awaitable[Mapping[str, object]]]

STAGE_NAMES: Final[tuple[str, ...]] = get_args(Phase)


class CommitSink(Protocol):
    """检查点存储对循环的接口。``agent_runtime.checkpoint.CheckpointStore`` 满足它。"""

    async def commit(self, state: AgentState) -> int: ...


class EventSink(Protocol):
    """循环向外发出的事件。只在对应的检查点提交成功之后调用。"""

    async def stage_committed(
        self, phase: Phase, update: Mapping[str, object], state: AgentState
    ) -> None: ...

    async def approval_requested(self, request: PendingApproval, state: AgentState) -> None: ...


@dataclass(frozen=True, slots=True)
class TaskRuntime:
    stages: Mapping[Phase, Stage]
    checkpoints: CommitSink
    events: EventSink

    def __post_init__(self) -> None:
        missing = [name for name in STAGE_NAMES if name not in self.stages]
        if missing:
            raise ValueError(f"缺少阶段函数：{missing}")
