"""事件信封与载荷整理。设计文档：docs/08_observability/01_events.md §1、§4、§9。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

#: 事件模式版本。新增字段不升版本；删除或改变字段含义必须升
SCHEMA_VERSION: Final = 1
#: 终态事件：流在它之后关闭；``task.finished`` 总是最后一个带序号的事件
TERMINAL_EVENTS: Final = frozenset({"task.finished"})
#: 信封保留字：载荷里的同名键会被丢弃，不能伪造序号或任务
RESERVED: Final = frozenset({"event", "task_id", "seq", "ts", "v"})
#: 摘要类字段：强制限长，不依赖发出者自觉（隔离承诺的一部分）
_CLIPPED_KEYS: Final = ("summary", "excerpt", "detail", "reason")

HEARTBEAT: Final = "heartbeat"
STREAM_DROPPED: Final = "stream.dropped"


def clip(text: str, limit: int) -> str:
    """压平空白并截断。``limit`` 以字符计。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: max(limit - 1, 0)] + "…"


def _is_clipped_key(key: str) -> bool:
    return any(key == name or key.endswith("_" + name) for name in _CLIPPED_KEYS)


def sanitize_payload(value: object, limit: int) -> object:
    """递归整理载荷：摘要类字符串字段限长，保留字被丢弃（仅顶层由 ``Event`` 处理）。"""
    if isinstance(value, Mapping):
        return {
            k: clip(v, limit)  # pyright: ignore[reportUnknownArgumentType]
            if isinstance(k, str) and _is_clipped_key(k) and isinstance(v, str)
            else sanitize_payload(v, limit)
            for k, v in value.items()  # pyright: ignore[reportUnknownVariableType]
        }
    if isinstance(value, list | tuple):
        return [sanitize_payload(v, limit) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


@dataclass(frozen=True, slots=True)
class Event:
    task_id: str
    #: 任务内单调递增；传输类事件（心跳）为 0，不计入序号
    seq: int
    ts: float
    name: str
    data: Mapping[str, object] = field(default_factory=dict[str, object])
    v: int = SCHEMA_VERSION

    @property
    def is_transport(self) -> bool:
        return self.seq == 0

    def to_dict(self) -> dict[str, object]:
        body = {k: val for k, val in self.data.items() if k not in RESERVED}
        return {
            "event": self.name,
            "task_id": self.task_id,
            "seq": self.seq,
            "ts": self.ts,
            "v": self.v,
            **body,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), default=str)

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> Event:
        data = {k: v for k, v in raw.items() if k not in RESERVED}
        return cls(
            task_id=str(raw["task_id"]),
            seq=int(raw["seq"]),  # type: ignore[arg-type]
            ts=float(raw["ts"]),  # type: ignore[arg-type]
            name=str(raw["event"]),
            data=data,
            v=int(raw.get("v", 1)),  # type: ignore[arg-type]
        )
