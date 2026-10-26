"""挂起信号。"""

from __future__ import annotations

from agent_runtime.core.errors import CoreError
from agent_runtime.core.state import PendingApproval


class ApprovalRequired(CoreError):
    """阶段函数请求挂起等待人工审批。

    不携带增量：循环不会应用该阶段的任何局部结果，所以阶段在抛出之前不得产生外部副作用。
    设计文档：docs/02_runtime/04_approval.md §2。
    """

    def __init__(self, payload: PendingApproval) -> None:
        super().__init__(f"需要人工审批：{payload.approval_id}")
        self.payload = payload
