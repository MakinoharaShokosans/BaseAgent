"""护栏纯函数集合：权限判定、循环检测、金丝雀、注入扫描、信封。没有预算类护栏（docs/12_decisions/0006）。设计文档：docs/05_security/"""

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
from agent_runtime.guardrails.loops import (
    LoopLimits,
    build_error_notice,
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
    "InjectionHit",
    "LoopLimits",
    "PermissionDecision",
    "PermissionPolicy",
    "action_signature",
    "build_error_notice",
    "check_permission",
    "compute_fingerprint",
    "defang",
    "detect_canary_leak",
    "escape_attr",
    "is_alternating",
    "is_fingerprint_loop",
    "observation_envelope",
    "register_fingerprints",
    "scan_injection",
    "update_consecutive_errors",
    "wrap",
]
