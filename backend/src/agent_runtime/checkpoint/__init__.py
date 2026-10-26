"""检查点存储、崩溃恢复与启动扫描。

状态的编解码在 ``agent_runtime.core``。设计文档：docs/02_runtime/03_checkpoint_and_resume.md
"""

from agent_runtime.checkpoint.errors import CheckpointCorruptError, CheckpointError
from agent_runtime.checkpoint.recovery import (
    CANCELLED_REASON,
    NOT_EXECUTED,
    UNKNOWN_RESULT,
    RecoveryAction,
    RecoveryResult,
    mark_cancelled,
    recover_interrupted_dispatch,
)
from agent_runtime.checkpoint.scan import RecoveryReport, scan_for_recovery
from agent_runtime.checkpoint.store import CheckpointStore, UnfinishedTask

__all__ = [
    "CANCELLED_REASON",
    "NOT_EXECUTED",
    "UNKNOWN_RESULT",
    "CheckpointCorruptError",
    "CheckpointError",
    "CheckpointStore",
    "RecoveryAction",
    "RecoveryReport",
    "RecoveryResult",
    "UnfinishedTask",
    "mark_cancelled",
    "recover_interrupted_dispatch",
    "scan_for_recovery",
]
