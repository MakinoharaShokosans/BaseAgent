"""配置模型：config.toml 每一节对应一个类，字段默认值即“代码内置默认值”。

规则出处：docs/01_architecture/04_configuration.md §6。这里只放不依赖运行环境的校验；
依赖环境的（密钥是否存在、目录是否可写、路径是否落在数据目录下）在 loader 里做。
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from baseagent_config.sources import FileDataSource

PosInt = Annotated[int, Field(gt=0)]
NonNegInt = Annotated[int, Field(ge=0)]
PosFloat = Annotated[float, Field(gt=0)]
Ratio = Annotated[float, Field(gt=0, lt=1)]
Port = Annotated[int, Field(ge=1, le=65535)]

PermissionLevel = Literal["read_only", "workspace_write", "full_permissions"]
ModelTier = Literal["reasoning", "fast"]

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: 视为敏感的环境变量名片段：这类值在 MCP 配置里必须写成 "env:VAR" 引用
_SECRET_NAME = re.compile(r"key|token|secret|password|passwd|credential|authorization", re.I)


def _require_loopback(host: str) -> str:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"只允许回环地址（127.0.0.1 / localhost / ::1），收到 {host!r}")
    return host


def _compile_all(patterns: list[str]) -> list[str]:
    for pattern in patterns:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"正则无法编译：{pattern!r}（{exc}）") from exc
    return patterns


class _Section(BaseModel):
    """所有配置节的基类：未知键报错，加载后不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ==============================================================================
# 1-3 应用、日志、存储
# ==============================================================================


class AppConfig(_Section):
    home: str = "~/.local/share/baseagent"


