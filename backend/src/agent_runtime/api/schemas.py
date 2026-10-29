"""对外 DTO。永远不是内部状态对象；写请求拒绝未知字段。设计文档：docs/09_api/01_endpoints.md。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: 路径与正文里的资源 id：只接受安全字符集，防止穿越到轨迹与产物目录
ResourceId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
MemoryLayer = Literal["profile", "guidelines", "architecture", "pitfalls"]
PermissionName = Literal["read_only", "workspace_write", "full_permissions"]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ------------------------------------------------------------------ 工作区


class WorkspaceCreate(_Request):
    name: str = Field(min_length=1, max_length=120)
    root_path: str = Field(min_length=1, max_length=4096)
    description: str = Field(default="", max_length=2000)
    workspace_id: ResourceId | None = None


class WorkspaceUpdate(_Request):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class WorkspaceOut(_Response):
    workspace_id: str
    name: str
    root_path: str
    description: str
    created_at: float


class MemoryItemCreate(_Request):
    text: str = Field(min_length=1, max_length=2000)
    layer: MemoryLayer | None = None


class WorkspaceMemoryOut(_Response):
    profile: list[str]
    guidelines: list[str]
    architecture: list[str]
    pitfalls: list[str]


# ------------------------------------------------------------------ 会话


class ForkRequest(_Request):
    #: 只复制到这一轮（含）为止；省略则复制整个会话
    from_turn_id: int | None = Field(default=None, ge=1)


class SessionOut(_Response):
    session_id: str
    workspace_id: str
    created_at: float
    title: str | None = None
    pinned: bool = False
    archived: bool = False
    updated_at: float = 0.0


class SessionUpdate(_Request):
    title: str | None = Field(default=None, max_length=120)
    pinned: bool | None = None
    archived: bool | None = None


class TurnOut(_Response):
    id: int
    task_id: str
    role: str
    content: str
    created_at: float


class FactOut(_Response):
    text: str
    task_id: str
    trust: str
    source: str
    created_at: float


class FailureOut(_Response):
    action: str
    failure_reason: str
    conclusion: str
    task_id: str
    created_at: float


class SessionMemoryOut(_Response):
    summary: str
    watermark: int
    facts: list[FactOut]
    failures: list[FailureOut]
    pending_facts: list[FactOut]
    pending_failures: list[FailureOut]


class CompactionOut(_Response):
    status: Literal["compacted", "nothing_to_compact", "failed"]
    #: 本次并入摘要的轮次数
    turns: int
    memory: SessionMemoryOut


# ------------------------------------------------------------------ 任务


class TaskCreate(_Request):
    task_goal: str = Field(min_length=1)
    parent_task_id: ResourceId | None = None
    permission_level: PermissionName | None = None
    confirm_full_permissions: bool = False


class ApproveBody(_Request):
    approval_id: str = Field(min_length=1, max_length=128)
    decision: Literal["once", "always"] = "once"


class RejectBody(_Request):
    approval_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="", max_length=1000)


class ApprovalRequestOut(_Response):
    approval_id: str
    call_ids: list[str]
    required_level: str
    current_level: str
    action_type: str
    command: str
    reason: str
    escalation_count: int
    related_actions: list[str]


class PlanItemOut(_Response):
    id: int
    title: str
    status: str


class TaskOut(_Response):
    task_id: str
    session_id: str
    workspace_id: str
    task_goal: str
    status: str
    permission_level: str
    approval_request: ApprovalRequestOut | None
    plan: list[PlanItemOut]
    step_count: int
    total_tokens: int
    consecutive_errors: int
    termination_reason: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    artifact_count: int


class DecisionOut(_Response):
    task_id: str
    status: str
    message: str


class ArtifactOut(_Response):
    artifact_id: str
    size: int


# ------------------------------------------------------------------ 文件


class FileContentOut(_Response):
    path: str
    content: str
    hash: str
    size: int


class FileSave(_Request):
    path: str = Field(min_length=1, max_length=4096)
    content: str
    base_hash: str | None = None


class FileSaveOut(_Response):
    path: str
    hash: str
    size: int


class TreeNode(_Response):
    name: str
    path: str
    is_dir: bool
    children: list[TreeNode] | None = None


class DirectoryEntry(_Response):
    name: str
    path: str
    has_children: bool


class DirectoryListing(_Response):
    path: str
    parent: str | None
    entries: list[DirectoryEntry]
    truncated: bool


# ------------------------------------------------------------------ 设置


class SessionSettingsIn(_Request):
    #: ``GET /models`` 的端点名；``None`` 表示按配置顺序
    model: str | None = Field(default=None, min_length=1, max_length=128)
    context_token_limit: int | None = Field(default=None, ge=1)


class RangeOut(_Response):
    min: int
    max: int
    step: int


class SessionSettingsOut(_Response):
    model: str | None
    context_token_limit: int | None
    defaults: dict[str, int]
    bounds: dict[str, RangeOut]


class AppSettingsIn(_Request):
    #: ``None`` 或空白表示恢复内置提示词
    system_prompt: str | None = None


class AppSettingsOut(_Response):
    system_prompt: str | None
    system_prompt_default: str
    system_prompt_max_chars: int


class PromptInfoOut(_Response):
    name: str
    kind: Literal["template"] = "template"
    title: str
    usage: str = ""
    size: int = 0
    customized: bool = False
    max_chars: int = 0


class PromptDetailOut(_Response):
    name: str
    content: str
    default: str
    customized: bool
    max_chars: int


class PromptIn(_Request):
    content: str


class WorkspaceSettingsIn(_Request):
    permission_level: PermissionName | None = None
    model: str | None = Field(default=None, max_length=128)
    rules_enabled: bool | None = None


class WorkspaceSettingsOut(_Response):
    permission_level: PermissionName | None
    model: str | None
    rules_enabled: bool | None
    defaults: dict[str, object]


class SkillSourceOut(_Response):
    source: Literal["builtin", "global", "workspace"]
    enabled: bool
    count: int
    trusted: bool


class SkillToggleIn(_Request):
    enabled: bool


class SkillSourceToggleIn(_Request):
    enabled: bool


class McpSettingsOut(_Response):
    enabled: bool


class McpSettingsIn(_Request):
    enabled: bool


class McpServerOut(_Response):
    name: str
    transport: str
    status: str
    enabled: bool
    exposure: Literal["pinned", "deferred"]
    disabled_tools: list[str]
    command_preview: str
    tool_count: int
    tools: list[str]
    rejected: list[dict[str, str]]
    last_error: str
    retry_in_sec: int


class McpServerIn(_Request):
    enabled: bool | None = None
    exposure: Literal["pinned", "deferred"] | None = None
    disabled_tools: list[str] | None = None
