"""护栏纯函数集合：预算、账本、权限判定、循环检测、金丝雀、注入扫描、授权窗口。

目前已实现物理预算、金丝雀、权限判定、重复指纹与连续错误、注入样态扫描；其余随对应阶段加入。设计文档：docs/05_security/
"""

from agent_runtime.guardrails.budget import (
    BudgetLimits,
    BudgetVerdict,
    Dimension,
    PhysicalBudgetGuard,
    Usage,
    evaluate_budget,
)
from agent_runtime.guardrails.canary import detect_canary_leak
from agent_runtime.guardrails.envelope import (
    ENVELOPE_TAGS,
    UNTRUSTED_WARNING,
    defang,
    escape_attr,
    observation_envelope,
    wrap,
)
from agent_runtime.guardrails.injection import MAX_HITS, InjectionHit, scan_injection
from agent_runtime.guardrails.ledger import BudgetLedger, ChildBudget, Reservation
from agent_runtime.guardrails.loops import (
    LoopLimits,
    build_replan_notice,
    compute_fingerprint,
    is_alternating,
    is_fingerprint_loop,
    register_fingerprints,
    update_consecutive_errors,
)
from agent_runtime.guardrails.permissions import (
    PermissionDecision,
    PermissionPolicy,
    action_signature,
    check_permission,
)

__all__ = [
    "ENVELOPE_TAGS",
    "MAX_HITS",
    "UNTRUSTED_WARNING",
    "BudgetLedger",
    "BudgetLimits",
    "BudgetVerdict",
    "ChildBudget",
    "Dimension",
    "InjectionHit",
    "LoopLimits",
    "PermissionDecision",
    "PermissionPolicy",
    "PhysicalBudgetGuard",
    "Reservation",
    "Usage",
    "action_signature",
    "build_replan_notice",
    "check_permission",
    "compute_fingerprint",
    "defang",
    "detect_canary_leak",
    "escape_attr",
    "evaluate_budget",
    "is_alternating",
    "is_fingerprint_loop",
    "observation_envelope",
    "register_fingerprints",
    "scan_injection",
    "update_consecutive_errors",
    "wrap",
]
