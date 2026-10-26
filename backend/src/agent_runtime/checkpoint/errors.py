"""检查点存储的错误类型。"""

from __future__ import annotations

from agent_runtime.core import CoreError


class CheckpointError(CoreError):
    """存储不可用或写入失败。提交失败视为任务失败，不允许带着未持久化的进度继续跑。"""


class CheckpointCorruptError(CheckpointError):
    """库里的某一行无法还原为合法状态（被篡改、被截断或格式不符）。"""
