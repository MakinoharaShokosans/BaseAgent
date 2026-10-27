"""AgentState 与增量更新语义。

设计文档：docs/02_runtime/01_state_model.md §2-4。
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict

from agent_runtime.core.errors import InvalidStateError, InvalidUpdateError
from agent_runtime.core.messages import Message, assert_atomic_pairs

PermissionLevel = Literal["read_only", "workspace_write", "full_permissions"]
Phase = Literal["planner", "budget_guard", "executor", "tool_runner", "evaluator"]
#: 终止哨兵：路由函数返回它表示不再有下一个阶段
DONE: Final = "done"
NextPhase = Literal["planner", "budget_guard", "executor", "tool_runner", "evaluator", "done"]

PERMISSION_LEVELS: tuple[str, ...] = get_args(PermissionLevel)
PHASES: tuple[str, ...] = (*get_args(Phase), DONE)

#: 最近一次修改文件之后的验证情况：none 没改过；unchecked 改了还没运行过命令；
#: failed / passed 改后最近一条命令失败 / 成功
Verification = Literal["none", "unchecked", "failed", "passed"]
VERIFICATIONS: tuple[str, ...] = get_args(Verification)

CURRENT_SCHEMA_VERSION: Final = 2


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Milestone(_Frozen):
    id: int
    title: str
    description: str = ""
    status: Literal["pending", "in_progress", "completed", "failed"] = "pending"


class FailedAttempt(_Frozen):
    action: str
    failure_reason: str
    conclusion: str


class PendingApproval(_Frozen):
    """待审批请求。``approval_id`` 由确定性摘要生成，见 docs/02_runtime/04_approval.md §3。"""

    approval_id: str
    call_ids: tuple[str, ...]
    required_level: PermissionLevel
    current_level: PermissionLevel
    action_type: str
    command: str
    reason: str
    escalation_count: int
    related_actions: tuple[str, ...] = ()


class ApprovalDecision(_Frozen):
    approval_id: str
    approved: bool
    scope: Literal["once", "always"] = "once"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class AgentState:
    """一次任务的全部状态。不可变：只通过 :func:`apply_update` 得到新对象。

    序列类字段都是元组；``artifacts`` 是字典，约定不得原地修改（``apply_update`` 总是整体替换）。
    """

    # 归属：全生命周期不变
    workspace_id: str
    workspace_path: str
    session_id: str
    task_id: str
    task_goal: str
    permission_level: PermissionLevel
    canary_token: str
    # 规划
    milestones: tuple[Milestone, ...] = ()
    current_milestone_idx: int = 0
    # 上下文
    messages: tuple[Message, ...] = ()
    # 记忆
    rolling_summary: str = ""
    confirmed_facts: tuple[str, ...] = ()
    failed_attempts: tuple[FailedAttempt, ...] = ()
    # 产物
    artifacts: Mapping[str, str] = dataclasses.field(default_factory=dict[str, str])
    # 预算
    step_count: int = 0
    total_tokens: int = 0
    # 防御
    consecutive_errors: int = 0
    fingerprint_history: tuple[str, ...] = ()
    # 回路控制（schema v2）
    #: 工具批次之后是否回到 planner 重规划；planner 运行时清零
    replan_needed: bool = False
    #: 自上次规划以来执行了几个工具批次
    steps_since_plan: int = 0
    #: 验收累计打回次数
    rejections: int = 0
    #: planner 连续降级（没拿到可用结构化结论）的次数
    planner_degraded: int = 0
    verification: Verification = "none"
    # 终止
    should_terminate: bool = False
    termination_reason: str = ""
    # 控制
    phase: NextPhase = "planner"
    pending_approval: PendingApproval | None = None
    approval_decision: ApprovalDecision | None = None
    dispatch_started: tuple[str, ...] = ()
    schema_version: int = CURRENT_SCHEMA_VERSION


#: 初始化后不可再更新的字段
FIXED_FIELDS: Final = frozenset(
    {
        "workspace_id",
        "workspace_path",
        "session_id",
        "task_id",
        "task_goal",
        "permission_level",
        "canary_token",
        "schema_version",
    }
)
FIELD_NAMES: Final = tuple(f.name for f in dataclasses.fields(AgentState))


# ==============================================================================
# 字段校验：new_state、apply_update 与解码共用
# ==============================================================================

_Check = Callable[[str, object], Any]


def _bad(field: str, why: str) -> InvalidUpdateError:
    return InvalidUpdateError(f"{field}：{why}")


def _nonempty_str(field: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _bad(field, "必须是非空字符串")
    return value


def _str(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise _bad(field, "必须是字符串")
    return value


def _count(field: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _bad(field, "必须是非负整数")
    return value


def _flag(field: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise _bad(field, "必须是布尔值")
    return value


def _str_tuple(field: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise _bad(field, "必须是字符串列表")
    items = cast(Sequence[object], value)
    if not all(isinstance(i, str) for i in items):
        raise _bad(field, "必须是字符串列表")
    return tuple(cast(Sequence[str], items))


def _typed_tuple[T](kind: type[T]) -> _Check:
    def check(field: str, value: object) -> tuple[T, ...]:
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise _bad(field, f"必须是 {kind.__name__} 列表")
        items = cast(Sequence[object], value)
        if not all(isinstance(i, kind) for i in items):
            raise _bad(field, f"必须是 {kind.__name__} 列表")
        return tuple(cast(Sequence[T], items))

    return check


def _optional[T](kind: type[T]) -> _Check:
    def check(field: str, value: object) -> T | None:
        if value is not None and not isinstance(value, kind):
            raise _bad(field, f"必须是 {kind.__name__} 或 None")
        return value

    return check


def _permission(field: str, value: object) -> str:
    if value not in PERMISSION_LEVELS:
        raise _bad(field, f"未知权限级别 {value!r}")
    return cast(str, value)


def _phase(field: str, value: object) -> str:
    if value not in PHASES:
        raise _bad(field, f"未知阶段 {value!r}")
    return cast(str, value)


def _verification(field: str, value: object) -> str:
    if value not in VERIFICATIONS:
        raise _bad(field, f"未知验证状态 {value!r}")
    return cast(str, value)


def _artifacts(field: str, value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise _bad(field, "必须是 字符串 -> 字符串 的映射")
    items = cast(Mapping[object, object], value)
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in items.items()):
        raise _bad(field, "必须是 字符串 -> 字符串 的映射")
    return dict(cast(Mapping[str, str], items))


def check_messages(field: str, value: object) -> tuple[Message, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise _bad(field, "必须是 Message 序列")
    items = cast(Sequence[object], value)
    if not all(isinstance(m, Message) for m in items):
        raise _bad(field, "必须是 Message 序列")
    return tuple(cast(Sequence[Message], items))


VALIDATORS: Final[dict[str, _Check]] = {
    "workspace_id": _nonempty_str,
    "workspace_path": _nonempty_str,
    "session_id": _nonempty_str,
    "task_id": _nonempty_str,
    "task_goal": _nonempty_str,
    "permission_level": _permission,
    "canary_token": _nonempty_str,
    "milestones": _typed_tuple(Milestone),
    "current_milestone_idx": _count,
    "messages": check_messages,
    "rolling_summary": _str,
    "confirmed_facts": _str_tuple,
    "failed_attempts": _typed_tuple(FailedAttempt),
    "artifacts": _artifacts,
    "step_count": _count,
    "total_tokens": _count,
    "consecutive_errors": _count,
    "fingerprint_history": _str_tuple,
    "replan_needed": _flag,
    "steps_since_plan": _count,
    "rejections": _count,
    "planner_degraded": _count,
    "verification": _verification,
    "should_terminate": _flag,
    "termination_reason": _str,
    "phase": _phase,
    "pending_approval": _optional(PendingApproval),
    "approval_decision": _optional(ApprovalDecision),
    "dispatch_started": _str_tuple,
    "schema_version": _count,
}
assert set(VALIDATORS) == set(FIELD_NAMES)


def build_state(values: Mapping[str, object]) -> AgentState:
    """校验并构造。字段必须齐全；解码与 :func:`new_state` 共用。"""
    missing = [n for n in FIELD_NAMES if n not in values]
    if missing:
        raise InvalidStateError(f"缺少字段：{missing}")
    return AgentState(**{n: VALIDATORS[n](n, values[n]) for n in FIELD_NAMES})  # type: ignore[arg-type]


def new_state(
    *,
    workspace_id: str,
    workspace_path: str,
    session_id: str,
    task_id: str,
    task_goal: str,
    permission_level: str,
    canary_token: str,
    messages: Sequence[Message] = (),
    rolling_summary: str = "",
    confirmed_facts: Sequence[str] = (),
    failed_attempts: Sequence[FailedAttempt] = (),
) -> AgentState:
    """任务启动时的初始状态。会话继承来的记忆（摘要、事实、失败尝试）从这里带入。"""
    defaults = {f.name: _default_of(f) for f in dataclasses.fields(AgentState) if _has_default(f)}
    given: dict[str, object] = {
        "workspace_id": workspace_id,
        "workspace_path": workspace_path,
        "session_id": session_id,
        "task_id": task_id,
        "task_goal": task_goal,
        "permission_level": permission_level,
        "canary_token": canary_token,
        "messages": messages,
        "rolling_summary": rolling_summary,
        "confirmed_facts": confirmed_facts,
        "failed_attempts": failed_attempts,
    }
    return build_state({**defaults, **given})


def _has_default(f: dataclasses.Field[Any]) -> bool:
    return f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING


def _default_of(f: dataclasses.Field[Any]) -> object:
    if f.default is not dataclasses.MISSING:
        return f.default
    return f.default_factory()  # type: ignore[misc]


# ==============================================================================
# 更新与不变量
# ==============================================================================


def apply_update(state: AgentState, update: Mapping[str, object]) -> AgentState:
    """应用阶段返回的增量，返回新状态；纯函数，全有或全无。

    ``messages`` 追加；其余列表与字典整体替换；标量覆盖；未出现的字段保持不变。
    初始化后不可变的字段（归属、任务目标、基线权限、金丝雀）不可更新。
    """
    changes: dict[str, object] = {}
    for name, value in update.items():
        if name in FIXED_FIELDS:
            raise InvalidUpdateError(f"{name}：初始化后不可更新")
        if name not in VALIDATORS:
            raise InvalidUpdateError(f"{name}：未知的状态字段")
        checked = VALIDATORS[name](name, copy.deepcopy(value) if name == "artifacts" else value)
        changes[name] = (*state.messages, *checked) if name == "messages" else checked
    return dataclasses.replace(state, **changes)


def check_invariants(state: AgentState) -> None:
    """提交检查点前调用。违反即视为实现缺陷。"""
    # 最后一组工具调用允许暂无配对：等待审批时；或刚由 executor 产出、正等 tool_runner 执行时
    pending_tail = state.pending_approval is not None or state.phase == "tool_runner"
    assert_atomic_pairs(state.messages, allow_pending_tail=pending_tail)

    if state.current_milestone_idx > len(state.milestones):
        raise InvalidStateError(
            f"current_milestone_idx={state.current_milestone_idx} 超出里程碑数量 "
            f"{len(state.milestones)}"
        )

    if state.dispatch_started:
        last_calls: set[str] = set()
        for message in reversed(state.messages):
            if message.role == "assistant" and message.tool_calls:
                last_calls = {c.id for c in message.tool_calls}
                break
        stray = sorted(set(state.dispatch_started) - last_calls)
        if stray:
            raise InvalidStateError(f"dispatch_started 含不属于最近一次工具调用的 id：{stray}")
