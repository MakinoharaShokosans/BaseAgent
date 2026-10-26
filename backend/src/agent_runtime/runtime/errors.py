"""任务续跑的错误。"""

from __future__ import annotations

from agent_runtime.core import CoreError


class TaskNotFoundError(CoreError):
    """没有这个任务的检查点。"""


class TaskNotResumableError(CoreError):
    """任务处于不可续跑的终态（已成功完成）。"""


class ApprovalRequiredFirstError(CoreError):
    """任务挂起在审批上，续跑必须带决策。"""


class TaskAlreadyRunningError(CoreError):
    """同一任务已有运行实例，续跑直接拒绝，不排队。"""


class TaskQueueFullError(CoreError):
    """并发名额已满（对外映射为 429 TASK_QUEUE_FULL）。挂起等待审批的任务不占名额。"""


class TaskAlreadyExistsError(CoreError):
    """该 task_id 已有检查点；提交不能覆盖它。"""


class FullPermissionsNotConfirmedError(CoreError):
    """请求 full_permissions 但没有显式确认。"""
