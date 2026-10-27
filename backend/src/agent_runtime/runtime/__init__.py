"""运行时门面：把循环、检查点、审批装配成对外的任务入口。

提供新任务（``create_task`` / ``run_task``）、续跑（``resume_task``）、以及带并发名额和
状态映射的 ``TaskRunner``。它在 ``loop``、``checkpoint``、``approval`` 之上，三者彼此独立，
只由这一层组合。设计文档：docs/02_runtime/、docs/01_architecture/02_task_lifecycle.md
"""

from agent_runtime.runtime.active import ActiveTasks
from agent_runtime.runtime.assembly import (
    APPROVAL_HOOKS,
    assembler_from_settings,
    budget_limits_from_settings,
    build_prompt_assets,
    compaction_config_from_settings,
    file_limits_from_settings,
    fold_policy_from_settings,
    loop_limits_from_settings,
    max_concurrent_tasks,
    memory_flow_from_settings,
    observation_limits_from_settings,
    permission_policy_from_settings,
    progress_hint_from_settings,
    resolve_permission_level,
    rules_from_settings,
    skill_limits_from_settings,
    skill_roots_from_settings,
)
from agent_runtime.runtime.errors import (
    ApprovalRequiredFirstError,
    FullPermissionsNotConfirmedError,
    TaskAlreadyExistsError,
    TaskAlreadyRunningError,
    TaskNotFoundError,
    TaskNotResumableError,
    TaskQueueFullError,
)
from agent_runtime.runtime.events import (
    LifecycleEvents,
    LlmCallEvents,
    TaskEvents,
    approval_payload,
    delivery_of,
)
from agent_runtime.runtime.resume import ResumeDeps, prepare_resume, resume_task
from agent_runtime.runtime.run import RunnerDeps, TaskRequest, create_task, run_task
from agent_runtime.runtime.runner import (
    TaskObserver,
    TaskRunner,
    TaskSnapshot,
    TaskStatus,
)

__all__ = [
    "APPROVAL_HOOKS",
    "ActiveTasks",
    "ApprovalRequiredFirstError",
    "FullPermissionsNotConfirmedError",
    "LifecycleEvents",
    "LlmCallEvents",
    "ResumeDeps",
    "RunnerDeps",
    "TaskAlreadyExistsError",
    "TaskAlreadyRunningError",
    "TaskEvents",
    "TaskNotFoundError",
    "TaskNotResumableError",
    "TaskObserver",
    "TaskQueueFullError",
    "TaskRequest",
    "TaskRunner",
    "TaskSnapshot",
    "TaskStatus",
    "approval_payload",
    "assembler_from_settings",
    "budget_limits_from_settings",
    "build_prompt_assets",
    "compaction_config_from_settings",
    "create_task",
    "delivery_of",
    "file_limits_from_settings",
    "fold_policy_from_settings",
    "loop_limits_from_settings",
    "max_concurrent_tasks",
    "memory_flow_from_settings",
    "observation_limits_from_settings",
    "permission_policy_from_settings",
    "prepare_resume",
    "progress_hint_from_settings",
    "resolve_permission_level",
    "resume_task",
    "rules_from_settings",
    "run_task",
    "skill_limits_from_settings",
    "skill_roots_from_settings",
]
