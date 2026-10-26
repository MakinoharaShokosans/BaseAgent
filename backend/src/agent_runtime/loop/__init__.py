"""主循环：阶段、路由表、调度。阶段是“状态进、增量出”的函数。

设计文档：docs/02_runtime/02_main_loop.md
"""

from agent_runtime.loop.drive import OutcomeStatus, TaskOutcome, drive
from agent_runtime.loop.routes import ALLOWED_TRANSITIONS, ROUTES, Router, next_phase
from agent_runtime.loop.runtime import CommitSink, EventSink, Stage, TaskRuntime

__all__ = [
    "ALLOWED_TRANSITIONS",
    "ROUTES",
    "CommitSink",
    "EventSink",
    "OutcomeStatus",
    "Router",
    "Stage",
    "TaskOutcome",
    "TaskRuntime",
    "drive",
    "next_phase",
]