class LoggingConfig(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    console: bool = True
    file: str = "logs/baseagent.jsonl"
    rotation_mb: PosInt = 20
    retention_files: PosInt = 10


class StorageConfig(_Section):
    metadata_db: str = "baseagent_meta.db"
    checkpoint_db: str = "checkpoints/state.db"
    events_db: str = "events/events.db"
    traces_dir: str = "traces"
    artifacts_dir: str = "artifacts"


# ==============================================================================
# 4 HTTP API
# ==============================================================================


class AuthConfig(_Section):
    enabled: bool = True
    token_file: str = "api_token"
    token_env: str = "BASEAGENT_API_TOKEN"
    fail_limit: PosInt = 10
    fail_window_sec: PosInt = 60


class FilesystemConfig(_Section):
    browse_roots: list[str] = ["~"]
    browse_max_entries: PosInt = 500
    native_picker_enabled: bool = True
    native_picker_timeout_sec: PosInt = 120


class WorkspaceConfig(_Section):
    forbidden_roots: list[str] = [
        "/",
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/boot",
        "/sys",
        "/proc",
        "/dev",
        "/var",
        "/root",
    ]
    forbid_home_itself: bool = True


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: Port = 11328
    max_concurrent_tasks: PosInt = 1
    cors_allow_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    allowed_hosts: list[str] = []
    sse_heartbeat_sec: PosInt = 15
    sse_buffer_events: PosInt = 1000
    sse_subscriber_queue: PosInt = 256
    request_max_bytes: PosInt = 2_000_000
    task_goal_max_chars: PosInt = 20000
    docs_enabled: bool = True
    auth: AuthConfig = AuthConfig()
    filesystem: FilesystemConfig = FilesystemConfig()
    workspace: WorkspaceConfig = WorkspaceConfig()

    _loopback = field_validator("host")(_require_loopback)

    @model_validator(mode="after")
    def _browse_roots_clear_of_forbidden(self) -> Self:
        # "/" 只禁止把根目录本身当工作区；其余禁用根连同其下的路径都禁止浏览
        forbidden = [r.rstrip("/") for r in self.workspace.forbidden_roots if r.rstrip("/")]
        for root in self.filesystem.browse_roots:
            if root.startswith("~"):
                continue  # 用户目录，由 forbid_home_itself 另行约束
            clean = root.rstrip("/") or "/"
            for banned in forbidden:
                if clean == banned or clean.startswith(banned + "/"):
                    raise ValueError(f"browse_roots 中的 {root!r} 落在禁用根 {banned!r} 之内")
        return self


# ==============================================================================
# 5 运行时
# ==============================================================================


class GuardrailsConfig(_Section):
    """护栏。没有预算类上限：额度耗尽按模型错误处理（docs/12_decisions/0006）。"""

    consecutive_errors_limit: PosInt = 3
    identical_fingerprint_limit: PosInt = 3
    #: 连续被重复拦截的批次数达到它，任务以 failed 结束
    max_consecutive_blocks: PosInt = 5


class LoopConfig(_Section):
    """主循环的形状：一个模型档、一份工具集，没有阶段。"""

    #: 主循环使用的模型档
    model_tier: ModelTier = "reasoning"
    #: 单次模型调用的超时秒数；超时按模型错误处理（任务 failed，可续跑）
    call_timeout_sec: PosInt = 300
    #: 摆动（ABAB…）检测的周期数；0 关闭
    alternation_cycles: NonNegInt = 3


class ApprovalConfig(_Section):
    default_level: PermissionLevel = "workspace_write"
    require_full_permissions_confirm: bool = True


class RuntimeConfig(_Section):
    guardrails: GuardrailsConfig = GuardrailsConfig()
    loop: LoopConfig = LoopConfig()
    approval: ApprovalConfig = ApprovalConfig()


# ==============================================================================
# 6 模型
# ==============================================================================


class Capabilities(_Section):
    """端点能力声明。保守默认：什么都不假设，由配置显式打开。"""

    tools: bool = False
    json_object: bool = False
    json_schema: bool = False
    usage_in_response: bool = False
    merge_consecutive_user: bool = False
    thinking_control: Literal["none", "deepseek"] = "none"
    thinking_with_tools: bool = False
    reasoning_field: str | None = None
    reasoning_passback: Literal["never", "tool_turns", "opaque", "all_when_tools"] = "never"
    #: 端点支持流式 chat.completions；不声明时按整条响应处理
    streaming: bool = False


class Endpoint(_Section):
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    timeout_sec: PosInt
    context_window: PosInt
    max_output_tokens: PosInt
    reasoning_effort: Literal["low", "medium", "high", "max"] | None = None
    capabilities: Capabilities = Capabilities()

    @model_validator(mode="after")
    def _output_fits_window(self) -> Self:
        if self.max_output_tokens >= self.context_window:
            raise ValueError(
                f"max_output_tokens（{self.max_output_tokens}）必须小于上下文窗口"
                f"（{self.context_window}）"
            )
        return self


class TierConfig(_Section):
    temperature: Annotated[float, Field(ge=0, le=2)] = 0.0
    endpoints: list[Endpoint] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [e.name for e in self.endpoints]
        duplicated = sorted({n for n in names if names.count(n) > 1})
        if duplicated:
            raise ValueError(f"同一档内端点重名：{duplicated}")
        return self


class ModelsConfig(_Section):
    reasoning: TierConfig
    fast: TierConfig


# ==============================================================================
# 7 模型网关
# ==============================================================================


class RetryConfig(_Section):
    max_retries: PosInt = 3
    backoff_factor: PosFloat = 2.0
    backoff_max_sec: PosInt = 30
    jitter: Annotated[float, Field(ge=0, le=1)] = 0.2
    honor_retry_after: bool = True


class CircuitConfig(_Section):
    open_after: PosInt = 2
    open_sec: PosInt = 60


class ConcurrencyConfig(_Section):
    per_endpoint: PosInt = 4


class DeadlineConfig(_Section):
    min_attempt_sec: PosInt = 5


class UsageConfig(_Section):
    estimate_when_missing: bool = True
    estimate_safety_factor: Annotated[float, Field(ge=1)] = 1.2


class StructuredConfig(_Section):
    repair_retries: NonNegInt = 1
    prefer_schema: bool = True
    on_truncate_raise_max_tokens: bool = True


class LlmConfig(_Section):
    retry: RetryConfig = RetryConfig()
    circuit: CircuitConfig = CircuitConfig()
    concurrency: ConcurrencyConfig = ConcurrencyConfig()
    deadline: DeadlineConfig = DeadlineConfig()
    usage: UsageConfig = UsageConfig()
    structured: StructuredConfig = StructuredConfig()


# ==============================================================================
# 8 上下文工程
# ==============================================================================


class SessionContextConfig(_Section):
    token_limit: PosInt = 32000
    high_watermark: Ratio = 0.80
    compaction_ratio: Ratio = 0.40
    active_window_turns: PosInt = 3
    compaction_backoff_sec: list[PosInt] = [30, 120, 600]

    @model_validator(mode="after")
    def _hysteresis(self) -> Self:
        if self.high_watermark <= self.compaction_ratio:
            raise ValueError(
                f"high_watermark（{self.high_watermark}）必须大于 "
                f"compaction_ratio（{self.compaction_ratio}），否则压缩后立刻再次触发"
            )
        return self


class TaskContextConfig(_Section):
    soft_ratio: Ratio = 0.60
    hard_ratio: Ratio = 0.80
    soft_tokens: PosInt = 100_000
    hard_tokens: PosInt = 160_000
    fold_keep_recent_steps: PosInt = 3
    compaction_enabled: bool = True
    #: 压缩摘要的字符上限，以及送去总结的旧步骤的字符上限（超出的每条结果会被截短）
    compaction_summary_chars: PosInt = 4000
    compaction_input_chars: PosInt = 60_000
    fold_exempt_tools: list[str] = ["load_skill", "mcp_describe"]
    #: 结果可以靠“用同样的参数再调一次”取回的工具：折叠时不需要离线副本，只留一行存根
    fold_reproducible_tools: list[str] = ["view_file", "grep", "list_dir", "read_artifact"]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.soft_ratio >= self.hard_ratio:
            raise ValueError(
                f"soft_ratio（{self.soft_ratio}）必须小于 hard_ratio（{self.hard_ratio}）"
            )
        if self.soft_tokens >= self.hard_tokens:
            raise ValueError(
                f"soft_tokens（{self.soft_tokens}）必须小于 hard_tokens（{self.hard_tokens}）"
            )
        return self

    def soft_trigger(self, context_window: int) -> int:
        """折叠触发线：窗口很大时比例线形同虚设，所以取比例线与绝对线的较小者。"""
        return min(int(context_window * self.soft_ratio), self.soft_tokens)

    def hard_trigger(self, context_window: int) -> int:
        """压缩触发线，取法同 :meth:`soft_trigger`。"""
        return min(int(context_window * self.hard_ratio), self.hard_tokens)


class ObservationConfig(_Section):
    max_tokens: PosInt = 1500
    head_lines: PosInt = 20
    tail_lines: PosInt = 30
    keyword_lines_limit: PosInt = 30
    json_outline_max_depth: PosInt = 3
    json_outline_array_items: PosInt = 5
    scalar_preview_chars: PosInt = 120
    exempt_tools: list[str] = ["load_skill", "mcp_describe"]
    exempt_multiplier: PosInt = 8
    #: 读类工具自己有行数、条数上限与翻页提示，不再按头尾截断；只受下面这条更高的上限约束
    passthrough_tools: list[str] = ["view_file", "grep", "list_dir", "read_artifact", "websearch"]
    passthrough_max_tokens: PosInt = 12_000


class MemoryConfig(_Section):
    max_facts: PosInt = 50
    max_failed_attempts: PosInt = 30
    max_pending_facts: PosInt = 20
    summary_max_chars: PosInt = 300
    persist_task_facts: bool = True
    require_provenance: bool = True


class RulesConfig(_Section):
    allow_workspace: bool = False
    max_chars: PosInt = 8000
    candidates: list[str] = [
        "CLAUDE.MD",
        "CLAUDE.md",
        "AGENTS.md",
        ".baseagent/rules.md",
    ]


class PromptConfig(_Section):
    #: 草稿回显第一行重复任务目标的最大字符数
    goal_echo_chars: PosInt = 300


class ContextConfig(_Section):
    session: SessionContextConfig = SessionContextConfig()
    task: TaskContextConfig = TaskContextConfig()
    observation: ObservationConfig = ObservationConfig()
    memory: MemoryConfig = MemoryConfig()
    rules: RulesConfig = RulesConfig()
    prompt: PromptConfig = PromptConfig()


# ==============================================================================
# 9 工具
# ==============================================================================


class FilesToolsConfig(_Section):
    view_max_lines: PosInt = 400
    view_max_file_mb: PosInt = 10
    write_max_bytes: PosInt = 1_000_000
    grep_max_results: PosInt = 200
    list_dir_max_entries: PosInt = 200
    ignored_dirs: list[str] = [
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "coverage",
        "htmlcov",
        ".cache",
    ]


class DispatchConfig(_Section):
    default_timeout_sec: PosInt = 60


class ToolsConfig(_Section):
    files: FilesToolsConfig = FilesToolsConfig()
    dispatch: DispatchConfig = DispatchConfig()


# ==============================================================================
# 10-12 sidecar
# ==============================================================================


class ServicesConfig(_Section):
    shell_url: str = "http://127.0.0.1:8002"
    web_url: str = "http://127.0.0.1:8003"
    timeout_sec: PosInt = 60
    auto_start_sidecars: bool = True
    startup_timeout_sec: PosInt = 10
    stop_grace_sec: PosInt = 3
    sidecar_env_allowlist: list[str] = [
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "BASEAGENT_CONFIG",
        "BASEAGENT_HOME",
    ]


class BashShellConfig(_Section):
    host: str = "127.0.0.1"
    port: Port = 8002
    timeout_sec: PosInt = 60
    max_timeout_sec: PosInt = 600
    sigterm_grace_sec: PosInt = 2
    rlimit_as_mb: PosInt = 2048
    rlimit_fsize_mb: PosInt = 50
    rlimit_cpu_sec: PosInt = 300
    memory_pool_mb: PosInt = 8192
    max_concurrent: PosInt = 4
    artifacts_dir: str = "artifacts"
    stream_chunk_bytes: PosInt = 65536
    distill_head_lines: PosInt = 20
    distill_tail_lines: PosInt = 30
    distill_max_keyword_lines: PosInt = 200
    structured_sniff_max_bytes: PosInt = 262144
    child_env_allowlist: list[str] = ["PATH", "LANG", "LC_ALL", "TERM", "TZ"]
    child_home: str = "task_scratch"
    extra_deny_patterns: list[str] = []

    _loopback = field_validator("host")(_require_loopback)

    @field_validator("extra_deny_patterns")
    @classmethod
    def _patterns_compile(cls, value: list[str]) -> list[str]:
        return _compile_all(value)

    @model_validator(mode="after")
    def _timeout_within_maximum(self) -> Self:
        if self.timeout_sec > self.max_timeout_sec:
            raise ValueError(
                f"timeout_sec（{self.timeout_sec}）不能大于 max_timeout_sec"
                f"（{self.max_timeout_sec}）"
            )
        return self


class WebSearchConfig(_Section):
    host: str = "127.0.0.1"
    port: Port = 8003
    provider: str = "searxng"
    searxng_instances: list[str] = [
        "https://searx.tiekoetter.com",
        "https://searx.be",
        "https://search.ononoki.org",
    ]
    searxng_language: str = "zh-CN"
    ddgs_backends: list[str] = ["yahoo", "bing", "google"]
    max_results: PosInt = 8
    fetch_concurrency: PosInt = 5
    per_host_concurrency: PosInt = 2
    request_timeout_sec: PosInt = 10
    total_timeout_sec: PosInt = 30
    max_content_tokens: PosInt = 1500
    preview_chars: PosInt = 200
    user_agent: str = "Mozilla/5.0 (compatible; BaseAgentBot/0.1)"
    artifacts_dir: str = "artifacts"
    block_private_addresses: bool = True
    allow_fake_ip: bool = False
    allowed_ports: list[Port] = [80, 443]
    max_redirects: NonNegInt = 5
    max_download_bytes: PosInt = 2_000_000
    allowed_content_types: list[str] = ["text/html", "text/plain", "application/xhtml+xml"]

    _loopback = field_validator("host")(_require_loopback)


# ==============================================================================
# 13-14 研究与委派
# ==============================================================================


class WebSearchToolConfig(_Section):
    enabled: bool = True
    model_tier: ModelTier = "fast"
    max_rounds: PosInt = 3
    max_sources: PosInt = 5
    max_source_chars: PosInt = 6000
    max_wall_time_sec: PosInt = 60
    extract_reserve_sec: PosInt = 20
    max_calls_per_task: PosInt = 3
    max_findings: PosInt = 8
    max_answer_chars: PosInt = 500
    max_code_examples: PosInt = 5
    max_code_chars: PosInt = 2000
    max_report_chars: PosInt = 4000


class SubagentConfig(_Section):
    enabled: bool = True
    model_tier: ModelTier = "fast"
    max_depth: NonNegInt = 1
    max_steps: PosInt = 8
    max_wall_time_sec: PosInt = 90
    max_assigned_tools: PosInt = 6
    max_findings: PosInt = 6
    max_observation_chars: PosInt = 4000
    max_answer_chars: PosInt = 400
    max_citations: PosInt = 8
    max_report_chars: PosInt = 3000
    denied_tools: list[str] = [
        "spawn_subagent",
        "websearch",
        "update_plan",
        "note",
        "mcp_describe",
        "mcp_call",
    ]

    @field_validator("denied_tools")
    @classmethod
    def _no_recursive_spawn(cls, value: list[str]) -> list[str]:
        if "spawn_subagent" not in value:
            raise ValueError("denied_tools 必须包含 spawn_subagent，子智能体不得再委派")
        return value


# ==============================================================================
# 15-16 技能与权限
# ==============================================================================


class SkillsConfig(_Section):
    allow_builtin: bool = True
    allow_global: bool = True
    allow_workspace: bool = False
    protect_trusted_names: bool = True
    scan_untrusted_body: bool = True
    max_description_chars: PosInt = 200
    max_triggers: PosInt = 12
    high_privilege_tools: list[str] = ["bash", "write_file", "edit_file"]


class PermissionsConfig(_Section):
    default_level: PermissionLevel = "workspace_write"
    unknown_tool_level: PermissionLevel = "full_permissions"
    read_only_tools: list[str] = [
        "view_file",
        "list_dir",
        "grep",
        "read_artifact",
        "load_skill",
        "update_plan",
        "note",
        "mcp_describe",
        "websearch",
        "spawn_subagent",
    ]
    workspace_write_tools: list[str] = ["write_file", "edit_file"]
    full_permission_tools: list[str] = []
    bash_tools: list[str] = ["bash"]
    full_permission_patterns: list[str] = [
        r"\bgit\s+push\b",
        r"\b(curl|wget)\b",
        r"\b(ssh|scp|rsync)\b",
        r"\bpip3?\s+install\b",
        r"\bnpm\s+(install|i|ci)\b",
        r"\b(yarn|pnpm)\s+(add|install)\b",
        r"\b(apt|apt-get|yum|dnf|apk)\s+(install|upgrade|remove)\b",
        r"\bsudo\b",
        r"\b(docker|docker-compose|kubectl|helm)\b",
        r"\|\s*(sh|bash|zsh)\b",
    ]
    workspace_write_patterns: list[str] = [
        r">>?\s*\S",
        r"\b(rm|mv|cp|mkdir|rmdir|touch|ln)\b",
        r"\b(sed|perl)\s+-i\b",
        r"\b(chmod|chown)\b",
        r"\bgit\s+(add|commit|checkout|switch|merge|rebase|reset|stash|tag|clean)\b",
        r"\b(make|cmake|ninja)\b",
        r"\b(gcc|g\+\+|clang|clang\+\+|rustc|cargo|go\s+(build|test))\b",
        r"\b(pytest|python\s+-m\s+pytest)\b",
        r"\b(npm|yarn|pnpm)\s+(run|test)\b",
        r"\btee\b",
    ]

    @field_validator("full_permission_patterns", "workspace_write_patterns")
    @classmethod
    def _patterns_compile(cls, value: list[str]) -> list[str]:
        return _compile_all(value)


# ==============================================================================
# 17 MCP
# ==============================================================================


class McpServerConfig(_Section):
    enabled: bool = False
    transport: Literal["stdio", "sse"] = "stdio"
    command: str = ""
    args: list[str] = []
    url: str = ""
    env: dict[str, str] = {}
    allow_tools: list[str] = []
    #: pinned：Schema 直接进 tools；deferred：只在索引里，经 mcp_describe / mcp_call 使用。
    #: 空表示沿用 [mcp].exposure
    exposure: Literal["", "pinned", "deferred"] = ""
    #: 可安全重试的工具（支持 ``*`` 通配）：崩溃恢复据此决定重试还是标记“结果未知”
    idempotent_tools: list[str] = []
    #: 覆盖 [mcp] 的全局资源上限；0 表示沿用全局值
    rlimit_as_mb: NonNegInt = 0
    rlimit_fsize_mb: NonNegInt = 0
    rlimit_cpu_sec: NonNegInt = 0

    @model_validator(mode="after")
    def _transport_fields(self) -> Self:
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio 服务器必须给出 command")
        if self.transport == "sse" and not self.url:
            raise ValueError("sse 服务器必须给出 url")
        for name, value in self.env.items():
            if _SECRET_NAME.search(name) and not value.startswith("env:"):
                raise ValueError(
                    f'环境变量 {name} 看起来是敏感值，必须写成 "env:变量名" 引用 .env，不能是明文'
                )
        return self


class McpConfig(_Section):
    enabled: bool = True
    connection_timeout_sec: PosInt = 30
    call_timeout_sec: PosInt = 50
    retry_backoff_sec: list[PosInt] = [30, 120, 600]
    max_description_chars: PosInt = 1000
    reject_on_injection: bool = True
    exposed_name_max_len: PosInt = 64
    #: 全局默认暴露方式（决策 0007）
    exposure: Literal["pinned", "deferred"] = "deferred"
    #: deferred 索引的条数上限，超出时只列服务器与工具数
    index_max_listed: PosInt = 60
    index_desc_chars: PosInt = 80
    refresh_tools_each_task: bool = True
    rlimit_as_mb: PosInt = 4096
    rlimit_fsize_mb: PosInt = 50
    rlimit_cpu_sec: PosInt = 300
    servers: dict[str, McpServerConfig] = {}


# ==============================================================================
# 18-20 可观测性、安全、评测
# ==============================================================================


class EventsConfig(_Section):
    persist: bool = True
    summary_clip_chars: PosInt = 300
    tool_result_clip_chars: PosInt = 500


class RedactionConfig(_Section):
    enabled: bool = True
    key_patterns: list[str] = ["key", "token", "secret", "password", "authorization", "cookie"]
    value_patterns: list[str] = [
        r"sk-[A-Za-z0-9]{16,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    ]

    @field_validator("key_patterns", "value_patterns")
    @classmethod
    def _patterns_compile(cls, value: list[str]) -> list[str]:
        return _compile_all(value)


class SecurityConfig(_Section):
    canary_salt_file: str = "canary_salt"


# ==============================================================================
# 根
# ==============================================================================


class Settings(BaseSettings):
    """整份配置。

    来源优先级（高覆盖低）：显式传入 > 环境变量 ``BASEAGENT_<节>__<键>``
    > config.toml > 默认值。
    实际装配在 :func:`baseagent_config.load_settings`，直接实例化只会读环境变量和默认值。
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_prefix="BASEAGENT_",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 显式传入 > 环境变量 > config.toml；
        # 不读 .env 文件（.env 由启动脚本加载进环境）
        return (init_settings, env_settings, FileDataSource(settings_cls))

    app: AppConfig = AppConfig()
    logging: LoggingConfig = LoggingConfig()
    storage: StorageConfig = StorageConfig()
    server: ServerConfig = ServerConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    models: ModelsConfig
    llm: LlmConfig = LlmConfig()
    context: ContextConfig = ContextConfig()
    tools: ToolsConfig = ToolsConfig()
    services: ServicesConfig = ServicesConfig()
    bash_shell: BashShellConfig = BashShellConfig()
    web_search: WebSearchConfig = WebSearchConfig()
    websearch: WebSearchToolConfig = WebSearchToolConfig()
    subagent: SubagentConfig = SubagentConfig()
    skills: SkillsConfig = SkillsConfig()
    permissions: PermissionsConfig = PermissionsConfig()
    mcp: McpConfig = McpConfig()
    events: EventsConfig = EventsConfig()
    redaction: RedactionConfig = RedactionConfig()
    security: SecurityConfig = SecurityConfig()

    @model_validator(mode="after")
    def _cross_section(self) -> Self:
        if self.mcp.call_timeout_sec >= self.tools.dispatch.default_timeout_sec:
            raise ValueError(
                f"mcp.call_timeout_sec（{self.mcp.call_timeout_sec}）必须小于工具派发超时"
                f"（tools.dispatch.default_timeout_sec={self.tools.dispatch.default_timeout_sec}），"
                "让 MCP 管理器先咬合而不是被派发器取消"
            )
        return self
