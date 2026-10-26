"""不可变的消息类型，与 OpenAI 兼容的对话格式一一对应。

设计文档：docs/02_runtime/01_state_model.md §1；思考内容字段见
docs/07_llm_gateway/03_reasoning_content.md。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

from agent_runtime.core.errors import AtomicPairError, InvalidMessageError, StateDecodeError

Role = Literal["system", "user", "assistant", "tool"]
ROLES: tuple[str, ...] = get_args(Role)

_MESSAGE_KEYS = frozenset(
    {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "name",
        "reasoning",
        "reasoning_state",
        "produced_by",
    }
)
_TOOL_CALL_KEYS = frozenset({"id", "name", "args"})


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    args: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise InvalidMessageError("ToolCall.id 不能为空")
        if not isinstance(self.name, str) or not self.name:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise InvalidMessageError("ToolCall.name 不能为空")
        if not isinstance(self.args, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise InvalidMessageError("ToolCall.args 必须是对象")
        # 深拷贝：之后对传入字典的修改不能渗进已构造的消息
        object.__setattr__(self, "args", copy.deepcopy(dict(self.args)))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "args": copy.deepcopy(dict(self.args))}

    @classmethod
    def from_dict(cls, data: object) -> ToolCall:
        obj = _as_object(data, "tool_calls 项")
        _check_keys(obj, _TOOL_CALL_KEYS, _TOOL_CALL_KEYS, "tool_calls 项")
        try:
            return cls(id=cast(str, obj["id"]), name=cast(str, obj["name"]), args=obj["args"])  # type: ignore[arg-type]
        except InvalidMessageError as exc:
            raise StateDecodeError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()  # 仅 assistant
    tool_call_id: str | None = None  # 仅 tool：指向被响应的 ToolCall.id
    name: str | None = None  # 仅 tool：工具名
    reasoning: str | None = None  # 仅 assistant：明文思考内容，供展示与审计
    reasoning_state: Mapping[str, Any] | None = None  # 仅 assistant：不透明载荷，仅用于回传
    produced_by: str | None = None  # 仅 assistant：产生该消息的端点

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise InvalidMessageError(f"未知 role：{self.role!r}")
        if not isinstance(self.content, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise InvalidMessageError("content 必须是字符串")

        calls = tuple(self.tool_calls)
        object.__setattr__(self, "tool_calls", calls)
        if self.reasoning_state is not None:
            # 保持键顺序的深拷贝；回传时必须与取回时逐字节相同
            object.__setattr__(self, "reasoning_state", copy.deepcopy(dict(self.reasoning_state)))

        if self.role != "assistant":
            for field, present in (
                ("tool_calls", bool(calls)),
                ("reasoning", self.reasoning is not None),
                ("reasoning_state", self.reasoning_state is not None),
                ("produced_by", self.produced_by is not None),
            ):
                if present:
                    raise InvalidMessageError(f"{self.role} 消息不能带 {field}")
        if self.role != "tool":
            if self.tool_call_id is not None:
                raise InvalidMessageError(f"{self.role} 消息不能带 tool_call_id")
            if self.name is not None:
                raise InvalidMessageError(f"{self.role} 消息不能带 name")
        elif not self.tool_call_id:
            raise InvalidMessageError("tool 消息必须带非空的 tool_call_id")

        ids = [c.id for c in calls]
        if len(set(ids)) != len(ids):
            raise InvalidMessageError(f"同一条消息内 tool_call id 重复：{ids}")

    def to_dict(self) -> dict[str, Any]:
        """只写出非默认的字段，字段顺序固定。"""
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [c.to_dict() for c in self.tool_calls]
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            out["name"] = self.name
        if self.reasoning is not None:
            out["reasoning"] = self.reasoning
        if self.reasoning_state is not None:
            out["reasoning_state"] = copy.deepcopy(dict(self.reasoning_state))
        if self.produced_by is not None:
            out["produced_by"] = self.produced_by
        return out

    @classmethod
    def from_dict(cls, data: object) -> Message:
        """严格解码：未知字段、缺必需字段、类型不符都报错；可选字段缺失视为默认值。"""
        obj = _as_object(data, "消息")
        _check_keys(obj, {"role", "content"}, _MESSAGE_KEYS, "消息")
        role, content = obj["role"], obj["content"]
        if role not in ROLES:
            raise StateDecodeError(f"未知 role：{role!r}")
        if not isinstance(content, str):
            raise StateDecodeError("消息的 content 必须是字符串")

        raw_calls = obj.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise StateDecodeError("消息的 tool_calls 必须是数组")
        state = obj.get("reasoning_state")
        if state is not None and not isinstance(state, dict):
            raise StateDecodeError("消息的 reasoning_state 必须是对象")
        for field in ("tool_call_id", "name", "reasoning", "produced_by"):
            value = obj.get(field)
            if value is not None and not isinstance(value, str):
                raise StateDecodeError(f"消息的 {field} 必须是字符串")

        try:
            return cls(
                role=cast(Role, role),
                content=content,
                tool_calls=tuple(ToolCall.from_dict(c) for c in cast(list[object], raw_calls)),
                tool_call_id=cast("str | None", obj.get("tool_call_id")),
                name=cast("str | None", obj.get("name")),
                reasoning=cast("str | None", obj.get("reasoning")),
                reasoning_state=cast("dict[str, Any] | None", state),
                produced_by=cast("str | None", obj.get("produced_by")),
            )
        except InvalidMessageError as exc:
            raise StateDecodeError(str(exc)) from exc


def _as_object(data: object, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise StateDecodeError(f"{what}必须是对象")
    return cast(dict[str, Any], data)


def _check_keys(
    obj: Mapping[str, Any], required: frozenset[str] | set[str], allowed: frozenset[str], what: str
) -> None:
    missing = sorted(required - obj.keys())
    if missing:
        raise StateDecodeError(f"{what}缺少必需字段：{missing}")
    unknown = sorted(obj.keys() - allowed)
    if unknown:
        raise StateDecodeError(f"{what}含未知字段：{unknown}")


def assert_atomic_pairs(messages: Sequence[Message], *, allow_pending_tail: bool = False) -> None:
    """断言每条带 tool_calls 的助手消息后紧跟与之一一对应的 tool 消息。

    ``allow_pending_tail`` 用于任务处于“等待审批”时：最后一组允许暂无（或只有部分）配对，
    但不允许出现不属于它的结果。更早的组仍必须完整。
    """
    index, total = 0, len(messages)
    while index < total:
        message = messages[index]
        if message.role == "tool":
            raise AtomicPairError(f"第 {index} 条 tool 消息没有对应的助手 tool_calls")
        if message.role != "assistant" or not message.tool_calls:
            index += 1
            continue

        expected = {c.id for c in message.tool_calls}
        end = index + 1
        seen: list[str] = []
        while end < total and messages[end].role == "tool":
            seen.append(cast(str, messages[end].tool_call_id))
            end += 1

        foreign = sorted(set(seen) - expected)
        if foreign:
            raise AtomicPairError(f"第 {index} 条助手消息之后出现不属于它的工具结果：{foreign}")
        if len(set(seen)) != len(seen):
            raise AtomicPairError(f"第 {index} 条助手消息之后的工具结果重复：{seen}")
        missing = sorted(expected - set(seen))
        if missing and not (allow_pending_tail and end == total):
            raise AtomicPairError(f"第 {index} 条助手消息的 tool_calls 缺少配对结果：{missing}")
        index = end
