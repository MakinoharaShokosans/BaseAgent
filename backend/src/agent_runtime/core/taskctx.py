"""当前任务的上下文变量：让深处的旁路（网关的调用记录、日志）不必逐层传 task_id。

由任务运行器在任务开始时设置；asyncio 的子任务会继承它。
"""

from __future__ import annotations

from contextvars import ContextVar

current_task_id: ContextVar[str | None] = ContextVar("baseagent_current_task_id", default=None)
