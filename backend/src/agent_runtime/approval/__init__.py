"""权限闸门、审批协议、会话白名单。

目前已实现决策的解析与绑定校验；闸门与白名单随 ``tool_runner`` 一起做。
设计文档：docs/02_runtime/04_approval.md
"""

from agent_runtime.approval.binding import (
    bind_decision,
    check_binding,
    compute_approval_id,
    parse_decision,
)
from agent_runtime.approval.errors import (
    ApprovalBindingError,
    ApprovalMismatchError,
    InvalidDecisionError,
    NotWaitingForApprovalError,
)

__all__ = [
    "ApprovalBindingError",
    "ApprovalMismatchError",
    "InvalidDecisionError",
    "NotWaitingForApprovalError",
    "bind_decision",
    "check_binding",
    "compute_approval_id",
    "parse_decision",
]
