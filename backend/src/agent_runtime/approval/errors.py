"""审批决策的绑定错误。任一项失败都拒绝，任务保持挂起。"""

from __future__ import annotations

from agent_runtime.core import CoreError


class ApprovalBindingError(CoreError):
    """决策不能绑定到当前的待审批请求。"""


class NotWaitingForApprovalError(ApprovalBindingError):
    """任务不在等待审批。重复提交同一决策时第二次落在这里。"""


class ApprovalMismatchError(ApprovalBindingError):
    """决策或重新判定的结果与待审批请求对不上。"""


class InvalidDecisionError(ApprovalBindingError, ValueError):
    """决策本身格式不合法。"""
