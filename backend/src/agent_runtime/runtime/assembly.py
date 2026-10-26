"""从配置装配运行时参数。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from aegis_config import Settings
from agent_runtime.approval import check_binding, compute_approval_id
from agent_runtime.context import (
    FoldPolicy,
    ObservationLimits,
    PromptAssembler,
    PromptAssets,
    PromptLibrary,
    RulesCache,
    RulesResult,
)
from agent_runtime.core import PermissionLevel
from agent_runtime.core.state import PERMISSION_LEVELS
from agent_runtime.guardrails import BudgetLimits, LoopLimits, PermissionPolicy
from agent_runtime.loop.stages import ApprovalHooks
from agent_runtime.memory import (
    CompactionConfig,
    Extractor,
    MemoryStore,
    MergeLimits,
    SessionCompactor,
)
from agent_runtime.runtime.errors import FullPermissionsNotConfirmedError
from agent_runtime.runtime.memory_flow import MemoryFlow
from agent_runtime.skills import SkillLimits, SkillRegistry, SkillRoot, skill_roots
from tools.builtin import FileLimits


def budget_limits_from_settings(settings: Settings) -> BudgetLimits:
    g = settings.runtime.guardrails
    return BudgetLimits(
        max_steps=g.max_steps,
        max_total_tokens=g.max_total_tokens,
        max_wall_time_sec=g.max_wall_time_sec,
        warning_ratio=g.warning_ratio,
    )


#: tool_runner 的审批协议：``loop`` 与 ``approval`` 是同层模块、不能互相导入，由这一层把两者接上
APPROVAL_HOOKS = ApprovalHooks(approval_id=compute_approval_id, check_binding=check_binding)


def permission_policy_from_settings(settings: Settings) -> PermissionPolicy:
    p = settings.permissions
    return PermissionPolicy(
        read_only_tools=frozenset(p.read_only_tools),
        workspace_write_tools=frozenset(p.workspace_write_tools),
        full_permission_tools=frozenset(p.full_permission_tools),
        bash_tools=frozenset(p.bash_tools),
        full_permission_patterns=tuple(p.full_permission_patterns),
        workspace_write_patterns=tuple(p.workspace_write_patterns),
        unknown_tool_level=p.unknown_tool_level,
    )


def loop_limits_from_settings(settings: Settings) -> LoopLimits:
    g = settings.runtime.guardrails
    return LoopLimits(
        consecutive_errors_limit=g.consecutive_errors_limit,
        identical_fingerprint_limit=g.identical_fingerprint_limit,
    )


def observation_limits_from_settings(settings: Settings) -> ObservationLimits:
    o = settings.context.observation
    return ObservationLimits(
        max_tokens=o.max_tokens,
        head_lines=o.head_lines,
        tail_lines=o.tail_lines,
        keyword_lines_limit=o.keyword_lines_limit,
        json_outline_max_depth=o.json_outline_max_depth,
        json_outline_array_items=o.json_outline_array_items,
        scalar_preview_chars=o.scalar_preview_chars,
        exempt_tools=tuple(o.exempt_tools),
        exempt_multiplier=o.exempt_multiplier,
    )


def file_limits_from_settings(settings: Settings) -> FileLimits:
    f = settings.tools.files
    return FileLimits(
        view_max_lines=f.view_max_lines,
        view_max_file_bytes=f.view_max_file_mb * 1024 * 1024,
        write_max_bytes=f.write_max_bytes,
    )


def skill_limits_from_settings(settings: Settings) -> SkillLimits:
    k = settings.skills
    return SkillLimits(
        max_description_chars=k.max_description_chars,
        max_triggers=k.max_triggers,
        high_privilege_tools=frozenset(k.high_privilege_tools),
        protect_trusted_names=k.protect_trusted_names,
        scan_untrusted_body=k.scan_untrusted_body,
    )


#: 随发行版的内置技能（``src/skills``）
_BUILTIN_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def skill_roots_from_settings(
    settings: Settings, workspace_root: Path, *, global_dir: Path | None = None
) -> tuple[SkillRoot, ...]:
    k = settings.skills
    return skill_roots(
        builtin_dir=_BUILTIN_SKILLS_DIR,
        global_dir=global_dir or Path.home() / ".aegis" / "skills",
        workspace_root=workspace_root,
        allow_builtin=k.allow_builtin,
        allow_global=k.allow_global,
        allow_workspace=k.allow_workspace,
    )


def rules_from_settings(settings: Settings, workspace_root: Path, cache: RulesCache) -> RulesResult:
    r = settings.context.rules
    return cache.load(
        workspace_root, allow=r.allow_workspace, candidates=r.candidates, max_chars=r.max_chars
    )


def build_prompt_assets(
    library: PromptLibrary,
    *,
    workspace_name: str,
    workspace_description: str = "",
    rules: RulesResult | None = None,
    skills: SkillRegistry | None = None,
    tool_names: Iterable[str] = (),
) -> PromptAssets:
    """任务启动时一次装好的资产；记忆与环境自检由调用方用 ``dataclasses.replace`` 补上。"""
    return PromptAssets(
        system=library.get("system"),
        workspace_name=workspace_name,
        workspace_description=workspace_description,
        rules=rules.rules if rules else None,
        rules_status=rules.status if rules else "absent",
        skills_manifest=skills.manifest() if skills else "",
        tool_names=tuple(sorted(tool_names)),
    )


def fold_policy_from_settings(settings: Settings, context_window: int) -> FoldPolicy:
    """``context_window``：承载任务内往返的那一档模型的窗口（取各端点里最小的更稳妥）。"""
    t = settings.context.task
    return FoldPolicy(
        trigger_tokens=t.soft_trigger(context_window),
        keep_recent_steps=t.fold_keep_recent_steps,
        exempt_tools=frozenset(t.fold_exempt_tools),
    )


def assembler_from_settings(
    settings: Settings,
    assets: PromptAssets,
    count_tokens: Callable[[str], int] = len,
    *,
    context_window: int | None = None,
) -> PromptAssembler:
    fold = fold_policy_from_settings(settings, context_window) if context_window else None
    return PromptAssembler(
        assets,
        memory_role=settings.context.prompt.memory_role,
        count_tokens=count_tokens,
        fold=fold,
    )


def compaction_config_from_settings(settings: Settings) -> CompactionConfig:
    s, m = settings.context.session, settings.context.memory
    return CompactionConfig(
        token_limit=s.token_limit,
        high_watermark=s.high_watermark,
        compaction_ratio=s.compaction_ratio,
        keep_recent_turns=s.active_window_turns,
        backoff_sec=tuple(s.compaction_backoff_sec),
        limits=MergeLimits(
            max_facts=m.max_facts,
            max_failures=m.max_failed_attempts,
            summary_max_chars=m.summary_max_chars,
            require_provenance=m.require_provenance,
        ),
    )


def memory_flow_from_settings(
    settings: Settings,
    store: MemoryStore,
    extractor: Extractor,
    count_tokens: Callable[[str], int],
) -> MemoryFlow:
    m = settings.context.memory
    config = compaction_config_from_settings(settings)
    return MemoryFlow(
        store,
        SessionCompactor(store, extractor, config),
        active_window_turns=settings.context.session.active_window_turns,
        max_pending=m.max_pending_facts,
        persist_task_facts=m.persist_task_facts,
        count_tokens=count_tokens,
    )


def max_concurrent_tasks(settings: Settings) -> int:
    return settings.server.max_concurrent_tasks


def resolve_permission_level(
    settings: Settings, requested: str | None, *, confirmed: bool
) -> PermissionLevel:
    """任务的权限基线：取请求值，缺省用配置的基线。

    ``full_permissions`` 需要显式确认（可由配置关闭这一要求）；基线被配置成 full_permissions
    时，不给请求值也不能绕过确认。
    """
    level = requested or settings.runtime.approval.default_level
    if level not in PERMISSION_LEVELS:
        raise ValueError(f"permission_level 不合法：{level!r}，可选 {list(PERMISSION_LEVELS)}")
    if (
        level == "full_permissions"
        and settings.runtime.approval.require_full_permissions_confirm
        and not confirmed
    ):
        raise FullPermissionsNotConfirmedError("以 full_permissions 提交任务需要显式确认")
    return level  # type: ignore[return-value]
