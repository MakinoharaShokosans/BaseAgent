"""纯契约层：消息、状态、编解码、错误、工具协议、信封。零 I/O，不依赖项目内其他部分。

设计文档：docs/02_runtime/01_state_model.md
"""

from agent_runtime.core.codec import (
    MIGRATIONS,
    MINIMUM_SCHEMA_VERSION,
    dumps_state,
    loads_state,
    state_from_dict,
    state_to_dict,
)
from agent_runtime.core.errors import (
    AtomicPairError,
    CoreError,
    InvalidMessageError,
    InvalidStateError,
    InvalidUpdateError,
    StateDecodeError,
)
from agent_runtime.core.messages import (
    Message,
    Role,
    ToolCall,
    assert_atomic_pairs,
    unanswered_calls,
)
from agent_runtime.core.state import (
    CURRENT_SCHEMA_VERSION,
    MAX_FACT_CHARS,
    MAX_FACTS,
    MAX_FAILED_ATTEMPTS,
    MAX_PLAN_ITEMS,
    AgentState,
    ApprovalDecision,
    FailedAttempt,
    PendingApproval,
    PermissionLevel,
    PlanItem,
    Verification,
    apply_update,
    check_invariants,
    new_state,
)
from agent_runtime.core.suspend import ApprovalRequired
from agent_runtime.core.taskctx import current_task_id
from agent_runtime.core.tools import AgentTool, ToolResult, ToolSchema, Trust

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MAX_FACTS",
    "MAX_FACT_CHARS",
    "MAX_FAILED_ATTEMPTS",
    "MAX_PLAN_ITEMS",
    "MIGRATIONS",
    "MINIMUM_SCHEMA_VERSION",
    "AgentState",
    "AgentTool",
    "ApprovalDecision",
    "ApprovalRequired",
    "AtomicPairError",
    "CoreError",
    "FailedAttempt",
    "InvalidMessageError",
    "InvalidStateError",
    "InvalidUpdateError",
    "Message",
    "PendingApproval",
    "PermissionLevel",
    "PlanItem",
    "Role",
    "StateDecodeError",
    "ToolCall",
    "ToolResult",
    "ToolSchema",
    "Trust",
    "Verification",
    "apply_update",
    "assert_atomic_pairs",
    "check_invariants",
    "current_task_id",
    "dumps_state",
    "loads_state",
    "new_state",
    "state_from_dict",
    "state_to_dict",
    "unanswered_calls",
]
