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

PERMISSION_LEVELS: tuple[str, ...] = get_args(PermissionLevel)

#: 最近一次修改文件之后的验证情况：none 没改过；unchecked 改了还没运行过命令；
#: failed / passed 改后最近一条命令失败 / 成功
Verification = Literal["none", "unchecked", "failed", "passed"]
VERIFICATIONS: tuple[str, ...] = get_args(Verification)

CURRENT_SCHEMA_VERSION: Final = 4

#: 会话草稿的上限（docs/02_runtime/05_plan_and_scratch.md）
MAX_PLAN_ITEMS: Final = 12
MAX_FACTS: Final = 20
MAX_FACT_CHARS: Final = 300
MAX_FAILED_ATTEMPTS: Final = 10


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanItem(_Frozen):
    id: int
    title: str
    status: Literal["pending", "in_progress", "completed"] = "pending"


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
    # 对话
    messages: tuple[Message, ...] = ()
    # 草稿：任务内临时记忆，由 update_plan / note 工具写入
    plan: tuple[PlanItem, ...] = ()
    facts: tuple[str, ...] = ()
    failed_attempts: tuple[FailedAttempt, ...] = ()
    # 产物
    artifacts: Mapping[str, str] = dataclasses.field(default_factory=dict[str, str])
    # 防御（瞬态护栏计数）
    consecutive_errors: int = 0
    fingerprint_history: tuple[str, ...] = ()
    consecutive_blocks: int = 0
    verification: Verification = "none"
    # 控制
    pending_approval: PendingApproval | None = None
    approval_decision: ApprovalDecision | None = None
    # 任务内压缩：目标消息之后的前 ``stream_cut`` 条往返已并入 ``stream_summary``，
    # 装配视图用摘要代替它们；``messages`` 本身不动（检查点与轨迹仍是完整的）
    stream_summary: str = ""
    stream_cut: int = 0
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


def _count(field: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _bad(field, "必须是非负整数")
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


def _text(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise _bad(field, "必须是字符串")
    return value


def _permission(field: str, value: object) -> str:
    if value not in PERMISSION_LEVELS:
        raise _bad(field, f"未知权限级别 {value!r}")
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
    "messages": check_messages,
    "plan": _typed_tuple(PlanItem),
    "facts": _str_tuple,
    "failed_attempts": _typed_tuple(FailedAttempt),
    "artifacts": _artifacts,
    "consecutive_errors": _count,
    "fingerprint_history": _str_tuple,
    "consecutive_blocks": _count,
    "verification": _verification,
    "pending_approval": _optional(PendingApproval),
    "approval_decision": _optional(ApprovalDecision),
    "stream_summary": _text,
    "stream_cut": _count,
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
) -> AgentState:
    """任务启动时的初始状态。会话继承来的记忆不入状态，只在冻结的提示词资产里。"""
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
    # 最后一组工具调用允许暂无配对：回合进行中（模型回复已提交、工具尚未完成），或正挂起在审批上
    assert_atomic_pairs(state.messages, allow_pending_tail=True)

    if len(state.plan) > MAX_PLAN_ITEMS:
        raise InvalidStateError(f"plan 条数 {len(state.plan)} 超过上限 {MAX_PLAN_ITEMS}")
    ids = [item.id for item in state.plan]
    if len(set(ids)) != len(ids):
        raise InvalidStateError("plan 的 id 重复")
    if len(state.facts) > MAX_FACTS:
        raise InvalidStateError(f"facts 条数 {len(state.facts)} 超过上限 {MAX_FACTS}")
    if any(len(f) > MAX_FACT_CHARS for f in state.facts):
        raise InvalidStateError(f"facts 单条超过 {MAX_FACT_CHARS} 字符")
    if len(state.failed_attempts) > MAX_FAILED_ATTEMPTS:
        raise InvalidStateError(
            f"failed_attempts 条数 {len(state.failed_attempts)} 超过上限 {MAX_FAILED_ATTEMPTS}"
        )
