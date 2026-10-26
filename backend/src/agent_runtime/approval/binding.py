"""审批请求的标识、决策的解析与绑定。设计文档：docs/02_runtime/04_approval.md §3-5。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

from pydantic import ValidationError

from agent_runtime.approval.errors import (
    ApprovalBindingError,
    ApprovalMismatchError,
    InvalidDecisionError,
    NotWaitingForApprovalError,
)
from agent_runtime.core import AgentState, ApprovalDecision, apply_update


def compute_approval_id(task_id: str, step_count: int, call_ids: Iterable[str]) -> str:
    """确定性的请求标识：同一任务、同一步、同一批越级调用永远得到同一个。

    崩溃后重新进入同一阶段会生成同一个请求，前端已展示的卡片仍然有效；决策因此能精确绑定到
    这一批调用。输入用 JSON 编码，字段边界无歧义。
    """
    material = json.dumps([task_id, step_count, sorted(call_ids)], ensure_ascii=False)
    return "appr_" + hashlib.sha256(material.encode()).hexdigest()[:16]


def parse_decision(raw: object) -> ApprovalDecision:
    """把外部给来的决策严格解析出来。``scope`` 缺省为 ``once``；``approved`` 必须是真布尔值。"""
    if not isinstance(raw, Mapping):
        raise InvalidDecisionError("决策必须是对象")
    try:
        decision = ApprovalDecision.model_validate_json(json.dumps(raw), strict=True)
    except (ValidationError, TypeError, ValueError) as exc:
        raise InvalidDecisionError(f"决策不合法：{exc}") from exc
    if not decision.approval_id:
        raise InvalidDecisionError("决策的 approval_id 不能为空")
    return decision


def bind_decision(state: AgentState, decision: ApprovalDecision) -> AgentState:
    """把决策写入状态，等重新进入的 ``tool_runner`` 消费。纯函数。

    请求（``pending_approval``）保留，由 ``tool_runner`` 消费决策时一并清空。
    """
    pending = state.pending_approval
    if pending is None:
        raise NotWaitingForApprovalError("任务当前不在等待审批")
    if decision.approval_id != pending.approval_id:
        raise ApprovalMismatchError(
            f"决策的 approval_id（{decision.approval_id}）"
            f"与待审批请求（{pending.approval_id}）不一致"
        )
    if state.approval_decision is not None:
        raise ApprovalBindingError("已有一个尚未被消费的决策")
    return apply_update(state, {"approval_decision": decision})


def check_binding(state: AgentState, escalation_call_ids: Iterable[str]) -> None:
    """``tool_runner`` 重新做纯判定之后，核对决策确实对应这一批越级调用。

    ``approval_id`` 必须能由任务、步数与调用集合重新算出，所以被改写的请求不被信任；
    重启后重新判定出的越级集合与当初请求不同，也不能套用旧决策。
    """
    pending, decision = state.pending_approval, state.approval_decision
    if pending is None:
        raise NotWaitingForApprovalError("任务当前不在等待审批")
    if decision is None:
        raise ApprovalBindingError("没有可消费的决策")

    escalations = tuple(escalation_call_ids)
    if set(pending.call_ids) != set(escalations) or len(escalations) != len(set(escalations)):
        raise ApprovalMismatchError(
            f"重新判定的越级调用 {sorted(escalations)} 与请求的 call_ids "
            f"{sorted(pending.call_ids)} 不一致"
        )
    expected = compute_approval_id(state.task_id, state.step_count, escalations)
    if pending.approval_id != expected:
        raise ApprovalMismatchError("待审批请求的 approval_id 无法由当前状态重新算出")
    if decision.approval_id != pending.approval_id:
        raise ApprovalMismatchError("决策的 approval_id 与待审批请求不一致")
