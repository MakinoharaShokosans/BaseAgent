"""AgentState 的 JSON 编解码。

严格、无损、可迁移；不使用 pickle，也不按类型名反射构造对象，检查点被篡改的最坏后果是
解码失败。设计文档：docs/02_runtime/01_state_model.md §5。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Final, cast

from pydantic import BaseModel, ValidationError

from agent_runtime.core.errors import InvalidStateError, StateDecodeError
from agent_runtime.core.messages import Message
from agent_runtime.core.state import (
    CURRENT_SCHEMA_VERSION,
    FIELD_NAMES,
    AgentState,
    ApprovalDecision,
    FailedAttempt,
    PendingApproval,
    PlanItem,
    build_state,
)

Migration = Callable[[dict[str, Any]], dict[str, Any]]

#: 低于此版本的检查点不再支持加载
MINIMUM_SCHEMA_VERSION: Final = 3

#: 第 n 版升到第 n+1 版的迁移函数。v1、v2 是五阶段工作流的状态（含阶段、里程碑、预算计数），
#: 与纯 agent loop 的状态不兼容，不迁移（docs/12_decisions/0006）
MIGRATIONS: Final[dict[int, Migration]] = {
    # v3 → v4：加任务内压缩的两个字段（没压缩过：摘要为空、切点为 0）
    3: lambda data: {**data, "stream_summary": "", "stream_cut": 0},
}

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIGRATIONS",
    "MINIMUM_SCHEMA_VERSION",
    "dumps_state",
    "loads_state",
    "state_from_dict",
    "state_to_dict",
]


# ==============================================================================
# 编码
# ==============================================================================


def state_to_dict(state: AgentState) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in FIELD_NAMES:
        value = getattr(state, name)
        if name == "messages":
            out[name] = [m.to_dict() for m in state.messages]
        elif name in ("plan", "failed_attempts"):
            out[name] = [m.model_dump(mode="json") for m in cast("tuple[BaseModel, ...]", value)]
        elif name in ("pending_approval", "approval_decision"):
            out[name] = None if value is None else cast(BaseModel, value).model_dump(mode="json")
        elif name in ("facts", "fingerprint_history"):
            out[name] = list(cast("tuple[str, ...]", value))
        elif name == "artifacts":
            out[name] = dict(cast("Mapping[str, str]", value))
        else:
            out[name] = value
    return out


def dumps_state(state: AgentState) -> str:
    """紧凑 JSON。不排序键：思考内容的不透明载荷必须保持原来的键顺序。"""
    return json.dumps(state_to_dict(state), ensure_ascii=False, separators=(",", ":"))


# ==============================================================================
# 解码
# ==============================================================================


def loads_state(text: str | bytes) -> AgentState:
    try:
        data = json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise StateDecodeError(f"不是合法的 JSON：{exc}") from exc
    return state_from_dict(cast("dict[str, Any]", data))


def state_from_dict(
    data: dict[str, Any],
    *,
    migrations: Mapping[int, Migration] | None = None,
    current_version: int = CURRENT_SCHEMA_VERSION,
    minimum_version: int = MINIMUM_SCHEMA_VERSION,
) -> AgentState:
    """严格解码。版本低于当前时按 ``migrations`` 逐级升级。"""
    if not isinstance(data, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise StateDecodeError("状态必须是 JSON 对象")
    table = MIGRATIONS if migrations is None else migrations
    data = _migrate(data, table, current_version, minimum_version)

    missing = [n for n in FIELD_NAMES if n not in data]
    if missing:
        raise StateDecodeError(f"状态缺少字段：{missing}")
    unknown = sorted(set(data) - set(FIELD_NAMES))
    if unknown:
        raise StateDecodeError(f"状态含未知字段：{unknown}")

    values: dict[str, object] = dict(data)
    values["messages"] = _list(data, "messages", Message.from_dict)
    values["plan"] = _list(data, "plan", lambda d: _model(PlanItem, d))
    values["failed_attempts"] = _list(data, "failed_attempts", lambda d: _model(FailedAttempt, d))
    for name, kind in (
        ("pending_approval", PendingApproval),
        ("approval_decision", ApprovalDecision),
    ):
        raw = data[name]
        values[name] = None if raw is None else _model(kind, raw)

    try:
        return build_state(values)
    except InvalidStateError as exc:
        raise StateDecodeError(str(exc)) from exc


def _migrate(
    data: dict[str, Any], table: Mapping[int, Migration], current: int, minimum: int
) -> dict[str, Any]:
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise StateDecodeError(f"schema_version 缺失或不是整数：{version!r}")
    if version > current:
        raise StateDecodeError(
            f"schema_version={version} 高于当前代码支持的 {current}，请升级程序后再加载"
        )
    if version < minimum:
        raise StateDecodeError(f"schema_version={version} 低于最低支持版本 {minimum}")
    while version < current:
        step = table.get(version)
        if step is None:
            raise StateDecodeError(f"缺少从版本 {version} 升级的迁移函数")
        data = step(dict(data))
        version += 1
        data["schema_version"] = version
    return data


def _list(data: Mapping[str, Any], name: str, decode: Callable[[object], Any]) -> tuple[Any, ...]:
    raw = data[name]
    if not isinstance(raw, list):
        raise StateDecodeError(f"{name} 必须是数组")
    out: list[Any] = []
    for index, item in enumerate(cast("list[object]", raw)):
        try:
            out.append(decode(item))
        except StateDecodeError as exc:
            raise StateDecodeError(f"{name}[{index}]：{exc}") from exc
    return tuple(out)


def _model[M: BaseModel](kind: type[M], raw: object) -> M:
    # 走严格的 JSON 模式：JSON 数组可以进元组字段，但 "1" 不会被悄悄当成整数 1
    try:
        return kind.model_validate_json(json.dumps(raw), strict=True)
    except (ValidationError, TypeError, ValueError) as exc:
        raise StateDecodeError(f"{kind.__name__} 不合法：{exc}") from exc
