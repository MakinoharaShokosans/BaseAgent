"""记忆的数据模型。设计文档：docs/03_context/04_memory.md §4、§5。

``FailedAttempt`` 复用 ``core`` 里的唯一定义。每条事实与失败尝试都带出处（任务 id）、
来源与信任，这样可追溯、可撤销，也能让由不可信内容衍生的条目以数据权威呈现。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.core import FailedAttempt, Trust


@dataclass(frozen=True, slots=True)
class Fact:
    text: str
    #: 出处：产生它的任务
    task_id: str
    trust: Trust
    #: task（任务内验证）| compressor（压缩器提炼）
    source: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "task_id": self.task_id,
            "trust": self.trust,
            "source": self.source,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Fact:
        return cls(d["text"], d["task_id"], d["trust"], d["source"], float(d["created_at"]))


@dataclass(frozen=True, slots=True)
class Failure:
    attempt: FailedAttempt
    task_id: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.attempt.action,
            "failure_reason": self.attempt.failure_reason,
            "conclusion": self.attempt.conclusion,
            "task_id": self.task_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Failure:
        attempt = FailedAttempt(
            action=d["action"], failure_reason=d["failure_reason"], conclusion=d["conclusion"]
        )
        return cls(attempt, d["task_id"], float(d["created_at"]))


@dataclass(frozen=True, slots=True)
class SessionMemory:
    #: 水位线：此 id 及之前的轮次已提炼进摘要
    watermark: int = 0
    summary: str = ""
    facts: tuple[Fact, ...] = ()
    failures: tuple[Failure, ...] = ()
    #: “近期新增”：任务收尾沉淀的，下一次压缩时才并入
    pending_facts: tuple[Fact, ...] = ()
    pending_failures: tuple[Failure, ...] = ()
    last_action_target: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict[str, tuple[str, ...]]
    )
    #: 压缩连续失败次数与下次允许重试的时间（退避）
    compaction_failures: int = 0
    retry_after: float = 0.0

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "facts": [f.to_dict() for f in self.facts],
            "failures": [f.to_dict() for f in self.failures],
            "pending_facts": [f.to_dict() for f in self.pending_facts],
            "pending_failures": [f.to_dict() for f in self.pending_failures],
            "last_action_target": {k: list(v) for k, v in self.last_action_target.items()},
            "compaction_failures": self.compaction_failures,
            "retry_after": self.retry_after,
        }

    @classmethod
    def from_json_dict(cls, watermark: int, summary: str, d: Mapping[str, Any]) -> SessionMemory:
        return cls(
            watermark=watermark,
            summary=summary,
            facts=tuple(Fact.from_dict(x) for x in d.get("facts", ())),
            failures=tuple(Failure.from_dict(x) for x in d.get("failures", ())),
            pending_facts=tuple(Fact.from_dict(x) for x in d.get("pending_facts", ())),
            pending_failures=tuple(Failure.from_dict(x) for x in d.get("pending_failures", ())),
            last_action_target={k: tuple(v) for k, v in d.get("last_action_target", {}).items()},
            compaction_failures=int(d.get("compaction_failures", 0)),
            retry_after=float(d.get("retry_after", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceMemory:
    """用户手动维护的四类内容；只有显式的用户动作能写。"""

    profile: tuple[str, ...] = ()
    guidelines: tuple[str, ...] = ()
    architecture: tuple[str, ...] = ()
    pitfalls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Turn:
    id: int
    session_id: str
    task_id: str
    role: str
    content: str
    token_count: int
    created_at: float


@dataclass(frozen=True, slots=True)
class Workspace:
    workspace_id: str
    name: str
    root_path: str
    description: str
    created_at: float = 0.0


@dataclass(frozen=True, slots=True)
class SessionInfo:
    session_id: str
    workspace_id: str
    created_at: float


TaskStatusName = str


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """任务元数据：列表、刷新后找回、级联删除都靠它，不依赖进程内存。"""

    task_id: str
    session_id: str
    workspace_id: str
    task_goal: str
    status: TaskStatusName
    permission_level: str
    created_at: float
    termination_reason: str = ""
    parent_task_id: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    step_count: int = 0
    total_tokens: int = 0
    artifact_count: int = 0
