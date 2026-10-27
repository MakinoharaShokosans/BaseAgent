"""从配置装配运行时参数：预算上限、并发名额、权限基线。出处：docs/01_architecture/04_configuration.md。"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

import pytest

from aegis_config import Settings
from agent_runtime.guardrails import BudgetLimits
from agent_runtime.runtime import (
    FullPermissionsNotConfirmedError,
    budget_limits_from_settings,
    max_concurrent_tasks,
    resolve_permission_level,
)


@pytest.fixture
def raw(config_path: Path) -> dict[str, Any]:
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def settings_with(raw: dict[str, Any], section: str, key: str, value: Any) -> Settings:
    data = copy.deepcopy(raw)
    data[section][key] = value
    return Settings.model_validate(data)


@pytest.mark.unit
def test_budget_limits_follow_the_reference_config(raw: dict[str, Any]) -> None:
    limits = budget_limits_from_settings(Settings.model_validate(raw))
    assert limits == BudgetLimits(
        max_steps=25, max_total_tokens=200_000, max_wall_time_sec=900, warning_ratio=0.9
    )


@pytest.mark.unit
def test_budget_limits_follow_overrides(raw: dict[str, Any]) -> None:
    data = copy.deepcopy(raw)
    data["runtime"]["guardrails"].update(max_steps=40, max_total_tokens=5000, warning_ratio=0.8)
    limits = budget_limits_from_settings(Settings.model_validate(data))
    assert (limits.max_steps, limits.max_total_tokens, limits.warning_ratio) == (40, 5000, 0.8)


@pytest.mark.unit
def test_concurrency_slots_come_from_the_server_section(raw: dict[str, Any]) -> None:
    assert max_concurrent_tasks(Settings.model_validate(raw)) == 1
    assert max_concurrent_tasks(settings_with(raw, "server", "max_concurrent_tasks", 3)) == 3


@pytest.mark.unit
def test_permission_defaults_to_the_configured_baseline(raw: dict[str, Any]) -> None:
    settings = Settings.model_validate(raw)
    assert resolve_permission_level(settings, None, confirmed=False) == "workspace_write"


@pytest.mark.unit
def test_permission_baseline_is_configurable(raw: dict[str, Any]) -> None:
    data = copy.deepcopy(raw)
    data["runtime"]["approval"]["default_level"] = "read_only"
    settings = Settings.model_validate(data)
    assert resolve_permission_level(settings, None, confirmed=False) == "read_only"


@pytest.mark.unit
@pytest.mark.parametrize("level", ["read_only", "workspace_write"])
def test_lower_levels_need_no_confirmation(raw: dict[str, Any], level: str) -> None:
    assert resolve_permission_level(Settings.model_validate(raw), level, confirmed=False) == level


@pytest.mark.unit
def test_full_permissions_needs_explicit_confirmation(raw: dict[str, Any]) -> None:
    settings = Settings.model_validate(raw)
    with pytest.raises(FullPermissionsNotConfirmedError):
        resolve_permission_level(settings, "full_permissions", confirmed=False)
    assert (
        resolve_permission_level(settings, "full_permissions", confirmed=True) == "full_permissions"
    )


@pytest.mark.unit
def test_confirmation_requirement_can_be_switched_off(raw: dict[str, Any]) -> None:
    data = copy.deepcopy(raw)
    data["runtime"]["approval"]["require_full_permissions_confirm"] = False
    settings = Settings.model_validate(data)
    assert (
        resolve_permission_level(settings, "full_permissions", confirmed=False)
        == "full_permissions"
    )


@pytest.mark.unit
def test_unknown_levels_are_rejected(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="root"):
        resolve_permission_level(Settings.model_validate(raw), "root", confirmed=True)


@pytest.mark.unit
def test_a_default_of_full_permissions_still_needs_confirmation(raw: dict[str, Any]) -> None:
    """基线被配置成 full_permissions 时，不给请求值也不能绕过确认。"""
    data = copy.deepcopy(raw)
    data["runtime"]["approval"]["default_level"] = "full_permissions"
    settings = Settings.model_validate(data)
    with pytest.raises(FullPermissionsNotConfirmedError):
        resolve_permission_level(settings, None, confirmed=False)


# ==============================================================================
# 工具阶段的配置装配
# ==============================================================================


@pytest.mark.unit
def test_the_permission_policy_follows_the_reference_config(raw: dict[str, Any]) -> None:
    from agent_runtime.guardrails import check_permission
    from agent_runtime.runtime import permission_policy_from_settings

    policy = permission_policy_from_settings(Settings.model_validate(raw))
    assert "view_file" in policy.read_only_tools
    assert "write_file" in policy.workspace_write_tools
    assert policy.bash_tools == frozenset({"bash"})
    assert policy.unknown_tool_level == "full_permissions"
    # 参考配置里的正则是单引号字面量，装配后仍能判定
    assert (
        check_permission(policy, "workspace_write", "bash", {"command": "git push"}).allowed
        is False
    )
    assert (
        check_permission(policy, "workspace_write", "bash", {"command": "pytest -q"}).allowed
        is True
    )
    assert check_permission(policy, "read_only", "mcp_new_tool", {}).allowed is False


@pytest.mark.unit
def test_loop_limits_follow_the_reference_config(raw: dict[str, Any]) -> None:
    from agent_runtime.guardrails import LoopLimits
    from agent_runtime.runtime import loop_limits_from_settings

    assert loop_limits_from_settings(Settings.model_validate(raw)) == LoopLimits(
        consecutive_errors_limit=3,
        identical_fingerprint_limit=3,
        alternation_cycles=3,
        replan_interval=6,
    )


@pytest.mark.unit
def test_observation_limits_follow_the_reference_config(raw: dict[str, Any]) -> None:
    from agent_runtime.context import ObservationLimits
    from agent_runtime.runtime import observation_limits_from_settings

    limits = observation_limits_from_settings(Settings.model_validate(raw))
    assert limits == ObservationLimits(
        max_tokens=1500,
        head_lines=20,
        tail_lines=30,
        keyword_lines_limit=30,
        json_outline_max_depth=3,
        json_outline_array_items=5,
        scalar_preview_chars=120,
        exempt_tools=("load_skill",),
        exempt_multiplier=8,
    )


@pytest.mark.unit
def test_the_real_approval_hooks_are_wired() -> None:
    from agent_runtime.approval import check_binding, compute_approval_id
    from agent_runtime.runtime import APPROVAL_HOOKS

    assert APPROVAL_HOOKS.approval_id is compute_approval_id
    assert APPROVAL_HOOKS.check_binding is check_binding


@pytest.mark.unit
def test_file_limits_follow_the_reference_config(raw: dict[str, Any]) -> None:
    from agent_runtime.runtime import file_limits_from_settings
    from tools.builtin import FileLimits

    assert file_limits_from_settings(Settings.model_validate(raw)) == FileLimits(
        view_max_lines=400, view_max_file_bytes=10 * 1024 * 1024, write_max_bytes=1_000_000
    )


@pytest.mark.unit
def test_skill_limits_follow_the_reference_config(raw: dict[str, Any]) -> None:
    from agent_runtime.runtime import skill_limits_from_settings
    from agent_runtime.skills import SkillLimits

    assert skill_limits_from_settings(Settings.model_validate(raw)) == SkillLimits(
        max_description_chars=200,
        max_triggers=12,
        high_privilege_tools=frozenset({"bash", "write_file", "edit_file"}),
        protect_trusted_names=True,
        scan_untrusted_body=True,
    )


@pytest.mark.unit
def test_skill_roots_follow_the_source_switches(raw: dict[str, Any], tmp_path: Path) -> None:
    from agent_runtime.runtime import skill_roots_from_settings

    settings = Settings.model_validate(raw)
    default = skill_roots_from_settings(settings, tmp_path, global_dir=tmp_path / "g")
    assert [r.source for r in default] == ["builtin", "global"]
    assert default[0].path.name == "skills" and default[1].path == tmp_path / "g"

    data = copy.deepcopy(raw)
    data["skills"]["allow_workspace"] = True
    opened = skill_roots_from_settings(Settings.model_validate(data), tmp_path)
    assert next(iter(opened)).source == "workspace"
    assert opened[0].path == tmp_path / ".aegis" / "skills"


@pytest.mark.unit
def test_workspace_rules_stay_off_unless_the_config_opts_in(
    raw: dict[str, Any], tmp_path: Path
) -> None:
    from agent_runtime.context import RulesCache
    from agent_runtime.runtime import rules_from_settings

    (tmp_path / "AGENTS.md").write_text("规则")
    assert (
        rules_from_settings(Settings.model_validate(raw), tmp_path, RulesCache()).status
        == "disabled"
    )
    data = copy.deepcopy(raw)
    data["context"]["rules"]["allow_workspace"] = True
    opened = rules_from_settings(Settings.model_validate(data), tmp_path, RulesCache())
    assert opened.status == "enabled" and opened.rules is not None


@pytest.mark.unit
def test_prompt_assets_and_the_assembler_follow_the_config(raw: dict[str, Any]) -> None:
    from agent_runtime.context import PromptLibrary
    from agent_runtime.runtime import assembler_from_settings, build_prompt_assets

    assets = build_prompt_assets(
        PromptLibrary(), workspace_name="p", tool_names=["view_file", "bash"]
    )
    assert assets.tool_names == ("bash", "view_file") and "数据隔离" in assets.system
    assert assets.rules_status == "absent" and assets.skills_manifest == ""

    default = assembler_from_settings(Settings.model_validate(raw), assets)
    data = copy.deepcopy(raw)
    data["context"]["prompt"]["memory_role"] = "system"
    switched = assembler_from_settings(Settings.model_validate(data), assets)
    assert default._memory_role == "user" and switched._memory_role == "system"  # type: ignore[reportPrivateUsage]


@pytest.mark.unit
def test_compaction_config_follows_the_reference_config(raw: dict[str, Any]) -> None:
    from agent_runtime.memory import CompactionConfig, MergeLimits
    from agent_runtime.runtime import compaction_config_from_settings

    assert compaction_config_from_settings(Settings.model_validate(raw)) == CompactionConfig(
        token_limit=32000,
        high_watermark=0.8,
        compaction_ratio=0.4,
        keep_recent_turns=3,
        backoff_sec=(30, 120, 600),
        limits=MergeLimits(
            max_facts=50, max_failures=30, summary_max_chars=300, require_provenance=True
        ),
    )


@pytest.mark.unit
async def test_the_memory_flow_is_built_from_the_memory_section(
    raw: dict[str, Any], tmp_path: Path
) -> None:
    from agent_runtime.memory import MemoryStore
    from agent_runtime.runtime import memory_flow_from_settings

    async def no_extractor(request: Any) -> None:
        return None

    store = MemoryStore(tmp_path / "m.db")
    data = copy.deepcopy(raw)
    data["context"]["memory"].update(max_pending_facts=7, persist_task_facts=False)
    data["context"]["session"]["active_window_turns"] = 5
    flow = memory_flow_from_settings(Settings.model_validate(data), store, no_extractor, len)
    assert (flow._max_pending, flow._persist, flow._window) == (7, False, 5)  # type: ignore[reportPrivateUsage]


@pytest.mark.unit
def test_the_fold_policy_follows_the_task_section_and_takes_the_smaller_trigger(
    raw: dict[str, Any],
) -> None:
    from agent_runtime.context import FoldPolicy
    from agent_runtime.runtime import fold_policy_from_settings

    s = Settings.model_validate(raw)
    assert fold_policy_from_settings(s, 128_000) == FoldPolicy(76_800, 3, frozenset({"load_skill"}))
    assert fold_policy_from_settings(s, 1_000_000).trigger_tokens == 100_000  # 绝对上限生效


@pytest.mark.unit
def test_the_assembler_folds_only_when_a_window_is_given(raw: dict[str, Any]) -> None:
    from agent_runtime.context import PromptAssets
    from agent_runtime.runtime import assembler_from_settings

    assets = PromptAssets(system="s", workspace_name="w")
    s = Settings.model_validate(raw)
    assert assembler_from_settings(s, assets)._fold is None  # type: ignore[reportPrivateUsage]
    folded = assembler_from_settings(s, assets, context_window=128_000)
    assert folded._fold is not None  # type: ignore[reportPrivateUsage]
